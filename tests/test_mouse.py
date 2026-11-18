"""Mouse-button hotkeys: names, the hook helper's decisions, the app's
state machine with a fake hook, hotkey capture, and one real helper process
that is configured so it never swallows anything."""

import fakes  # noqa: F401  (HOLDTOTALK_HOME -> a temporary folder first)

import shutil
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import app as app_mod
import keys
import mousehelper
import mousehook
import settings
import tray
from app import App

CTRL = (0x11, 0xA2, 0xA3)


class Names(unittest.TestCase):
    def test_mouse_names_and_aliases(self):
        for name, vk in (("mouse3", 4), ("Mouse 3", 4), ("middle mouse", 4),
                         ("mouse4", 5), ("MOUSE  4", 5), ("xbutton1", 5),
                         ("x1", 5), ("mouse back", 5), ("mb4", 5),
                         ("mouse5", 6), ("xbutton2", 6), ("mouse forward", 6),
                         ("x2", 6)):
            with self.subTest(name=name):
                self.assertEqual(keys.parse_hotkey(name), [(vk,)])

    def test_combos_with_keyboard_keys(self):
        self.assertEqual(keys.parse_hotkey("ctrl+mouse4"), [CTRL[:1], (5,)])
        self.assertEqual(keys.parse_hotkey("mouse5 + right alt"),
                         [(6,), (0xA5,)])
        self.assertEqual(keys.parse_hotkey("mouse4+mouse5"), [(5,), (6,)])
        self.assertEqual(keys.mouse_buttons(keys.parse_hotkey("ctrl+mouse4")),
                         {5})
        self.assertEqual(keys.mouse_buttons(keys.parse_hotkey("ctrl+d")),
                         frozenset())

    def test_left_and_right_click_are_rejected(self):
        for bad in ("mouse1", "mouse2", "lbutton", "rbutton", "left click",
                    "ctrl+right click", "mouse 1", "left mouse", "lmb",
                    "mouse4+mouse2"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "left and right"):
                    keys.parse_hotkey(bad)

    def test_other_errors(self):
        for bad in ("mouse6", "mouse4+x1", "mouse4+", "mouse"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                keys.parse_hotkey(bad)

    def test_canonical_and_description(self):
        self.assertEqual(keys.canonical_hotkey(" Mouse Back + Ctrl "),
                         "mouse4+ctrl")
        self.assertEqual(settings.normalize_hotkey("XButton2"), "mouse5")
        self.assertEqual(settings.normalize_hotkey(" Ctrl + Windows "),
                         "ctrl+windows")
        self.assertEqual(keys.describe_hotkey("x1"),
                         "mouse4 (Back side button, XBUTTON1)")
        self.assertEqual(keys.describe_hotkey("ctrl+windows"), "ctrl+windows")
        with self.assertRaises(ValueError):
            settings.normalize_hotkey("right click")

    def test_combo_from_vks(self):
        self.assertEqual(keys.combo_from_vks({5}), "mouse4")
        self.assertEqual(keys.combo_from_vks({6, 0xA2}), "ctrl+mouse5")
        self.assertEqual(keys.combo_from_vks({4}), "mouse3")
        self.assertEqual(keys.combo_from_vks({1, 2}), "")     # never L/R
        self.assertEqual(keys.combo_from_vks({1, 5}), "mouse4")


class Compile(unittest.TestCase):
    def test_mouse_only_hotkey_has_no_cancel_keys_and_no_mask(self):
        groups, other, mask = App._compile_hotkey("mouse4")
        self.assertEqual(other, [])
        self.assertFalse(mask)

    def test_combo_keeps_the_keyboard_rules(self):
        groups, other, mask = App._compile_hotkey("alt+mouse4")
        self.assertTrue(mask)                   # Alt release opens menus
        self.assertIn(ord("V"), other)
        self.assertNotIn(0x12, other)


class HelperDecisions(unittest.TestCase):
    """mousehelper.Helper.button: the code the hook callback runs."""

    def make(self, buttons=(5,), groups=()):
        lines = []
        return mousehelper.Helper(buttons, groups, lines.append), lines

    def test_swallows_only_the_bound_button(self):
        h, lines = self.make()
        self.assertTrue(h.button(5, True))
        self.assertFalse(h.button(6, True))     # Forward passes through
        self.assertFalse(h.button(6, False))
        self.assertTrue(h.button(5, False))
        self.assertEqual(lines, ["D 5\n", "U 5\n"])

    def test_up_without_swallowed_down_passes(self):
        h, lines = self.make()
        self.assertFalse(h.button(5, False))    # pressed before we hooked
        self.assertEqual(lines, [])

    def test_missed_release_self_heals(self):
        h, lines = self.make()
        h.button(5, True)
        self.assertTrue(h.button(5, True))      # up was missed: still ours
        self.assertTrue(h.button(5, False))
        self.assertEqual(h.swallowed, set())

    def test_desktop_switch_releases_held_buttons(self):
        h, lines = self.make(buttons=(5, 6))
        h.button(5, True)
        h.button(6, True)
        h.release_all()
        self.assertEqual(lines[-2:], ["U 5\n", "U 6\n"])
        self.assertFalse(h.button(5, False))    # late up is not eaten

    def test_combo_condition(self):
        held = set()
        with mock.patch.object(mousehelper.user32, "GetAsyncKeyState",
                               side_effect=lambda vk: -32768 if vk in held
                               else 0):
            h, lines = self.make(groups=[CTRL])
            self.assertFalse(h.button(5, True))  # no ctrl: Back works
            self.assertFalse(h.button(5, False))
            held.add(0xA2)
            self.assertTrue(h.button(5, True))
            held.clear()
            self.assertTrue(h.button(5, False))  # its down was eaten
        self.assertEqual(lines, ["D 5\n", "U 5\n"])

    def test_args(self):
        self.assertEqual(mousehelper.parse_args(
            ["--parent", "7", "--buttons", "5", "--keys", "11.a2+5b.5c"]),
            (7, {5}, [(0x11, 0xA2), (0x5B, 0x5C)]))
        self.assertEqual(mousehelper.parse_args(["--buttons", "5",
                                                 "--capture"])[1], {4, 5, 6})
        with self.assertRaises(SystemExit):
            mousehelper.parse_args(["--buttons", "1"])     # left click
        self.assertEqual(mousehook.key_groups_arg([CTRL, (0x5B, 0x5C)]),
                         "11.a2.a3+5b.5c")


class Heartbeat(unittest.TestCase):
    """E5: MouseHook.poll judges the helper only on pings it could answer."""

    def make(self):
        import threading
        h = mousehook.MouseHook.__new__(mousehook.MouseHook)
        h.state, h.error = "active", ""
        h._lock = threading.Lock()
        h._down = {5}                         # the user holds mouse4
        h._ping_seq, h.tid = 0, 1
        h._last_ping, h._last_pong = 0.0, None
        h._last_poll = None
        h._unanswered_since = h._unanswered_seq = None
        self.killed, self.pinged = [], []
        h._kill = lambda: self.killed.append(True)
        h._post = lambda msg, w=0, l=0: self.pinged.append(w) or True
        return h

    def answer(self, h):
        h._line(["P", str(self.pinged[-1])])  # the helper's pong

    def test_our_stall_does_not_kill_a_healthy_helper(self):
        h = self.make()
        t0 = time.monotonic()
        for i in range(3):                    # normal ticks, prompt pongs
            self.assertEqual(h.poll(t0 + i), "active")
            self.answer(h)
        # The Tk thread is stuck for 3.6 s (slow callback, sleep/resume).
        self.assertEqual(h.poll(t0 + 2 + 3.6), "active")
        self.assertEqual(self.killed, [])
        self.assertTrue(h.is_down(5))         # the recording survives

    def test_a_helper_that_stops_answering_is_still_caught(self):
        h = self.make()
        t0 = time.monotonic()
        h.poll(t0)
        self.answer(h)
        for i in range(1, 50):                # ticks keep coming, no pongs
            h.poll(t0 + i * 0.1)
        self.assertEqual(h.state, "dead")
        self.assertEqual(self.killed, [True])
        self.assertIn("stopped answering", h.error)


# -- the app state machine with a fake hook -------------------------------------

class FakeHook:
    def __init__(self, buttons, key_groups, capture):
        self.args = (set(buttons), key_groups, capture)
        self.state = "active"
        self.down = set()
        self.stopped = False
        self.error = ""
        self.pid = self.helper_pid = 1234
        self.active_since = time.monotonic()

    def poll(self, now=None):
        return self.state

    def is_down(self, vk):
        return self.state == "active" and vk in self.down

    def stop(self):
        self.stopped = True
        self.state = "stopped"


class FakeRecorder:
    def __init__(self):
        self.recording = False
        self.starts = 0
        self.level = 0.0

    def start(self):
        self.recording = True
        self.starts += 1

    def stop(self):
        self.recording = False
        return np.full(16000, 0.1, dtype=np.float32)    # 1 s, not silent

    def close(self):
        pass

    def set_device(self, device):
        pass


class StateMachineBase(unittest.TestCase):
    hotkey = "mouse4"

    def setUp(self):
        self.keys_down = set()          # what GetAsyncKeyState would say
        patcher = mock.patch.object(keys, "key_down",
                                    side_effect=lambda vk: vk in self.keys_down)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hooks = []
        self.fail_factory = False

        def factory(buttons, key_groups, capture):
            if self.fail_factory:
                raise OSError("no hooks today")
            hook = FakeHook(buttons, key_groups, capture)
            self.hooks.append(hook)
            return hook
        self.app = self.make_app(self.hotkey, factory)

    def make_app(self, hotkey, factory):
        a = App.__new__(App)
        a._init_state({"hotkey": hotkey, "min_seconds": 0.1},
                      engine=types.SimpleNamespace(), dry_run=True)
        a.recorder = FakeRecorder()
        a.hook_factory = factory
        return a

    def step(self, n=1):
        for _ in range(n):
            self.app._step()

    @property
    def hook(self):
        return self.hooks[-1] if self.hooks else None

    def jobs(self):
        return self.app._jobs.qsize()


class MouseHotkey(StateMachineBase):
    def test_hook_started_with_the_bound_button(self):
        self.step()
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.hook.args, ({5}, (), False))
        self.assertEqual(self.app.mouse_hook_state, "active")
        self.step(3)
        self.assertEqual(len(self.hooks), 1)      # not restarted every tick

    def test_hold_and_release_from_the_hook(self):
        self.step()
        self.hook.down.add(5)
        self.step()
        self.assertEqual(self.app._state, "recording")
        self.keys_down.add(ord("A"))              # typing does not cancel
        self.step()
        self.assertEqual(self.app._state, "recording")
        self.hook.down.discard(5)
        self.step()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.jobs(), 1)

    def test_missed_release_reported_by_helper_ends_the_recording(self):
        # e.g. the session locked while held: the helper sends "U 5" itself
        # (HelperDecisions.test_desktop_switch_releases_held_buttons).
        self.step()
        self.hook.down.add(5)
        self.step()
        self.hook.down.clear()
        self.step()
        self.assertEqual((self.app._state, self.jobs()), ("idle", 1))
        self.hook.down.add(5)                     # next press works again
        self.step()
        self.assertEqual(self.app._state, "recording")

    def test_helper_death_cancels_and_falls_back_to_polling(self):
        self.step()
        self.hook.down.add(5)
        self.step()
        self.assertEqual(self.app._state, "recording")
        with self.assertLogs("holdtotalk.app", "WARNING") as logs:
            self.hook.state = "dead"
            self.hook.down.clear()
            self.step()
        self.assertEqual(self.app._state, "idle")
        self.assertEqual(self.jobs(), 0)          # nothing half-pasted
        self.assertTrue(any("falling back" in m or "polling" in m
                            for m in logs.output))
        self.assertEqual(self.app.mouse_hook_state, "fallback")
        # Polling now: the unswallowed button works through GetAsyncKeyState.
        self.keys_down.add(5)
        self.step()
        self.assertEqual(self.app._state, "recording")
        self.keys_down.discard(5)
        self.step()
        self.assertEqual(self.jobs(), 1)

    def test_dead_helper_is_retried_then_given_up(self):
        self.step()
        for i in range(app_mod.HOOK_MAX_FAILURES):
            self.hook.state = "dead"
            with self.assertLogs("holdtotalk.app", "WARNING"):
                self.step()
            self.app._hook_retry_at = 0.0         # skip the wait
            self.step()
        self.assertEqual(len(self.hooks), app_mod.HOOK_MAX_FAILURES)
        self.assertEqual(self.app.mouse_hook_state, "fallback")
        self.app.set_paused(True)                 # pause/resume retries
        self.step()
        self.app.set_paused(False)
        self.step()
        self.assertEqual(len(self.hooks), app_mod.HOOK_MAX_FAILURES + 1)

    def test_helper_that_cannot_start(self):
        self.fail_factory = True
        with self.assertLogs("holdtotalk.app", "WARNING") as logs:
            self.step()
        self.assertIn("no hooks today", logs.output[0])
        self.assertEqual(self.app.mouse_hook_state, "fallback")
        self.keys_down.add(5)
        self.step()
        self.assertEqual(self.app._state, "recording")

    def test_pause_removes_the_hook_and_resume_restores_it(self):
        self.step()
        first = self.hook
        self.app.set_paused(True)
        self.step()
        self.assertTrue(first.stopped)
        self.assertEqual(self.app.mouse_hook_state, "off")
        self.app.set_paused(False)
        self.step()
        self.assertIsNot(self.hook, first)
        self.assertEqual(self.app.mouse_hook_state, "active")

    def test_no_hook_while_the_model_loads(self):
        self.app.status = "loading"
        self.step()
        self.assertEqual(self.hooks, [])
        self.app.status = "ready"
        self.step()
        self.assertEqual(len(self.hooks), 1)

    def test_shutdown_removes_the_hook(self):
        self.step()
        self.app.overlay = mock.Mock()
        self.app.root = mock.Mock()
        self.app._shutdown()
        self.assertTrue(self.hook.stopped)
        self.step()
        self.assertEqual(len(self.hooks), 1)     # never restarted

    def test_button_held_by_hook_and_shown_as_listening(self):
        self.step()
        self.hook.down.add(5)
        self.step()
        handle = tray.TrayHandle.__new__(tray.TrayHandle)
        handle.app = self.app
        self.assertEqual(handle.state(), "listening")


