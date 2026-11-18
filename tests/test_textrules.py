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


import ctypes
import glob
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from unittest import mock
from pathlib import Path

import pipeline
import settings
import textrules
from textrules import apply, vocabulary_terms


def cfg_with(**kw):
    cfg = dict(settings.DEFAULTS)
    cfg.update(kw)
    return cfg


def D(*pairs):
    return [{"heard": h, "wanted": w} for h, w in pairs]


def S(*pairs):
    return [{"trigger": t, "text": x} for t, x in pairs]


ADDRESS = "12 Elm Street\nSpringfield 4321"
SIG = "Best regards,\nSam"


class Dictionary(unittest.TestCase):
    def a(self, text, *pairs, **kw):
        return apply(text, cfg_with(dictionary=D(*pairs), **kw))

    def test_basic_and_case_insensitive(self):
        self.assertEqual(self.a("Send the jason file.", ("jason", "JSON")),
                         "Send the JSON file.")
        self.assertEqual(self.a("JASON and Jason", ("jason", "JSON")),
                         "JSON and JSON")

    def test_word_boundaries(self):
        pairs = [("jason", "JSON")]
        self.assertEqual(self.a("I moved to Jasonville.", *pairs),
                         "I moved to Jasonville.")
        self.assertEqual(self.a("Ask Bjason.", *pairs), "Ask Bjason.")
        self.assertEqual(self.a("jason_x jason2", *pairs), "jason_x jason2")
        self.assertEqual(self.a("The jason's keys", *pairs),
                         "The JSON's keys")

    def test_whisper_punctuation_kept_around_match(self):
        pairs = [("jason", "JSON")]
        self.assertEqual(self.a("Jason, please.", *pairs), "JSON, please.")
        self.assertEqual(self.a("Is it jason?", *pairs), "Is it JSON?")
        self.assertEqual(self.a('"Jason" (jason).', *pairs),
                         '"JSON" (JSON).')

    def test_multi_word_with_whisper_separators(self):
        pairs = [("visual studio code", "VS Code")]
        for heard in ("visual studio code", "Visual Studio Code",
                      "Visual, studio code", "visual-studio code",
                      "Visual  Studio\tCode"):
            self.assertEqual(self.a(f"I use {heard} daily.", *pairs),
                             "I use VS Code daily.", heard)
        # A sentence break between the words is not the phrase.
        self.assertEqual(self.a("I like visual. Studio code is", *pairs),
                         "I like visual. Studio code is")
        # Nor is a line break.
        self.assertEqual(self.a("visual studio\ncode", *pairs),
                         "visual studio\ncode")

    def test_hyphenated_heard_matches_spaced_speech(self):
        self.assertEqual(self.a("my e mail and e-mail", ("e-mail", "email")),
                         "my email and email")

    def test_longest_match_first_and_overlaps(self):
        pairs = [("new york", "NY"), ("new york city", "NYC"),
                 ("york city", "YC")]
        self.assertEqual(self.a("New York City and new York.", *pairs),
                         "NYC and NY.")
        # Order in the config does not matter.
        self.assertEqual(self.a("New York City", *reversed(pairs)), "NYC")

    def test_single_pass_no_chaining(self):
        pairs = [("alpha", "beta"), ("beta", "gamma")]
        self.assertEqual(self.a("alpha beta", *pairs), "beta gamma")
        # wanted containing heard does not loop
        self.assertEqual(self.a("react", ("react", "React react")),
                         "React react")

    def test_regex_special_characters(self):
        self.assertEqual(self.a("I write c++ and c#.", ("c++", "C++"),
                                ("c#", "C#")), "I write C++ and C#.")
        self.assertEqual(self.a("use .net now", (".net", ".NET")),
                         "use .NET now")
        self.assertEqual(self.a("a (b) c", ("(b)", "[B]")), "a [B] c")
        self.assertEqual(self.a("cost one dollar", ("one dollar", r"$1 \1 \g<0>")),
                         r"cost $1 \1 \g<0>")
        self.assertEqual(self.a("a.b axb", ("a.b", "AB")), "AB axb")

    def test_apostrophe_variants(self):
        self.assertEqual(self.a("I don\u2019t know", ("don't know", "dunno")),
                         "I dunno")
        self.assertEqual(self.a("I don't know", ("don\u2019t know", "dunno")),
                         "I dunno")

    def test_sentence_start_capital(self):
        pairs = [("gonna", "going to"), ("iphone", "iPhone"),
                 ("npm", "npm")]
        self.assertEqual(self.a("Gonna go. Then gonna stay.", *pairs),
                         "Going to go. Then going to stay.")
        self.assertEqual(self.a("Iphone is here. My iphone.", *pairs),
                         "iPhone is here. My iPhone.")
        self.assertEqual(self.a("Run npm. Npm is fine.", *pairs),
                         "Run npm. Npm is fine.")

    def test_bad_entries_skipped(self):
        cfg = cfg_with(dictionary=[
            None, "jason", 5, [], {}, {"heard": "", "wanted": "X"},
            {"heard": "   ", "wanted": "X"}, {"heard": None, "wanted": "X"},
            {"heard": 3, "wanted": "X"}, {"heard": "ok", "wanted": None},
            {"heard": "ok", "wanted": 7}, {"heard": "ok", "wanted": ""},
            {"heard": "-", "wanted": "dash"}, {"wanted": "X"},
            {"heard": "jason", "wanted": "JSON"}])
        self.assertEqual(apply("ok jason - ok", cfg), "ok JSON - ok")
        for bad in (None, "x", 5, {"a": 1}):
            self.assertEqual(apply("jason", cfg_with(dictionary=bad)),
                             "jason")

    def test_duplicates_first_wins(self):
        self.assertEqual(self.a("jason", ("jason", "JSON"), ("Jason", "Jay")),
                         "JSON")

    def test_legacy_replacements_still_apply(self):
        cfg = cfg_with(replacements={"jason": "JSON", "": "x", "k8s": 5,
                                     "kube": "Kubernetes"})
        self.assertEqual(apply("Jason on kube.", cfg), "JSON on Kubernetes.")
        # The dictionary list wins over the legacy dict for the same phrase.
        cfg["dictionary"] = D(("jason", "Jason Bourne"))
        self.assertEqual(apply("jason", cfg), "Jason Bourne")
        cfg["replacements"] = ["not", "a", "dict"]
        self.assertEqual(apply("jason", cfg), "Jason Bourne")

    def test_non_string_and_bad_cfg(self):
        self.assertEqual(apply("", cfg_with()), "")
        self.assertIsNone(apply(None, cfg_with()))
        self.assertEqual(apply("jason", None), "jason")
        self.assertEqual(apply("jason", {"textrules": "bad"}), "jason")
        self.assertEqual(apply("jason", {"textrules": {
            "snippet_mode": 5, "history_limit": "x",
            "formatting_commands": "yes"}}), "jason")


