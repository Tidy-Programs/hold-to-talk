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


import copy
import unittest

import learn
import pipeline
import settings
import textrules
from learn import LearnError


def base_cfg(**kw):
    cfg = copy.deepcopy(settings.DEFAULTS)
    cfg["cleanup"]["enabled"] = False
    cfg.update(kw)
    return cfg


# (pasted text, corrected text, expected)
# expected: None = nothing to learn, or a list of
#   (kind, dictionary heard or None, wanted, vocabulary term or None)
LEARN = [
    ("Open the Sorbex animations folder.", "Open the Zorvex animations folder.",
     [("misheard", "sorbex", "Zorvex", "Zorvex")]),
    ("I think Sorbox will love this new idea.",
     "I think Zorvex will love this new idea.",
     [("misheard", "sorbox", "Zorvex", "Zorvex")]),
    ("Push it to github tonight.", "Push it to GitHub tonight.",
     [("spelling", "github", "GitHub", "GitHub")]),
    ("Push it to git hub tonight.", "Push it to GitHub tonight.",
     [("spelling", "git hub", "GitHub", "GitHub")]),
    ("Load it with pie torch.", "Load it with PyTorch.",
     [("misheard", "pie torch", "PyTorch", "PyTorch")]),
    ("Send the jason file.", "Send the JSON file.",
     [("misheard", "jason", "JSON", "JSON")]),
    ("Caliph said the build is broken again.",
     "Kaelith said the build is broken again.",
     [("misheard", "caliph", "Kaelith", "Kaelith")]),
    ("We moved the files to Brian Vault last night.",
     "We moved the files to Brienvault last night.",
     [("misheard", "brian vault", "Brienvault", "Brienvault")]),
    ("I sent the report to Trevik Maraska yesterday.",
     "I sent the report to Trevik Moraska yesterday.",
     [("misheard", "maraska", "Moraska", "Moraska")]),
    ("The Zorblan dashboard is ready for review.",
     "The Zorblin dashboard is ready for review.",
     [("misheard", "zorblan", "Zorblin", "Zorblin")]),
    ("Send the draft to Fadriks before lunch.",
     "Send the draft to Phaedrix before lunch.",
     [("misheard", "fadriks", "Phaedrix", "Phaedrix")]),
    ("We use cooper netties at work.", "We use Kubernetes at work.",
     [("misheard", "cooper netties", "Kubernetes", "Kubernetes")]),
    ("Write it in type script.", "Write it in TypeScript.",
     [("spelling", "type script", "TypeScript", "TypeScript")]),
    ("Sorbex's idea was great.", "Zorvex's idea was great.",
     [("misheard", "sorbex", "Zorvex", "Zorvex")]),
    ("Talk to sarah tomorrow.", "Talk to Sarah tomorrow.",
     [("spelling", "sarah", "Sarah", "Sarah")]),
    ("Call Dwoskin and Merobe about the invoice.",
     "Call Dwoskyn and Merrowby about the invoice.",
     [("misheard", "dwoskin", "Dwoskyn", "Dwoskyn"),
      ("misheard", "merobe", "Merrowby", "Merrowby")]),
    ("sorbex", "Zorvex", [("misheard", "sorbex", "Zorvex", "Zorvex")]),
    ("Ask Jon about it.", "Ask John about it.",
     [("misheard", "jon", "John", "John")]),
    ("Bring the Allen key.", "Bring the Alan key.",
     [("misheard", "allen", "Alan", "Alan")]),
    # Common heard word: vocabulary only, never a rule.
    ("Open the server animations folder.", "Open the Zorvex animations folder.",
     [("term", None, "Zorvex", "Zorvex")]),
    ("Server's idea was great.", "Zorvex's idea was great.",
     [("term", None, "Zorvex", "Zorvex")]),
    # Left another occurrence alone: no rule, but the term is still useful.
    ("Sorbex said sorbex twice.", "Zorvex said sorbex twice.",
     [("term", None, "Zorvex", "Zorvex")]),
]

