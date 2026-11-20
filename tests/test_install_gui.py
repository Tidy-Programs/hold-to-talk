"""The setup window (install-gui.ps1) and how install.bat opens it.

The window is driven without a person through its test switch -AutoRun,
against the fake engine in tests/fixtures (which changes nothing at all) and,
when install.ps1 already speaks the window's contract (-Check / -Progress),
once against the real engine. Every run passes all five test overrides into a
temporary folder and a throw-away key under HKCU\\Software\\HoldToTalkTest;
the real engine also gets -NoDeps -SkipModel -Gpu no (no pip, no download,
nothing that looks at the graphics card). The windows of these runs are
placed off-screen and never take the focus.
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

ROOT = Path(__file__).resolve().parent.parent
GUI = ROOT / "install-gui.ps1"
FAKE = ROOT / "tests" / "fixtures" / "fake_install_engine.ps1"
SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
CMD = str(SYSROOT / "System32" / "cmd.exe")
HAVE_PS = POWERSHELL.is_file()
TEST_KEY_ROOT = r"Software\HoldToTalkTest"


def _have_wpf():
    if not HAVE_PS:
        return False
    try:
        out = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-Command",
             "Add-Type -AssemblyName PresentationFramework; 'wpf-ok'"],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return "wpf-ok" in out.stdout


HAVE_WPF = _have_wpf()


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HTT_") and k != "HOLDTOTALK_HOME"}
    env["HTT_NO_PAUSE"] = "1"
    env.update(extra)
    return env


def run_ps(args, env=None, timeout=240):
    out = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        + [str(a) for a in args],
        capture_output=True, timeout=timeout, env=env or clean_env())
    return out.returncode, (out.stdout.decode("utf-8", "replace")
                            + out.stderr.decode("utf-8", "replace"))


def run_bat(bat, args, env, timeout=240):
    line = f'{CMD} /s /c ""{bat}" {args}"'
    out = subprocess.run(line, env=env, capture_output=True, timeout=timeout)
    return out.returncode, (out.stdout.decode("utf-8", "replace")
                            + out.stderr.decode("utf-8", "replace"))


def delete_key_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                except OSError:
                    break
                i += 1
        for s in subs:
            delete_key_tree(path + "\\" + s)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


class ScriptFiles(unittest.TestCase):
    def test_files_are_ascii(self):
        for path in (GUI, FAKE, ROOT / "install.bat"):
            data = path.read_bytes()
            self.assertTrue(all(b < 128 for b in data), path.name)
        bat = (ROOT / "install.bat").read_bytes()
        self.assertEqual(bat.count(b"\n"), bat.count(b"\r\n"), "install.bat: CRLF")

    @unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
    def test_scripts_parse(self):
        # Parsed only, never run (see ps_parse_errors in test_install.py).
        from test_install import ps_parse_errors
        self.assertEqual(ps_parse_errors([GUI, FAKE]), "")

    def test_only_the_guarded_test_helper_ticks_the_terms_box(self):
        text = GUI.read_text(encoding="ascii")
        # In the XAML the box has no IsChecked at all: never pre-ticked.
        m = re.search(r'<CheckBox x:Name="AgreeBox"[^>]*>', text)
        self.assertIsNotNone(m)
        self.assertNotIn("IsChecked", m.group(0))
        # Every assignment that can tick it is inside Set-TermsBoxForTest.
        helper = re.search(r"function Set-TermsBoxForTest\b.*?\n}\n", text, re.S)
        self.assertIsNotNone(helper)
        body = helper.group(0)
        self.assertIn("throw 'Refused", body)
        self.assertIn("$script:OverridesComplete", body)
        outside = text.replace(body, "")
        for line in outside.splitlines():
            if re.search(r"AgreeBox\.(IsChecked\s*=(?!=)|SetValue|SetCurrentValue)",
                         line):
                self.assertRegex(line, r"AgreeBox\.IsChecked\s*=\s*\$false",
                                 f"the program may only untick the box: {line}")
            self.assertNotRegex(line, r"AgreeBox.*(Toggle\(\)|ToggleButtonAutomationPeer)",
                                line)
        # The acceptance goes to the engine only when the box is ticked.
        self.assertRegex(text, r"if \(\$ui\.AgreeBox\.IsChecked -eq \$true -and "
                               r"\$script:TermsOk\) \{ \$a \+= @\('-AcceptedTerms'")

    def test_the_window_uses_the_contract(self):
        text = GUI.read_text(encoding="ascii")
        for needle in ("'-Check'", "'-Progress'", "'-AcceptedTerms'", "'-Nvidia'",
                       "'-Model'", "'-DesktopShortcut'", "'@@htt '",
                       "CreateNoWindow = $true", "ReadLineAsync()",
                       "DispatcherTimer", "taskkill.exe",
                       "AppsUseLightTheme", "I have read and agree to the Terms of Use",
                       "By agreeing you accept the whole terms.",
                       "This includes the sections on no warranty, limits of "
                       "liability, assumption of risk, indemnity and disputes "
                       "(sections 9 to 14).",
                       "https://www.python.org/downloads/windows/"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"Add_OutputDataReceived|Add_ErrorDataReceived")
        # plain, calm wording: no exclamation marks in what the window says
        for shown in re.findall(r'(?:Text|Content|Header)="([^"]*)"', text):
            self.assertNotIn("!", shown)

    def test_the_modes_are_decided_from_the_facts(self):
        text = GUI.read_text(encoding="ascii")
        self.assertIn("function Get-Mode", text)
        self.assertIn("function Update-ModeText", text)
        # install mode keeps the original strings; the words come from one table
        for needle in ("Installs for your Windows account only. No administrator "
                       "rights needed.",
                       "What will be installed", "Installing Hold to talk"):
            self.assertIn(needle, text)
        for word in ("Update", "Updating", "updated", "Repair", "Repairing", "repaired"):
            self.assertIn(f"'{word}'", text)

    def test_the_window_does_not_take_a_command_from_a_marker(self):
        text = GUI.read_text(encoding="ascii")
        self.assertNotRegex(text, r"\.launch\b", "no marker may supply the start command")
        self.assertIn("function Get-StartCommand", text)
        self.assertIn("Scripts\\pythonw.exe", text)
        # the check box is in the always-visible button row, not on the page
        row = text.index("<!-- the button row: always visible -->")
        self.assertGreater(text.index('x:Name="StartNow"'), row)
        self.assertGreater(text.index('x:Name="DiskWarn"'),
                           text.index('x:Name="PageChoose"'))
        self.assertLess(text.index('x:Name="DiskWarn"'),
                        text.index('x:Name="PlanPanel"'))

    def test_install_python_for_me_is_pinned_and_checked(self):
        text = GUI.read_text(encoding="ascii")
        # one pinned file: the exact address, size and SHA-256
        self.assertIn("Url     = 'https://www.python.org/ftp/python/3.12.10/"
                      "python-3.12.10-amd64.exe'", text)
        self.assertIn("Bytes   = [int64]26964224", text)
        self.assertIn("Sha256  = '67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb'",
                      text)
        # per user, no administrator rights, python.exe not on PATH, no file associations,
        # no shortcuts (names checked against docs.python.org/3.12/using/windows.html)
        self.assertIn("'/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1', "
                      "'InstallLauncherAllUsers=0',\n                'Include_tcltk=1', "
                      "'Include_test=0', 'SimpleInstall=1', 'AssociateFiles=0', 'Shortcuts=0')", text)
        self.assertNotIn("your PATH setting is not changed", text)
        for needle in ("Get-AuthenticodeSignature", "Python Software Foundation",
                       "AllowAutoRedirect = $false", "[Net.SecurityProtocolType]::Tls12",
                       "HttpCompletionOption]::ResponseHeadersRead",
                       "CreateNew", "function Remove-PythonWorkDir",
                       "Refused: nothing is downloaded in test mode.",
                       "Refused: nothing is run in test mode.",
                       "Install Python for me", "PSF License",
                       "Nothing is downloaded until you choose the button."):
            self.assertIn(needle, text)
        # the only network code is the one pinned request; nothing is fetched at start
        self.assertNotRegex(text, r"Invoke-WebRequest|Invoke-RestMethod|DownloadFile|"
                                  r"DownloadString|WebClient|Start-BitsTransfer")
        self.assertEqual(len(re.findall(r"SendAsync\(", text)), 1)
        # verification needs both the hash and the signature, before anything is run
        verify = text.index("'verify' {")
        self.assertLess(verify, text.index("$p.Proc = & $script:PyRun"))
        self.assertLess(text.index("function Test-PythonInstaller"), verify)
        body = re.search(r"function Test-PythonInstaller.*?\n}\n", text, re.S).group(0)
        for needle in ("Get-StreamSha256", "$Lock", "FileShare]::Read"):
            self.assertIn(needle, body)
        sig = re.search(r"function Test-PythonSignature.*?\n}\n", text, re.S).group(0)
        for needle in ("'Valid'", "$Pin.Signer"):
            self.assertIn(needle, sig)
        # the file stays open (FileShare.Read) from before the check until the process has started
        self.assertLess(text.index("$p.Lock = New-Object IO.FileStream"), verify)
        run = text.index("$p.Proc = & $script:PyRun")
        self.assertLess(run, text.index("try { $p.Lock.Dispose() }"))
        # a folder that cannot be made private stops the run
        self.assertIn("throw 'A private folder could not be made on this PC", text)
        # 15 minutes, then "Stop waiting"; 3010 is a success
        self.assertIn("$script:PyTimeoutSeconds = 900", text)
        self.assertIn("$code -ne 0 -and $code -ne 3010", text)
        self.assertIn('Content="Stop waiting"', text)

    def test_the_setup_exe_route_never_offers_the_text_installer(self):
        text = GUI.read_text(encoding="ascii")
        self.assertIn("[switch]$FromSetupExe", text)
        self.assertRegex(text, r"Set-Shown \$ui\.TextInstallerBtn \(-not \$FromSetupExe")
        for fn in ("Open-TextInstaller", "Start-TextInstallerFallback"):
            body = re.search(r"function %s.*?\n}\n" % fn, text, re.S).group(0)
            self.assertIn("if ($FromSetupExe)", body, fn)
        # the ZIP wording lives in one place and is skipped in that route
        self.assertEqual(len(re.findall("Extract the whole ZIP",
                                        text.split("function Invoke-SelfTest")[0])), 2)
        adv = re.search(r"function Get-DownloadAdvice.*?\n}\n", text, re.S).group(0)
        self.assertIn("if ($FromSetupExe)", adv)
        self.assertIn("project's page", adv)
        # documents are opened from a copy, which is deleted on exit
        self.assertIn("function Remove-DocumentCopies", text)
        self.assertGreaterEqual(text.count("Remove-DocumentCopies"), 4)
        # the Setup.exe tells the window
        iss = (ROOT / "installer" / "holdtotalk.iss").read_text(encoding="ascii")
        self.assertIn("-File \"' + Gui + '\" -FromSetupExe", iss)

    def test_the_fake_engine_switches_to_python_found_only_after_the_stub(self):
        text = FAKE.read_text(encoding="ascii")
        self.assertIn("HTT_FAKE_PYTHON_INSTALLED", text)

    def test_nothing_personal_in_the_files(self):
        user = os.environ.get("USERNAME", "\0").lower()
        for path in (GUI, FAKE, ROOT / "install.bat"):
            text = path.read_text(encoding="ascii").lower()
            self.assertNotIn("\\users\\" + user + "\\", text, path.name)
            self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[a-z]{2,}", path.name)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class SelfTest(unittest.TestCase):
    def test_selftest_passes(self):
        code, out = run_ps([GUI, "-SelfTest"])
        self.assertEqual(code, 0, out)
        self.assertIn("selftest ok", out)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class SelfTestFromTheSetupExe(unittest.TestCase):
    def test_selftest_passes_with_the_setup_exe_switch_too(self):
        code, out = run_ps([GUI, "-SelfTest", "-FromSetupExe"])
        self.assertEqual(code, 0, out)
        self.assertIn("selftest ok", out)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class AutoRunWithTheFakeEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-gui-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.record = self.tmp / "record.txt"
        self.key = fr"{TEST_KEY_ROOT}\gui-{uuid.uuid4().hex[:8]}\Uninstall\HoldToTalk"
        self.overrides = ["-InstallRoot", self.tmp / "prog",
                          "-EnvDir", self.tmp / "home" / "venv",
                          "-StartMenuDir", self.tmp / "sm",
                          "-DesktopDir", self.tmp / "dt", "-RegKey", self.key]

    def auto(self, scenario, mode, extra=(), overrides=None):
        env = clean_env(HTT_FAKE_SCENARIO=scenario, HTT_FAKE_RECORD=str(self.record))
        if overrides is None:
            overrides = self.overrides
        code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", mode, *extra,
                            *overrides], env=env)
        runs = []
        if self.record.exists():
            runs = [json.loads(l) for l in
                    self.record.read_text(encoding="ascii").splitlines() if l]
        return code, out, runs

    def progress_params(self, runs):
        prog = [r for r in runs if r["mode"] == "progress"]
        self.assertEqual(len(prog), 1, runs)
        return prog[0]["params"]

    def test_success_passes_the_acceptance_and_the_choices(self):
        code, out, runs = self.auto("ok,gpu", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertEqual(runs[0]["mode"], "check")
        p = self.progress_params(runs)
        import settings
        self.assertEqual(p["AcceptedTerms"], settings.TERMS_VERSION)
        self.assertEqual((p["Nvidia"], p["Model"], p["DesktopShortcut"]),
                         ("yes", "yes", "yes"))
        # the overrides reach the engine unchanged
        self.assertEqual(p["RegKey"], self.key)
        self.assertEqual(Path(p["InstallRoot"]), self.tmp / "prog")
        # the fake changes nothing
        self.assertFalse((self.tmp / "prog").exists())

    def wording(self, out):
        line = [l for l in out.splitlines() if l.startswith("mode: ")]
        self.assertEqual(len(line), 1, out)
        return dict(part.split(": ", 1) for part in line[0].split(" | "))

    def test_install_mode_keeps_the_install_wording(self):
        code, out, runs = self.auto("ok", "install")
        self.assertEqual(code, 0, out)
        w = self.wording(out)
        self.assertEqual(w["mode"], "install")
        self.assertEqual(w["window"], "Install Hold to talk")
        self.assertEqual(w["rail"], "Installing / Setup")
        self.assertEqual(w["button"], "Install")
        self.assertEqual(w["page title"], "Installing Hold to talk")
        self.assertEqual(w["result"], "Hold to talk is installed")
        self.assertEqual(w["lead"], "Version 0.9.1 is installed in this folder:")

    def test_update_mode_says_update_everywhere(self):
        code, out, runs = self.auto("ok,update", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        w = self.wording(out)
        self.assertEqual(w["mode"], "update")
        self.assertEqual(w["window"], "Update Hold to talk")
        self.assertEqual(w["rail"], "Updating / Update")
        self.assertEqual(w["button"], "Update")
        self.assertEqual(w["page title"], "Updating Hold to talk")
        self.assertEqual(w["result"], "Hold to talk is updated")
        self.assertEqual(w["lead"], "Version 0.9.1 is now installed in this folder:")
        # the engine still gets the same arguments and the terms are still accepted
        import settings
        self.assertEqual(self.progress_params(runs)["AcceptedTerms"], settings.TERMS_VERSION)

    def test_repair_mode_says_repair_everywhere(self):
        code, out, runs = self.auto("ok,repair", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        w = self.wording(out)
        self.assertEqual(w["mode"], "repair")
        self.assertEqual(w["window"], "Repair Hold to talk")
        self.assertEqual(w["rail"], "Repairing / Repair")
        self.assertEqual(w["button"], "Repair")
        self.assertEqual(w["page title"], "Repairing Hold to talk")
        self.assertEqual(w["result"], "Hold to talk is repaired")

    def test_update_and_repair_failures_say_so(self):
        for scenario, word in (("fail,update", "updated"), ("fail,repair", "repaired")):
            with self.subTest(scenario=scenario):
                self.record.unlink(missing_ok=True)
                code, out, runs = self.auto(scenario, "install")
                self.assertEqual(code, 1, out)
                self.assertEqual(self.wording(out)["result"],
                                 f"Hold to talk could not be {word}")

    def test_a_stopped_update_says_update_stopped(self):
        code, out, runs = self.auto("slow,update", "stop-install")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.wording(out)["result"], "Update stopped")

    def test_unticked_choices_are_passed_as_no(self):
        code, out, runs = self.auto("ok,gpu", "install-none", ["-NoDeps", "-Gpu", "yes"])
        self.assertEqual(code, 0, out)
        p = self.progress_params(runs)
        self.assertEqual((p["Nvidia"], p["Model"], p["DesktopShortcut"]),
                         ("no", "no", "no"))
        self.assertEqual(p.get("NoDeps"), "True")
        self.assertEqual(p.get("Gpu"), "yes")

    def test_failure_exits_1(self):
        code, out, runs = self.auto("fail", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)
        self.assertIn("Installing the libraries failed", out)

    def test_python_vanishing_during_the_install_is_a_failure(self):
        code, out, runs = self.auto("py-gone", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)

    def test_missing_python_installs_nothing_and_exits_3(self):
        code, out, runs = self.auto("nopython", "install")
        self.assertEqual(code, 3, out)
        # checked twice (Check again), never installed
        self.assertEqual([r["mode"] for r in runs], ["check", "check"])

    def test_install_python_for_me_in_test_mode_calls_the_stub_and_downloads_nothing(self):
        temp = Path(tempfile.gettempdir())
        before = {p.name for p in temp.glob("htt-py-*")}
        code, out, runs = self.auto("nopython", "install-python")
        self.assertEqual(code, 0, out)
        self.assertIn("pyinstall=1", out)               # the stub was called, once
        self.assertIn("result=success", out)            # ...and Python was "found" after it
        self.assertEqual([r["mode"] for r in runs], ["check", "check", "progress"])
        self.assertEqual({p.name for p in temp.glob("htt-py-*")}, before,
                         "a folder for the installer was made in test mode")
        # without the button the stub is never called
        self.record.unlink()
        code, out, runs = self.auto("nopython", "install")
        self.assertEqual(code, 3, out)
        self.assertIn("pyinstall=0", out)

    def test_cancel_at_the_terms_exits_3_and_never_installs(self):
        code, out, runs = self.auto("ok", "cancel-terms")
        self.assertEqual(code, 3, out)
        self.assertIn("page=terms", out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_stop_during_the_install_exits_2_and_stops_the_children(self):
        code, out, runs = self.auto("slow", "stop-install")
        self.assertEqual(code, 2, out)
        self.assertIn("result=stopped", out)
        pid = [r for r in runs if r["mode"] == "progress"][0]["childPid"]
        alive = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
            text=True, timeout=60).stdout
        self.assertNotIn(str(pid), alive, "the engine's child is still running")

    def test_a_failing_check_shows_the_error_and_exits_1(self):
        code, out, runs = self.auto("check-fail", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("page=error", out)
        # the engine's own error text is what the page says
        self.assertIn("error page: The installer could not check this PC / "
                      "Something went wrong while checking.", out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_autorun_refuses_without_the_five_overrides(self):
        cases = ([], self.overrides[:8],
                 self.overrides[:9] + [r"Software\Microsoft\Windows\CurrentVersion"
                                       r"\Uninstall\HoldToTalk"])
        for given in cases:
            with self.subTest(given=len(given)):
                code, out, runs = self.auto("ok", "install", overrides=given)
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
                code, out, runs = self.auto("ok", "install", overrides=given)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertEqual(runs, [], "the engine was started")
        # climbing out of the temp folder with .. does not count as inside it
        given = list(self.overrides)
        given[1] = str(self.tmp / ".." / ".." / "elsewhere")
        code, out, runs = self.auto("ok", "install", overrides=given)
        self.assertEqual((code, runs), (1, []), out)
        self.assertIn("Refused", out)

    def test_autorun_refuses_a_key_outside_the_test_key_root(self):
        for key in (r"Software\HoldToTalkOther\x", r"Software\HoldToTalkTest",
                    "Software\\HoldToTalkTest\\",
                    r"Software\Classes\HoldToTalk"):
            with self.subTest(key=key):
                given = list(self.overrides[:9]) + [key]
                code, out, runs = self.auto("ok", "install", overrides=given)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertEqual(runs, [], "the engine was started")

    def test_a_short_form_of_the_temp_folder_is_not_refused(self):
        # The short (8.3) form and the long form of a path are the same folder.
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(self.tmp), buf, 1024):
            self.skipTest("no short path for the temp folder")
        short = Path(buf.value)
        given = ["-InstallRoot", short / "prog", "-EnvDir", short / "home" / "venv",
                 "-StartMenuDir", short / "sm", "-DesktopDir", short / "dt",
                 "-RegKey", self.key]
        code, out, runs = self.auto("ok", "install", overrides=given)
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)

    def test_a_lone_carriage_return_does_not_forge_a_marker(self):
        # the engine prints "progress<CR>@@htt {error}" and "...{done}" on stdout
        code, out, runs = self.auto("cr-forge", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertNotIn("Error: CRFORGED", out)

    def test_autorun_refuses_a_data_folder_from_the_environment_outside_the_temp_folder(self):
        for name in ("HOLDTOTALK_HOME", "HTT_ENV_DIR"):
            with self.subTest(variable=name):
                self.record.unlink(missing_ok=True)
                env = clean_env(HTT_FAKE_RECORD=str(self.record), **{name: r"C:\Users\you\data"})
                code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", "install",
                                    *self.overrides], env=env)
                self.assertEqual(code, 1, out)
                self.assertIn("Refused", out)
                self.assertIn(name, out)
                self.assertFalse(self.record.exists(), "the engine was started")
        env = clean_env(HTT_FAKE_RECORD=str(self.record), HOLDTOTALK_HOME=str(self.tmp / "data"))
        code, out = run_ps([GUI, "-Engine", FAKE, "-AutoRun", "install", *self.overrides], env=env)
        self.assertEqual(code, 0, out)

    def test_from_the_setup_exe_the_window_runs_the_same_way(self):
        code, out, runs = self.auto("ok", "install", extra=["-FromSetupExe"])
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)

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

    def test_a_marker_on_stderr_is_only_log_text(self):
        # the engine exits 0 and prints no done marker, but writes forged
        # error and done markers to stderr: that is a failure, not a success
        code, out, runs = self.auto("stderr-done", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("result=failure", out)
        self.assertNotIn("result=success", out)
        self.assertNotIn("Error: FORGED", out)      # the line a believed marker logs
        self.assertNotIn("\nFinished.", out)
        # the facts on stderr are not facts
        self.record.unlink()
        code, out, runs = self.auto("stderr-facts", "install")
        self.assertEqual(code, 1, out)
        self.assertIn("page=error", out)
        self.assertEqual([r["mode"] for r in runs], ["check"])

    def test_a_forged_launch_is_never_used(self):
        code, out, runs = self.auto("ok,forge-launch", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        line = [l for l in out.splitlines() if l.startswith("start-command: ")]
        self.assertEqual(len(line), 1, out)
        exe, args, work = line[0][len("start-command: "):].split(" | ")
        self.assertEqual(Path(exe), self.tmp / "home" / "venv" / "Scripts" / "pythonw.exe")
        self.assertEqual(args, '"' + str(self.tmp / "prog" / "dictate.py") + '" --open')
        self.assertEqual(Path(work), self.tmp / "prog")
        self.assertNotIn("Forged", line[0])

    def test_odd_step_markers_neither_hang_nor_grow_the_list(self):
        code, out, runs = self.auto("ok,bad-steps", "install")
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        self.assertIn("steps=7", out)


@unittest.skipUnless(HAVE_WPF, "needs Windows PowerShell with WPF")
class AutoRunWithTheRealEngine(unittest.TestCase):
    """One install through the window with the real install.ps1, into
    temporary places (all five overrides, -NoDeps -SkipModel -Gpu no)."""

    def test_install_records_the_acceptance_as_installer(self):
        engine = (ROOT / "install.ps1").read_text(encoding="ascii")
        if "[switch]$Check" not in engine:
            self.skipTest("install.ps1 does not speak the window's contract yet")
        tmp = Path(tempfile.mkdtemp(prefix="htt-guir-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        sub = fr"{TEST_KEY_ROOT}\guireal-{uuid.uuid4().hex[:8]}"
        self.addCleanup(delete_key_tree, sub)
        key = sub + r"\Uninstall\HoldToTalk"
        code, out = run_ps(
            [GUI, "-AutoRun", "install", "-NoDeps", "-SkipModel", "-Gpu", "no",
             "-InstallRoot", tmp / "prog", "-EnvDir", tmp / "home" / "venv",
             "-StartMenuDir", tmp / "sm", "-DesktopDir", tmp / "dt",
             "-RegKey", key], timeout=600)
        self.assertEqual(code, 0, out)
        self.assertIn("result=success", out)
        rec = json.loads((tmp / "home" / "data" / "terms-accepted.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(rec["how"], "installer")
        import settings
        self.assertEqual(rec["version"], settings.TERMS_VERSION)
        self.assertTrue((tmp / "sm" / "Hold to talk.lnk").is_file())
        self.assertTrue((tmp / "dt" / "Hold to talk.lnk").is_file())


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class InstallBatRouting(unittest.TestCase):
    """install.bat with arguments runs the text installer; without any it
    opens the window, and falls back to the text installer with one line
    when the window cannot be shown. A stub stands in for install.ps1."""

    STUB = "Write-Host ('stub install.ps1 got [' + ($args -join ' ') + ']')\r\nexit 0\r\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-bat-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copy(ROOT / "install.bat", self.tmp / "install.bat")
        shutil.copy(GUI, self.tmp / "install-gui.ps1")
        (self.tmp / "install.ps1").write_text(self.STUB, encoding="ascii")

    def test_arguments_reach_the_text_installer(self):
        code, out = run_bat(self.tmp / "install.bat", "-Yes -Console", clean_env())
        self.assertEqual(code, 0, out)
        self.assertIn("stub install.ps1 got [-Yes -Console]", out)
        self.assertNotIn("setup window", out)

    @unittest.skipUnless(HAVE_WPF, "needs WPF")
    def test_a_window_that_cannot_open_falls_back_with_one_line(self):
        code, out = run_bat(self.tmp / "install.bat", "",
                            clean_env(HTT_GUI_TEST_FAIL="1"))
        self.assertEqual(code, 0, out)
        self.assertIn("The setup window could not be opened (", out)
        self.assertIn("Using the text installer instead.", out)
        self.assertIn("stub install.ps1 got []", out)

    def test_the_launcher_leaves_no_reason_file_behind(self):
        scratch = Path(tempfile.mkdtemp(prefix="htt-tmp-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        env = clean_env(HTT_GUI_TEST_FAIL="1", TEMP=str(scratch), TMP=str(scratch))
        code, out = run_ps([self.tmp / "install-gui.ps1", "-Launch"], env=env)
        self.assertEqual(code, 1, out)
        self.assertIn("The setup window could not be opened (", out)
        self.assertEqual(list(scratch.glob("htt-setup-*")), [])

    def test_a_missing_window_script_falls_back_with_one_line(self):
        (self.tmp / "install-gui.ps1").unlink()
        code, out = run_bat(self.tmp / "install.bat", "", clean_env())
        self.assertEqual(code, 0, out)
        self.assertIn("The setup window (install-gui.ps1) is missing. Using the "
                      "text installer instead.", out)
        self.assertIn("stub install.ps1 got []", out)

    def test_a_reason_from_the_window_is_shown_before_the_text_installer(self):
        code, out = run_bat(self.tmp / "install.bat", "",
                            clean_env(HTT_FALLBACK_REASON="The setup window stopped: x"))
        self.assertEqual(code, 0, out)
        self.assertIn("The setup window stopped: x. Using the text installer instead.", out)
        self.assertIn("stub install.ps1 got []", out)

    def test_the_bat_opens_the_window_only_without_arguments(self):
        bat = (ROOT / "install.bat").read_text(encoding="ascii")
        self.assertIn('if not "%~1"=="" goto text', bat)
        self.assertNotIn("[%1]", bat)
        self.assertIn('install-gui.ps1" -Launch', bat)
        self.assertIn("install.ps1 is missing next to install.bat", bat)
        self.assertLess(bat.index("install.ps1 is missing"), bat.index("goto text"))


if __name__ == "__main__":
    unittest.main()
