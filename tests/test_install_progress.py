"""The install engine as the installer window drives it: install.ps1 with
-Check (one "facts" line, changes nothing) and -Progress -AcceptedTerms (marker
lines, no questions), the refusal rules around them, and the answers the window
passes (-Nvidia, -Model, -DesktopShortcut). See the contract in install.ps1's
header comment.

Nothing here touches the real profile: every run that could change anything
gets all five test overrides (temporary install folder, environment, Start
menu and Desktop folders, a registry key under HKCU\\Software\\HoldToTalkTest\\)
and -NoDeps (no pip). The engine is started the way the window starts it: no
console window, standard input closed or empty, output read through pipes.
The temporary folder name has a space and a non-ASCII letter on purpose.
"""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
HAVE_PS = POWERSHELL.is_file()
CREATE_NO_WINDOW = 0x08000000
MARK = "@@htt "
REG_BASE = r"Software\HoldToTalkTest"

FACT_FIELDS = ("kind", "appName", "version", "termsVersion", "testMode",
               "layout", "installedVersion", "python", "gpu", "env", "model",
               "files", "estimates", "disk", "steps", "launch")


def manifest(root=ROOT):
    out = []
    for line in (root / "install-files.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def terms_version():
    import settings
    return settings.TERMS_VERSION


def delete_key_tree(path):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            subs = []
            while True:
                try:
                    subs.append(winreg.EnumKey(k, len(subs)))
                except OSError:
                    break
    except OSError:
        return
    for s in subs:
        delete_key_tree(path + "\\" + s)
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def key_exists(path):
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, path))
        return True
    except OSError:
        return False


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err
        self.text = out + err
        self.lines = out.splitlines()
        self.markers = []
        for line in self.lines:
            if line.startswith(MARK):
                self.markers.append(json.loads(line[len(MARK):]))

    def of(self, kind):
        return [m for m in self.markers if m["kind"] == kind]


def long_name(path):
    """A path as the installer reports it: folder names in their long form.
    %TEMP%, and so the places the tests make, can be in the short 8.3 form
    (as on GitHub's machines); the part that exists is expanded."""
    return os.path.realpath(path)


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class EngineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A short base path (the project folder can be long, Windows stops at
        # 260), then a folder with a space and a non-ASCII letter.
        cls.base = Path(tempfile.mkdtemp(prefix="htt-p-"))
        cls.tmp = cls.base / "t e\u00e9st"
        cls.tmp.mkdir()
        cls.src = cls.tmp / "Src"
        cls.src.mkdir()
        for rel in manifest():
            dest = cls.src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest)
        cls.keybase = fr"{REG_BASE}\prog-{uuid.uuid4().hex[:8]}"
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith("HTT_") and k != "HOLDTOTALK_HOME"}
        cls.env["HTT_NO_PAUSE"] = "1"
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)
        delete_key_tree(cls.keybase)
        try:                                # only if nothing else is in it
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_BASE)
        except OSError:
            pass

    def places(self):
        """A fresh set of temporary places (nothing is created yet)."""
        type(self).count += 1
        root = self.tmp / f"run{self.count}"
        return dict(root=root, prog=root / "prog", env=root / "home" / "venv",
                    data=root / "home" / "data", sm=root / "sm",
                    dt=root / "dt", key=fr"{self.keybase}\k{self.count}")

    @staticmethod
    def override_args(p):
        return ["-InstallRoot", str(p["prog"]), "-EnvDir", str(p["env"]),
                "-StartMenuDir", str(p["sm"]), "-DesktopDir", str(p["dt"]),
                "-RegKey", p["key"]]

    def run_engine(self, args, env=None, stdin=None, timeout=900):
        """install.ps1 as the window runs it: no console window, stdin empty,
        stdout and stderr read through pipes, decoded as UTF-8."""
        cmd = [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(self.src / "install.ps1")] + list(args)
        started = time.time()
        out = subprocess.run(cmd, env=env or self.env, capture_output=True,
                             stdin=stdin if stdin is not None
                             else subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW, timeout=timeout)
        # The window modes are UTF-8 (strictly decoded); the text mode prints
        # in the console's code page, as it always did.
        errors = "strict" if ("-Check" in args or "-Progress" in args) \
            else "replace"
        r = Result(out.returncode, out.stdout.decode("utf-8", errors),
                   out.stderr.decode("utf-8", errors))
        r.seconds = time.time() - started
        r.raw = out.stdout
        return r

    def check(self, p, *extra, env=None):
        return self.run_engine(["-Check", "-NoDeps"] + self.override_args(p)
                               + list(extra), env=env)

    def progress(self, p, *extra, version=None, env=None):
        return self.run_engine(
            ["-Progress", "-AcceptedTerms", version or terms_version(),
             "-NoDeps", "-SkipModel", "-NoLaunch"] + self.override_args(p)
            + list(extra), env=env)

    def assert_nothing_changed(self, p):
        for name in ("prog", "env", "data", "sm", "dt"):
            self.assertFalse(p[name].exists(), f"{name} was created")
        self.assertFalse(p["root"].exists(), "a folder was created")
        self.assertFalse(key_exists(p["key"]), "the registry key was made")

    def no_python_env(self):
        fake = self.tmp / "fakepath" / "Microsoft" / "WindowsApps"
        fake.mkdir(parents=True, exist_ok=True)
        shutil.copy(SYSROOT / "System32" / "whoami.exe", fake / "python.exe")
        return dict(self.env, PATH=str(fake))


