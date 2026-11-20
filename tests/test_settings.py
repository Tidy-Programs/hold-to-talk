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


import json
import unittest
from pathlib import Path

import settings


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_defaults(self):
        self.assertEqual(settings.load_config(self.path), settings.DEFAULTS)

    def test_round_trip_preserves_unknown_keys(self):
        self.path.write_text(json.dumps({"hotkey": "f9", "future_key": [1, 2]}),
                             encoding="utf-8")
        cfg = settings.load_config(self.path)
        self.assertEqual(cfg["hotkey"], "f9")
        self.assertEqual(cfg["future_key"], [1, 2])
        self.assertEqual(cfg["model"], settings.DEFAULTS["model"])
        cfg["language"] = "en"
        cfg["vocabulary"] = ["Zürich", "naïve"]
        settings.save_config(cfg, self.path)
        again = settings.load_config(self.path)
        self.assertEqual(again, cfg)

    def test_partial_save_keeps_existing_keys(self):
        self.path.write_text(json.dumps({"hotkey": "f9", "other": 1}),
                             encoding="utf-8")
        settings.save_config({"language": "de"}, self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"hotkey": "f9", "other": 1, "language": "de"})

    def test_save_leaves_no_temp_files(self):
        settings.save_config(dict(settings.DEFAULTS), self.path)
        settings.save_config(dict(settings.DEFAULTS), self.path)
        self.assertEqual(os.listdir(self.tmp.name), ["config.json"])

    def test_bad_json_uses_defaults_and_keeps_a_backup(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("holdtotalk.settings", "WARNING"):
            cfg = settings.load_config(self.path)
        self.assertEqual(cfg, settings.DEFAULTS)
        backup = self.path.with_name("config.json.bad")
        self.assertEqual(backup.read_text(encoding="utf-8"), "{not json")

    def test_the_removed_allow_remote_key_is_ignored_warned_once_and_dropped(self):
        self.path.write_text(json.dumps({"cleanup": {
            "allow_remote": True, "url": "http://example.com:11434",
            "model": "m"}}), encoding="utf-8")
        with self.assertLogs("holdtotalk.settings", "WARNING") as lg:
            cfg = settings.load_config(self.path)
        self.assertEqual(len(lg.records), 1)
        self.assertIn("allow_remote", lg.output[0])
        self.assertNotIn("allow_remote", cfg["cleanup"])
        self.assertEqual(cfg["cleanup"]["model"], "m")
        # a non-loopback address is refused whatever the file says
        import cleanup
        self.assertFalse(cleanup.endpoint_info(cfg)["allowed"])
        # the key is still in the file until the next save, which drops it
        self.assertIn("allow_remote", self.path.read_text(encoding="utf-8"))
        settings.save_config(cfg, self.path)
        self.assertNotIn("allow_remote", self.path.read_text(encoding="utf-8"))
        # even a save that does not mention the section drops it
        self.path.write_text(json.dumps({"cleanup": {"allow_remote": True}}),
                             encoding="utf-8")
        settings.save_config({"language": "de"}, self.path)
        self.assertNotIn("allow_remote", self.path.read_text(encoding="utf-8"))

    def test_utf8_bom_is_accepted(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + b'{"hotkey": "f8"}')
        self.assertEqual(settings.load_config(self.path)["hotkey"], "f8")

    def test_defaults_not_mutated(self):
        cfg = settings.load_config(self.path)
        cfg["vocabulary"].append("x")      # shared list would leak
        self.assertEqual(settings.DEFAULTS["vocabulary"], [])


class BadJsonIsNeverOverwritten(unittest.TestCase):
    """Item 1: a JSON error used to be merged into {} and saved over."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.broken = ('{"vocabulary": ["Zorvex"], "hotkey": "f9",  oops'
                       ' "dictionary": []}')
        self.path.write_text(self.broken, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def backups(self):
        return sorted(Path(self.tmp.name).glob("config.json.bad-*"))

    def test_save_refuses_and_keeps_the_file_and_a_backup(self):
        with self.assertRaises(settings.ConfigError) as cm:
            settings.save_config({"language": "de"}, self.path)
        self.assertIn("not changed", str(cm.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.broken)
        [backup] = self.backups()
        self.assertEqual(backup.read_text(encoding="utf-8"), self.broken)
        # Nothing but the file and its backup is left behind.
        self.assertEqual(sorted(os.listdir(self.tmp.name)),
                         sorted(["config.json", backup.name]))

    def test_repeating_does_not_pile_up_identical_backups(self):
        for _ in range(3):
            with self.assertRaises(settings.ConfigError):
                settings.save_config({"language": "de"}, self.path)
        self.assertEqual(len(self.backups()), 1)

    def test_apply_changes_reports_it_instead_of_saving(self):
        class App:
            def reload_config(self):
                raise AssertionError("must not be reached")
        result = settings.apply_changes(App(), {"language": "de"}, self.path)
        self.assertEqual(result["rejected"], ["language"])
        self.assertIn("Could not save", result["errors"][0])
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.broken)

    def test_top_level_list_is_also_refused(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaises(settings.ConfigError):
            settings.save_config({"language": "de"}, self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[1, 2]")

    def test_blank_file_is_simply_replaced(self):
        self.path.write_text("  \n", encoding="utf-8")
        settings.save_config({"language": "de"}, self.path)
        self.assertEqual(json.loads(self.path.read_text("utf-8")),
                         {"language": "de"})
        self.assertEqual(self.backups(), [])

    def test_a_missing_folder_is_created(self):
        deep = Path(self.tmp.name) / "a" / "b" / "config.json"
        settings.save_config({"hotkey": "f8"}, deep)
        self.assertEqual(settings.load_config(deep)["hotkey"], "f8")


class ValueValidation(unittest.TestCase):
    """Item 2: wrongly typed values silently broke dictation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return settings.load_config(self.path)

    def test_the_reviewers_examples_fall_back_with_one_warning_each(self):
        for bad in ("0,5", None, "abc", True, [1]):
            with self.subTest(min_seconds=bad):
                with self.assertLogs("holdtotalk.settings", "WARNING") as lg:
                    cfg = self.load({"min_seconds": bad})
                self.assertEqual(cfg["min_seconds"],
                                 settings.DEFAULTS["min_seconds"])
                self.assertEqual(len(lg.records), 1)
                self.assertIn("min_seconds", lg.output[0])
                if isinstance(bad, str):
                    self.assertNotIn(bad, lg.output[0])   # no values logged
        with self.assertLogs("holdtotalk.settings", "WARNING") as lg:
            cfg = self.load({"language": "English"})
        self.assertIsNone(cfg["language"])
        self.assertEqual(len(lg.records), 1)
        self.assertIn("language", lg.output[0])

    def test_good_values_pass_silently(self):
        data = {"min_seconds": 0.5, "language": None, "hotkey": "f9",
                "remove_fillers": False, "vocabulary": ["Zorvex"],
                "cleanup": {"timeout": 5, "enabled": False},
                "sounds": {"volume": 0.25}, "future_key": {"a": 1}}
        with self.assertNoLogs("holdtotalk.settings", "WARNING"):
            cfg = self.load(data)
        self.assertEqual(cfg["min_seconds"], 0.5)
        self.assertEqual(cfg["cleanup"]["timeout"], 5)
        self.assertIs(cfg["cleanup"]["enabled"], False)
        self.assertEqual(cfg["sounds"], {"enabled": True, "volume": 0.25,
                                         "mute_start": False,
                                         "mute_others": False})
        self.assertEqual(cfg["future_key"], {"a": 1})
        for code in ("en", "de", "EN", " fr "):
            self.assertEqual(self.load({"language": code})["language"],
                             code.strip().lower())

    def test_each_type_is_checked_against_its_default(self):
        clean, problems = settings.validate({
            "remove_fillers": "yes",           # bool
            "vocabulary": "a, b",              # list
            "replacements": [],                # dict
            "model": 3,                        # str
            "device": "tpu",                   # one of auto/cuda/cpu
            "input_device": 1.5,               # None, int or str
            "cleanup": {"timeout": "fast", "min_words": 2.5, "enabled": 1,
                        "url": ""},
            "textrules": {"snippet_mode": "nowhere", "history_limit": 0},
            "overlay": {"color": "red"},
            "learning": "on",                  # a whole section
            "sounds": {"enabled": "no", "volume": [1]},
            "learned": {},
            "min_seconds": float("nan"),
        })
        d = settings.DEFAULTS
        self.assertEqual(clean["remove_fillers"], d["remove_fillers"])
        self.assertEqual(clean["vocabulary"], [])
        self.assertEqual(clean["replacements"], {})
        self.assertEqual(clean["model"], d["model"])
        self.assertEqual(clean["device"], "auto")
        self.assertIsNone(clean["input_device"])
        self.assertEqual(clean["cleanup"], {
            "timeout": d["cleanup"]["timeout"],
            "min_words": d["cleanup"]["min_words"],
            "enabled": d["cleanup"]["enabled"], "url": d["cleanup"]["url"]})
        self.assertEqual(clean["textrules"], {
            "snippet_mode": d["textrules"]["snippet_mode"],
            "history_limit": d["textrules"]["history_limit"]})
        self.assertEqual(clean["overlay"], {"color": d["overlay"]["color"]})
        self.assertEqual(clean["learning"], d["learning"])
        self.assertEqual(clean["sounds"], {"enabled": d["sounds"]["enabled"],
                                           "volume": d["sounds"]["volume"]})
        self.assertEqual(clean["learned"], [])
        self.assertEqual(clean["min_seconds"], d["min_seconds"])
        named = " ".join(problems)
        for key in ("remove_fillers", "vocabulary", "replacements", "model",
                    "device", "input_device", "cleanup.timeout",
                    "cleanup.min_words", "cleanup.enabled", "cleanup.url",
                    "textrules.snippet_mode", "textrules.history_limit",
                    "overlay.color", "learning", "sounds.enabled",
                    "sounds.volume", "learned", "min_seconds"):
            self.assertIn(key, named)

    def test_validate_does_not_modify_its_input_or_add_keys(self):
        cfg = {"min_seconds": "x", "cleanup": {"timeout": "x"}}
        before = json.dumps(cfg)
        clean, problems = settings.validate(cfg)
        self.assertEqual(json.dumps(cfg), before)
        self.assertEqual(set(clean), {"min_seconds", "cleanup"})
        self.assertEqual(len(problems), 2)
        self.assertEqual(settings.validate({})[1], [])

    def test_language_accepts_whisper_codes_and_null_only(self):
        for ok in (None, "en", "zh", "haw"):
            self.assertEqual(settings.validate({"language": ok})[1], [])
        for bad in ("English", "xx", "", 5, ["en"], "auto"):
            clean, problems = settings.validate({"language": bad})
            self.assertIsNone(clean["language"], bad)
            self.assertEqual(len(problems), 1, bad)

    def test_volume_out_of_range_is_clamped(self):
        clean, problems = settings.validate({"sounds": {"volume": 7}})
        self.assertEqual(clean["sounds"]["volume"], 1.0)
        self.assertEqual(len(problems), 1)

    def test_not_a_dict_gives_defaults(self):
        clean, problems = settings.validate([1])
        self.assertEqual(clean, settings.DEFAULTS)
        self.assertTrue(problems)


class SectionsAndNewKeys(unittest.TestCase):
    """Item 17."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        self.assertIsNone(settings.DEFAULTS["pause_hotkey"])
        self.assertEqual(settings.DEFAULTS["sounds"],
                         {"enabled": True, "volume": 0.5,
                          "mute_start": False, "mute_others": False})
        for name in ("sounds", "overlay", "learning", "cleanup", "textrules"):
            self.assertIn(name, settings.SECTIONS)
            self.assertIsInstance(settings.DEFAULTS[name], dict)

    def test_every_section_merges_per_key_on_load_and_save(self):
        partial = {"sounds": {"volume": 0.2}, "overlay": {"color": "#112233"},
                   "learning": {}, "cleanup": {"model": "x:1b"},
                   "textrules": {"snippet_mode": "whole"}}
        self.path.write_text(json.dumps(partial), encoding="utf-8")
        cfg = settings.load_config(self.path)
        self.assertEqual(cfg["sounds"], {"enabled": True, "volume": 0.2,
                                         "mute_start": False,
                                         "mute_others": False})
        self.assertEqual(cfg["overlay"]["background"],
                         settings.OVERLAY_DEFAULTS["background"])
        self.assertEqual(cfg["overlay"]["color"], "#112233")
        self.assertEqual(cfg["learning"], settings.DEFAULTS["learning"])
        self.assertEqual(cfg["cleanup"]["model"], "x:1b")
        self.assertEqual(cfg["cleanup"]["url"], settings.DEFAULTS["cleanup"]["url"])
        self.assertEqual(cfg["textrules"]["snippet_mode"], "whole")
        self.assertIn("history_limit", cfg["textrules"])
        # Saving one key of a section keeps the others in the file.
        settings.save_config({"sounds": {"enabled": False},
                              "learning": {"auto_apply": True}}, self.path)
        data = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(data["sounds"], {"volume": 0.2, "enabled": False})
        self.assertEqual(data["learning"], {"auto_apply": True})
        self.assertEqual(data["cleanup"], {"model": "x:1b"})

    def test_pause_hotkey_change(self):
        v = settings.validate_change
        self.assertEqual(v("pause_hotkey", None), {"pause_hotkey": None})
        self.assertEqual(v("pause_hotkey", ""), {"pause_hotkey": None})
        self.assertEqual(v("pause_hotkey", "F9"), {"pause_hotkey": "f9"})
        with self.assertRaises(ValueError) as cm:
            v("pause_hotkey", "banana")
        self.assertIn("pause hotkey", str(cm.exception))
        with self.assertRaises(ValueError):
            v("pause_hotkey", 5)
        with self.assertRaises(ValueError):
            v("pause_hotkey", "right click")
        main = {"hotkey": "ctrl+windows"}
        with self.assertRaises(ValueError) as cm:
            v("pause_hotkey", "Win + Ctrl", main)       # same keys
        self.assertIn("different", str(cm.exception))
        self.assertEqual(v("pause_hotkey", "f9", main), {"pause_hotkey": "f9"})
        # And the main hotkey may not become the pause hotkey.
        with self.assertRaises(ValueError):
            v("hotkey", "f9", {"pause_hotkey": "f9"})
        self.assertEqual(v("hotkey", "f8", {"pause_hotkey": "f9"}),
                         {"hotkey": "f8"})

    def test_sounds_change(self):
        v = settings.validate_change
        self.assertEqual(v("sounds", {"volume": 5}),
                         {"sounds": {"volume": 1.0}})
        self.assertEqual(v("sounds", {"volume": -1}),
                         {"sounds": {"volume": 0.0}})
        self.assertEqual(v("sounds", {"enabled": False, "volume": 0.3}),
                         {"sounds": {"enabled": False, "volume": 0.3}})
        self.assertEqual(v("sounds", {"mute_others": True}),
                         {"sounds": {"mute_others": True}})
        for bad in ({"enabled": "yes"}, {"volume": "loud"}, {"volume": True},
                    {"mute_others": "yes"}, {"loudness": 1}, {}, "on", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    v("sounds", bad)
        self.assertIsNone(v("something_else", 1))


class LazyLanguages(unittest.TestCase):
    """P3: importing settings must not import faster-whisper (or PyAV)."""

    def test_importing_settings_is_light(self):
        import subprocess
        import sys
        code = ("import sys; sys.dont_write_bytecode = True; "
                "sys.path.insert(0, %r); import settings; "
                "print(sorted(m for m in ('faster_whisper', 'ctranslate2', "
                "'av', 'onnxruntime') if m in sys.modules))" % str(
                    Path(settings.__file__).resolve().parent))
        out = subprocess.run([sys.executable, "-B", "-c", code],
                             capture_output=True, text=True, timeout=120,
                             env=dict(os.environ))
        self.assertEqual(out.stdout.strip(), "[]", out.stderr[-500:])

    def test_builtin_list_matches_the_installed_library(self):
        try:
            from faster_whisper.tokenizer import _LANGUAGE_CODES
        except Exception:
            self.skipTest("faster-whisper is not installed here")
        self.assertEqual(tuple(settings.LANGUAGE_CODES), tuple(_LANGUAGE_CODES))

    def test_the_helpers_still_work(self):
        self.assertEqual(settings.normalize_language("EN - English"), "en")
        self.assertIsNone(settings.normalize_language("auto"))
        with self.assertRaises(ValueError):
            settings.normalize_language("xx")
        codes = [c for c, _ in settings.languages()]
        self.assertEqual(codes[0], "en")
        self.assertIn("yue", codes)


class ComputeTypeSetting(unittest.TestCase):
    """P6: "compute_type": "auto" or a type the engine can be asked for."""

    def test_default_is_auto(self):
        self.assertEqual(settings.DEFAULTS["compute_type"], "auto")
        self.assertEqual(settings.COMPUTE_TYPES[0], "auto")

    def test_accepted_and_rejected_values(self):
        for ok in settings.COMPUTE_TYPES:
            clean, problems = settings.validate({"compute_type": ok})
            self.assertEqual((clean["compute_type"], problems), (ok, []))
        clean, problems = settings.validate({"compute_type": " Int8_Float16 "})
        self.assertEqual((clean["compute_type"], problems),
                         ("int8_float16", []))
        for bad in ("fast", "", 16, None, ["int8"]):
            clean, problems = settings.validate({"compute_type": bad})
            self.assertEqual(clean["compute_type"], "auto", bad)
            self.assertTrue(problems, bad)

class ExcerptsAreScrubbedOnLoad(unittest.TestCase):
    """Item 12 (second part): old records carried dictation excerpts."""

    def test_load_blanks_before_and_after_and_rewrites_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            rec = {"id": "a1", "heard": "sorbex", "wanted": "Zorvex",
                   "before": "Open the sorbex folder", "after": "Open the Zorvex folder"}
            path.write_text(json.dumps({"learned": [rec], "hotkey": "f9"}),
                            encoding="utf-8")
            cfg = settings.load_config(path)
            self.assertEqual(cfg["learned"][0]["before"], "")
            self.assertEqual(cfg["learned"][0]["after"], "")
            self.assertEqual(cfg["learned"][0]["heard"], "sorbex")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Open the", text)
            self.assertEqual(json.loads(text)["hotkey"], "f9")

if __name__ == "__main__":
    unittest.main()
