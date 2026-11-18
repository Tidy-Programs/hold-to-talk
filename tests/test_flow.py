"""The hotkey flow on a bare App (no Tk): E1 pasting never collides with the
keyboard hotkey, E6 stray keys no longer throw a recording away, F1 the
pause shortcut, F2 the chimes. Keys, paste, chimes and microphone are
faked: no real input, clipboard or sound."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import shutil
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import app as app_mod
import keys
import settings
from app import App
from fakes import FakeRecorder, FakeSounds

CTRL, WIN, ALT, SHIFT = 0x11, 0x5B, 0x12, 0x10


def cfg_with(**kw):
    cfg = settings.load_config(Path(tempfile.gettempdir()) / "no-such.json")
    cfg["cleanup"] = dict(cfg["cleanup"], enabled=False)
    cfg.update(min_seconds=0.1, **kw)
    return cfg


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Base(unittest.TestCase):
    hotkey = "ctrl+windows"
    pause = None
    dry_run = False

    def setUp(self):
        self.down = set()
        self.pastes = []
        self.sounds = FakeSounds()
        for target, name, value in (
                (keys, "key_down", lambda vk: vk in self.down),
                (keys, "tap_mask_key", lambda: None),
                (keys, "paste_text", self.fake_paste),
                (app_mod, "sounds", self.sounds)):
            p = mock.patch.object(target, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)
        engine = types.SimpleNamespace(
            transcribe=lambda samples: "second dictation words",
            device="cpu")
        a = self.app = App.__new__(App)
        a._init_state(cfg_with(hotkey=self.hotkey, pause_hotkey=self.pause),
                      engine=engine, dry_run=self.dry_run)
        a.recorder = FakeRecorder()
        a.hook_factory = mock.Mock(side_effect=OSError("no hook in tests"))
        self.notices = []
        a.on_notice.append(lambda k, m: self.notices.append((k, m)))
        self.worker = threading.Thread(target=a._worker, daemon=True)
        self.worker.start()
        self.addCleanup(lambda: (a._jobs.put(None), self.worker.join(5)))

    def fake_paste(self, text, restore=True, **kw):
        self.pastes.append((text, self.app._state, set(self.down)))
        return "pasted"

    def step(self, n=1):
        for _ in range(n):
            self.app._step()

    def press(self, *vks):
        self.down |= set(vks)
        self.step()

    def release(self, *vks):
        self.down -= set(vks or self.down)
        self.step()


class PasteWaitsForTheHotkey(Base):
    def test_never_pasted_during_a_recording_and_order_kept(self):
        self.press(CTRL, WIN)
        self.assertEqual(self.app._state, "recording")
        self.app.submit_transcript("first dictation words")
        time.sleep(0.4)
        self.assertEqual(self.pastes, [])          # queued, not sent
        self.release()                             # second one ends
        self.assertTrue(wait_for(lambda: len(self.pastes) == 2))
        texts = [p[0].lower() for p in self.pastes]
        self.assertTrue(texts[0].startswith("first"))
        self.assertTrue(texts[1].startswith("second"))
        for text, state, held in self.pastes:
            self.assertEqual(state, "idle")
            self.assertFalse(held & {CTRL, WIN})

    def test_hotkey_held_but_not_recording_also_waits(self):
        self.press(CTRL, WIN)
        self.app._discard("test")                 # blocked, still held
        self.app._blocked = True
        self.app.submit_transcript("first dictation words")
        time.sleep(0.3)
        self.assertEqual(self.pastes, [])
        self.release()
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))

    def test_synthetic_key_up_does_not_end_the_recording(self):
        self.press(CTRL, WIN)
        try:
            keys.last_injected = time.monotonic()  # our Ctrl+V just went
            self.release()                         # Windows says: Ctrl up
            self.assertEqual(self.app._state, "recording")
            keys.last_injected = 0.0
            self.step()
            self.assertEqual(self.app._state, "idle")
        finally:
            keys.last_injected = 0.0

    def test_no_recording_can_start_while_our_keys_go_out(self):
        with self.app._paste_guard() as ok:
            self.assertTrue(ok)
            self.down |= {CTRL, WIN}
            t = threading.Thread(target=self.step)
            t.start()
            t.join(0.3)
            self.assertTrue(t.is_alive())          # waits for the guard
            self.assertEqual(self.app._state, "idle")
        t.join(5)
        self.assertEqual(self.app._state, "recording")
        with self.app._paste_guard() as ok:
            self.assertFalse(ok)                   # recording: no keys
        self.release()


class MouseOnlyUnchanged(Base):
    hotkey = "mouse4"

    def test_paste_during_a_mouse_recording_as_before(self):
        with self.assertLogs("holdtotalk.app", "WARNING"):
            self.press(keys.VK_XBUTTON1)          # no hook: polled
        self.assertEqual(self.app._state, "recording")
        self.app.submit_transcript("first dictation words")
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))
        self.assertEqual(self.pastes[0][1], "recording")
        self.press(ord("A"))                       # typing never cancels
        self.assertEqual(self.app._state, "recording")
        self.release()


class StrayKeys(Base):
    def test_stray_key_after_the_first_moment_keeps_the_speech(self):
        self.press(CTRL, WIN)
        self.app._started_mono -= 1.0             # a second into it
        self.press(ord("A"))
        self.assertEqual(self.app._state, "recording")
        self.release()
        self.assertTrue(wait_for(lambda: len(self.pastes) == 1))

    def test_shortcut_at_the_start_is_discarded_logged_and_reported(self):
        self.press(CTRL, WIN)
        with self.assertLogs("holdtotalk.app", "INFO") as logs:
            self.press(ord("C"))
        self.assertEqual(self.app._state, "idle")
        self.assertTrue(any("Recording discarded" in m for m in logs.output))
        self.assertEqual([k for k, _ in self.notices], ["discarded"])
        self.assertTrue(self.app._blocked)

    def test_every_discard_is_reported(self):
        self.press(CTRL, WIN)
        self.app.set_paused(True)
        with self.assertLogs("holdtotalk.app", "INFO") as logs:
            self.step()
        self.assertTrue(any("discarded" in m and "paused" in m
                            for m in logs.output))
        self.assertIn("discarded", [k for k, _ in self.notices])


class PauseShortcut(Base):
    pause = "ctrl+alt+p"

    def test_toggles_once_per_press_also_while_paused(self):
        self.press(CTRL, ALT, ord("P"))
        self.assertTrue(self.app.paused)
        self.assertEqual(self.app._flash[0], "paused")
        self.step(5)                               # still held: no repeat
        self.assertTrue(self.app.paused)
        self.release()
        self.press(CTRL, ALT, ord("P"))
        self.assertFalse(self.app.paused)
        self.assertEqual(self.app._flash[0], "resumed")

    def test_not_while_dictating(self):
        self.press(CTRL, WIN)
        self.app._started_mono -= 1.0
        self.press(ALT, ord("P"))                  # ctrl is already down
        self.assertFalse(self.app.paused)
        self.assertEqual(self.app._state, "recording")
        self.release()

    def test_part_of_the_first_moment_counts_as_the_shortcut(self):
        self.press(CTRL, WIN)
        self.press(ALT, ord("P"))
        self.assertTrue(self.app.paused)
        self.assertEqual(self.app._state, "idle")

    def test_feedback_on_the_pill(self):
        self.app.overlay = mock.Mock()
        self.press(CTRL, ALT, ord("P"))
        self.app._draw()
        self.app.overlay.update.assert_called_with("paused", 0.0)
        self.app._flash = ("paused", 0)            # expired
        self.app._draw()
        self.app.overlay.update.assert_called_with("hidden", 0.0)

    def test_other_keys_and_part_of_the_hotkey(self):
        for spec, ok in (("pause", True), ("ctrl+shift+home", True),
                         ("num 5", True), ("volume mute", True),
                         ("ctrl", False), ("windows", False)):
            with self.subTest(spec=spec):
                if ok:
                    self.app._set_pause_hotkey(spec)
                    self.assertTrue(self.app._pause_groups)
                else:
                    with self.assertRaises(ValueError):
                        self.app._set_pause_hotkey(spec)

    def test_reload_applies_live_and_rejects_bad(self):
        d = Path(tempfile.mkdtemp(prefix="htt-flow-"))
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(settings, "CONFIG_PATH", d / "config.json"):
            settings.save_config({"hotkey": "ctrl+windows",
                                  "pause_hotkey": "f8",
                                  "cleanup": {"enabled": False}})
            res = self.app._reload()
            self.assertIn("pause_hotkey", res["applied"])
            self.press(0x77)
            self.assertTrue(self.app.paused)
            self.release()
            settings.save_config({"pause_hotkey": "ctrl"})  # part of hotkey
            res = self.app._reload()
            self.assertTrue(res["errors"])
            self.assertEqual(self.app.cfg["pause_hotkey"], "f8")
            self.press(0x77)
            self.assertFalse(self.app.paused)

    def test_capture_records_new_key_names(self):
        got = []
        self.app.capture_hotkey(got.append)
        self.press(SHIFT)
        self.press(0x21)                           # Page Up
        self.release()
        self.assertEqual(got, ["shift+page up"])
        self.assertFalse(self.app.paused)          # not toggled by capture


class Chimes(Base):
    hotkey = "f9"

    def test_start_is_not_muted_by_default(self):
        self.press(0x78)
        self.assertEqual(self.sounds.played, ["start"])
        self.assertIsNone(self.app.recorder.muted)
        self.release()

    def test_mute_start_false_is_not_muted(self):
        self.app.cfg["sounds"] = dict(self.app.cfg["sounds"], mute_start=False)
        self.press(0x78)
        self.assertEqual(self.sounds.played, ["start"])
        self.assertIsNone(self.app.recorder.muted)
        self.release()

    def test_mute_start_true_but_chimes_off_does_not_mute(self):
        self.app.cfg["sounds"] = dict(self.app.cfg["sounds"], mute_start=True,
                                      enabled=False)
        self.app._chime = lambda kind: False
        self.press(0x78)
        self.assertIsNone(self.app.recorder.muted)
        self.release()

    def test_start_on_press_stop_after_the_tail(self):
        self.app.cfg["sounds"] = dict(self.app.cfg["sounds"], mute_start=True)
        take = types.SimpleNamespace(done=False, timed_out=False,
                                     wait=lambda timeout=None: self.app
                                     .recorder.samples)
        self.app.recorder.stop_async = lambda: take
        self.press(0x78)
        self.assertEqual(self.sounds.played, ["start"])
        chime = App._chime_seconds("start")
        self.assertAlmostEqual(chime, 0.17, places=2)
        self.assertAlmostEqual(self.app.recorder.muted,
                               chime + app_mod.CHIME_MARGIN)
        self.release()
        self.assertEqual(self.sounds.played, ["start"])  # tail still coming
        take.done = True
        self.step()
        self.assertEqual(self.sounds.played, ["start", "stop"])

    def test_preview_and_missing_module(self):
        self.assertTrue(self.app.play_sound("stop"))
        self.assertEqual(self.sounds.played, ["stop"])
        with mock.patch.object(app_mod, "sounds", None):
            self.assertFalse(self.app.play_sound("start"))
            self.press(0x78)                       # still records
            self.assertEqual(self.app._state, "recording")
            self.release()


class DryRunIsSilent(Base):
    hotkey = "f9"
    dry_run = True

    def test_no_chime(self):
        self.press(0x78)
        self.release()
        self.assertEqual(self.sounds.played, [])
        self.assertFalse(self.app.play_sound("start"))


if __name__ == "__main__":
    unittest.main()
