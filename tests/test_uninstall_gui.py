"""The uninstaller window (uninstall-gui.ps1), the new switches of uninstall.ps1 and
how uninstall.bat opens the window.

The window is driven without a person through its test switch -AutoRun, against the
fake engine in tests/fixtures (which changes nothing at all) and once, together with
the setup window, against the real engines. Every run passes all five test overrides
into a temporary folder and a throw-away key under HKCU\\Software\\HoldToTalkTest; the
real install uses -NoDeps -SkipModel -Gpu no. The windows of these runs are placed
off-screen and never take the focus. Nothing here touches the installed app.
"""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path

from test_install import ps_parse_errors
from test_install_gui import (HAVE_PS, HAVE_WPF, POWERSHELL, TEST_KEY_ROOT,
                              clean_env, delete_key_tree, run_bat, run_ps)

ROOT = Path(__file__).resolve().parent.parent
GUI = ROOT / "uninstall-gui.ps1"
SETUP_GUI = ROOT / "install-gui.ps1"
ENGINE = ROOT / "uninstall.ps1"
FAKE = ROOT / "tests" / "fixtures" / "fake_uninstall_engine.ps1"
BAT = ROOT / "uninstall.bat"


class ScriptFiles(unittest.TestCase):
    def test_files_are_ascii(self):
        for path in (GUI, FAKE, ENGINE, BAT):
            data = path.read_bytes()
            self.assertTrue(all(b < 128 for b in data), path.name)
        bat = BAT.read_bytes()
        self.assertEqual(bat.count(b"\n"), bat.count(b"\r\n"), "uninstall.bat: CRLF")

    @unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
    def test_scripts_parse(self):
        self.assertEqual(ps_parse_errors([GUI, FAKE, ENGINE]), "")

    def test_it_is_installed_and_in_scope(self):
        listed = (ROOT / "install-files.txt").read_text(encoding="ascii").splitlines()
        self.assertIn("uninstall-gui.ps1", listed)
        self.assertEqual(listed.index("uninstall-gui.ps1"),
                         listed.index("uninstall.ps1") + 1)
        self.assertIn("`uninstall-gui.ps1`",
                      (ROOT / "SECURITY.md").read_text(encoding="utf-8"))

    def test_the_window_does_not_remove_anything_itself(self):
        text = GUI.read_text(encoding="ascii")
        # it starts the engine as a hidden child process with explicit answers
        for needle in ("'-Check'", "'-Progress'", "'-DeleteUserData'", "'@@htt '",
                       "CreateNoWindow = $true", "ReadLineAsync()", "DispatcherTimer",
                       "RedirectStandardInput = $true", "StandardInput.Close()"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"Add_OutputDataReceived|Add_ErrorDataReceived")
        # the only places that delete are the launcher's reason file and its own
        # temporary copy
        calls = [m.start() for m in re.finditer(r"Remove-Item\b", text)]
        funcs = re.findall(r"\nfunction ([\w-]+)", text)
        self.assertTrue(funcs)
        for pos in calls:
            before = text[:pos]
            owner = re.findall(r"\nfunction ([\w-]+)", before)
            owner = owner[-1] if owner else "(script)"
            inside_launcher_or_temp = owner in ("Invoke-Launcher", "Remove-OwnTempCopy")
            line = text[text.rfind("\n", 0, pos) + 1:text.find("\n", pos)]
            self.assertTrue(
                inside_launcher_or_temp or "ReasonFile" in line,
                f"Remove-Item outside the launcher / its own temp copy: {line.strip()}")
        for banned in ("Remove-PathWithRetry", "Remove-AppDataFiles", "Remove-ItemProperty",
                       "Stop-Process"):
            self.assertNotIn(banned, text)

    def test_the_data_box_starts_unticked_and_only_a_guarded_helper_ticks_it(self):
        text = GUI.read_text(encoding="ascii")
        m = re.search(r'<CheckBox x:Name="DataBox"[^>]*>', text)
        self.assertIsNotNone(m)
        self.assertNotIn("IsChecked", m.group(0))
        self.assertIn("Also delete my settings, dictation history and logs "
                      "(this cannot be undone)", text)
        helper = re.search(r"function Set-DataBoxForTest\b.*?\n}\n", text, re.S)
        self.assertIsNotNone(helper)
        body = helper.group(0)
        self.assertIn("throw 'Refused", body)
        self.assertIn("$script:OverridesComplete", body)
        outside = text.replace(body, "")
        for line in outside.splitlines():
            if re.search(r"DataBox\.(IsChecked\s*=(?!=)|SetValue|SetCurrentValue)", line):
                self.assertRegex(line, r"DataBox\.IsChecked\s*=\s*\$false",
                                 f"the program may only untick the box: {line}")
            self.assertNotRegex(line, r"DataBox.*(Toggle\(\)|ToggleButtonAutomationPeer)")
        # the answer is yes only for a ticked AND confirmed box
        self.assertRegex(text, r"\$yes = \(\$ui\.DataBox\.IsChecked -eq \$true\) -and "
                               r"\$script:DataConfirmed")
        self.assertIn("Delete your settings and history for good?", text)

    def test_it_uses_the_look_of_the_setup_window(self):
        text = GUI.read_text(encoding="ascii")
        setup = SETUP_GUI.read_text(encoding="ascii")
        # the copied resources are the same as the setup window's
        a = setup.index("<Window.Resources>")
        b = setup.index("</Window.Resources>")
        self.assertIn(setup[a:b], text)
        pal = re.search(r"\$Palettes = @\{.*?\n\}\n", setup, re.S).group(0)
        self.assertIn(pal, text)
        self.assertIn("FontFamily=\"Segoe UI Variable Text, Segoe UI\"", text)
        self.assertNotIn(". (Join-Path $Here 'install-gui.ps1')", text)
        for shown in re.findall(r'(?:Text|Content|Header)="([^"]*)"', text):
            self.assertNotIn("!", shown)

    def test_nothing_personal_in_the_files(self):
        user = os.environ.get("USERNAME", "\0").lower()
        for path in (GUI, FAKE, BAT, ENGINE):
            text = path.read_text(encoding="ascii").lower()
            self.assertNotIn("\\users\\" + user + "\\", text, path.name)
            self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[a-z]{2,}", path.name)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class SelfTest(unittest.TestCase):
    def test_selftest_passes(self):
        code, out = run_ps([GUI, "-SelfTest"])
        self.assertEqual(code, 0, out)
        self.assertIn("selftest ok", out)

    def test_the_window_deletes_its_own_temporary_copy_and_nothing_else(self):
        scratch = Path(tempfile.mkdtemp(prefix="htt-ut-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        env = clean_env(TEMP=str(scratch), TMP=str(scratch))
        for name, expect_gone in (("htt-uninstall-12345", True),
                                  ("some-other-folder", False),
                                  ("htt-uninstall-abc", False)):
            with self.subTest(folder=name):
                folder = scratch / name
                folder.mkdir()
                shutil.copy(GUI, folder / "uninstall-gui.ps1")
                shutil.copy(ROOT / "app.ico", folder / "app.ico")
                for name in ("uninstall.ps1", "htt-common.ps1"):
                    shutil.copy(ROOT / name, folder / name)
                code, out = run_ps([folder / "uninstall-gui.ps1", "-SelfTest"], env=env)
                self.assertEqual(code, 0, out)
                self.assertIn("selftest ok", out)
                self.assertEqual(not folder.exists(), expect_gone, out)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class AutoRunWithTheFakeEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-ugui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.record = self.tmp / "record.txt"
        self.key = fr"{TEST_KEY_ROOT}\ugui-{uuid.uuid4().hex[:8]}\Uninstall\HoldToTalk"
        self.overrides = ["-InstallRoot", self.tmp / "prog",
                          "-EnvDir", self.tmp / "home" / "venv",
                          "-StartMenuDir", self.tmp / "sm",
                          "-DesktopDir", self.tmp / "dt", "-RegKey", self.key]

    def auto(self, scenario, mode, overrides=None, extra=()):
        env = clean_env(HTT_FAKE_SCENARIO=scenario, HTT_FAKE_RECORD=str(self.record))
        if overrides is None:
            overrides = self.overrides
        code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", mode, *extra, *overrides],
                           env=env)
        runs = []
        if self.record.exists():
            runs = [json.loads(l) for l in
                    self.record.read_text(encoding="ascii").splitlines() if l]
        return code, out, runs

    def progress(self, runs):
        prog = [r for r in runs if r["mode"] == "progress"]
        self.assertEqual(len(prog), 1, runs)
        return prog[0]["params"]

    def test_the_default_answer_about_the_data_is_no(self):
        code, out, runs = self.auto("ok", "remove")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertEqual(runs[0]["mode"], "check")
        p = self.progress(runs)
        self.assertEqual(p["DeleteUserData"], "no")
        self.assertNotIn("DeleteData", p)
        self.assertNotIn("Yes", p)
        # the overrides reach the engine unchanged
        self.assertEqual(p["RegKey"], self.key)
        self.assertEqual(Path(p["InstallRoot"]), self.tmp / "prog")
        self.assertIn("data=no", out)
        self.assertFalse((self.tmp / "prog").exists())      # the fake changes nothing

    def test_a_ticked_and_confirmed_box_gives_yes(self):
        code, out, runs = self.auto("ok", "remove-data")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.progress(runs)["DeleteUserData"], "yes")
        self.assertIn("data=yes", out)

    def test_ticking_and_then_choosing_keep_gives_no(self):
        code, out, runs = self.auto("ok", "remove-keep")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.progress(runs)["DeleteUserData"], "no")

    def test_cancel_exits_3_and_removes_nothing(self):
        code, out, runs = self.auto("ok", "cancel")
        self.assertEqual(code, 3, out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_the_bat_dir_is_passed_on_when_given(self):
        code, out, runs = self.auto("ok", "remove", extra=["-BatDir", self.tmp / "prog"])
        self.assertEqual(code, 0, out)
        self.assertEqual(Path(self.progress(runs)["BatDir"]), self.tmp / "prog")

    def test_failure_exits_1_with_the_engine_text(self):
        code, out, runs = self.auto("fail", "remove")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)
        self.assertIn("Could not remove the shortcuts.", out)

    def test_a_crash_without_a_marker_is_a_failure(self):
        code, out, runs = self.auto("crash", "remove")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)

    def test_a_failing_check_shows_the_error_and_exits_1(self):
        code, out, runs = self.auto("check-fail", "remove")
        self.assertEqual(code, 1, out)
        self.assertIn("page=error", out)
        self.assertIn("error page: The uninstaller could not check this PC / "
                      "Something went wrong while checking.", out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_a_marker_on_stderr_is_only_log_text(self):
        # exit 0, no done marker on stdout, forged error and done markers on stderr
        code, out, runs = self.auto("stderr-done", "remove")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)
        self.assertNotIn("result=success", out)
        self.assertNotIn("Error: FORGED", out)       # the line a believed marker logs
        self.assertNotIn("\nFinished.", out)
        # the facts on stderr are not facts
        self.record.unlink()
        code, out, runs = self.auto("stderr-facts", "remove")
        self.assertEqual(code, 1, out)
        self.assertIn("page=error", out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_autorun_refuses_without_the_five_overrides(self):
        cases = ([], self.overrides[:8],
                 self.overrides[:9] + [r"Software\Microsoft\Windows\CurrentVersion"
                                       r"\Uninstall\HoldToTalk"])
        for given in cases:
            with self.subTest(given=len(given)):
                code, out, runs = self.auto("ok", "remove-data", overrides=given)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertEqual(runs, [], "the engine was started")

    def test_autorun_refuses_folders_outside_the_temp_folder(self):
        outside = str(ROOT / "not-in-temp")        # never created: refused first
        names = ["-InstallRoot", "-EnvDir", "-StartMenuDir", "-DesktopDir"]
        for i, name in enumerate(names):
            with self.subTest(folder=name):
                given = list(self.overrides)
                given[i * 2 + 1] = outside
                self.assertEqual(given[i * 2], name)
                code, out, runs = self.auto("ok", "remove-data", overrides=given)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertEqual(runs, [], "the engine was started")
        given = list(self.overrides)
        given[1] = str(self.tmp / ".." / ".." / "elsewhere")
        code, out, runs = self.auto("ok", "remove-data", overrides=given)
        self.assertEqual((code, runs), (1, []), out)
        self.assertIn("Refused", out)

    def test_autorun_refuses_a_key_outside_the_test_key_root(self):
        for key in (r"Software\HoldToTalkOther\x", r"Software\HoldToTalkTest",
                    "Software\\HoldToTalkTest\\", r"Software\Classes\HoldToTalk"):
            with self.subTest(key=key):
                given = list(self.overrides[:9]) + [key]
                code, out, runs = self.auto("ok", "remove-data", overrides=given)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertEqual(runs, [], "the engine was started")

    def test_a_lone_carriage_return_does_not_forge_a_marker(self):
        # the engine prints "progress<CR>@@htt {error}" and "...{done}" on stdout
        code, out, runs = self.auto("cr-forge", "remove")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertNotIn("Error: CRFORGED", out)

    def test_autorun_refuses_a_data_folder_from_the_environment_outside_the_temp_folder(self):
        for name in ("HOLDTOTALK_HOME", "HTT_ENV_DIR"):
            with self.subTest(variable=name):
                self.record.unlink(missing_ok=True)
                env = clean_env(HTT_FAKE_RECORD=str(self.record), **{name: r"C:\Users\you\data"})
                code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", "remove",
                                    *self.overrides], env=env)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertIn(name, out)
                self.assertFalse(self.record.exists(), "the engine was started")
        # a data folder inside the temp folder is fine (the tests themselves set one)
        env = clean_env(HTT_FAKE_RECORD=str(self.record), HOLDTOTALK_HOME=str(self.tmp / "data"))
        code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", "remove", *self.overrides], env=env)
        self.assertEqual(code, 0, out)

    def test_engine_is_refused_without_the_overrides(self):
        env = clean_env(HTT_FAKE_RECORD=str(self.record))
        outside = ["-InstallRoot", ROOT / "x" / "prog", "-EnvDir", ROOT / "x" / "v",
                   "-StartMenuDir", ROOT / "x" / "sm", "-DesktopDir", ROOT / "x" / "dt",
                   "-RegKey", self.key]
        for given in ([], self.overrides[:8], outside,
                      self.overrides[:9] + [r"Software\Other\x"]):
            with self.subTest(given=len(given)):
                # no -AutoRun: if it were honoured, a window would open
                code, out = run_ps([GUI, "-Engine", FAKE, *given], env=env, timeout=120)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused: -Engine", out)
                self.assertFalse(self.record.exists(), "the engine was started")


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class EngineSwitches(unittest.TestCase):
    """uninstall.ps1 -Check and -Progress, in a sandbox (nothing real is touched)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-ue-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = fr"{TEST_KEY_ROOT}\ue-{uuid.uuid4().hex[:8]}\Uninstall\HoldToTalk"
        self.addCleanup(delete_key_tree, self.key.rsplit("\\", 2)[0])
        self.overrides = ["-InstallRoot", self.tmp / "prog",
                          "-EnvDir", self.tmp / "home" / "venv",
                          "-StartMenuDir", self.tmp / "sm",
                          "-DesktopDir", self.tmp / "dt", "-RegKey", self.key]

    def run_engine(self, *args, stdin=None):
        out = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ENGINE), *[str(a) for a in args]],
            capture_output=True, timeout=240, env=clean_env(), input=stdin)
        return (out.returncode, out.stdout.decode("utf-8", "replace"),
                out.stderr.decode("utf-8", "replace"))

    @staticmethod
    def markers(text):
        return [json.loads(l[6:]) for l in text.splitlines() if l.startswith("@@htt ")]

    def test_check_prints_facts_and_changes_nothing(self):
        (self.tmp / "home" / "data").mkdir(parents=True)
        (self.tmp / "home" / "data" / "config.json").write_text("{}")
        before = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        code, out, err = self.run_engine("-Check", *self.overrides)
        self.assertEqual(code, 0, out + err)
        facts = [m for m in self.markers(out) if m["kind"] == "facts"]
        self.assertEqual(len(facts), 1, out)
        f = facts[0]
        self.assertTrue(f["testMode"])
        self.assertTrue(f["dataExists"])
        self.assertFalse(f["installed"])
        self.assertEqual(len(f["steps"]), 6)
        # (the installer reports the long form; %TEMP% can be short 8.3 names)
        self.assertEqual(Path(f["layout"]["installRoot"]),
                         Path(os.path.realpath(self.tmp / "prog")))
        after = sorted(p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*"))
        self.assertEqual(before, after)

    def test_progress_needs_an_explicit_answer_and_refuses_odd_combinations(self):
        for args in (["-Progress"], ["-Progress", "-DeleteData", "-DeleteUserData", "no"],
                     ["-DeleteUserData", "no"], ["-Check", "-Progress", "-DeleteUserData", "no"]):
            with self.subTest(args=args):
                code, out, err = self.run_engine(*args, *self.overrides)
                self.assertEqual(code, 1, out + err)
                self.assertEqual([m["kind"] for m in self.markers(out)], ["error"], out)
                self.assertTrue(self.tmp.exists())

    def test_progress_never_reads_input_and_keeps_the_data_by_default(self):
        (self.tmp / "home" / "data").mkdir(parents=True)
        (self.tmp / "home" / "data" / "config.json").write_text("{}")
        prog = self.tmp / "prog"
        prog.mkdir()
        (prog / "dictate.py").write_text("# x")
        (prog / "uninstall.bat").write_text("rem x")
        # input that would answer "y" to the data question if anything read it
        code, out, err = self.run_engine("-Progress", "-DeleteUserData", "no",
                                         *self.overrides, stdin=b"y\ny\ny\n")
        self.assertEqual(code, 0, out + err)
        kinds = [m["kind"] for m in self.markers(out)]
        self.assertEqual(kinds, ["step"] * 6 + ["done"], out)
        done = self.markers(out)[-1]
        self.assertFalse(done["dataDeleted"])
        self.assertTrue((self.tmp / "home" / "data" / "config.json").exists())
        self.assertNotIn("Remove Hold to talk now?", out)
        # -BatDir was not given: the batch file is left for its own run
        self.assertTrue((prog / "uninstall.bat").exists())

    def test_progress_with_yes_deletes_the_data_and_the_program_folder_with_bat_dir(self):
        data = self.tmp / "home" / "data"
        (data / "logs").mkdir(parents=True)
        (data / "config.json").write_text("{}")
        (data / "logs" / "dictate.log").write_text("x")
        (data / "notes.docx").write_text("mine")
        prog = self.tmp / "prog"
        prog.mkdir()
        (prog / "dictate.py").write_text("# x")
        (prog / "uninstall.bat").write_text("rem x")
        code, out, err = self.run_engine("-Progress", "-DeleteUserData", "yes",
                                         "-BatDir", prog, *self.overrides)
        self.assertEqual(code, 0, out + err)
        done = self.markers(out)[-1]
        self.assertTrue(done["dataDeleted"])
        self.assertEqual(done["leftInPlace"], ["notes.docx"])
        self.assertTrue(done["selfRemoved"])
        self.assertFalse(prog.exists())
        self.assertFalse((data / "config.json").exists())
        self.assertTrue((data / "notes.docx").exists())

    def test_the_text_mode_still_asks_and_still_exits_10_for_its_own_bat(self):
        prog = self.tmp / "prog"
        prog.mkdir()
        (prog / "dictate.py").write_text("# x")
        code, out, err = self.run_engine("-BatDir", prog, *self.overrides, stdin=b"n\n")
        self.assertEqual(code, 2, out + err)
        self.assertIn("Nothing was changed.", out)
        self.assertEqual(self.markers(out), [])
        code, out, err = self.run_engine("-Yes", "-BatDir", prog, *self.overrides)
        self.assertEqual(code, 10, out + err)
        self.assertEqual(self.markers(out), [], "the text mode printed a marker")

    # -- junctions: the uninstaller never follows a link out of the tree -----------

    def junction(self, link, target):
        """mklink /J inside the test's temp folder only; removed (as a link) before
        the folder is deleted."""
        link, target = Path(link), Path(target)
        self.assertTrue(str(link).startswith(str(self.tmp)), "junction outside the sandbox")
        link.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            self.skipTest("mklink /J is not available: " + r.stdout.decode("mbcs", "replace"))
        self.addCleanup(lambda: os.path.lexists(link) and os.rmdir(link))
        return link

    def outside(self):
        out = self.tmp / "outside"
        (out / "sub").mkdir(parents=True)
        (out / "keep.txt").write_text("precious")
        (out / "sub" / "keep2.txt").write_text("precious too")
        ro = out / "readonly.txt"
        ro.write_text("ro")
        os.chmod(ro, 0o444)
        self.addCleanup(lambda: os.path.exists(ro) and os.chmod(ro, 0o666))
        return out

    def assert_outside_intact(self, out):
        self.assertEqual((out / "keep.txt").read_text(), "precious")
        self.assertEqual((out / "sub" / "keep2.txt").read_text(), "precious too")
        self.assertTrue((out / "readonly.txt").exists())

    def test_a_junction_inside_the_environment_is_removed_as_a_link(self):
        out = self.outside()
        env = self.tmp / "home" / "venv"
        (env / "Scripts").mkdir(parents=True)
        (env / "Scripts" / "python.exe").write_text("x")
        (env / "Scripts" / "ro.txt").write_text("x")
        os.chmod(env / "Scripts" / "ro.txt", 0o444)
        link = self.junction(env / "Lib" / "site", out)
        self.assertTrue((link / "keep.txt").exists())
        prog = self.tmp / "prog"
        prog.mkdir()
        (prog / "dictate.py").write_text("# x")
        self.junction(prog / "link-to-outside", out)
        code, o, err = self.run_engine("-Progress", "-DeleteUserData", "no", *self.overrides)
        self.assertEqual(code, 0, o + err)
        self.assertFalse(env.exists(), "the environment was not removed")
        self.assertFalse((prog / "dictate.py").exists())
        self.assertFalse(os.path.lexists(prog / "link-to-outside"), "the link in the program folder is left")
        self.assert_outside_intact(out)

    def test_an_environment_folder_that_is_itself_a_junction_is_refused(self):
        out = self.outside()
        env = self.tmp / "home" / "venv"
        self.junction(env, out)
        code, o, err = self.run_engine("-Progress", "-DeleteUserData", "no", *self.overrides)
        self.assertEqual(code, 0, o + err)
        warnings = [m for m in self.markers(o) if m["kind"] == "warning"]
        self.assertTrue(any("link" in m["text"] for m in warnings), o)
        self.assertTrue(os.path.lexists(env), "the link itself was removed")
        self.assert_outside_intact(out)

    def test_a_program_folder_that_is_itself_a_junction_is_left_alone(self):
        out = self.outside()
        (out / "dictate.py").write_text("# not ours")
        self.addCleanup(lambda: (out / "dictate.py").unlink(missing_ok=True))
        self.junction(self.tmp / "prog", out)
        code, o, err = self.run_engine("-Progress", "-DeleteUserData", "no", *self.overrides)
        self.assertEqual(code, 0, o + err)
        self.assertEqual((out / "dictate.py").read_text(), "# not ours")
        self.assert_outside_intact(out)

    def test_the_text_uninstaller_does_not_follow_a_junction_either(self):
        out = self.outside()
        env = self.tmp / "home" / "venv"
        env.mkdir(parents=True)
        self.junction(env / "Lib", out)
        code, o, err = self.run_engine("-Yes", *self.overrides)
        self.assertIn(code, (0, 10), o + err)
        self.assertFalse(env.exists())
        self.assert_outside_intact(out)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class InstallThenUninstallThroughTheWindows(unittest.TestCase):
    """A real test-mode install through the setup window, then a real test-mode
    removal through the uninstaller window, both in temporary places."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-uall-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        sub = fr"{TEST_KEY_ROOT}\uall-{uuid.uuid4().hex[:8]}"
        self.addCleanup(delete_key_tree, sub)
        self.key = sub + r"\Uninstall\HoldToTalk"
        self.run_key = sub + r"\Run"
        self.prog = self.tmp / "prog"
        self.overrides = ["-InstallRoot", self.prog, "-EnvDir", self.tmp / "home" / "venv",
                          "-StartMenuDir", self.tmp / "sm", "-DesktopDir", self.tmp / "dt",
                          "-RegKey", self.key]
        code, out = run_ps([SETUP_GUI, "-AutoRun", "install", "-NoDeps", "-SkipModel",
                            "-Gpu", "no", *self.overrides], timeout=600)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.prog / "uninstall-gui.ps1").is_file(),
                        "the installer did not copy uninstall-gui.ps1")
        self.data = self.tmp / "home" / "data"
        self.assertTrue((self.data / "terms-accepted.json").is_file())
        (self.data / "logs").mkdir(exist_ok=True)
        (self.data / "config.json").write_text("{}")
        (self.data / "history.sqlite3").write_text("x")
        (self.data / "logs" / "dictate.log").write_text("x")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.run_key, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "HoldToTalk", 0, winreg.REG_SZ, "x")
            winreg.SetValueEx(k, "SomeOtherProgram", 0, winreg.REG_SZ, "y")

    def remove(self, mode):
        # As uninstall.bat does: the scripts of the installed copy are copied to a
        # folder htt-uninstall-<n> in the temp folder and run from there (so the
        # program folder can be deleted); the window ends by deleting that copy.
        copy = Path(tempfile.gettempdir()) / f"htt-uninstall-{uuid.uuid4().int % 10**9}"
        copy.mkdir()
        self.addCleanup(shutil.rmtree, copy, ignore_errors=True)
        for name in ("uninstall-gui.ps1", "uninstall.ps1", "htt-common.ps1", "app.ico"):
            shutil.copy(self.prog / name, copy / name)
        code, out = run_ps([copy / "uninstall-gui.ps1", "-AutoRun", mode,
                            "-BatDir", self.prog, *self.overrides],
                           env=clean_env(HTT_RUN_KEY=self.run_key), timeout=600)
        self.assertFalse(copy.exists(), "the window did not delete its temporary copy")
        return code, out

    def run_values(self):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.run_key) as k:
            return [winreg.EnumValue(k, i)[0] for i in range(winreg.QueryInfoKey(k)[1])]

    def test_remove_keeps_the_data_by_default(self):
        code, out = self.remove("remove")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertIn("data=no", out)
        self.assertFalse(self.prog.exists(), "the program folder (and uninstall.bat) is gone")
        self.assertFalse((self.tmp / "home" / "venv").exists())
        self.assertFalse((self.tmp / "sm" / "Hold to talk.lnk").exists())
        self.assertFalse((self.tmp / "dt" / "Hold to talk.lnk").exists())
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key)
        self.assertEqual(self.run_values(), ["SomeOtherProgram"])
        self.assertEqual((self.data / "config.json").read_text(), "{}")
        self.assertTrue((self.data / "history.sqlite3").exists())

    def test_remove_with_the_data_leaves_nothing_behind(self):
        code, out = self.remove("remove-data")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertIn("data=yes", out)
        left = [p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*")
                if p.is_file()]
        self.assertEqual(left, [], "files left in the sandbox")
        self.assertFalse(self.prog.exists())
        self.assertFalse((self.tmp / "home").exists())
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key)
        self.assertEqual(self.run_values(), ["SomeOtherProgram"])


