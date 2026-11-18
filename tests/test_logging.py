"""Item 9: the modules never write dictated text, vocabulary, dictionary or
snippet contents to the log. A static check over every log call, so a new
one cannot slip in."""

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("cleanup", "pipeline", "textrules", "learn", "settings", "stt",
           "sounds")
LEVELS = {"debug", "info", "warning", "warn", "error", "exception",
          "critical", "log"}
# Names that hold what the user said or configured.
FORBIDDEN = {"text", "raw", "final", "out", "original", "corrected", "heard",
             "wanted", "term", "terms", "trigger", "vocab", "vocabulary",
             "words", "dropped", "novel", "entries", "entry", "snippet",
             "snippets", "reply", "detail", "payload", "prompt", "sample",
             "content", "audio", "cfg", "config"}


def log_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("log", "logging")):
            yield node


def names_outside_len(node):
    """Variable names in an expression, except inside len(...) (lengths and
    counts are fine to log)."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "len"):
        return
    if isinstance(node, ast.Name):
        yield node.id
    for child in ast.iter_child_nodes(node):
        yield from names_outside_len(child)


class NoDictationInLogs(unittest.TestCase):
    def test_no_log_call_mentions_user_content(self):
        found = 0
        for name in MODULES:
            tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
            for call in log_calls(tree):
                found += 1
                for arg in call.args[1:]:
                    for node_id in names_outside_len(arg):
                        self.assertNotIn(
                            node_id, FORBIDDEN,
                            f"{name}.py line {call.lineno}: log call uses "
                            f"{node_id!r}")
                self.assertFalse(
                    any(isinstance(n, ast.JoinedStr)
                        for n in ast.walk(call.args[0])) if call.args else 0,
                    f"{name}.py line {call.lineno}: f-string in a log call")
        self.assertGreater(found, 20)           # the check really looked

    def test_exceptions_are_logged_by_class_not_traceback(self):
        # log.exception() would print the exception message (which can
        # quote a dictation) and the traceback; only the class is logged.
        for name in ("cleanup", "pipeline", "textrules"):
            tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
            for call in log_calls(tree):
                self.assertNotEqual(call.func.attr, "exception",
                                    f"{name}.py line {call.lineno}")


if __name__ == "__main__":
    unittest.main()