class Check(EngineTestCase):
    def test_one_facts_line_with_every_field_and_nothing_changed(self):
        p = self.places()
        r = self.check(p, "-Gpu", "yes")
        self.assertEqual(r.code, 0, r.text)
        self.assertEqual(len(r.markers), 1, r.text)
        self.assertEqual([l for l in r.lines if l.startswith(MARK)],
                         [r.lines[0]], "the facts line is the only output")
        self.assertFalse(r.raw.startswith(b"\xef\xbb\xbf"), "no byte-order mark")
        f = r.of("facts")[0]
        for name in FACT_FIELDS:
            self.assertIn(name, f)
        self.assertEqual(f["appName"], "Hold to talk")
        import settings
        self.assertEqual(f["version"], settings.APP_VERSION)
        self.assertEqual(f["termsVersion"], settings.TERMS_VERSION)
        self.assertIs(f["testMode"], True)
        self.assertEqual(f["layout"], dict(
            installRoot=long_name(p["prog"]), envDir=long_name(p["env"]),
            dataDir=long_name(p["data"]),
            startMenuLnk=long_name(p["sm"] / "Hold to talk.lnk"),
            desktopLnk=long_name(p["dt"] / "Hold to talk.lnk")))
        self.assertIsNone(f["installedVersion"])
        self.assertIs(f["python"]["ok"], True)
        for name in ("version", "exe", "via"):
            self.assertTrue(f["python"][name])
        self.assertEqual(f["gpu"], {"found": True, "runtimeInstalled": False})
        self.assertEqual(f["env"], {"exists": False, "depsCurrent": False})
        m = f["model"]
        self.assertEqual((m["name"], m["cpuName"]), ("large-v3-turbo", "small"))
        self.assertEqual((m["downloadMB"], m["cpuDownloadMB"]), (1500, 500))
        self.assertIsNone(m["present"])
        self.assertEqual(f["files"]["count"], len(manifest(self.src)))
        self.assertEqual(f["files"]["sizeBytes"], sum(
            (self.src / rel).stat().st_size for rel in manifest(self.src)))
        e = f["estimates"]
        self.assertEqual(set(e), {"libsDownloadMB", "libsDiskMB",
                                  "nvidiaDownloadMB", "nvidiaDiskMB"})
        self.assertGreater(e["libsDownloadMB"], 0)
        self.assertGreater(e["libsDiskMB"], e["libsDownloadMB"])
        self.assertEqual((e["nvidiaDownloadMB"], e["nvidiaDiskMB"]),
                         (1300, 2000))
        self.assertRegex(f["disk"]["drive"], r"^[A-Za-z]:$")
        self.assertGreater(f["disk"]["freeBytes"], 0)
        self.assertEqual(len(f["steps"]), 7)
        self.assertEqual(f["steps"][0], "Looking for Python")
        self.assertEqual(f["launch"], dict(
            exe=long_name(p["env"] / "Scripts" / "pythonw.exe"),
            args=f'"{long_name(p["prog"] / "dictate.py")}" --open',
            workDir=long_name(p["prog"])))
        self.assertLess(r.seconds, 60)
        self.assert_nothing_changed(p)

    def test_no_card_found_is_reported(self):
        f = self.check(self.places(), "-Gpu", "no").of("facts")[0]
        self.assertEqual(f["gpu"], {"found": False, "runtimeInstalled": False})

    def test_check_never_asks_and_never_shows_the_terms(self):
        r = self.check(self.places())
        self.assertNotIn("Terms of Use", r.text)
        self.assertNotIn("I agree", r.text)

    def test_a_missing_python_is_reported_inside_the_facts(self):
        p = self.places()
        r = self.check(p, env=self.no_python_env())
        self.assertEqual(r.code, 0, r.text)
        py = r.of("facts")[0]["python"]
        self.assertIs(py["ok"], False)
        self.assertTrue(py["notes"])
        self.assertTrue(all(isinstance(n, str) for n in py["notes"]))
        self.assertTrue(any("py -3.12" in n for n in py["notes"]), py)
        self.assertEqual(r.of("error"), [])
        self.assert_nothing_changed(p)

    def test_the_installed_version_and_the_kept_choice_are_read(self):
        # An install folder with a settings.py, and an environment that keeps
        # a requirements hash for the NVIDIA runtime (no Python in it: it does
        # not "exist" as a working environment, but the kept choice shows).
        p = self.places()
        p["prog"].mkdir(parents=True)
        (p["prog"] / "settings.py").write_text('APP_VERSION = "0.1.7"\n',
                                               encoding="ascii")

        got = self.check(p, "-Gpu", "yes").of("facts")[0]
        self.assertEqual(got["installedVersion"], "0.1.7")
        # the env folder is not made by -Check
        self.assertFalse(p["env"].exists())

    def test_half_overridden_check_is_an_error_marker_and_exit_1(self):
        r = self.run_engine(["-Check", "-InstallRoot", str(self.tmp / "x")])
        self.assertEqual(r.code, 1, r.text)
        self.assertEqual(len(r.of("error")), 1)
        self.assertIn("all together", r.of("error")[0]["text"])
        self.assertFalse((self.tmp / "x").exists())


