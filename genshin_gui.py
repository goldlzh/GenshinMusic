import os
import sys
import time
import random
import threading
import queue
import json
import uuid
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from song_memory import clamp_ratio, normalize_memory, preferred_memory, resolve_memory_start
from song_library import (SORT_MODES, normalize_sort_mode, song_key, song_entry,
                          sort_songs, register_songs, format_timestamp)

try:
    import pydirectinput
except ImportError:
    os.system("pip install pydirectinput")
    import pydirectinput

try:
    import mido
except ImportError:
    os.system("pip install mido")
    import mido

pydirectinput.PAUSE = 0
pydirectinput.FAILSAFE = False

from midi_engine import (
    NOTE_MAP, DRUM_CHANNEL, DURATION_MODES, KeyboardPlayer,
    analyze_midi, auto_select_channels, filter_channels, detect_melody,
    detect_glissando, remove_glissando, keep_melody_only, find_best_transpose,
    count_playable, count_chords, finalize_notes, performance_duration,
    build_schedule, build_unavailable_notes, PlaybackSnapshot,
)
from performance_view import PerformanceView, time_text
from live_midi import LiveMidiInput, LiveSettings, midi_backend, is_genshin_foreground


GM_INSTRUMENTS = [
    "大钢琴", "亮音钢琴", "电钢琴(EP)", "酒吧钢琴", "电钢琴1", "电钢琴2", "羽管键琴", "翼琴",
    "钢片琴", "钟琴", "音乐盒", "颤音琴", "马林巴", "木琴", "管钟", "扬琴",
    "风琴1", "打击风琴", "摇滚风琴", "教堂管风琴", "簧风琴", "手风琴", "口琴", "探戈手风琴",
    "尼龙吉他", "钢弦吉他", "爵士电吉他", "闷音电吉他", "过载吉他", "失真吉他", "吉他泛音", "原声贝斯",
    "指弹贝斯", "拨片贝斯", "无品贝斯", "掌击贝斯1", "掌击贝斯2", "合成贝斯1", "合成贝斯2", "小提琴",
    "中提琴", "大提琴", "低音提琴", "颤弓弦乐", "拨弦弦乐", "竖琴", "定音鼓", "弦乐合奏1",
    "弦乐合奏2", "合成弦乐1", "合成弦乐2", "人声合唱", "人声Oohs", "合成人声", "管弦乐击奏", "小号",
    "长号", "大号", "弱音小号", "圆号", "铜管组", "合成铜管1", "合成铜管2", "高音萨克斯",
    "中音萨克斯", "次中音萨克斯", "上低音萨克斯", "双簧管", "英国管", "巴松管", "单簧管", "短笛",
    "长笛", "竖笛", "排箫", "吹瓶声", "尺八", "哨笛", "陶笛", "方波主音",
    "锯齿波主音", "汽笛风琴主音", "合成主音", "合成人声主音", "五度主音", "低音+主音", "新时代铺底", "暖音铺底",
    "复音铺底", "唱诗铺底", "弓弦铺底", "金属铺底", "光环铺底", "扫弦铺底", "冰雨音效", "音轨音效",
    "水晶音效", "大气音效", "明亮音效", "鬼魅音效", "回声音效", "科幻音效", "西塔琴", "班卓琴",
    "三味线", "古筝", "卡林巴", "风笛", "提琴(民族)", "唢呐", "叮当铃", "阿哥哥",
    "钢鼓", "木鱼", "太鼓", "定音筒鼓", "合成鼓", "反钹", "吉他滑弦", "呼吸声",
    "海浪声", "鸟鸣", "电话铃", "直升机", "掌声", "枪声",
]


def gm_name(program):
    if 0 <= program < len(GM_INSTRUMENTS):
        return GM_INSTRUMENTS[program]
    return f"音色#{program}"


def build_report_text(info, tagged, offset, mode_desc, gliss_runs, gliss_notes,
                      enable_fold=False, suggested_offsets=None):
    suggested_offsets = suggested_offsets or {
        fold: find_best_transpose(tagged, fold) for fold in (False, True)}
    melody_count = sum(note.melody for note in tagged)
    lines = ["=" * 45, "       MIDI 曲目解析报告", "=" * 45,
             f"物理音轨总数     : {info['total_tracks']} 条",
             f"筛选后含音符音轨 : {len({note.track for note in tagged})} 条",
             f"旋律识别         : {mode_desc}", "", "乐器（按 MIDI 通道区分）:"]
    for channel, data in sorted(info['channel_info'].items()):
        names = "、".join(gm_name(program) for program in sorted(data['programs']))
        lines.append(f"  通道 {channel:2d}: {names}  （{data['notes']} 音）")
    if info['drum_notes']:
        lines.append(f"  鼓组通道 {DRUM_CHANNEL}: {info['drum_notes']} 音，已排除")
    lines.extend(["", f"筛选后音符总数   : {len(tagged)}（主旋律 {melody_count} / 伴奏 {len(tagged) - melody_count}）",
                  f"原谱长于1秒的音  : {sum(note.end - note.start > 1 for note in tagged)} 个",
                  f"扫音检测         : {gliss_runs} 段，共 {gliss_notes} 个音", "", "----- 分别计算转调 -----"])
    for fold in (False, True):
        suggested = suggested_offsets[fold]
        mel, acc = count_playable(tagged, suggested, fold)
        label = "开启折叠" if fold else "关闭折叠"
        lines.append(f"  {label}: 建议 {suggested:+d} 半音；可弹 {mel + acc}/{len(tagged)}（旋律 {mel} / 伴奏 {acc}）")
    lines.append(f"当前采用         : {'开启' if enable_fold else '关闭'}八度折叠，建议 {offset:+d} 半音")
    lines.append("同数优先保护旋律走向、减少同键冲突，再比较伴奏、折叠距离和移调幅度。")
    lines.append("音长/转调/速度等参数在下次起播时应用；标签按总时长比例定位。")
    lines.append("=" * 45)
    return "\n".join(lines)


def format_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class Player(KeyboardPlayer):
    def __init__(self, notes, speed, start_ratio, progress_cb, log_cb, finish_cb):
        super().__init__(notes, speed, start_ratio, progress_cb, log_cb, finish_cb,
                         keyboard=pydirectinput)


class GlobalHotkey:
    """基于 Windows RegisterHotKey 的全局快捷键，不依赖额外第三方库。"""

    MODIFIERS = {
        "ALT": 0x0001,
        "CTRL": 0x0002,
        "CONTROL": 0x0002,
        "SHIFT": 0x0004,
        "WIN": 0x0008,
        "WINDOWS": 0x0008,
    }
    SPECIAL_KEYS = {
        "SPACE": 0x20,
        "TAB": 0x09,
        "ENTER": 0x0D,
        "ESC": 0x1B,
        "ESCAPE": 0x1B,
        "INSERT": 0x2D,
        "DELETE": 0x2E,
        "HOME": 0x24,
        "END": 0x23,
        "PAGEUP": 0x21,
        "PAGEDOWN": 0x22,
    }
    # 常用标点使用 Windows 虚拟键码；名称形式可以避免“+”与组合键分隔符冲突。
    SYMBOL_KEYS = {
        "/": (0xBF, "/"),
        "SLASH": (0xBF, "/"),
        "-": (0xBD, "-"),
        "MINUS": (0xBD, "-"),
        "=": (0xBB, "="),
        "EQUAL": (0xBB, "="),
        "+": (0x6B, "Num+"),
        "PLUS": (0x6B, "Num+"),
        "*": (0x6A, "Num*"),
        "MULTIPLY": (0x6A, "Num*"),
        ",": (0xBC, ","),
        "COMMA": (0xBC, ","),
        ".": (0xBE, "."),
        "PERIOD": (0xBE, "."),
        ";": (0xBA, ";"),
        "SEMICOLON": (0xBA, ";"),
        "'": (0xDE, "'"),
        "QUOTE": (0xDE, "'"),
        "[": (0xDB, "["),
        "LBRACKET": (0xDB, "["),
        "]": (0xDD, "]"),
        "RBRACKET": (0xDD, "]"),
        "\\": (0xDC, "\\"),
        "BACKSLASH": (0xDC, "\\"),
        "`": (0xC0, "`"),
        "BACKTICK": (0xC0, "`"),
        "NUM/": (0x6F, "Num/"),
        "NUM*": (0x6A, "Num*"),
        "NUM-": (0x6D, "Num-"),
        "NUM+": (0x6B, "Num+"),
        "NUM.": (0x6E, "Num."),
    }
    HOTKEY_ID = 0x4A71
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_NOREPEAT = 0x4000

    def __init__(self, hotkey_text, callback):
        self.hotkey_text = hotkey_text
        self.callback = callback
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.registered = False
        self.error_code = 0
        self.modifiers = 0
        self.vk = 0
        self.normalized = ""

    @classmethod
    def parse(cls, text):
        raw = text.strip().replace("＋", "+")
        # “+”本身表示主键；组合键中的加号请写成 Plus 或 Num+。
        if raw == "+":
            parts = ["NUM+"]
        else:
            upper_raw = raw.upper()
            # 保护 Num+，避免被作为组合键分隔符拆开。
            protected = upper_raw.replace("NUM+", "NUMPLUS")
            parts = [p.strip() for p in protected.split("+") if p.strip()]
            parts = ["NUM+" if p == "NUMPLUS" else p for p in parts]
        if not parts:
            raise ValueError("快捷键不能为空")
        modifiers = 0
        modifier_names = []
        main_key = None
        vk = None
        for part in parts:
            if part in cls.MODIFIERS:
                modifiers |= cls.MODIFIERS[part]
                canonical = "Ctrl" if part in ("CTRL", "CONTROL") else (
                    "Alt" if part == "ALT" else ("Shift" if part == "SHIFT" else "Win")
                )
                if canonical not in modifier_names:
                    modifier_names.append(canonical)
                continue
            if main_key is not None:
                raise ValueError("快捷键只能包含一个主按键")
            if len(part) == 1 and ("A" <= part <= "Z" or "0" <= part <= "9"):
                vk = ord(part)
                main_key = part
            elif part.startswith("F") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
                vk = 0x70 + int(part[1:]) - 1
                main_key = part
            elif part in cls.SPECIAL_KEYS:
                vk = cls.SPECIAL_KEYS[part]
                main_key = "Esc" if part in ("ESC", "ESCAPE") else part.title()
            elif part in cls.SYMBOL_KEYS:
                vk, main_key = cls.SYMBOL_KEYS[part]
            else:
                raise ValueError(f"不支持的按键：{part}")
        if main_key is None:
            raise ValueError("请指定主按键，例如 F8、/、Num+ 或 Ctrl+Alt+L")
        normalized = "+".join(modifier_names + [main_key])
        return modifiers, vk, normalized

    def start(self):
        self.modifiers, self.vk, self.normalized = self.parse(self.hotkey_text)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=1.0)
        return self.registered

    def stop(self):
        if self.thread_id:
            ctypes.windll.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)

    def _run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.thread_id = kernel32.GetCurrentThreadId()
        flags = self.modifiers | self.MOD_NOREPEAT
        self.registered = bool(user32.RegisterHotKey(None, self.HOTKEY_ID, flags, self.vk))
        if not self.registered:
            self.error_code = ctypes.get_last_error()
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    self.callback()
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)
            self.registered = False


