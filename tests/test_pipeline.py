import atexit
import os
import shutil
import tempfile

# Never touch real user data: the data folder is a temporary one (also shared
# with test_00_home.py) before any project module is imported.
if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)


import unittest

import cleanup
import pipeline
from settings import DEFAULTS


class Normalize(unittest.TestCase):
    def n(self, text, **cfg):
        return pipeline.normalize(text, dict(DEFAULTS, **cfg))

    def test_selftest_sentence(self):
        self.assertEqual(
            self.n("Um, this is a quick test of the dictation app. It should "
                   "type exactly what I say, with punctuation."),
            "This is a quick test of the dictation app. It should type "
            "exactly what I say, with punctuation.")

    def test_fillers_mid_sentence(self):
        self.assertEqual(self.n("I, uh, think so"), "I think so")
        self.assertEqual(self.n("so umm we go"), "So we go")
        self.assertEqual(self.n("that's it, um."), "That's it.")
        self.assertEqual(self.n("Hmm... okay"), "Okay")

    def test_words_containing_fillers_survive(self):
        text = "Umbrella, humming, uhuru and hummus."
        self.assertEqual(self.n(text), text)

    def test_fillers_kept_when_disabled(self):
        self.assertEqual(self.n("um, hello", remove_fillers=False),
                         "Um, hello")

    def test_whitespace_and_capital(self):
        self.assertEqual(self.n("  hello   world  ,  again "),
                         "Hello world, again")
        self.assertEqual(self.n(""), "")
        self.assertEqual(self.n("um"), "")
        self.assertEqual(self.n("élan"), "Élan")


class Run(unittest.TestCase):
    def setUp(self):
        self.saved = list(pipeline.STAGES)

    def tearDown(self):
        pipeline.STAGES[:] = self.saved

    def test_default_stages(self):
        self.assertEqual(pipeline.run("um, hi", dict(DEFAULTS)), "Hi")

    def test_stages_run_in_order(self):
        pipeline.STAGES[:] = [lambda t, c: t + "a", lambda t, c: t + "b"]
        self.assertEqual(pipeline.run("x", {}), "xab")

    def test_failing_stage_is_skipped_and_logged(self):
        def boom(text, cfg):
            raise RuntimeError("model offline")
        pipeline.STAGES.append(boom)
        pipeline.STAGES.append(lambda t, c: t + "!")
        with self.assertLogs("holdtotalk.pipeline", "ERROR") as logs:
            out = pipeline.run("um, keep my words", dict(DEFAULTS))
        self.assertEqual(out, "Keep my words!")
        self.assertIn("boom", logs.output[0])

    def test_non_string_result_is_skipped(self):
        pipeline.STAGES[:] = [lambda t, c: None, lambda t, c: t.upper()]
        with self.assertLogs("holdtotalk.pipeline", "ERROR"):
            self.assertEqual(pipeline.run("abc", {}), "ABC")

    def test_every_stage_failing_returns_raw(self):
        pipeline.STAGES[:] = [lambda t, c: 1 / 0]
        with self.assertLogs("holdtotalk.pipeline", "ERROR"):
            self.assertEqual(pipeline.run("raw text", {}), "raw text")


NO_AI = {"cleanup": {"enabled": False}}