NOTHING = [
    ("The meeting is on Tuesday.", "The meeting is on Wednesday."),
    ("It is a big problem.", "It is a large problem."),
    ("I think its fine", "I think it's fine."),
    ("Their going home.", "They're going home."),
    ("hello world", "Hello, world!"),
    ("Okay so we go now", "Okay, so we go now."),
    ("We should ship it today.", "We must ship it today, no later."),
    ("Send it to Sorbex.", "Send it to Sorbex and Kaelith."),
    ("Send it to Sorbex please.", "Send it please."),
    ("Use Python for this.", "Use Rust for this."),
    ("the plan is good", "The plan is good"),
    ("Send an e-mail.", "Send an email."),
    ("I have 2 cats.", "I have two cats."),
    ("I'll be there in 5 minutes.", "I'll be there in five minutes."),
    ("Can you send me the file", "Please send the file over to me now"),
    ("We meet at the bank.", "We meet at the river."),
    ("It fell a part.", "It fell apart."),
    ("Put it over their.", "Put it over there."),
    ("Apple pie is great.", "apple pie is great."),
    ("I want to go to the shop and buy milk and eggs today",
     "Tomorrow I need to visit the market for bread instead"),
    ("one two three four five six", "uno dos tres cuatro cinco seis"),
    ("The Sorbex team met. Then the Sorbex team left.",
     "The Zorvex crew met. Then the team went home early."),
    ("Same text.", "Same text."),
    ("Something here.", ""),
    ("Something here.", "   "),
    ("", "Zorvex"),
    ("Send it to sorbex now.", "Send it to Zorvex right now please."),
    ("Write this in javascript", "Write this in Java."),
    ("a b c d e f", "a x c y e z g"),
    ("We need the new line feature.", "We need the new-line feature."),
    ("THE END", "The End"),
    ("Deploy to AWS today.", "Deploy to Azure today."),
    ("We are using react", "We are using React"),       # "react": common
    ("Open Visual Studio Code.", "Open VS Code."),
    ("I'm gonna go.", "I'm going to go."),
    ("Ok lets go", "OK let's go"),
    ("call Dr Smith", "call Dr. Smith"),
]


class Rules(unittest.TestCase):
    def test_learns(self):
        for orig, corr, expected in LEARN:
            with self.subTest(orig=orig, corr=corr):
                got = learn.suggest(orig, corr)
                self.assertEqual(
                    [(s["kind"], (s["dictionary"] or {}).get("heard"),
                      s["wanted"], s["vocabulary"]) for s in got],
                    expected)
                for s in got:
                    if s["dictionary"]:
                        self.assertEqual(s["dictionary"]["wanted"],
                                         s["wanted"])
                    for key in ("id", "summary", "reason", "before", "after",
                                "confidence", "warnings"):
                        self.assertIn(key, s)
                    self.assertTrue(s["summary"].strip())

    def test_learns_nothing(self):
        for orig, corr in NOTHING:
            with self.subTest(orig=orig, corr=corr):
                self.assertEqual(learn.suggest(orig, corr), [])

    def test_odd_input_never_raises(self):
        for a, b in ((None, "x"), ("x", None), (5, "x"), ("...", "!!!"),
                     ("x", "y"), ("a" * 5000, "b" * 5000)):
            self.assertEqual(learn.suggest(a, b), [])

    def test_common_word_rule_offered_only_for_manual_add(self):
        [s] = learn.suggest("Open the server folder.", "Open the Zorvex folder.")
        self.assertIsNone(s["dictionary"])
        self.assertEqual(s["manual_rule"], {"heard": "server",
                                            "wanted": "Zorvex"})
        self.assertIn("ordinary word", s["reason"])
        res = learn.add_entry({}, **s["manual_rule"])     # explicit, by hand
        self.assertTrue(any("ordinary English word" in w
                            for w in res["warnings"]))

    def test_rewrite_verdicts(self):
        self.assertEqual(learn.analyse("a b c d e f g h", "a X c Y e Z g W")[0],
                         "rewritten (too many changes)")
        self.assertEqual(learn.analyse(
            "We will send it now to them",
            "We will happily and quickly deliver everything now to them")[0],
            "rewritten (long change)")
        self.assertEqual(learn.analyse("send it now", "please send all of it "
                                       "to them now")[0],
                         "rewritten (too much changed)")
        self.assertIsNone(learn.analyse("send sorbex", "send Zorvex")[0])

    def test_confidence(self):
        [s] = learn.suggest("Push to github.", "Push to GitHub.")
        self.assertEqual(s["confidence"], "high")
        [s] = learn.suggest("Ask sorbex.", "Ask Zorvex.")
        self.assertEqual(s["confidence"], "normal")
        # Two changes in one correction: not "high", even for spelling.
        got = learn.suggest("Push github to sorbex.", "Push GitHub to Zorvex.")
        self.assertEqual({s["confidence"] for s in got}, {"normal"})

    def test_known_items_are_not_suggested(self):
        cfg = {"vocabulary": ["Zorvex"],
               "dictionary": [{"heard": "sorbex", "wanted": "Zorvex"}]}
        self.assertEqual(learn.suggest("Ask sorbex.", "Ask Zorvex.", cfg), [])
        [s] = learn.suggest("Ask sorbex.", "Ask Zorvex.",
                            {"vocabulary": ["Zorvex"]})
        self.assertIsNone(s["vocabulary"])          # case-insensitive dedupe
        self.assertEqual(s["dictionary"],
                         {"heard": "sorbex", "wanted": "Zorvex"})

    def test_conflicting_entry_is_flagged(self):
        cfg = {"dictionary": [{"heard": "sorbex", "wanted": "Sorbexx"}]}
        [s] = learn.suggest("Ask sorbex.", "Ask Zorvex.", cfg)
        self.assertEqual(s["replaces"], {"heard": "sorbex", "wanted": "Sorbexx"})
        self.assertTrue(s["warnings"])

    def test_correcting_a_rules_output_fixes_that_rule(self):
        # The dictionary already turned "sorbex" into "Sorvex"; the user
        # corrects "Sorvex" -> "Zorvex". The rule that fires is "sorbex".
        cfg = {"dictionary": [{"heard": "sorbex", "wanted": "Sorvex"}]}
        [s] = learn.suggest("Ask Sorvex.", "Ask Zorvex.", cfg)
        self.assertEqual(s["dictionary"], {"heard": "sorbex",
                                           "wanted": "Zorvex"})
        self.assertEqual(s["replaces"], {"heard": "sorbex", "wanted": "Sorvex"})

    def test_ids_are_stable(self):
        a = learn.suggest("Ask sorbex.", "Ask Zorvex.")
        b = learn.suggest("Tell sorbex hi.", "Tell Zorvex hi.")
        self.assertEqual(a[0]["id"], b[0]["id"])

    def test_is_common(self):
        for w in ("the", "Server", "servers", "running", "walked", "it's",
                  "Tuesday", "parties", "follow-up", "stopped"):
            self.assertTrue(learn.is_common(w), w)
        for w in ("Zorvex", "github", "Kaelith", "JSON", "x86", ""):
            self.assertFalse(learn.is_common(w), w)

    def test_similarity(self):
        self.assertGreater(learn.similarity("sorbex", "Zorvex"), 0.6)
        self.assertGreater(learn.similarity("cooper netties", "Kubernetes"),
                           0.7)
        self.assertLess(learn.similarity("Python", "Rust"), 0.6)
        self.assertEqual(learn.similarity("", "x"), 0.0)


