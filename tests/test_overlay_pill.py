"""The listening pill: re-raised above other topmost windows without being
activated, placed on the pointer's monitor, robust to a replaced window
handle, and a draw failure is logged (throttled) without stopping the loop.

No real window is created: Win32 and Tk are faked."""

import fakes  # noqa: F401  (HOLDTOTALK_HOME -> a temporary folder first)

import collections
import re
import unittest
from pathlib import Path
from unittest import mock

import app as app_mod
import overlay
from app import App

PILL, REPLACEMENT = 100, 200


class FakeUser32:
    """Just the user32 calls overlay.py makes. Anything else (an activating
    call) raises AttributeError."""

    def __init__(self):
        self.alive = {PILL}
        self.wrapper = PILL           # what GetParent(winfo_id) returns
        self.styles = {}
        self.moves = []               # (hwnd, insert_after, x, y, flags)

    def IsWindow(self, h):
        return h in self.alive

    def GetParent(self, child):
        return self.wrapper

    def GetWindowLongW(self, h, index):
        return self.styles.get(h, 0)

    def SetWindowLongW(self, h, index, value):
        self.styles[h] = value
        return 0

    def SetWindowPos(self, h, after, x, y, cx, cy, flags):
        after = -1 if after is overlay.HWND_TOPMOST else after
        self.moves.append((h, after, x, y, flags))
        return True

    def raises(self):
        return [m for m in self.moves if m[1] == -1]


class FakeWin:
    def __init__(self):
        self.geom = "132x34+-20000+-20000"
        self.fail = 0

    def geometry(self, new=None):
        if new is None:
            return self.geom
        if self.fail:
            self.fail -= 1
            raise RuntimeError("synthetic Tk failure")
        self.geom = new

    def winfo_id(self):
        return 5


MONITORS = {   # name: (monitor rect, work area)
    "primary": ((0, 0, 1920, 1080), (0, 0, 1920, 1040)),
    "right": ((1920, 0, 4480, 1440), (1920, 0, 4480, 1392)),
    "left-above": ((-2560, -360, 0, 1080), (-2560, -360, 0, 1032)),
}


def monitor_at(point):
    for rect, work in MONITORS.values():
        if rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]:
            return work
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.u32 = FakeUser32()
        patcher = mock.patch.object(overlay, "user32", self.u32)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1000.0
        self.pointer = (500, 500)
        ov = self.ov = overlay.Overlay.__new__(overlay.Overlay)
        ov.root = mock.Mock()
        ov.root.winfo_screenwidth.return_value = 1920
        ov.root.winfo_screenheight.return_value = 1080
        ov.win, ov.canvas = FakeWin(), mock.Mock()
        ov.color, ov.background, ov.dim = "#ffffff", "#000000", "#555555"
        ov.scale, ov.w, ov.h = 1.0, 132, 34
        ov.bars, ov.dots = list(range(overlay.BAR_COUNT)), [1, 2, 3]
        ov.pos, ov.home, ov.away = overlay.AWAY, None, "132x34+-20000+-20000"
        ov.hwnd = PILL
        ov.levels = collections.deque([0.0] * overlay.BAR_COUNT,
                                      maxlen=overlay.BAR_COUNT)
        ov.state, ov.frame, ov.visible, ov._next_raise = "hidden", 0, False, 0.0
        ov.clock = lambda: self.now
        ov.cursor_pos = lambda: self.pointer
        ov.work_area_at = monitor_at

    def tick(self, state, seconds=0.0):
        self.now += seconds
        self.ov.update(state, 0.05)


