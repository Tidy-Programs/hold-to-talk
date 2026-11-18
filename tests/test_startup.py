"""E11: start-up order (lock, then migration, then config), data paths, the
test-instance rules, --uninstall and the self-correcting Start with Windows
entry. The registry, the lock and the migration are all faked; the code
folder is replaced by a temporary one wherever files could be renamed."""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import dictate
import settings
import tray


class Stop(Exception):
    pass


class Order(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-start-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "dictate.log").write_text("old log\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "HOLDTOTALK_HOME"}
        for p in (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(settings, "APP_DIR", self.tmp),
                  mock.patch.object(settings, "DATA_DIR", self.tmp / "data"),
                  mock.patch.object(settings, "migrate_legacy_data",
                                    self.fake_migrate),
                  mock.patch.object(settings, "ensure_data_dirs",
                                    lambda: self.calls.append("dirs")),
                  mock.patch.object(settings, "load_config",
                                    self.fake_load),
                  mock.patch.object(dictate, "open_running",
                                    lambda suffix: False)):
            p.start()
            self.addCleanup(p.stop)
        self.migrate_result = {"config": "migrated", "history": "migrated",
                               "renamed": ["config.json.migrated"],
                               "errors": []}

    def fake_migrate(self, app_dir, data_dir):
        self.calls.append(("migrate", Path(app_dir), Path(data_dir)))
        return self.migrate_result

    def fake_load(self, path=None):
        self.calls.append("load_config")
        raise Stop()

    def lock(self, free):
        def single(suffix):
            self.calls.append(("lock", suffix))
            return free
        return mock.patch.object(dictate, "_single_instance", single)

    args = types.SimpleNamespace(dry_run=True, ui_port=0, open=False,
                                 log_text=False)

    def test_second_start_reads_and_migrates_nothing(self):
        with self.lock(False):
            rc = dictate.run_app(self.args, False, self.tmp / "x.log")
        self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [("lock", "")])

    def test_lock_then_migrate_then_config(self):
        with self.lock(True), self.assertRaises(Stop):
            dictate.run_app(self.args, False, self.tmp / "x.log")
        self.assertEqual(self.calls, [
            ("lock", ""), "dirs",
            ("migrate", self.tmp, self.tmp / "data"), "load_config"])
        # The old log (it may hold dictated text) is retired, not deleted.
        self.assertFalse((self.tmp / "dictate.log").exists())
        self.assertEqual((self.tmp / "dictate.log.migrated")
                         .read_text(encoding="utf-8"), "old log\n")

    def test_migration_problem_is_told_plainly(self):
        self.migrate_result = {"config": "migrated", "history": "failed",
                               "renamed": [], "errors": ["history: locked"]}
        with self.lock(True), self.assertRaises(Stop), \
                self.assertLogs("holdtotalk", "ERROR") as logs:
            dictate.run_app(self.args, False, self.tmp / "x.log")
        self.assertIn("history from the previous version could not be "
                      "moved", logs.output[-1])
        self.assertTrue((self.tmp / "dictate.log").exists())   # kept

    def test_test_instance_never_migrates(self):
        with self.lock(True), self.assertRaises(Stop):
            dictate.run_app(self.args, False, self.tmp / "x.log",
                            self.tmp / "h.sqlite3", test=True)
        self.assertNotIn("migrate", [c[0] for c in self.calls
                                     if isinstance(c, tuple)])

    def test_holdtotalk_home_never_migrates(self):
        with mock.patch.dict(os.environ, {"HOLDTOTALK_HOME": str(self.tmp)}):
            self.assertIsNone(dictate.migrate_once())
        self.assertEqual(self.calls, [])


