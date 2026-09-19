"""USB MIDI 输入 → 游戏按键。设备回调只排队，单一工作线程拥有键盘。"""

import collections
import ctypes
from dataclasses import dataclass
import os
import queue
import threading
import time

from midi_engine import KeyboardPlayer, NOTE_MAP, map_note, DRUM_CHANNEL


@dataclass(frozen=True)
class LiveSettings:
    transpose: int = 0
    fold: bool = True
    follow_release: bool = True
    sustain: bool = True
    merge: bool = True
    merge_window_ms: int = 30

    def __post_init__(self):
        if not 0 <= self.merge_window_ms <= 200:
            raise ValueError('合并区间必须在 0～200ms 之间')


def is_genshin_foreground():
    if os.name != 'nt':
        return False
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        name = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            return False
        return os.path.basename(name.value).casefold() in {'yuanshen.exe', 'genshinimpact.exe'}
    finally:
        kernel32.CloseHandle(handle)


class LiveNoteState:
    """不接触设备或 GUI 的 MIDI 状态机，返回 (down/up, key) 动作。"""

    def __init__(self, settings=LiveSettings()):
        self.settings = settings
        self.voices = collections.defaultdict(collections.deque)
        self.pedal = set()
        self.pressed = {}
        self.pending_down = {}
        self.last_release = {}
        self.merge_deadline = None

    def feed(self, message, now, *, flush=True):
        channel = getattr(message, 'channel', None)
        if channel == DRUM_CHANNEL:
            return []
        if message.type in ('reset', 'stop'):
            return self.release_all(now)
        source = (channel, getattr(message, 'note', None))
        actions = []
        if message.type == 'note_on' and message.velocity > 0:
            pitch = map_note(message.note + self.settings.transpose, self.settings.fold)
            if pitch is None:
                return []
            key = NOTE_MAP[pitch]
            # 踏板保留的旧同音由新起音接替，仍按独立起音重新触发。
            window = self.settings.merge_window_ms / 1000 if self.settings.merge else 0
            if self.merge_deadline is None or now > self.merge_deadline + 1e-9:
                self.merge_deadline = now + window
            ready = max(now, self.merge_deadline)
            self.voices[source] = collections.deque(v for v in self.voices[source]
                                                   if v['held'] or not v['triggered'])
            self.voices[source].append({'key': key, 'held': True, 'start': ready,
                                       'ready': ready, 'triggered': False,
                                       'until': float('inf') if self.settings.follow_release else
                                           max(ready, self.last_release.get(key, -1) + .01) + .04})
        elif message.type == 'note_off' or (message.type == 'note_on' and message.velocity == 0):
            voice = next((v for v in self.voices.get(source, ()) if v['held']), None)
            if voice:
                voice['held'] = False
                if self.settings.follow_release and not (self.settings.sustain and channel in self.pedal):
                    voice['until'] = max(now, voice['start'] + .04,
                                         self.pending_down.get(voice['key'], -1) + .04,
                                         self.pressed.get(voice['key'], -1) + .04)
        elif message.type == 'control_change':
            if message.control == 64:
                if message.value >= 64:
                    self.pedal.add(channel)
                else:
                    self.pedal.discard(channel)
                    for (ch, _), voices in self.voices.items():
                        if ch == channel:
                            for voice in voices:
                                if not voice['held']:
                                    voice['until'] = min(voice['until'], max(now, voice['start'] + .04))
            elif message.control in (120, 123):
                for src in list(self.voices):
                    if src[0] == channel:
                        self.voices.pop(src)
                self.pedal.discard(channel)
            elif message.control == 121:
                self.pedal.discard(channel)
                for (ch, _), voices in self.voices.items():
                    if ch == channel:
                        for voice in voices:
                            if not voice['held']:
                                voice['until'] = now
        return actions + (self.tick(now) if flush else [])

    def tick(self, now):
        desired = set()
        starting = set()
        for source in list(self.voices):
            self.voices[source] = collections.deque(v for v in self.voices[source]
                                                   if not v['triggered'] or v['until'] > now)
            if not self.voices[source]:
                self.voices.pop(source)
            else:
                for voice in self.voices[source]:
                    if voice['ready'] > now + 1e-9:
                        continue
                    desired.add(voice['key'])
                    if not voice['triggered']:
                        starting.add(voice['key'])
                        voice['triggered'] = True
                        voice['until'] = max(voice['until'], now + .04)
        actions = []
        # 仅在合并窗口到期时重新起音；收集和弦期间绝不提前松开原来的长音。
        for key in starting & set(self.pressed):
            if now - self.pressed[key] > .015:
                actions.append(('up', key))
                self.pressed.pop(key)
                self.last_release[key] = now
                self.pending_down[key] = now + .01
        for key in set(self.pressed) - desired:
            actions.append(('up', key))
            self.pressed.pop(key)
            self.last_release[key] = now
        for key in set(self.pending_down) - desired:
            self.pending_down.pop(key)
        for key in sorted(desired - set(self.pressed)):
            when = self.pending_down.setdefault(key, self.last_release.get(key, -1) + .01)
            if now >= when:
                actions.append(('down', key))
                self.pressed[key] = now
                self.pending_down.pop(key, None)
                for voices in self.voices.values():
                    for voice in voices:
                        if voice['key'] == key and voice['triggered']:
                            voice['until'] = max(voice['until'], now + .04)
        return actions

    def release_all(self, now=0):
        actions = [('up', key) for key in self.pressed]
        self.last_release.update({key: now for key in self.pressed})
        self.pressed.clear()
        self.voices.clear()
        self.pedal.clear()
        self.pending_down.clear()
        self.merge_deadline = None
        return actions


