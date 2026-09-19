"""Use temporary app data and fake live sessions; never opens MIDI hardware."""
import threading
import unittest
from unittest import mock
import genshin_gui as gui
from test_song_memory import AppMemoryTests as AppFixture


class FakeLive:
    def __init__(self, name, settings, **kwargs):
        self.name, self.settings = name, settings
        self.stopped = threading.Event(); self.paused = threading.Event()
        self.active_keys = (); self.status = kwargs['on_status']
    def start(self): pass
    def stop(self): self.stopped.set()


class LiveGuiTests(unittest.TestCase):
    setUp = AppFixture.setUp
    close_root = AppFixture.close_root

    def connect(self):
        self.app.live_device_var.set('Test keyboard')
        with mock.patch.object(gui, 'LiveMidiInput', FakeLive), mock.patch.object(self.app, '_refresh_midi_ports', return_value=['Test keyboard']):
            self.app._start_live(delay=0)
        return self.app.live_input

    def test_controls_pause_stop_and_ignore_old_status(self):
        live = self.connect()
        self.assertIsNotNone(live)
        self.app._on_play_pause(); self.assertTrue(live.paused.is_set())
        self.app._on_play_pause(); self.assertFalse(live.paused.is_set())
        self.app._on_stop(); self.assertTrue(live.stopped.is_set())
        self.assertTrue(self.app._live_auto_suspended)
        value = self.app.live_status_var.get()
        live.status('stale status'); self.app._drain_queue()
        self.assertEqual(value, self.app.live_status_var.get())

    def test_song_start_releases_live_and_settings_are_separate(self):
        self.app.live_transpose_var.set('12')
        live = self.connect()
        self.assertEqual(12, live.settings.transpose)
        self.app._start_playback(start_ratio=0, countdown=0)
        self.assertTrue(live.stopped.is_set())
        self.assertIsNone(self.app.live_input)
        self.assertTrue(self.app.session_active)

    def test_invalid_transpose_does_not_open_device(self):
        self.app.live_transpose_var.set('100')
        with mock.patch.object(gui, 'LiveMidiInput') as constructor:
            self.app._start_live()
            constructor.assert_not_called()

    def test_merge_window_saved_passed_to_session_and_validated(self):
        self.app.live_merge_ms_var.set('85')
        self.app.live_merge_var.set(True)
        live = self.connect()
        self.assertEqual(85, live.settings.merge_window_ms)
        self.assertTrue(live.settings.merge)
        self.assertEqual(85, self.app.label_store['settings']['live_midi']['merge_window_ms'])
        self.app._on_stop()
        for value in ('-1', '201', '', '2.5'):
            self.app.live_merge_ms_var.set(value)
            with mock.patch.object(gui, 'LiveMidiInput') as constructor:
                self.app._start_live()
                constructor.assert_not_called()


del AppFixture  # Avoid unittest discovering the imported fixture as another test class.
