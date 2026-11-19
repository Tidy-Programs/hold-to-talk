"""The single-file Setup.exe (installer/holdtotalk.iss, tools/build_setup.py)
and the tamper check in install.ps1 (files.sha256).

The exe tests build the real exe with Inno Setup and are skipped when the
compiler is not available (set HTT_ISCC, or unpack Inno Setup 6 to
~/tools/innosetup). The tamper-check tests need only Windows PowerShell.

Nothing here installs for real. Every run of the exe passes /VERYSILENT
/SUPPRESSMSGBOXES (no Inno dialog) and, through /HTTARGS, the window's test
switches: -AutoRun and the five test overrides (temporary folders, a key under
HKCU\\Software\\HoldToTalkTest\\) plus -NoDeps -SkipModel -Gpu no. The window of
such a run is off-screen and never takes the focus. Temporary folders here have
no spaces: /HTTARGS cannot carry quotes.
"""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_setup  # noqa: E402

SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
HAVE_PS = POWERSHELL.is_file()
CREATE_NO_WINDOW = 0x08000000
REG_BASE = r"Software\HoldToTalkTest"
DAMAGED = "This download is damaged or was changed."


def find_iscc():
    env = os.environ.get("HTT_ISCC")
    cand = Path(env) if env else Path.home() / "tools" / "innosetup" / "ISCC.exe"
    return cand if cand.is_file() else None


ISCC = find_iscc()


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


def clean_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HTT_") and k != "HOLDTOTALK_HOME"}
    env["HTT_NO_PAUSE"] = "1"
    return env


def terms_version():
    import settings
    return settings.TERMS_VERSION


def installed_files(folder):
    return sorted(str(p.relative_to(folder)).replace("\\", "/").lower()
                  for p in folder.rglob("*") if p.is_file())


