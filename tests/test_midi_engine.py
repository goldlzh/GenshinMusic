"""纯 MIDI 与虚拟键盘/时钟回归测试，不向桌面发送输入。"""

import unittest
from dataclasses import FrozenInstanceError

import mido

from midi_engine import (
    MidiNote, KeyboardPlayer, build_tempo_map, extract_track_notes,
    finalize_notes, build_schedule, performance_duration, find_best_transpose,
    count_playable, KEY_RETRIGGER_GAP,
    build_unavailable_notes,
)


def midi_note(start=0, end=1, pitch=60, melody=True, channel=0, track=0, pedal_end=0):
    return MidiNote(start, end, pitch, channel, track, melody, pedal_end)


def prepare(notes, mode='score', pedal=False, fold=False):
    return finalize_notes(notes, 0, fold, log=lambda _: None,
                          duration_mode=mode, sustain_pedal=pedal)


def extract(*tracks):
    mid = mido.MidiFile(ticks_per_beat=480)
    mid.tracks.extend(mido.MidiTrack(track) for track in tracks)
    return extract_track_notes(mid, build_tempo_map(mid), mid.ticks_per_beat, log=lambda _: None)


class MidiParsingTests(unittest.TestCase):
    def test_duration_crosses_tempo_change_and_accepts_zero_velocity_off(self):
        tracks = extract([
            mido.Message('note_on', note=60, velocity=80, time=240),
            mido.MetaMessage('set_tempo', tempo=1000000, time=240),
            mido.Message('note_on', note=60, velocity=0, time=480),
        ])
        note = tracks[0][1][0]
        self.assertAlmostEqual(note.start, 0.25)
        self.assertAlmostEqual(note.end, 1.5)
        self.assertAlmostEqual(note.end - note.start, 1.25)

    def test_repeated_overlapping_notes_are_paired_in_order(self):
        notes = extract([
            mido.Message('note_on', note=60, time=0),
            mido.Message('note_on', note=60, time=120),
            mido.Message('note_off', note=60, time=120),
            mido.Message('note_off', note=60, time=120),
        ])[0][1]
        self.assertEqual([(n.start, n.end) for n in notes], [(0, .25), (.125, .375)])

    def test_channel_and_track_identity_are_preserved(self):
        tracks = extract([
            mido.Message('note_on', note=60, channel=0),
            mido.Message('note_on', note=60, channel=1),
            mido.Message('note_on', note=36, channel=9),
            mido.Message('note_off', note=60, channel=1, time=240),
            mido.Message('note_off', note=60, channel=0, time=240),
        ])
        notes = tracks[0][1]
        self.assertEqual(len(notes), 2)
        self.assertEqual([(n.channel, n.end) for n in notes], [(0, .5), (1, .25)])

    def test_off_and_pedal_can_arrive_on_another_track(self):
        tracks = extract([
            mido.Message('note_on', note=60),
        ], [
            mido.Message('control_change', control=64, value=127),
            mido.Message('note_off', note=60, time=240),
            mido.Message('control_change', control=64, value=0, time=240),
        ])
        note = tracks[0][1][0]
        self.assertEqual(note.track, 0)
        self.assertEqual(note.end, .25)
        self.assertEqual(note.pedal_end, .5)

    def test_missing_note_off_has_bounded_fallback(self):
        note = extract([
            mido.Message('note_on', note=60),
            mido.MetaMessage('end_of_track', time=48000),
        ])[0][1][0]
        self.assertEqual(note.end, .25)
        self.assertEqual(note.pedal_end, .25)

    def test_unreleased_pedal_ends_at_file_end(self):
        note = extract([
            mido.Message('control_change', control=64, value=127),
            mido.Message('note_on', note=60),
            mido.Message('note_off', note=60, time=240),
            mido.MetaMessage('end_of_track', time=240),
        ])[0][1][0]
        self.assertEqual(note.end, .25)
        self.assertEqual(note.pedal_end, .5)