class Preload(unittest.TestCase):
    """P3: the speech libraries are imported off the main thread, early."""

    def fake_modules(self, order, stt_error=None):
        def dlls():
            order.append(threading.get_ident())
            if stt_error:
                raise stt_error
        return {"stt": types.SimpleNamespace(add_cuda_dll_dirs=dlls),
                "ctranslate2": types.ModuleType("ctranslate2"),
                "faster_whisper": types.ModuleType("faster_whisper")}

    def test_runs_on_a_background_thread(self):
        order = []
        with mock.patch.dict(sys.modules, self.fake_modules(order)):
            thread = dictate._preload_engine()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(thread.daemon)
        self.assertEqual(len(order), 1)
        self.assertNotEqual(order[0], threading.get_ident())

    def test_a_problem_is_left_for_the_engine_load_to_report(self):
        order = []
        mods = self.fake_modules(order, stt_error=OSError("no cuda"))
        with mock.patch.dict(sys.modules, mods):
            thread = dictate._preload_engine()
            thread.join(5)
        self.assertFalse(thread.is_alive())          # and nothing raised

    def test_it_starts_before_the_window_modules_are_imported(self):
        import inspect
        source = inspect.getsource(dictate.run_app)
        self.assertLess(source.index("_preload_engine()"),
                        source.index("import api"))

class Paths(unittest.TestCase):
    def test_real_instance_uses_the_data_folder(self):
        cfg, hist, log, test = dictate.data_paths()
        self.assertFalse(test)
        self.assertEqual((cfg, hist, log), (settings.CONFIG_PATH,
                                            settings.HISTORY_PATH,
                                            settings.LOG_PATH))
        self.assertEqual(dictate.LOG_PATH, settings.LOG_PATH)

    def test_any_override_makes_a_self_contained_test_instance(self):
        base = Path(tempfile.gettempdir()) / "htt-paths-x"
        for kw in ({"config": base / "c.json"},
                   {"history": base / "h.sqlite3"},
                   {"log_file": base / "l.log"}):
            with self.subTest(kw=list(kw)):
                cfg, hist, log, test = dictate.data_paths(**kw)
                self.assertTrue(test)
                for p in (cfg, hist, log):
                    self.assertEqual(p.parent, base.resolve())

    def test_instance_suffix(self):
        self.assertEqual(dictate.instance_suffix(dictate.DEFAULT_CONFIG), "")
        self.assertRegex(dictate.instance_suffix(
            Path(tempfile.gettempdir()) / "c.json"), r"^-[0-9a-f]{8}$")


