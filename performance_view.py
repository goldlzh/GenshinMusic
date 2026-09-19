"""演奏时值谱与 21 键状态。绘图只在 Tk 主线程执行。"""

import bisect
import collections
import math
import time
import tkinter as tk
from tkinter import ttk

from midi_engine import NOTE_MAP, PlaybackSnapshot, TIME_EPSILON


KEYS = tuple(NOTE_MAP.values())
KEY_INDEX = {key: i for i, key in enumerate(KEYS)}
KEY_PITCH = {key: pitch for pitch, key in NOTE_MAP.items()}
NOTE_NAMES = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
COLORS = {
    'background': '#111c2b', 'grid': '#2b3e54', 'staff': '#56677d',
    'melody': '#72b6ff', 'accompaniment': '#b19bf3', 'past': '#506078',
    'active': '#5ce5af', 'recent': '#f7c873', 'text': '#dce6f4',
    'muted': '#8da0b8', 'key': '#233449', 'cursor': '#f7c873',
    'unavailable': '#ff737d', 'unavailable_past': '#a8525e', 'outside_grid': '#563440',
}
PHASE_NAMES = {'preview': '谱面预览', 'ready': '等待起播', 'countdown': '倒计时',
               'playing': '演奏中', 'paused': '已暂停', 'stopped': '已停止', 'finished': '演奏完成'}


def pitch_name(key):
    pitch = KEY_PITCH[key] if isinstance(key, str) else key
    return f'{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}'