class ArrangementTests(unittest.TestCase):
    def test_three_modes_keep_independent_chord_durations(self):
        notes = [midi_note(end=1, melody=True), midi_note(end=.5, pitch=64, melody=False)]
        expected = {'fixed': {'A': .04, 'D': .04},
                    'score': {'A': 1, 'D': .5}, 'melody': {'A': 1, 'D': .04}}
        for mode, durations in expected.items():
            with self.subTest(mode=mode):
                actual = {n.key: n.end - n.start for n in build_schedule(prepare(notes, mode))}
                self.assertEqual(actual, durations)

    def test_pedal_is_opt_in_and_only_extends_sustained_voices(self):
        notes = [midi_note(end=.2, pedal_end=1),
                 midi_note(end=.2, pedal_end=2, pitch=64, melody=False)]
        off = build_schedule(prepare(notes))
        self.assertEqual([n.end for n in off], [.2, .2])
        score = build_schedule(prepare(notes, pedal=True))
        self.assertEqual([n.end for n in score], [1, 2])
        melody = build_schedule(prepare(notes, 'melody', pedal=True))
        self.assertEqual([n.end for n in melody], [1, .04])

    def test_speed_scales_offsets_and_durations_without_reordering(self):
        notes = [midi_note(0, .2, 60), midi_note(.06, .26, 62), midi_note(.09, .29, 64)]
        result = build_schedule(prepare(notes), 2)
        for note, start in zip(result, [0, .03, .045]):
            self.assertAlmostEqual(note.start, start)
            self.assertAlmostEqual(note.end - note.start, .1)

    def test_minimum_hold_is_applied_after_speed_scaling(self):
        short = prepare([midi_note(end=.02)])
        self.assertAlmostEqual(build_schedule(short, .5)[0].end, .04)
        self.assertAlmostEqual(build_schedule(short, 2)[0].end, .04)
        fixed = prepare([midi_note(end=1)], 'fixed')
        self.assertAlmostEqual(build_schedule(fixed, .5)[0].end, .04)

    def test_repeated_key_within_old_chord_window_is_retriggered(self):
        notes = prepare([midi_note(0, 1), midi_note(.06, 1.06)])
        result = build_schedule(notes)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0].end, .05)
        self.assertAlmostEqual(result[1].start - result[0].end, KEY_RETRIGGER_GAP)
        faster = build_schedule(notes, 2)
        self.assertAlmostEqual(faster[1].start, .03)
        self.assertAlmostEqual(faster[1].start - faster[0].end, KEY_RETRIGGER_GAP)

    def test_simultaneous_same_key_merges_and_uses_latest_release(self):
        result = build_schedule(prepare([midi_note(end=1), midi_note(end=2, melody=False)]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].end, 2)
        self.assertTrue(result[0].melody)

    def test_folded_accompaniment_cannot_interrupt_sustained_melody(self):
        # 84 折叠到 72，与当前主旋律共享 Q 键。
        result = build_schedule(prepare([
            midi_note(0, 2, 72), midi_note(.3, 1, 84, melody=False),
            midi_note(.6, 1.2, 72),
        ], fold=True))
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0].end, .59)
        self.assertAlmostEqual(result[1].start, .6)

    def test_silence_compression_protects_overlapping_long_notes(self):
        notes = build_schedule(prepare([midi_note(2, 5), midi_note(4.5, 4.8, 64), midi_note(7, 7.5, 67)]))
        self.assertEqual([(n.start, n.end) for n in notes], [(0, 3), (2.5, 2.8), (4, 4.5)])

    def test_silence_is_recomputed_after_a_long_note_is_cut_by_retrigger(self):
        result = build_schedule(prepare([midi_note(0, 10), midi_note(1, 2), midi_note(12, 12.5, 64)]))
        self.assertAlmostEqual(result[0].end, .99)
        self.assertAlmostEqual(result[-1].start, 3)
        self.assertAlmostEqual(result[-1].end, 3.5)

    def test_duration_includes_last_release_not_last_onset(self):
        notes = prepare([midi_note(0, 6), midi_note(1, 1.5, 64)])
        self.assertEqual(performance_duration(notes), 6)
        self.assertEqual(performance_duration(notes, 2), 3)

    def test_folded_recommendation_uses_reachable_notes(self):
        notes = [midi_note(pitch=24), midi_note(1, 2, 28), midi_note(2, 3, 31)]
        no_fold = find_best_transpose(notes, False)
        fold = find_best_transpose(notes, True)
        self.assertEqual(count_playable(notes, no_fold, False), (0, 0))
        self.assertEqual(count_playable(notes, fold, True), (3, 0))

    def test_transpose_ties_prefer_zero_when_no_folding_is_needed(self):
        notes = [midi_note(pitch=pitch, start=i, end=i+.5) for i, pitch in enumerate([60, 64, 67])]
        for fold in (False, True):
            self.assertEqual(find_best_transpose(notes, fold), 0)

    def test_invalid_speed_is_rejected(self):
        for speed in (0, -1, float('nan'), float('inf')):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                build_schedule(prepare([midi_note()]), speed)


