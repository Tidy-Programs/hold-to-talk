"""E7 transcription failures recover the engine, E8 a failed model load
reaches 'failed' and retry_engine() works, E9 dictated text never reaches
the log, E10 the hotkey loop survives and its errors are throttled. Fake
engines only (no Whisper)."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import app as app_mod
import keys
import settings
from app import App

SECRET = "Quillon marmalade zephyr"


def cfg_with(**kw):
    cfg = settings.load_config(Path(tempfile.gettempdir()) / "no-such.json")
    cfg["cleanup"] = dict(cfg["cleanup"], enabled=False)
    cfg.update(min_seconds=0.1, hotkey="f9", **kw)
    return cfg


def bare(engine=None, dry_run=True):
    a = App.__new__(App)
    a._init_state(cfg_with(), engine=engine, dry_run=dry_run)
    a.notices = []
    a.on_notice.append(lambda k, m: a.notices.append((k, m)))
    a.results = []
    a.on_result.append(lambda raw, final, s: a.results.append(final))
    return a


def drain(a):
    """Run what other threads queued for the Tk thread."""
    while not a._calls.empty():
        fn = a._calls.get()
        try:
            fn()
        except Exception as exc:          # the old code's NameError lands here
            a.notices.append(("drain-error", type(exc).__name__))


class Engine:
    """Fails `fails` times (shared across instances via a dict)."""

    def __init__(self, cfg=None, plan=None, device="cuda"):
        self.plan = plan if plan is not None else {"fails": 0, "seen": []}
        self.device = device
        self.cfg = cfg

    def transcribe(self, samples):
        self.plan["seen"].append(samples)
        if self.plan["fails"] > 0:
            self.plan["fails"] -= 1
            raise RuntimeError("CUDA failed: synthetic")
        return SECRET


class GpuEngine:
    """Always fails on the GPU, works on the processor."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.device = "cpu" if cfg.get("device") == "cpu" else "cuda"
        self.fallback_reason = ("asked for after GPU errors"
                                if self.device == "cpu" else None)

    def transcribe(self, samples):
        if self.device != "cpu":
            raise RuntimeError("CUDA error: an illegal memory access")
        return SECRET


AUDIO = np.full(16000, 0.1, dtype=np.float32)


class Recovery(unittest.TestCase):
    def setUp(self):
        for target in ("wave.open", "numpy.save"):
            p = mock.patch(target, side_effect=AssertionError("disk"))
            p.start()
            self.addCleanup(p.stop)

    def run_job(self, a):
        with self.assertLogs("holdtotalk.app", "INFO") as logs:
            a._process(("audio", AUDIO))
        drain(a)
        return logs.output

    def test_one_failure_is_retried_with_the_same_audio(self):
        plan = {"fails": 1, "seen": []}
        a = bare(Engine(plan=plan))
        factory = mock.Mock()
        a._factory = factory
        self.run_job(a)
        self.assertEqual(len(a.results), 1)
        self.assertTrue(all(s is AUDIO for s in plan["seen"]))
        factory.assert_not_called()

    def test_repeated_failures_reload_the_engine(self):
        plan = {"fails": 2, "seen": []}
        a = bare(Engine(plan=plan))
        a._factory = mock.Mock(side_effect=lambda: Engine(plan=plan))
        statuses = []
        a.on_status.append(lambda s, d: statuses.append(s))
        self.run_job(a)
        a._factory.assert_called_once()
        self.assertEqual(statuses, ["loading", "ready"])
        self.assertEqual(len(a.results), 1)
        self.assertEqual(len(plan["seen"]), 3)
        self.assertIn("engine", [k for k, _ in a.notices])

    def test_gpu_that_keeps_failing_falls_back_to_the_processor(self):
        cfg = cfg_with(device="auto")
        a = bare(GpuEngine(cfg))
        a._factory = lambda: GpuEngine(a.cfg)
        self.run_job(a)
        self.assertEqual(a.engine.device, "cpu")
        self.assertEqual(a.status, "ready")
        self.assertIn("processor", a.status_detail)
        self.assertEqual(len(a.results), 1)

    def test_giving_up_shows_failed_and_retry_recovers(self):
        plan = {"fails": 99, "seen": []}
        a = bare(Engine(plan=plan, device="cpu"))
        a._factory = lambda: Engine(plan=plan, device="cpu")
        with self.assertLogs("holdtotalk.app", "ERROR"):
            a._process(("audio", AUDIO))
        drain(a)
        self.assertEqual(a.status, "failed")
        self.assertIn("keeps failing", a.status_detail)
        self.assertIn("error", [k for k, _ in a.notices])
        self.assertEqual(a.results, [])
        plan["fails"] = 0
        self.assertTrue(a.retry_engine())
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")


