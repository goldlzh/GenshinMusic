"""Real geometry checks: transparent windows, temporary data, no MIDI/key output."""
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock

from test_song_memory import AppMemoryTests as AppFixture


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class WindowLayoutTests(unittest.TestCase):
    setUp = AppFixture.setUp
    close_root = AppFixture.close_root

    def show(self, geometry):
        self.root.attributes('-alpha', 0)
        self.root.geometry(geometry)
        self.root.deiconify()
        self.root.update()

    def reveal(self, widget):
        # If a scrolling ancestor exists, use its public reveal operation.
        parent = widget.master
        while parent is not None:
            if hasattr(parent, 'see'):
                parent.see(widget)
            parent = parent.master
        self.root.update()

    def assert_visible(self, widget):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        right, bottom = x + widget.winfo_width(), y + widget.winfo_height()
        parent = widget.master
        while parent is not None:
            self.assertGreaterEqual(x, parent.winfo_rootx() - 2, str(widget))
            self.assertGreaterEqual(y, parent.winfo_rooty() - 2, str(widget))
            if widget.winfo_width() <= parent.winfo_width():
                self.assertLessEqual(right, parent.winfo_rootx() + parent.winfo_width() + 2, str(widget))
            if widget.winfo_height() <= parent.winfo_height():
                self.assertLessEqual(bottom, parent.winfo_rooty() + parent.winfo_height() + 2, str(widget))
            parent = parent.master

    def test_usb_controls_remain_reachable_and_stop_stays_visible_in_short_window(self):
        self.show('640x600')
        tab_id = next(t for t in self.app.main_tabs.tabs() if 'USB' in self.app.main_tabs.tab(t, 'text'))
        self.app.main_tabs.select(tab_id)
        self.root.update()
        tab = self.root.nametowidget(tab_id)
        controls = [w for w in descendants(tab) if w.winfo_class() in ('TButton', 'TSpinbox', 'TCheckbutton')]
        for widget in controls:
            with self.subTest(widget=str(widget)):
                self.reveal(widget)
                self.assert_visible(widget)
                self.assert_visible(self.app.stop_btn)

    def test_song_settings_remain_reachable_with_many_channels_and_narrow_window(self):
        for index in range(16):
            ttk.Checkbutton(self.app.channel_frame, text=f'通道 {index}').grid(row=index // 4, column=index % 4)
        self.show('640x600')
        for widget in (self.app.clear_memory_btn, self.app.region_select_btn, self.app.melody_combo,
                       self.app.seek_scale, self.app.song_combo):
            with self.subTest(widget=str(widget)):
                self.reveal(widget)
                self.assert_visible(widget)
                self.assert_visible(self.app.stop_btn)

    def test_floating_score_toolbar_remains_reachable_at_enlarged_font(self):
        self.root.tk.call('tk', 'scaling', 2.0)
        self.show('820x800')
        self.app.visual_topmost_var.set(False)
        self.app._open_visual_window()
        window = self.app.visual_window
        window.attributes('-alpha', 0)
        window.geometry('680x360')
        self.root.update()
        for widget in descendants(self.app.floating_visual):
            if widget.winfo_class() in ('TCheckbutton', 'TCombobox'):
                self.reveal(widget)
                self.assert_visible(widget)

    def test_switching_tabs_restores_song_controls_without_changing_settings(self):
        self.show('820x800')
        before = self.app.speed_var.get(), self.app.duration_mode_var.get()
        self.app.main_tabs.select(self.app.live_tab)
        self.root.update()
        self.assertFalse(self.app.region_select_btn.winfo_ismapped())
        self.app.main_tabs.select(self.app.visual_view)
        self.root.update()
        self.reveal(self.app.region_select_btn)
        self.assert_visible(self.app.region_select_btn)
        self.assertEqual(before, (self.app.speed_var.get(), self.app.duration_mode_var.get()))

    def test_score_wheel_browses_time_without_scrolling_form(self):
        from midi_engine import PlaybackSnapshot, ScheduledNote
        self.show('640x600')
        view = self.app.visual_view
        view.set_schedule([ScheduledNote(0, 30, 'A', True)])
        view.update_state(PlaybackSnapshot(5, 'paused'), force=True)
        self.reveal(view.canvas)
        old_position = self.app.body_scroll.canvas.yview()
        view.canvas.event_generate('<MouseWheel>', delta=-120, x=100, y=50)
        self.root.update()
        self.assertFalse(view.follow_var.get())
        self.assertEqual(old_position, self.app.body_scroll.canvas.yview())

    def test_resize_and_tab_switch_recompute_bounds_before_revealing_controls(self):
        self.show('640x600')
        for geometry in ('820x800', '820x980', '1200x900'):
            self.app.main_tabs.select(self.app.live_tab)
            self.root.update()
            self.show(geometry)
            self.app.main_tabs.select(self.app.visual_view)
            self.root.update()
            for widget in descendants(self.app.body_scroll.content):
                if widget.winfo_class() == 'TButton' and widget.winfo_ismapped():
                    self.reveal(widget)
                    self.assert_visible(widget)


class HighDpiWindowLayoutTests(WindowLayoutTests):
    def setUp(self):
        make_root = tk.Tk

        def scaled_root(*args, **kwargs):
            root = make_root(*args, **kwargs)
            # Set before constructing any widgets, approximating Windows 200%.
            root.tk.call('tk', 'scaling', 192 / 72)
            return root

        with mock.patch.object(tk, 'Tk', side_effect=scaled_root):
            super().setUp()


class OverflowContainerTests(unittest.TestCase):
    def setUp(self):
        from scrollable_frame import ScrollableFrame
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.addCleanup(self.root.destroy)
        self.scroll = ScrollableFrame(self.root)
        self.scroll.pack(fill='both', expand=True)
        self.fields = []
        for row in range(24):
            entry = ttk.Entry(self.scroll.content, width=70)
            entry.grid(row=row, column=0, pady=3)
            self.fields.append(entry)
        self.root.geometry('400x260')
        self.root.deiconify()
        self.root.update()

    def test_focus_reveals_field_and_bars_disappear_after_growing(self):
        last = self.fields[-1]
        last.event_generate('<FocusIn>')
        self.root.update()
        self.assertGreater(self.scroll.canvas.yview()[0], 0)
        bottom = last.winfo_rooty() + last.winfo_height()
        self.assertLessEqual(bottom, self.scroll.canvas.winfo_rooty() + self.scroll.canvas.winfo_height())
        self.root.geometry('1400x1100')
        self.root.update()
        self.assertFalse(self.scroll.vertical.winfo_ismapped())
        self.assertFalse(self.scroll.horizontal.winfo_ismapped())
        self.assertEqual(self.scroll.canvas.yview(), (0.0, 1.0))

    def test_wheel_scrolls_form_and_destroy_removes_only_own_bindings(self):
        unrelated = self.root.bind('<MouseWheel>', lambda e: None, add='+')
        self.fields[0].event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertGreater(self.scroll.canvas.yview()[0], 0)
        self.fields[0].event_generate('<MouseWheel>', delta=-120, state=1)
        self.root.update()
        self.assertGreater(self.scroll.canvas.xview()[0], 0)
        ids = [identifier for _, identifier in self.scroll._bindings]
        self.scroll.destroy()
        self.root.update()
        remaining = self.root.bind('<MouseWheel>')
        self.assertIn(unrelated, remaining)
        self.assertTrue(all(identifier not in remaining for identifier in ids))

    def test_native_text_wheel_does_not_also_scroll_the_form(self):
        log = tk.Text(self.scroll.content, width=20, height=3)
        log.grid(row=0, column=1)
        log.insert('1.0', '\n'.join(str(i) for i in range(100)))
        self.root.update()
        self.scroll.see(log)
        before = self.scroll.canvas.yview()
        log.event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertGreater(log.yview()[0], 0)
        self.assertEqual(before, self.scroll.canvas.yview())

    def test_native_combobox_wheel_does_not_also_scroll_the_form(self):
        combo = ttk.Combobox(self.scroll.content, values=('a', 'b', 'c'), state='readonly')
        combo.grid(row=0, column=1)
        combo.current(0)
        self.root.update()
        self.scroll.see(combo)
        before = self.scroll.canvas.yview()
        combo.event_generate('<MouseWheel>', delta=-120)
        self.root.update()
        self.assertEqual(combo.get(), 'b')
        self.assertEqual(before, self.scroll.canvas.yview())

    def test_top_level_can_cancel_pending_work_before_destroying_children(self):
        self.scroll._schedule()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.scroll.destroy()
        self.root.update()


del AppFixture