class ApplyUndo(unittest.TestCase):
    def test_round_trip_through_textrules(self):
        cfg = base_cfg()
        raw = "Please open the Sorbex animations folder."
        self.assertEqual(textrules.apply(raw, cfg), raw)
        sugg = learn.suggest(raw, raw.replace("Sorbex", "Zorvex"), cfg,
                             history_id=7)
        res = learn.apply_suggestions(cfg, sugg, now=1000.0)
        self.assertEqual(res["skipped"], [])
        cfg2 = dict(cfg, **res["changes"])
        self.assertEqual(textrules.apply(raw, cfg2),
                         "Please open the Zorvex animations folder.")
        self.assertIn("Zorvex", textrules.vocabulary_terms(cfg2))
        [rec] = res["applied"]
        self.assertEqual(rec["history_id"], 7)
        self.assertEqual(rec["ts"], 1000.0)
        self.assertEqual(rec["added_entry"], {"heard": "sorbex",
                                              "wanted": "Zorvex"})
        self.assertEqual(rec["added_term"], "Zorvex")
        [item] = learn.learned_items(cfg2)
        self.assertTrue(item["active"])
        # undo removes exactly that
        u = learn.undo(cfg2, rec["id"])
        cfg3 = dict(cfg2, **u["changes"])
        self.assertEqual(textrules.apply(raw, cfg3), raw)
        self.assertEqual(cfg3["vocabulary"], [])
        self.assertEqual(cfg3["dictionary"], [])
        self.assertEqual(cfg3["learned"], [])
        self.assertEqual(u["warnings"], [])
        with self.assertRaises(LearnError):
            learn.undo(cfg3, rec["id"])

    def test_input_cfg_not_mutated(self):
        cfg = base_cfg(vocabulary=["X"],
                       dictionary=[{"heard": "a b", "wanted": "AB"}])
        snapshot = copy.deepcopy(cfg)
        res = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                         "Ask Zorvex."))
        learn.undo(dict(cfg, **res["changes"]), res["applied"][0]["id"])
        self.assertEqual(cfg, snapshot)

    def test_duplicates_are_skipped(self):
        cfg = base_cfg()
        sugg = learn.suggest("Ask sorbex.", "Ask Zorvex.")
        res = learn.apply_suggestions(cfg, sugg)
        cfg = dict(cfg, **res["changes"])
        res2 = learn.apply_suggestions(cfg, sugg)       # accepted twice
        self.assertEqual(res2["applied"], [])
        self.assertEqual(len(res2["skipped"]), 1)
        self.assertEqual(res2["changes"]["dictionary"], cfg["dictionary"])
        self.assertEqual(res2["changes"]["vocabulary"], ["Zorvex"])

    def test_never_clobbers_hand_made_entry_silently(self):
        hand = {"heard": "sorbex", "wanted": "Sorbex Inc."}
        cfg = base_cfg(dictionary=[hand])
        s = {"id": "x", "kind": "misheard", "heard": "sorbex",
             "wanted": "Zorvex",
             "dictionary": {"heard": "sorbex", "wanted": "Zorvex"},
             "vocabulary": None, "replaces": None}
        res = learn.apply_suggestions(cfg, [s])
        self.assertEqual(res["changes"]["dictionary"], [hand])
        self.assertIn("left as is", res["skipped"][0]["reason"])

    def test_replace_with_consent_and_undo_restores(self):
        other = {"heard": "zed", "wanted": "Zed"}
        old = {"heard": "sorbex", "wanted": "Sorbexx"}
        cfg = base_cfg(dictionary=[other, old, {"heard": "q", "wanted": "Q"}])
        [s] = learn.suggest("Ask sorbex.", "Ask Zorvex.", cfg)
        res = learn.apply_suggestions(cfg, [s])
        cfg2 = dict(cfg, **res["changes"])
        self.assertEqual(cfg2["dictionary"][1],
                         {"heard": "sorbex", "wanted": "Zorvex"})
        self.assertEqual(res["applied"][0]["replaced_entry"], old)
        u = learn.undo(cfg2, res["applied"][0]["id"])
        self.assertEqual(u["changes"]["dictionary"], cfg["dictionary"])
        self.assertEqual(u["restored_entry"], old)

    def test_undo_leaves_user_edits_alone(self):
        cfg = base_cfg()
        res = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                         "Ask Zorvex."))
        cfg2 = dict(cfg, **res["changes"])
        cfg2["dictionary"] = [{"heard": "sorbex", "wanted": "Sorbex"}]
        [item] = learn.learned_items(cfg2)
        self.assertTrue(item["active"])           # the term is still there
        u = learn.undo(cfg2, res["applied"][0]["id"])
        self.assertEqual(u["changes"]["dictionary"],
                         [{"heard": "sorbex", "wanted": "Sorbex"}])
        self.assertEqual(u["changes"]["vocabulary"], [])
        self.assertEqual(len(u["warnings"]), 1)
        self.assertIn("changed", u["warnings"][0])

    def test_undo_one_of_two_keeps_the_other(self):
        cfg = base_cfg()
        r1 = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                        "Ask Zorvex."), now=1)
        cfg = dict(cfg, **r1["changes"])
        r2 = learn.apply_suggestions(cfg, learn.suggest("Push github.",
                                                        "Push GitHub."), now=2)
        cfg = dict(cfg, **r2["changes"])
        self.assertEqual([i["wanted"] for i in learn.learned_items(cfg)],
                         ["GitHub", "Zorvex"])          # newest first
        u = learn.undo(cfg, r1["applied"][0]["id"])
        cfg = dict(cfg, **u["changes"])
        self.assertEqual(cfg["dictionary"],
                         [{"heard": "github", "wanted": "GitHub"}])
        self.assertEqual(cfg["vocabulary"], ["GitHub"])
        self.assertEqual(textrules.apply("ask sorbex on github", cfg),
                         "ask sorbex on GitHub")

    def test_term_only_suggestion(self):
        cfg = base_cfg()
        sugg = learn.suggest("Open the server folder.", "Open the Zorvex folder.")
        res = learn.apply_suggestions(cfg, sugg)
        self.assertEqual(res["changes"]["dictionary"], [])
        self.assertEqual(res["changes"]["vocabulary"], ["Zorvex"])
        self.assertEqual(res["applied"][0]["kind"], "term")
        u = learn.undo(dict(cfg, **res["changes"]), res["applied"][0]["id"])
        self.assertEqual(u["removed_term"], "Zorvex")
        self.assertEqual(u["changes"]["vocabulary"], [])

    def test_malformed_suggestions_are_skipped(self):
        res = learn.apply_suggestions({}, [None, {}, {"dictionary": "x"},
                                           {"dictionary": {"heard": " ",
                                                           "wanted": "x"}},
                                           {"vocabulary": "a\nb"}])
        self.assertEqual(res["applied"], [])
        self.assertEqual(len(res["skipped"]), 5)
        self.assertEqual(res["changes"], {"vocabulary": [], "dictionary": [],
                                          "learned": []})

    def test_bad_existing_entries_are_preserved(self):
        cfg = {"dictionary": ["junk", {"heard": 5}], "vocabulary": [None]}
        res = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                         "Ask Zorvex."))
        self.assertEqual(res["changes"]["dictionary"][:2],
                         ["junk", {"heard": 5}])
        self.assertEqual(res["changes"]["vocabulary"][0], None)

    def test_auto_apply(self):
        off = base_cfg()
        out = learn.learn_from_correction(off, "Push github to sorbex.",
                                          "Push GitHub to sorbex.")
        self.assertIsNone(out["auto"])
        self.assertEqual(len(out["suggestions"]), 1)
        on = base_cfg(learning={"auto_apply": True})
        out = learn.learn_from_correction(on, "Push github to sorbex.",
                                          "Push GitHub to sorbex.", now=5)
        self.assertEqual(out["suggestions"], [])
        self.assertEqual(out["auto"]["changes"]["dictionary"],
                         [{"heard": "github", "wanted": "GitHub"}])
        self.assertTrue(out["auto"]["applied"][0]["auto"])
        # A mis-hearing is never auto-applied.
        out = learn.learn_from_correction(on, "Ask sorbex.", "Ask Zorvex.")
        self.assertIsNone(out["auto"])
        self.assertEqual(len(out["suggestions"]), 1)

    def test_settings(self):
        self.assertEqual(learn.settings_of({}), {"auto_apply": False})
        self.assertEqual(learn.settings_of({"learning": {"auto_apply": 1}}),
                         {"auto_apply": True})
        self.assertEqual(learn.settings_of({"learning": "bad"}),
                         learn.DEFAULTS)
        self.assertEqual(learn.settings_of(None), learn.DEFAULTS)

    def test_saved_and_reloaded_config(self):
        import os
        import tempfile
        d = tempfile.mkdtemp(prefix="htt-learn-")
        path = os.path.join(d, "config.json")
        try:
            cfg = settings.load_config(path)
            res = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                             "Ask Zorvex."))
            settings.save_config(res["changes"], path)
            cfg = settings.load_config(path)
            self.assertEqual(len(learn.learned_items(cfg)), 1)
            self.assertEqual(textrules.apply("Ask sorbex.", cfg), "Ask Zorvex.")
            u = learn.undo(cfg, learn.learned_items(cfg)[0]["id"])
            settings.save_config(u["changes"], path)
            cfg = settings.load_config(path)
            self.assertEqual(textrules.apply("Ask sorbex.", cfg), "Ask sorbex.")
            self.assertEqual(cfg["learned"], [])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class Manual(unittest.TestCase):
    def test_terms(self):
        cfg = {"vocabulary": ["Zorvex"]}
        for bad, msg in (("", "Type a word"), ("   ", "Type a word"),
                         (None, "Type a word"), ("a\nb", "one line"),
                         ("--", "letter or digit"), ("x" * 101, "too long"),
                         ("Zorvex", "already in your vocabulary")):
            with self.subTest(bad=bad):
                with self.assertRaises(LearnError) as cm:
                    learn.add_term(cfg, bad)
                self.assertIn(msg, str(cm.exception))
        res = learn.add_term(cfg, "  Kaelith ")
        self.assertEqual(res["changes"]["vocabulary"], ["Zorvex", "Kaelith"])
        self.assertEqual(res["warnings"], [])
        self.assertTrue(learn.add_term(cfg, "table")["warnings"])
        self.assertTrue(learn.add_term(cfg, "a b c d e f g")["warnings"])
        self.assertEqual(cfg, {"vocabulary": ["Zorvex"]})       # untouched

    def test_edit_remove_term(self):
        cfg = {"vocabulary": ["Zorvex", "Kaelith"]}
        self.assertEqual(learn.edit_term(cfg, "Zorvex", "Zorvex")
                         ["changes"]["vocabulary"], ["Zorvex", "Kaelith"])
        with self.assertRaises(LearnError):
            learn.edit_term(cfg, "Zorvex", "kaelith")         # duplicate
        with self.assertRaises(LearnError):
            learn.edit_term(cfg, "Nope", "X")
        self.assertEqual(learn.remove_term(cfg, "KAELITH")
                         ["changes"]["vocabulary"], ["Zorvex"])
        with self.assertRaises(LearnError) as cm:
            learn.remove_term(cfg, "Nope")
        self.assertIn("not in your vocabulary", str(cm.exception))
        with self.assertRaises(LearnError):
            learn.remove_term({}, "x")

    def test_entries(self):
        cfg = {"dictionary": [{"heard": "jason", "wanted": "JSON"}],
               "replacements": {"pie torch": "PyTorch"}}
        for heard, wanted, msg in (
                ("", "x", "heard"), ("x", "", "instead"),
                (None, "x", "heard"), ("x", None, "instead"),
                ("a\nb", "x", "one line"), ("!!", "x", "letter or digit"),
                ("Same", "Same", "change nothing"),
                ("JASON", "Json", "already has an entry"),
                ("  jason ", "x", "already has an entry")):
            with self.subTest(heard=heard, wanted=wanted):
                with self.assertRaises(LearnError) as cm:
                    learn.add_entry(cfg, heard, wanted)
                self.assertIn(msg, str(cm.exception))
        res = learn.add_entry(cfg, " sorbex ", "Zorvex")
        self.assertEqual(res["changes"]["dictionary"][-1],
                         {"heard": "sorbex", "wanted": "Zorvex"})
        # Any single lowercase word is risky, not only the common ones.
        [warning] = res["warnings"]
        self.assertIn("every “sorbex” in every dictation will become "
                      "“Zorvex”", warning)
        w = learn.add_entry(cfg, "table", "Tabel")["warnings"]
        self.assertIn("ordinary English word", w[0])
        self.assertIn("very short", " ".join(
            learn.add_entry(cfg, "zq", "ZQ")["warnings"]))
        self.assertIn("overrides", learn.add_entry(cfg, "pie torch",
                                                   "PyTorch!")["warnings"][0])
        # case-only fix is allowed
        [w] = learn.add_entry(cfg, "github", "GitHub")["warnings"]
        self.assertIn("every “github” in every dictation", w)   # item 11

    def test_edit_remove_entry(self):
        cfg = {"dictionary": [{"heard": "jason", "wanted": "JSON"},
                              {"heard": "kube", "wanted": "kubectl"}]}
        res = learn.edit_entry(cfg, "Jason", "jayson", "JSON")
        self.assertEqual(res["changes"]["dictionary"][0],
                         {"heard": "jayson", "wanted": "JSON"})
        res = learn.edit_entry(cfg, "jason", "jason", "Json5")   # same heard
        self.assertEqual(res["changes"]["dictionary"][0]["wanted"], "Json5")
        with self.assertRaises(LearnError):
            learn.edit_entry(cfg, "jason", "kube", "x")          # duplicate
        with self.assertRaises(LearnError):
            learn.edit_entry(cfg, "nope", "a", "b")
        self.assertEqual(learn.remove_entry(cfg, "KUBE")["changes"]
                         ["dictionary"], [{"heard": "jason",
                                           "wanted": "JSON"}])
        with self.assertRaises(LearnError):
            learn.remove_entry(cfg, "nope")

    def test_check_helpers(self):
        self.assertEqual(learn.check_term({}, "Zorvex"), [])
        self.assertEqual(learn.check_entry({}, "sor bex", "Zorvex"), [])
        with self.assertRaises(ValueError):            # LearnError is one
            learn.check_entry({}, "", "x")

    def test_preview(self):
        p = learn.preview_entry("sorbex", "Zorvex",
                                "Sorbex met sorbex. Sorbexes are fine.")
        self.assertEqual(p["text"], "Zorvex met Zorvex. Sorbexes are fine.")
        self.assertEqual(p["matches"], 2)
        self.assertEqual(p["full"], p["text"])
        cfg = {"dictionary": [{"heard": "jason", "wanted": "JSON"},
                              {"heard": "sorbex", "wanted": "Old"}]}
        p = learn.preview_entry("sorbex", "Zorvex", "sorbex sent jason", cfg)
        self.assertEqual(p["text"], "Zorvex sent jason")
        self.assertEqual(p["full"], "Zorvex sent JSON")
        self.assertEqual(learn.preview_entry("", "x", "abc"),
                         {"text": "abc", "matches": 0, "full": "abc"})
        self.assertEqual(learn.preview_entry("a", "b", None)["text"], "")


