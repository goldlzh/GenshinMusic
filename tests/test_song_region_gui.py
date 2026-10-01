"""Region selection and persistence through the real Tk application, with no key input."""
import copy
import mido
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest import mock

from midi_engine import MidiNote, KeyboardPlayer, find_best_transpose, select_tagged_region
from test_song_memory import AppMemoryTests as AppFixture, gui


class RegionGuiTests(unittest.TestCase):
    setUp = AppFixture.setUp
    close_root = AppFixture.close_root
    read_saved = AppFixture.read_saved

    def open_hidden_visual(self):
        window = tk.Toplevel(self.root)
        window.withdraw()
        with mock.patch.object(gui.tk, 'Toplevel', return_value=window):
            self.app._open_visual_window()
        return self.app.floating_visual

    def prepare_two_keys(self):
        app = self.app
        app.tagged = [MidiNote(t, t + .5, pitch, 0, 0, True)
                      for t, pitch in [(0, 60), (1, 64), (10, 61), (11, 65)]]
        app.transpose_var.set('0')
        app.fold_var.set(False)
        app.gliss_var.set(False)
        app.duration_mode_var.set('score')
        app._refresh_total()
        return app

    def test_two_visual_clicks_include_unavailable_notes_and_remap_source_region(self):
        app = self.prepare_two_keys()
        floating = self.open_hidden_visual()
        source = app.tagged[2:]
        # C# is currently unavailable. The range must still use C# and F, not the first key.
        raw_start, raw_end = 9.9, 11.5
        left, right = gui.source_region_to_playback(app.cached_notes, raw_start, raw_end)
        app._begin_region_selection()
        app.visual_view.on_seek(left)
        self.assertTrue(app._region_selecting)
        self.assertAlmostEqual(floating.selection_anchor, left)
        floating.on_seek(right)
        self.assertFalse(app._region_selecting)
        self.assertEqual(int(app.transpose_var.get()), find_best_transpose(source, False))
        self.assertEqual(app._suggestion_source_range, (raw_start, raw_end))
        selected = select_tagged_region(app.tagged, app.cached_notes,
                                       app.play_range['start'] * app.visual_total,
                                       app.play_range['end'] * app.visual_total)
        self.assertEqual(selected, source)
        self.assertEqual(app.seek_var.get(), app.play_range['start'])
        self.assertEqual(app.visual_view.play_range, floating.play_range)
        self.assertIsNone(floating.selection_anchor)
        app._start_playback(0, countdown=0)
        self.assertIsNotNone(app.player)
        self.assertAlmostEqual(app.player.region_start, app.play_range['start'] * app.player.total)
        self.assertAlmostEqual(app.player.region_end, app.play_range['end'] * app.player.total)

    def test_scale_clicks_are_consumed_in_reverse_order_without_seek_drag(self):
        app = self.app
        app._begin_region_selection()
        event = SimpleNamespace(x=100, y=10)
        with mock.patch.object(app.seek_scale, 'get', side_effect=[.8, .3]):
            for _ in range(2):
                self.assertEqual(app._on_scale_press(event), 'break')
                self.assertEqual(app._on_scale_release(event), 'break')
        self.assertFalse(app._region_selecting)
        self.assertFalse(app.user_dragging)
        self.assertLess(app.play_range['start'], app.play_range['end'])
        self.assertEqual(app.seek_var.get(), app.play_range['start'])
        self.assertIsNone(app.player)

    def test_cancel_or_empty_second_point_preserves_existing_range(self):
        app = self.app
        app._set_play_range(.2, .9)
        before = copy.deepcopy(app.play_range)
        app._begin_region_selection()
        app._accept_region_point(.25)
        app._accept_region_point(.25)
        self.assertTrue(app._region_selecting)
        app._accept_region_point(.3)  # between note heads
        self.assertTrue(app._region_selecting)
        self.assertEqual(app.play_range, before)
        app._on_play_pause()
        self.assertFalse(app.session_active)
        app._cancel_region_selection()
        self.assertEqual(app.play_range, before)
        self.assertIsNone(app.visual_view.selection_anchor)

    def test_handles_clamp_never_cross_and_cancel_a_countdown(self):
        app = self.app
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(0, countdown=1)
        with mock.patch.object(app, '_progress_track_geometry', return_value=(0, 1000)):
            app._on_range_press(SimpleNamespace(x=0))
            self.assertFalse(app.session_active)
            app._on_range_motion(SimpleNamespace(x=1500))
            self.assertGreater(app.play_range['start'], .99)
            self.assertLess(app.play_range['start'], app.play_range['end'])
            app._on_range_release(SimpleNamespace(x=200))
            app._on_range_press(SimpleNamespace(x=1000))
            app._on_range_release(SimpleNamespace(x=100))
        self.assertGreater(app.play_range['end'], app.play_range['start'])
        callbacks[0]()
        self.assertIsNone(app.player)
        self.assertNotIn('playback_memory', app._current_song_entry())
        self.assertEqual(app.seek_var.get(), app.play_range['start'])

    def test_manual_automatic_and_label_memories_restore_their_own_ranges(self):
        app = self.app
        app._set_play_range(.2, .7)
        app._remember_manually()
        app._insert_label()
        label = next(item for item in app._current_labels() if item.get('play_range'))
        app._set_play_range(.1, .9)
        app._start_playback(.1, countdown=0)
        app._on_stop()
        memories = self.read_saved()['songs'][app.current_song_key]['playback_memory']
        self.assertEqual(memories['manual']['play_range'], {'start': .2, 'end': .7})
        self.assertEqual(memories['auto']['play_range'], {'start': .1, 'end': .9})
        app._restore_memory()
        self.assertEqual(app.play_range, {'start': .2, 'end': .7})
        app._reset_play_range()
        app._jump_to_label(label['id'])
        self.assertEqual(app.play_range, {'start': .2, 'end': .7})
        app._jump_to_label('verse')
        self.assertEqual(app.play_range, {'start': 0., 'end': 1.})
        app._analyze_song('B.mid')
        self.assertEqual(app.play_range, {'start': 0., 'end': 1.})
        app._analyze_song('A.mid')
        self.assertEqual(app.play_range, {'start': .2, 'end': .7})

    def test_range_seeking_and_restart_at_end_stay_inside_region(self):
        app = self.app
        app._set_play_range(.2, .7)
        app._start_playback(.95, countdown=0)
        self.assertEqual(app.player.start_ratio, .2)
        app._on_visual_seek(0)
        self.assertEqual(app.player.play_time, app.player.region_start)
        self.assertTrue(app.player.pause_event.is_set())
        app.seek_var.set(1)
        app._on_scale_release(None)
        self.assertEqual(app.player.play_time, app.player.region_end)
        app.mode_var.set('sequential')
        app.playlist_order = [app.current_song_name]
        app._handle_finish((True, app.play_gen))
        self.assertAlmostEqual(app.visual_snapshot.position, .7 * app.visual_total)
        self.assertFalse(app.session_active)

    def test_speed_changes_preserve_source_selection_and_floating_overlay(self):
        app = self.prepare_two_keys()
        app._begin_region_selection()
        left, right = gui.source_region_to_playback(app.cached_notes, 9.9, 11.5)
        app._on_visual_seek(left)
        app._on_visual_seek(right)
        source = app._suggestion_source_range
        floating = self.open_hidden_visual()
        for speed in (.5, 2.):
            app.speed_var.set(speed)
            app._on_speed_change()
            expected = gui.source_region_to_playback(app.cached_notes, *source, speed=speed)
            self.assertAlmostEqual(app.play_range['start'] * app.visual_total, expected[0], places=4)
            self.assertAlmostEqual(app.play_range['end'] * app.visual_total, expected[1], places=4)
            self.assertEqual(app.visual_view.play_range, floating.play_range)

    def test_reset_full_song_clears_overlay_and_selection_scope(self):
        app = self.app
        app._set_play_range(.2, .7)
        app._begin_region_selection()
        app._accept_region_point(.4)
        app._reset_play_range()
        self.assertEqual(app.play_range, {'start': 0., 'end': 1.})
        self.assertIsNone(app._suggestion_source_range)
        self.assertIsNone(app.visual_view.play_range)
        self.assertIsNone(app.visual_view.selection_anchor)
        self.assertEqual(app.seek_var.get(), 0)

    def test_source_selection_that_becomes_full_playable_axis_stays_scoped(self):
        app = self.app
        mid = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        for i, pitch in enumerate((64, 63, 67)):
            track.append(mido.Message('note_on', note=pitch, velocity=80, time=0 if i == 0 else 2880))
            track.append(mido.Message('note_off', note=pitch, time=960))
        mid.save(self.base / 'songs' / 'A.mid')
        app._analyze_song('A.mid')
        app.fold_var.set(False)
        app.transpose_var.set('0')
        app._refresh_total()
        left, right = gui.source_region_to_playback(app.cached_notes, 3., 6.)
        app._begin_region_selection()
        app._on_visual_seek(left)
        app._on_visual_seek(right)
        self.assertEqual(app.play_range, {'start': 0., 'end': 1.})
        self.assertEqual(int(app.transpose_var.get()), -1)
        for restore in (False, True):
            if restore:
                app._remember_manually()
                app._reset_play_range()
                app._restore_memory()
            app._use_suggested()
            self.assertEqual(int(app.transpose_var.get()), -1)
            self.assertAlmostEqual(app._suggestion_source_range[0], 3.)
            self.assertAlmostEqual(app._suggestion_source_range[1], 6.)

    def test_restoring_memory_cancels_unfinished_selection(self):
        app = self.app
        app._set_play_range(.2, .7)
        app._remember_manually()
        app._begin_region_selection()
        app._accept_region_point(.8)
        app._restore_memory()
        self.assertFalse(app._region_selecting)
        self.assertIsNone(app._region_anchor)
        self.assertEqual(app.play_range, {'start': .2, 'end': .7})

    def test_exact_note_head_survives_remapping_and_memory_roundtrip(self):
        app = self.app
        # Total 6 seconds: a boundary at 1/6 must not be rounded to 1.000002 seconds.
        mid = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        mid.tracks.append(track)
        for pitch in (60, 62, 64, 65, 67, 69):
            track.append(mido.Message('note_on', note=pitch, velocity=80, time=0))
            track.append(mido.Message('note_off', note=pitch, time=960))
        mid.save(self.base / 'songs' / 'A.mid')
        app._analyze_song('A.mid')
        app._begin_region_selection()
        app._on_visual_seek(1.)
        app._on_visual_seek(4.)
        app._remember_manually()
        app._reset_play_range()
        app._restore_memory()
        region = app.play_range
        player = KeyboardPlayer(app.cached_notes, 1, region['start'], lambda *a: None,
                                lambda *a: None, lambda *a: None, keyboard=mock.Mock(),
                                region_start_ratio=region['start'], end_ratio=region['end'])
        # Event membership, rather than a fake player's reported progress, proves no head is lost.
        down_times = [event[0] for event in player.events if event[1] == 1]
        self.assertTrue(any(abs(t - 1.) < 1e-7 for t in down_times), down_times)
        self.assertFalse(any(abs(t - 4.) < 1e-7 for t in down_times), down_times)


del AppFixture