# ==================== GUI ====================

class App:
    HOTKEY_DEFAULTS = {
        "insert": "F8",
        "previous": "F6",
        "play": "F7",
        "stop": "F9",
        "next": "F10",
    }
    HOTKEY_TITLES = {
        "insert": "插入标签",
        "previous": "上一曲",
        "play": "播放/暂停",
        "stop": "停止",
        "next": "下一曲",
    }
    RESERVED_GAME_VKS = {ord(key) for key in NOTE_MAP.values()}

    def __init__(self, root):
        self.root = root
        root.title("原神 MIDI 自动演奏  by goldlzh  UID:179236334")
        root.geometry(f"820x{min(980, root.winfo_screenheight() - 80)}")

        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.abspath(__file__))
        self.music_dir = os.path.join(self.base_dir, "songs")
        self.label_store_path = os.path.join(self.base_dir, "song_labels.json")
        self.label_store = self._load_label_store()
        self.current_song_name = None
        self.current_song_key = None
        # 起播点与不断更新的播放进度分离，避免记忆被进度回调覆盖。
        self.start_label_id = None
        self.start_ratio = 0.0
        self.hotkey_managers = {}

        self.info = None
        self.tagged = None
        self.suggested_offset = 0
        self.suggested_offsets = {False: 0, True: 0}
        self.melody_description = ""
        self.channel_vars = {}
        self.filtered_nb = []
        self.track_idx_map = {}
        self.all_songs = []
        self.current_index = -1
        self.playlist_order = []
        self._library_metadata_dirty = False

        self.player = None
        self.session_active = False
        self.play_gen = 0
        self.countdown_abort = threading.Event()
        self.cached_notes = []
        self.user_dragging = False
        self.updating_scale = False
        self.visual_notes = ()
        self.visual_unavailable = ()
        self.visual_total = 0.0
        self.visual_snapshot = PlaybackSnapshot()
        self._visual_seek_serial = 0
        self._pending_start = None
        self.visual_title = ""
        self.visual_detail = ""
        self.visual_window = None
        self.floating_visual = None
        self.visual_mode_var = tk.StringVar(root, value="五线谱时值")
        self.visual_span_var = tk.StringVar(root, value="8 秒")
        self.visual_topmost_var = tk.BooleanVar(root, value=True)
        self._visual_after_id = None
        self._closing = False
        self.live_input = None
        self._live_generation = 0
        self.live_backend = None
        self._live_auto_suspended = False
        self._live_after_id = None
        self._next_live_scan = 0.0

        self.msg_queue = queue.Queue()

        self._build_ui()
        self._load_song_list()
        self.root.after(60, self._drain_queue)
        self.root.after(100, self._activate_all_hotkeys)
        self._visual_after_id = self.root.after(33, self._visual_tick)
        self._live_after_id = self.root.after(250, self._live_tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 界面 ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        self.hotkey_vars = {}
        self.root.columnconfigure(0, weight=1)
        # 通道较多或窗口变矮时，谱面让出空间，播放/停止按钮保持可见。
        self.root.rowconfigure(1, weight=1)

        top = ttk.LabelFrame(self.root, text="曲目")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=4)
        ttk.Label(top, text="搜索:").grid(row=0, column=0, **pad)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._filter_songs())
        ttk.Entry(top, textvariable=self.search_var, width=22).grid(row=0, column=1, **pad)
        self.song_combo = ttk.Combobox(top, state="readonly", width=42)
        self.song_combo.grid(row=0, column=2, **pad)
        self.song_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_song_times())
        ttk.Button(top, text="解析", command=self._on_analyze).grid(row=0, column=3, **pad)
        ttk.Button(top, text="刷新曲库", command=self._load_song_list).grid(row=0, column=4, **pad)
        sorting = ttk.Frame(top)
        sorting.grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(sorting, text="排序:").pack(side="left", padx=(0, 12))
        self.sort_var = tk.StringVar(value=self.label_store["settings"]["song_sort"])
        for value, title in SORT_MODES.items():
            ttk.Radiobutton(sorting, text=title, variable=self.sort_var, value=value,
                            command=self._on_sort_change).pack(side="left", padx=(0, 18))
        ttk.Label(sorting, text="中文按拼音 · 时间由近到远").pack(side="left")
        self.song_time_var = tk.StringVar(value="请选择曲目")
        ttk.Label(top, textvariable=self.song_time_var).grid(
            row=2, column=0, columnspan=5, sticky="w", **pad)

        self.main_tabs = ttk.Notebook(self.root, height=280)
        self.main_tabs.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.visual_view = PerformanceView(self.main_tabs, on_detach=self._open_visual_window,
                                           on_seek=self._on_visual_seek,
                                           view_mode=self.visual_mode_var, window_span=self.visual_span_var)
        self.main_tabs.add(self.visual_view, text="演奏可视化")
        rep = ttk.Frame(self.main_tabs)
        self.main_tabs.add(rep, text="解析报告 / 日志")
        self.report_text = tk.Text(rep, height=13, wrap="none", font=("Consolas", 9))
        self.report_text.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(rep, command=self.report_text.yview)
        sb.pack(side="right", fill="y")
        self.report_text.config(yscrollcommand=sb.set)
        self._build_live_panel()

        param = ttk.LabelFrame(self.root, text="演奏参数")
        param.grid(row=2, column=0, sticky="ew", padx=8, pady=4)

        ttk.Label(param, text="乐器通道:").grid(row=0, column=0, sticky="nw", **pad)
        self.channel_frame = ttk.Frame(param)
        self.channel_frame.grid(row=0, column=1, columnspan=3, sticky="w", **pad)

        ttk.Label(param, text="主旋律音轨:").grid(row=1, column=0, sticky="w", **pad)
        self.melody_combo = ttk.Combobox(param, state="readonly", width=28)
        self.melody_combo.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        self.melody_combo.bind("<<ComboboxSelected>>", lambda e: self._recompute())

        self.melody_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(param, text="只弹主旋律", variable=self.melody_only_var,
                        command=self._on_mapping_change).grid(row=2, column=0, sticky="w", **pad)
        self.gliss_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(param, text="屏蔽扫音", variable=self.gliss_var,
                        command=self._on_mapping_change).grid(row=2, column=1, sticky="w", **pad)
        self.fold_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(param, text="八度折叠", variable=self.fold_var,
                        command=self._on_mapping_change).grid(row=2, column=2, sticky="w", **pad)

        ttk.Label(param, text="转调(半音):").grid(row=3, column=0, sticky="w", **pad)
        self.transpose_var = tk.StringVar(value="0")
        trans_entry = ttk.Entry(param, textvariable=self.transpose_var, width=8)
        trans_entry.grid(row=3, column=1, sticky="w", **pad)
        trans_entry.bind("<Return>", lambda e: self._refresh_total())
        trans_entry.bind("<FocusOut>", lambda e: self._refresh_total())
        ttk.Button(param, text="用建议值", command=self._use_suggested).grid(row=3, column=2, sticky="w", **pad)

        ttk.Label(param, text="播放速度:").grid(row=4, column=0, sticky="w", **pad)
        self.speed_var = tk.DoubleVar(value=1.0)
        ttk.Scale(param, from_=0.5, to=2.0, variable=self.speed_var, orient="horizontal",
                  length=200, command=lambda v: self._on_speed_change()).grid(
            row=4, column=1, columnspan=2, sticky="w", **pad)
        self.speed_label = ttk.Label(param, text="1.00 倍")
        self.speed_label.grid(row=4, column=3, sticky="w", **pad)

        ttk.Label(param, text="音长模式:").grid(row=5, column=0, sticky="w", **pad)
        self.duration_mode_var = tk.StringVar(value="score")
        duration_choices = ttk.Frame(param)
        duration_choices.grid(row=5, column=1, columnspan=3, sticky="w", **pad)
        for value, title in DURATION_MODES.items():
            ttk.Radiobutton(duration_choices, text=title, value=value,
                            variable=self.duration_mode_var,
                            command=self._on_duration_change).pack(side="left", padx=(0, 12))
        self.sustain_var = tk.BooleanVar(value=False)
        self.sustain_check = ttk.Checkbutton(param, text="跟随 MIDI 延音踏板（长音模式）",
                                             variable=self.sustain_var, command=self._refresh_total)
        self.sustain_check.grid(row=6, column=1, columnspan=3, sticky="w", **pad)
        ttk.Label(param, text="音长、转调和速度等参数在下次起播时应用。",
                  foreground="#666666").grid(row=7, column=0, columnspan=4, sticky="w", **pad)

        # 播放模式
        mode = ttk.LabelFrame(self.root, text="播放模式")
        mode.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        self.mode_var = tk.StringVar(value="sequential")
        ttk.Radiobutton(mode, text="顺序播放", value="sequential", variable=self.mode_var).pack(side="left", padx=8)
        ttk.Radiobutton(mode, text="单曲循环", value="single", variable=self.mode_var).pack(side="left", padx=8)
        ttk.Radiobutton(mode, text="随机播放", value="random", variable=self.mode_var).pack(side="left", padx=8)

        # 标签工具栏；全局快捷键在游戏窗口前台时也能插入无名称标签
        tagbar = ttk.Frame(self.root)
        tagbar.grid(row=4, column=0, sticky="ew", padx=8, pady=(4, 0))
        ttk.Button(tagbar, text="插入标签", command=self._insert_label).pack(side="left", padx=4)
        ttk.Label(tagbar, text="插入快捷键:").pack(side="left", padx=(12, 4))
        default_hotkey = self.label_store.get("settings", {}).get(
            "insert_hotkey", self.HOTKEY_DEFAULTS["insert"])
        self.hotkey_var = tk.StringVar(value=default_hotkey)
        self.hotkey_vars["insert"] = self.hotkey_var
        hotkey_entry = ttk.Entry(tagbar, textvariable=self.hotkey_var, width=16)
        hotkey_entry.pack(side="left", padx=2)
        self._bind_hotkey_entry(hotkey_entry, "insert")
        ttk.Button(tagbar, text="应用",
                   command=lambda: self._apply_hotkey_from_ui("insert")).pack(side="left", padx=4)
        ttk.Label(tagbar, text="双击标签改名，右键可删除", foreground="#666666").pack(side="right", padx=4)

        # 进度条（可拖动跳转）及其可点击标签层
        prog = ttk.Frame(self.root)
        prog.grid(row=5, column=0, sticky="ew", padx=8, pady=2)
        marker_row = ttk.Frame(prog)
        marker_row.pack(fill="x")
        self.marker_canvas = tk.Canvas(marker_row, height=30, highlightthickness=0,
                                       background=self.root.cget("background"))
        self.marker_canvas.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(marker_row, text="", width=12).pack(side="right")
        self.marker_canvas.bind("<Configure>", lambda e: self._draw_labels())

        seek_row = ttk.Frame(prog)
        seek_row.pack(fill="x")
        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(seek_row, from_=0.0, to=1.0, variable=self.seek_var,
                                    orient="horizontal", command=self._on_scale_move)
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=4)
        self.seek_scale.bind("<Button-1>", self._on_scale_press)
        self.seek_scale.bind("<ButtonRelease-1>", self._on_scale_release)
        self.time_label = ttk.Label(seek_row, text="0:00/0:00", width=23)
        self.time_label.pack(side="right")

        memory_frame = ttk.LabelFrame(self.root, text="歌曲记忆")
        memory_frame.grid(row=6, column=0, sticky="ew", padx=8, pady=4)
        memory_actions = ttk.Frame(memory_frame)
        memory_actions.pack(fill="x", padx=4, pady=2)
        self.remember_btn = ttk.Button(memory_actions, text="手动记忆",
                                       command=self._remember_manually, state="disabled")
        self.remember_btn.pack(side="left", padx=3)
        self.restore_memory_btn = ttk.Button(memory_actions, text="恢复记忆",
                                             command=self._restore_memory, state="disabled")
        self.restore_memory_btn.pack(side="left", padx=3)
        self.clear_memory_btn = ttk.Button(memory_actions, text="清除手动记忆",
                                           command=self._clear_manual_memory, state="disabled")
        self.clear_memory_btn.pack(side="left", padx=3)
        ttk.Label(memory_actions, text="手动优先，自动保存不会覆盖", foreground="#666666").pack(
            side="left", padx=8)
        self.memory_status_var = tk.StringVar(value="请先解析歌曲。")
        ttk.Label(memory_frame, textvariable=self.memory_status_var).pack(
            anchor="w", padx=8, pady=(0, 3))

        # 播放控制
        ctrl = ttk.Frame(self.root)
        ctrl.grid(row=7, column=0, sticky="ew", padx=8, pady=6)
        for col in range(4):
            ctrl.columnconfigure(col, weight=1, uniform="player_controls")

        self.prev_btn = ttk.Button(ctrl, text="上一曲", command=self._on_prev)
        self.prev_btn.grid(row=0, column=0, sticky="ew", padx=4)
        self.play_btn = ttk.Button(ctrl, text="播放", command=self._on_play_pause)
        self.play_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self.stop_btn = ttk.Button(ctrl, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=2, sticky="ew", padx=4)
        self.next_btn = ttk.Button(ctrl, text="下一曲", command=self._on_next)
        self.next_btn.grid(row=0, column=3, sticky="ew", padx=4)

        settings = self.label_store.get("settings", {})
        for col, action in enumerate(("previous", "play", "stop", "next")):
            value = settings.get(f"{action}_hotkey", self.HOTKEY_DEFAULTS[action])
            var = tk.StringVar(value=value)
            self.hotkey_vars[action] = var
            entry = ttk.Entry(ctrl, textvariable=var, width=13, justify="center")
            entry.grid(row=1, column=col, sticky="ew", padx=4, pady=(3, 0))
            self._bind_hotkey_entry(entry, action)

        self.status_var = tk.StringVar(value="就绪。请选择曲目并解析。")
        ttk.Label(self.root, textvariable=self.status_var).grid(row=8, column=0, sticky="ew", padx=10, pady=2)

    # ---------- 歌曲标签 / 全局快捷键 ----------
    def _build_live_panel(self):
        tab = ttk.Frame(self.main_tabs, padding=12)
        self.main_tabs.add(tab, text="USB MIDI 实时演奏")
        saved = self.label_store['settings'].get('live_midi', {})
        saved = saved if isinstance(saved, dict) else {}
        self.live_device_var = tk.StringVar(value=str(saved.get('device', '')))
        self.live_transpose_var = tk.StringVar(value=str(saved.get('transpose', 0)))
        self.live_fold_var = tk.BooleanVar(value=saved.get('fold', True) is True)
        self.live_hold_var = tk.BooleanVar(value=saved.get('follow_release', True) is True)
        self.live_pedal_var = tk.BooleanVar(value=saved.get('sustain', True) is True)
        self.live_auto_var = tk.BooleanVar(value=saved.get('auto_connect', False) is True)
        self.live_merge_var = tk.BooleanVar(value=saved.get('merge', True) is True)
        self.live_merge_ms_var = tk.StringVar(value=str(saved.get('merge_window_ms', 30)))
        self.live_status_var = tk.StringVar(value='连接电子琴后刷新设备；正式输出仅在原神处于前台时启用。')
        self.live_keys_var = tk.StringVar(value='当前按键：无')
        devices = ttk.Frame(tab)
        devices.pack(fill='x', pady=4)
        ttk.Label(devices, text='MIDI 输入：').pack(side='left')
        self.live_combo = ttk.Combobox(devices, textvariable=self.live_device_var, state='readonly', width=38)
        self.live_combo.pack(side='left', padx=6)
        ttk.Button(devices, text='刷新设备', command=self._refresh_midi_ports).pack(side='left')
        options = ttk.Frame(tab)
        options.pack(fill='x', pady=8)
        ttk.Label(options, text='移调（−48～48 半音）：').pack(side='left')
        ttk.Spinbox(options, from_=-48, to=48, width=5, textvariable=self.live_transpose_var).pack(side='left')
        for title, var in [('八度折叠', self.live_fold_var), ('跟随琴键松开', self.live_hold_var),
                           ('跟随延音踏板', self.live_pedal_var)]:
            ttk.Checkbutton(options, text=title, variable=var).pack(side='left', padx=8)
        merge_row = ttk.Frame(tab)
        merge_row.pack(fill='x', pady=4)
        ttk.Checkbutton(merge_row, text='合并同时按下的音符', variable=self.live_merge_var).pack(side='left')
        ttk.Spinbox(merge_row, from_=0, to=200, width=5, textvariable=self.live_merge_ms_var).pack(side='left', padx=6)
        ttk.Label(merge_row, text='毫秒（0～200；仅合并按下，松开独立）').pack(side='left')
        ttk.Checkbutton(tab, text='即插即弹（原神在前台时自动连接所选设备）', variable=self.live_auto_var,
                        command=self._change_live_auto).pack(anchor='w', pady=4)
        buttons = ttk.Frame(tab)
        buttons.pack(fill='x', pady=8)
        ttk.Button(buttons, text='连接并开始（5 秒准备）', command=self._start_live).pack(side='left')
        ttk.Button(buttons, text='停止实时演奏', command=lambda: self._stop_live(True)).pack(side='left', padx=8)
        ttk.Label(tab, textvariable=self.live_status_var, wraplength=680).pack(anchor='w', pady=6)
        ttk.Label(tab, textvariable=self.live_keys_var).pack(anchor='w', pady=6)
        ttk.Label(tab, text='关闭“跟随琴键松开”时固定短按 40ms；参数在下次连接时应用。\n'
                  '断线、切离原神、暂停或停止会释放琴键；文件播放与实时演奏互斥。',
                  wraplength=680).pack(anchor='w', pady=6)

    def _live_settings(self):
        try:
            transpose = int(self.live_transpose_var.get())
            if not -48 <= transpose <= 48:
                raise ValueError()
        except ValueError:
            self.live_status_var.set('移调请输入 −48 到 48 的整数。')
            return None
        try:
            merge_ms = int(self.live_merge_ms_var.get())
            if not 0 <= merge_ms <= 200:
                raise ValueError()
        except ValueError:
            self.live_status_var.set('合并区间请输入 0 到 200 的整数（毫秒）。')
            return None
        settings = LiveSettings(transpose, self.live_fold_var.get(), self.live_hold_var.get(),
                                self.live_pedal_var.get(), self.live_merge_var.get(), merge_ms)
        self.label_store['settings']['live_midi'] = dict(
            device=self.live_device_var.get(), transpose=transpose, fold=settings.fold,
            follow_release=settings.follow_release, sustain=settings.sustain,
            auto_connect=self.live_auto_var.get(), merge=settings.merge, merge_window_ms=merge_ms)
        self._save_label_store()
        return settings

    def _refresh_midi_ports(self):
        try:
            if self.live_backend is None:
                self.live_backend = midi_backend()
            names = self.live_backend.get_input_names()
            self.live_combo['values'] = names
            if not self.live_device_var.get() and names:
                self.live_device_var.set(names[0])
            if not names:
                self.live_status_var.set('尚未发现 MIDI 输入设备，请连接支持 USB MIDI 的电子琴。')
            return names
        except Exception as exc:
            self.live_status_var.set(f'读取 MIDI 设备失败：{exc}')
            return []

    def _change_live_auto(self):
        self._live_auto_suspended = False
        self._live_settings()
        self._next_live_scan = 0

    def _start_live(self, delay=5.0):
        settings = self._live_settings()
        if settings is None:
            return
        if self.live_device_var.get() not in self._refresh_midi_ports():
            self.live_status_var.set('请连接并选择一个有效的 MIDI 输入设备。')
            return
        self._on_stop()
        self._live_auto_suspended = False
        self._live_generation += 1
        generation = self._live_generation
        live = LiveMidiInput(self.live_device_var.get(), settings, keyboard=pydirectinput,
                             backend=self.live_backend, delay=delay,
                             on_status=lambda value: self.msg_queue.put(('live_status', (generation, value))))
        self.live_input = live
        try:
            live.start()
            self.live_status_var.set(f'已连接，{int(delay)} 秒后接受输入；请切回原神乐器界面。')
            self.play_btn.config(text='暂停实时')
            self.stop_btn.config(state='normal')
        except Exception as exc:
            live.stop()
            self.live_input = None
            self.live_status_var.set(f'连接失败：{exc}')

    def _stop_live(self, disarm=False):
        if disarm:
            self._live_auto_suspended = True
        if self.live_input is not None:
            self._live_generation += 1
            self.live_input.stop()
            self.live_input = None
            self.live_status_var.set('实时演奏已停止；点击连接或重新勾选即插即弹可再次启用。')
            self.live_keys_var.set('当前按键：无')
            self.play_btn.config(text='播放')
            self.stop_btn.config(state='disabled')

    def _live_tick(self):
        if self._closing:
            return
        if self.live_input:
            if self.live_input.stopped.is_set():
                self.live_input.stop()
                self.live_input = None
                self.live_keys_var.set('当前按键：无')
                self.play_btn.config(text='播放')
                self.stop_btn.config(state='disabled')
            else:
                self.live_keys_var.set('当前按键：' + (' '.join(self.live_input.active_keys) or '无'))
        elif (self.live_auto_var.get() and not self._live_auto_suspended and not self.session_active
              and time.monotonic() >= self._next_live_scan):
            self._next_live_scan = time.monotonic() + 2
            if is_genshin_foreground() and self.live_device_var.get() in self._refresh_midi_ports():
                self._start_live(delay=0)
        self._live_after_id = self.root.after(100, self._live_tick)

    def _load_label_store(self):
        defaults = {f"{action}_hotkey": value
                    for action, value in self.HOTKEY_DEFAULTS.items()}
        defaults["song_sort"] = "name"
        empty = {"version": 1, "settings": defaults, "songs": {}}
        if not os.path.exists(self.label_store_path):
            return empty
        try:
            with open(self.label_store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return empty
            data.setdefault("version", 1)
            if not isinstance(data.get("settings"), dict):
                data["settings"] = {}
            for key, value in defaults.items():
                data["settings"].setdefault(key, value)
            data["settings"]["song_sort"] = normalize_sort_mode(data["settings"]["song_sort"])
            # 旧配置若使用了游戏21键或无法解析，则自动回退到安全默认值。
            for action, fallback in self.HOTKEY_DEFAULTS.items():
                key = f"{action}_hotkey"
                try:
                    _, vk, _ = GlobalHotkey.parse(str(data["settings"].get(key, fallback)))
                    if vk in self.RESERVED_GAME_VKS:
                        data["settings"][key] = fallback
                except ValueError:
                    data["settings"][key] = fallback
            if not isinstance(data.get("songs"), dict):
                data["songs"] = {}
            return data
        except (OSError, ValueError, TypeError):
            return empty

    def _save_label_store(self):
        temp_path = self.label_store_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.label_store, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.label_store_path)
            return True
        except OSError as e:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            self.set_status(f"曲库/标签/歌曲记忆保存失败：{e}")
            return False

    def _song_key(self, name):
        return song_key(name)

    def _current_song_entry(self, create=False):
        if not self.current_song_key:
            return {}
        songs = self.label_store.setdefault("songs", {})
        entry = songs.get(self.current_song_key)
        if not isinstance(entry, dict):
            entry = {}
            if create:
                songs[self.current_song_key] = entry
        if create:
            entry["file"] = self.current_song_name
            entry.setdefault("labels", [])
        return entry

    def _current_labels(self, create=False):
        return self._current_song_entry(create).get("labels", [])

    # ---------- 每首歌的演奏配置与起播点 ----------
    def _capture_memory(self, start_ratio=None):
        label_id = self.start_label_id
        label = self._find_label(label_id) if label_id else None
        if start_ratio is None:
            # 手动记忆保存所选起点，不使用实时滚动的播放进度。
            start_ratio = clamp_ratio(label["ratio"]) if label else self.start_ratio
        start_ratio = clamp_ratio(start_ratio)
        if not label or abs(clamp_ratio(label["ratio"]) - start_ratio) > 0.000001:
            label_id = None
        return normalize_memory({
            "channels": [ch for ch, var in self.channel_vars.items() if var.get()],
            "melody_track": self.track_idx_map.get(self.melody_combo.get()),
            "melody_only": self.melody_only_var.get(),
            "remove_glissando": self.gliss_var.get(),
            "octave_fold": self.fold_var.get(),
            "duration_mode": self.duration_mode_var.get(),
            "sustain_pedal": self.sustain_var.get(),
            "transpose": self.transpose_var.get(),
            "speed": self.speed_var.get(),
            "start": {"label_id": label_id, "ratio": start_ratio},
        })

    def _save_memory(self, source, memory):
        if not self.current_song_key or memory is None:
            return False
        entry = self._current_song_entry(create=True)
        previous = entry.get("playback_memory")
        memories = dict(previous) if isinstance(previous, dict) else {}
        memories[source] = memory
        entry["playback_memory"] = memories
        if not self._save_label_store():
            if previous is None:
                entry.pop("playback_memory", None)
            else:
                entry["playback_memory"] = previous
            return False
        self._refresh_memory_status()
        return True

    def _remember_manually(self):
        if not self.tagged or not self._compute_notes(silent=True):
            self.set_status("请先解析歌曲并选择有效的演奏配置，再手动记忆。")
            return
        if self._save_memory("manual", self._capture_memory()):
            self.set_status("已手动记忆当前配置和所选起播点；自动保存不会覆盖它。")

    def _restore_memory(self):
        if not self.info:
            self.set_status("请先解析歌曲。")
            return
        source, memory = preferred_memory(self._current_song_entry())
        if memory is None:
            self.set_status("这首歌还没有演奏记忆。")
            return
        if self.session_active:
            self._on_stop()
        self._apply_memory(memory)
        self.set_status(f"已恢复{'手动' if source == 'manual' else '自动'}记忆，点击播放即可从记忆起点演奏。")

    def _clear_manual_memory(self):
        entry = self._current_song_entry()
        previous = entry.get("playback_memory", {})
        if not isinstance(previous, dict) or "manual" not in previous:
            return
        entry["playback_memory"] = {key: value for key, value in previous.items() if key != "manual"}
        if not self._save_label_store():
            entry["playback_memory"] = previous
            return
        self._refresh_memory_status()
        self.set_status("已清除手动记忆；下次解析或点击“恢复记忆”将使用最近的自动记忆。")

    def _apply_memory(self, memory):
        # 没有记忆的歌曲恢复默认参数，避免串用上一首歌的配置。
        settings = memory or {}
        available = set(self.channel_vars)
        keep = set(settings.get("channels", [])) & available
        if not keep:
            keep = auto_select_channels(self.info["channel_info"])
        for ch, var in self.channel_vars.items():
            var.set(ch in keep)
        self.melody_combo.set("自动")
        self.melody_only_var.set(settings.get("melody_only", False))
        self.gliss_var.set(settings.get("remove_glissando", True))
        self.fold_var.set(settings.get("octave_fold", True))
        self.duration_mode_var.set(settings.get("duration_mode", "score"))
        self.sustain_var.set(settings.get("sustain_pedal", False))
        self.sustain_check.config(state="disabled" if self.duration_mode_var.get() == "fixed" else "normal")
        self.speed_var.set(settings.get("speed", 1.0))
        self._on_speed_change()
        self._recompute(melody_track=settings.get("melody_track"),
                        transpose=settings.get("transpose"))
        self.start_ratio, self.start_label_id = (
            resolve_memory_start(memory, self._current_labels()) if memory else (0.0, None))
        self.seek_var.set(self.start_ratio)
        self._set_visual_position(self.start_ratio)
        total = self._display_total()
        self.time_label.config(text=f"{time_text(self.start_ratio * total)}/{time_text(total)}")
        self._draw_labels()
        self._refresh_memory_status()

    def _refresh_memory_status(self):
        if not self.current_song_key:
            self.memory_status_var.set("请先解析歌曲。")
            for button in (self.remember_btn, self.restore_memory_btn, self.clear_memory_btn):
                button.config(state="disabled")
            return
        source, memory = preferred_memory(self._current_song_entry())
        state = {"manual": "手动记忆优先", "auto": "自动记忆", None: "尚未记忆"}[source]
        label = self._find_label(self.start_label_id) if self.start_label_id else None
        if label:
            name = str(label.get("name", "")).strip() or "无名称标签"
            name = name if len(name) <= 22 else name[:21] + "…"
            point = f"标签「{name}」({clamp_ratio(label['ratio']):.1%})"
        else:
            point = "曲首" if self.start_ratio == 0 else f"进度 {self.start_ratio:.1%}"
        self.memory_status_var.set(f"{state}  |  所选起播点：{point}")
        self.remember_btn.config(state="normal" if self.tagged else "disabled")
        self.restore_memory_btn.config(state="normal" if memory else "disabled")
        memories = self._current_song_entry().get("playback_memory", {})
        self.clear_memory_btn.config(
            state="normal" if isinstance(memories, dict) and "manual" in memories else "disabled")

    def _current_progress_ratio(self):
        if self.player and self.session_active and self.player.total > 0:
            return max(0.0, min(self.player.play_time / self.player.total, 1.0))
        return max(0.0, min(self.seek_var.get(), 1.0))

    def _insert_label(self, source="按钮"):
        if not self.current_song_key:
            self.set_status("请先选择并解析一首歌曲，再插入标签。")
            return
        ratio = self._current_progress_ratio()
        labels = self._current_labels(create=True)
        labels.append({
            "id": uuid.uuid4().hex,
            "ratio": round(ratio, 6),
            "name": "",
        })
        labels.sort(key=lambda item: float(item.get("ratio", 0.0)))
        self._save_label_store()
        self._draw_labels()
        total = self.player.total if (self.player and self.session_active) else self._display_total()
        self.set_status(f"已通过{source}在 {format_time(ratio * total)} 插入无名称标签。")

    def _find_label(self, label_id):
        for item in self._current_labels():
            if item.get("id") == label_id:
                return item
        return None

    def _draw_labels(self):
        if not hasattr(self, "marker_canvas"):
            return
        canvas = self.marker_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 2)
        left_pad = 9
        usable = max(width - left_pad * 2, 1)
        for item in self._current_labels():
            label_id = item.get("id")
            if not label_id:
                label_id = uuid.uuid4().hex
                item["id"] = label_id
            ratio = max(0.0, min(float(item.get("ratio", 0.0)), 1.0))
            x = left_pad + ratio * usable
            tag = f"marker_{label_id}"
            color = "#2471a3" if label_id == self.start_label_id else "#e67e22"
            canvas.create_line(x, 15, x, 29, fill=color, width=2,
                               tags=("song_marker", tag))
            canvas.create_polygon(x - 5, 15, x + 5, 15, x, 22,
                                  fill=color, outline=color,
                                  tags=("song_marker", tag))
            name = str(item.get("name", "")).strip()
            if name:
                shown = name if len(name) <= 12 else name[:11] + "…"
                anchor = "n"
                tx = x
                if x < 35:
                    anchor, tx = "nw", 1
                elif x > width - 35:
                    anchor, tx = "ne", width - 1
                canvas.create_text(tx, 0, text=shown, anchor=anchor,
                                   fill="#8a4500", font=("Microsoft YaHei UI", 8),
                                   tags=("song_marker", tag))
            canvas.tag_bind(tag, "<Button-1>",
                            lambda e, lid=label_id: self._jump_to_label(lid))
            canvas.tag_bind(tag, "<Double-Button-1>",
                            lambda e, lid=label_id: self._rename_label(lid))
            canvas.tag_bind(tag, "<Button-3>",
                            lambda e, lid=label_id: self._show_label_menu(e, lid))
        canvas.tag_bind("song_marker", "<Enter>", lambda e: canvas.config(cursor="hand2"))
        canvas.tag_bind("song_marker", "<Leave>", lambda e: canvas.config(cursor=""))

    def _jump_to_label(self, label_id):
        item = self._find_label(label_id)
        if not item:
            return
        ratio = max(0.0, min(float(item.get("ratio", 0.0)), 1.0))
        self._move_playhead(ratio, label_id=label_id)
        total = self.visual_total
        name = str(item.get("name", "")).strip() or "无名称标签"
        self._draw_labels()
        self._refresh_memory_status()
        self.set_status(f"已跳转到{name}：{format_time(ratio * total)}。")

    def _rename_label(self, label_id):
        item = self._find_label(label_id)
        if not item:
            return
        name = simpledialog.askstring("修改标签名", "标签名称：",
                                      initialvalue=item.get("name", ""), parent=self.root)
        if name is None:
            return
        item["name"] = name.strip()
        self._save_label_store()
        self._draw_labels()
        self._refresh_memory_status()

    def _delete_label(self, label_id):
        item = self._find_label(label_id)
        if not item:
            return
        ratio = clamp_ratio(item.get("ratio", 0))
        # 删除锚点时保留它最后的位置，手动和自动记忆都不会失效。
        memories = self._current_song_entry().get("playback_memory", {})
        if isinstance(memories, dict):
            for memory in memories.values():
                start = memory.get("start") if isinstance(memory, dict) else None
                if isinstance(start, dict) and start.get("label_id") == label_id:
                    start.update(label_id=None, ratio=round(ratio, 6))
        if self.start_label_id == label_id:
            self.start_label_id = None
            self.start_ratio = ratio
        labels = self._current_labels()
        before = len(labels)
        labels[:] = [item for item in labels if item.get("id") != label_id]
        if len(labels) != before:
            self._save_label_store()
            self._draw_labels()
            self._refresh_memory_status()
            self.set_status("标签已删除。")

    def _move_label_to_current(self, label_id):
        item = self._find_label(label_id)
        if not item:
            return
        item["ratio"] = round(self._current_progress_ratio(), 6)
        if self.start_label_id == label_id:
            self.start_ratio = item["ratio"]
        self._current_labels().sort(key=lambda label: float(label.get("ratio", 0.0)))
        self._save_label_store()
        self._draw_labels()
        self._refresh_memory_status()
        self.set_status("标签已移动到当前播放位置。")

    def _show_label_menu(self, event, label_id):
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="跳转到此处", command=lambda: self._jump_to_label(label_id))
        menu.add_command(label="修改标签名", command=lambda: self._rename_label(label_id))
        menu.add_command(label="移动到当前进度", command=lambda: self._move_label_to_current(label_id))
        menu.add_separator()
        menu.add_command(label="删除标签", command=lambda: self._delete_label(label_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _bind_hotkey_entry(self, entry, action):
        entry.bind("<Return>", lambda e, a=action: self._apply_hotkey_from_ui(a))
        entry.bind("<FocusOut>", lambda e, a=action: self._apply_hotkey_if_changed(a))

    def _apply_hotkey_if_changed(self, action):
        manager = self.hotkey_managers.get(action)
        text = self.hotkey_vars[action].get().strip()
        if manager is None or text.upper() != manager.normalized.upper():
            self._activate_hotkey(action, text, save=True)

    def _apply_hotkey_from_ui(self, action):
        manager = self.hotkey_managers.get(action)
        text = self.hotkey_vars[action].get().strip()
        if manager and text.upper() == manager.normalized.upper():
            return True
        return self._activate_hotkey(action, text, save=True)

    def _activate_all_hotkeys(self):
        for action in self.HOTKEY_DEFAULTS:
            if self._activate_hotkey(action, self.hotkey_vars[action].get(), save=False):
                manager = self.hotkey_managers[action]
                self.label_store.setdefault("settings", {})[
                    f"{action}_hotkey"] = manager.normalized
        self._save_label_store()

    def _hotkey_callback(self, action):
        self.msg_queue.put(("hotkey", action))

    def _activate_hotkey(self, action, text, save):
        title = self.HOTKEY_TITLES[action]
        old_manager = self.hotkey_managers.get(action)
        old_text = old_manager.normalized if old_manager else None
        fallback = old_text or self.HOTKEY_DEFAULTS[action]
        try:
            _, vk, _ = GlobalHotkey.parse(text)
        except ValueError as e:
            self.hotkey_vars[action].set(fallback)
            messagebox.showerror(f"{title}快捷键无效", str(e), parent=self.root)
            return False

        if vk in self.RESERVED_GAME_VKS:
            key_name = chr(vk)
            self.hotkey_vars[action].set(fallback)
            messagebox.showerror(
                f"{title}快捷键不可用",
                f"按键 {key_name} 属于原神乐器的21键映射，不能用于任何软件快捷键。",
                parent=self.root,
            )
            return False

        if old_manager:
            old_manager.stop()
            self.hotkey_managers.pop(action, None)

        manager = GlobalHotkey(text, lambda a=action: self._hotkey_callback(a))
        if not manager.start():
            if old_text:
                restored = GlobalHotkey(old_text, lambda a=action: self._hotkey_callback(a))
                if restored.start():
                    self.hotkey_managers[action] = restored
                    self.hotkey_vars[action].set(restored.normalized)
            else:
                self.hotkey_vars[action].set(self.HOTKEY_DEFAULTS[action])
            messagebox.showerror(
                f"{title}快捷键注册失败",
                "该快捷键可能已被其他功能或其他程序占用，请换一个组合。",
                parent=self.root,
            )
            return False

        self.hotkey_managers[action] = manager
        self.hotkey_vars[action].set(manager.normalized)
        if save:
            key = f"{action}_hotkey"
            self.label_store.setdefault("settings", {})[key] = manager.normalized
            self._save_label_store()
        self.set_status(f"{title}快捷键已设为 {manager.normalized}。")
        return True

    def _handle_hotkey_action(self, action):
        if action == "insert":
            self._insert_label(source="快捷键")
        elif action == "previous":
            self._on_prev()
        elif action == "play":
            self._on_play_pause()
        elif action == "stop":
            self._on_stop()
        elif action == "next":
            self._on_next()

    # ---------- 通用 ----------
    def _on_speed_change(self):
        self.speed_label.config(text=f"{self.speed_var.get():.2f} 倍")
        if self.tagged and not self.session_active:
            total = self._display_total()
            ratio = self.seek_var.get()
            self.time_label.config(text=f"{time_text(ratio * total)}/{time_text(total)}")
            self._preview_visual()

    def _refresh_total(self):
        """参数变化时只刷新总时长与时间标签，不重算 tagged、不重置转调。"""
        if not self.tagged or self.session_active:
            return
        self.cached_notes = self._compute_notes(silent=True)
        total = self._display_total()
        ratio = self.seek_var.get()
        self.time_label.config(text=f"{time_text(ratio * total)}/{time_text(total)}")
        self._preview_visual()

    # ---------- 当前演奏可视化（仅 Tk 主线程绘图） ----------
    def _set_visual_schedule(self, notes, snapshot=None, title=None, detail=None, *, unavailable=()):
        self.visual_notes = tuple(notes)
        self.visual_unavailable = tuple(unavailable)
        self.visual_total = max((note.end for note in self.visual_notes), default=0.0)
        self.visual_snapshot = snapshot or PlaybackSnapshot()
        self._visual_seek_serial = 0
        self.visual_title = (self.current_song_name or "") if title is None else title
        self.visual_detail = self.visual_detail if detail is None else detail
        for view in (self.visual_view, self.floating_visual):
            if view is not None:
                view.set_schedule(self.visual_notes, self.visual_title, self.visual_detail,
                                  unavailable=self.visual_unavailable)

    def _preview_visual(self):
        if self.session_active:
            return
        speed = round(max(self.speed_var.get(), .01), 2)
        notes = build_schedule(self.cached_notes, speed)
        unavailable = self._compute_unavailable(self.cached_notes, speed)
        total = max((note.end for note in notes), default=0.0)
        snapshot = PlaybackSnapshot(clamp_ratio(self.seek_var.get()) * total, 'preview')
        detail = f"{DURATION_MODES[self.duration_mode_var.get()]} · {speed:.2f}x"
        self._set_visual_schedule(notes, snapshot, detail=detail, unavailable=unavailable)

    def _set_visual_position(self, ratio):
        phase = 'countdown' if self.session_active and self.player is None and not self.user_dragging else 'preview'
        self.visual_snapshot = PlaybackSnapshot(clamp_ratio(ratio) * self.visual_total, phase,
                                                seek_serial=self._visual_seek_serial)
        for view in (self.visual_view, self.floating_visual):
            if view is not None:
                view.follow_position()
                view.update_state(self.visual_snapshot)

    def _visual_tick(self):
        if self._closing:
            return
        if self.player and self.session_active and not self.user_dragging:
            getter = getattr(self.player, 'get_visual_state', None)
            if getter:
                snapshot = getter()
                if snapshot.seek_serial >= self._visual_seek_serial:
                    self.visual_snapshot = snapshot
            else:
                # 测试/替代播放器可仅提供进度；不推测已发送的琴键。
                phase = 'paused' if self.player.pause_event.is_set() else 'playing'
                self.visual_snapshot = PlaybackSnapshot(self.player.play_time, phase)
        for view in (self.visual_view, self.floating_visual):
            if view is not None:
                view.update_state(self.visual_snapshot)
        self._visual_after_id = self.root.after(33, self._visual_tick)

    def _open_visual_window(self):
        if self.visual_window is not None and self.visual_window.winfo_exists():
            self.visual_window.deiconify()
            self.visual_window.lift()
            return
        window = tk.Toplevel(self.root)
        self.visual_window = window
        window.title("实时演奏可视化 · 原神 MIDI")
        window.geometry(f"{min(1040, window.winfo_screenwidth() - 80)}x{min(580, window.winfo_screenheight() - 80)}")
        window.minsize(680, 360)
        toolbar = ttk.Frame(window)
        toolbar.pack(fill='x', padx=8, pady=(5, 2))
        ttk.Checkbutton(toolbar, text="置顶显示", variable=self.visual_topmost_var,
                        command=lambda: window.attributes('-topmost', self.visual_topmost_var.get())).pack(side='left')
        ttk.Label(toolbar, text="显示软件发出的按键状态，便于观察长音与和弦。",
                  foreground='#666666').pack(side='left', padx=12)
        window.attributes('-topmost', self.visual_topmost_var.get())
        self.floating_visual = PerformanceView(window, view_mode=self.visual_mode_var,
                                               window_span=self.visual_span_var, on_seek=self._on_visual_seek)
        self.floating_visual.pack(fill='both', expand=True, padx=8, pady=(0, 6))
        self.floating_visual.set_schedule(self.visual_notes, self.visual_title, self.visual_detail,
                                          unavailable=self.visual_unavailable)
        window.protocol('WM_DELETE_WINDOW', self._close_visual_window)

    def _close_visual_window(self):
        window = self.visual_window
        self.visual_window = None
        self.floating_visual = None
        if window is not None:
            window.destroy()

    def log(self, msg):
        self.msg_queue.put(("log", msg))

    def set_status(self, msg):
        self.msg_queue.put(("status", msg))

    def set_progress(self, ratio, now, total, gen, seek_serial=0):
        self.msg_queue.put(("progress", (gen, seek_serial, ratio, now, total)))

    def _display_total(self):
        speed = round(max(self.speed_var.get(), 0.01), 2)
        return performance_duration(self.cached_notes, speed)

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self.report_text.insert("end", payload + "\n")
                    self.report_text.see("end")
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == 'live_status':
                    generation, message = payload
                    if generation == self._live_generation:
                        self.live_status_var.set(message)
                elif kind == "progress":
                    gen, seek_serial, ratio, now, total = payload
                    if gen != self.play_gen or not self.session_active or seek_serial < self._visual_seek_serial:
                        continue
                    if not self.user_dragging:
                        self.updating_scale = True
                        self.seek_var.set(ratio)
                        self.updating_scale = False
                        self.time_label.config(text=f"{time_text(now)}/{time_text(total)}")
                elif kind == "hotkey":
                    self._handle_hotkey_action(payload)
                elif kind == "finish":
                    self._handle_finish(payload)
        except queue.Empty:
            pass
        self.root.after(60, self._drain_queue)

    # ---------- 曲库 ----------
    def _load_song_list(self):
        try:
            with os.scandir(self.music_dir) as files:
                self.all_songs = [f.name for f in files if f.is_file()
                                  and f.name.lower().endswith(('.mid', '.midi'))]
        except FileNotFoundError:
            self.all_songs = []
            messagebox.showwarning("提示", f"未找到曲库目录:\n{self.music_dir}\n"
                                          f"请在程序目录下建立 songs 文件夹。")
            self._sort_song_list()
            return
        self._library_metadata_dirty |= register_songs(
            self.all_songs, self.music_dir, self.label_store)
        if self._library_metadata_dirty:
            self._library_metadata_dirty = not self._save_label_store()
        self._sort_song_list()

    def _sort_song_list(self):
        self.all_songs = sort_songs(self.all_songs, self.label_store, self.sort_var.get())
        self.current_index = (self.all_songs.index(self.current_song_name)
                              if self.current_song_name in self.all_songs else -1)
        self._filter_songs()

    def _on_sort_change(self):
        mode = normalize_sort_mode(self.sort_var.get())
        self.sort_var.set(mode)
        self.label_store["settings"]["song_sort"] = mode
        self._sort_song_list()
        # 用户主动换排序时更新后续播放顺序；起播时间造成的自动重排不改队列。
        self.playlist_order = list(self.all_songs)
        self._save_label_store()

    def _filter_songs(self):
        selected = self.song_combo.get()
        kw = self.search_var.get().strip().casefold()
        shown = [f for f in self.all_songs if kw in f.casefold()] if kw else self.all_songs
        self.song_combo["values"] = shown
        if selected in shown:
            self.song_combo.set(selected)
        elif self.current_song_name in shown:
            self.song_combo.set(self.current_song_name)
        elif shown:
            self.song_combo.current(0)
        else:
            self.song_combo.set("")
        self._refresh_song_times()

    def _select_in_combo(self, name):
        vals = list(self.song_combo["values"])
        if name in vals:
            self.song_combo.current(vals.index(name))
        else:
            self.song_combo.set(name)
        self._refresh_song_times()

    def _refresh_song_times(self):
        name = self.song_combo.get()
        if not name:
            self.song_time_var.set("没有匹配的曲目")
            return
        entry = song_entry(self.label_store, name)
        added = format_timestamp(entry.get("added_at"))
        played = format_timestamp(entry.get("last_played_at"))
        self.song_time_var.set(f"所选曲目 · 添加：{added}    最近播放：{played}")

    def _record_song_played(self, name):
        entry = self.label_store["songs"].setdefault(self._song_key(name),
                                                   {"file": name, "labels": []})
        previous = entry.get("last_played_at")
        entry["last_played_at"] = time.time()
        if not self._save_label_store():
            if previous is None:
                entry.pop("last_played_at", None)
            else:
                entry["last_played_at"] = previous
        self._sort_song_list()

    def _playback_order(self):
        # 已从曲库删除的歌曲不再排入队列；保留当前歌曲的位置以便跳到其后。
        order = [name for name in (self.playlist_order or self.all_songs)
                 if name in self.all_songs or name == self.current_song_name]
        return order or list(self.all_songs)

    # ---------- 解析 ----------
    def _on_analyze(self):
        name = self.song_combo.get()
        if not name:
            messagebox.showinfo("提示", "请先选择一首曲目。")
            return
        if name in self.all_songs:
            self.current_index = self.all_songs.index(name)
        self._analyze_song(name)

    def _analyze_song(self, name):
        # 切歌前结束旧会话，防止倒计时/旧进度覆盖新歌的起点与记忆。
        self._on_stop()
        self.info = None
        self.tagged = None
        self.current_song_name = None
        self.current_song_key = None
        self.current_index = -1
        self.cached_notes = []
        self._set_visual_schedule([], title="", detail="")
        self.start_label_id = None
        self.start_ratio = 0.0
        self._refresh_memory_status()
        path = os.path.join(self.music_dir, name)
        self.report_text.delete("1.0", "end")
        info = analyze_midi(path, log=self.log)
        if not info:
            self.set_status("解析失败。")
            self.tagged = None
            return False
        self.info = info
        self.current_song_name = name
        self.current_song_key = self._song_key(name)
        self.current_index = self.all_songs.index(name) if name in self.all_songs else -1
        self.root.after_idle(self._draw_labels)
        for w in self.channel_frame.winfo_children():
            w.destroy()
        self.channel_vars = {}
        auto_keep = auto_select_channels(info["channel_info"])
        for i, ch in enumerate(sorted(info["channel_info"])):
            var = tk.BooleanVar(value=(ch in auto_keep))
            notes = info["channel_info"][ch]["notes"]
            ttk.Checkbutton(self.channel_frame, text=f"通道{ch}({notes}音)",
                            variable=var,
                            command=self._recompute).grid(row=i // 4, column=i % 4, sticky="w", padx=4)
            self.channel_vars[ch] = var
        source, memory = preferred_memory(self._current_song_entry())
        self._apply_memory(memory)
        if memory:
            self.set_status(f"解析完成，已恢复{'手动' if source == 'manual' else '自动'}记忆及起播点。")
        return True

    def _recompute(self, melody_track=None, transpose=None):
        if not self.info:
            return
        keep = {ch for ch, v in self.channel_vars.items() if v.get()}
        if not keep:
            self.set_status("请至少勾选一个通道。")
            self.tagged = None
            self.cached_notes = []
            self._preview_visual()
            self._refresh_memory_status()
            return
        filtered_nb = filter_channels(self.info["note_bearing"], keep)
        if not filtered_nb:
            self.set_status("筛选后无音符。")
            self.tagged = None
            self.cached_notes = []
            self._preview_visual()
            self._refresh_memory_status()
            return
        self.filtered_nb = filtered_nb
        track_opts = ["自动"]
        self.track_idx_map = {"自动": None}
        for idx, tn in filtered_nb:
            label = f"音轨{idx} ({len(tn)}音)"
            track_opts.append(label)
            self.track_idx_map[label] = idx
        self.melody_combo["values"] = track_opts
        if melody_track is not None:
            self.melody_combo.set(next((name for name, idx in self.track_idx_map.items()
                                       if idx == melody_track), "自动"))
        if self.melody_combo.get() not in track_opts:
            self.melody_combo.current(0)
        forced_idx = self.track_idx_map.get(self.melody_combo.get())
        tagged, self.melody_description = detect_melody(filtered_nb, forced_idx)
        self.tagged = tagged
        self._update_suggestions()
        self.transpose_var.set(str(self.suggested_offset if transpose is None else transpose))
        self.cached_notes = self._compute_notes(silent=True)
        total = self._display_total()
        ratio = self.seek_var.get()
        self.time_label.config(text=f"{time_text(ratio * total)}/{time_text(total)}")
        self._preview_visual()
        self.set_status(f"解析完成，建议转调 {self.suggested_offset:+d}。调整参数后点播放。")
        self._refresh_memory_status()

    def _effective_tagged(self):
        tagged = list(self.tagged or [])
        if self.melody_only_var.get():
            tagged = keep_melody_only(tagged)
        if self.gliss_var.get():
            gliss_set, _ = detect_glissando(tagged)
            tagged = remove_glissando(tagged, gliss_set)
        return tagged

    def _update_suggestions(self):
        tagged = self._effective_tagged()
        self.suggested_offsets = {fold: find_best_transpose(tagged, fold) for fold in (False, True)}
        self.suggested_offset = self.suggested_offsets[self.fold_var.get()]
        gliss_set, gliss_runs = detect_glissando(self.tagged or [])
        report = build_report_text(self.info, tagged, self.suggested_offset,
                                   self.melody_description, gliss_runs, len(gliss_set),
                                   self.fold_var.get(), self.suggested_offsets)
        self.report_text.delete("1.0", "end")
        self.report_text.insert("end", report + "\n")

    def _on_mapping_change(self):
        if not self.tagged:
            return
        self._update_suggestions()
        self.transpose_var.set(str(self.suggested_offset))
        self._refresh_total()
        self.set_status(f"已按{'开启' if self.fold_var.get() else '关闭'}八度折叠重新计算，"
                        f"转调 {self.suggested_offset:+d}；下次起播时应用。")

    def _on_duration_change(self):
        self.sustain_check.config(state="disabled" if self.duration_mode_var.get() == "fixed" else "normal")
        self._refresh_total()

    def _use_suggested(self):
        self.transpose_var.set(str(self.suggested_offset))
        self._refresh_total()

    def _compute_notes(self, silent=False):
        if not self.tagged:
            return []
        log = (lambda *a: None) if silent else self.log
        tagged = self._effective_tagged()
        try:
            offset = int(self.transpose_var.get())
        except ValueError:
            offset = 0
        return finalize_notes(tagged, offset, enable_fold=self.fold_var.get(), log=log,
                              duration_mode=self.duration_mode_var.get(), sustain_pedal=self.sustain_var.get())

    def _compute_unavailable(self, playable_notes, speed):
        try:
            offset = int(self.transpose_var.get())
        except ValueError:
            offset = 0
        return build_unavailable_notes(self._effective_tagged(), offset, self.fold_var.get(),
                                       playable_notes, speed, self.duration_mode_var.get(), self.sustain_var.get())

    # ---------- 进度条拖动 ----------
    def _on_scale_press(self, event):
        self.user_dragging = True
        if self.player:
            self.player.suppress_progress = True

    def _on_scale_move(self, val):
        if self.updating_scale:
            return
        ratio = float(val)
        total = self.visual_total if self.session_active else self._display_total()
        self.time_label.config(text=f"{time_text(ratio * total)}/{time_text(total)}")
        self._set_visual_position(ratio)

    def _on_scale_release(self, event):
        ratio = self.seek_var.get()
        self._move_playhead(ratio)
        if self.player and self.session_active:
            self.player.suppress_progress = False
        self.user_dragging = False
        self._set_visual_position(ratio)
        self._draw_labels()
        self._refresh_memory_status()

    def _move_playhead(self, ratio, *, label_id=None):
        ratio = clamp_ratio(ratio)
        self.start_ratio, self.start_label_id = ratio, label_id
        self.updating_scale = True
        self.seek_var.set(ratio)
        self.updating_scale = False
        if self.player and self.session_active:
            serial = self.player.seek(ratio * self.player.total)
            self._visual_seek_serial = serial if isinstance(serial, int) else 0
        elif self._pending_start is not None:
            self._pending_start.update(ratio=ratio, label_id=label_id)
        self.time_label.config(text=f'{time_text(ratio * self.visual_total)}/{time_text(self.visual_total)}')
        self._set_visual_position(ratio)
        self._draw_labels()
        self._refresh_memory_status()

    def _on_visual_seek(self, seconds):
        if not self.visual_notes or self.visual_total <= 0:
            return
        ratio = clamp_ratio(seconds / self.visual_total)
        if self.session_active:
            if self.player:
                # 定位前先暂停，防止跳转后立即发音；由用户手动继续。
                self.player.pause_event.set()
                self.play_btn.config(text='继续')
            else:
                # 取消待起播的倒计时，旧回调也不能自动启动演奏。
                self._on_stop()
        self._move_playhead(ratio)
        action = '继续' if self.player and self.session_active else '播放'
        self.set_status(f'已定位至 {time_text(ratio * self.visual_total)}，点击“{action}”开始演奏。')

    # ---------- 播放控制 ----------
    def _on_play_pause(self):
        if self.live_input:
            if self.live_input.paused.is_set():
                self.live_input.paused.clear()
                self.play_btn.config(text='暂停实时')
            else:
                self.live_input.paused.set()
                self.play_btn.config(text='继续实时')
            return
        if self.session_active:
            if self.player is None:
                return  # 倒计时中
            if self.player.pause_event.is_set():
                self.player.pause_event.clear()
                self.play_btn.config(text="暂停")
                self.set_status("继续播放。")
            else:
                self.player.pause_event.set()
                self.play_btn.config(text="继续")
                self.set_status("已暂停。")
        else:
            if not self.tagged:
                messagebox.showinfo("提示", "请先选择并解析曲目。")
                return
            self._start_playback(start_ratio=self.seek_var.get(), countdown=5)

    def _start_playback(self, start_ratio, countdown):
        self._stop_live(True)
        if self.session_active or self.player:
            self._on_stop()
        cn = self._compute_notes(silent=False)
        if not cn:
            self.cached_notes = []
            self._preview_visual()
            self.set_status("没有可演奏的音符。")
            return
        speed = round(self.speed_var.get(), 2)
        # 快照必须来自生成 cn 时的配置；倒计时中修改界面不会串入自动记忆。
        memory = self._capture_memory(start_ratio)
        start_ratio = clamp_ratio(start_ratio)
        self.start_ratio = start_ratio
        self.start_label_id = memory["start"]["label_id"] if memory else None
        pending_start = {'ratio': start_ratio, 'label_id': self.start_label_id}
        self._pending_start = pending_start
        self._refresh_memory_status()
        self._draw_labels()
        self.session_active = True
        if not self.playlist_order:
            self.playlist_order = list(self.all_songs)
        self.countdown_abort.clear()
        self.play_gen += 1
        gen = self.play_gen
        compiled = build_schedule(cn, speed)
        unavailable = self._compute_unavailable(cn, speed)
        visual_position = start_ratio * max((note.end for note in compiled), default=0.0)
        visual_detail = f"{DURATION_MODES[self.duration_mode_var.get()]} · {speed:.2f}x"
        visual_title = self.current_song_name or ""
        self._set_visual_schedule(compiled, PlaybackSnapshot(visual_position, 'countdown'),
                                  visual_title, visual_detail, unavailable=unavailable)
        self.main_tabs.select(self.visual_view)
        self.play_btn.config(text="暂停")
        self.stop_btn.config(state="normal")
        self.log(f"\n速度 {speed:.2f} 倍。请在 {countdown} 秒内切回原神乐器界面...")

        def tick(remaining):
            if self.countdown_abort.is_set() or gen != self.play_gen:
                return
            if remaining > 0:
                self.set_status(f"倒计时 {remaining} 秒...切回游戏！")
                self.root.after(1000, lambda: tick(remaining - 1))
                return
            # 只在真正起播时保存，取消倒计时不会覆盖上次记忆。
            # 保存在 Tk 主线程，避免后台线程读取界面或竞争写 JSON。
            launch_ratio = pending_start['ratio']
            launch_position = launch_ratio * max((note.end for note in compiled), default=0.0)
            if memory:
                memory['start'] = dict(pending_start)
            self._pending_start = None
            player = Player(cn, speed, launch_ratio,
                            lambda ratio, now, total: self.set_progress(ratio, now, total, gen,
                                                                       getattr(player, 'seek_serial', 0)), self.log,
                            lambda fin: self.msg_queue.put(("finish", (fin, gen))))
            self.player = player
            self._set_visual_schedule(getattr(player, 'notes', compiled),
                                      PlaybackSnapshot(launch_position, 'ready'), visual_title, visual_detail,
                                      unavailable=unavailable)
            try:
                player.start()
            except Exception as e:
                self._on_stop()
                self.set_status(f"播放启动失败：{e}")
                return
            self._save_memory("auto", memory)
            self._record_song_played(visual_title)

        tick(countdown)

    def _on_stop(self):
        self._stop_live(True)
        self.countdown_abort.set()
        self.play_gen += 1
        self._pending_start = None
        self._visual_seek_serial = 0
        self.user_dragging = False
        if self.player:
            self.player.stop()
            position = self.player.play_time
        else:
            position = self.visual_snapshot.position
        self.visual_snapshot = PlaybackSnapshot(position, 'stopped')
        self.session_active = False
        self.playlist_order = []
        self.player = None
        self.play_btn.config(text="播放")
        self.stop_btn.config(state="disabled")
        self.set_status("已停止。")

    def _random_song_index(self):
        """随机选择歌曲；曲库不止一首时避免连续选中当前歌曲。"""
        count = len(self.all_songs)
        if count <= 1:
            return 0
        if not 0 <= self.current_index < count:
            return random.randrange(count)
        idx = random.randrange(count - 1)
        return idx + 1 if idx >= self.current_index else idx

    def _handle_finish(self, payload):
        finished, gen = payload
        if gen != self.play_gen:
            return  # 旧的播放实例，忽略
        position = self.visual_total if finished else self.visual_snapshot.position
        self.visual_snapshot = PlaybackSnapshot(position, 'finished' if finished else 'stopped')
        self.session_active = False
        self.player = None
        self.play_btn.config(text="播放")
        self.stop_btn.config(state="disabled")
        if not finished:
            self.playlist_order = []
            return  # 被停止
        mode = self.mode_var.get()
        if mode == "single":
            self._play_song(self.current_song_name, countdown=3)
        elif mode == "sequential":
            order = self._playback_order()
            nxt = order.index(self.current_song_name) + 1 if self.current_song_name in order else 0
            if nxt < len(order):
                self._play_song(order[nxt], countdown=3, order=order)
            else:
                self.playlist_order = []
                self.set_status("顺序播放结束。")
        elif mode == "random":
            if self.all_songs:
                nxt = self._random_song_index()
                self._play_index(nxt, countdown=3)

    def _play_index(self, idx, countdown):
        if not self.all_songs:
            return
        idx = max(0, min(idx, len(self.all_songs) - 1))
        self._play_song(self.all_songs[idx], countdown)

    def _play_song(self, name, countdown, order=None):
        if name not in self.all_songs:
            self.set_status("曲目已不在曲库中，请刷新曲库。")
            return
        order = list(order if order is not None else self._playback_order())
        self.search_var.set("")  # 清搜索，保证 combo 里能选中
        self._select_in_combo(name)
        if self._analyze_song(name):
            self.playlist_order = order
            self._start_playback(start_ratio=self.seek_var.get(), countdown=countdown)

    def _on_prev(self):
        if not self.all_songs:
            return
        order = self._playback_order()
        idx = order.index(self.current_song_name) - 1 if self.current_song_name in order else -1
        if idx < 0:
            idx = len(order) - 1
        name = order[idx]
        self._on_stop()
        gen = self.play_gen
        self.root.after(300, lambda: self._play_song(name, countdown=3,
                                                    order=self.playlist_order or order)
                        if gen == self.play_gen else None)

    def _on_next(self):
        if not self.all_songs:
            return
        order = self._playback_order()
        if self.mode_var.get() == "random":
            name = self.all_songs[self._random_song_index()]
        else:
            idx = order.index(self.current_song_name) + 1 if self.current_song_name in order else 0
            if idx >= len(order):
                idx = 0
            name = order[idx]
        self._on_stop()
        gen = self.play_gen
        self.root.after(300, lambda: self._play_song(name, countdown=3,
                                                    order=self.playlist_order or order)
                        if gen == self.play_gen else None)

    def _on_close(self):
        self._closing = True
        if self._live_after_id is not None:
            self.root.after_cancel(self._live_after_id)
        if self._visual_after_id is not None:
            self.root.after_cancel(self._visual_after_id)
        self._on_stop()
        for manager in list(self.hotkey_managers.values()):
            manager.stop()
        self.hotkey_managers.clear()
        self.root.after(200, self.root.destroy)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