class EndToEnd(unittest.TestCase):
    """Through pipeline.run with the LLM stage off (deterministic)."""

    def test_learn_then_undo(self):
        cfg = base_cfg()
        raw = "caliph said the build is broken again"
        wrong = pipeline.run(raw, cfg)
        self.assertEqual(wrong, "Caliph said the build is broken again")
        fixed_text = "Kaelith said the build is broken again"
        sugg = learn.suggest(wrong, fixed_text, cfg)
        res = learn.apply_suggestions(cfg, sugg)
        cfg.update(res["changes"])
        self.assertEqual(pipeline.run(raw, cfg), fixed_text)
        cfg.update(learn.undo(cfg, res["applied"][0]["id"])["changes"])
        self.assertEqual(pipeline.run(raw, cfg), wrong)


class AutoLearningIsCautious(unittest.TestCase):
    """Item 11."""

    def conf(self, before, after):
        return [(s["kind"], s["confidence"], s["wanted"])
                for s in learn.suggest(before, after)]

    def test_changing_only_the_case_of_an_ordinary_word_is_never_high(self):
        for before, after in (
                ("This is urgent today.", "This is URGENT today."),
                ("Compile the kernel today.", "Compile the Kernel today."),
                ("Fix the cache today.", "Fix the CACHE today."),
                ("Fix the cache today.", "Fix the Cache today.")):
            with self.subTest(after=after):
                got = self.conf(before, after)
                self.assertTrue(got)
                self.assertTrue(all(c == "normal" for _, c, _ in got), got)

    def test_product_like_spellings_are_still_high(self):
        for before, after, wanted in (
                ("Push to github now.", "Push to GitHub now.", "GitHub"),
                ("Use typescript here.", "Use TypeScript here.", "TypeScript"),
                ("Run it on ios today.", "Run it on iOS today.", "iOS"),
                ("Play the mp3 file.", "Play the MP3 file.", "MP3"),
                ("Push it to git hub tonight.", "Push it to GitHub tonight.",
                 "GitHub")):
            with self.subTest(wanted=wanted):
                self.assertEqual(self.conf(before, after),
                                 [("spelling", "high", wanted)])

    def test_auto_apply_leaves_ordinary_words_alone(self):
        cfg = base_cfg(learning={"auto_apply": True})
        for before, after in (("This is urgent today.", "This is URGENT today."),
                              ("Compile the kernel today.",
                               "Compile the Kernel today.")):
            out = learn.learn_from_correction(cfg, before, after)
            self.assertIsNone(out["auto"], after)
            self.assertEqual(len(out["suggestions"]), 1)
        out = learn.learn_from_correction(cfg, "Push to github now.",
                                          "Push to GitHub now.")
        self.assertEqual(out["auto"]["changes"]["dictionary"],
                         [{"heard": "github", "wanted": "GitHub"}])

    def test_any_single_lowercase_word_gets_the_warning(self):
        for word in ("cache", "patent", "dictation", "punctuation", "sorbex"):
            with self.subTest(word=word):
                [w] = learn.check_entry({}, word, "Zorvex")
                self.assertIn(f"every “{word}” in every dictation will "
                              "become “Zorvex”", w)
        # Common words keep their wording; others do not claim "English".
        self.assertIn("ordinary English word",
                      learn.check_entry({}, "table", "Tabel")[0])
        self.assertNotIn("English", learn.check_entry({}, "cache", "Zorvex")[0])

    def test_suggestions_carry_the_same_warning_as_adding_by_hand(self):
        # L4: "cache" -> a name came back from suggest() with warnings: [].
        for pasted, corrected, word in (
                ("Ask Cache about it.", "Ask Kash about it.", "cache"),
                ("Talk to patent today.", "Talk to Patents today.",
                 "patent")):
            with self.subTest(word=word):
                [s] = learn.suggest(pasted, corrected)
                self.assertEqual(s["dictionary"]["heard"], word)
                by_hand = learn.check_entry({}, word, s["dictionary"]["wanted"])
                self.assertEqual(s["warnings"], by_hand)
                self.assertIn("single lowercase word", s["warnings"][0])

    def test_a_replaced_entry_keeps_its_own_warning_first(self):
        cfg = base_cfg(dictionary=[{"heard": "cache", "wanted": "Cash"}])
        [s] = learn.suggest("Ask Cache about it.", "Ask Kash about it.", cfg)
        self.assertIn("Replaces", s["warnings"][0])
        self.assertIn("single lowercase word", s["warnings"][1])
    def test_other_shapes_do_not_get_it(self):
        for heard in ("Cache", "CACHE", "c3po", "sor bex", "pie-torch"):
            with self.subTest(heard=heard):
                self.assertFalse(any("every" in w for w in
                                     learn.check_entry({}, heard, "Zorvex")))