class ChordDurationTests(unittest.TestCase):
    def test_only_chord_accompaniment_is_sustained(self):
        tagged = [midi_note(0, 1, 72), midi_note(0, .8, 48, False), midi_note(0, .6, 52, False),
                  midi_note(0, .4, 55, False), midi_note(.5, 1.5, 50, False)]
        result = {note.key: note for note in build_schedule(prepare(tagged, 'chord'))}
        for key, duration in {'Q': .04, 'Z': .8, 'C': .6, 'B': .4, 'X': .04}.items():
            self.assertAlmostEqual(result[key].end - result[key].start, duration)

    def test_melody_plus_one_accompaniment_note_is_not_a_sustained_chord(self):
        result = build_schedule(prepare([midi_note(0, 1), midi_note(0, 1, 48, False)], 'chord'))
        self.assertEqual([note.end for note in result], [.04, .04])

    def test_arpeggio_and_single_note_bass_remain_short(self):
        result = build_schedule(prepare([midi_note(i*.08, 1, pitch, False)
                                         for i, pitch in enumerate((48, 52, 55, 60))], 'chord'))
        for note in result:
            self.assertAlmostEqual(note.end - note.start, .04)

    def test_near_simultaneous_group_does_not_chain_and_keeps_original_offsets_at_any_speed(self):
        tagged = [midi_note(0, 1, 48, False), midi_note(.01, 1.01, 52, False), midi_note(.02, 1.02, 55, False)]
        for speed in (.5, 1, 2):
            with self.subTest(speed=speed):
                result = build_schedule(prepare(tagged, 'chord'), speed)
                self.assertAlmostEqual(result[1].start, .01 / speed)
                self.assertAlmostEqual(result[0].end, 1 / speed)
                self.assertAlmostEqual(result[1].end - result[1].start, 1 / speed)
                self.assertAlmostEqual(result[2].end - result[2].start, .04)

    def test_octave_folding_duplicates_do_not_form_a_two_key_chord(self):
        tagged = [midi_note(0, 1, 72, False), midi_note(0, 2, 84, False)]
        result = build_schedule(prepare(tagged, 'chord', fold=True))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].end, .04)

    def test_unavailable_note_cannot_turn_a_solo_key_into_a_chord(self):
        tagged = [midi_note(0, 1, 48, False), midi_note(0, 1, 49, False)]
        result = build_schedule(prepare(tagged, 'chord'))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].end, .04)

    def test_unavailable_onset_cannot_break_a_playable_chord_group(self):
        tagged = [midi_note(0, 1, 49, False), midi_note(.012, 1, 48, False), midi_note(.023, 1, 52, False)]
        result = prepare(tagged, 'chord')
        self.assertTrue(all(note.sustained for note in result))

    def test_shared_melody_key_stays_short_beside_other_chord_tones(self):
        tagged = [midi_note(0, 1, 60), midi_note(0, 2, 60, False), midi_note(0, 2, 64, False)]
        result = {note.key: note for note in build_schedule(prepare(tagged, 'chord'))}
        self.assertEqual(result['A'].end, .04)
        self.assertEqual(result['D'].end, 2)

    def test_pedal_only_extends_qualifying_chords_and_red_notes_follow_same_mode(self):
        tagged = [midi_note(0, .2, 72, pedal_end=2), midi_note(0, .2, 48, False, pedal_end=1),
                  midi_note(0, .2, 52, False, pedal_end=1.5), midi_note(0, .2, 49, False, pedal_end=1),
                  midi_note(0, .2, 73, pedal_end=1), midi_note(.5, .8, 50, False, pedal_end=1.5)]
        playable = prepare(tagged, 'chord', pedal=True)
        result = {note.key: note for note in build_schedule(playable)}
        self.assertEqual(result['Q'].end, .04)
        self.assertEqual(result['Z'].end, 1)
        self.assertEqual(result['C'].end, 1.5)
        self.assertAlmostEqual(result['X'].end - result['X'].start, .04)
        red = build_unavailable_notes(tagged, 0, False, playable, duration_mode='chord', sustain_pedal=True)
        self.assertEqual([(note.pitch, note.end) for note in red], [(49, 1), (73, .04)])


