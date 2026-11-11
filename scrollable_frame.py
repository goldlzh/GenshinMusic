"""Overflow protection for forms; child canvases retain their own wheel bindings."""
import tkinter as tk
from tkinter import ttk


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        # Its parent owns viewport size; hiding bars must not renegotiate the form.
        self.grid_propagate(False)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=1, height=1,
                                xscrollincrement=1, yscrollincrement=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.content = ttk.Frame(self.canvas)
        self._item = self.canvas.create_window(0, 0, window=self.content, anchor='nw')
        self.vertical = ttk.Scrollbar(self, command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self._pending = None
        self._closed = False
        self.bind('<Configure>', self._schedule)
        self.content.bind('<Configure>', self._schedule)
        self.canvas.bind('<Configure>', self._schedule)
        self._top = self.winfo_toplevel()
        self._bindings = [(seq, self._top.bind(seq, callback, add='+')) for seq, callback in (
            ('<MouseWheel>', self._wheel), ('<Button-4>', self._wheel),
            ('<Button-5>', self._wheel), ('<FocusIn>', self._focus),
            ('<Configure>', self._child_geometry), ('<Map>', self._child_geometry),
            ('<Unmap>', self._child_geometry))]
        self.bind('<Destroy>', self._dispose, add='+')

    def _schedule(self, event=None):
        if not self._closed and self._pending is None:
            # Match callback ownership to the application's top-level shutdown.
            self._pending = self._top.after_idle(self._layout)

    def _layout(self):
        self._pending = None
        if self._closed:
            return
        requested_w = self.content.winfo_reqwidth()
        requested_h = self.content.winfo_reqheight()
        width, height = self.winfo_width(), self.winfo_height()
        need_x = need_y = False
        # Compute from the full viewport each time, so scrollbars disappear on grow.
        for _ in range(3):
            view_w = max(1, width - (self.vertical.winfo_reqwidth() if need_y else 0))
            view_h = max(1, height - (self.horizontal.winfo_reqheight() if need_x else 0))
            need_x, need_y = requested_w > view_w, requested_h > view_h
        if need_y:
            self.vertical.grid(row=0, column=1, sticky='ns')
        else:
            self.vertical.grid_remove()
        if need_x:
            self.horizontal.grid(row=1, column=0, sticky='ew')
        else:
            self.horizontal.grid_remove()
        content_w, content_h = max(view_w, requested_w), max(view_h, requested_h)
        self.canvas.itemconfigure(self._item, width=content_w, height=content_h)
        self.canvas.configure(scrollregion=(0, 0, content_w, content_h))
        if not need_x:
            self.canvas.xview_moveto(0)
        if not need_y:
            self.canvas.yview_moveto(0)

    def _contains(self, widget):
        while isinstance(widget, tk.Misc):
            if widget is self.content or widget is self.canvas:
                return True
            widget = widget.master
        return False

    def _child_geometry(self, event):
        # Embedded frames can change their requested size without changing their
        # allocated size (e.g. restoring hidden sections in a Notebook).
        if self._contains(event.widget):
            self._schedule()

    def _wheel(self, event):
        if not self._contains(event.widget):
            return
        number = getattr(event, 'num', None)
        sequence = f'<Button-{number}>' if number in (4, 5) else '<MouseWheel>'
        widget_class = event.widget.winfo_class()
        # Text/combobox/etc. class handlers run before this toplevel binding and
        # may consume the wheel without returning "break". Do not scroll twice.
        if event.widget.bind_class(widget_class, sequence) or event.widget.bind_class(
                widget_class, sequence.replace('<', '<Shift-')):
            return
        delta = 1 if number == 4 else -1 if number == 5 else getattr(event, 'delta', 0) / 120
        horizontal = bool(getattr(event, 'state', 0) & 1)
        view = self.canvas.xview if horizontal else self.canvas.yview
        if delta and view() != (0.0, 1.0):
            scroll = self.canvas.xview_scroll if horizontal else self.canvas.yview_scroll
            scroll(round(-delta * 48), 'units')
            return 'break'

    def _focus(self, event):
        if self._contains(event.widget) and event.widget.winfo_class() not in ('Canvas', 'Text'):
            self.see(event.widget)

    def see(self, widget):
        """Reveal a focused control without moving focus or invoking its command."""
        if self._closed or not self._contains(widget):
            return
        self.update_idletasks()
        if self._pending is not None:
            self._top.after_cancel(self._pending)
        self._layout()
        self.update_idletasks()
        region = self.canvas.bbox(self._item)
        if not region:
            return
        for start, size, origin, span, total, offset, move in (
            (widget.winfo_rootx(), widget.winfo_width(), self.canvas.winfo_rootx(),
             self.canvas.winfo_width(), region[2], self.canvas.canvasx(0), self.canvas.xview_moveto),
            (widget.winfo_rooty(), widget.winfo_height(), self.canvas.winfo_rooty(),
             self.canvas.winfo_height(), region[3], self.canvas.canvasy(0), self.canvas.yview_moveto)):
            relative = start - origin
            if relative < 0 or size > span:
                move(max(0, offset + relative - 4) / total)
            elif relative + size > span:
                move(max(0, offset + relative + min(size, span) - span + 4) / total)

    def _dispose(self, event):
        if event.widget is not self:
            return
        self._closed = True
        if self._pending is not None:
            self._top.after_cancel(self._pending)
            self._pending = None
        for sequence, binding in self._bindings:
            self._top.unbind(sequence, binding)
