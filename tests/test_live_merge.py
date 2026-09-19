import unittest
from live_midi import LiveNoteState, LiveSettings
from test_live_midi import on, off, cc


class MergeTests(unittest.TestCase):
    def test_configurable_window_groups_downs_only(self):
        for ms in (1, 10, 30, 75, 200):
            with self.subTest(ms=ms):
                s = LiveNoteState(LiveSettings(merge_window_ms=ms))
                end = ms / 1000
                self.assertEqual([], s.feed(on(60), 0))
                self.assertEqual([], s.feed(on(64), end * .8))
                self.assertEqual([], s.tick(end - .00001))
                self.assertEqual([('down', 'A'), ('down', 'D')], s.tick(end))
                self.assertEqual([('up', 'A')], s.feed(off(60), end + .1))
                self.assertEqual({'D'}, set(s.pressed))
                self.assertEqual([('up', 'D')], s.feed(off(64), end + .2))

    def test_window_does_not_slide_or_merge_outside_boundary(self):
        s = LiveNoteState(LiveSettings(merge_window_ms=30))
        s.feed(on(60), 0); s.feed(on(62), .025)
        self.assertEqual([('down', 'A'), ('down', 'S')], s.tick(.030))
        self.assertEqual([], s.feed(on(64), .031))
        self.assertEqual([], s.tick(.060))
        self.assertEqual([('down', 'D')], s.tick(.061))

    def test_active_note_release_does_not_wait_for_new_chord(self):
        s = LiveNoteState(LiveSettings(merge_window_ms=100))
        s.feed(on(60), 0); s.tick(.1)
        s.feed(on(62), .2)
        self.assertEqual([('up', 'A')], s.feed(off(60), .21))
        s.feed(on(64), .25)
        self.assertEqual([], s.tick(.299))
        self.assertEqual([('down', 'D'), ('down', 'S')], s.tick(.3))

    def test_quick_tap_before_window_is_not_lost(self):
        s = LiveNoteState(LiveSettings(merge_window_ms=200))
        s.feed(on(60), 0); s.feed(off(60), .01)
        self.assertEqual([('down', 'A')], s.tick(.2))
        self.assertEqual([], s.tick(.239))
        self.assertEqual([('up', 'A')], s.tick(.241))

    def test_pedal_up_is_independent_of_pending_chord(self):
        s = LiveNoteState(LiveSettings(merge_window_ms=100))
        s.feed(on(), 0); s.tick(.1); s.feed(cc(64,127), .2); s.feed(off(), .3)
        s.feed(on(64), .4)
        self.assertEqual([('up','A')], s.feed(cc(64,0), .41))
        self.assertEqual([('down','D')], s.tick(.5))

    def test_zero_disabled_panic_and_late_worker(self):
        for settings in (LiveSettings(merge_window_ms=0), LiveSettings(merge=False)):
            self.assertEqual([('down','A')], LiveNoteState(settings).feed(on(), 0))
        for command in (cc(120,0), cc(123,0)):
            s = LiveNoteState(); s.feed(on(),0); s.feed(command,.01)
            self.assertEqual([],s.tick(.1))
        s = LiveNoteState()
        s.feed(on(), 0, flush=False); s.feed(off(), .01, flush=False)
        self.assertEqual([('down','A')], s.tick(.2))
        self.assertEqual([], s.tick(.23))
        self.assertEqual([('up','A')], s.tick(.241))

    def test_fold_ownership_and_retrigger_release_at_window(self):
        s=LiveNoteState()
        s.feed(on(36),0);s.feed(on(48,1),.01)
        self.assertEqual([('down','Z')], s.tick(.03))
        self.assertEqual([], s.feed(off(36),.1))
        self.assertEqual([('up','Z')], s.feed(off(48,1),.2))
        s=LiveNoteState();s.feed(on(),0);s.tick(.03)
        self.assertEqual([],s.feed(on(),.1))
        self.assertEqual({'A'},set(s.pressed))
        self.assertEqual([('up','A')],s.tick(.13))
        self.assertEqual([('down','A')],s.tick(.141))

    def test_invalid_window_rejected(self):
        for value in (-1,201):
            with self.assertRaises(ValueError): LiveSettings(merge_window_ms=value)