class Snippets(unittest.TestCase):
    def a(self, text, mode=None, fmt=False, **kw):
        cfg = cfg_with(snippets=S(("my address", ADDRESS),
                                  ("my signature", SIG)), **kw)
        cfg["textrules"] = {"formatting_commands": fmt}
        if mode:
            cfg["textrules"]["snippet_mode"] = mode
        return apply(text, cfg)

    def test_trigger_variants_whisper_renders(self):
        for raw in ("My address.", "my address,", "My address", "MY ADDRESS!",
                    "My, address.", "my address...", "My address?",
                    "My-address."):
            self.assertEqual(self.a(raw), ADDRESS, raw)
        # surrounding whitespace is the caller's (normalize already strips)
        self.assertEqual(self.a("  My address.  "), "  " + ADDRESS + "  ")

    def test_trigger_punctuation_in_config_is_ignored(self):
        cfg = cfg_with(snippets=S(("My address!", ADDRESS)))
        self.assertEqual(apply("my address.", cfg), ADDRESS)

    def test_sentence_mode(self):
        self.assertEqual(self.a("Thanks for your help. My signature."),
                         "Thanks for your help. " + SIG)
        self.assertEqual(self.a("My address. Thanks!"),
                         ADDRESS + " Thanks!")
        self.assertEqual(self.a("My address. My signature."),
                         ADDRESS + " " + SIG)
        # inside a sentence: left alone
        for raw in ("I changed my address last week.",
                    "Send it to my address, please.",
                    "Thanks, my signature.",
                    "Is this my address book?"):
            self.assertEqual(self.a(raw), raw, raw)

    def test_whole_mode(self):
        self.assertEqual(self.a("My address.", "whole"), ADDRESS)
        self.assertEqual(self.a("Thanks. My signature.", "whole"),
                         "Thanks. My signature.")

    def test_anywhere_mode(self):
        self.assertEqual(self.a("Send it to my address, please.", "anywhere"),
                         f"Send it to {ADDRESS}, please.")
        self.assertEqual(self.a("My address.", "anywhere"), ADDRESS)
        self.assertEqual(self.a("Ship to my address.", "anywhere"),
                         f"Ship to {ADDRESS}.")
        # still whole-phrase only
        self.assertEqual(self.a("my addresses", "anywhere"), "my addresses")

    def test_multiline_text_verbatim(self):
        text = "  Line one\n\n\tLine two  \r\nend"
        cfg = cfg_with(snippets=S(("block", text)))
        self.assertEqual(apply("Block.", cfg), text)

    def test_expansion_not_reprocessed(self):
        cfg = cfg_with(
            snippets=S(("sig", "Jason\nnew line, my address. c++"),
                       ("my address", ADDRESS)),
            dictionary=D(("jason", "JSON"), ("c++", "C++")))
        cfg["textrules"] = {"formatting_commands": True}
        self.assertEqual(apply("Sig. Jason.", cfg),
                         "Jason\nnew line, my address. c++ JSON.")
        # replacement-template characters in snippet text are literal
        cfg = cfg_with(snippets=S(("cash", r"$5 \1 \g<0>")))
        self.assertEqual(apply("Cash.", cfg), r"$5 \1 \g<0>")

    def test_private_use_chars_in_input_cannot_fake_placeholders(self):
        cfg = cfg_with(snippets=S(("sig", SIG)))
        self.assertEqual(apply("a\ue000\ue100\ue001b", cfg), "ab")

    def test_longest_trigger_wins(self):
        cfg = cfg_with(snippets=S(("sig", "short"), ("sig long", "long")))
        self.assertEqual(apply("Sig long.", cfg), "long")
        self.assertEqual(apply("Sig.", cfg), "short")

    def test_trigger_before_dictionary(self):
        # A dictionary entry cannot break a trigger.
        cfg = cfg_with(snippets=S(("my address", ADDRESS)),
                       dictionary=D(("address", "ADDR")))
        self.assertEqual(apply("My address.", cfg), ADDRESS)
        self.assertEqual(apply("An address.", cfg), "An ADDR.")

    def test_bad_snippets_skipped(self):
        cfg = cfg_with(snippets=[None, "x", {"trigger": "", "text": "E"},
                                 {"trigger": "!!", "text": "E"},
                                 {"trigger": None, "text": "E"},
                                 {"trigger": "sig", "text": None},
                                 {"trigger": "sig", "text": 5},
                                 {"text": "E"},
                                 {"trigger": "sig", "text": SIG}])
        self.assertEqual(apply("Sig.", cfg), SIG)
        self.assertEqual(apply("Sig.", cfg_with(snippets={"a": 1})), "Sig.")

    def test_many_snippets(self):
        cfg = cfg_with(snippets=S(*[(f"snip {i}", f"<{i}>")
                                    for i in range(300)]))
        raw = " ".join(f"Snip {i}." for i in range(300))
        self.assertEqual(apply(raw, cfg),
                         " ".join(f"<{i}>" for i in range(300)))


