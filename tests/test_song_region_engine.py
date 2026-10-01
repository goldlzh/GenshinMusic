"""Song regions use the full playback timeline and fake keyboard/clock only."""
import unittest

from midi_engine import (
    KeyboardPlayer, PlaybackTimeline, ScheduledNote, build_unavailable_notes,
    finalize_notes, find_best_transpose, playback_region_to_source,
    select_tagged_region, source_region_to_playback,
)
from test_midi_engine import FakeClock, FakeKeyboard, midi_note, prepare


class RegionMappingTests(unittest.TestCase):
    def setUp(self):
        self.tagged = [midi_note(2, 2.5, 60), midi_note(6, 6.2, 61), midi_note(10, 10.5, 64)]
        self.playable = prepare(self.tagged)

    def test_inverse_timeline_restores_leading_wait_and_positions_inside_compressed_gap(self):
        timeline = PlaybackTimeline([ScheduledNote(2, 2.5, 'A', True), ScheduledNote(10, 10.5, 'D', True)])
        for playback, raw in ((-1, 1), (0, 2), (.5, 2.5), (1, 6.25), (1.5, 10), (2, 10.5)):
            with self.subTest(playback=playback):
                self.assertAlmostEqual(timeline.inverse_position(playback), raw)
                self.assertAlmostEqual(timeline.position(raw), playback)

    def test_selection_in_compressed_gap_includes_currently_unplayable_note(self):
        selected = select_tagged_region(self.tagged, self.playable, .9, 1.1)
        self.assertEqual(selected, [self.tagged[1]])
        self.assertEqual(find_best_transpose(selected), -1)
        raw = playback_region_to_source(self.playable, .9, 1.1, tagged=self.tagged)
        self.assertAlmostEqual(raw[0], 5.5)
        self.assertAlmostEqual(raw[1], 7)
        red = build_unavailable_notes(self.tagged, 0, False, self.playable)
        self.assertTrue(.9 <= red[0].start < 1.1)

    def test_new_transpose_reprojects_same_source_region_after_origin_and_gap_change(self):
        raw = playback_region_to_source(self.playable, .9, 1.1, tagged=self.tagged)
        transposed = finalize_notes(self.tagged, 1, log=lambda _: None)
        new_region = source_region_to_playback(transposed, *raw, tagged=self.tagged)
        self.assertLess(new_region[0], 0)  # New first playable note is at raw time 6.
        recovered = playback_region_to_source(transposed, *new_region, tagged=self.tagged)
        self.assertAlmostEqual(recovered[0], raw[0])
        self.assertAlmostEqual(recovered[1], raw[1])
        self.assertEqual(select_tagged_region(self.tagged, transposed, *new_region), [self.tagged[1]])

    def test_mapping_uses_scaled_schedule_with_real_time_minimum_hold(self):
        tagged = [midi_note(0, .005, 60), midi_note(5, 5.1, 61), midi_note(10, 10.2, 64)]
        playable = prepare(tagged, mode='fixed')
        region = source_region_to_playback(playable, 5, 10, speed=2, tagged=tagged)
        self.assertAlmostEqual(region[1], .54)
        raw = playback_region_to_source(playable, *region, speed=2, tagged=tagged)
        self.assertAlmostEqual(raw[0], 5)
        self.assertAlmostEqual(raw[1], 10)
        self.assertEqual(select_tagged_region(tagged, playable, *region, speed=2), [tagged[1]])

    def test_half_open_selection_uses_onsets_and_keeps_conflict_dropped_source_notes(self):
        tagged = [midi_note(0, 10, 60), midi_note(2, 3, 60, melody=False), midi_note(4, 5, 64)]
        playable = prepare(tagged)
        self.assertEqual(select_tagged_region(tagged, playable, 2, 4), [tagged[1]])
        self.assertEqual(select_tagged_region(tagged, playable, 2, 2), [])

    def test_no_playable_notes_use_earliest_tagged_onset_as_origin(self):
        tagged = [midi_note(5, 6, 61), midi_note(7, 8, 63)]
        self.assertEqual(playback_region_to_source([], 0, 1, speed=2, tagged=tagged), (5, 7))
        self.assertEqual(source_region_to_playback([], 5, 7, speed=2, tagged=tagged), (0, 1))
        self.assertEqual(select_tagged_region(tagged, [], 0, 1, speed=2), [tagged[0]])

    def test_invalid_region_or_speed_is_rejected(self):
        for start, end in ((1, 0), (0, float('inf')), (float('nan'), 1)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                playback_region_to_source(self.playable, start, end)
        with self.assertRaises(ValueError):
            source_region_to_playback(self.playable, 0, 1, speed=0)


class RegionPlayerTests(unittest.TestCase):
    def make_player(self, notes, start_ratio=0, *, region_start_ratio=0, end_ratio=1, speed=1):
        self.clock = FakeClock()
        self.keyboard = FakeKeyboard(self.clock)
        self.finished, self.progress, self.errors = [], [], []
        return KeyboardPlayer(notes, speed, start_ratio,
                              lambda *args: self.progress.append(args), self.errors.append,
                              self.finished.append, keyboard=self.keyboard,
                              clock=self.clock, waiter=self.clock.wait,
                              region_start_ratio=region_start_ratio, end_ratio=end_ratio)

    def test_region_plays_start_head_excludes_end_head_and_clips_held_note(self):
        notes = prepare([midi_note(0, 4, 60), midi_note(1, 3, 64), midi_note(2, 2.5, 67)])
        for speed in (.5, 1, 2):
            with self.subTest(speed=speed):
                player = self.make_player(notes, .25, region_start_ratio=.25, end_ratio=.5, speed=speed)
                player._run()
                self.assertEqual([event[:2] for event in self.keyboard.events], [('down', 'd'), ('up', 'd')])
                self.assertAlmostEqual(self.keyboard.events[0][2], 0)
                self.assertAlmostEqual(self.keyboard.events[-1][2], 1 / speed)
                self.assertEqual(self.finished, [True])
                self.assertEqual(self.errors, [])
                self.assertEqual(self.progress[-1], (.5, 2 / speed, 4 / speed))
                self.assertEqual(len(player.notes), 3)  # Visual note ids remain on the complete score.
                snapshot = player.get_visual_state()
                self.assertEqual(snapshot.phase, 'finished')
                self.assertEqual(snapshot.position, 2 / speed)
                self.assertEqual(snapshot.active_note_ids, ())
                self.assertFalse(self.keyboard.held)

    def test_region_with_only_old_tails_waits_for_region_end_without_pressing(self):
        player = self.make_player(prepare([midi_note(0, 4)]), .25, region_start_ratio=.25, end_ratio=.5)
        player._run()
        self.assertEqual(self.keyboard.events, [])
        self.assertAlmostEqual(self.clock.now, 1)
        self.assertEqual(self.progress[-1], (.5, 2, 4))
        self.assertEqual(self.finished, [True])

    def test_seek_is_clamped_to_region_and_end_seek_completes_normally(self):
        player = self.make_player(prepare([midi_note(0, 4), midi_note(1, 3, 64)]),
                                  region_start_ratio=.25, end_ratio=.5)
        self.assertEqual(player.play_time, 1)
        self.assertEqual(player.seek(-100), 1)
        self.assertEqual(player.seek_request, (1, 1))
        self.assertEqual(player.seek(100), 2)
        self.assertEqual(player.seek_request, (2, 2))
        player._run()
        self.assertEqual(self.keyboard.events, [])
        self.assertEqual(self.finished, [True])
        self.assertEqual(player.get_visual_state().seek_serial, 2)
        self.assertEqual(player.get_visual_state().position, 2)

    def test_default_region_preserves_backward_seek_before_initial_start(self):
        player = self.make_player(prepare([midi_note(0, 2), midi_note(.5, 1, 64)]), .5)
        player.seek(0)
        player._run()
        self.assertEqual([key for action, key, _ in self.keyboard.events if action == 'down'], ['a', 'd'])
        self.assertEqual(self.progress[-1], (1, 2, 2))

    def test_pause_resume_preserves_confirmed_key_but_still_ends_at_region_boundary(self):
        player = self.make_player(prepare([midi_note(0, 4), midi_note(1, 3, 64)]), .25,
                                  region_start_ratio=.25, end_ratio=.5)
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
        self.assertEqual([event[:2] for event in self.keyboard.events],
                         [('down', 'd'), ('up', 'd'), ('down', 'd'), ('up', 'd')])
        self.assertAlmostEqual(self.keyboard.events[-1][2], 1.3, delta=.02)
        self.assertEqual(player.play_time, 2)
        self.assertEqual(self.finished, [True])
        self.assertEqual(self.errors, [])

    def test_region_seek_skips_old_tail_and_intervening_note_but_plays_later_head(self):
        player = self.make_player(prepare([midi_note(0, 4), midi_note(1, 3, 64),
                                          midi_note(1.5, 1.6, 67), midi_note(2, 2.5, 69)]), .25,
                                  region_start_ratio=.25, end_ratio=.75)
        jumped = []
        def hook(now):
            if now >= .2 and not jumped:
                jumped.append(player.seek(1.8))
        self.clock.hook = hook
        player._run()
        self.assertEqual([event[:2] for event in self.keyboard.events],
                         [('down', 'd'), ('up', 'd'), ('down', 'h'), ('up', 'h')])
        self.assertEqual(player.play_time, 3)
        self.assertEqual(player.get_visual_state().seek_serial, 1)
        self.assertEqual(self.finished, [True])

    def test_reversed_or_outside_ratios_clamp_to_valid_region(self):
        notes = prepare([midi_note(0, 4)])
        player = self.make_player(notes, -10, region_start_ratio=.8, end_ratio=.2)
        self.assertEqual((player.region_start, player.region_end, player.play_time), (3.2, 3.2, 3.2))
        player._run()
        self.assertEqual(self.keyboard.events, [])
        self.assertEqual(self.finished, [True])
        self.assertEqual(self.clock.now, 0)
        player = self.make_player(notes, 10, region_start_ratio=-1, end_ratio=2)
        self.assertEqual((player.region_start, player.region_end, player.play_time), (0, 4, 4))

    def test_stop_inside_region_is_not_reported_as_completion(self):
        player = self.make_player(prepare([midi_note(0, 4), midi_note(1, 3, 64)]), .25,
                                  region_start_ratio=.25, end_ratio=.5)
        self.clock.hook = lambda now: player.stop() if now >= .2 else None
        player._run()
        self.assertEqual(self.finished, [False])
        self.assertFalse(self.keyboard.held)
        self.assertLess(player.play_time, player.region_end)
