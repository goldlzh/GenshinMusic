import time
import unittest
import threading
import mido
from live_midi import LiveSettings, LiveNoteState, LiveMidiInput
from usb_midi import decode_short_message


def on(note=60, channel=0, velocity=100):
    return mido.Message('note_on', note=note, channel=channel, velocity=velocity)


def off(note=60, channel=0):
    return mido.Message('note_off', note=note, channel=channel)


def cc(control, value, channel=0):
    return mido.Message('control_change', control=control, value=value, channel=channel)


class LiveStateTests(unittest.TestCase):
    def test_held_note_ignores_velocity_and_ends_on_release(self):
        for velocity in (1, 64, 127):
            state = LiveNoteState(LiveSettings(merge=False))
            self.assertEqual(state.feed(on(velocity=velocity), 0), [('down', 'A')])
            self.assertEqual(state.tick(2), [])
            self.assertEqual(state.feed(off(), 2), [('up', 'A')])

    def test_zero_velocity_is_release_and_short_taps_have_minimum(self):
        state = LiveNoteState(LiveSettings(merge=False))
        state.feed(on(), 0)
        self.assertEqual(state.feed(on(velocity=0), .01), [])
        self.assertEqual(state.tick(.041), [('up', 'A')])

    def test_pedal_only_extends_released_notes(self):
        state = LiveNoteState(LiveSettings(merge=False))
        state.feed(on(), 0)
        state.feed(cc(64, 127), .1)
        self.assertEqual(state.feed(off(), .2), [])
        self.assertEqual(state.tick(1), [])
        self.assertEqual(state.feed(cc(64, 0), 2), [('up', 'A')])

    def test_fixed_mode_ignores_off_and_pedal_duration(self):
        state = LiveNoteState(LiveSettings(follow_release=False, merge=False))
        state.feed(on(), 0)
        state.feed(cc(64, 127), .01)
        self.assertEqual(state.tick(.041), [('up', 'A')])

    def test_folded_sources_share_ownership(self):
        state = LiveNoteState(LiveSettings(merge=False))
        self.assertEqual(state.feed(on(36), 0), [('down', 'Z')])
        self.assertEqual(state.feed(on(48, 1), .001), [])
        self.assertEqual(state.feed(off(36), .1), [])
        self.assertEqual(state.feed(off(48, 1), .2), [('up', 'Z')])

    def test_transpose_fold_and_drum_filter(self):
        state = LiveNoteState(LiveSettings(transpose=-1, fold=False, merge=False))
        self.assertEqual(state.feed(on(61), 0), [('down', 'A')])
        self.assertEqual(state.feed(on(25), .1), [])
        self.assertEqual(state.feed(on(61, 9), .1), [])

    def test_retrigger_has_gap_and_minimum_hold_from_actual_press(self):
        state = LiveNoteState(LiveSettings(merge=False))
        state.feed(on(), 0)
        state.feed(off(), .1)
        self.assertEqual(state.feed(on(), .101), [])
        state.feed(off(), .102)
        self.assertEqual(state.tick(.109), [])
        self.assertEqual(state.tick(.112), [('down', 'A')])
        self.assertEqual(state.tick(.150), [])
        self.assertEqual(state.tick(.153), [('up', 'A')])

    def test_all_notes_off_and_panic_clear_state(self):
        for control in (120, 123):
            state = LiveNoteState(LiveSettings(merge=False))
            state.feed(on(), 0)
            self.assertEqual(state.feed(cc(control, 0), .1), [('up', 'A')])
            self.assertEqual(state.tick(.2), [])
        state.feed(on(), 1)
        self.assertEqual(state.release_all(), [('up', 'A')])
        self.assertEqual(state.tick(4), [])

    def test_winmm_short_packets(self):
        self.assertEqual(decode_short_message(0x643C90), on())
        self.assertEqual(decode_short_message(0x0005C2), mido.Message('program_change', channel=2, program=5))
        self.assertEqual(decode_short_message(0x7F40B0), cc(64,127))
        self.assertIsNone(decode_short_message(0xF8))


class FakeBackend:
    error = None
    def __init__(self): self.connected = True; self.closed = False
    def get_input_names(self): return ['Test'] if self.connected else []
    def open_input(self, name, callback): self.callback = callback; return self
    def close(self): self.closed = True


class FakeKeyboard:
    def __init__(self): self.down = set(); self.events = []
    def keyDown(self, key): self.down.add(key); self.events.append(('down', key))
    def keyUp(self, key): self.down.discard(key); self.events.append(('up', key))


class LiveInputTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=1.5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.005)
        self.fail('MIDI worker timed out')

    def test_focus_pause_stop_and_device_disconnect_release_keys(self):
        backend, keyboard = FakeBackend(), FakeKeyboard()
        focus = threading.Event(); focus.set()
        ready = threading.Event()
        live = LiveMidiInput('Test', LiveSettings(), keyboard=keyboard, backend=backend,
            foreground=focus.is_set, delay=0, on_status=lambda text: ready.set() if '就绪' in text else None)
        live.start()
        try:
            self.assertTrue(ready.wait(1))
            backend.callback(on()); self.wait_for(lambda: 'a' in keyboard.down)
            focus.clear(); self.wait_for(lambda: not keyboard.down)
            backend.callback(on(62)); time.sleep(.02)
            ready.clear(); focus.set(); self.assertTrue(ready.wait(1)); time.sleep(.02)
            self.assertFalse(keyboard.down)  # no replay of background notes
            backend.callback(on()); self.wait_for(lambda: 'a' in keyboard.down)
            live.paused.set(); self.wait_for(lambda: not keyboard.down)
            ready.clear(); live.paused.clear(); self.assertTrue(ready.wait(1))
            backend.callback(on()); self.wait_for(lambda: 'a' in keyboard.down)
            backend.connected = False
            self.wait_for(live.stopped.is_set)
            self.assertFalse(keyboard.down)
        finally:
            live.stop()
        self.assertTrue(backend.closed)

    def test_stop_before_ready_never_sends_note_down(self):
        backend, keyboard = FakeBackend(), FakeKeyboard()
        live = LiveMidiInput('Test', LiveSettings(), keyboard=keyboard, backend=backend, foreground=lambda: True, delay=5)
        live.start(); backend.callback(on()); live.stop()
        self.assertFalse([e for e in keyboard.events if e[0] == 'down'])
        self.assertTrue(backend.closed)


if __name__ == '__main__': unittest.main()