class ComboHotkey(StateMachineBase):
    hotkey = "ctrl+mouse5"

    def test_combo_needs_both(self):
        self.step()
        self.assertEqual(self.hook.args, ({6}, ((0x11,),), False))
        self.hook.down.add(6)
        self.step()
        self.assertEqual(self.app._state, "idle")
        self.keys_down.add(0x11)
        self.step()
        self.assertEqual(self.app._state, "recording")
        self.keys_down.add(ord("C"))              # ctrl+c: another shortcut
        self.step()
        self.assertEqual(self.app._state, "idle")
        self.assertTrue(self.app._blocked)


class KeyboardHotkey(StateMachineBase):
    hotkey = "ctrl+windows"

    def test_installs_no_hook(self):
        self.keys_down |= {0x11, 0x5B}
        self.step(3)
        self.assertEqual(self.app._state, "recording")
        self.keys_down.clear()
        self.step()
        self.assertEqual(self.jobs(), 1)
        self.app.set_paused(True)
        self.step()
        self.app.set_paused(False)
        self.step()
        self.assertEqual(self.hooks, [])
        self.assertEqual(self.app.mouse_hook_state, "off")

    def test_other_key_still_cancels_but_not_our_own_paste(self):
        self.keys_down |= {0x11, 0x5B}
        self.step()
        keys.last_injected = time.monotonic()    # we just sent Ctrl+V
        self.keys_down.add(ord("V"))
        try:
            self.step()
            self.assertEqual(self.app._state, "recording")
            keys.last_injected = 0.0
            self.step()
            self.assertEqual(self.app._state, "idle")
        finally:
            keys.last_injected = 0.0


