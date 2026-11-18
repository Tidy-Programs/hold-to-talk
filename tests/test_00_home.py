"""Runs first (the name sorts before test_a...): points HOLDTOTALK_HOME at a
temporary folder before any project module is imported, so no test in the
suite can reach the real config, history or log."""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)

import settings  # noqa: E402  (after the environment is set)


class DataFolderIsTemporary(unittest.TestCase):
    def test_user_data_paths_are_inside_the_temporary_home(self):
        # Only meaningful when this module was imported first (the full
        # discover run); a single module run sets its own home.
        home = Path(os.environ["HOLDTOTALK_HOME"]).resolve()
        for path in (settings.CONFIG_PATH, settings.HISTORY_PATH,
                     settings.LOG_PATH):
            self.assertTrue(Path(path).resolve().is_relative_to(home), path)
        self.assertNotEqual(Path(settings.DATA_DIR).resolve(),
                            settings.APP_DIR)


if __name__ == "__main__":
    unittest.main()