class UnavailableNoteTests(unittest.TestCase):
    def rejected(self, tagged, fold=False, speed=1, mode='score', pedal=False, offset=0):
        playable = finalize_notes(tagged, offset, fold, log=lambda _: None,
                                   duration_mode=mode, sustain_pedal=pedal)
        return build_unavailable_notes(tagged, offset, fold, playable, speed, mode, pedal)

    def test_outside_range_and_accidentals_are_kept_until_they_can_be_mapped(self):
        tagged = [midi_note(pitch=36), midi_note(pitch=60), midi_note(pitch=61), midi_note(pitch=84)]
        off = self.rejected(tagged)
        self.assertEqual([(n.pitch, n.reason) for n in off], [(36, '超出音域'), (61, '无对应琴键'), (84, '超出音域')])
        self.assertEqual([n.pitch for n in self.rejected(tagged, fold=True)], [61])
        self.assertEqual(self.rejected([midi_note(pitch=61)], offset=-1), [])

    def test_outside_accidentals_are_folded_for_display_but_remain_unavailable(self):
        note = self.rejected([midi_note(pitch=97)], fold=True)[0]
        self.assertEqual(note.pitch, 73)
        self.assertEqual(note.reason, '无对应琴键')

    def test_rejected_notes_align_with_compressed_silence_and_speed(self):
        tagged = [midi_note(2, 3), midi_note(5, 6, 61), midi_note(10, 11, 64), midi_note(10, 11, 85)]
        normal = self.rejected(tagged)
        fast = self.rejected(tagged, speed=2)
        self.assertAlmostEqual(normal[0].start, 1 + 2 / 7)
        self.assertAlmostEqual(normal[0].end, 1 + 3 / 7)
        self.assertEqual((normal[1].start, normal[1].end), (2, 3))
        for old, new in zip(normal, fast):
            self.assertAlmostEqual(new.start, old.start / 2)
            self.assertAlmostEqual(new.end, old.end / 2)

    def test_original_intro_and_tail_remain_browsable_without_extending_playback(self):
        tagged = [midi_note(0, 1, 36), midi_note(5, 6), midi_note(10, 11, 84)]
        playable = prepare(tagged)
        rejected = self.rejected(tagged)
        self.assertEqual([(n.start, n.end) for n in rejected], [(-5, -4), (5, 6)])
        self.assertEqual(performance_duration(playable), 1)

    def test_all_unavailable_song_still_has_a_preview(self):
        rejected = self.rejected([midi_note(10, 11, 36), midi_note(15, 16, 84)])
        self.assertEqual([(n.start, n.end) for n in rejected], [(0, 1), (5, 6)])

    def test_duration_mode_and_pedal_apply_to_visual_duration(self):
        tagged = [midi_note(0, 2), midi_note(0, .2, 61, pedal_end=1),
                  midi_note(0, .2, 63, melody=False, pedal_end=1)]
        self.assertEqual([n.end for n in self.rejected(tagged, mode='fixed', pedal=True)], [.04, .04])
        self.assertEqual([n.end for n in self.rejected(tagged, mode='score', pedal=True)], [1, 1])
        self.assertEqual([n.end for n in self.rejected(tagged, mode='melody', pedal=True)], [1, .04])

    def test_rejected_alignment_uses_conflict_resolved_long_notes(self):
        tagged = [midi_note(0, 10), midi_note(1, 2), midi_note(7, 8, 61), midi_note(12, 13, 64)]
        rejected = self.rejected(tagged)
        self.assertAlmostEqual(rejected[0].start, 2.5)
        self.assertAlmostEqual(rejected[0].end, 2.6)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.hook = None

    def __call__(self):
        return self.now

    def wait(self, delay):
        self.now += delay
        if self.hook:
            self.hook(self.now)
        if self.now > 20:
            raise AssertionError('播放器未在预计时间内结束')


