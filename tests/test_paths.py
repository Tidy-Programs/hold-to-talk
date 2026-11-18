"""Item 3: user data lives outside the source folder; legacy data moves over
safely. Everything here uses temporary folders and invented data."""

import atexit
import os
import shutil
import tempfile

if "htt-test-home-" not in os.environ.get("HOLDTOTALK_HOME", ""):
    _home = tempfile.mkdtemp(prefix="htt-test-home-")
    os.environ["HOLDTOTALK_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)

import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

import settings
import textrules


class Locations(unittest.TestCase):
    def test_default_is_under_local_app_data(self):
        env = {"LOCALAPPDATA": r"C:\Fake\Local"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("HOLDTOTALK_HOME", None)
            self.assertEqual(settings._default_data_dir(),
                             Path(r"C:\Fake\Local") / "HoldToTalk" / "data")

    def test_override_is_the_data_directory_itself(self):
        with mock.patch.dict(os.environ, {"HOLDTOTALK_HOME": r"C:\Elsewhere"}):
            self.assertEqual(settings._default_data_dir(),
                             Path(r"C:\Elsewhere"))

    def test_constants_are_derived_from_the_data_dir(self):
        self.assertEqual(settings.CONFIG_PATH, settings.DATA_DIR / "config.json")
        self.assertEqual(settings.HISTORY_PATH,
                         settings.DATA_DIR / "history.sqlite3")
        self.assertEqual(settings.LOG_PATH,
                         settings.DATA_DIR / "logs" / "dictate.log")
        self.assertEqual(settings.APP_DIR,
                         Path(settings.__file__).resolve().parent)

    def test_tests_run_against_a_temporary_data_dir(self):
        self.assertIn("htt-test-home-", str(settings.DATA_DIR))
        self.assertEqual(textrules.default_history_path(),
                         settings.HISTORY_PATH)

    def test_directories_are_created_on_demand(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "d"
            with mock.patch.object(settings, "DATA_DIR", data), \
                    mock.patch.object(settings, "LOG_PATH",
                                      data / "logs" / "dictate.log"):
                self.assertFalse(data.exists())
                settings.ensure_data_dirs()
                self.assertTrue((data / "logs").is_dir())
            h = textrules.History(Path(tmp) / "x" / "y" / "h.sqlite3")
            try:
                h.record("a", "b")
                self.assertTrue(h.flush())
                self.assertTrue((Path(tmp) / "x" / "y" / "h.sqlite3").exists())
            finally:
                h.close()


def make_history(path, rows, wal=False):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts REAL NOT NULL, raw TEXT NOT NULL, final TEXT NOT NULL,"
                 " seconds REAL)")
    for i in range(rows):
        conn.execute("INSERT INTO history (ts, raw, final, seconds) VALUES"
                     " (?, ?, ?, 1.0)", (float(i), f"raw {i}", f"Zorvex {i}"))
    conn.commit()
    return conn


class Migration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Path(self.tmp.name) / "app"
        self.data = Path(self.tmp.name) / "data"
        self.app.mkdir()
        self.cfg_text = json.dumps({"hotkey": "f9", "vocabulary": ["Zorvex"]})
        self.conns = []

    def tearDown(self):
        for c in self.conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self.tmp.cleanup()

    def migrate(self):
        return settings.migrate_legacy_data(self.app, self.data)

    def names(self, folder):
        return sorted(p.name for p in folder.iterdir())

    def test_moves_config_and_history_and_renames_the_old_ones(self):
        (self.app / "config.json").write_text(self.cfg_text, encoding="utf-8")
        make_history(self.app / "history.sqlite3", 7).close()
        out = self.migrate()
        self.assertEqual(out["config"], "migrated")
        self.assertEqual(out["history"], "migrated")
        self.assertEqual(out["errors"], [])
        self.assertEqual(sorted(out["renamed"]),
                         ["config.json.migrated", "history.sqlite3.migrated"])
        self.assertEqual((self.data / "config.json").read_text("utf-8"),
                         self.cfg_text)
        conn = sqlite3.connect(self.data / "history.sqlite3")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM history")
                         .fetchone()[0], 7)
        conn.close()
        # Nothing was deleted: the old data is still there, renamed.
        self.assertEqual(self.names(self.app),
                         ["config.json.migrated", "history.sqlite3.migrated"])
        self.assertEqual((self.app / "config.json.migrated").read_text("utf-8"),
                         self.cfg_text)

    def test_a_wal_history_leaves_no_copying_files_behind(self):
        # L3: verifying the copy opened it in WAL mode, which made
        # history.sqlite3.copying-shm / -wal that the rename did not move.
        (self.app / "config.json").write_text(self.cfg_text, encoding="utf-8")
        make_history(self.app / "history.sqlite3", 5, wal=True).close()
        out = self.migrate()
        self.assertEqual(out["history"], "migrated")
        self.assertEqual(self.names(self.data),
                         ["config.json", "history.sqlite3"])
        conn = sqlite3.connect(self.data / "history.sqlite3")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM history")
                         .fetchone()[0], 5)
        conn.close()

    def test_a_failed_copy_leaves_nothing_behind(self):
        make_history(self.app / "history.sqlite3", 3, wal=True).close()
        with mock.patch.object(settings, "_row_count", return_value=99):
            out = self.migrate()
        self.assertEqual(out["history"], "failed")
        self.assertEqual(self.names(self.data), [])
    def test_second_run_does_nothing(self):
        (self.app / "config.json").write_text(self.cfg_text, encoding="utf-8")
        make_history(self.app / "history.sqlite3", 3).close()
        self.migrate()
        before = (self.names(self.app), self.names(self.data))
        out = self.migrate()
        self.assertEqual(out, {"config": "none", "history": "none",
                               "renamed": [], "errors": []})
        self.assertEqual((self.names(self.app), self.names(self.data)), before)

    def test_nothing_to_migrate(self):
        self.assertEqual(self.migrate(), {"config": "none", "history": "none",
                                          "renamed": [], "errors": []})
        self.assertFalse(self.data.exists())

    def test_existing_new_files_are_never_overwritten(self):
        self.data.mkdir()
        (self.data / "config.json").write_text('{"hotkey": "f1"}',
                                               encoding="utf-8")
        make_history(self.data / "history.sqlite3", 2).close()
        (self.app / "config.json").write_text(self.cfg_text, encoding="utf-8")
        make_history(self.app / "history.sqlite3", 9).close()
        out = self.migrate()
        self.assertEqual((out["config"], out["history"]),
                         ("kept_new", "kept_new"))
        self.assertEqual(out["renamed"], [])
        self.assertEqual((self.data / "config.json").read_text("utf-8"),
                         '{"hotkey": "f1"}')
        conn = sqlite3.connect(self.data / "history.sqlite3")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM history")
                         .fetchone()[0], 2)
        conn.close()
        self.assertEqual(self.names(self.app),
                         ["config.json", "history.sqlite3"])      # untouched

    def test_locked_old_history_changes_nothing(self):
        settings.BACKUP_WAIT, saved = 0.3, settings.BACKUP_WAIT
        self.addCleanup(setattr, settings, "BACKUP_WAIT", saved)
        make_history(self.app / "history.sqlite3", 4).close()
        # A rollback-journal database under an exclusive lock cannot be read.
        locker = sqlite3.connect(self.app / "history.sqlite3", timeout=0.1,
                                 isolation_level=None)
        self.conns.append(locker)
        locker.execute("BEGIN EXCLUSIVE")
        out = self.migrate()
        self.assertEqual(out["history"], "failed")
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["renamed"], [])
        self.assertFalse((self.data / "history.sqlite3").exists())
        self.assertEqual([n for n in self.names(self.data) if "copying" in n],
                         [] if self.data.exists() else [])
        self.assertTrue((self.app / "history.sqlite3").exists())
        locker.execute("ROLLBACK")
        locker.close()
        # Once the lock is gone the next start-up finishes the job.
        out = self.migrate()
        self.assertEqual(out["history"], "migrated")

    def test_rows_still_in_the_write_ahead_log_are_copied(self):
        old = self.app / "history.sqlite3"
        writer = make_history(old, 25, wal=True)       # stays open: WAL kept
        self.conns.append(writer)
        wal = Path(str(old) + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        # A plain file copy of the main file would miss the rows.
        plain = Path(self.tmp.name) / "plain.sqlite3"
        shutil.copyfile(old, plain)
        probe = sqlite3.connect(plain)
        try:
            lost = probe.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        except sqlite3.Error:
            lost = 0
        finally:
            probe.close()
        self.assertLess(lost, 25)
        out = self.migrate()
        self.assertIn(out["history"], ("copied", "migrated"))
        conn = sqlite3.connect(self.data / "history.sqlite3")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM history")
                         .fetchone()[0], 25)
        conn.close()

    def test_wal_companions_follow_the_renamed_file(self):
        old = self.app / "history.sqlite3"
        writer = make_history(old, 5, wal=True)
        writer.close()                       # clean close removes the WAL
        out = self.migrate()
        self.assertEqual(out["history"], "migrated")
        self.assertFalse(old.exists())
        self.assertFalse(Path(str(old) + "-wal").exists())

    def test_an_old_name_that_is_taken_is_not_overwritten(self):
        (self.app / "config.json.migrated").write_text("older", "utf-8")
        (self.app / "config.json").write_text(self.cfg_text, encoding="utf-8")
        out = self.migrate()
        self.assertEqual(out["config"], "migrated")
        self.assertEqual((self.app / "config.json.migrated").read_text("utf-8"),
                         "older")
        self.assertEqual(out["renamed"], ["config.json.migrated-2"])

    def test_default_arguments_are_the_real_folders_but_are_not_used_here(self):
        # Documented defaults only; this test never calls it on them.
        import inspect
        sig = inspect.signature(settings.migrate_legacy_data)
        self.assertEqual(list(sig.parameters), ["app_dir", "data_dir"])


if __name__ == "__main__":
    unittest.main()