class Refusals(EngineTestCase):
    def test_the_wrong_terms_version_stops_before_anything_changes(self):
        for wrong in ("9.9", "0", "1.0.1", "v1.0"):
            with self.subTest(version=wrong):
                p = self.places()
                r = self.progress(p, version=wrong)
                self.assertEqual(r.code, 1, r.text)
                self.assertEqual(len(r.of("error")), 1)
                self.assertIn("Terms of Use", r.of("error")[0]["text"])
                self.assertEqual(r.of("done"), [])
                self.assertEqual(r.of("step"), [])
                self.assert_nothing_changed(p)

    def test_progress_without_accepted_terms_exits_3_changing_nothing(self):
        p = self.places()
        r = self.run_engine(["-Progress", "-NoDeps", "-SkipModel", "-NoLaunch"]
                            + self.override_args(p))
        self.assertEqual(r.code, 3, r.text)
        self.assertEqual(len(r.of("error")), 1)
        self.assertEqual(r.of("done"), [])
        self.assert_nothing_changed(p)
        # an empty version is no statement either
        r = self.run_engine(["-Progress", "-AcceptedTerms", "", "-NoDeps"]
                            + self.override_args(p))
        self.assertEqual(r.code, 3, r.text)
        self.assert_nothing_changed(p)

    def test_yes_with_progress_or_accepted_terms_is_refused(self):
        for extra in (["-Progress", "-Yes"],
                      ["-Progress", "-Yes", "-AcceptedTerms", terms_version()],
                      ["-Yes", "-AcceptedTerms", terms_version()]):
            with self.subTest(extra=extra):
                p = self.places()
                r = self.run_engine(extra + ["-NoDeps", "-SkipModel"]
                                    + self.override_args(p))
                self.assertEqual(r.code, 1, r.text)
                self.assertEqual(r.of("done"), [])
                self.assertFalse(
                    (p["data"] / "terms-accepted.json").exists())
                self.assert_nothing_changed(p)

    def test_accepted_terms_without_progress_is_refused(self):
        for extra in (["-AcceptedTerms", terms_version()],
                      ["-Check", "-AcceptedTerms", terms_version()]):
            with self.subTest(extra=extra):
                p = self.places()
                r = self.run_engine(extra + ["-NoDeps"]
                                    + self.override_args(p))
                self.assertEqual(r.code, 1, r.text)
                self.assertIn("-AcceptedTerms", r.text)
                self.assertNotIn("Looking for Python", r.text)
                self.assert_nothing_changed(p)

    def test_check_and_progress_together_are_refused(self):
        p = self.places()
        r = self.run_engine(["-Check", "-Progress", "-AcceptedTerms",
                             terms_version(), "-NoDeps"]
                            + self.override_args(p))
        self.assertEqual(r.code, 1, r.text)
        self.assert_nothing_changed(p)

    def test_yes_alone_still_writes_no_record(self):
        p = self.places()
        r = self.run_engine(["-Yes", "-NoDeps", "-SkipModel", "-NoLaunch"]
                            + self.override_args(p))
        try:
            self.assertEqual(r.code, 0, r.text)
            self.assertEqual(r.markers, [], "no marker lines in text mode")
            self.assertIn("No acceptance of the Terms of Use was recorded",
                          r.text)
            self.assertFalse((p["data"] / "terms-accepted.json").exists())
            self.assertTrue((p["sm"] / "Hold to talk.lnk").is_file())
            self.assertTrue((p["dt"] / "Hold to talk.lnk").is_file())
        finally:
            delete_key_tree(p["key"])


