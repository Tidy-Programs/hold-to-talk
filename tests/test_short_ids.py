"""The short identifiers made with SHA-1 (dictate.instance_suffix and
learn._sid) are not security, so the calls say usedforsecurity=False for
security scanners. The values must stay exactly as they were: stored
identifiers depend on them. The expected values were computed with the code
before that argument was added."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import re
import unittest
from pathlib import Path

import dictate
import learn

ROOT = Path(__file__).resolve().parent.parent


class ShortIds(unittest.TestCase):
    def test_instance_suffix_is_unchanged(self):
        for path, want in ((r"C:\htt-fixed\a\config.json", "-a02b3dcc"),
                           (r"C:\Zorvex\Config.JSON", "-0f1216c5")):
            self.assertEqual(dictate.instance_suffix(path), want)

    def test_sid_is_unchanged(self):
        for args, want in ((("dictionary", "zor vex", "Zorvex"), "3b5974a0ff03"),
                           (("vocab", "Zorvex", "Zorvex"), "49dd7607c2e7"),
                           (("dictionary", "Caf\u00e9 STRASSE", "Stra\u00dfe"),
                            "31312d40a34b")):
            self.assertEqual(learn._sid(*args), want)

    def test_sha1_is_marked_as_not_for_security(self):
        for name in ("dictate.py", "learn.py"):
            text = (ROOT / name).read_text(encoding="utf-8")
            calls = re.findall(r"hashlib\.sha1\((.*?)\)\.hexdigest", text, re.S)
            self.assertTrue(calls, name)
            for call in calls:
                self.assertIn("usedforsecurity=False", call, name)


if __name__ == "__main__":
    unittest.main()
