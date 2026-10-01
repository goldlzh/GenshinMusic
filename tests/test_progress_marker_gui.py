"""Check rendered marker positions against the real themed slider, without game input."""
from tkinter import ttk
import unittest
from unittest import mock

from test_song_memory import AppMemoryTests as AppFixture, gui


class ProgressMarkerGuiTests(unittest.TestCase):
    close_root = AppFixture.close_root
    _set_up_app = AppFixture.setUp
    RATIOS = (0.0, .25, 101.880 / 212.755, .5, .75, 1.0)

    def setUp(self):
        self._set_up_app()
        patcher = mock.patch.object(gui, 'pydirectinput')
        patcher.start()
        self.addCleanup(patcher.stop)
        # Geometry needs a mapped window; keep it invisible and cancel app polling.
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.attributes('-alpha', 0.0)
        self.root.overrideredirect(True)
        self.root.deiconify()
        labels = self.app._current_labels(create=True)
        labels[:] = [dict(id=f'probe_{i}', name='', ratio=ratio)
                     for i, ratio in enumerate(self.RATIOS)]
        self.layout(820)

    def layout(self, width):
        self.root.geometry(f'{width}x900+0+0')
        self.root.update()
        self.root.update_idletasks()
        self.assertGreater(self.app.seek_scale.winfo_width(), 100)

    def assert_markers_aligned(self):
        canvas, scale = self.app.marker_canvas, self.app.seek_scale
        self.assertEqual(canvas.winfo_width(), scale.winfo_width())
        self.assertEqual(canvas.winfo_rootx(), scale.winfo_rootx())
        for i, ratio in enumerate(self.RATIOS):
            with self.subTest(ratio=ratio):
                self.app.seek_var.set(ratio)
                self.root.update_idletasks()
                # Independent oracle: locate the pixels of the actual theme slider.
                pixels = [x for x in range(scale.winfo_width())
                          if 'slider' in str(scale.identify(x, scale.winfo_height() // 2))]
                self.assertTrue(pixels)
                slider_x = scale.winfo_rootx() + (pixels[0] + pixels[-1]) / 2
                lines = [item for item in canvas.find_withtag(f'marker_probe_{i}')
                         if canvas.type(item) == 'line']
                self.assertEqual(len(lines), 1)
                marker_x = canvas.winfo_rootx() + canvas.coords(lines[0])[0]
                # Themes quantize slider placement to integer pixels.
                self.assertAlmostEqual(marker_x, slider_x, delta=2.0)

    def test_markers_follow_slider_at_endpoints_and_screenshot_ratio_across_themes_and_resize(self):
        style = ttk.Style(self.root)
        for theme in style.theme_names():
            style.theme_use(theme)
            for width in (820, 1264):
                with self.subTest(theme=theme, width=width):
                    self.layout(width)
                    self.assert_markers_aligned()

    def test_time_text_width_changes_keep_both_tracks_aligned(self):
        for width in (12, 23, 35):
            with self.subTest(time_width=width):
                self.app.time_label.configure(width=width, text='1:41.880/3:32.755')
                self.layout(1100)
                self.assert_markers_aligned()

    def test_range_handles_align_with_real_slider_and_two_click_binding_selects(self):
        app = self.app
        for theme in ttk.Style(self.root).theme_names():
            ttk.Style(self.root).theme_use(theme)
            self.layout(1100)
            app._set_play_range(.2, .8)
            for key in ('start', 'end'):
                app.seek_var.set(app.play_range[key])
                self.root.update_idletasks()
                pixels = [x for x in range(app.seek_scale.winfo_width())
                          if 'slider' in str(app.seek_scale.identify(x, app.seek_scale.winfo_height() // 2))]
                slider_x = app.seek_scale.winfo_rootx() + (pixels[0] + pixels[-1]) / 2
                pointer = next(i for i in app.range_canvas.find_withtag(f'range_{key}')
                               if app.range_canvas.type(i) == 'polygon')
                pointer_x = app.range_canvas.winfo_rootx() + app.range_canvas.coords(pointer)[0]
                self.assertAlmostEqual(slider_x, pointer_x, delta=2)
        app._reset_play_range()
        app.region_select_btn.invoke()
        for ratio in (.3, .8):
            app.seek_var.set(ratio)
            self.root.update_idletasks()
            pixels = [x for x in range(app.seek_scale.winfo_width())
                      if 'slider' in str(app.seek_scale.identify(x, app.seek_scale.winfo_height() // 2))]
            x, y = round((pixels[0] + pixels[-1]) / 2), app.seek_scale.winfo_height() // 2
            app.seek_scale.event_generate('<ButtonPress-1>', x=x, y=y)
            app.seek_scale.event_generate('<ButtonRelease-1>', x=x, y=y)
            self.root.update()
        self.assertFalse(app._region_selecting)
        self.assertAlmostEqual(app.play_range['start'], .3, delta=.005)
        self.assertAlmostEqual(app.play_range['end'], .8, delta=.005)
        self.assertEqual(app.seek_var.get(), app.play_range['start'])
        self.assertFalse(app.user_dragging)

    def test_redrawing_markers_never_changes_progress_or_dispatches_scale_command(self):
        app = self.app
        ratio = self.RATIOS[2]
        app.seek_var.set(ratio)
        self.root.update_idletasks()
        writes = []
        trace = app.seek_var.trace_add('write', lambda *args: writes.append(args))
        command = mock.Mock()
        app.seek_scale.configure(command=command)
        saved = self.base.joinpath('song_labels.json').read_bytes()
        try:
            for _ in range(10):
                app._draw_labels()
            self.assertEqual(app.seek_var.get(), ratio)
            self.assertEqual(writes, [])
            command.assert_not_called()
            self.assertEqual(self.base.joinpath('song_labels.json').read_bytes(), saved)
        finally:
            app.seek_var.trace_remove('write', trace)


del AppFixture