class Capture(StateMachineBase):
    def capture(self, timeout=10.0):
        got = []
        self.app.capture_hotkey(got.append, timeout=timeout)
        return got

    def test_capture_returns_a_mouse_button_and_swallows_during_capture(self):
        self.step()
        hotkey_hook = self.hook
        got = self.capture()
        self.step()
        self.assertTrue(hotkey_hook.stopped)
        cap = self.hook
        self.assertEqual(cap.args, ({4, 5, 6}, (), True))
        cap.down.add(5)
        self.step()
        self.assertEqual(self.app._state, "idle")   # hotkey inactive
        self.assertEqual(got, [])
        cap.down.clear()
        self.step()
        self.assertEqual(got, ["mouse4"])
        self.step()
        self.assertTrue(cap.stopped)
        self.assertEqual(self.hook.args, ({5}, (), False))
        self.assertFalse(self.app.paused)

    def test_capture_keyboard_combo_and_mouse_combo(self):
        got = self.capture()
        self.keys_down |= {0xA2}
        self.step()
        self.keys_down |= {ord("D")}
        self.step()
        self.keys_down.clear()
        self.step()
        got2 = self.capture()
        self.step()
        self.keys_down.add(0xA2)
        self.hook.down.add(6)
        self.step()
        self.keys_down.clear()
        self.hook.down.clear()
        self.step()
        self.assertEqual((got, got2), (["ctrl+d"], ["ctrl+mouse5"]))

    def test_esc_timeout_cancel_and_supersede_give_none(self):
        got = self.capture()
        self.keys_down.add(0x1B)
        self.step()
        self.keys_down.clear()
        self.step()
        got_timeout = self.capture(timeout=0)
        self.step()
        got_cancel = self.capture()
        self.app.cancel_capture()
        got_old = self.capture()
        got_new = self.capture()
        self.assertEqual((got, got_timeout, got_cancel, got_old, got_new),
                         ([None], [None], [None], [None], []))

    def test_keys_held_at_start_are_ignored(self):
        self.keys_down.add(0x0D)                  # Enter activated the button
        got = self.capture()
        self.step()
        self.keys_down.clear()
        self.step()
        self.assertEqual(got, [])
        self.keys_down.add(0x78)
        self.step()
        self.keys_down.clear()
        self.step()
        self.assertEqual(got, ["f9"])

    def test_pause_state_is_kept(self):
        self.app.set_paused(True)
        got = self.capture()
        self.step()
        self.assertEqual(self.hook.args[2], True)  # capture hooks even paused
        self.hook.down.add(4)
        self.step()
        self.hook.down.clear()
        self.step()
        self.assertEqual(got, ["mouse3"])
        self.assertTrue(self.app.paused)
        self.step()
        self.assertEqual(self.app.mouse_hook_state, "off")

    def test_capture_without_a_hook_polls(self):
        self.fail_factory = True
        got = self.capture()
        with self.assertLogs("holdtotalk.app", "WARNING"):
            self.step()
        self.keys_down.add(6)
        self.step()
        self.keys_down.clear()
        self.step()
        self.assertEqual(got, ["mouse5"])