def pitch_degree(pitch, chromatic=False):
    degree = (pitch // 12 - 4) * 7 + (0, 0, 1, 1, 2, 3, 3, 4, 4, 5, 5, 6)[pitch % 12]
    return degree + (.5 if chromatic and pitch % 12 in (1, 3, 6, 8, 10) else 0)


def time_text(seconds):
    if seconds < 0:
        return '-' + time_text(-seconds)
    minutes, ms = divmod(round(seconds * 1000), 60000)
    return f'{minutes}:{ms // 1000:02d}.{ms % 1000:03d}'


class ScheduleIndex:
    """按键/音高二分定位可见音符，前缀最晚结束时间兼容重叠的不可奏音。"""

    def __init__(self, notes=()):
        self.notes = tuple(notes)
        self.total = max((note.end for note in self.notes), default=0.0)
        self.start = min((note.start for note in self.notes), default=0.0)
        groups = collections.defaultdict(list)
        for index, note in enumerate(self.notes):
            groups[getattr(note, 'key', None) or note.pitch].append((index, note))
        self.groups = []
        for group in groups.values():
            group.sort(key=lambda item: item[1].start)
            ends, latest = [], -math.inf
            for _, note in group:
                latest = max(latest, note.end)
                ends.append(latest)
            self.groups.append((group, [note.start for _, note in group], ends))

    def visible(self, left, right):
        result = []
        for group, starts, ends in self.groups:
            first = bisect.bisect_right(ends, left)
            last = bisect.bisect_left(starts, right)
            result.extend((index, note) for index, note in group[first:last] if note.end > left)
        return result


def note_color(index, note, snapshot):
    if index in snapshot.active_note_ids:
        return COLORS['active']
    if note.end <= snapshot.position or note.start < snapshot.position - TIME_EPSILON:
        return COLORS['past']
    return COLORS['melody' if note.melody else 'accompaniment']


class PerformanceView(ttk.Frame):
    def __init__(self, parent, *, on_detach=None, on_seek=None, view_mode=None, window_span=None):
        super().__init__(parent)
        self.index = ScheduleIndex()
        self.unavailable_index = ScheduleIndex()
        self.low_degree, self.high_degree = 0, 20
        self.snapshot = PlaybackSnapshot()
        self.song_name = ''
        self.detail = ''
        self.on_seek = on_seek
        self._drawn_timeline = None
        self.view_mode = view_mode or tk.StringVar(self, value='五线谱时值')
        self.window_span = window_span or tk.StringVar(self, value='8 秒')
        self._revision = 0
        self._last_signature = None
        self._items = {}
        self._note_styles = {}
        self._key_items = {}
        self._key_styles = {}
        self._key_size = None
        self._unavailable_items = {}
        self._hover_text = ''
        self._status_text = ''
        self._manual_left = None
        self.follow_var = tk.BooleanVar(self, value=True)

        toolbar = ttk.Frame(self)
        toolbar.pack(fill='x', pady=(2, 3))
        ttk.Label(toolbar, text='谱面:').pack(side='left', padx=(4, 3))
        view_combo = ttk.Combobox(toolbar, textvariable=self.view_mode, state='readonly',
                                  values=('五线谱时值', '长条谱'), width=12)
        view_combo.pack(side='left')
        ttk.Label(toolbar, text='时间窗:').pack(side='left', padx=(12, 3))
        span_combo = ttk.Combobox(toolbar, textvariable=self.window_span, state='readonly',
                                  values=('4 秒', '8 秒', '12 秒'), width=6)
        span_combo.pack(side='left')
        ttk.Checkbutton(toolbar, text='跟随演奏', variable=self.follow_var,
                        command=self._invalidate).pack(side='left', padx=8)
        if on_detach:
            ttk.Button(toolbar, text='独立窗口 / 置顶', command=on_detach).pack(side='right', padx=4)
        view_combo.bind('<<ComboboxSelected>>', self._invalidate)
        span_combo.bind('<<ComboboxSelected>>', self._invalidate)

        self.canvas = tk.Canvas(self, height=140, background=COLORS['background'],
                                highlightthickness=0)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', self._on_wheel)
        self.canvas.bind('<Button-5>', self._on_wheel)
        self.canvas.bind('<Button-1>', self._on_score_click)
        self.canvas.tag_bind('unavailable', '<Enter>', self._show_unavailable_hint)
        self.canvas.tag_bind('unavailable', '<Leave>', self._hide_unavailable_hint)
        self.time_scrollbar = ttk.Scrollbar(self, orient='horizontal', command=self._scroll_time)
        self.keyboard_canvas = tk.Canvas(self, height=66, background=COLORS['background'],
                                         highlightthickness=0)
        legend = ttk.Label(self, text='绿：按下  蓝/紫：旋律/伴奏  灰：已释放/跳过  红：无法演奏  金边：刚触发；线长表示音长',
                            font=('Microsoft YaHei UI', 8))
        hint = ttk.Label(self, text='滚轮：前后浏览 · Shift+滚轮：微调 · 左键：仅调整进度，手动播放',
                         font=('Microsoft YaHei UI', 8))
        self.status_var = tk.StringVar(value='解析歌曲后显示谱面；按键状态来自播放器。')
        self.status_label = ttk.Label(self, textvariable=self.status_var, font=('Microsoft YaHei UI', 8))
        # 先为键盘与说明保留空间，缩小时只压缩谱面，避免底部状态被挤掉。
        self.status_label.pack(side='bottom', fill='x', padx=3, pady=(0, 2))
        hint.pack(side='bottom', anchor='w', padx=3)
        legend.pack(side='bottom', anchor='w', padx=3)
        self.keyboard_canvas.pack(side='bottom', fill='x')
        self.time_scrollbar.pack(side='bottom', fill='x')
        self.canvas.pack(fill='both', expand=True)
        self.bind('<Configure>', self._invalidate)

    def _invalidate(self, event=None):
        self._last_signature = None

    def set_schedule(self, notes, song_name='', detail='', *, unavailable=()):
        self.index = ScheduleIndex(notes)
        self.unavailable_index = ScheduleIndex(unavailable)
        degrees = [pitch_degree(note.pitch, chromatic=True) for note in self.unavailable_index.notes]
        self.low_degree = min(0, math.floor(min(degrees, default=0)))
        self.high_degree = max(20, math.ceil(max(degrees, default=20)))
        self.song_name, self.detail = song_name, detail
        self.snapshot = PlaybackSnapshot()
        self.canvas.delete('note')
        self._items.clear()
        self._note_styles.clear()
        self.canvas.delete('unavailable')
        self._unavailable_items.clear()
        self._hover_text = ''
        self._manual_left = None
        self._drawn_timeline = None
        self.follow_var.set(True)
        self._revision += 1
        self._invalidate()

    def _time_bounds(self, span):
        start = min(0.0, self.unavailable_index.start)
        end = max(self.index.total, self.unavailable_index.total, 0.0)
        return start - span * .23, end + span * .77

    def _scroll_time(self, action, value, unit=None):
        span = float(self.window_span.get().split()[0])
        low, high = self._time_bounds(span)
        left = self._manual_left if self._manual_left is not None else self.snapshot.position - span * .23
        if action == 'moveto':
            left = low + float(value) * (high - low)
        elif action == 'scroll':
            left += float(value) * span * (.9 if unit == 'pages' else .1)
        self._manual_left = max(low, min(left, high - span))
        self.follow_var.set(False)
        self._invalidate()
        self.update_state(self.snapshot, force=True)

    def _on_wheel(self, event):
        number = getattr(event, 'num', None)
        steps = -1 if number == 4 else 1 if number == 5 else -getattr(event, 'delta', 0) / 120.0
        if getattr(event, 'state', 0) & 0x0001:
            steps *= .1
        if steps:
            self._scroll_time('scroll', steps, 'units')
        return 'break'

    def follow_position(self):
        """用户主动定位时，让谱面跟着定位光标移动。"""
        self.follow_var.set(True)
        self._manual_left = None
        self._invalidate()

    def _on_score_click(self, event):
        if not self.on_seek or not self.index.notes or self._drawn_timeline is None:
            return 'break'
        margin, right, left_time, pixels_per_second = self._drawn_timeline
        if not margin <= event.x <= right:
            return 'break'
        position = left_time + (event.x - margin) / pixels_per_second
        # 命中音头时对齐它的起音；点击延长线仍按点击处时间定位，不倒回旧音头。
        current = self.canvas.find_withtag('current')
        if current:
            tag = next((tag for tag in self.canvas.gettags(current[0]) if tag.startswith('note:')), None)
            if tag:
                note = self.index.notes[int(tag.split(':')[1])]
                head_x = margin + (note.start - left_time) * pixels_per_second
                if abs(event.x - head_x) <= 6:
                    position = note.start
        self.on_seek(max(0.0, min(position, self.index.total)))
        return 'break'

    def _show_unavailable_hint(self, event):
        current = self.canvas.find_withtag('current')
        if not current:
            return
        tag = next((tag for tag in self.canvas.gettags(current[0]) if tag.startswith('unavailable:')), None)
        if tag:
            note = self.unavailable_index.notes[int(tag.split(':')[1])]
            self._hover_text = f'{pitch_name(note.pitch)} · {note.reason} · {time_text(note.start)} ～ {time_text(note.end)} · 仅显示，不发送按键'
            self.status_var.set(self._hover_text)

    def _hide_unavailable_hint(self, event):
        self._hover_text = ''
        self.status_var.set(self._status_text)

    def update_state(self, snapshot, *, wall_time=None, force=False):
        self.snapshot = snapshot
        if not force and not self.winfo_ismapped():
            return
        wall_time = time.perf_counter() if wall_time is None else wall_time
        recent = frozenset(index for index, when in snapshot.recent_triggers if 0 <= wall_time - when <= .18)
        width, height = max(self.canvas.winfo_width(), 200), max(self.canvas.winfo_height(), 70)
        signature = (self._revision, round(snapshot.position, 3), snapshot.phase,
                     snapshot.active_note_ids, recent, width, height,
                     self.keyboard_canvas.winfo_width(), self.view_mode.get(), self.window_span.get())
        signature += (self.follow_var.get(), self._manual_left)
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self.status_label.configure(wraplength=max(100, width - 6))
        span = float(self.window_span.get().split()[0])
        margin, right = 58.0, width - 8.0
        left_time = snapshot.position - span * .23
        low, high = self._time_bounds(span)
        if not self.follow_var.get():
            if self._manual_left is None:
                self._manual_left = left_time
            left_time = max(low, min(self._manual_left, high - span))
        else:
            self._manual_left = None
        self.time_scrollbar.set((left_time - low) / (high - low), (left_time + span - low) / (high - low))
        pixels_per_second = (right - margin) / span
        self._drawn_timeline = (margin, right, left_time, pixels_per_second)

        def x_at(seconds):
            return margin + (seconds - left_time) * pixels_per_second

        top, bottom = (38.0 if self.unavailable_index.notes else 23.0), height - 6.0
        row_height = (bottom - top) / (self.high_degree - self.low_degree + 1)
        staff_low, staff_high = self.low_degree - 4, self.high_degree + 2
        step = (bottom - top) / (staff_high - staff_low)
        staff = self.view_mode.get() == '五线谱时值'

        def y_at(degree):
            return (bottom - (degree - staff_low) * step if staff else
                    bottom - (degree - self.low_degree + .5) * row_height)

        self._draw_grid(width, height, margin, right, left_time, span, x_at, y_at, staff, row_height)
        visible = self.index.visible(left_time, left_time + span)
        visible_ids = {index for index, _ in visible}
        for index in list(self._items):
            if index not in visible_ids:
                for item in self._items.pop(index):
                    self.canvas.delete(item)
                self._note_styles.pop(index, None)
        for index, note in visible:
            if index not in self._items:
                tags = ('note', f'note:{index}')
                self._items[index] = (
                    self.canvas.create_rectangle(0, 0, 0, 0, tags=tags),
                    self.canvas.create_oval(0, 0, 0, 0, tags=tags),
                    self.canvas.create_line(0, 0, 0, 0, tags=tags),
                    self.canvas.create_line(0, 0, 0, 0, tags=tags),
                    self.canvas.create_text(0, 0, text=note.key, tags=tags,
                                            fill=COLORS['background'], font=('Consolas', 8, 'bold')),
                )
            bar, head, stem, ledger, label = self._items[index]
            degree = KEY_INDEX[note.key]
            x1, x2, y = max(margin, x_at(note.start)), min(right, x_at(note.end)), y_at(degree)
            x2 = max(x1 + 1.5, x2)
            color = note_color(index, note, snapshot)
            active = index in snapshot.active_note_ids
            outline = COLORS['recent'] if index in recent and not active else color
            half = max(1.3, min(6.0, row_height * .35))
            head_x = x_at(note.start)
            head_visible = staff and margin <= head_x <= right - 4
            ledger_degree = 7 if degree == 7 else 19 if degree >= 19 else None
            label_visible = not staff and row_height >= 12 and x2 - x1 >= 20
            style = (color, outline, staff, head_visible, label_visible)
            if self._note_styles.get(index) != style:
                self.canvas.itemconfigure(bar, fill=color, outline=outline)
                self.canvas.itemconfigure(head, fill=color, outline=outline,
                                          state='normal' if head_visible else 'hidden')
                self.canvas.itemconfigure(stem, fill=color, state='normal' if head_visible else 'hidden')
                self.canvas.itemconfigure(ledger, fill=COLORS['staff'],
                                          state='normal' if head_visible and ledger_degree is not None else 'hidden')
                self.canvas.itemconfigure(label, state='normal' if label_visible else 'hidden')
                self._note_styles[index] = style
            if staff:
                self.canvas.coords(bar, x1, y - 1, x2, y + 1)
                if head_visible:
                    radius = max(2, min(4.3, step * .7))
                    self.canvas.coords(head, head_x - radius * 1.4, y - radius,
                                       head_x + radius * 1.4, y + radius)
                    stem_x = head_x + radius * 1.2
                    self.canvas.coords(stem, stem_x, y, stem_x, max(top, y - step * 3))
                if ledger_degree is not None and head_visible:
                    self.canvas.coords(ledger, head_x - 8, y_at(ledger_degree), head_x + 8, y_at(ledger_degree))
                    self.canvas.tag_lower(ledger, head)
            else:
                self.canvas.coords(bar, x1, y - half, x2, y + half)
                if label_visible:
                    self.canvas.coords(label, (x1 + x2) / 2, y)
        self._draw_unavailable(left_time, span, margin, right, x_at, y_at, staff, row_height, snapshot)
        self.canvas.delete('cursor')
        cursor_x = x_at(snapshot.position)
        if margin <= cursor_x <= right:
            self.canvas.create_line(cursor_x, top - 4, cursor_x, bottom, fill=COLORS['cursor'], width=2, tags='cursor')
            self.canvas.create_polygon(cursor_x - 4, top - 5, cursor_x + 4, top - 5, cursor_x, top,
                                       fill=COLORS['cursor'], outline='', tags='cursor')
        if not self.index.notes and not self.unavailable_index.notes:
            self.canvas.create_text((margin + right) / 2, height / 2,
                                     text='选择歌曲并解析后，音符将在这里显示', fill=COLORS['muted'],
                                     font=('Microsoft YaHei UI', 10), tags='cursor')
        self._draw_keyboard(snapshot, recent)
        held = [self.index.notes[index].key for index in snapshot.active_note_ids if 0 <= index < len(self.index.notes)]
        key_text = ' '.join(held[:9]) + (' …' if len(held) > 9 else '')
        state_text = f'按下 {len(held)} 键：{key_text}' if held else '当前未按键'
        title = self.song_name if len(self.song_name) <= 24 else self.song_name[:23] + '…'
        missing = f' · 无法演奏 {len(self.unavailable_index.notes)} 音' if self.unavailable_index.notes else ''
        self._status_text = (f'{PHASE_NAMES.get(snapshot.phase, "预览")}  {time_text(snapshot.position)} / '
                             f'{time_text(self.index.total)}  ·  {state_text}{missing}  ·  {title} {self.detail}')
        self.status_var.set(self._hover_text or self._status_text)

    def _draw_unavailable(self, left, span, margin, right, x_at, y_at, staff, row_height, snapshot):
        visible = self.unavailable_index.visible(left, left + span)
        visible_ids = {index for index, _ in visible}
        for index in list(self._unavailable_items):
            if index not in visible_ids:
                for item in self._unavailable_items.pop(index):
                    self.canvas.delete(item)
        for index, note in visible:
            if index not in self._unavailable_items:
                tags = ('unavailable', f'unavailable:{index}')
                self._unavailable_items[index] = (
                    self.canvas.create_rectangle(0, 0, 0, 0, tags=tags),
                    self.canvas.create_oval(0, 0, 0, 0, tags=tags, width=2),
                    self.canvas.create_text(0, 0, text=pitch_name(note.pitch), anchor='sw',
                                            tags=tags, font=('Consolas', 8, 'bold')),
                )
            bar, head, label = self._unavailable_items[index]
            color = COLORS['unavailable_past'] if note.end <= snapshot.position else COLORS['unavailable']
            x1, x2 = max(margin, x_at(note.start)), min(right, x_at(note.end))
            x2 = max(x1 + 2, x2)
            y = y_at(pitch_degree(note.pitch, chromatic=not staff))
            half = 2.5 if staff else max(1.3, min(5, row_height * .3))
            self.canvas.coords(bar, x1, y - half, x2, y + half)
            # 五线谱中升音与本位音共用线位，空心虚线保留下面可奏长音的颜色。
            self.canvas.itemconfigure(bar, fill='' if staff else color, outline=color,
                                      dash=(3, 2) if staff else ())
            head_x = x_at(note.start)
            head_visible = staff and margin <= head_x <= right - 4
            self.canvas.coords(head, head_x - 4, y - 2.5, head_x + 4, y + 2.5)
            self.canvas.itemconfigure(head, fill='', outline=color,
                                      state='normal' if head_visible else 'hidden')
            self.canvas.coords(label, x1 + 4, y - 3)
            self.canvas.itemconfigure(label, fill=color)

    def _draw_grid(self, width, height, margin, right, left_time, span, x_at, y_at, staff, row_height):
        canvas = self.canvas
        canvas.delete('grid')
        canvas.create_rectangle(margin, y_at(20.5), right, y_at(-.5), fill='#162638', outline='', tags='grid')
        if staff:
            for degree in (-3, -1, 1, 3, 5, 9, 11, 13, 15, 17):
                canvas.create_line(margin, y_at(degree), right, y_at(degree),
                                   fill=COLORS['staff'], tags='grid')
            for text, degree in (('高音 G', 13), ('低音 F', 1)):
                canvas.create_text(margin - 7, y_at(degree), text=text, anchor='e',
                                   fill=COLORS['muted'], font=('Microsoft YaHei UI', 8), tags='grid')
            canvas.create_text(margin - 7, y_at(7), text='C4', anchor='e',
                               fill=COLORS['muted'], font=('Consolas', 8), tags='grid')
        else:
            for degree in range(self.low_degree, self.high_degree + 1):
                octave, note_index = divmod(degree, 7)
                pitch = 48 + octave * 12 + (0, 2, 4, 5, 7, 9, 11)[note_index]
                key = NOTE_MAP.get(pitch)
                y = y_at(degree)
                canvas.create_line(margin, y + row_height / 2, right, y + row_height / 2,
                                   fill=(COLORS['outside_grid'] if key is None else
                                         COLORS['staff'] if degree % 7 == 0 else COLORS['grid']), tags='grid')
                if row_height >= 11 or degree % 7 == 0:
                    canvas.create_text(margin - 5, y, text=f'{key} {pitch_name(key)}' if key else pitch_name(pitch), anchor='e',
                                       fill=COLORS['muted'] if key else COLORS['unavailable'], font=('Consolas', 8), tags='grid')
        tick_step = 2 if span > 8 else 1
        tick = math.ceil(left_time / tick_step) * tick_step
        while tick < left_time + span:
            x = x_at(tick)
            canvas.create_line(x, 23, x, height - 6, fill=COLORS['grid'], dash=(2, 4), tags='grid')
            canvas.create_text(x, 10, text=time_text(tick).rsplit('.', 1)[0],
                               fill=COLORS['muted'], font=('Consolas', 8), tags='grid')
            tick += tick_step
        canvas.tag_lower('grid')

    def _draw_keyboard(self, snapshot, recent):
        canvas = self.keyboard_canvas
        width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 66)
        active_keys = {self.index.notes[index].key for index in snapshot.active_note_ids
                       if 0 <= index < len(self.index.notes)}
        recent_keys = {self.index.notes[index].key for index in recent if 0 <= index < len(self.index.notes)}
        if self._key_size != (width, height):
            canvas.delete('all')
            self._key_items.clear()
            self._key_styles.clear()
            self._key_size = width, height
            cell_w, cell_h = (width - 45) / 7, height / 3
            for row, (label, keys) in enumerate((('高', KEYS[14:]), ('中', KEYS[7:14]), ('低', KEYS[:7]))):
                y1, y2 = row * cell_h + 2, (row + 1) * cell_h - 2
                canvas.create_text(17, (y1 + y2) / 2, text=label, fill=COLORS['muted'],
                                   font=('Microsoft YaHei UI', 8))
                for column, key in enumerate(keys):
                    x1, x2 = 36 + column * cell_w, 36 + (column + 1) * cell_w - 5
                    rect = canvas.create_rectangle(x1, y1, x2, y2)
                    text = canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2,
                                               text=f'{key} · {pitch_name(key)}', font=('Consolas', 9, 'bold'))
                    self._key_items[key] = rect, text
        for key, (rect, text) in self._key_items.items():
            pressed = key in active_keys
            style = (pressed, key in recent_keys)
            if self._key_styles.get(key) == style:
                continue
            fill = COLORS['active'] if pressed else COLORS['key']
            outline = COLORS['recent'] if key in recent_keys and not pressed else fill
            canvas.itemconfigure(rect, fill=fill, outline=outline, width=2 if key in recent_keys else 1)
            canvas.itemconfigure(text, fill=COLORS['background'] if pressed else COLORS['text'])
            self._key_styles[key] = style