class Formatting(unittest.TestCase):
    def a(self, text, on=True, **kw):
        cfg = cfg_with(**kw)
        cfg["textrules"] = {"formatting_commands": on}
        return apply(text, cfg)

    def test_off_by_default(self):
        raw = "Dear John, new line. How are you?"
        self.assertEqual(apply(raw, cfg_with()), raw)
        self.assertFalse(textrules.DEFAULTS["formatting_commands"])

    def test_whisper_style_commands(self):
        self.assertEqual(self.a("Dear John, new line. How are you?"),
                         "Dear John,\nHow are you?")
        self.assertEqual(self.a("Dear John, new line, how are you?"),
                         "Dear John,\nHow are you?")
        self.assertEqual(self.a("Thanks. New paragraph. Bye."),
                         "Thanks.\n\nBye.")
        self.assertEqual(self.a("Thanks, newline. Bye"), "Thanks,\nBye")
        self.assertEqual(self.a("New paragraph. Hello."), "\n\nHello.")
        self.assertEqual(self.a("Hello. New line."), "Hello.\n")
        self.assertEqual(self.a("One. New line, new line. Two."),
                         "One.\n\nTwo.")

    def test_ordinary_speech_untouched(self):
        for raw in ("We launched a new line of products.",
                    "The new paragraph is shorter.",
                    "It is a new line.",
                    "Hello new line how are you",
                    "Draw a newline character"):
            self.assertEqual(self.a(raw), raw, raw)

    def test_after_snippet(self):
        cfg = cfg_with(snippets=S(("my signature", SIG)))
        cfg["textrules"] = {"formatting_commands": True}
        self.assertEqual(apply("My signature. New line. PS.", cfg),
                         SIG + "\nPS.")