class Languages(unittest.TestCase):
    """Item 4: the English filler list deleted real words in other languages."""

    def setUp(self):
        self.saved = list(pipeline.STAGES)
        self.cfg = dict(DEFAULTS, **NO_AI)

    def tearDown(self):
        pipeline.STAGES[:] = self.saved

    def test_german_um_survives_when_the_language_is_given(self):
        text = "Ich komme um drei Uhr"
        self.assertEqual(pipeline.run(text, self.cfg, language="de"), text)
        self.assertEqual(pipeline.normalize(text, self.cfg, language="de"),
                         text)
        self.assertEqual(pipeline.run("Eu chego um pouco tarde", self.cfg,
                                      language="pt"),
                         "Eu chego um pouco tarde")

    def test_english_still_loses_its_fillers(self):
        for lang in ("en", "EN", "en-US", None):
            self.assertEqual(
                pipeline.run("I um think so", self.cfg, language=lang),
                "I think so", lang)

    def test_the_config_language_is_the_fallback(self):
        text = "Ich komme um drei Uhr"
        cfg = dict(self.cfg, language="de")
        self.assertEqual(pipeline.run(text, cfg), text)
        # An explicit language wins over the config's.
        self.assertEqual(pipeline.run("I um think so", cfg, language="en"),
                         "I think so")
        # Unknown everywhere = today's behaviour (English fillers removed).
        self.assertEqual(pipeline.run("so um yes", self.cfg), "So yes")

    def test_stages_get_the_language_only_if_they_accept_it(self):
        seen = {}

        def old(text, cfg):
            seen["old"] = True
            return text

        def new(text, cfg, language=None):
            seen["new"] = language
            return text

        def wide(text, cfg, **kw):
            seen["wide"] = kw
            return text
        pipeline.STAGES[:] = [old, new, wide]
        pipeline.run("x", {}, language="de")
        self.assertEqual(seen, {"old": True, "new": "de",
                                "wide": {"language": "de"}})
        seen.clear()
        pipeline.run("x", {})                    # unknown: nothing passed
        self.assertEqual(seen, {"old": True, "new": None, "wide": {}})

    def test_cleanup_stage_receives_the_language(self):
        calls = []
        orig = cleanup.clean_verbose
        cleanup.clean_verbose = lambda text, cfg, language=None: (
            calls.append(language) or (text, "unchanged"))
        self.addCleanup(setattr, cleanup, "clean_verbose", orig)
        pipeline.run("Ich komme um drei Uhr", dict(DEFAULTS), language="de")
        self.assertEqual(calls, ["de"])


class TraceFailures(unittest.TestCase):
    """Item 5: a failed stage looked like "no change" in the trace."""

    def setUp(self):
        self.saved = list(pipeline.STAGES)

    def tearDown(self):
        pipeline.STAGES[:] = self.saved

    def test_raising_stage_is_marked_failed_without_dictated_text(self):
        def boom(text, cfg):
            raise KeyError("secret sentence about Zorvex")
        pipeline.STAGES[:] = [lambda t, c: t + "!", boom,
                              lambda t, c: t + "?"]
        trace = []
        with self.assertLogs("holdtotalk.pipeline", "ERROR") as lg:
            out = pipeline.run("secret sentence about Zorvex", {}, trace)
        self.assertEqual(out, "secret sentence about Zorvex!?")
        ok1, bad, ok2 = trace
        self.assertFalse(ok1.failed)
        self.assertFalse(ok2.failed)
        self.assertTrue(bad.failed)
        self.assertTrue(bad["failed"])
        self.assertTrue(bad.error.startswith("KeyError"))
        for word in ("secret", "sentence", "Zorvex"):
            self.assertNotIn(word, bad.error)
            self.assertNotIn(word, "\n".join(lg.output))
        self.assertLessEqual(len(bad.error), 150)
        self.assertEqual(bad[1], "secret sentence about Zorvex!")   # unchanged
        self.assertEqual(bad.get("error"), bad.error)
        self.assertNotIn("error", ok1.as_dict())

    def test_entries_still_unpack_as_three_values(self):
        pipeline.STAGES[:] = [lambda t, c: t.upper(), lambda t, c: None]
        trace = []
        with self.assertLogs("holdtotalk.pipeline", "ERROR"):
            pipeline.run("abc", {}, trace)
        for name, text, secs in trace:
            self.assertIsInstance(name, str)
            self.assertIsInstance(secs, float)
        self.assertTrue(trace[1].failed)
        self.assertIn("NoneType", trace[1].error)
        self.assertEqual(trace[1][1], "ABC")

    def test_working_stages_are_not_failed(self):
        trace = []
        pipeline.run("hello there my friend", dict(DEFAULTS, **NO_AI), trace)
        self.assertEqual(len(trace), len(pipeline.STAGES))
        self.assertFalse(any(e.failed for e in trace))

if __name__ == "__main__":
    unittest.main()
