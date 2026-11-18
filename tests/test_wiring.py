"""How the modules fit together: stage order, config defaults and merging,
the Whisper glossary, history, live reload, cleanup start-up rules and the
windowless log. No windows, no Whisper model, no real Ollama, no real files
next to the app."""

import fakes  # noqa: F401  (HOLDTOTALK_HOME -> a temporary folder first)

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

import app as app_mod
import cleanup
import dictate
import pipeline
import settings
import stt
import textrules
import tray

SIG = "Best regards,\nSam\n"


def dead_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def cfg_with(**kw):
    cfg = settings.load_config(Path(tempfile.gettempdir()) / "no-such-cfg.json")
    cfg.update(kw)
    return cfg


class StageOrder(unittest.TestCase):
    def setUp(self):
        self.saved = list(pipeline.STAGES)

    def tearDown(self):
        pipeline.STAGES[:] = self.saved

    def test_order(self):
        self.assertEqual(pipeline.STAGES,
                         [pipeline.normalize, cleanup.clean, textrules.apply])

    def test_llm_cannot_undo_rules(self):
        # A misbehaving "LLM" that rewrites the spelling and the trigger:
        # text rules run after it, so the user's rules still win.
        def fake_llm(text, cfg):
            return text.replace("JSON", "jason")
        pipeline.STAGES[1] = fake_llm
        cfg = cfg_with(dictionary=[{"heard": "jason", "wanted": "JSON"}],
                       snippets=[{"trigger": "my signature", "text": SIG}])
        trace = []
        out = pipeline.run("um, send the jason file. My signature.", cfg,
                           trace)
        self.assertEqual(out, "Send the JSON file. " + SIG)
        self.assertEqual([t[0] for t in trace],
                         ["normalize", "test_wiring.fake_llm",
                          "textrules.apply"])
        self.assertEqual(trace[0][1], "Send the jason file. My signature.")

    def test_real_stages_cleanup_off_or_unreachable(self):
        raw = "Um, send the jason file to the team today. My signature."
        want = "Send the JSON file to the team today. " + SIG
        rules = dict(dictionary=[{"heard": "jason", "wanted": "JSON"}],
                     snippets=[{"trigger": "my signature", "text": SIG}])
        off = cfg_with(cleanup={"enabled": False}, **rules)
        self.assertEqual(pipeline.run(raw, off), want)
        url = dead_url()
        down = cfg_with(cleanup={"url": url, "timeout": 3.0}, **rules)
        started = time.perf_counter()
        self.assertEqual(pipeline.run(raw, down), want)
        self.assertLess(time.perf_counter() - started, 3.5)
        # The next dictation does not pay for the refused connection again.
        started = time.perf_counter()
        self.assertEqual(pipeline.run(raw, down), want)
        self.assertLess(time.perf_counter() - started, 0.5)
        cleanup._down_until.pop(url, None)

    def test_short_utterance_and_snippet_only(self):
        cfg = cfg_with(cleanup={"url": dead_url()},
                       snippets=[{"trigger": "my signature", "text": SIG}])
        started = time.perf_counter()
        self.assertEqual(pipeline.run("Uh, my signature.", cfg), SIG)
        self.assertEqual(pipeline.run("um, yes please", cfg), "Yes please")
        self.assertLess(time.perf_counter() - started, 0.5)   # no request

    def test_text_to_paste(self):
        on, off = {"trailing_space": True}, {"trailing_space": False}
        self.assertEqual(pipeline.text_to_paste("Hi.", on), "Hi. ")
        self.assertEqual(pipeline.text_to_paste("Hi.", off), "Hi.")
        lst = "We need:\n1. Eggs\n2. Milk"
        self.assertEqual(pipeline.text_to_paste(lst, on), lst + " ")
        self.assertEqual(pipeline.text_to_paste(SIG, on), SIG)   # no " \n"
        self.assertEqual(pipeline.text_to_paste("", on), "")

    def test_multiline_llm_output_survives_textrules(self):
        def fake_llm(text, cfg):
            return "For the trip we need:\n1. Passports\n2. Chargers"
        pipeline.STAGES[1] = fake_llm
        cfg = cfg_with(textrules={"formatting_commands": True})
        out = pipeline.run("for the trip we need first passports second "
                           "chargers", cfg)
        self.assertEqual(out, "For the trip we need:\n1. Passports\n"
                              "2. Chargers")