class Progress(EngineTestCase):
    def test_a_full_run_prints_the_markers_and_records_the_terms(self):
        p = self.places()
        facts = self.check(p).of("facts")[0]
        empty = self.tmp / "empty-stdin.txt"
        empty.write_bytes(b"")
        with open(empty, "rb") as stdin:       # input redirected from an empty file
            r = self.run_engine(
                ["-Progress", "-AcceptedTerms", terms_version(), "-NoDeps",
                 "-SkipModel", "-NoLaunch"] + self.override_args(p),
                stdin=stdin)
        try:
            self.assertEqual(r.code, 0, r.text)
            self.assertFalse(r.raw.startswith(b"\xef\xbb\xbf"))
            steps = r.of("step")
            self.assertEqual([s["n"] for s in steps], list(range(1, 8)))
            self.assertEqual([s["title"] for s in steps], facts["steps"])
            self.assertEqual({s["total"] for s in steps}, {len(facts["steps"])})
            done = r.of("done")
            self.assertEqual(len(done), 1)
            self.assertEqual(r.markers[-1]["kind"], "done")
            self.assertEqual(r.of("error"), [])
            d = done[0]
            self.assertEqual(d["installRoot"], long_name(p["prog"]))
            self.assertEqual(d["envDir"], long_name(p["env"]))
            self.assertEqual(d["dataDir"], long_name(p["data"]))
            self.assertEqual(d["startMenuLnk"], long_name(p["sm"] / "Hold to talk.lnk"))
            self.assertEqual(d["desktopLnk"], long_name(p["dt"] / "Hold to talk.lnk"))
            self.assertIs(d["termsRecorded"], True)
            self.assertEqual(d["launch"], facts["launch"])
            # the markers are in the order of the steps, the done one last
            kinds = [m["kind"] for m in r.markers]
            self.assertEqual(kinds.count("step"), 7)
            self.assertEqual(kinds[-1], "done")
            rec = json.loads((p["data"] / "terms-accepted.json").read_text(
                encoding="utf-8"))
            self.assertEqual(rec["how"], "installer")
            self.assertEqual(rec["version"], terms_version())
            self.assertEqual([f.name for f in p["data"].iterdir()],
                             ["terms-accepted.json"])
            self.assertTrue((p["sm"] / "Hold to talk.lnk").is_file())
            self.assertTrue((p["dt"] / "Hold to talk.lnk").is_file())
            self.assertTrue((p["prog"] / "dictate.py").is_file())
            self.assertTrue((p["env"] / "Scripts" / "pythonw.exe").is_file())
            self.assertIn("were accepted in the installer window", r.text)
            self.assertNotIn("Please read the Terms of Use", r.text)
        finally:
            delete_key_tree(p["key"])

    def test_every_marker_is_one_line_of_json_with_non_ascii_and_spaces(self):
        p = self.places()
        r = self.progress(p)
        try:
            self.assertEqual(r.code, 0, r.text)
            self.assertIn("t e\u00e9st", r.of("done")[0]["installRoot"])
            for line in r.lines:
                if line.startswith("@@htt"):
                    self.assertTrue(line.startswith(MARK), line)
                    self.assertIsInstance(json.loads(line[len(MARK):]), dict)
            self.assertTrue(r.markers)
            # the raw bytes are UTF-8, not the console's code page
            self.assertIn("t e\u00e9st".encode("utf-8"), r.raw)
        finally:
            delete_key_tree(p["key"])

    def test_desktop_shortcut_no(self):
        p = self.places()
        r = self.progress(p, "-DesktopShortcut", "no")
        try:
            self.assertEqual(r.code, 0, r.text)
            self.assertIsNone(r.of("done")[0]["desktopLnk"])
            self.assertFalse((p["dt"] / "Hold to talk.lnk").exists())
            self.assertTrue((p["sm"] / "Hold to talk.lnk").is_file())
            self.assertIs(r.of("done")[0]["termsRecorded"], True)
        finally:
            delete_key_tree(p["key"])

    def test_desktop_shortcut_no_leaves_an_existing_one_alone(self):
        p = self.places()
        p["dt"].mkdir(parents=True)
        (p["dt"] / "Hold to talk.lnk").write_text("mine", encoding="ascii")
        r = self.progress(p, "-DesktopShortcut", "no")
        try:
            self.assertEqual(r.code, 0, r.text)
            self.assertEqual((p["dt"] / "Hold to talk.lnk").read_text(), "mine")
        finally:
            delete_key_tree(p["key"])

    def test_nvidia_no_is_respected_and_yes_or_ask_install_it(self):
        for answer, installs in (("no", False), ("yes", True), (None, True)):
            with self.subTest(nvidia=answer):
                p = self.places()
                extra = ["-Gpu", "yes"] + (
                    ["-Nvidia", answer] if answer else [])
                r = self.progress(p, *extra)
                try:
                    self.assertEqual(r.code, 0, r.text)
                    flat = " ".join(r.text.split())
                    self.assertIn("An NVIDIA graphics card was found", flat)
                    self.assertEqual(
                        "Not installing the NVIDIA runtime" in flat,
                        not installs, flat)
                    self.assertEqual("-Nvidia yes: installing" in flat,
                                     answer == "yes")
                finally:
                    delete_key_tree(p["key"])

    def test_nvidia_no_in_the_text_installer_asks_nothing(self):
        p = self.places()
        r = self.run_engine(["-Nvidia", "no", "-Gpu", "yes", "-NoDeps",
                             "-SkipModel", "-NoLaunch"]
                            + self.override_args(p),
                            stdin=None)
        # stdin is empty: the terms are not accepted, nothing changes
        self.assertEqual(r.code, 3, r.text)
        self.assert_nothing_changed(p)
        proc = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.src / "install.ps1"), "-Nvidia", "no", "-Gpu",
             "yes", "-NoDeps", "-SkipModel", "-NoLaunch"]
            + self.override_args(p), env=self.env, capture_output=True,
            input=b"I agree\r\n", timeout=900, creationflags=CREATE_NO_WINDOW)
        try:
            text = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, text)
            self.assertIn("Not installing the NVIDIA runtime", text)
            self.assertNotIn("Install the NVIDIA runtime?", text)
            self.assertNotIn("@@htt", text)
        finally:
            delete_key_tree(p["key"])

    def test_no_suitable_python_is_exit_4_with_the_help_text(self):
        p = self.places()
        r = self.progress(p, env=self.no_python_env())
        self.assertEqual(r.code, 4, r.text)
        errors = r.of("error")
        self.assertEqual(len(errors), 1)
        for needle in ("python.org", "64-bit", "Microsoft Store"):
            self.assertIn(needle, errors[0]["text"])
        self.assertIn("\n", errors[0]["text"])
        self.assertEqual(r.of("done"), [])
        self.assertEqual([s["n"] for s in r.of("step")], [1])
        self.assert_nothing_changed(p)

    def test_the_text_mode_prints_no_markers(self):
        # exit 3 at the terms, as before, and not one marker line
        p = self.places()
        r = self.run_engine(["-NoDeps"] + self.override_args(p))
        self.assertEqual(r.code, 3, r.text)
        self.assertEqual(r.markers, [])
        self.assertNotIn(MARK, r.text)
        self.assert_nothing_changed(p)


