"""谱面索引与隐藏 Canvas 验证；不启动播放器或发送键盘输入。"""

import tkinter as tk
import unittest
from unittest import mock
from types import SimpleNamespace

from midi_engine import PlaybackSnapshot, ScheduledNote, UnavailableNote
from performance_view import COLORS, KEYS, PerformanceView, ScheduleIndex, note_color, time_text


class ScheduleIndexTests(unittest.TestCase):
    def test_visible_window_keeps_notes_sustained_from_far_before_the_window(self):
        notes = [ScheduledNote(0, 30, 'A', True), ScheduledNote(1, 2, 'D', False),
                 ScheduledNote(15, 16, 'D', False), ScheduledNote(18, 19, 'D', False),
                 ScheduledNote(30.1, 31, 'A', True)]
        index = ScheduleIndex(notes)
        self.assertEqual({i for i, note in index.visible(14, 18)}, {0, 2})
        self.assertEqual({i for i, note in index.visible(16, 18)}, {0})
        self.assertEqual({i for i, note in index.visible(30, 32)}, {4})
        self.assertEqual(index.total, 31)

    def test_green_requires_actual_input_even_when_playhead_overlaps_note(self):
        note = ScheduledNote(0, 10, 'A', True)
        for phase in ('preview', 'ready', 'countdown', 'playing', 'paused', 'stopped', 'finished'):
            with self.subTest(phase=phase):
                self.assertNotEqual(note_color(0, note, PlaybackSnapshot(5, phase)), COLORS['active'])
        self.assertEqual(note_color(0, note, PlaybackSnapshot(5, 'playing', (0,))), COLORS['active'])
        self.assertEqual(note_color(0, note, PlaybackSnapshot(11, 'preview')), COLORS['past'])

    def test_unavailable_index_handles_overlapping_same_pitch_durations(self):
        index = ScheduleIndex([UnavailableNote(0, 30, 36, '超出音域'), UnavailableNote(1, 2, 36, '超出音域'),
                               UnavailableNote(15, 20, 36, '超出音域')])
        self.assertEqual({i for i, note in index.visible(10, 15)}, {0})
        self.assertEqual({i for i, note in index.visible(16, 18)}, {0, 2})


class CanvasTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.view = PerformanceView(self.root)
        self.view.pack(fill='both', expand=True)
        # 隐藏顶层窗口不执行子控件布局；指定绘图区的测量值验证真实 Canvas 项。
        for canvas, width, height in ((self.view.canvas, 800, 260), (self.view.keyboard_canvas, 800, 66)):
            for name, value in (('winfo_width', width), ('winfo_height', height)):
                patcher = mock.patch.object(canvas, name, return_value=value)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.notes = (ScheduledNote(0, 5, 'A', True), ScheduledNote(1, 1.04, 'D', False),
                      ScheduledNote(3, 4, 'U', True), ScheduledNote(3, 4, 'Z', False))
        self.view.set_schedule(self.notes, '测试曲目.mid', '原谱音长 · 1.00x')

    def draw(self, snapshot, now=1):
        self.view.update_state(snapshot, wall_time=now, force=True)

    def test_wheel_browses_without_seeking_and_shift_allows_fine_steps(self):
        callback = mock.Mock(); self.view.on_seek = callback
        self.view.set_schedule([ScheduledNote(0, 30, 'A', True)])
        snapshot = PlaybackSnapshot(5, 'paused')
        self.draw(snapshot)
        start = self.view._drawn_timeline[2]
        self.view._on_wheel(SimpleNamespace(delta=-120, state=0))
        self.assertAlmostEqual(self.view._manual_left, start + .8)
        self.assertFalse(self.view.follow_var.get())
        self.view._on_wheel(SimpleNamespace(delta=120, state=1))
        self.assertAlmostEqual(self.view._manual_left, start + .72)
        self.view._on_wheel(SimpleNamespace(delta=60, state=0))
        self.assertAlmostEqual(self.view._manual_left, start + .32)
        self.assertIs(self.view.snapshot, snapshot)
        callback.assert_not_called()
        self.assertTrue(self.view.canvas.bind('<MouseWheel>'))
        self.view.follow_position(); self.draw(snapshot)
        self.assertAlmostEqual(self.view._drawn_timeline[2], start)

    def test_click_uses_scrolled_timeline_and_clamps_to_playable_song(self):
        callback = mock.Mock(); self.view.on_seek = callback
        self.view.set_schedule([ScheduledNote(0, 30, 'A', True)])
        self.draw(PlaybackSnapshot(5, 'playing', (0,)))
        self.view._scroll_time('scroll', 5, 'units')
        margin, right, left, pixels = self.view._drawn_timeline
        self.view._on_score_click(SimpleNamespace(x=margin + pixels * 2.25))
        callback.assert_called_once_with(left + 2.25)
        callback.reset_mock()
        self.view._on_score_click(SimpleNamespace(x=margin - 1))
        callback.assert_not_called()
        self.view._scroll_time('moveto', '1')
        self.view._on_score_click(SimpleNamespace(x=right))
        callback.assert_called_once_with(30)

    def test_click_on_note_head_snaps_but_extension_does_not_rewind(self):
        callback = mock.Mock(); self.view.on_seek = callback
        self.draw(PlaybackSnapshot(1, 'preview'))
        margin, _, left, pixels = self.view._drawn_timeline
        # 当前图元分别模拟长音音头和延长线；二者引用同一音符，但定位语义不同。
        for item, offset, expected in ((self.view._items[0][1], 2 / pixels, 0),
                                       (self.view._items[0][0], 2, 2)):
            with mock.patch.object(self.view.canvas, 'find_withtag', return_value=(item,)):
                self.view._on_score_click(SimpleNamespace(x=margin + (offset - left) * pixels))
            self.assertAlmostEqual(callback.call_args.args[0], expected)

    def test_preview_renders_previous_long_tails_grey_and_precision_is_preserved(self):
        self.draw(PlaybackSnapshot(2.345, 'preview'))
        self.assertEqual(self.view.canvas.itemcget(self.view._items[0][0], 'fill'), COLORS['past'])
        self.assertIn('0:02.345', self.view.status_var.get())
        self.assertEqual(time_text(59.9999), '1:00.000')
        self.assertEqual(time_text(-.125), '-0:00.125')

    def test_empty_or_unplayable_score_does_not_trigger_playback(self):
        callback = mock.Mock(); self.view.on_seek = callback
        self.view.set_schedule([], unavailable=[UnavailableNote(0, 2, 36, '超出音域')])
        self.draw(PlaybackSnapshot())
        self.view._on_score_click(SimpleNamespace(x=300))
        callback.assert_not_called()

    def test_staff_and_roll_highlight_actual_chord_and_release_on_pause(self):
        for mode in ('五线谱时值', '长条谱'):
            with self.subTest(mode=mode):
                self.view.view_mode.set(mode)
                self.draw(PlaybackSnapshot(1.02, 'playing', (0, 1)))
                for i, key in ((0, 'A'), (1, 'D')):
                    self.assertEqual(self.view.canvas.itemcget(self.view._items[i][0], 'fill'), COLORS['active'])
                    self.assertEqual(self.view.keyboard_canvas.itemcget(self.view._key_items[key][0], 'fill'),
                                     COLORS['active'])
                self.assertEqual(len(self.view._key_items), 21)
                self.assertIn('按下 2 键', self.view.status_var.get())
                self.draw(PlaybackSnapshot(1.02, 'paused'))
                for key in KEYS:
                    self.assertEqual(self.view.keyboard_canvas.itemcget(self.view._key_items[key][0], 'fill'),
                                     COLORS['key'])
                self.assertIn('已暂停', self.view.status_var.get())

    def test_recent_short_note_has_gold_border_but_is_not_shown_as_held(self):
        snapshot = PlaybackSnapshot(1.1, 'playing', (0,), ((1, 1.0),))
        self.draw(snapshot, now=1.1)
        rect = self.view._key_items['D'][0]
        self.assertEqual(self.view.keyboard_canvas.itemcget(rect, 'fill'), COLORS['key'])
        self.assertEqual(self.view.keyboard_canvas.itemcget(rect, 'outline'), COLORS['recent'])
        self.draw(snapshot, now=1.3)
        self.assertEqual(self.view.keyboard_canvas.itemcget(rect, 'outline'), COLORS['key'])

    def test_switching_song_removes_old_note_items_and_key_highlights(self):
        self.draw(PlaybackSnapshot(1, 'playing', (0,)))
        old_ids = {item for items in self.view._items.values() for item in items}
        self.view.set_schedule([ScheduledNote(0, 1, 'Q', True)], '下一首.mid')
        self.draw(PlaybackSnapshot())
        self.assertFalse(old_ids.intersection(self.view.canvas.find_all()))
        self.assertEqual(self.view.keyboard_canvas.itemcget(self.view._key_items['A'][0], 'fill'), COLORS['key'])
        self.assertIn('下一首.mid', self.view.status_var.get())

    def test_scroll_retains_long_note_tail_and_culls_offscreen_items(self):
        self.view.set_schedule([ScheduledNote(0, 30, 'A', True), ScheduledNote(1, 2, 'D', False),
                                ScheduledNote(15, 16, 'Q', True)])
        self.draw(PlaybackSnapshot(1, 'playing', (0, 1)))
        self.draw(PlaybackSnapshot(15, 'playing', (0, 2)))
        self.assertEqual(set(self.view._items), {0, 2})
        long_bar = self.view._items[0][0]
        self.assertGreater(self.view.canvas.coords(long_bar)[2], 58)
        self.assertEqual(self.view.canvas.itemcget(long_bar, 'fill'), COLORS['active'])

    def test_empty_score_and_window_zoom_render_in_both_views(self):
        for mode in ('五线谱时值', '长条谱'):
            for span in ('4 秒', '8 秒', '12 秒'):
                with self.subTest(mode=mode, span=span):
                    self.view.view_mode.set(mode)
                    self.view.window_span.set(span)
                    self.draw(PlaybackSnapshot(3, 'preview'))
                    self.view.set_schedule([])
                    self.draw(PlaybackSnapshot())
                    self.assertFalse(self.view._items)
                    self.assertIn('当前未按键', self.view.status_var.get())
                    self.view.set_schedule(self.notes)

    def test_unavailable_high_low_and_accidental_notes_are_red_and_fit_both_views(self):
        rejected = [UnavailableNote(1, 3, pitch, '超出音域' if pitch != 61 else '无对应琴键')
                    for pitch in (24, 61, 108)]
        self.view.set_schedule(self.notes, unavailable=rejected)
        for mode in ('五线谱时值', '长条谱'):
            with self.subTest(mode=mode):
                self.view.view_mode.set(mode)
                self.draw(PlaybackSnapshot(1.5, 'playing', (0,)))
                self.assertEqual(len(self.view._unavailable_items), 3)
                for bar, head, label in self.view._unavailable_items.values():
                    self.assertEqual(self.view.canvas.itemcget(bar, 'outline'), COLORS['unavailable'])
                    x1, y1, x2, y2 = self.view.canvas.coords(bar)
                    self.assertGreater(y1, 23)
                    self.assertLess(y2, 260)
                green_keys = {key for key, (rect, text) in self.view._key_items.items()
                              if self.view.keyboard_canvas.itemcget(rect, 'fill') == COLORS['active']}
                self.assertEqual(green_keys, {'A'})
                self.assertIn('无法演奏 3 音', self.view.status_var.get())
                self.draw(PlaybackSnapshot(3.5, 'playing'))
                self.assertTrue(all(self.view.canvas.itemcget(items[0], 'outline') == COLORS['unavailable_past']
                                    for items in self.view._unavailable_items.values()))

    def test_browse_rejected_intro_and_tail_then_resume_following(self):
        self.view.set_schedule(self.notes, unavailable=[UnavailableNote(-20, -19, 36, '超出音域'),
                                                       UnavailableNote(30, 31, 84, '超出音域')])
        self.view._scroll_time('moveto', '0')
        self.draw(PlaybackSnapshot(2, 'playing', (0,)))
        self.assertEqual(set(self.view._unavailable_items), {0})
        self.assertFalse(self.view.follow_var.get())
        self.view._scroll_time('moveto', '1')
        self.draw(PlaybackSnapshot(2, 'playing', (0,)))
        self.assertEqual(set(self.view._unavailable_items), {1})
        self.view.follow_var.set(True)
        self.draw(PlaybackSnapshot(2, 'playing', (0,)))
        self.assertFalse(self.view._unavailable_items)
        self.assertIsNone(self.view._manual_left)

    def test_all_unavailable_preview_clears_when_another_song_is_loaded(self):
        self.view.set_schedule([], unavailable=[UnavailableNote(0, 2, 84, '超出音域')])
        self.draw(PlaybackSnapshot())
        self.assertTrue(self.view._unavailable_items)
        self.assertFalse(self.view._items)
        self.assertFalse(any(self.view.canvas.itemcget(i, 'text').startswith('选择歌曲')
                             for i in self.view.canvas.find_withtag('cursor') if self.view.canvas.type(i) == 'text'))
        old_ids = {item for items in self.view._unavailable_items.values() for item in items}
        self.view.set_schedule(self.notes)
        self.draw(PlaybackSnapshot())
        self.assertFalse(self.view._unavailable_items)
        self.assertFalse(old_ids.intersection(self.view.canvas.find_all()))

    def test_play_range_tracks_seconds_in_both_views_without_changing_playback_or_notes(self):
        callback = mock.Mock(); self.view.on_seek = callback
        for mode in ('五线谱时值', '长条谱'):
            with self.subTest(mode=mode):
                self.view.view_mode.set(mode)
                snapshot = PlaybackSnapshot(1.5, 'playing', (0,))
                self.draw(snapshot)
                index = self.view.index
                self.view.set_play_range(2, 4)
                margin, _, left, pixels = self.view._drawn_timeline
                expected = [margin + (seconds - left) * pixels for seconds in (2, 4)]
                fill, = self.view.canvas.find_withtag('play-range-fill')
                coords = self.view.canvas.coords(fill)
                self.assertAlmostEqual(coords[0], expected[0])
                self.assertAlmostEqual(coords[2], expected[1])
                for tag, x in zip(('play-range-start', 'play-range-end'), expected):
                    line, = self.view.canvas.find_withtag(tag)
                    self.assertAlmostEqual(self.view.canvas.coords(line)[0], x)
                stacking = list(self.view.canvas.find_all())
                background, = self.view.canvas.find_withtag('grid-background')
                self.assertLess(stacking.index(background), stacking.index(fill))
                for item in self.view.canvas.find_withtag('grid'):
                    if item != background:
                        self.assertGreater(stacking.index(item), stacking.index(fill))
                self.assertGreater(stacking.index(self.view._items[0][0]), stacking.index(fill))
                self.assertEqual(self.view.canvas.itemcget(self.view._items[0][0], 'fill'), COLORS['active'])
                self.assertEqual(self.view.keyboard_canvas.itemcget(self.view._key_items['A'][0], 'fill'), COLORS['active'])
                self.assertIs(self.view.snapshot, snapshot)
                self.assertIs(self.view.index, index)
                self.assertEqual(self.view.index.notes, self.notes)
                self.assertIn('选区 0:02.000 ～ 0:04.000', self.view.status_var.get())
        callback.assert_not_called()

    def test_play_range_clips_to_browsed_window_without_moving_it_or_inventing_endpoints(self):
        self.view.set_schedule([ScheduledNote(0, 30, 'A', True)])
        self.view.window_span.set('4 秒')
        self.view.follow_var.set(False)
        self.view._manual_left = 5
        snapshot = PlaybackSnapshot(7, 'paused')
        self.draw(snapshot)
        self.view.set_play_range(2, 12)
        margin, right, left, _ = self.view._drawn_timeline
        self.assertEqual(left, 5)
        fill, = self.view.canvas.find_withtag('play-range-fill')
        coords = self.view.canvas.coords(fill)
        self.assertEqual((coords[0], coords[2]), (margin, right))
        self.assertFalse(self.view.canvas.find_withtag('play-range-start'))
        self.assertFalse(self.view.canvas.find_withtag('play-range-end'))
        self.assertFalse(self.view.follow_var.get())
        self.view._manual_left = 20
        self.draw(snapshot)
        self.assertFalse(self.view.canvas.find_withtag('play-range'))
        self.assertEqual(self.view.play_range, (2, 12))

    def test_selection_anchor_is_visual_only_and_existing_seek_callback_keeps_full_song_coordinates(self):
        callback = mock.Mock(); self.view.on_seek = callback
        snapshot = PlaybackSnapshot(2.5, 'paused')
        self.draw(snapshot)
        self.view.set_play_range(2, 3)
        self.view.set_selection_anchor(1.25)
        margin, _, left, pixels = self.view._drawn_timeline
        line, = self.view.canvas.find_withtag('selection-anchor-line')
        self.assertAlmostEqual(self.view.canvas.coords(line)[0], margin + (1.25 - left) * pixels)
        self.assertIn('首点 0:01.250', self.view.status_var.get())
        self.assertIn('再点击一个位置完成选段', self.view.status_var.get())
        self.assertIs(self.view.snapshot, snapshot)
        for item in self.view.canvas.find_withtag('play-range') + self.view.canvas.find_withtag('selection-anchor'):
            self.assertEqual(self.view.canvas.itemcget(item, 'state'), 'disabled')
        callback.assert_not_called()
        self.view._on_score_click(SimpleNamespace(x=margin + (1 - left) * pixels))
        callback.assert_called_once_with(1)
        self.view.set_selection_anchor(None)
        self.assertFalse(self.view.canvas.find_withtag('selection-anchor'))
        self.assertNotIn('首点', self.view.status_var.get())

    def test_narrow_range_captions_remain_inside_the_plot_without_overlapping(self):
        self.view.set_schedule([ScheduledNote(0, 30, 'A', True)])
        self.view.window_span.set('4 秒')
        self.view.follow_var.set(False)
        self.view._manual_left = 5
        self.draw(PlaybackSnapshot(7, 'paused'))
        for start, end in ((5, 5.1), (8.9, 9)):
            with self.subTest(start=start, end=end):
                self.view.set_play_range(start, end)
                margin, right, _, _ = self.view._drawn_timeline
                captions = self.view.canvas.find_withtag('play-range-caption')
                self.assertEqual(len(captions), 2)
                bounds = [self.view.canvas.bbox(item) for item in captions]
                self.assertTrue(all(margin <= box[0] < box[2] <= right for box in bounds))
                self.assertLessEqual(bounds[0][3], bounds[1][1])

    def test_range_inputs_are_sorted_clamped_and_can_be_cleared_without_stale_drawings(self):
        self.draw(PlaybackSnapshot(1, 'preview'))
        self.view.set_play_range(9, -2)
        self.assertEqual(self.view.play_range, (0, 5))
        self.view.set_selection_anchor(9)
        self.assertEqual(self.view.selection_anchor, 5)
        for invalid in (float('nan'), float('inf'), 'invalid', None):
            with self.subTest(invalid=invalid):
                self.view.set_play_range(1, 2)
                self.view.set_play_range(invalid, 2)
                self.assertIsNone(self.view.play_range)
                self.assertFalse(self.view.canvas.find_withtag('play-range'))
                self.view.set_selection_anchor(1)
                self.view.set_selection_anchor(invalid)
                self.assertIsNone(self.view.selection_anchor)
                self.assertFalse(self.view.canvas.find_withtag('selection-anchor'))
        self.view.set_play_range(3, 3)
        self.assertIsNone(self.view.play_range)
        self.view.set_play_range(1, 2)
        self.view.set_play_range()
        self.assertFalse(self.view.canvas.find_withtag('play-range'))

    def test_new_schedule_immediately_removes_range_and_first_point_without_extending_empty_scores(self):
        self.draw(PlaybackSnapshot(1, 'preview'))
        self.view.set_play_range(1, 4)
        self.view.set_selection_anchor(2)
        self.view.set_schedule([], unavailable=[UnavailableNote(0, 10, 84, '超出音域')])
        self.assertIsNone(self.view.play_range)
        self.assertIsNone(self.view.selection_anchor)
        self.assertFalse(self.view.canvas.find_withtag('play-range'))
        self.assertFalse(self.view.canvas.find_withtag('selection-anchor'))
        self.view.set_play_range(1, 4)
        self.view.set_selection_anchor(2)
        self.draw(PlaybackSnapshot())
        self.assertIsNone(self.view.play_range)
        self.assertIsNone(self.view.selection_anchor)
        self.assertEqual(self.view.index.total, 0)


if __name__ == '__main__':
    unittest.main()