class ConfigDefaults(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-cfg-"))
        self.path = self.dir / "config.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def data(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_new_keys_have_defaults(self):
        cfg = settings.load_config(self.path)
        self.assertEqual(cfg["dictionary"], [])
        self.assertEqual(cfg["snippets"], [])
        self.assertEqual(cfg["cleanup"], cleanup.DEFAULTS)
        self.assertEqual(cfg["textrules"], textrules.DEFAULTS)
        self.assertEqual(cleanup.DEFAULTS["timeout"], 3.0)

    def test_partial_section_gets_the_other_defaults(self):
        self.path.write_text(json.dumps({
            "cleanup": {"model": "other:1b"},
            "textrules": {"history_limit": 9},
            "replacements": {"a": "b"}}), encoding="utf-8")
        cfg = settings.load_config(self.path)
        self.assertEqual(cfg["cleanup"],
                         dict(cleanup.DEFAULTS, model="other:1b"))
        self.assertEqual(cfg["textrules"],
                         dict(textrules.DEFAULTS, history_limit=9))
        self.assertEqual(cfg["replacements"], {"a": "b"})
        cfg["cleanup"]["enabled"] = False                 # not shared
        self.assertTrue(settings.DEFAULTS["cleanup"]["enabled"])

    def test_bad_section_value_is_kept_but_harmless(self):
        self.path.write_text(json.dumps({"cleanup": "nonsense"}),
                             encoding="utf-8")
        cfg = settings.load_config(self.path)
        self.assertEqual(cleanup.merged_settings(cfg), cleanup.DEFAULTS)

    def test_partial_section_save_keeps_keys(self):
        self.path.write_text(json.dumps({
            "cleanup": {"model": "m", "extra": [1]},
            "replacements": {"x": "y", "old": "gone"}}), encoding="utf-8")
        settings.save_config({"cleanup": {"enabled": False},
                              "replacements": {"x": "y"}}, self.path)
        data = self.data()
        self.assertEqual(data["cleanup"],
                         {"model": "m", "extra": [1], "enabled": False})
        self.assertEqual(data["replacements"], {"x": "y"})   # data: replaced

    def test_full_round_trip(self):
        cfg = settings.load_config(self.path)
        cfg["textrules"]["snippet_mode"] = "whole"
        settings.save_config(cfg, self.path)
        self.assertEqual(settings.load_config(self.path), cfg)

    def test_tray_save_does_not_freeze_defaults_into_file(self):
        self.path.write_text("{}", encoding="utf-8")

        class Fake:
            cfg = {}

            def reload_config(self):
                return {"applied": ["cleanup"], "restart_required": [],
                        "errors": []}
        settings.apply_changes(Fake(), {"cleanup": {"enabled": False},
                                    "textrules": {"formatting_commands": True}},
                           self.path)
        self.assertEqual(self.data(), {"cleanup": {"enabled": False},
                                       "textrules": {"formatting_commands":
                                                     True}})


class Glossary(unittest.TestCase):
    def test_cap_keeps_prefix_and_first_terms(self):
        terms = [f"Term{i:03d}" for i in range(500)]
        prompt = stt.glossary_prompt(terms)
        self.assertTrue(prompt.startswith("Glossary: Term000, Term001"))
        self.assertTrue(prompt.endswith("."))
        self.assertLessEqual(stt.estimate_tokens(prompt),
                             stt.PROMPT_MAX_TOKENS)
        self.assertLess(prompt.count("Term"), 500)
        self.assertIsNone(stt.glossary_prompt([]))
        words = lambda t: len(t.split())                       # noqa: E731
        self.assertEqual(stt.glossary_prompt(["a", "b", "c"], words, 3),
                         "Glossary: a, b.")

    def test_transcribe_uses_vocabulary_terms(self):
        seen = {}

        class FakeModel:
            def transcribe(self, audio, **kw):
                seen.update(kw)
                return iter(()), None
        engine = stt.Engine.__new__(stt.Engine)
        engine.model = FakeModel()
        engine.cfg = cfg_with(
            vocabulary=["Kubernetes"],
            dictionary=[{"heard": "jason", "wanted": "JSON"},
                        {"heard": "addr", "wanted": "line1\nline2"}])
        engine.transcribe([0.0] * 160)
        self.assertEqual(seen["initial_prompt"], "Glossary: Kubernetes, JSON.")
        engine.cfg = cfg_with()
        engine.transcribe([0.0] * 160)
        self.assertIsNone(seen["initial_prompt"])


class FakeApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self.on_result = []
        self.on_dictation = []


def dictation(raw, final, seconds, ts=None):
    return {"raw": raw, "final": final, "audio_seconds": seconds,
            "ts": time.time() if ts is None else ts}


class HistoryHook(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-hook-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_attach_history_records_dictations_with_live_cfg(self):
        app = FakeApp(cfg_with())
        history = dictate.attach_history(app, self.dir / "h.sqlite3")
        try:
            self.assertEqual(len(app.on_dictation), 1)
            ts = 1_700_000_000.25
            for fn in app.on_dictation:
                fn(dictation("um send the jason file", "Send the JSON file.",
                             1.5, ts))
            app.cfg = cfg_with(save_history=False)     # reload replaces cfg
            for fn in app.on_dictation:
                fn(dictation("secret", "Secret.", 1.0))
            rows = history.recent()
            self.assertEqual([(r["raw"], r["final"], r["seconds"])
                              for r in rows],
                             [("um send the jason file",
                               "Send the JSON file.", 1.5)])
            # The window finds the row by the dictation's own timestamp.
            self.assertEqual(history.at(ts)["id"], rows[0]["id"])
            self.assertIsNone(history.at(ts + 1))
        finally:
            history.close()


class LiveReload(unittest.TestCase):
    """App._reload on a bare App (no Tk): text settings apply live."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-reload-"))
        self.saved_path = settings.CONFIG_PATH
        settings.CONFIG_PATH = self.dir / "config.json"
        settings.CONFIG_PATH.write_text("{}", encoding="utf-8")

    def tearDown(self):
        settings.CONFIG_PATH = self.saved_path
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_text_settings_apply_without_errors_or_restart(self):
        app = app_mod.App.__new__(app_mod.App)
        app.cfg = settings.load_config()
        app.engine = None                     # still loading: must not crash
        calls = []
        app.on_reload = [lambda old, new, res: calls.append(res)]
        settings.save_config({
            "dictionary": [{"heard": "jason", "wanted": "JSON"}],
            "snippets": [{"trigger": "sig", "text": "S"}],
            "vocabulary": ["Kubernetes"],
            "cleanup": {"enabled": False},
            "textrules": {"formatting_commands": True},
            "trailing_space": False})
        result = app._reload()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["restart_required"], [])
        self.assertEqual(sorted(result["applied"]),
                         ["cleanup", "dictionary", "snippets", "textrules",
                          "trailing_space", "vocabulary"])
        self.assertFalse(app.cfg["cleanup"]["enabled"])
        self.assertEqual(app.cfg["cleanup"]["model"],
                         cleanup.DEFAULTS["model"])      # defaults kept
        self.assertEqual(calls, [result])
        self.assertEqual(pipeline.run("Sig.", app.cfg), "S")


class CleanupStartup(unittest.TestCase):
    def test_only_the_default_local_server_is_started(self):
        self.assertTrue(cleanup._is_default_local_url("http://127.0.0.1:11434"))
        self.assertTrue(cleanup._is_default_local_url("http://localhost:11434/"))
        self.assertFalse(cleanup._is_default_local_url("http://127.0.0.1:9"))
        self.assertFalse(cleanup._is_default_local_url("http://10.0.0.2:11434"))
        self.assertFalse(cleanup._is_default_local_url("not a url"))

    def test_unreachable_nondefault_url_does_not_start_anything(self):
        started = []
        saved = cleanup.start_server
        cleanup.start_server = started.append
        try:
            self.assertFalse(cleanup.ensure_server(
                {"cleanup": {"url": dead_url()}}, wait=0.1))
        finally:
            cleanup.start_server = saved
        self.assertEqual(started, [])

    def test_prepare_never_raises_and_disabled_does_nothing(self):
        with self.assertLogs("holdtotalk.cleanup", "ERROR"):
            t = cleanup.prepare_in_background(lambda: 1 / 0)
            t.join(5)
        self.assertFalse(t.is_alive())
        t = cleanup.prepare_in_background(lambda: {"cleanup": {"enabled":
                                                               False}})
        t.join(5)
        self.assertFalse(t.is_alive())


class Windowless(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="htt-log-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_capped_log_rotates(self):
        log = dictate.CappedLog(self.dir / "d.log", max_bytes=100)
        for i in range(30):
            log.write(f"line {i:02d} " + "x" * 10 + "\n")
        log.flush()
        log._file.close()
        self.assertLessEqual((self.dir / "d.log").stat().st_size, 130)
        self.assertTrue((self.dir / "d.log.1").exists())
        self.assertIn("line 29", (self.dir / "d.log").read_text())

    def test_none_streams_are_redirected(self):
        saved = sys.stdout, sys.stderr, dict(os.environ)
        sys.stdout = sys.stderr = None
        try:
            self.assertTrue(dictate._prepare_streams(self.dir / "w.log"))
            print("printed under pythonw")
            sys.stderr.write("stderr too\n")
            import tqdm
            for _ in tqdm.tqdm(range(3)):    # a progress bar must not crash
                pass
            stream = sys.stdout
        finally:
            sys.stdout, sys.stderr = saved[0], saved[1]
            os.environ.clear()
            os.environ.update(saved[2])
        stream._file.close()
        text = (self.dir / "w.log").read_text(encoding="utf-8")
        self.assertIn("printed under pythonw", text)
        self.assertIn("stderr too", text)
        self.assertFalse(dictate._prepare_streams(self.dir / "x.log"))


if __name__ == "__main__":
    unittest.main()