class SourceRules(unittest.TestCase):
    def test_read_host_is_only_in_the_text_installer_functions(self):
        text = (ROOT / "install.ps1").read_text(encoding="ascii")
        current, where = None, {}
        for n, line in enumerate(text.splitlines(), 1):
            m = re.match(r"function ([\w-]+)", line)
            if m:
                current = m.group(1)
            if "Read-Host" in line and not line.lstrip().startswith("#"):
                where.setdefault(current, []).append(n)
        self.assertEqual(set(where), {"Ask-YesNo", "Show-TermsPaged",
                                      "Confirm-Terms"}, where)
        # Ask-YesNo answers by itself first in machine mode; the other two
        # are only reached through the text branch.
        ask = text[text.index("function Ask-YesNo"):]
        ask = ask[:ask.index("Read-Host")]
        self.assertIn("if ($Yes -or $Progress) { return $Default }", ask)
        self.assertRegex(text, r"else \{\s*\$how = Confirm-Terms")
        self.assertLess(text.index("$how = 'installer'"),
                        text.index("$how = Confirm-Terms"))

    def test_the_new_switches_are_documented_in_the_header(self):
        head = (ROOT / "install.ps1").read_text(encoding="ascii")
        head = head[:head.index("[CmdletBinding()]")]
        for name in ("-Console", "-Check", "-Progress", "-AcceptedTerms",
                     "-Nvidia", "-Model", "-DesktopShortcut"):
            self.assertIn(name, head)
        self.assertIn("the same statement as", " ".join(head.split()))

    def test_estimates_are_measured_figures_not_zero(self):
        text = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertRegex(text, r"libsDownloadMB = [1-9]\d*;")
        self.assertRegex(text, r"libsDiskMB = [1-9]\d*;")


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class ModelAnswer(unittest.TestCase):
    """-Model yes|no|ask in Offer-ModelDownload, taken out of install.ps1 with
    the PowerShell parser (nothing else of the installer runs; the speech
    model is never downloaded)."""

    SCRIPT = r"""
param([string]$Install, [string]$ModelAnswer, [string]$Mode)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Install, [ref]$null, [ref]$null)
foreach ($fn in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Offer-ModelDownload' }, $true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$Model = $ModelAnswer; $Progress = ($Mode -eq 'progress')
$script:log = @()
function Get-ModelName($d, $g) { return 'tiny' }
function Test-ModelOnDisk($e, $d, $n) { return @{ Code = 3; Rev = '' } }
function Say($t) { }
function Warn-User($t) { $script:log += 'warn' }
function Activity($t) { $script:log += 'activity' }
function Ask-YesNo($q, $d) { $script:log += 'asked'; return $d }
function Invoke-Console($e, $a) { $script:log += 'download'; return 0 }
$script:ModelFile = 'x'
Offer-ModelDownload 'py' 'dir' $true
$script:log -join ','
"""

    def run_case(self, answer, mode="text"):
        with tempfile.TemporaryDirectory(prefix="htt-m-") as d:
            script = Path(d) / "m.ps1"
            script.write_text(self.SCRIPT, encoding="ascii")
            out = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script), str(ROOT / "install.ps1"), answer,
                 mode], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        lines = out.stdout.strip().splitlines()
        return lines[-1] if lines else ""

    def test_model_yes_no_ask(self):
        self.assertEqual(self.run_case("yes"), "activity,download")
        self.assertEqual(self.run_case("no"), "")
        self.assertEqual(self.run_case("ask"), "asked,activity,download")
        self.assertEqual(self.run_case("ask", "progress"),
                         "asked,activity,download")


if __name__ == "__main__":
    unittest.main()