class Raise(Base):
    def test_show_moves_on_screen_and_raises_without_activating(self):
        self.tick("listening")
        (h, after, x, y, flags), = self.u32.moves
        self.assertEqual((h, after), (PILL, -1))
        self.assertTrue(flags & overlay.SWP_NOACTIVATE)
        self.assertFalse(flags & overlay.SWP_NOZORDER)
        self.assertEqual((x, y), overlay.place((0, 0, 1920, 1040), 132, 34, 36))
        self.assertEqual(self.ov.win.geom, f"132x34+{x}+{y}")
        self.assertTrue(self.ov.visible)
        style = self.u32.styles[PILL]
        self.assertEqual(style & overlay.OVERLAY_EXSTYLE, overlay.OVERLAY_EXSTYLE)

    def test_reraise_on_every_state_change(self):
        self.tick("listening")
        self.tick("working", 0.015)                 # transcription
        self.tick("listening", 0.015)               # second press
        self.assertEqual(len(self.u32.raises()), 3)

    def test_periodic_reraise_while_visible(self):
        self.tick("listening")
        for _ in range(10):                         # 150 ms: not yet
            self.tick("listening", 0.015)
        self.assertEqual(len(self.u32.raises()), 1)
        for _ in range(7):                          # past 250 ms
            self.tick("listening", 0.015)
        self.assertEqual(len(self.u32.raises()), 2)
        for _ in range(66):                         # ~1 s more: ~4 more
            self.tick("working", 0.015)
        self.assertGreaterEqual(len(self.u32.raises()), 6)
        self.assertLessEqual(len(self.u32.raises()), 8)   # cheap: not per tick

    def test_no_reraise_while_hidden(self):
        self.tick("listening")
        self.tick("hidden", 0.015)
        hide = self.u32.moves[-1]
        self.assertEqual(hide[2:4], overlay.AWAY)
        self.assertTrue(hide[4] & overlay.SWP_NOZORDER)
        self.assertTrue(hide[4] & overlay.SWP_NOACTIVATE)
        before = len(self.u32.moves)
        for _ in range(200):
            self.tick("hidden", 0.015)
        self.assertEqual(len(self.u32.moves), before)
        self.assertEqual(len(self.u32.raises()), 1)

    def test_every_call_is_non_activating(self):
        for state in ["listening"] * 30 + ["working"] * 30 + ["hidden"] * 3 + \
                ["listening"] * 3:
            self.tick(state, 0.015)
        self.assertTrue(all(m[4] & overlay.SWP_NOACTIVATE for m in self.u32.moves))
        src = Path(overlay.__file__).read_text(encoding="utf-8")
        for bad in ("SetForegroundWindow", "BringWindowToTop",
                    "SetActiveWindow", "SetFocus", ".deiconify(", ".lift(",
                    ".focus"):
            self.assertNotIn(bad, src)
        self.assertIsNone(re.search(r"ShowWindow\([^)]*SW_SHOW\b", src))

    def test_style_restored_if_tk_reset_it(self):
        self.tick("listening")
        self.u32.styles[PILL] = 0                    # Tk touched attributes
        self.tick("listening", 0.3)
        self.assertEqual(self.u32.styles[PILL] & overlay.OVERLAY_EXSTYLE,
                         overlay.OVERLAY_EXSTYLE)

    def test_failed_show_is_retried_next_tick(self):
        self.ov.win.fail = 1
        with self.assertRaises(RuntimeError):
            self.tick("listening")
        self.assertFalse(self.ov.visible)
        self.tick("listening", 0.015)
        self.assertTrue(self.ov.visible)
        self.assertEqual(len(self.u32.raises()), 1)


class PauseStates(Base):
    def test_paused_and_resumed_show_without_activating(self):
        self.ov.icon = 9
        for state in ("paused", "paused", "resumed", "resumed", "hidden"):
            self.tick(state, 0.015)
        self.assertTrue(all(m[4] & overlay.SWP_NOACTIVATE
                            for m in self.u32.moves))
        self.assertEqual(len(self.u32.raises()), 2)   # shown, state change
        with self.assertRaises(ValueError):
            self.ov.update("sleeping")