class LiveSwitch(StateMachineBase):
    """reload_config switching keyboard <-> mouse hotkeys."""
    hotkey = "ctrl+windows"

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(prefix="htt-mouse-"))
        self.saved = settings.CONFIG_PATH
        settings.CONFIG_PATH = self.dir / "config.json"
        self.app.cfg = settings.load_config()
        self.app.on_reload = []

    def tearDown(self):
        settings.CONFIG_PATH = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_both_directions(self):
        self.step()
        self.assertEqual(self.hooks, [])
        settings.save_config({"hotkey": "mouse5"})
        self.assertEqual(self.app._reload()["errors"], [])
        self.assertEqual(self.hook.args, ({6}, (), False))
        self.step()
        self.hook.down.add(6)
        self.step()
        self.hook.down.clear()                    # blocked until released
        self.step()
        self.hook.down.add(6)
        self.step()
        self.assertEqual(self.app._state, "recording")
        settings.save_config({"hotkey": "ctrl+windows"})
        self.app._reload()
        self.assertTrue(self.hook.stopped)
        self.assertEqual(self.app._state, "idle")
        self.step()
        self.assertEqual(len(self.hooks), 1)
        self.assertEqual(self.app.mouse_hook_state, "off")
        settings.save_config({"hotkey": "right click"})
        result = self.app._reload()
        self.assertTrue(result["errors"])
        self.assertEqual(self.app.cfg["hotkey"], "ctrl+windows")


# -- a real helper process ---------------------------------------------------------

class RealHelper(unittest.TestCase):
    """Starts mousehelper.py for real. It requires the unassigned key 0xE8
    to be held, which never happens, so it cannot swallow a real click."""

    def test_lifecycle(self):
        hook = mousehook.MouseHook({5}, key_groups=[(0xE8,)])
        try:
            deadline = time.monotonic() + 10
            while hook.poll() == "starting" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(hook.state, "active", hook.error)
            self.assertTrue(hook.simulate(5, True))
            time.sleep(0.2)
            self.assertFalse(hook.is_down(5))    # combo not held: not eaten
            stats = hook.stats()
            self.assertIsNotNone(stats)
            self.assertEqual(stats["dropped"], 0)
            hook._kill()                         # helper crashes
            deadline = time.monotonic() + 5
            while hook.poll() != "dead" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(hook.state, "dead")
        finally:
            hook.stop()
        self.assertIsNotNone(hook.proc.poll())


if __name__ == "__main__":
    unittest.main()