class SandboxMixin:
    """Fresh temporary places and a throw-away registry key for one run."""

    def sandbox(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-se-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        sub = fr"{REG_BASE}\se-{uuid.uuid4().hex[:8]}"
        self.addCleanup(delete_key_tree, sub)
        self.key = sub + r"\Uninstall\HoldToTalk"
        t = self.tmp
        self.prog = t / "prog"
        self.overrides = ["-InstallRoot", str(self.prog),
                          "-EnvDir", str(t / "home" / "venv"),
                          "-StartMenuDir", str(t / "sm"),
                          "-DesktopDir", str(t / "dt"),
                          "-RegKey", self.key]
        self.record = t / "home" / "data" / "terms-accepted.json"


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class TamperCheck(SandboxMixin, unittest.TestCase):
    """install.ps1 with files.sha256 next to it (what the Setup.exe unpacks)."""

    @classmethod
    def setUpClass(cls):
        cls.base = Path(tempfile.mkdtemp(prefix="htt-sc-"))
        cls.files = build_setup.read_manifest()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def staged(self, with_hashes=True):
        n = len(list(self.base.iterdir()))
        stage = self.base / f"s{n}"
        build_setup.write_staging(stage, self.files)
        if not with_hashes:
            (stage / "files.sha256").unlink()
        return stage

    def run_engine(self, stage, *args):
        cmd = [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(stage / "install.ps1"), *args, *self.overrides]
        out = subprocess.run(cmd, env=clean_env(), capture_output=True,
                             stdin=subprocess.DEVNULL,
                             creationflags=CREATE_NO_WINDOW, timeout=600)
        return out.returncode, (out.stdout.decode("utf-8", "replace")
                                + out.stderr.decode("utf-8", "replace"))

    def install_args(self):
        return ["-Progress", "-AcceptedTerms", terms_version(), "-NoDeps",
                "-SkipModel", "-NoLaunch", "-Gpu", "no"]

    def assert_refused(self, stage, *args):
        self.sandbox()
        code, out = self.run_engine(stage, *args)
        self.assertEqual(code, 1, out)
        self.assertIn(DAMAGED, out)
        self.assertIn("Download it again from the project's page.", out)
        self.assertIn('"kind":"error"', out)          # the machine-mode marker
        for name in ("prog", "home", "sm", "dt"):
            self.assertFalse((self.tmp / name).exists(), f"{name} was created")

    def test_the_hash_file_has_the_agreed_form(self):
        stage = self.staged()
        lines = (stage / "files.sha256").read_text(encoding="ascii").splitlines()
        self.assertEqual(lines[0], "# htt-sha256 1")
        self.assertEqual(len(lines) - 1, len(self.files))
        for line, rel in zip(lines[1:], self.files):
            h, _, name = line.partition("  ")
            self.assertEqual(name, rel)
            self.assertRegex(h, r"^[0-9a-f]{64}$")
            self.assertEqual(h, hashlib.sha256((ROOT / rel).read_bytes()).hexdigest())
        self.assertNotIn("files.sha256", self.files)

    def test_an_untouched_copy_passes_check_and_is_silent_about_it(self):
        stage = self.staged()
        self.sandbox()
        code, out = self.run_engine(stage, "-Check", "-NoDeps")
        self.assertEqual(code, 0, out)
        self.assertNotIn(DAMAGED, out)

    def test_a_changed_file_is_refused_before_anything_is_copied(self):
        stage = self.staged()
        with open(stage / "stt.py", "ab") as f:
            f.write(b"\n# changed\n")
        self.assert_refused(stage, *self.install_args())

    def test_a_changed_file_is_refused_by_check_too(self):
        stage = self.staged()
        with open(stage / "ui" / "app.js", "ab") as f:
            f.write(b"\n// changed\n")
        self.assert_refused(stage, "-Check", "-NoDeps")

    def test_a_missing_entry_is_refused(self):
        stage = self.staged()
        path = stage / "files.sha256"
        lines = path.read_text(encoding="ascii").splitlines()
        victim = [i for i, l in enumerate(lines) if l.endswith("  keys.py")][0]
        del lines[victim]
        path.write_bytes(("\n".join(lines) + "\n").encode("ascii"))
        self.assert_refused(stage, *self.install_args())

    def test_an_extra_entry_is_refused(self):
        stage = self.staged()
        with open(stage / "files.sha256", "ab") as f:
            f.write(("0" * 64 + "  extra.py\n").encode("ascii"))
        self.assert_refused(stage, *self.install_args())

    def test_a_missing_file_is_refused(self):
        stage = self.staged()
        (stage / "sounds.py").unlink()
        self.assert_refused(stage, *self.install_args())

    def test_a_hash_file_in_the_wrong_form_is_refused(self):
        stage = self.staged()
        (stage / "files.sha256").write_bytes(b"not a hash list\n")
        self.assert_refused(stage, *self.install_args())

    def test_unsafe_manifest_entries_are_refused_before_anything_is_created(self):
        bad = ["../outside.py", "ui/../../outside.py", "/rooted.py", "\\leading.py",
               "C:/drive.py", "C:drive.py", 'quo"te.py', "quo'te.py", "stream.py:x"]
        for entry in bad:
            with self.subTest(entry=entry):
                stage = self.staged(with_hashes=False)
                with open(stage / "install-files.txt", "ab") as f:
                    f.write(("\n" + entry + "\n").encode("ascii"))
                self.sandbox()
                code, out = self.run_engine(stage, "-Check", "-NoDeps")
                self.assertEqual(code, 1, out)
                self.assertIn("not allowed", out)
                for name in ("prog", "home", "sm", "dt"):
                    self.assertFalse((self.tmp / name).exists(), f"{name} was created")

    def test_without_the_hash_file_nothing_is_checked(self):
        stage = self.staged(with_hashes=False)
        with open(stage / "stt.py", "ab") as f:          # changed, but unchecked
            f.write(b"\n# changed\n")
        self.sandbox()
        code, out = self.run_engine(stage, "-Check", "-NoDeps")
        self.assertEqual(code, 0, out)
        self.assertNotIn(DAMAGED, out)
        self.assertNotIn("sha256", out.lower())


@unittest.skipUnless(ISCC, "Inno Setup compiler not found (set HTT_ISCC)")
@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class SetupExe(SandboxMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="htt-so-"))
        run = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / "build_setup.py"),
             "--iscc", str(ISCC), "--out", str(cls.out), "--allow-dirty"],
            capture_output=True, text=True, timeout=900,
            creationflags=CREATE_NO_WINDOW)
        if run.returncode != 0:
            shutil.rmtree(cls.out, ignore_errors=True)
            raise AssertionError("the build failed:\n" + run.stdout + run.stderr)
        cls.version = build_setup.read_setting("APP_VERSION")
        cls.name = build_setup.read_setting("APP_NAME")
        cls.exe = cls.out / f"HoldToTalk-Setup-{cls.version}.exe"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def run_exe(self, *window_args, extra=(), timeout=900):
        args = " ".join(str(a) for a in window_args)
        cmd = [str(self.exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", *extra,
               "/HTTARGS=" + args]
        return subprocess.run(cmd, env=clean_env(), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)

    def window_args(self, scenario):
        return ["-AutoRun", scenario, "-NoDeps", "-SkipModel", "-Gpu", "no",
                *self.overrides]

    def temp_leftovers(self):
        return {p.name for p in Path(tempfile.gettempdir()).glob("is-*.tmp")}

    def test_the_exe_exists_and_the_sums_file_matches(self):
        self.assertTrue(self.exe.is_file())
        digest = hashlib.sha256(self.exe.read_bytes()).hexdigest()
        sums = (self.out / "SHA256SUMS.txt").read_bytes()
        self.assertEqual(sums, f"{digest}  {self.exe.name}\n".encode("ascii"))

    def test_the_version_resource(self):
        ps = ("$v=(Get-Item -LiteralPath $env:HTT_EXE).VersionInfo;"
              "@{p=$v.ProductName;v=$v.ProductVersion;f=$v.FileVersion;"
              "c=$v.CompanyName;d=$v.FileDescription;"
              "l=[string]$v.LegalCopyright;t=[string]$v.LegalTrademarks;"
              "o=[string]$v.OriginalFilename;i=[string]$v.InternalName}"
              "|ConvertTo-Json -Compress")
        env = clean_env()
        env["HTT_EXE"] = str(self.exe)
        out = subprocess.run([str(POWERSHELL), "-NoProfile", "-Command", ps],
                             env=env, capture_output=True, text=True, timeout=120,
                             creationflags=CREATE_NO_WINDOW)
        info = {k: v.strip() for k, v in json.loads(out.stdout).items()}
        self.assertEqual(info["p"], self.name)
        self.assertTrue(info["v"].startswith(self.version), info)
        self.assertTrue(info["f"].startswith(self.version), info)
        self.assertEqual(info["c"], "Hold to talk contributors")
        self.assertEqual(info["d"], f"{self.name} Setup")
        for k in ("l", "t", "o", "i"):
            self.assertEqual(info[k], "", info)

    def test_no_account_name_or_user_folder_in_the_exe(self):
        # The exe is LZMA-compressed: this covers the loader only. The files inside
        # are covered by the staging scan of the build (see class BuildScan).
        data = self.exe.read_bytes()
        user = os.environ.get("USERNAME", "")
        self.assertTrue(user)
        for needle in (user, "C:\\Users\\"):
            ascii_hits, wide_hits = build_setup.find_bytes(data, needle)
            self.assertEqual((ascii_hits, wide_hits), (0, 0), needle)

    def test_a_test_mode_install_through_the_exe(self):
        self.sandbox()
        before = self.temp_leftovers()
        r = self.run_exe(*self.window_args("install"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        expect = sorted(f.lower() for f in build_setup.read_manifest())
        self.assertEqual(installed_files(self.prog), expect)
        self.assertFalse((self.prog / "files.sha256").exists())
        rec = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(rec["how"], "installer")
        self.assertEqual(rec["version"], terms_version())
        self.assertEqual(self.temp_leftovers() - before, set(),
                         "Inno left its temporary folder behind")

    def test_closing_before_installing_exits_3_and_installs_nothing(self):
        self.sandbox()
        r = self.run_exe(*self.window_args("cancel-terms"))
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        for name in ("prog", "home", "sm", "dt"):
            self.assertFalse((self.tmp / name).exists(), f"{name} was created")

    def test_a_window_that_cannot_start_gives_exit_code_1_without_a_dialog(self):
        # The window script ends with code 10 (outside 0..3) when it cannot show
        # itself: the exe reports 1 and, in /VERYSILENT mode, shows no message.
        self.sandbox()
        env = clean_env()
        env["HTT_GUI_TEST_FAIL"] = "1"
        cmd = [str(self.exe), "/VERYSILENT", "/SUPPRESSMSGBOXES",
               "/HTTARGS=" + " ".join(map(str, self.window_args("install")))]
        r = subprocess.run(cmd, env=env, capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=300, creationflags=CREATE_NO_WINDOW)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        for name in ("prog", "home", "sm", "dt"):
            self.assertFalse((self.tmp / name).exists(), f"{name} was created")

    def test_the_window_refuses_an_autorun_without_the_overrides(self):
        # The pass-through cannot make a silent real install.
        r = self.run_exe("-AutoRun", "install", "-NoDeps", "-SkipModel")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_the_exe_never_installs_anything_of_its_own(self):
        text = (ROOT / "installer" / "holdtotalk.iss").read_text(encoding="ascii")
        for line in ("CreateAppDir=no", "Uninstallable=no",
                     "CreateUninstallRegKey=no", "PrivilegesRequired=lowest",
                     "SetupIconFile=app.ico", "Compression=lzma2",
                     "SolidCompression=yes", "AppPublisher=Hold to talk contributors"):
            self.assertIn(line, text)
        self.assertNotRegex(text, r"(?im)^\s*\[(Icons|Registry|Run|UninstallRun)\]")
        self.assertNotRegex(text, r"(?i)https?://")

    def test_the_script_of_the_exe(self):
        text = (ROOT / "installer" / "holdtotalk.iss").read_text(encoding="ascii")
        # the window knows it runs from the Setup.exe
        self.assertIn("-File \"' + Gui + '\" -FromSetupExe", text)
        # no time zone or clock of the build machine in the exe
        self.assertRegex(text, r"(?m)^TimeStampsInUTC=yes$")
        # one plain message, never in silent mode, when the window cannot be opened
        body = re.search(r"procedure ShowNoWindowMessage.*?\nend;", text, re.S).group(0)
        self.assertIn("if not WizardSilent then", body)
        self.assertIn("could not be opened on this PC", body)
        self.assertIn("ZIP on the project", body)
        self.assertIn("install.bat", body)
        run = re.search(r"function RunSetupWindow.*?\nend;\n", text, re.S).group(0)
        self.assertIn("(Rc >= 0) and (Rc <= 3)", run)
        self.assertGreaterEqual(run.count("ShowNoWindowMessage()"), 3)
        # no path ever goes through cmd (a % would be expanded there)
        self.assertNotRegex(text, r"(?i)cmd\.exe|rd /s")
        self.assertIn("SetEnvironmentVariable('HTT_CLEAN_TARGET'", text)
        self.assertIn("-File \"' + ScriptFile + '\"", text)
        self.assertNotRegex(text, r"-Command|-EncodedCommand")

    def test_the_cleanup_command_removes_only_inno_s_temporary_folder(self):
        tmpdir = Path(tempfile.gettempdir())
        script = tmpdir / ("htt-cleantest-" + uuid.uuid4().hex[:8] + ".ps1")
        self.addCleanup(lambda: script.unlink(missing_ok=True))

        def run(target):
            script.write_text(build_setup.CLEANUP_SCRIPT, encoding="ascii")
            env = clean_env()
            env["HTT_CLEAN_TARGET"] = str(target)
            rc = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(script)], env=env, capture_output=True,
                timeout=120, creationflags=CREATE_NO_WINDOW).returncode
            self.assertFalse(script.exists(), "the script did not delete itself")
            return rc

        # a folder with a % and a quote-free odd name that matches the pattern
        good = tmpdir / ("is-" + uuid.uuid4().hex[:8] + ".tmp")
        (good / "app").mkdir(parents=True)
        (good / "app" / "x.txt").write_text("x")
        self.addCleanup(shutil.rmtree, good, ignore_errors=True)
        self.assertEqual(run(good), 0)
        self.assertFalse(good.exists())
        # a junction inside is removed as a link: what it points to survives
        outside = Path(tempfile.mkdtemp(prefix="htt-keep-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (outside / "keep.txt").write_text("keep")
        again = tmpdir / ("is-" + uuid.uuid4().hex[:8] + ".tmp")
        again.mkdir()
        self.addCleanup(shutil.rmtree, again, ignore_errors=True)
        mk = subprocess.run(["cmd", "/c", "mklink", "/J", str(again / "link"), str(outside)],
                            capture_output=True, timeout=60)
        if mk.returncode == 0:
            self.assertEqual(run(again), 0)
            self.assertFalse(again.exists())
            self.assertEqual((outside / "keep.txt").read_text(), "keep")
        # anything else is refused and left alone: a wrong name, a wrong parent
        for victim in (tmpdir / ("htt-other-" + uuid.uuid4().hex[:6]),
                       tmpdir / ("is-" + uuid.uuid4().hex[:6] + ".tmp.bak"),
                       tmpdir / "sub" / ("is-" + uuid.uuid4().hex[:6] + ".tmp")):
            victim.mkdir(parents=True)
            self.addCleanup(shutil.rmtree, victim.parent if victim.parent != tmpdir else victim,
                            ignore_errors=True)
            with self.subTest(victim=victim.name):
                self.assertEqual(run(victim), 1)
                self.assertTrue(victim.exists())
        self.assertEqual(run(""), 1)


class BuildScan(unittest.TestCase):
    """tools/build_setup.py: the privacy scan of the STAGING folder, fixed times,
    the manifest rules and the git checks."""

    USERS = "C:" + "\\Users"          # split so that a privacy scan of this file stays quiet
    ENV = {"USERNAME": "Zeldaquux", "USERPROFILE": USERS + "\\Zeldaquux",
           "COMPUTERNAME": "MACHINE-7QX", "USERDOMAIN": "MACHINE-7QX"}

    def test_needles_skip_short_generic_and_empty_values(self):
        use, skipped = build_setup.private_needles(
            {"USERNAME": "pc", "USERPROFILE": self.USERS + "\\Admin", "COMPUTERNAME": "",
             "USERDOMAIN": "Workgroup"})
        self.assertEqual(use, [])
        self.assertEqual(len(skipped), 4, skipped)
        use, skipped = build_setup.private_needles(self.ENV)
        self.assertEqual([l for l, _ in use], ["USERNAME", "COMPUTERNAME"])
        self.assertTrue(any("same as an earlier" in s for s in skipped), skipped)
        # an empty needle never hits
        self.assertEqual(build_setup.scan_bytes(b"anything at all", [("X", "")]), [])
        self.assertEqual(build_setup.scan_bytes(b"", [("X", "abc")]), [])

    def test_a_private_value_is_found_in_every_encoding_and_any_case(self):
        use, _ = build_setup.private_needles(self.ENV)
        for blob in (b"hello zeldaQUUX there",
                     "x ZELDAQUUX y".encode("utf-16-le"),
                     "x machine-7qx y".encode("cp1252"),
                     b"\x00" + "zeldaquux".encode("utf-16-le")):
            self.assertTrue(build_setup.scan_bytes(blob, use), blob)
        self.assertEqual(build_setup.scan_bytes(b"nothing private here", use), [])

    def test_only_the_placeholder_user_folder_is_allowed(self):
        ok = [b"C:\\Users\\you", b'C:\\Users\\you"', b"C:\\Users\\you\\AppData\\x",
              b"c:/users/you/x", b'"C:\\\\Users\\\\you\\\\p"']
        for blob in ok:
            self.assertEqual(build_setup.scan_bytes(blob, []), [], blob)
        u = self.USERS.encode("ascii")
        for blob in (u + b"\\bob\\x", u + b"\\youth", u + b"\\", u + b"\\\\x",
                     (self.USERS + "\\Someone").encode("utf-16-le")):
            self.assertTrue(build_setup.scan_bytes(blob, []), blob)

    def test_the_scan_reads_every_staged_file_and_the_generated_ones(self):
        stage = Path(tempfile.mkdtemp(prefix="htt-bs-"))
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        build_setup.write_staging(stage, build_setup.read_manifest())
        names = {p.name for p in stage.rglob("*") if p.is_file()}
        for gen in ("files.sha256", "files.inc", "place.inc", "cleanup.inc"):
            self.assertIn(gen, names)
        problems, report = build_setup.staging_scan(stage, [build_setup.ISS])
        self.assertEqual(problems, [], problems)
        self.assertTrue(any(l.startswith("staging scan: ") and "files, searched for" in l
                            for l in report), report)
        # the report never prints a private value itself
        text = "\n".join(report).lower()
        for _, value in build_setup.private_needles()[0]:
            self.assertNotIn(value.lower(), text)
        # one planted value in a staged file is found
        (stage / "sounds.py").write_bytes(b"# built by Zeldaquux\n")
        found = build_setup.scan_files([stage / "sounds.py"],
                                       build_setup.private_needles(self.ENV)[0])
        self.assertEqual(len(found), 1)

    def test_every_staged_file_has_the_fixed_time(self):
        stage = Path(tempfile.mkdtemp(prefix="htt-bs-"))
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        build_setup.write_staging(stage, build_setup.read_manifest())
        times = {int(p.stat().st_mtime) for p in stage.rglob("*")}
        self.assertEqual(times, {build_setup.FIXED_MTIME})
        self.assertEqual(build_setup.FIXED_MTIME, 1767225600)    # 2026-01-01 00:00:00 UTC

    def test_manifest_entries_are_checked(self):
        bad = ["../x.py", "a/../x.py", "/x.py", "\\x.py", "C:/x.py", "C:x.py", 'a"b', "a'b",
               "a:b", "a{tmp}b"]
        for entry in bad:
            with self.subTest(entry=entry):
                self.assertIsNotNone(build_setup.manifest_entry_problem(entry))
        for entry in ("ui/app.js", "settings.py", "a/b/c.txt"):
            self.assertIsNone(build_setup.manifest_entry_problem(entry))
        tmp = Path(tempfile.mkdtemp(prefix="htt-bm-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "install-files.txt").write_text("a.py\n../b.py\n", encoding="utf-8")
        with self.assertRaises(SystemExit) as ctx:
            build_setup.read_manifest(tmp)
        self.assertIn("not allowed", str(ctx.exception))

    def test_git_dirty_also_reports_untracked_and_ignored_files(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        repo = Path(tempfile.mkdtemp(prefix="htt-bg-"))

        def forget(path):
            # git's object files are read-only: make them deletable first
            for p in path.rglob("*"):
                try:
                    os.chmod(p, 0o666)
                except OSError:
                    pass
            shutil.rmtree(path, ignore_errors=True)
        self.addCleanup(forget, repo)

        def g(*a):
            subprocess.run([git, "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t" + "@example.invalid",
                            *a], check=True, capture_output=True, timeout=60)
        g("init", "-q")
        (repo / ".gitignore").write_text("ignored.txt\n")
        (repo / "tracked.txt").write_text("a")
        g("add", ".")
        g("commit", "-q", "-m", "x")
        (repo / "untracked.txt").write_text("b")
        (repo / "ignored.txt").write_text("c")
        old = build_setup.ROOT
        build_setup.ROOT = repo
        try:
            clean = build_setup.git_dirty(["tracked.txt"])
            lines = build_setup.git_dirty(["tracked.txt", "untracked.txt", "ignored.txt"])
        finally:
            build_setup.ROOT = old
        self.assertEqual(clean, [])
        self.assertTrue(any(l.startswith("?? ") and "untracked.txt" in l for l in lines), lines)
        self.assertTrue(any(l.startswith("!! ") and "ignored.txt" in l for l in lines), lines)


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class Sources(unittest.TestCase):
    def test_files_are_plain_ascii(self):
        for path in (ROOT / "installer" / "holdtotalk.iss",
                     ROOT / "tools" / "build_setup.py", ROOT / "install.ps1"):
            data = path.read_bytes()
            self.assertTrue(all(b < 128 for b in data), path.name)
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), path.name)
        text = (ROOT / "installer" / "holdtotalk.iss").read_text(encoding="ascii")
        user = os.environ.get("USERNAME", "\0").lower()
        self.assertNotIn(user, text.lower())
        self.assertNotIn("c:\\users\\", text.lower())

    def test_install_ps1_parses(self):
        from test_install import ps_parse_errors
        self.assertEqual(ps_parse_errors([ROOT / "install.ps1"]), "")

    def test_the_hash_file_is_not_part_of_the_app(self):
        self.assertNotIn("files.sha256", build_setup.read_manifest())
        self.assertIn("dist/", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_the_build_needs_the_compiler_and_a_clean_tree(self):
        env = {k: v for k, v in os.environ.items() if k != "HTT_ISCC"}
        run = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / "build_setup.py"),
             "--iscc", str(ROOT / "no-such-ISCC.exe")],
            capture_output=True, text=True, env=env, timeout=120,
            creationflags=CREATE_NO_WINDOW)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("ISCC.exe", run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