class Vocabulary(unittest.TestCase):
    def test_terms(self):
        cfg = cfg_with(vocabulary=["Kubernetes", " JSON ", "", 5, None, "json"],
                       dictionary=D(("jason", "JSON"), ("kube", "kubectl"),
                                    ("addr", "line1\nline2")),
                       replacements={"pie torch": "PyTorch",
                                     "kube": "ignored"})
        self.assertEqual(vocabulary_terms(cfg),
                         ["Kubernetes", "JSON", "kubectl", "PyTorch"])

    def test_missing_and_bad(self):
        self.assertEqual(vocabulary_terms({}), [])
        self.assertEqual(vocabulary_terms(None), [])
        self.assertEqual(vocabulary_terms({"vocabulary": "abc",
                                           "dictionary": "x"}), [])


class Settings(unittest.TestCase):
    def test_layering(self):
        s = textrules.settings_of({"textrules": {"history_limit": 7}})
        self.assertEqual(s["history_limit"], 7)
        self.assertEqual(s["snippet_mode"], "sentence")
        self.assertEqual(textrules.settings_of({}), textrules.DEFAULTS)
        bad = textrules.settings_of({"textrules": {"snippet_mode": "x",
                                                   "history_limit": -3}})
        self.assertEqual(bad["snippet_mode"], "sentence")
        self.assertEqual(bad["history_limit"], 1)


class WithPipeline(unittest.TestCase):
    """apply() as a stage after normalize, on Whisper-style transcripts."""

    def setUp(self):
        self.saved = list(pipeline.STAGES)
        pipeline.STAGES[:] = [pipeline.normalize, textrules.apply]

    def tearDown(self):
        pipeline.STAGES[:] = self.saved

    def test_realistic(self):
        cfg = cfg_with(dictionary=D(("jason", "JSON"),
                                    ("visual studio code", "VS Code")),
                       snippets=S(("my address", ADDRESS),
                                  ("my signature", SIG)))
        self.assertEqual(
            pipeline.run("Um, this is a quick test of the dictation app. It "
                         "should type exactly what I say, with punctuation.",
                         cfg),
            "This is a quick test of the dictation app. It should type "
            "exactly what I say, with punctuation.")
        self.assertEqual(
            pipeline.run("Please send the Jason file to my colleague. "
                         "My address.", cfg),
            "Please send the JSON file to my colleague. " + ADDRESS)
        self.assertEqual(
            pipeline.run("I moved to Jasonville last year, and I use Visual "
                         "Studio Code every day.", cfg),
            "I moved to Jasonville last year, and I use VS Code every day.")
        self.assertEqual(pipeline.run("Uh, my signature.", cfg), SIG)


# -- history ------------------------------------------------------------------

class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="htt-hist-")
        self.path = Path(self.dir) / "h.sqlite3"
        self.opened = []
        # Reads normally wait at most 0.25 s for queued writes (Tk thread);
        # tests wait longer so a slow disk cannot make them flaky.
        self.saved_wait = textrules.READ_FLUSH_WAIT
        textrules.READ_FLUSH_WAIT = 5.0

    def tearDown(self):
        textrules.READ_FLUSH_WAIT = self.saved_wait
        for h in self.opened:
            h.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def H(self, cfg=None, **kw):
        h = textrules.History(self.path, cfg=cfg, **kw)
        self.opened.append(h)
        return h


