"""Windows WinMM MIDI 输入，无需额外驱动库或编译扩展。"""

import ctypes
from ctypes import wintypes
import threading


def decode_short_message(packed):
    import mido
    status = packed & 0xFF
    if not 0x80 <= status < 0xF0:
        return None
    length = 2 if (status & 0xF0) in (0xC0, 0xD0) else 3
    return mido.Message.from_bytes([(packed >> (8 * i)) & 0xFF for i in range(length)])


class MidiInCaps(ctypes.Structure):
    _fields_ = [('manufacturer', wintypes.WORD), ('product', wintypes.WORD),
                ('version', wintypes.DWORD), ('name', wintypes.WCHAR * 32),
                ('support', wintypes.DWORD)]


class WindowsMidiBackend:
    def __init__(self):
        self.api = ctypes.WinDLL('winmm')
        self.api.midiInGetNumDevs.restype = wintypes.UINT
        self.api.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(MidiInCaps), wintypes.UINT]
        self.api.midiInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT,
                                       ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        self.api.midiInGetErrorTextW.argtypes = [wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
        for name in ('midiInStart', 'midiInStop', 'midiInReset', 'midiInClose'):
            getattr(self.api, name).argtypes = [wintypes.HANDLE]

    def _devices(self):
        result = {}
        for index in range(self.api.midiInGetNumDevs()):
            caps = MidiInCaps()
            if self.api.midiInGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
                result[f'{caps.name} [{index}]'] = index
        return result

    def get_input_names(self):
        return list(self._devices())

    def check(self, code):
        if code:
            text = ctypes.create_unicode_buffer(256)
            self.api.midiInGetErrorTextW(code, text, len(text))
            raise OSError(f'MIDI {code}: {text.value}')

    def open_input(self, name, callback):
        devices = self._devices()
        if name not in devices:
            raise OSError('所选 MIDI 设备已断开')
        return WindowsMidiPort(self, devices[name], callback)


class WindowsMidiPort:
    def __init__(self, backend, index, callback):
        self.backend = backend
        self.handle = wintypes.HANDLE()
        self.lock = threading.Lock()
        self.closed = False
        self.error = None
        callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.UINT,
                                          ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)

        def receive(handle, event, instance, packed, stamp):
            if self.closed:
                return
            if event == 0x3C5:  # MIM_ERROR
                self.error = '设备报告 MIDI 输入错误'
                return
            if event != 0x3C3:  # MIM_DATA（本功能不接收 SysEx）
                return
            try:
                message = decode_short_message(packed)
                if message is not None:
                    callback(message)
            except Exception as exc:
                self.error = str(exc)

        self._callback = callback_type(receive)  # 保持引用，直到 midiInClose 完成。
        backend.check(backend.api.midiInOpen(ctypes.byref(self.handle), index,
                                            ctypes.cast(self._callback, ctypes.c_void_p).value, 0, 0x30000))
        try:
            backend.check(backend.api.midiInStart(self.handle))
        except Exception:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.backend.api.midiInStop(self.handle)
            self.backend.api.midiInReset(self.handle)
            self.backend.api.midiInClose(self.handle)
