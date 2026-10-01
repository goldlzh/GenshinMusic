"""运行：python -m unittest discover -s tests -v

使用临时 MIDI/JSON 和隐藏 Tk 窗口；热键和真实键盘输入全部禁用。
"""

import copy
import json
import pathlib
import sys
import tempfile
import threading
import tkinter as tk
import unittest
from unittest import mock

import mido
from midi_engine import MidiNote

from song_memory import (clamp_ratio, normalize_memory, normalize_play_range,
                         normalize_source_range, preferred_memory, resolve_memory_start)

# 即使将来测试误触演奏函数，也不会向用户桌面发送按键。
with mock.patch.dict(sys.modules, {"pydirectinput": mock.Mock()}):
    import genshin_gui as gui


class MemoryDataTests(unittest.TestCase):
    def test_normalize_rejects_invalid_or_empty_channels(self):
        for value in (None, [], {}, {"channels": "0"}, {"channels": [9, -1, 16, True]}):
            with self.subTest(value=value):
                self.assertIsNone(normalize_memory(value))

    def test_normalize_clamps_untrusted_values_and_copies(self):
        raw = {"channels": [2, 0, 2, 9], "speed": float("inf"),
               "transpose": "bad", "melody_track": -1, "octave_fold": "false",
               "start": {"label_id": 99, "ratio": 3}}
        value = normalize_memory(raw)
        self.assertEqual(value["channels"], [0, 2])
        self.assertEqual(value["speed"], 1.0)
        self.assertEqual(value["transpose"], 0)
        self.assertIsNone(value["melody_track"])
        self.assertTrue(value["octave_fold"])
        self.assertEqual(value["start"], {"label_id": None, "ratio": 1.0})
        value["channels"].append(3)
        self.assertEqual(raw["channels"], [2, 0, 2, 9])
        self.assertEqual(normalize_memory({"channels": [0], "speed": 9})["speed"], 2.0)
        self.assertEqual(normalize_memory({"channels": [0], "speed": -9})["speed"], 0.5)

    def test_ratio_handles_nonfinite_and_bad_values(self):
        for value in (None, "bad", float("nan"), float("inf"), -1):
            self.assertEqual(clamp_ratio(value), 0.0)
        self.assertEqual(clamp_ratio(2), 1.0)

    def test_manual_has_priority_and_invalid_manual_falls_back(self):
        entry = {"playback_memory": {"manual": {"channels": [1]}, "auto": {"channels": [0]}}}
        self.assertEqual(preferred_memory(entry)[0], "manual")
        entry["playback_memory"]["manual"] = {"channels": []}
        self.assertEqual(preferred_memory(entry)[0], "auto")
        self.assertEqual(preferred_memory({"labels": []}), (None, None))

    def test_start_follows_id_then_falls_back_to_ratio(self):
        memory = normalize_memory({"channels": [0], "start": {"label_id": "label", "ratio": 0.3}})
        self.assertEqual(resolve_memory_start(memory, [{"id": "label", "ratio": 0.7}]), (0.7, "label"))
        self.assertEqual(resolve_memory_start(memory, []), (0.3, None))


    def test_play_range_defaults_keep_legacy_memory_and_label_start(self):
        raw = {"channels": [0], "start": {"label_id": "verse", "ratio": 0.3}}
        memory = normalize_memory(raw)
        self.assertEqual(memory["play_range"], {"start": 0.0, "end": 1.0})
        self.assertEqual(memory["start"], raw["start"])
        self.assertEqual(resolve_memory_start(memory, [{"id": "verse", "ratio": 0.7}]),
                         (0.7, "verse"))
        memory["play_range"]["start"] = 0.2
        self.assertEqual(normalize_memory(raw)["play_range"], {"start": 0.0, "end": 1.0})

    def test_play_range_normalizes_order_bounds_without_losing_precision(self):
        cases = (
            ({"start": 0.8, "end": 0.2}, {"start": 0.2, "end": 0.8}),
            ({"start": -3, "end": 0.8}, {"start": 0.0, "end": 0.8}),
            ({"start": 3, "end": 0.2}, {"start": 0.2, "end": 1.0}),
            ({"start": "0.1234567", "end": "0.9876543"},
             {"start": 0.1234567, "end": 0.9876543}),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_play_range(raw), expected)

    def test_invalid_or_empty_play_ranges_fall_back_to_full_song(self):
        invalid = [None, [], "0.2-0.8", {}, {"start": 0.2}, {"end": 0.8},
                   {"start": 0.5, "end": 0.5}, {"start": -2, "end": -1},
                   {"start": 2, "end": 3}]
        for endpoint in (None, "bad", [], {}, True, False, float("nan"),
                         float("inf"), float("-inf")):
            invalid.extend(({"start": endpoint, "end": 0.8},
                            {"start": 0.2, "end": endpoint}))
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_play_range(raw), {"start": 0.0, "end": 1.0})
                self.assertEqual(normalize_memory({"channels": [0], "play_range": raw})["channels"],
                                 [0])

    def test_play_range_accepts_every_positive_width_without_quantization(self):
        for start, end in ((0.0, 0.000001), (0.5, 0.500001), (0.999999, 1.0),
                           (0.5000001, 0.5000004), (0.5, 0.5000000000000001)):
            with self.subTest(start=start):
                expected = {"start": start, "end": end}
                self.assertEqual(normalize_play_range(expected), expected)
                self.assertEqual(normalize_play_range({"start": end, "end": start}), expected)
                self.assertEqual(normalize_play_range(json.loads(json.dumps(expected))), expected)

    def test_onset_boundaries_and_memory_start_preserve_one_sixth_through_json(self):
        boundary = 1 / 6
        # In a six-second song, rounding this ratio upward skips the one-second onset.
        for start, end in ((boundary, 5 / 6), (0.0, boundary)):
            with self.subTest(start=start, end=end):
                raw = {"channels": [0], "start": {"ratio": start},
                       "play_range": {"start": start, "end": end}}
                saved = normalize_memory(raw)
                restored = normalize_memory(json.loads(json.dumps(saved)))
                self.assertEqual(restored["play_range"], raw["play_range"])
                self.assertEqual(resolve_memory_start(restored, []), (start, None))
                self.assertEqual(restored["start"]["ratio"] * 6, start * 6)
                self.assertEqual(restored["play_range"]["end"] * 6, end * 6)
                self.assertEqual(normalize_memory(restored), restored)

    def test_source_range_accepts_raw_seconds_without_clamping_or_quantization(self):
        for raw in ([1 / 6, 50 / 6], (0, 123.456789), ("0.1234567", "3.9876543")):
            with self.subTest(raw=raw):
                expected = tuple(float(endpoint) for endpoint in raw)
                self.assertEqual(normalize_source_range(raw), expected)
                memory = normalize_memory({"channels": [0], "region_source": raw})
                self.assertEqual(memory["region_source"], expected)
                restored = normalize_memory(json.loads(json.dumps(memory)))
                self.assertEqual(restored["region_source"], expected)

    def test_invalid_source_ranges_and_legacy_memory_have_no_source_region(self):
        invalid = [None, [], [1], [0, 1, 2], "0,1", {"start": 0, "end": 1},
                   [1, 1], [2, 1], [-1, 1]]
        for endpoint in (None, "bad", [], {}, True, False, float("nan"),
                         float("inf"), float("-inf")):
            invalid.extend(([endpoint, 1], [0, endpoint]))
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_source_range(raw))
                self.assertIsNone(normalize_memory({"channels": [0], "region_source": raw})["region_source"])
        self.assertIsNone(normalize_memory({"channels": [0]})["region_source"])

    def test_source_region_survives_full_playback_range_and_is_an_independent_snapshot(self):
        raw_source = [12 + 1 / 6, 20 + 5 / 6]
        memory = normalize_memory({"channels": [0], "region_source": raw_source,
                                   "play_range": {"start": 0, "end": 1}})
        raw_source[0] = 0
        restored = normalize_memory(json.loads(json.dumps(memory)))
        self.assertEqual(restored["region_source"], (12 + 1 / 6, 20 + 5 / 6))
        self.assertEqual(restored["play_range"], {"start": 0, "end": 1})

    def test_range_snapshots_are_independent_for_memories_and_labels(self):
        raw_range = {"start": 0.2, "end": 0.8}
        raw_memory = {"channels": [0], "play_range": raw_range}
        memory = normalize_memory(raw_memory)
        label_range = normalize_play_range(raw_range)
        memory["play_range"]["start"] = 0.4
        label_range["end"] = 0.9
        self.assertEqual(raw_range, {"start": 0.2, "end": 0.8})
        self.assertEqual(memory["play_range"], {"start": 0.4, "end": 0.8})
        self.assertEqual(label_range, {"start": 0.2, "end": 0.9})
        self.assertEqual(normalize_memory(json.loads(json.dumps(raw_memory)))["play_range"],
                         raw_range)

    def test_preferred_memory_restores_each_sources_own_play_range(self):
        manual = {"channels": [1], "play_range": {"start": 0.1, "end": 0.4}}
        automatic = {"channels": [0], "play_range": {"start": 0.6, "end": 0.9}}
        entry = {"playback_memory": {"manual": manual, "auto": automatic}}
        source, memory = preferred_memory(entry)
        self.assertEqual(source, "manual")
        self.assertEqual(memory["play_range"], manual["play_range"])
        memory["play_range"]["end"] = 1.0
        self.assertEqual(manual["play_range"]["end"], 0.4)
        entry["playback_memory"].pop("manual")
        source, memory = preferred_memory(entry)
        self.assertEqual(source, "auto")
        self.assertEqual(memory["play_range"], automatic["play_range"])