class LoadFailure(unittest.TestCase):
    def test_failed_load_reaches_failed_then_retry_works(self):
        a = bare(engine=None)
        self.assertEqual(a.status, "loading")
        attempts = []

        def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("model files are missing")
            return Engine()
        with self.assertLogs("holdtotalk.app", "ERROR"):
            a.load_engine(factory)
            a.loader_thread.join(5)
        drain(a)
        self.assertNotIn("drain-error", [k for k, _ in a.notices])
        self.assertEqual(a.status, "failed")
        self.assertIn("model files are missing", a.status_detail)
        self.assertTrue(a.retry_engine())
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")
        self.assertFalse(a.retry_engine())        # nothing to retry now

    def test_retry_is_safe_from_another_thread(self):
        a = bare(engine=None)
        self.assertFalse(a.retry_engine())        # no factory yet
        import threading
        a._factory = lambda: Engine()
        a.status = "failed"
        t = threading.Thread(target=a.retry_engine)
        t.start()
        t.join(5)
        a.loader_thread.join(5)
        drain(a)
        self.assertEqual(a.status, "ready")


class NoTextInTheLog(unittest.TestCase):
    def capture(self, a, job):
        handler_logs = []

        class H(logging.Handler):
            def emit(self, record):
                handler_logs.append(logging.Formatter(
                    "%(message)s").format(record))
        h = H(logging.DEBUG)
        root = logging.getLogger()
        old = root.level
        root.addHandler(h)
        root.setLevel(logging.DEBUG)
        try:
            a._process(job)
            drain(a)
        finally:
            root.removeHandler(h)
            root.setLevel(old)
        return "\n".join(handler_logs)

    def words(self, text):
        return [w for w in SECRET.lower().split() if w in text.lower()]

    def test_dry_run_dictation(self):
        a = bare(Engine())
        out = self.capture(a, ("audio", AUDIO))
        self.assertEqual(len(a.results), 1)
        self.assertIn("3 words", out)
        self.assertEqual(self.words(out), [])

    def test_failed_paste(self):
        a = bare(Engine(), dry_run=False)
        with mock.patch.object(keys, "paste_text", side_effect=RuntimeError(
                f"cannot paste {SECRET}")), \
                mock.patch.object(keys, "key_down", return_value=False):
            out = self.capture(a, ("audio", AUDIO))
        self.assertIn("Paste failed", out)
        self.assertEqual(self.words(out), [])
        self.assertEqual(len(a.results), 1)       # still reaches history

    def test_nothing_left_after_cleanup(self):
        a = bare(Engine())
        out = self.capture(a, ("text", "um, uh", 1.0))
        self.assertNotIn("uh", out.replace("uh-", ""))
        out = self.capture(a, ("text", f"um {SECRET}", 1.0))
        self.assertEqual(self.words(out), [])

    def test_developer_flag_logs_text(self):
        a = bare(Engine())
        a.log_text = True
        out = self.capture(a, ("audio", AUDIO))
        self.assertTrue(self.words(out))


class TickLoop(unittest.TestCase):
    def test_step_error_still_draws_and_is_throttled(self):
        a = bare(Engine())
        a.root = mock.Mock()
        a.overlay = mock.Mock()
        a._step = mock.Mock(side_effect=RuntimeError("synthetic step bug"))
        with self.assertLogs("holdtotalk.app", "ERROR") as logs:
            for _ in range(50):
                a._tick()
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(a.overlay.update.call_count, 50)   # still drawn
        self.assertEqual(a.root.after.call_count, 50)
        a._tick_error_at -= app_mod.TICK_ERROR_LOG_SECONDS
        with self.assertLogs("holdtotalk.app", "ERROR") as logs:
            a._tick()
            a._tick()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("49 similar errors", logs.output[0])


class Lambdas(unittest.TestCase):
    def test_no_closure_over_an_except_variable(self):
        # E8's bug pattern: a lambda inside `except ... as exc:` using exc.
        import ast
        for mod in ("app", "keys", "audio", "mousehook", "mousehelper",
                    "overlay", "dictate"):
            path = Path(app_mod.__file__).with_name(mod + ".py")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for handler in ast.walk(tree):
                if not isinstance(handler, ast.ExceptHandler) or \
                        not handler.name:
                    continue
                for node in ast.walk(handler):
                    if isinstance(node, (ast.Lambda, ast.FunctionDef)):
                        names = {n.id for n in ast.walk(node)
                                 if isinstance(n, ast.Name)}
                        self.assertNotIn(handler.name, names,
                                         f"{mod}.py line {node.lineno}")


if __name__ == "__main__":
    unittest.main()