class UndoCannotHitHandMadeEntries(unittest.TestCase):
    """Item 12."""

    def learned(self):
        cfg = base_cfg()
        res = learn.apply_suggestions(
            cfg, learn.suggest("Ask sorbex.", "Ask Zorvex."), now=1)
        return dict(cfg, **res["changes"]), res["applied"][0]["id"]

    def test_undo_after_remove_and_re_add_by_hand_removes_nothing(self):
        cfg, rid = self.learned()
        for fn, args in ((learn.remove_entry, ("sorbex",)),
                         (learn.remove_term, ("Zorvex",))):
            cfg = dict(cfg, **fn(cfg, *args)["changes"])
        self.assertTrue(cfg["learned"][0]["inactive"])
        cfg = dict(cfg, **learn.add_entry(cfg, "sorbex", "Zorvex")["changes"])
        cfg = dict(cfg, **learn.add_term(cfg, "Zorvex")["changes"])
        [item] = learn.learned_items(cfg)
        self.assertFalse(item["active"])       # not the learned one any more
        u = learn.undo(cfg, rid)
        self.assertEqual(u["changes"]["dictionary"],
                         [{"heard": "sorbex", "wanted": "Zorvex"}])
        self.assertEqual(u["changes"]["vocabulary"], ["Zorvex"])
        self.assertIsNone(u["removed_entry"])
        self.assertIsNone(u["removed_term"])
        self.assertIsNone(u["restored_entry"])
        self.assertEqual(len(u["warnings"]), 1)
        self.assertIn("nothing was undone", u["warnings"][0])
        self.assertEqual(len(u["changes"]["learned"]), 1)    # record kept

    def test_editing_marks_it_inactive_too(self):
        cfg, rid = self.learned()
        cfg = dict(cfg, **learn.edit_entry(cfg, "sorbex", "sorbex",
                                           "Zorvex Inc.")["changes"])
        self.assertTrue(cfg["learned"][0]["inactive"])
        cfg2, rid2 = self.learned()
        cfg2 = dict(cfg2, **learn.edit_term(cfg2, "Zorvex", "Zorvex 2")["changes"])
        self.assertTrue(cfg2["learned"][0]["inactive"])
        u = learn.undo(cfg2, rid2)
        self.assertEqual(u["changes"]["vocabulary"], ["Zorvex 2"])
        self.assertEqual(u["changes"]["dictionary"],
                         [{"heard": "sorbex", "wanted": "Zorvex"}])

    def test_untouched_items_still_undo_and_only_the_right_one_goes_inactive(self):
        cfg = base_cfg()
        r1 = learn.apply_suggestions(cfg, learn.suggest("Ask sorbex.",
                                                        "Ask Zorvex."), now=1)
        cfg = dict(cfg, **r1["changes"])
        r2 = learn.apply_suggestions(cfg, learn.suggest("Push github.",
                                                        "Push GitHub."), now=2)
        cfg = dict(cfg, **r2["changes"])
        cfg = dict(cfg, **learn.remove_entry(cfg, "github")["changes"])
        flags = {r["heard"]: bool(r.get("inactive")) for r in cfg["learned"]}
        self.assertEqual(flags, {"sorbex": False, "github": True})
        u = learn.undo(cfg, r1["applied"][0]["id"])
        self.assertEqual(u["changes"]["dictionary"], [])
        self.assertEqual(u["changes"]["vocabulary"], ["GitHub"])

    def test_helpers_report_no_learned_change_when_nothing_matches(self):
        cfg = base_cfg(dictionary=[{"heard": "jason", "wanted": "JSON"}])
        self.assertNotIn("learned", learn.remove_entry(cfg, "jason")["changes"])

    def test_records_no_longer_keep_excerpts_of_the_dictation(self):
        cfg = base_cfg()
        sugg = learn.suggest("Open the sorbex animations folder.",
                             "Open the Zorvex animations folder.")
        self.assertIn("animations", sugg[0]["before"])      # shown, not stored
        res = learn.apply_suggestions(cfg, sugg, now=1)
        [rec] = res["changes"]["learned"]
        self.assertEqual((rec["before"], rec["after"]), ("", ""))
        self.assertNotIn("animations", repr(res["changes"]))

    def test_old_records_are_blanked_when_read(self):
        cfg = base_cfg(learned=[{"id": "a", "ts": 1, "kind": "term",
                                 "heard": "x", "wanted": "Zorvex",
                                 "added_entry": None, "added_term": "Zorvex",
                                 "before": "private words", "after": "more"}],
                       vocabulary=["Zorvex"])
        [item] = learn.learned_items(cfg)
        self.assertEqual((item["before"], item["after"]), ("", ""))
        recs, changed = learn.scrub_excerpts(cfg["learned"])
        self.assertTrue(changed)
        self.assertEqual(recs[0]["before"], "")
        self.assertEqual(learn.scrub_excerpts(recs)[1], False)

if __name__ == "__main__":
    unittest.main()