class FakePlayer:
    def __init__(self, chords, speed, start_ratio, progress_cb, log_cb, finish_cb,
                 *, region_start_ratio=0.0, end_ratio=1.0):
        self.total = gui.performance_duration(chords, speed)
        self.region_start = self.total * region_start_ratio
        self.region_end = self.total * end_ratio
        self.play_time = self.total * start_ratio
        self.start_ratio = start_ratio
        self.speed = speed
        self.progress_cb = progress_cb
        self.finish_cb = finish_cb
        self.pause_event = threading.Event()
        self.suppress_progress = False
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def seek(self, seconds):
        self.play_time = max(self.region_start, min(seconds, self.region_end))


class AppMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="genshin-memory-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        songs = self.base / "songs"
        songs.mkdir()
        for name in ("A.mid", "B.mid"):
            mid = mido.MidiFile()
            for channel, first_note in enumerate((60, 67, 72)):
                track = mido.MidiTrack()
                mid.tracks.append(track)
                for index, pitch in enumerate((first_note, first_note + 2, first_note + 4)):
                    track.append(mido.Message("note_on", channel=channel, note=pitch,
                                              velocity=80, time=0 if index == 0 else 240))
                    track.append(mido.Message("note_off", channel=channel, note=pitch, time=240))
            mid.save(songs / name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_root)
        self.patches = [mock.patch.object(gui, "__file__", str(self.base / "genshin_gui.py")),
                        mock.patch.object(gui.App, "_activate_all_hotkeys"),
                        mock.patch.object(gui, "Player", FakePlayer)]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = gui.App(self.root)
        self.app._analyze_song("A.mid")
        self.app.current_index = 0
        self.app._current_labels(create=True).extend([
            {"id": "verse", "name": "主歌", "ratio": 0.25},
            {"id": "chorus", "name": "副歌", "ratio": 0.6},
        ])
        self.app._save_label_store()

    def close_root(self):
        if hasattr(self, "app"):
            self.app._on_stop()
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()

    def customize(self):
        app = self.app
        app.channel_vars[1].set(False)
        app._recompute(melody_track=0)
        app.transpose_var.set("0")  # 特别验证 0 不被建议值覆盖。
        app.speed_var.set(1.4)
        app.melody_only_var.set(True)
        app.gliss_var.set(False)
        app.fold_var.set(False)
        app.duration_mode_var.set("melody")
        app.sustain_var.set(True)
        app._jump_to_label("verse")

    def read_saved(self):
        return json.loads((self.base / "song_labels.json").read_text(encoding="utf-8"))

    def test_sort_choice_persists_and_search_refresh_keep_selection(self):
        app = self.app
        app._select_in_combo('B.mid')
        app.sort_var.set('added')
        app._on_sort_change()
        app.search_var.set('.mid')
        app._load_song_list()
        self.assertEqual(app.song_combo.get(), 'B.mid')
        self.assertEqual(app.current_song_name, 'A.mid')
        self.assertEqual(app.all_songs[app.current_index], 'A.mid')
        self.assertEqual(app._load_label_store()['settings']['song_sort'], 'added')
        app.search_var.set('missing')
        self.assertEqual(app.song_combo.get(), '')
        self.assertEqual(app.song_time_var.get(), '没有匹配的曲目')
        app.search_var.set('')
        self.assertEqual(app.song_combo.get(), 'A.mid')

    def test_refresh_adds_only_midi_files_and_retains_added_time(self):
        app = self.app
        before = self.read_saved()['songs'][app._song_key('A.mid')]['added_at']
        (self.base / 'songs' / 'C.MIDI').write_bytes((self.base / 'songs' / 'A.mid').read_bytes())
        (self.base / 'songs' / 'ignored.mid').mkdir()
        (self.base / 'songs' / 'notes.txt').write_text('ignore')
        with mock.patch.object(gui.time, 'time', return_value=2000000000):
            app._load_song_list()
        saved = self.read_saved()['songs']
        self.assertEqual(saved[app._song_key('A.mid')]['added_at'], before)
        self.assertEqual(saved[app._song_key('C.MIDI')]['added_at'], 2000000000)
        self.assertNotIn('last_played_at', saved[app._song_key('C.MIDI')])
        app.sort_var.set('added')
        app._on_sort_change()
        self.assertEqual(app.all_songs[0], 'C.MIDI')
        self.assertEqual(len(app.all_songs), 3)

    def test_time_records_analyzed_song_at_start_not_combo_selection(self):
        app = self.app
        app._select_in_combo('B.mid')
        with mock.patch.object(gui.time, 'time', return_value=2000000000):
            app._start_playback(0, countdown=0)
        songs = self.read_saved()['songs']
        self.assertEqual(songs[app._song_key('A.mid')]['last_played_at'], 2000000000)
        self.assertNotIn('last_played_at', songs[app._song_key('B.mid')])
        self.assertEqual(app.song_combo.get(), 'B.mid')
        self.assertIn('最近播放：尚无记录', app.song_time_var.get())
        app._select_in_combo('A.mid')
        self.assertIn(gui.format_timestamp(2000000000), app.song_time_var.get())

    def test_play_time_waits_for_countdown_and_is_not_changed_by_pause_seek_or_stop(self):
        app = self.app
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            with mock.patch.object(gui.time, 'time', return_value=1000):
                app._start_playback(0, countdown=1)
            self.assertNotIn('last_played_at', app._current_song_entry())
            with mock.patch.object(gui.time, 'time', return_value=2000):
                callbacks.pop(0)()
        self.assertEqual(app._current_song_entry()['last_played_at'], 2000)
        with mock.patch.object(gui.time, 'time', return_value=3000):
            app._on_play_pause()
            app._on_play_pause()
            app.seek_var.set(.5)
            app._on_scale_release(None)
            app._on_stop()
            app._analyze_song('A.mid')
        self.assertEqual(app._current_song_entry()['last_played_at'], 2000)
        with mock.patch.object(gui.time, 'time', return_value=4000):
            app._start_playback(.5, countdown=0)
        self.assertEqual(app._current_song_entry()['last_played_at'], 4000)

    def test_cancelled_countdown_and_failed_player_do_not_record_a_play(self):
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app._start_playback(0, countdown=1)
            self.app._on_stop()
            callbacks.pop(0)()
        self.assertNotIn('last_played_at', self.app._current_song_entry())
        with mock.patch.object(FakePlayer, 'start', side_effect=RuntimeError('cannot start')):
            self.app._start_playback(0, countdown=0)
        self.assertNotIn('last_played_at', self.app._current_song_entry())
        self.assertNotIn('playback_memory', self.app._current_song_entry())
        self.assertFalse(self.app.session_active)

    def test_invalid_configuration_does_not_record_a_play(self):
        for var in self.app.channel_vars.values():
            var.set(False)
        self.app._recompute()
        self.app._start_playback(0, countdown=0)
        self.assertNotIn('last_played_at', self.app._current_song_entry())

    def test_failed_play_time_write_keeps_previous_disk_and_memory_value(self):
        app = self.app
        app._current_song_entry()['last_played_at'] = 100
        app._save_label_store()
        with mock.patch.object(gui.os, 'replace', side_effect=OSError('write denied')):
            app._record_song_played('A.mid')
        self.assertEqual(app._current_song_entry()['last_played_at'], 100)
        self.assertEqual(self.read_saved()['songs'][app.current_song_key]['last_played_at'], 100)

    def test_failed_initial_library_write_retries_on_refresh(self):
        app = self.app
        (self.base / 'songs' / 'C.mid').write_bytes((self.base / 'songs' / 'A.mid').read_bytes())
        with mock.patch.object(app, '_save_label_store', return_value=False):
            app._load_song_list()
        self.assertNotIn(app._song_key('C.mid'), self.read_saved()['songs'])
        app._load_song_list()
        self.assertIn('added_at', self.read_saved()['songs'][app._song_key('C.mid')])

    def add_third_song_and_sort_by_play_time(self):
        app = self.app
        (self.base / 'songs' / 'C.mid').write_bytes((self.base / 'songs' / 'A.mid').read_bytes())
        app._load_song_list()
        for i, name in enumerate(('A.mid', 'B.mid', 'C.mid')):
            entry = app.label_store['songs'][app._song_key(name)]
            entry['last_played_at'] = 300 - i * 100
            entry['added_at'] = 100 + i * 100
        app.sort_var.set('played')
        app._on_sort_change()

    def test_recent_play_resort_does_not_repeat_songs_in_sequential_playback(self):
        app = self.app
        self.add_third_song_and_sort_by_play_time()
        original_start = app._start_playback
        with mock.patch.object(app, '_start_playback',
                               side_effect=lambda start_ratio, countdown: original_start(start_ratio, 0)):
            with mock.patch.object(gui.time, 'time', side_effect=[1000, 2000, 3000]):
                app._start_playback(0, countdown=0)
                played = [app.current_song_name]
                for _ in range(2):
                    app._handle_finish((True, app.play_gen))
                    played.append(app.current_song_name)
                self.assertEqual(played, ['A.mid', 'B.mid', 'C.mid'])
                self.assertEqual(app.all_songs, ['C.mid', 'B.mid', 'A.mid'])
                self.assertEqual(app.all_songs[app.current_index], 'C.mid')
                app._handle_finish((True, app.play_gen))
        self.assertFalse(app.session_active)
        self.assertEqual(app.playlist_order, [])

    def test_start_in_middle_then_next_uses_original_queue_not_new_display_position(self):
        app = self.app
        self.add_third_song_and_sort_by_play_time()
        app._analyze_song('B.mid')
        with mock.patch.object(gui.time, 'time', return_value=1000):
            app._start_playback(0, countdown=0)
        self.assertEqual(app.all_songs, ['B.mid', 'A.mid', 'C.mid'])
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._on_next()
        with mock.patch.object(app, '_play_song') as play:
            callbacks[0]()
        self.assertEqual(play.call_args.args[0], 'C.mid')

    def test_explicit_sort_during_playback_preserves_player_and_updates_next_order(self):
        app = self.app
        self.add_third_song_and_sort_by_play_time()
        app._analyze_song('B.mid')
        app._select_in_combo('B.mid')
        app._start_playback(.25, countdown=0)
        player, gen = app.player, app.play_gen
        timestamp = app._current_song_entry()['last_played_at']
        app.sort_var.set('added')
        app._on_sort_change()
        self.assertEqual(app.all_songs, ['C.mid', 'B.mid', 'A.mid'])
        self.assertEqual(app.song_combo.get(), 'B.mid')
        self.assertIs(app.player, player)
        self.assertEqual(app.play_gen, gen)
        self.assertEqual(app._current_song_entry()['last_played_at'], timestamp)
        with mock.patch.object(app, '_play_song') as play:
            app._handle_finish((True, gen))
        self.assertEqual(play.call_args.args[0], 'A.mid')

    def test_pending_next_keeps_target_identity_if_library_order_changes(self):
        app = self.app
        self.add_third_song_and_sort_by_play_time()
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._on_next()
        app.sort_var.set('added')
        app._on_sort_change()
        with mock.patch.object(app, '_play_song') as play:
            callbacks[0]()
        self.assertEqual(play.call_args.args[0], 'B.mid')
        self.assertEqual(play.call_args.kwargs['order'], ['C.mid', 'B.mid', 'A.mid'])

    def test_refresh_removed_current_song_still_advances_to_its_original_successor(self):
        app = self.app
        self.add_third_song_and_sort_by_play_time()
        app._analyze_song('B.mid')
        app._start_playback(0, countdown=0)
        player = app.player
        (self.base / 'songs' / 'B.mid').unlink()
        app._load_song_list()
        self.assertIs(app.player, player)
        self.assertEqual(app.current_index, -1)
        with mock.patch.object(app, '_play_song') as play:
            app._handle_finish((True, app.play_gen))
        self.assertEqual(play.call_args.args[0], 'C.mid')

    def test_auto_saves_all_actual_settings_and_restores_on_reanalysis(self):
        self.customize()
        expected = self.app._capture_memory()
        self.app._start_playback(self.app.seek_var.get(), countdown=0)
        self.assertTrue(self.app.player.started)
        self.assertEqual(self.app._current_song_entry()["playback_memory"]["auto"], expected)
        self.app.label_store = self.app._load_label_store()  # 模拟程序重开读取磁盘。
        self.app._analyze_song("B.mid")
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app._capture_memory(), expected)
        self.assertEqual(self.app.seek_var.get(), 0.25)
        self.assertEqual(self.app.transpose_var.get(), "0")
        self.assertIn("1.40", self.app.speed_label.cget("text"))

    def test_manual_is_never_overwritten_by_auto_and_clear_uses_latest_auto(self):
        self.customize()
        self.app._remember_manually()
        manual = copy.deepcopy(self.app._current_song_entry()["playback_memory"]["manual"])
        self.app.speed_var.set(1.8)
        self.app._jump_to_label("chorus")
        self.app._start_playback(0.6, countdown=0)
        memories = self.app._current_song_entry()["playback_memory"]
        self.assertEqual(memories["manual"], manual)
        self.assertEqual(memories["auto"]["speed"], 1.8)
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app._capture_memory(), manual)
        self.app._clear_manual_memory()
        self.app._restore_memory()
        self.assertEqual(self.app.speed_var.get(), 1.8)
        self.assertEqual(self.app.start_label_id, "chorus")
        self.assertEqual(self.app.seek_var.get(), 0.6)

    def test_second_manual_click_explicitly_replaces_manual_memory(self):
        self.app._remember_manually()
        self.app.speed_var.set(1.25)
        self.app._jump_to_label("chorus")
        self.app._remember_manually()
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app.speed_var.get(), 1.25)
        self.assertEqual(self.app.start_label_id, "chorus")

    def test_progress_pause_and_stop_do_not_replace_starting_label(self):
        self.app._jump_to_label("verse")
        self.app._start_playback(0.25, countdown=0)
        self.app.player.progress_cb(0.9, 0.9 * self.app.player.total, self.app.player.total)
        self.app._drain_queue()
        self.assertAlmostEqual(self.app.seek_var.get(), 0.9)
        self.app._on_play_pause()
        self.app._remember_manually()
        self.app._on_play_pause()
        self.app._on_stop()
        for value in self.app._current_song_entry()["playback_memory"].values():
            self.assertEqual(value["start"], {"label_id": "verse", "ratio": 0.25})

    def test_slider_memory_is_distinct_from_label_memory(self):
        self.app._jump_to_label("verse")
        self.app.seek_var.set(0.42)
        self.app._on_scale_release(None)
        self.app._remember_manually()
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app.start_label_id, None)
        self.assertEqual(self.app.seek_var.get(), 0.42)

    def test_cancelled_countdown_never_creates_auto_memory(self):
        callbacks = []
        with mock.patch.object(self.root, "after", side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app._start_playback(0.25, countdown=1)
            self.assertNotIn("playback_memory", self.app._current_song_entry())
            self.app._on_stop()
            callbacks.pop(0)()
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertIsNone(self.app.player)

    def test_countdown_keeps_captured_options_but_accepts_updated_start(self):
        self.customize()
        expected = self.app._capture_memory()
        callbacks = []
        with mock.patch.object(self.root, "after", side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app._start_playback(0.25, countdown=1)
            self.app.speed_var.set(1.75)
            self.app._jump_to_label("chorus")
            self.app._remember_manually()
            callbacks.pop(0)()
        memories = self.app._current_song_entry()["playback_memory"]
        expected['start'] = {'label_id': 'chorus', 'ratio': .6}
        self.assertEqual(memories["auto"], expected)
        self.assertEqual(memories["manual"]["speed"], 1.75)
        self.assertEqual(self.app.player.speed, 1.4)
        self.assertEqual(self.app.player.start_ratio, 0.6)

    def test_old_progress_and_finish_cannot_override_restored_start(self):
        self.app._jump_to_label("verse")
        self.app._start_playback(0.25, countdown=0)
        old_player = self.app.player
        self.app._analyze_song("B.mid")
        old_player.progress_cb(0.9, 0.9, 1.0)
        old_player.finish_cb(True)
        self.app._drain_queue()
        self.assertEqual(self.app.current_song_name, "B.mid")
        self.assertEqual(self.app.seek_var.get(), 0.0)
        self.assertFalse(self.app.session_active)
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertEqual(self.app.visual_title, "B.mid")
        self.assertEqual(self.app.visual_snapshot.phase, 'preview')
        self.assertEqual(self.app.visual_snapshot.position, 0.0)
        self.assertEqual(self.app.visual_snapshot.active_note_ids, ())

    def test_switching_song_cancels_countdown_without_saving_wrong_song(self):
        callbacks = []
        with mock.patch.object(self.root, "after", side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app._start_playback(0.25, countdown=1)
            self.app._analyze_song("B.mid")
            callbacks.pop(0)()
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertFalse(self.app.session_active)

    def test_new_song_resets_per_song_configuration_but_not_playlist_mode(self):
        self.customize()
        self.app.mode_var.set("random")
        self.app._analyze_song("B.mid")
        self.assertEqual(self.app.speed_var.get(), 1.0)
        self.assertFalse(self.app.melody_only_var.get())
        self.assertTrue(self.app.gliss_var.get())
        self.assertTrue(self.app.fold_var.get())
        self.assertEqual(self.app.duration_mode_var.get(), "score")
        self.assertFalse(self.app.sustain_var.get())
        self.assertEqual(self.app.melody_combo.get(), "自动")
        self.assertEqual(self.app.mode_var.get(), "random")
        self.assertEqual(self.app.seek_var.get(), 0.0)

    def test_playlist_entry_uses_remembered_start_instead_of_zero(self):
        self.app._jump_to_label("chorus")
        self.app._remember_manually()
        with mock.patch.object(self.app, "_start_playback") as play:
            self.app._play_index(0, countdown=3)
        play.assert_called_once_with(start_ratio=0.6, countdown=3)

    def test_single_loop_reloads_memory_and_ignores_end_progress(self):
        self.app.mode_var.set("single")
        self.app._jump_to_label("verse")
        self.app._start_playback(0.25, countdown=0)
        self.app.seek_var.set(1.0)
        with mock.patch.object(self.app, "_start_playback") as play:
            self.app._handle_finish((True, self.app.play_gen))
        play.assert_called_once_with(start_ratio=0.25, countdown=3)

    def test_label_rename_and_move_keep_identity_and_delete_keeps_last_ratio(self):
        self.app._jump_to_label("verse")
        self.app._remember_manually()
        self.app._start_playback(0.25, countdown=0)
        self.app._on_stop()
        with mock.patch.object(gui.simpledialog, "askstring", return_value="新名字"):
            self.app._rename_label("verse")
        self.app.seek_var.set(0.5)
        self.app._move_label_to_current("verse")
        self.app._restore_memory()
        self.assertEqual(self.app.seek_var.get(), 0.5)
        self.assertEqual(self.app.start_label_id, "verse")
        self.assertIn("新名字", self.app.memory_status_var.get())
        self.app._delete_label("verse")
        for value in self.app._current_song_entry()["playback_memory"].values():
            self.assertEqual(value["start"], {"label_id": None, "ratio": 0.5})
        self.app._restore_memory()
        self.assertEqual(self.app.seek_var.get(), 0.5)

    def test_missing_track_or_channel_falls_back_without_losing_other_settings(self):
        memory = normalize_memory({"channels": [15], "melody_track": 999, "speed": 1.3})
        self.app._save_memory("manual", memory)
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app.melody_combo.get(), "自动")
        self.assertTrue(any(var.get() for var in self.app.channel_vars.values()))
        self.assertEqual(self.app.speed_var.get(), 1.3)

    def test_old_labels_hotkeys_and_unknown_fields_survive_save(self):
        original_labels = copy.deepcopy(self.app._current_labels())
        self.app.label_store["settings"]["insert_hotkey"] = "Num+"
        self.app._current_song_entry()["user_extra"] = "keep"
        self.app._remember_manually()
        saved = self.read_saved()
        song = saved["songs"][self.app.current_song_key]
        self.assertEqual(song["labels"], original_labels)
        self.assertEqual(song["user_extra"], "keep")
        self.assertEqual(saved["settings"]["insert_hotkey"], "Num+")
        self.assertEqual(saved["version"], 1)

    def test_failed_memory_save_rolls_back_existing_memory(self):
        self.app._remember_manually()
        expected = copy.deepcopy(self.app._current_song_entry()["playback_memory"])
        self.app.speed_var.set(1.8)
        with mock.patch.object(self.app, "_save_label_store", return_value=False):
            self.app._remember_manually()
            self.app._clear_manual_memory()
        self.assertEqual(self.app._current_song_entry()["playback_memory"], expected)

    def test_invalid_playback_does_not_save_memory(self):
        for variable in self.app.channel_vars.values():
            variable.set(False)
        self.app._recompute()
        self.app._remember_manually()
        self.app._start_playback(0, countdown=0)
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertEqual(self.app.visual_notes, ())
        self.assertEqual(self.app.visual_total, 0.0)

    def test_failed_atomic_write_preserves_disk_data_and_removes_temp_file(self):
        original = self.read_saved()
        with mock.patch.object(gui.os, "replace", side_effect=OSError("test write denied")):
            self.app._remember_manually()
        self.assertEqual(self.read_saved(), original)
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertFalse((self.base / "song_labels.json.tmp").exists())

    def test_reanalysis_cancels_pending_next_song_callback(self):
        callbacks = []
        with mock.patch.object(self.root, "after", side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app._on_next()
            self.app._analyze_song("A.mid")
            callbacks.pop(0)()
        self.assertEqual(self.app.current_song_name, "A.mid")
        self.assertFalse(self.app.session_active)

    def test_stopped_player_queued_finish_cannot_restart_playlist(self):
        self.app.mode_var.set("single")
        self.app._start_playback(0, countdown=0)
        callback = self.app.player.finish_cb
        self.app._on_stop()
        callback(True)
        self.app._drain_queue()
        self.assertFalse(self.app.session_active)

    def test_analyze_failure_disables_memory_for_previous_song(self):
        self.assertFalse(self.app._analyze_song("missing.mid"))
        self.assertIsNone(self.app.current_song_key)
        self.assertEqual(str(self.app.remember_btn.cget("state")), "disabled")
        self.app._remember_manually()
        self.assertNotIn("playback_memory", self.app._current_song_entry())
        self.assertEqual(self.app.visual_notes, ())
        self.assertEqual(self.app.visual_total, 0.0)
        self.assertEqual(self.app.visual_title, '')

    def test_fold_switch_selects_separately_computed_transpose(self):
        with mock.patch.object(gui, "find_best_transpose", side_effect=lambda notes, fold: -2 if fold else 3):
            self.app.fold_var.set(False)
            self.app._on_mapping_change()
            self.assertEqual(self.app.transpose_var.get(), "3")
            self.app.fold_var.set(True)
            self.app._on_mapping_change()
            self.assertEqual(self.app.transpose_var.get(), "-2")
        self.assertEqual(self.app.suggested_offsets, {False: 3, True: -2})

    def test_old_memory_keeps_fixed_mode_and_new_fields_survive_roundtrip(self):
        self.app._save_memory("manual", normalize_memory({"channels": [0], "transpose": 0}))
        self.app._restore_memory()
        self.assertEqual(self.app.duration_mode_var.get(), "fixed")
        self.assertFalse(self.app.sustain_var.get())
        self.app.duration_mode_var.set("score")
        self.app.sustain_var.set(True)
        self.app._remember_manually()
        self.app._analyze_song("A.mid")
        self.assertEqual(self.app.duration_mode_var.get(), "score")
        self.assertTrue(self.app.sustain_var.get())

    def test_chord_mode_is_remembered_and_restored_for_manual_and_automatic_memory(self):
        self.app.duration_mode_var.set('chord')
        self.app.sustain_var.set(True)
        self.app._on_duration_change()
        self.app._start_playback(0, countdown=0)
        self.assertEqual(self.app._current_song_entry()['playback_memory']['auto']['duration_mode'], 'chord')
        self.app._on_stop()
        self.app._remember_manually()
        self.app._analyze_song('B.mid')
        self.app._analyze_song('A.mid')
        self.assertEqual(self.app.duration_mode_var.get(), 'chord')
        self.assertTrue(self.app.sustain_var.get())
        self.assertEqual(str(self.app.sustain_check.cget('state')), 'normal')
        self.assertIn('和弦长音', self.app.visual_detail)

    def test_chord_mode_preview_and_countdown_match_actual_held_voices(self):
        app = self.app
        app.tagged = [MidiNote(0, 1, 72, 0, 0, True), MidiNote(0, 1, 48, 1, 1),
                      MidiNote(.01, 1, 52, 1, 1), MidiNote(.5, 1, 50, 1, 1)]
        app.transpose_var.set('0')
        app.duration_mode_var.set('chord')
        app._on_duration_change()
        notes = {note.key: note for note in app.visual_notes}
        self.assertEqual(notes['Q'].end, .04)
        self.assertEqual(notes['Z'].end, 1)
        self.assertAlmostEqual(notes['X'].end - notes['X'].start, .04)
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(0, countdown=1)
            captured = app.visual_notes
            app.duration_mode_var.set('score')
            app._on_duration_change()
            callbacks.pop(0)()
        self.assertEqual(app.visual_notes, captured)
        self.assertIn('和弦长音', app.visual_detail)
        self.assertEqual(app._current_song_entry()['playback_memory']['auto']['duration_mode'], 'chord')

    def test_countdown_captures_duration_mode_and_pedal(self):
        callbacks = []
        with mock.patch.object(self.root, "after", side_effect=lambda delay, cb: callbacks.append(cb)):
            self.app.duration_mode_var.set("melody")
            self.app.sustain_var.set(True)
            self.app._start_playback(0, countdown=1)
            self.app.duration_mode_var.set("fixed")
            self.app.sustain_var.set(False)
            callbacks.pop(0)()
        memory = self.app._current_song_entry()["playback_memory"]["auto"]
        self.assertEqual(memory["duration_mode"], "melody")
        self.assertTrue(memory["sustain_pedal"])

    def test_visual_preview_follows_mode_speed_and_selected_start(self):
        app = self.app
        self.assertTrue(app.visual_notes)
        app.duration_mode_var.set('fixed')
        app._on_duration_change()
        self.assertTrue(all(abs(note.end - note.start - .04) < 1e-6 for note in app.visual_notes))
        app.duration_mode_var.set('score')
        app._on_duration_change()
        app.speed_var.set(1.5)
        app._on_speed_change()
        app._jump_to_label('chorus')
        self.assertEqual(app.visual_total, app._display_total())
        self.assertEqual(app.visual_snapshot.position, app.visual_total * .6)
        self.assertEqual(app.visual_snapshot.active_note_ids, ())
        self.assertTrue(any(note.end - note.start > .1 for note in app.visual_notes))
        self.assertIn('原谱音长 · 1.50x', app.visual_detail)

    def test_countdown_keeps_score_and_metadata_of_the_actual_arrangement(self):
        app = self.app
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(.25, countdown=1)
            notes, detail = app.visual_notes, app.visual_detail
            self.assertEqual(app.visual_snapshot.phase, 'countdown')
            self.assertEqual(app.visual_snapshot.active_note_ids, ())
            app.duration_mode_var.set('fixed')
            app._on_duration_change()
            app.speed_var.set(2)
            app._on_speed_change()
            self.assertEqual(app.visual_notes, notes)
            callbacks.pop(0)()
        self.assertEqual(app.visual_notes, notes)
        self.assertEqual(app.visual_detail, detail)
        self.assertEqual(app.visual_total, app.player.total)
        self.assertEqual(app.visual_snapshot.position, app.player.play_time)

    def test_visual_poll_uses_player_snapshot_and_stop_clears_all_highlights(self):
        app = self.app
        app._start_playback(0, countdown=0)
        actual = gui.PlaybackSnapshot(.2, 'playing', (0,), ((0, 0),))
        app.player.get_visual_state = mock.Mock(return_value=actual)
        with mock.patch.object(self.root, 'after'):
            app._visual_tick()
        self.assertIs(app.visual_snapshot, actual)
        self.assertIs(app.visual_view.snapshot, actual)
        app._on_stop()
        with mock.patch.object(self.root, 'after'):
            app._visual_tick()
        self.assertEqual(app.visual_view.snapshot.phase, 'stopped')
        self.assertEqual(app.visual_view.snapshot.active_note_ids, ())
        self.assertEqual(app.visual_view.snapshot.recent_triggers, ())

    def test_floating_view_shares_score_display_options_and_current_state(self):
        app = self.app
        # 窗口先隐藏再交给被测函数，避免测试时在桌面弹窗或抢占焦点。
        window = tk.Toplevel(self.root)
        window.withdraw()
        with mock.patch.object(gui.tk, 'Toplevel', return_value=window):
            app._open_visual_window()
        self.assertEqual(app.floating_visual.index.notes, app.visual_notes)
        self.assertIs(app.floating_visual.view_mode, app.visual_mode_var)
        self.assertIs(app.floating_visual.window_span, app.visual_span_var)
        app._jump_to_label('chorus')
        with mock.patch.object(self.root, 'after'):
            app._visual_tick()
        self.assertIs(app.floating_visual.snapshot, app.visual_view.snapshot)
        app._analyze_song('B.mid')
        self.assertEqual(app.floating_visual.song_name, 'B.mid')
        self.assertEqual(app.floating_visual.index.notes, app.visual_notes)
        app._close_visual_window()
        self.assertFalse(window.winfo_exists())
        self.assertIsNone(app.floating_visual)

    def test_unavailable_preview_follows_mapping_settings_and_is_shared_with_popup(self):
        app = self.app
        app.tagged = [MidiNote(0, 1, pitch, 0, 0, True) for pitch in (36, 60, 61, 84)]
        app.transpose_var.set('0')
        app.fold_var.set(False)
        app._refresh_total()
        self.assertEqual([note.pitch for note in app.visual_unavailable], [36, 61, 84])
        window = tk.Toplevel(self.root)
        window.withdraw()
        with mock.patch.object(gui.tk, 'Toplevel', return_value=window):
            app._open_visual_window()
        self.assertEqual(app.floating_visual.unavailable_index.notes, app.visual_unavailable)
        app.fold_var.set(True)
        app._refresh_total()
        self.assertEqual([note.pitch for note in app.visual_unavailable], [61])
        self.assertEqual(app.floating_visual.unavailable_index.notes, app.visual_unavailable)
        app._analyze_song('B.mid')
        self.assertEqual(app.visual_unavailable, ())
        self.assertEqual(app.floating_visual.unavailable_index.notes, ())

    def test_countdown_retains_unavailable_notes_from_actual_configuration(self):
        app = self.app
        app.tagged = [MidiNote(0, 1, pitch, 0, 0, True) for pitch in (36, 60)]
        app.transpose_var.set('0')
        app.fold_var.set(False)
        callbacks = []
        with mock.patch.object(self.root, 'after', side_effect=lambda delay, cb: callbacks.append(cb)):
            app._start_playback(0, countdown=1)
            captured = app.visual_unavailable
            self.assertEqual([note.pitch for note in captured], [36])
            app.fold_var.set(True)
            app._on_mapping_change()
            app.speed_var.set(2)
            app._on_speed_change()
            self.assertEqual(app.visual_unavailable, captured)
            callbacks.pop(0)()
        self.assertEqual(app.visual_unavailable, captured)
        self.assertEqual(app.visual_total, app.player.total)

    def test_no_playable_notes_still_preview_red_notes_and_do_not_start_player(self):
        app = self.app
        app.tagged = [MidiNote(0, 1, 36, 0, 0, True)]
        app.transpose_var.set('0')
        app.fold_var.set(False)
        app._start_playback(0, countdown=0)
        self.assertFalse(app.session_active)
        self.assertIsNone(app.player)
        self.assertEqual(app.visual_notes, ())
        self.assertEqual([note.pitch for note in app.visual_unavailable], [36])
        app._analyze_song('missing.mid')
        self.assertEqual(app.visual_unavailable, ())


if __name__ == "__main__":
    unittest.main()