class Autostart(unittest.TestCase):
    def setUp(self):
        self.reg = {}
        self.disabled = False
        self.sets = []
        for name, fn in (
                ("get_autostart", lambda n: self.reg.get(n)),
                ("set_autostart", self.fake_set),
                ("autostart_command",
                 lambda *a: '"C:\\venv\\pythonw.exe" "C:\\new\\dictate.py"'),
                ("_startup_disabled", lambda n: self.disabled)):
            p = mock.patch.object(tray, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def fake_set(self, enabled, name, command=None):
        self.sets.append((enabled, name, command))
        if enabled:
            self.reg[name] = command
        else:
            self.reg.pop(name, None)

    def installed_here(self):
        """This copy of the code is the installed one."""
        return mock.patch.object(dictate, "install_dirs",
                                 lambda: [str(settings.APP_DIR).lower()])

    def test_entry_for_another_folder_is_corrected(self):
        self.reg["HoldToTalk"] = '"C:\\venv\\pythonw.exe" "C:\\old\\dictate.py"'
        with self.installed_here(), self.assertLogs("holdtotalk", "INFO"):
            self.assertIsNotNone(dictate.fix_autostart("HoldToTalk"))
        self.assertIn("C:\\new\\", self.reg["HoldToTalk"])

    def test_a_source_checkout_does_not_take_the_entry_over(self):
        # L1: running start.bat from a checkout used to re-point it here.
        entry = '"C:\\venv\\pythonw.exe" "C:\\installed\\app\\dictate.py"'
        self.reg["HoldToTalk"] = entry
        with mock.patch.object(dictate, "install_dirs",
                               lambda: ["c:\\installed\\app"]), \
                self.assertLogs("holdtotalk", "INFO") as logs:
            self.assertIsNone(dictate.fix_autostart("HoldToTalk"))
        self.assertEqual(self.reg["HoldToTalk"], entry)
        self.assertEqual(self.sets, [])
        self.assertEqual(len(logs.output), 1)         # one line, once
        self.assertIn("left alone", logs.output[0])

    def test_the_installed_copy_corrects_a_moved_entry(self):
        self.reg["HoldToTalk"] = '"C:\\venv\\pythonw.exe" "D:\\gone\\dictate.py"'
        here = str(settings.APP_DIR).lower()
        with mock.patch.object(dictate, "install_dirs", lambda: [here]):
            self.assertIsNotNone(dictate.fix_autostart("HoldToTalk"))
        self.assertEqual(len(self.sets), 1)

    def test_an_entry_that_already_points_at_this_folders_install(self):
        # The entry's folder is this folder's parent: still the same install.
        parent = settings.APP_DIR.parent / "dictate.py"
        self.reg["HoldToTalk"] = f'"C:\\old\\pythonw.exe" "{parent}"'
        with mock.patch.object(dictate, "install_dirs", lambda: []):
            self.assertIsNotNone(dictate.fix_autostart("HoldToTalk"))

    def test_install_dirs_follow_the_installer(self):
        import winreg
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Fake\\Local"}), \
                mock.patch.object(winreg, "OpenKey", side_effect=OSError):
            self.assertEqual(
                [d.replace("/", "\\") for d in dictate.install_dirs()],
                [str(Path("C:\\Fake\\Local\\Programs\\HoldToTalk")
                     .resolve()).lower()])

    def test_left_alone(self):
        for value, disabled in (
                (None, False),
                ('"C:\\venv\\pythonw.exe" "C:\\new\\dictate.py"', False),
                ('"C:\\other\\app.exe"', False),
                ('"C:\\venv\\pythonw.exe" "C:\\old\\dictate.py"', True)):
            with self.subTest(value=value, disabled=disabled), \
                    self.installed_here():
                self.reg = {"HoldToTalk": value} if value else {}
                self.disabled = disabled
                self.assertIsNone(dictate.fix_autostart("HoldToTalk"))
        self.assertEqual(self.sets, [])

    def test_uninstall_removes_the_entry_and_deletes_nothing(self):
        self.reg["HoldToTalk"] = "x dictate.py"
        data = Path(tempfile.mkdtemp(prefix="htt-uninst-"))
        self.addCleanup(shutil.rmtree, data, True)
        (data / "config.json").write_text("{}", encoding="utf-8")
        lines = []
        with mock.patch.object(settings, "DATA_DIR", data):
            self.assertEqual(dictate.cmd_uninstall(out=lines.append), 0)
        self.assertEqual(self.sets, [(False, "HoldToTalk", None)])
        self.assertTrue((data / "config.json").exists())
        text = "\n".join(lines)
        for word in ("removed", str(data), "Speech models",
                     "Nothing else was deleted"):
            self.assertIn(word, text)

    def test_uninstall_of_a_test_instance_changes_nothing(self):
        lines = []
        dictate.cmd_uninstall(test=True, out=lines.append)
        self.assertEqual(self.sets, [])

    def test_main_log_text_flag_is_loud_and_off_by_default(self):
        tmp = Path(tempfile.mkdtemp(prefix="htt-main-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        saved = (settings.CONFIG_PATH, settings.HISTORY_PATH,
                 settings.LOG_PATH, settings.DATA_DIR)
        try:
            with self.assertLogs("holdtotalk", "WARNING") as logs, \
                    mock.patch("builtins.print"):
                rc = dictate.main(["--uninstall", "--log-text", "--config",
                                   str(tmp / "c.json")])
            self.assertEqual(rc, 0)
            self.assertIn("--log-text is ON", "\n".join(logs.output))
            self.assertEqual(self.sets, [])      # test instance
            with self.assertNoLogs("holdtotalk", "WARNING"), \
                    mock.patch("builtins.print"):
                dictate.main(["--uninstall", "--config", str(tmp / "c.json")])
        finally:
            (settings.CONFIG_PATH, settings.HISTORY_PATH,
             settings.LOG_PATH, settings.DATA_DIR) = saved


if __name__ == "__main__":
    unittest.main()