class Placement(unittest.TestCase):
    def check_inside(self, work, pos, w=132, h=34):
        self.assertTrue(work[0] <= pos[0] and pos[0] + w <= work[2], (work, pos))
        self.assertTrue(work[1] <= pos[1] and pos[1] + h <= work[3], (work, pos))

    def test_primary(self):
        self.assertEqual(overlay.place((0, 0, 1920, 1040), 132, 34, 36),
                         (894, 970))

    def test_secondary(self):
        pos = overlay.place((1920, 0, 4480, 1392), 132, 34, 36)
        self.assertEqual(pos, (1920 + (2560 - 132) // 2, 1392 - 34 - 36))
        self.check_inside((1920, 0, 4480, 1392), pos)

    def test_negative_coordinates(self):
        work = (-2560, -360, 0, 1032)
        pos = overlay.place(work, 132, 34, 36)
        self.assertEqual(pos, (-2560 + (2560 - 132) // 2, 1032 - 70))
        self.check_inside(work, pos)

    def test_taskbar_on_top_or_side_and_tiny_work_area(self):
        self.assertEqual(overlay.place((0, 48, 1920, 1080), 132, 34, 36),
                         (894, 1010))
        self.assertEqual(overlay.place((62, 0, 1920, 1080), 132, 34, 36),
                         (62 + (1858 - 132) // 2, 1010))
        self.assertEqual(overlay.place((0, 100, 800, 150), 132, 34, 36)[1], 100)

    def test_scaled_size_and_margin(self):        # 150 % system scale
        self.assertEqual(overlay.place((0, 0, 2560, 1380), 198, 51, 54),
                         ((2560 - 198) // 2, 1380 - 51 - 54))


class PointerMonitor(Base):
    def test_shows_on_the_pointer_monitor_each_time(self):
        for name, (rect, work) in MONITORS.items():
            with self.subTest(monitor=name):
                self.pointer = ((rect[0] + rect[2]) // 2, rect[1] + 10)
                self.tick("listening", 0.015)
                self.assertEqual(self.ov.pos, overlay.place(work, 132, 34, 36))
                Placement.check_inside(self, work, self.ov.pos)
                self.assertEqual(self.u32.moves[-1][2:4], self.ov.pos)
                self.tick("hidden", 0.015)

    def test_pointer_moving_while_shown_does_not_jump(self):
        self.tick("listening")
        first = self.ov.pos
        self.pointer = (3000, 100)
        self.tick("listening", 1.0)
        self.assertEqual(self.ov.pos, first)
        self.assertEqual(self.u32.moves[-1][2:4], first)

    def test_falls_back_to_primary(self):
        for cursor, lookup in ((lambda: None, lambda p: None),
                               (lambda: (1, 1), mock.Mock(side_effect=OSError))):
            with self.subTest():
                self.ov.cursor_pos, self.ov.work_area_at = cursor, lookup
                self.tick("listening", 0.015)
                # The old position: 84 px above the bottom of the screen.
                self.assertEqual(self.ov.pos, ((1920 - 132) // 2, 1080 - 34 - 84))
                self.tick("hidden", 0.015)


class Handle(Base):
    def test_destroyed_handle_is_re_resolved(self):
        self.tick("listening")
        self.u32.alive = {REPLACEMENT}
        self.u32.wrapper = REPLACEMENT
        self.tick("listening", 0.3)
        self.assertEqual(self.ov.hwnd, REPLACEMENT)
        self.assertEqual(self.u32.moves[-1][:2], (REPLACEMENT, -1))
        self.assertEqual(self.u32.styles[REPLACEMENT] & overlay.OVERLAY_EXSTYLE,
                         overlay.OVERLAY_EXSTYLE)

    def test_replaced_wrapper_is_re_resolved(self):
        self.u32.alive = {PILL, REPLACEMENT}
        self.u32.wrapper = REPLACEMENT
        self.tick("listening")
        self.assertEqual(self.ov.hwnd, REPLACEMENT)
        self.assertEqual(self.u32.moves[-1][0], REPLACEMENT)

    def test_valid_handle_kept(self):
        self.tick("listening")
        self.tick("listening", 0.3)
        self.assertEqual(self.ov.hwnd, PILL)
        self.u32.wrapper = 0                       # lookup failed: keep it
        self.tick("listening", 0.3)
        self.assertEqual(self.ov.hwnd, PILL)


class DrawFailure(unittest.TestCase):
    def setUp(self):
        a = self.app = App.__new__(App)
        a._preview, a._state, a._pending, a._closed = None, "idle", 1, False
        a._step = lambda: None
        a.root = mock.Mock()
        a.overlay = mock.Mock()
        a.overlay.update.side_effect = RuntimeError("synthetic draw failure")

    def test_logged_once_and_loop_keeps_running(self):
        with self.assertLogs("holdtotalk.app", "ERROR") as logs:
            for _ in range(50):
                self.app._tick()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Could not draw the listening pill", logs.output[0])
        self.assertIn("synthetic draw failure", logs.output[0])
        self.assertNotIn("Error in the hotkey loop", "".join(logs.output))
        self.assertEqual(self.app.overlay.update.call_count, 50)   # retried
        self.assertEqual(self.app.root.after.call_count, 50)       # rescheduled
        self.assertEqual(self.app._draw_errors, 49)

        # A minute later it is reported again, with the suppressed count.
        self.app._draw_error_at -= app_mod.DRAW_ERROR_LOG_SECONDS
        with self.assertLogs("holdtotalk.app", "ERROR") as logs:
            self.app._tick()
            self.app._tick()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("49 similar failures", logs.output[0])

    def test_recovers_when_drawing_works_again(self):
        with self.assertLogs("holdtotalk.app", "ERROR"):
            self.app._tick()
        self.app.overlay.update.side_effect = None
        self.app._tick()
        self.app.overlay.update.assert_called_with("working", 0.0)


if __name__ == "__main__":
    unittest.main()