STUB_TEXT = ("Write-Host ('stub uninstall.ps1 got [' + ($args -join ' ') + ']')\r\n"
             "exit 2\r\n")
# Stands in for the window: records how it was started and the folder it runs from,
# then removes that folder, as the real window does when it ends.
STUB_GUI = r"""
$d = Split-Path -Parent $MyInvocation.MyCommand.Path
$info = @{ args = @($args | ForEach-Object { [string]$_ }); dir = $d;
           files = @(Get-ChildItem -LiteralPath $d -Name | ForEach-Object { [string]$_ }) }
ConvertTo-Json $info -Compress | Set-Content -Encoding ASCII $env:HTT_STUB_OUT
Set-Location $env:TEMP
Remove-Item -LiteralPath $d -Recurse -Force
exit 0
"""


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class UninstallBatRouting(unittest.TestCase):
    """uninstall.bat with arguments runs the text uninstaller; without any it opens
    the window through the launcher, and falls back to the text uninstaller with one
    line when the window cannot be shown. Stubs stand in for the scripts; the folder
    in %TEMP% is a temporary one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-ub-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        shutil.copy(BAT, self.src / "uninstall.bat")
        (self.src / "uninstall.ps1").write_text(STUB_TEXT, encoding="ascii")
        (self.src / "htt-common.ps1").write_text("# stub\n", encoding="ascii")
        (self.src / "app.ico").write_bytes(b"ico")
        self.temp = self.tmp / "temp"
        self.temp.mkdir()
        self.out = self.tmp / "out.json"

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HTT_")}
        env.update(TEMP=str(self.temp), TMP=str(self.temp), HTT_NO_PAUSE="1",
                   HTT_STUB_OUT=str(self.out))
        env.update(extra)
        return env

    def run_it(self, args="", **extra):
        return run_bat(self.src / "uninstall.bat", args, self.env(**extra))

    def test_arguments_run_the_text_uninstaller_and_never_the_window(self):
        (self.src / "uninstall-gui.ps1").write_text(STUB_GUI, encoding="ascii")
        code, out = self.run_it("-Yes -DeleteData")
        self.assertEqual(code, 0, out)
        self.assertIn("stub uninstall.ps1 got [-BatDir", out)
        self.assertIn("-Yes -DeleteData]", out)
        self.assertNotIn("uninstaller window", out)
        self.assertFalse(self.out.exists(), "the window was started")
        self.assertEqual(list(self.temp.iterdir()), [], "the temporary copy is left behind")

    def test_no_arguments_open_the_window_from_the_private_copy(self):
        (self.src / "uninstall-gui.ps1").write_text(STUB_GUI, encoding="ascii")
        code, out = self.run_it("")
        self.assertEqual(code, 0, out)
        self.assertNotIn("stub uninstall.ps1", out, "the text uninstaller ran as well")
        info = json.loads(self.out.read_text(encoding="ascii"))
        folder = Path(info["dir"])
        self.assertEqual(folder.parent.resolve(), self.temp.resolve())
        self.assertTrue(folder.name.startswith("htt-uninstall-"))
        self.assertEqual(sorted(info["files"]),
                         ["app.ico", "htt-common.ps1", "uninstall-gui.ps1", "uninstall.ps1"])
        self.assertEqual(info["args"][0], "-Launch")
        self.assertEqual(info["args"][1], "-BatDir")
        self.assertEqual(Path(info["args"][2].rstrip(".")), self.src)
        # the window (here the stub) removes the copy when it ends; the batch file
        # did not remove it from under the window
        self.assertEqual(list(self.temp.iterdir()), [])
        self.assertTrue((self.src / "uninstall.bat").exists())

    @unittest.skipUnless(HAVE_WPF, "needs WPF")
    def test_a_window_that_cannot_open_falls_back_with_one_line(self):
        shutil.copy(GUI, self.src / "uninstall-gui.ps1")
        code, out = self.run_it("", HTT_GUI_TEST_FAIL="1")
        self.assertEqual(code, 0, out)
        self.assertIn("The uninstaller window could not be opened (", out)
        self.assertIn("Using the text uninstaller instead.", out)
        self.assertIn("stub uninstall.ps1 got [-BatDir", out)
        self.assertEqual(list(self.temp.iterdir()), [], "the temporary copy is left behind")

    def test_a_missing_window_script_falls_back_with_one_line(self):
        code, out = self.run_it("")
        self.assertEqual(code, 0, out)
        self.assertIn("The uninstaller window (uninstall-gui.ps1) is missing. "
                      "Using the text uninstaller instead.", out)
        self.assertIn("stub uninstall.ps1 got [-BatDir", out)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_the_launcher_leaves_no_reason_file_behind(self):
        shutil.copy(GUI, self.src / "uninstall-gui.ps1")
        code, out = run_ps([self.src / "uninstall-gui.ps1", "-Launch"],
                           env=self.env(HTT_GUI_TEST_FAIL="1"))
        self.assertEqual(code, 1, out)
        self.assertIn("The uninstaller window could not be opened (", out)
        self.assertEqual(list(self.temp.glob("htt-uninstall-reason-*")), [])

    def test_the_bat_routes_by_its_arguments(self):
        bat = BAT.read_text(encoding="ascii")
        self.assertIn('if not "%~1"=="" goto :text', bat)
        self.assertIn('uninstall-gui.ps1" -Launch', bat)
        self.assertLess(bat.index("goto :text"), bat.index("-Launch"))
        self.assertIn("uninstall-gui.ps1", bat[:bat.index("-Launch")])
        # the private folder and its protection come first, the copy of the window too
        self.assertLess(bat.index("icacls.exe"), bat.index('copy /y "%HERE%uninstall-gui.ps1"'))


if __name__ == "__main__":
    unittest.main()