class HistoryErasure(HistoryBase):
    """Item 13: deleted dictations must not stay recoverable on disk."""

    A = "Zorvex quokka telescope marmalade alpha"
    B = "Zorvex pelican lighthouse cinnamon bravo"

    def on_disk(self, phrase):
        """Files next to the database (db, -wal, -shm, ...) containing it."""
        needle = phrase.encode("utf-8")
        return [p.name for p in Path(self.dir).iterdir()
                if p.is_file() and needle in p.read_bytes()]

    def test_secure_delete_is_on(self):
        h = self.H()
        h.record("a", "b")
        self.assertTrue(h.flush())
        self.assertEqual(h._connect().execute(
            "PRAGMA secure_delete").fetchone()[0], 1)

    def test_clear_leaves_no_trace_in_any_database_file(self):
        h = self.H()
        h.record("raw " + self.A, "final " + self.A)
        self.assertTrue(h.flush())
        row = h.recent(1)[0]
        h.correct(row["id"], "corrected " + self.B)
        self.assertTrue(self.on_disk(self.A))          # it really was written
        self.assertTrue(self.on_disk(self.B))
        self.assertTrue(h.clear())
        self.assertEqual(self.on_disk(self.A), [])
        self.assertEqual(self.on_disk(self.B), [])
        wal = Path(str(self.path) + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
        self.assertEqual(h.count(), 0)
        h.record("again", "works afterwards")           # still usable
        self.assertTrue(h.flush())
        self.assertEqual(h.count(), 1)

    def test_delete_one_removes_only_that_text(self):
        h = self.H()
        h.record("raw " + self.A, "final " + self.A)
        h.record("raw " + self.B, "final " + self.B)
        self.assertTrue(h.flush())
        gone = next(r for r in h.recent(5) if self.A in r["final"])
        self.assertTrue(h.delete(gone["id"]))
        self.assertEqual(self.on_disk(self.A), [])
        self.assertTrue(self.on_disk(self.B))
        self.assertEqual(h.count(), 1)

    def test_rows_dropped_by_the_retention_limit_are_erased_too(self):
        h = self.H(cfg={"textrules": {"history_limit": 1}})
        h.record("raw " + self.A, "final " + self.A)
        self.assertTrue(h.flush())
        h.record("raw " + self.B, "final " + self.B)
        self.assertTrue(h.flush())
        self.assertEqual(h.count(), 1)
        self.assertEqual(self.on_disk(self.A), [])
        self.assertTrue(self.on_disk(self.B))

class HistoryCorrectionsLeaveNoTrace(HistoryBase):
    """L2: replaced or undone correction text, a reader holding the log,
    and the damaged-file copies a repair leaves behind."""

    A = "Zibblequant orchid lantern parsnip alpha"
    B1 = "Zibblequant willow harbour saffron bravo"
    B2 = "Zibblequant meadow compass nutmeg charlie"

    def on_disk(self, phrase):
        needle = phrase.encode("utf-8")
        return sorted(p.name for p in Path(self.dir).iterdir()
                      if p.is_file() and needle in p.read_bytes())

    def one_row(self, h):
        h.record("raw " + self.A, "final " + self.A)
        self.assertTrue(h.flush())
        return h.recent(1)[0]["id"]

    def test_a_replaced_correction_is_gone_from_every_file(self):
        h = self.H()
        row = self.one_row(h)
        h.correct(row, self.B1)
        h.correct(row, self.B2)
        self.assertEqual(self.on_disk(self.B1), [])
        self.assertTrue(self.on_disk(self.B2))          # really stored
        wal = Path(str(self.path) + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_an_undone_correction_is_gone_from_every_file(self):
        h = self.H()
        row = self.one_row(h)
        h.correct(row, self.B1)
        self.assertTrue(h.uncorrect(row))
        self.assertEqual(self.on_disk(self.B1), [])
        self.assertTrue(self.on_disk(self.A))           # the dictation stays
        self.assertFalse(h.uncorrect(row))              # nothing to undo

    def test_a_correction_equal_to_the_text_removes_it_cleanly(self):
        h = self.H()
        row = self.one_row(h)
        h.correct(row, self.B1)
        h.correct(row, "final " + self.A)               # back to what it was
        self.assertEqual(self.on_disk(self.B1), [])

    def test_a_reader_holding_the_log_is_reported_and_retried(self):
        h = self.H(busy_timeout=0.05)
        row = self.one_row(h)
        h.correct(row, self.B1)
        reader = sqlite3.connect(str(self.path), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM history").fetchall()
        try:
            with self.assertLogs("holdtotalk.textrules", "WARNING") as logs:
                h.correct(row, self.B2)             # replaces B1
            self.assertIn("could not be emptied", "\n".join(logs.output))
            self.assertNotIn(self.B1, "\n".join(logs.output))
            self.assertTrue(self.on_disk(self.B1))      # stays until then
        finally:
            reader.execute("ROLLBACK")
            reader.close()
        self.assertTrue(textrules.History._checkpoint(h._connect()))
        self.assertEqual(self.on_disk(self.B1), [])
        self.assertTrue(self.on_disk(self.B2))

    def test_the_checkpoint_is_retried_until_the_reader_goes(self):
        h = self.H(busy_timeout=0.05)
        row = self.one_row(h)
        h.correct(row, self.B1)
        reader = sqlite3.connect(str(self.path), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM history").fetchall()
        with self.assertLogs("holdtotalk.textrules", "WARNING"):
            h.correct(row, self.B2)
        self.assertTrue(self.on_disk(self.B1))
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            reader.execute("ROLLBACK")                  # gone after one wait
            reader.close()
        self.assertTrue(textrules.History._checkpoint(
            h._connect(), tries=3, pause=0.25, sleep=sleep))
        self.assertEqual(sleeps, [0.25])
        self.assertEqual(self.on_disk(self.B1), [])

    def test_a_checkpoint_that_cannot_finish_says_so_once(self):
        h = self.H(busy_timeout=0.05)
        self.one_row(h)
        reader = sqlite3.connect(str(self.path), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM history").fetchall()
        h.record("more", "rows")                        # a frame past the reader
        self.assertTrue(h.flush())
        try:
            with self.assertLogs("holdtotalk.textrules", "WARNING") as logs:
                done = textrules.History._checkpoint(
                    h._connect(), tries=3, pause=0, sleep=lambda s: None)
            self.assertFalse(done)
            self.assertEqual(len(logs.output), 1)
        finally:
            reader.close()

    def test_clear_removes_the_damaged_file_copies_too(self):
        h = self.H()
        h.record("raw " + self.A, "final " + self.A)
        self.assertTrue(h.flush())
        copy_a = Path(self.dir) / (self.path.name + ".corrupt-20260101-000000")
        copy_b = Path(self.dir) / (self.path.name + ".corrupt-20260102-000000")
        other = Path(self.dir) / "notes.txt"
        for p in (copy_a, copy_b):
            p.write_bytes(b"old " + self.A.encode() + b" bytes")
        other.write_text("keep me", encoding="utf-8")
        self.assertTrue(self.on_disk(self.A))
        result = h.clear()
        self.assertTrue(result)
        self.assertEqual(result.removed, [copy_a.name, copy_b.name])
        self.assertEqual(result.failed, [])
        self.assertEqual(self.on_disk(self.A), [])      # every file
        self.assertTrue(other.exists())                 # only its own copies

    def test_clear_without_copies_still_works(self):
        h = self.H()
        h.record("a", "b")
        self.assertTrue(h.flush())
        result = h.clear()
        self.assertTrue(result)
        self.assertEqual(result.removed, [])
        self.assertEqual(h.count(), 0)

    def test_a_copy_that_cannot_be_deleted_is_reported(self):
        h = self.H()
        h.record("a", "b")
        self.assertTrue(h.flush())
        stuck = Path(self.dir) / (self.path.name + ".corrupt-20260103-000000")
        stuck.write_bytes(b"x")
        real = os.remove

        def refuse(path, *a, **k):
            if str(path).endswith(stuck.name):
                raise PermissionError("in use")
            return real(path, *a, **k)
        with mock.patch.object(os, "remove", refuse), \
                self.assertLogs("holdtotalk.textrules", "WARNING"):
            result = h.clear()
        self.assertTrue(result)                         # the history is clear
        self.assertEqual((result.removed, result.failed), ([], [stuck.name]))

class HistoryBasics(HistoryBase):
    def test_default_path_is_in_the_data_folder(self):
        self.assertEqual(textrules.default_history_path(),
                         settings.HISTORY_PATH)
        self.assertNotEqual(textrules.default_history_path().parent,
                            settings.APP_DIR)

    def test_record_recent_search_delete_clear(self):
        h = self.H()
        before = time.time()
        h.record("um hello jason", "Hello JSON", 1.25)
        h.record("second", "Second one.", 2)
        h.record("100% sure_thing", "100% sure_thing", None)
        self.assertTrue(h.flush())
        rows = h.recent(10)
        self.assertEqual([r["final"] for r in rows],
                         ["100% sure_thing", "Second one.", "Hello JSON"])
        self.assertGreaterEqual(rows[-1]["ts"], before - 1)
        self.assertLessEqual(rows[0]["ts"], time.time() + 1)
        self.assertEqual(rows[-1]["seconds"], 1.25)
        self.assertEqual(rows[-1]["raw"], "um hello jason")
        self.assertEqual(len(h.recent(2)), 2)
        self.assertEqual([r["final"] for r in h.search("json")],
                         ["Hello JSON"])
        self.assertEqual([r["final"] for r in h.search("JASON")],
                         ["Hello JSON"])          # matches raw text too
        self.assertEqual([r["final"] for r in h.search("%")],
                         ["100% sure_thing"])     # LIKE wildcards escaped
        self.assertEqual([r["final"] for r in h.search("e_t")],
                         ["100% sure_thing"])
        self.assertEqual(h.search("e_o"), [])
        self.assertEqual(len(h.search("")), 3)
        self.assertTrue(h.delete(rows[1]["id"]))
        self.assertFalse(h.delete(rows[1]["id"]))
        self.assertFalse(h.delete("nope"))
        self.assertEqual(h.count(), 2)
        self.assertTrue(h.clear())
        self.assertEqual(h.recent(), [])
        # ids are never reused after delete/clear
        h.record("x", "x", 1)
        self.assertGreater(h.recent()[0]["id"], rows[0]["id"])

    def test_persists_across_instances(self):
        h = self.H()
        h.record("a", "A", 1)
        h.close()
        self.assertEqual([r["final"] for r in self.H().recent()], ["A"])

    def test_odd_values(self):
        h = self.H()
        h.record(None, 123, "not a number")
        h.record("r", "multi\nline \u00e9\U0001F600", float("1e3"))
        rows = h.recent()
        self.assertEqual(rows[1]["final"], "123")
        self.assertIsNone(rows[1]["seconds"])
        self.assertEqual(rows[0]["final"], "multi\nline \u00e9\U0001F600")

    def test_retention_cap(self):
        h = self.H(cfg={"textrules": {"history_limit": 5}})
        for i in range(12):
            h.record(str(i), str(i), 1)
        self.assertEqual([r["final"] for r in h.recent(100)],
                         ["11", "10", "9", "8", "7"])

    def test_save_history_false(self):
        cfg = {"save_history": False}
        h = self.H(cfg=lambda: cfg)
        h.record("a", "A", 1)
        self.assertEqual(h.count(), 0)
        self.assertFalse(h.enabled)
        cfg["save_history"] = True                 # live config is re-read
        h.record("b", "B", 1)
        self.assertEqual([r["final"] for r in h.recent()], ["B"])

    def test_record_is_on_record_signature(self):
        h = self.H()
        callbacks = [h.record]
        for fn in callbacks:
            fn("raw", "final", 0.5)
        self.assertEqual(h.recent()[0]["final"], "final")

    def test_broken_cfg_callable(self):
        h = self.H(cfg=lambda: 1 / 0)
        with self.assertLogs("holdtotalk.textrules", "ERROR"):
            h.record("a", "A", 1)
        self.assertEqual(h.count(), 1)

    def test_after_close_is_harmless(self):
        h = self.H()
        h.close()
        h.record("a", "A", 1)
        self.assertEqual(h.recent(), [])
        self.assertFalse(h.clear())


class HistoryConcurrency(HistoryBase):
    def test_concurrent_writers_threads_and_two_instances(self):
        a, b = self.H(), self.H()
        errors = []

        def write(h, tag):
            try:
                for i in range(50):
                    h.record(f"{tag}{i}", f"{tag}-{i}", i)
                    if i % 10 == 0:
                        h.recent(3)          # readers interleaved
            except Exception as exc:         # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=write, args=(h, t))
                   for h, t in ((a, "a"), (a, "b"), (a, "c"), (b, "d"),
                                (b, "e"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(a.flush(10) and b.flush(10))
        self.assertEqual(errors, [])
        finals = {r["final"] for r in a.recent(1000)}
        self.assertEqual(len(finals), 250)
        self.assertIn("e-49", finals)

    def test_record_returns_immediately_while_locked(self):
        # busy_timeout is long (1 s) on purpose: record() used to share a
        # lock with the writer thread, which holds it while SQLite waits out
        # the busy file, so record() could block for a whole busy_timeout.
        # The bounds below are far under that but loose enough for a
        # machine under heavy load (this test was flaky at 0.05 s).
        busy = 1.0
        h = self.H(busy_timeout=busy)
        h.record("first", "first", 1)
        h.flush()
        textrules.READ_FLUSH_WAIT = self.saved_wait     # the real default
        other = sqlite3.connect(str(self.path), isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        try:
            slowest = 0.0
            for i in range(5):        # the writer is busy-waiting meanwhile
                started = time.perf_counter()
                h.record(f"queued{i}", "queued", 1)
                slowest = max(slowest, time.perf_counter() - started)
                time.sleep(0.05)
            self.assertLess(slowest, busy * 0.3)
            started = time.perf_counter()
            with self.assertLogs("holdtotalk.textrules", "WARNING"):
                self.assertFalse(h.delete(1))      # busy -> False, no raise
            # flush wait + the writer's busy wait + its own busy wait, never
            # the writer's whole ~8 s retry schedule.
            self.assertLess(time.perf_counter() - started,
                            textrules.READ_FLUSH_WAIT + 2 * busy + 2.0)
        finally:
            other.execute("ROLLBACK")
            other.close()
        self.assertTrue(h.flush(20))     # the writer retried and succeeded
        self.assertEqual([r["raw"] for r in h.recent()],
                         [f"queued{i}" for i in range(4, -1, -1)] + ["first"])


class HistoryDamage(HistoryBase):
    def test_corrupt_file_is_moved_aside(self):
        self.path.write_bytes(b"this is not a database at all" * 100)
        h = self.H()
        with self.assertLogs("holdtotalk.textrules", "WARNING") as logs:
            h.record("a", "A", 1)
            self.assertEqual([r["final"] for r in h.recent()], ["A"])
        self.assertIn("damaged", "\n".join(logs.output))
        moved = glob.glob(str(self.path) + ".corrupt-*")
        self.assertEqual(len(moved), 1)
        self.assertEqual(Path(moved[0]).read_bytes()[:11], b"this is not")

    def test_corruption_found_later(self):
        h = self.H()
        h.record("a", "A", 1)
        h.close()
        data = bytearray(self.path.read_bytes())
        data[:16] = b"garbage garbage!"         # break the SQLite header
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(str(self.path) + suffix)
            except OSError:
                pass
        self.path.write_bytes(bytes(data))
        h2 = self.H()
        with self.assertLogs("holdtotalk.textrules", "WARNING"):
            self.assertEqual(h2.recent(), [])
        h2.record("b", "B", 1)
        self.assertEqual([r["final"] for r in h2.recent()], ["B"])

    def test_file_locked_by_another_program(self):
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.H().record("a", "A", 1)
        self.opened[0].close()
        handle = k32.CreateFileW(str(self.path), 0x80000000 | 0x40000000, 0,
                                 None, 3, 0x80, None)   # no sharing
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            h = self.H(busy_timeout=0.2)
            with self.assertLogs("holdtotalk.textrules", "WARNING"):
                self.assertEqual(h.recent(), [])
                self.assertFalse(h.clear())
                h.record("lost?", "B", 1)          # must not raise
        finally:
            k32.CloseHandle(handle)
        h.flush(5)
        # The next call reopens the file; the old row is still there.
        self.assertIn("A", [r["final"] for r in h.recent()])

    def test_unwritable_location(self):
        h = textrules.History(Path(self.dir) / "file.txt" / "h.sqlite3")
        self.opened.append(h)
        (Path(self.dir) / "file.txt").write_text("a file, not a folder")
        with self.assertLogs("holdtotalk.textrules", "WARNING"):
            h.record("a", "A", 1)
            self.assertEqual(h.recent(), [])


if __name__ == "__main__":
    unittest.main()