def midi_backend():
    from usb_midi import WindowsMidiBackend
    return WindowsMidiBackend()


class LiveMidiInput:
    def __init__(self, name, settings, *, keyboard, on_status=lambda _: None,
                 foreground=is_genshin_foreground, backend=None, delay=5.0):
        self.name, self.settings = name, settings
        self.keyboard, self.on_status, self.foreground = keyboard, on_status, foreground
        self.backend = backend or midi_backend()
        self.delay = delay
        self.events = queue.Queue(maxsize=4096)
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.state = LiveNoteState(settings)
        self.thread = None
        self.port = None
        self.active_keys = ()

    def _receive(self, message):
        if not self.stopped.is_set():
            try:
                self.events.put_nowait((time.monotonic(), message.copy()))
            except queue.Full:
                self.stopped.set()
                self.on_status('MIDI 输入过密，已停止并释放琴键。')

    def start(self):
        self.port = self.backend.open_input(self.name, callback=self._receive)
        self.thread = threading.Thread(target=self._run, name='USB-MIDI', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=.6)
        if self.port:
            self.port.close()
            self.port = None

    def _output(self, actions):
        for action, key in actions:
            if action == 'down' and self.stopped.is_set():
                continue
            (self.keyboard.keyDown if action == 'down' else self.keyboard.keyUp)(key.lower())
        self.active_keys = tuple(sorted(self.state.pressed))

    def _run(self):
        ready = time.monotonic() + self.delay
        next_device_check = 0.0
        was_allowed = False
        accept_after = ready
        with KeyboardPlayer._keyboard_lock:
            try:
                while not self.stopped.is_set():
                    now = time.monotonic()
                    if now >= next_device_check:
                        if self.name not in self.backend.get_input_names() or getattr(self.port, 'error', None):
                            self.on_status('电子琴已断开，已释放所有琴键。')
                            break
                        next_device_check = now + 1.0
                    allowed = now >= ready and not self.paused.is_set() and self.foreground()
                    if allowed != was_allowed:
                        self._output(self.state.release_all(now))
                        accept_after = now
                        self.on_status('实时演奏已就绪。' if allowed else '等待切回原神，琴键已释放。')
                        was_allowed = allowed
                    try:
                        received, message = self.events.get(timeout=.004)
                        if allowed and received >= accept_after:
                            self._output(self.state.feed(message, received, flush=False))
                        # 合并按硬件消息的到达时刻分组，不受工作线程唤醒抖动影响。
                        while True:
                            received, message = self.events.get_nowait()
                            if allowed and received >= accept_after:
                                self._output(self.state.feed(message, received, flush=False))
                    except queue.Empty:
                        pass
                    if allowed:
                        self._output(self.state.tick(time.monotonic()))
            except Exception as exc:
                self.on_status(f'实时 MIDI 已停止：{exc}')
            finally:
                # 对全部游戏键发松键，异常路径也不会留下未释放的按键。
                for key in NOTE_MAP.values():
                    try:
                        self.keyboard.keyUp(key.lower())
                    except Exception:
                        pass
                self.state.release_all()
                self.active_keys = ()
                self.stopped.set()
                if self.port:
                    self.port.close()
