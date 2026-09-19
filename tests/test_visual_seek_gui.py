"""Progress interactions in hidden Tk windows with fake players and temporary data."""
import tkinter as tk
import unittest
from dataclasses import replace
from unittest import mock

import genshin_gui as gui
from midi_engine import PlaybackSnapshot
from test_song_memory import AppMemoryTests as AppFixture


class VisualSeekGuiTests(unittest.TestCase):
    setUp = AppFixture.setUp
    close_root = AppFixture.close_root

    def open_hidden_visual(self):
        window = tk.Toplevel(self.root)
        window.withdraw()
        with mock.patch.object(gui.tk, 'Toplevel', return_value=window):
            self.app._open_visual_window()
        return self.app.floating_visual

    def tick_visual(self):
        with mock.patch.object(self.root, 'after'):
            self.app._visual_tick()

    def drain_messages(self):
        with mock.patch.object(self.root, 'after'):
            self.app._drain_queue()

    def test_drag_preview_updates_both_windows_before_release_while_paused_or_playing(self):
        app = self.app
        floating = self.open_hidden_visual()
        for paused in (False, True):
            with self.subTest(paused=paused):
                app._start_playback(.2, countdown=0)
                player = app.player
                if paused:
                    player.pause_event.set()
                actual = PlaybackSnapshot(player.play_time, 'paused' if paused else 'playing',
                                          active_note_ids=(0,))
                player.get_visual_state = mock.Mock(return_value=actual)
                with mock.patch.object(player, 'seek', wraps=player.seek) as seek:
                    app._on_scale_press(None)
                    for ratio in (.45, .731):
                        for view in (app.visual_view, floating):
                            view.follow_var.set(False)
                            view._manual_left = 99
                        app.seek_var.set(ratio)
                        app._on_scale_move(str(ratio))
                        self.tick_visual()
                        for view in (app.visual_view, floating):
                            self.assertAlmostEqual(view.snapshot.position, ratio * player.total)
                            self.assertEqual(view.snapshot.phase, 'preview')
                            self.assertEqual(view.snapshot.active_note_ids, ())
                            self.assertTrue(view.follow_var.get())
                            self.assertIsNone(view._manual_left)
                        self.assertIn(gui.time_text(ratio * player.total), app.time_label['text'])
                        self.assertEqual(player.pause_event.is_set(), paused)
                        self.assertTrue(player.suppress_progress)
                        seek.assert_not_called()
                    app._on_scale_release(None)
                    seek.assert_called_once_with(.731 * player.total)
                self.assertEqual(player.pause_event.is_set(), paused)
                self.assertFalse(app.user_dragging)
                self.assertFalse(player.suppress_progress)

    def test_old_player_snapshot_and_queued_progress_cannot_undo_a_seek(self):
        app = self.app
        app._start_playback(.2, countdown=0)
        player = app.player
        player.pause_event.set()
        old = PlaybackSnapshot(.2 * player.total, 'paused', seek_serial=0)
        player.get_visual_state = mock.Mock(return_value=old)
        player.seek = mock.Mock(return_value=1)
        app._on_scale_press(None)
        app.seek_var.set(.8)
        app._on_scale_move('.8')
        app._on_scale_release(None)
        self.tick_visual()
        app.set_progress(.2, old.position, player.total, app.play_gen, 0)
        self.drain_messages()
        self.assertEqual(app.seek_var.get(), .8)
        self.assertAlmostEqual(app.visual_snapshot.position, .8 * player.total)
        acknowledged = replace(old, position=.8 * player.total, seek_serial=1)
        player.get_visual_state.return_value = acknowledged
        self.tick_visual()
        self.assertIs(app.visual_snapshot, acknowledged)
        app.set_progress(.81, .81 * player.total, player.total, app.play_gen, 1)
        app.set_progress(.3, .3 * player.total, player.total, app.play_gen, 0)
        self.drain_messages()
        self.assertEqual(app.seek_var.get(), .81)

    def test_newer_seek_preview_waits_for_its_own_acknowledgement(self):
        app = self.app
        app._start_playback(0, countdown=0)
        player = app.player
        player.seek = mock.Mock(side_effect=[1, 2])
        app._move_playhead(.3)
        app._move_playhead(.7)
        player.get_visual_state = mock.Mock(return_value=PlaybackSnapshot(
            .3 * player.total, 'playing', seek_serial=1))
        self.tick_visual()
        self.assertAlmostEqual(app.visual_snapshot.position, .7 * player.total)
        self.assertEqual(app._visual_seek_serial, 2)

    def test_click_stopped_score_only_selects_position_until_manual_play(self):
        app = self.app
        with mock.patch.object(app, '_start_playback') as start:
            app.visual_view.on_seek(.42 * app.visual_total)
            start.assert_not_called()
            self.assertFalse(app.session_active)
            self.assertIsNone(app.player)
            self.assertEqual(app.play_btn['text'], '播放')
            app._on_play_pause()
            start.assert_called_once()
            self.assertAlmostEqual(start.call_args.kwargs['start_ratio'], .42)
            self.assertEqual(start.call_args.kwargs['countdown'], 5)
        self.assertAlmostEqual(app.start_ratio, .42)
        self.assertIsNone(app.start_label_id)

    def test_click_floating_score_seeks_and_waits_for_manual_resume(self):
        app = self.app
        floating = self.open_hidden_visual()
        app._start_playback(0, countdown=0)
        player, generation = app.player, app.play_gen
        player.pause_event.set()
        with mock.patch.object(player, 'seek', wraps=player.seek) as seek, \
                mock.patch.object(app, '_record_song_played') as record:
            floating.on_seek(.6 * player.total)
        seek.assert_called_once_with(.6 * player.total)
        self.assertTrue(player.pause_event.is_set())
        self.assertEqual(app.play_btn['text'], '继续')
        self.assertIs(app.player, player)
        self.assertEqual(app.play_gen, generation)
        record.assert_not_called()
        app._on_play_pause()
        self.assertFalse(player.pause_event.is_set())
        self.assertEqual(app.play_btn['text'], '暂停')

    def test_click_while_playing_pauses_before_seeking(self):
        app = self.app
        app._start_playback(0, countdown=0)
        player = app.player
        self.assertFalse(player.pause_event.is_set())

        def seek_when_paused(seconds):
            self.assertTrue(player.pause_event.is_set())
            player.play_time = seconds

        with mock.patch.object(player, 'seek', side_effect=seek_when_paused) as seek:
            app.visual_view.on_seek(.6 * player.total)
            app.visual_view.on_seek(.4 * player.total)
        self.assertEqual(seek.call_count, 2)
        self.assertTrue(player.pause_event.is_set())
        self.assertAlmostEqual(player.play_time, .4 * player.total)
        self.assertEqual(app.play_btn['text'], '继续')

    def test_click_during_countdown_cancels_automatic_start_and_keeps_position(self):
        app = self.app
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(.1, countdown=1)
        app.visual_view.on_seek(.65 * app.visual_total)
        with mock.patch.object(gui, 'Player') as player, mock.patch.object(app, '_record_song_played') as record:
            callbacks[0]()
        player.assert_not_called()
        record.assert_not_called()
        self.assertFalse(app.session_active)
        self.assertIsNone(app.player)
        self.assertIsNone(app._pending_start)
        self.assertAlmostEqual(app.seek_var.get(), .65)
        self.assertEqual(app.play_btn['text'], '播放')
        self.assertEqual(app.visual_snapshot.phase, 'preview')
        with mock.patch.object(app, '_start_playback') as start:
            app._on_play_pause()
        self.assertAlmostEqual(start.call_args.kwargs['start_ratio'], .65)
        self.assertEqual(start.call_args.kwargs['countdown'], 5)

    def test_drag_during_countdown_updates_actual_start_and_automatic_memory(self):
        app = self.app
        callbacks = []
        app.speed_var.set(1.25)
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(.1, countdown=1)
        app.speed_var.set(1.75)
        app._on_scale_press(None)
        app.seek_var.set(.67)
        app._on_scale_move('.67')
        app._on_scale_release(None)
        self.assertEqual(app.visual_snapshot.phase, 'countdown')
        callbacks[0]()
        self.assertEqual(app.player.start_ratio, .67)
        self.assertEqual(app.player.speed, 1.25)
        memory = app._current_song_entry()['playback_memory']['auto']
        self.assertEqual(memory['start'], {'ratio': .67, 'label_id': None})
        self.assertEqual(memory['speed'], 1.25)

    def test_wheel_browsing_does_not_change_playback_position(self):
        app = self.app
        app._start_playback(.2, countdown=0)
        player = app.player
        initial_ratio = app.seek_var.get()
        with mock.patch.object(player, 'seek') as seek:
            app.visual_view._on_wheel(mock.Mock(delta=-120, num=None, state=0))
        seek.assert_not_called()
        self.assertEqual(app.start_ratio, .2)
        self.assertEqual(app.seek_var.get(), initial_ratio)
        self.assertEqual(player.play_time, .2 * player.total)


del AppFixture