class FakeKeyboard:
    def __init__(self, clock):
        self.clock, self.held, self.events = clock, set(), []

    def keyDown(self, key):
        self.events.append(('down', key, self.clock()))
        self.held.add(key)

    def keyUp(self, key):
        if key in self.held:
            self.events.append(('up', key, self.clock()))
        self.held.discard(key)


class PlayerTests(unittest.TestCase):
    def make_player(self, notes, start_ratio=0):
        self.clock = FakeClock()
        self.keyboard = FakeKeyboard(self.clock)
        self.finished, self.progress, self.errors = [], [], []
        return KeyboardPlayer(notes, 1, start_ratio,
                              lambda *args: self.progress.append(args), self.errors.append,
                              self.finished.append, keyboard=self.keyboard,
                              clock=self.clock, waiter=self.clock.wait)

    def test_other_notes_play_while_long_key_is_held_and_tail_completes(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.1, .4, 64)]))
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events],
                         [('down', 'a'), ('down', 'd'), ('up', 'd'), ('up', 'a')])
        self.assertAlmostEqual(self.keyboard.events[-1][2], 2)
        self.assertEqual(self.finished, [True])
        self.assertFalse(self.keyboard.held)
        self.assertEqual(self.progress[-1], (1, 2, 2))

    def test_retrigger_does_not_receive_old_notes_release(self):
        player = self.make_player(prepare([midi_note(0, 1), midi_note(.2, 2)]))
        player._run()
        events = self.keyboard.events
        self.assertEqual([event[:2] for event in events],
                         [('down', 'a'), ('up', 'a'), ('down', 'a'), ('up', 'a')])
        self.assertAlmostEqual(events[1][2], .19)
        self.assertAlmostEqual(events[2][2], .2)
        self.assertAlmostEqual(events[3][2], 2)

    def test_stop_releases_long_note_without_waiting_for_duration(self):
        player = self.make_player(prepare([midi_note(0, 10)]))
        self.clock.hook = lambda now: player.stop() if now >= .2 else None
        player._run()
        self.assertFalse(self.keyboard.held)
        self.assertLess(self.clock.now, .22)
        self.assertEqual(self.finished, [False])

    def test_pause_releases_and_resume_restores_remaining_duration(self):
        player = self.make_player(prepare([midi_note(0, 1)]))
        stage = [0]

        def hook(now):
            if stage[0] == 0 and now >= .2:
                player.pause_event.set()
                stage[0] = 1
            elif stage[0] == 1 and now >= .5:
                player.pause_event.clear()
                stage[0] = 2

        self.clock.hook = hook
        player._run()
        events = self.keyboard.events
        self.assertEqual([event[0] for event in events], ['down', 'up', 'down', 'up'])
        self.assertAlmostEqual(events[1][2], .2, delta=.01)
        self.assertAlmostEqual(events[2][2], .5, delta=.01)
        self.assertAlmostEqual(events[3][2], 1.3, delta=.015)

    def test_seek_skips_old_onsets_including_long_note_tails(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.3, .4, 64)]))
        jumped = [False]

        def hook(now):
            if now >= .2 and not jumped[0]:
                jumped[0] = True
                player.seek(.8)

        self.clock.hook = hook
        player._run()
        self.assertFalse(any(key == 'd' for _, key, _ in self.keyboard.events))
        self.assertEqual([action for action, _, _ in self.keyboard.events], ['down', 'up'])
        self.assertLess(self.clock.now, 1.45)
        self.assertEqual(self.finished, [True])

    def test_seek_while_paused_changes_resume_position(self):
        player = self.make_player(prepare([midi_note(0, 1)]))
        stage = [0]

        def hook(now):
            if stage[0] == 0 and now >= .2:
                player.pause_event.set()
                stage[0] = 1
            elif stage[0] == 1 and now >= .3:
                player.seek(.8)
                stage[0] = 2
            elif stage[0] == 2 and now >= .5:
                player.pause_event.clear()
                stage[0] = 3

        self.clock.hook = hook
        player._run()
        self.assertAlmostEqual(self.clock.now, .7, delta=.02)
        self.assertEqual(self.finished, [True])

    def test_start_inside_long_note_does_not_press_its_remaining_tail(self):
        player = self.make_player(prepare([midi_note(0, 2)]), start_ratio=.75)
        player._run()
        self.assertAlmostEqual(self.clock.now, .5)
        self.assertEqual(self.keyboard.events, [])

    def test_middle_start_ignores_old_chord_but_plays_note_at_exact_start(self):
        old_chord = [midi_note(0, 4, pitch) for pitch in (48, 50, 52, 53, 55, 57, 59, 60, 62, 64, 65, 67)]
        player = self.make_player(prepare(old_chord + [midi_note(2, 2.5, 72)]), start_ratio=.5)
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events], [('down', 'q'), ('up', 'q')])
        self.assertAlmostEqual(self.keyboard.events[0][2], 0)
        self.assertAlmostEqual(self.keyboard.events[1][2], .5)

    def test_start_boundary_tolerates_ratio_rounding_without_skipping_note_head(self):
        player = self.make_player(prepare([midi_note(0, 1.3), midi_note(.7, 1, 64)]), start_ratio=.7 / 1.3)
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events], [('down', 'd'), ('up', 'd')])

    def test_seek_only_plays_new_onsets_after_the_target(self):
        player = self.make_player(prepare([midi_note(0, 3), midi_note(.4, 2, 64), midi_note(1, 1.5, 67)]))
        jumped = []
        def jump(now):
            if now >= .2 and not jumped:
                jumped.append(player.seek(.8))
        self.clock.hook = jump
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events],
                         [('down', 'a'), ('up', 'a'), ('down', 'g'), ('up', 'g')])
        self.assertAlmostEqual(self.keyboard.events[2][2], .4, delta=.01)
        self.assertEqual(player.get_visual_state().seek_serial, jumped[0])

    def test_pause_after_middle_start_never_resurrects_ignored_tails(self):
        player = self.make_player(prepare([midi_note(0, 4), midi_note(2, 3, 64), midi_note(3.5, 4, 67)]), start_ratio=.5)
        stage = [0]
        def pause(now):
            if stage[0] == 0 and now >= .2:
                player.pause_event.set(); stage[0] = 1
            elif stage[0] == 1 and now >= .4:
                player.pause_event.clear(); stage[0] = 2
        self.clock.hook = pause
        player._run()
        self.assertEqual([key for action, key, _ in self.keyboard.events if action == 'down'], ['d', 'd', 'g'])

    def test_backward_seek_can_replay_later_heads_without_reviving_earlier_tail(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.15, .7, 64)]))
        jumped = []
        def jump(now):
            if now >= .2 and not jumped:
                jumped.append(player.seek(.1))
        self.clock.hook = jump
        player._run()
        self.assertEqual([key for action, key, _ in self.keyboard.events if action == 'down'], ['a', 'd', 'd'])

    def test_latest_queued_seek_wins_before_start_and_reports_its_serial(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.5, 1, 64), midi_note(1.5, 1.8, 67)]))
        self.assertEqual(player.seek(.5), 1)
        self.assertEqual(player.seek(1.5), 2)
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events], [('down', 'g'), ('up', 'g')])
        self.assertEqual(player.get_visual_state().seek_serial, 2)

    def test_seek_and_resume_together_do_not_restore_old_note(self):
        player = self.make_player(prepare([midi_note(0, 1)]))
        stage = [0]

        def hook(now):
            if stage[0] == 0 and now >= .2:
                player.pause_event.set()
                stage[0] = 1
            elif stage[0] == 1 and now >= .4:
                player.seek(.8)
                player.pause_event.clear()
                stage[0] = 2

        self.clock.hook = hook
        player._run()
        self.assertEqual([action for action, _, _ in self.keyboard.events], ['down', 'up'])

    def test_keyboard_error_releases_pressed_keys(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.2, .4, 64)]))
        original = self.keyboard.keyDown

        def fail_on_d(key):
            if key == 'd':
                raise OSError('simulated input failure')
            original(key)

        self.keyboard.keyDown = fail_on_d
        player._run()
        self.assertFalse(self.keyboard.held)
        self.assertEqual(self.finished, [False])
        self.assertTrue(self.errors)

    def test_visual_snapshot_reports_held_keys_and_keeps_short_trigger_hint(self):
        player = self.make_player(prepare([midi_note(0, 1), midi_note(.05, .09, 64)]))
        samples = []

        def observe(now):
            snapshot = player.get_visual_state()
            actual = {player.notes[i].key.lower() for i in snapshot.active_note_ids}
            self.assertEqual(actual, self.keyboard.held)
            samples.append((now, snapshot))

        self.clock.hook = observe
        initial = player.get_visual_state()
        self.assertEqual(initial.active_note_ids, ())
        player._run()
        both = next(snapshot for now, snapshot in samples if .07 < now < .085)
        after_short = next(snapshot for now, snapshot in samples if .12 < now < .14)
        self.assertEqual(both.active_note_ids, (0, 1))
        self.assertEqual(after_short.active_note_ids, (0,))
        self.assertIn(1, [i for i, when in after_short.recent_triggers])
        self.assertEqual(player.get_visual_state().phase, 'finished')
        self.assertEqual(player.get_visual_state().active_note_ids, ())
        self.assertEqual(initial.phase, 'ready')  # 已取出的对象不会被工作线程改写。
        with self.assertRaises(FrozenInstanceError):
            initial.position = 2

    def test_visual_pause_seek_and_resume_are_consistent_with_actual_keys(self):
        player = self.make_player(prepare([midi_note(0, 1), midi_note(.5, 1, 64)]))
        stage, samples = [0], []

        def observe(now):
            snapshot = player.get_visual_state()
            samples.append((now, snapshot))
            self.assertEqual({player.notes[i].key.lower() for i in snapshot.active_note_ids},
                             self.keyboard.held)
            if snapshot.phase == 'paused':
                self.assertEqual(snapshot.active_note_ids, ())
            if stage[0] == 0 and now >= .2:
                player.pause_event.set()
                stage[0] = 1
            elif stage[0] == 1 and now >= .4:
                player.seek(.8)
                player.pause_event.clear()
                stage[0] = 2

        self.clock.hook = observe
        player._run()
        paused = [snapshot for now, snapshot in samples if .25 < now < .35]
        self.assertTrue(paused)
        self.assertTrue(all(snapshot.phase == 'paused' for snapshot in paused))
        restored = next(snapshot for now, snapshot in samples if .44 < now < .46)
        self.assertEqual(restored.phase, 'playing')
        self.assertEqual(restored.active_note_ids, ())
        self.assertEqual(restored.seek_serial, 1)
        self.assertGreaterEqual(restored.position, .8)

    def test_visual_updates_while_progress_slider_is_being_dragged_and_clears_on_stop(self):
        player = self.make_player(prepare([midi_note(0, 2)]))
        player.suppress_progress = True
        observed = []

        def observe(now):
            if now >= .2:
                observed.append(player.get_visual_state())
                player.stop()

        self.clock.hook = observe
        player._run()
        self.assertEqual(self.progress, [])
        self.assertGreater(observed[0].position, .1)
        self.assertEqual(observed[0].active_note_ids, (0,))
        self.assertEqual(player.get_visual_state().phase, 'stopped')
        self.assertEqual(player.get_visual_state().active_note_ids, ())
        self.assertEqual(player.get_visual_state().recent_triggers, ())

    def test_failed_input_never_appears_as_a_pressed_note(self):
        player = self.make_player(prepare([midi_note(0, 1)]))

        def fail(key):
            raise OSError('simulated input failure')

        self.keyboard.keyDown = fail
        player._run()
        snapshot = player.get_visual_state()
        self.assertEqual(snapshot.phase, 'stopped')
        self.assertEqual(snapshot.active_note_ids, ())
        self.assertEqual(snapshot.recent_triggers, ())


if __name__ == '__main__':
    unittest.main()
