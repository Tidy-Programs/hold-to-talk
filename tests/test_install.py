"""The release files: install-files.txt (the copy allow-list), the requirement
split, the .bat/.ps1 scripts, app.ico, .gitignore / .gitattributes, and a real
install -> update -> uninstall run of install.bat / uninstall.bat into a
temporary folder.

Nothing here touches the real profile: the installer runs only with its test
overrides (a temporary install folder, environment, Start menu and Desktop
folders and a throw-away registry key under HKCU\\Software), with -NoDeps (no
pip), -SkipModel (no download) and -NoLaunch (the app is never started). The
end-to-end tests need Windows PowerShell and a 64-bit Python 3.10-3.13 with
tkinter (as the installer itself does); no GPU, microphone, Ollama or network
access is used.
"""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import ast
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
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
    "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
HAVE_PS = POWERSHELL.is_file()
SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
CMD = str(SYSROOT / "System32" / "cmd.exe")


def ps_parse_errors(files, timeout=120):
    """The PowerShell parser's complaints about these script files, as text
    ("" when all of them parse). The files are only parsed, never run. Their
    names travel in an environment variable: anything put after
    "-Command <text>" on the command line is added to the command text, so a
    file name there would be run as a script."""
    code = ("$e = $null; foreach ($f in ($env:HTT_PARSE_FILES -split '\\|'))"
            " { [void][System.Management.Automation.Language.Parser]::"
            "ParseFile($f, [ref]$null, [ref]$e);"
            " $e | ForEach-Object { $f + ': ' + $_.Message } }; 'parsed'")
    env = dict(os.environ, HTT_PARSE_FILES="|".join(str(f) for f in files))
    out = subprocess.run([str(POWERSHELL), "-NoProfile", "-Command", code],
                         capture_output=True, text=True, timeout=timeout,
                         env=env)
    lines = out.stdout.strip().splitlines()
    if not lines or lines[-1] != "parsed":
        return "the parse check did not finish: " + out.stdout + out.stderr
    return "\n".join(lines[:-1])


# Names that must never be copied into an install, whatever the source
# folder holds (the allow-list must not contain any of them).
PERSONAL = re.compile(
    r"(^|/)(config\.json.*|history\.sqlite3.*|.*\.sqlite3.*|.*\.log(\.\d+)?|"
    r".*\.migrated.*|.*\.bad(-.*)?|.*\.wav|\.env.*)$", re.IGNORECASE)


def manifest(root=ROOT):
    entries = []
    for line in (root / "install-files.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def settings_terms_version():
    import settings
    return settings.TERMS_VERSION


def local_modules():
    return {p.stem for p in ROOT.glob("*.py")}


def imported_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".")[0])
    return out


class AllowList(unittest.TestCase):
    def test_every_listed_file_exists_once(self):
        entries = manifest()
        self.assertEqual(len(entries), len(set(e.lower() for e in entries)),
                         "a file is listed twice")
        for rel in entries:
            self.assertTrue((ROOT / rel).is_file(), f"{rel} is listed but missing")
            self.assertNotIn("\\", rel, "use / in install-files.txt")
            self.assertFalse(rel.startswith(("/", "..")), rel)

    def test_every_python_module_is_listed(self):
        listed = set(manifest())
        for path in ROOT.glob("*.py"):
            self.assertIn(path.name, listed,
                          f"{path.name} is a program module but is not in "
                          "install-files.txt: it would not be installed")

    def test_every_ui_file_is_listed(self):
        listed = set(manifest())
        for path in (ROOT / "ui").rglob("*"):
            if path.is_file():
                rel = path.relative_to(ROOT).as_posix()
                self.assertIn(rel, listed, f"{rel} is not in install-files.txt")

    def test_what_the_server_and_helper_load_is_listed(self):
        import api
        import mousehook
        listed = set(manifest())
        for file_name, _ in api.STATIC.values():
            self.assertIn("ui/" + file_name, listed)
        self.assertIn(mousehook.HELPER.name, listed)

    def test_listed_modules_import_only_listed_modules(self):
        listed = set(manifest())
        local = local_modules()
        for rel in manifest():
            if not rel.endswith(".py"):
                continue
            for name in imported_names(ROOT / rel) & local:
                self.assertIn(name + ".py", listed, f"{rel} imports {name}")

    def test_no_personal_data_name_is_listed(self):
        for rel in manifest():
            self.assertIsNone(PERSONAL.search(rel), rel)

    def test_scripts_and_documents_are_listed(self):
        listed = set(manifest())
        for name in ("install.bat", "install.ps1", "install-gui.ps1",
                     "htt-common.ps1",
                     "uninstall.bat", "uninstall.ps1", "uninstall-gui.ps1", "start.bat",
                     "install-files.txt", "app.ico", "requirements.txt",
                     "requirements-cuda.txt", "README.md", "LICENSE",
                     "TERMS.md", "terms.py",
                     "THIRD-PARTY-NOTICES.md", "SECURITY.md", "CHANGELOG.md"):
            self.assertIn(name, listed)
        for name in ("ARCHITECTURE.md", ".gitignore", ".github"):
            self.assertNotIn(name, listed)


class Notices(unittest.TestCase):
    """The plain-language liability and security notices."""

    def test_readme_has_the_no_warranty_section(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        m = re.search(r"^### No warranty\s*$(.*?)^#{1,3} ", text,
                      re.M | re.S)
        self.assertIsNotNone(m, "README.md needs a 'No warranty' section")
        body = " ".join(m.group(1).split())
        for needle in ("no warranty", "LICENSE", "THIRD-PARTY-NOTICES.md"):
            self.assertIn(needle, body)
        before = text.index("## Before you install")
        self.assertGreater(text.index("### No warranty"), before)
        self.assertLess(text.index("### No warranty"),
                        text.index("## Step 0"))

    def test_security_policy_exists_and_has_no_contact_detail(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Report a vulnerability", " ".join(text.replace(
            "**", "").split()))
        self.assertNotRegex(text, r"[\w.+-]+@[\w-]+\.[\w.-]+")
        self.assertNotRegex(text, r"https?://")

    def test_license_and_notices_ship_with_the_install(self):
        listed = set(manifest())
        for name in ("LICENSE", "TERMS.md", "THIRD-PARTY-NOTICES.md",
                     "SECURITY.md"):
            self.assertIn(name, listed)
            self.assertTrue((ROOT / name).is_file())

    def test_the_window_says_it_has_no_warranty(self):
        js = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Free to use; not to be passed on, published or sold. "
                      "No warranty. See LICENSE.", js)


class Requirements(unittest.TestCase):
    def names(self, filename):
        out = []
        for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                out.append(re.split(r"[<>=!~ ;\[]", line, 1)[0].lower())
        return out

    def test_core_has_no_nvidia_and_cuda_only_nvidia(self):
        core, cuda = self.names("requirements.txt"), self.names(
            "requirements-cuda.txt")
        self.assertTrue(core and cuda)
        self.assertFalse([n for n in core if n.startswith("nvidia")])
        self.assertTrue(all(n.startswith("nvidia-") for n in cuda), cuda)

    def test_every_third_party_import_is_a_core_requirement(self):
        stdlib = set(sys.stdlib_module_names)
        found = set()
        for path in ROOT.glob("*.py"):
            found |= imported_names(path) - stdlib - local_modules()
        # Import name -> the distribution that provides it. ctranslate2 comes
        # with faster-whisper; stt.py only touches it lazily. The same goes
        # for huggingface_hub (faster-whisper requires it, the lock file pins
        # it): stt.py uses it lazily to complete a half-finished download.
        provided = {"numpy": "numpy", "sounddevice": "sounddevice",
                    "faster_whisper": "faster-whisper", "pystray": "pystray",
                    "PIL": "pillow", "ctranslate2": "faster-whisper",
                    "huggingface_hub": "faster-whisper"}
        self.assertEqual(found - set(provided), set(),
                         "a new third-party import: add it to requirements.txt "
                         "and to THIRD-PARTY-NOTICES.md")
        core = set(self.names("requirements.txt"))
        for dist in {provided[n] for n in found}:
            self.assertIn(dist, core)

    def test_every_requirement_has_a_range(self):
        for filename in ("requirements.txt", "requirements-cuda.txt"):
            for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if line:
                    self.assertRegex(line, r">=.*,\s*<", f"{filename}: {line}")


def make_locks_module():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import make_locks
    finally:
        sys.path.pop(0)
    return make_locks


class LockFiles(unittest.TestCase):
    """W8: requirements*.lock.txt pin the exact, hashed versions of the
    ranges in requirements*.txt (made by tools/make_locks.py)."""

    PAIRS = (("requirements.txt", "requirements.lock.txt"),
             ("requirements-cuda.txt", "requirements-cuda.lock.txt"))

    def test_each_lock_agrees_with_its_range_file(self):
        ml = make_locks_module()
        for rng, lock in self.PAIRS:
            self.assertEqual(ml.problems(ROOT / rng, ROOT / lock), [],
                             f"{lock} is out of date: run "
                             r"tools\make_locks.py (see the README)")

    def test_the_test_fails_when_they_disagree(self):
        ml = make_locks_module()
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lock = d / "l.lock.txt"
            shutil.copy(ROOT / "requirements.lock.txt", lock)
            # a new requirement
            rng = d / "r.txt"
            rng.write_text((ROOT / "requirements.txt").read_text(
                encoding="utf-8") + "\nsix>=1.99,<2\n", encoding="utf-8")
            found = ml.problems(rng, lock)
            self.assertTrue(any("changed" in p for p in found), found)
            self.assertTrue(any("six" in p and "outside" in p for p in found),
                            found)
            # a range that no longer contains the pinned version
            rng.write_text("numpy>=9,<10\n", encoding="utf-8")
            self.assertTrue(ml.problems(rng, lock))
            # a requirement the lock does not know
            rng.write_text("brand-new-package>=1,<2\n", encoding="utf-8")
            self.assertTrue(any("not in the lock" in p
                                for p in ml.problems(rng, lock)))

    def test_locks_are_exact_hashed_and_split_like_the_ranges(self):
        ml = make_locks_module()
        core, _ = ml.parse_lock(ROOT / "requirements.lock.txt")
        cuda, _ = ml.parse_lock(ROOT / "requirements-cuda.lock.txt")
        self.assertFalse([n for n in core if n.startswith("nvidia")])
        self.assertTrue(cuda and all(n.startswith("nvidia-") for n in cuda))
        # the transitive packages the review named are pinned
        for name in ("ctranslate2", "huggingface-hub", "tokenizers", "av",
                     "onnxruntime", "faster-whisper", "numpy"):
            self.assertIn(name, core)
        self.assertFalse(set(core) & set(cuda))
        for pins in (core, cuda):
            for name, (version, hashes) in pins.items():
                self.assertRegex(version, r"^\d+(\.\d+)*$", name)
                self.assertTrue(hashes, name)
        for lock in ("requirements.lock.txt", "requirements-cuda.lock.txt"):
            text = (ROOT / lock).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"[A-Za-z]:\\|file:|@ ")
            self.assertNotIn(os.environ.get("USERNAME", "\0").lower(),
                             text.lower())

    def test_the_installer_uses_the_locks_by_default(self):
        install = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("'.lock.txt'", install)
        self.assertIn("UseRanges", install)
        self.assertIn("'--only-binary', ':all:'", install)
        listed = set(manifest())
        self.assertIn("requirements.lock.txt", listed)
        self.assertIn("requirements-cuda.lock.txt", listed)
        # a changed lock file makes the installer reinstall (hash of the
        # file it installs from)
        self.assertIn("Get-ReqFile $Dir 'requirements'", install)

    def test_wheel_selection_for_hashes(self):
        ml = make_locks_module()
        for good in ("x-1-py3-none-any.whl", "x-1-py2.py3-none-any.whl",
                     "x-1-cp312-cp312-win_amd64.whl",
                     "x-1-cp310-abi3-win_amd64.whl",
                     "x-1-cp37-abi3-win_amd64.whl"):
            self.assertTrue(ml.wheel_ok(good), good)
        for bad in ("x-1-cp312-cp312-win32.whl", "x-1-cp39-cp39-win_amd64.whl",
                    "x-1-cp314-cp314-win_amd64.whl",
                    "x-1-cp313-cp313t-win_amd64.whl",
                    "x-1-cp312-cp312-manylinux_2_17_x86_64.whl"):
            self.assertFalse(ml.wheel_ok(bad), bad)


class ScriptFiles(unittest.TestCase):
    def test_bat_files_are_ascii_with_crlf(self):
        for name in ("install.bat", "uninstall.bat", "start.bat"):
            data = (ROOT / name).read_bytes()
            self.assertTrue(all(b < 128 for b in data), name)
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), name)

    def test_ps1_files_are_ascii(self):
        # Windows PowerShell 5.1 reads a file without a byte-order mark as ANSI.
        for name in ("install.ps1", "install-gui.ps1", "uninstall.ps1",
                     "uninstall-gui.ps1", "htt-common.ps1"):
            data = (ROOT / name).read_bytes()
            self.assertTrue(all(b < 128 for b in data), name)

    @unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
    def test_ps1_files_parse(self):
        files = [ROOT / n for n in ("install.ps1", "install-gui.ps1",
                                    "uninstall.ps1", "uninstall-gui.ps1",
                                    "htt-common.ps1")]
        self.assertEqual(ps_parse_errors(files), "")

    @unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
    def test_the_parse_check_reports_a_broken_script(self):
        # The check must only parse, never run: this file would leave a marker
        # behind if it were run, and it has a syntax error that must be reported.
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "ran.txt"
            bad = Path(d) / "bad.ps1"
            bad.write_text("Set-Content -LiteralPath '%s' -Value ran\n"
                           "if ($true) {\n" % marker, encoding="ascii")
            good = Path(d) / "good.ps1"
            good.write_text("Set-Content -LiteralPath '%s' -Value ran\n" % marker,
                            encoding="ascii")
            self.assertEqual(ps_parse_errors([good]), "")
            self.assertIn("bad.ps1", ps_parse_errors([good, bad]))
            self.assertFalse(marker.exists(), "the parse check ran a script")

    def test_installer_scripts_use_the_documented_choices(self):
        install = (ROOT / "install.ps1").read_text(encoding="ascii")
        for needle in ("'3.12', '3.13', '3.11', '3.10'", "WindowsApps",
                       "AMD64", "tkinter", "htt-requirements.sha256",
                       "'requirements-cuda'", "nvidia-smi.exe",
                       "--quit", "gemma4:e2b-it-qat"):
            self.assertIn(needle, install + (ROOT / "htt-common.ps1")
                          .read_text(encoding="ascii"))
        bat = (ROOT / "install.bat").read_text(encoding="ascii")
        self.assertIn("-ExecutionPolicy Bypass -File", bat)
        self.assertIn("-ExecutionPolicy Bypass -File",
                      (ROOT / "start.bat").read_text(encoding="ascii"))

    def test_helper_scripts_run_isolated_from_a_private_folder(self):
        # W7a: the probe and model helper were written to %TEMP% and run
        # as "python probe.py": a json.py planted beside them was imported.
        install = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("@('-I', $script:ProbeFile)", install)
        self.assertEqual(install.count("'-I', $script:ModelFile"), 2)
        self.assertIn("@('-I', '-m', 'venv', $envDir)", install)
        self.assertIn("'-I', '-m', 'pip', 'install'", install)
        self.assertIn("NewGuid", install)              # a fresh folder
        self.assertNotIn('htt-probe-{0}', install)
        self.assertNotIn("htt-probe", install)
        self.assertNotIn("htt-model", install)

    def test_isolated_mode_ignores_a_planted_module(self):
        install = (ROOT / "install.ps1").read_text(encoding="ascii")
        probe = re.search(r"\$ProbeCode = @'\r?\n(.*?)\r?\n'@", install,
                          re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "probe.py").write_text(probe, encoding="ascii")
            (d / "json.py").write_text(
                "import pathlib\n"
                "pathlib.Path(__file__).with_name('PLANTED').write_text('x')\n"
                "def dumps(o): return '{}'\n", encoding="ascii")
            run = lambda *a: subprocess.run(            # noqa: E731
                [sys.executable, "-B", *a, str(d / "probe.py")],
                capture_output=True, text=True, timeout=60, cwd=d)
            run()                                       # as it used to be
            self.assertTrue((d / "PLANTED").exists(), "not reproduced")
            (d / "PLANTED").unlink()
            out = run("-I")
            self.assertFalse((d / "PLANTED").exists(), "planted file ran")
            self.assertIn('"exe"', out.stdout)

    def test_icon_is_a_multi_size_ico(self):
        from PIL import Image
        with Image.open(ROOT / "app.ico") as ico:
            self.assertEqual(ico.format, "ICO")
            sizes = set(ico.info["sizes"])
        for size in (16, 32, 48, 256):
            self.assertIn((size, size), sizes)

    def test_gitignore_and_gitattributes(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("config.json", "history.sqlite3*", "*.sqlite3*",
                        "*.log", "*.migrated*", "*.bad*", "*.wav", "venv/",
                        "__pycache__/", ".idea/", ".vscode/", "*.m4a", ".env"):
            self.assertIn(pattern, ignore.splitlines(), pattern)
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attrs, r"\*\.bat\s+text\s+eol=crlf")

    def test_icon_generator_matches_the_committed_icon(self):
        import tempfile as tf
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import make_icon
        finally:
            sys.path.pop(0)
        from PIL import Image
        with tf.TemporaryDirectory() as d:
            out = Path(d) / "x.ico"
            make_icon.build(out)
            with Image.open(out) as new, Image.open(ROOT / "app.ico") as old:
                self.assertEqual(set(new.info["sizes"]), set(old.info["sizes"]))


# -- a real install, update and uninstall into a temporary folder ------------------

def run_bat(bat, args, env, timeout=600):
    """Run a .bat the way a console does and return (exit code, output)."""
    line = f'{CMD} /s /c ""{bat}" {args}"'
    out = subprocess.run(line, env=env, capture_output=True, timeout=timeout)
    return out.returncode, out.stdout.decode("utf-8", "replace") + \
        out.stderr.decode("utf-8", "replace")


def read_shortcut(path):
    code = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            "$env:HTT_LNK);ConvertTo-Json @{target=$s.TargetPath;"
            "args=$s.Arguments;dir=$s.WorkingDirectory;icon=$s.IconLocation}")
    out = subprocess.run([str(POWERSHELL), "-NoProfile", "-Command", code],
                         env=dict(os.environ, HTT_LNK=str(path)),
                         capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout)


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class InstallEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A short path: the project folder can be long, Windows stops at 260.
        cls.tmp = Path(tempfile.mkdtemp(prefix="htt-i-"))
        cls.src = cls.tmp / "Src Caf\u00e9 \u00e9"       # a space and a non-ASCII letter
        cls.src.mkdir()
        for rel in manifest():
            dest = cls.src / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, dest)
        cls.decoys = ["config.json", "history.sqlite3", "history.sqlite3-wal",
                      "dictate.log", "dictate.log.migrated", "config.json.bad-1",
                      "recording.wav", "notes.txt", "extra_module.py"]
        for name in cls.decoys:
            (cls.src / name).write_text("decoy", encoding="utf-8")
        cls.key = fr"Software\HttTest-{uuid.uuid4().hex[:8]}\Uninstall\HoldToTalk"
        cls.prog = cls.tmp / "prog"
        cls.env_dir = cls.tmp / "home" / "venv"
        cls.data = cls.tmp / "home" / "data"
        cls.sm, cls.dt = cls.tmp / "sm", cls.tmp / "dt"
        cls.env = {k: v for k, v in os.environ.items()
                   if not k.startswith("HTT_") and k != "HOLDTOTALK_HOME"}
        cls.env["HTT_NO_PAUSE"] = "1"
        cls.overrides = (f'-InstallRoot "{cls.prog}" -EnvDir "{cls.env_dir}" '
                         f'-StartMenuDir "{cls.sm}" -DesktopDir "{cls.dt}" '
                         f'-RegKey "{cls.key}"')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        parts = cls.key.split("\\")
        for n in range(len(parts), 1, -1):    # never "Software" itself
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, "\\".join(parts[:n]))
            except OSError:
                pass            # already gone (the uninstaller removes the leaf)

    def install(self):
        return run_bat(self.src / "install.bat",
                       f"-Yes -NoDeps -SkipModel -NoLaunch {self.overrides}",
                       self.env)

    def reg(self):
        out = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                name, value, kind = winreg.EnumValue(k, i)
                out[name] = (value, kind)
        return out

    def test_1_install_update_uninstall(self):
        # -- first install
        code, out = self.install()
        self.assertEqual(code, 0, out)
        # -Yes is not silent: it prints the summary, and says that it does
        # not accept the terms for the person who will use the app. It writes
        # no acceptance record at all (the app asks that person).
        self.assertIn("Please read the Terms of Use", out)
        self.assertIn("no warranty of any kind", out)
        self.assertIn("THIRD-PARTY-NOTICES.md", out)
        self.assertIn("TERMS.md", out)
        flat = " ".join(out.split())
        self.assertIn("Using the -Yes switch installs without questions, but "
                      "it does not accept the Terms of Use for the person who "
                      "will use the app. The app will show the terms and ask "
                      "that person to choose I agree before it dictates.",
                      flat)
        self.assertNotIn("you accept the Terms of Use", flat)
        self.assertIn("This includes the sections on no warranty, limits of "
                      "liability, assumption of risk, indemnity and disputes "
                      "(sections 9 to 14).", flat)
        self.assertIn("Using Python 3.", out)
        self.assertFalse((self.data / "terms-accepted.json").exists())
        self.assertNotIn("Recorded that", out)
        self.assertIn("No acceptance of the Terms of Use was recorded", out)
        self.assertTrue((self.prog / "TERMS.md").is_file())
        for rel in manifest():
            self.assertTrue((self.prog / rel).is_file(), rel)
        for name in self.decoys:
            self.assertFalse((self.prog / name).exists(),
                             f"{name} must not be copied")
        self.assertTrue((self.env_dir / "Scripts" / "pythonw.exe").is_file())

        # The installer writes folder names in their long form; %TEMP% (and
        # so self.prog) can be in the short 8.3 form, as on GitHub's machines.
        prog = Path(os.path.realpath(self.prog))
        for lnk in (self.sm / "Hold to talk.lnk", self.dt / "Hold to talk.lnk"):
            sc = read_shortcut(lnk)
            self.assertEqual(Path(sc["target"]).resolve(),
                             (self.env_dir / "Scripts" / "pythonw.exe").resolve())
            self.assertEqual(sc["args"], f'"{prog / "dictate.py"}" --open')
            self.assertEqual(Path(sc["dir"]).resolve(), self.prog.resolve())
            self.assertEqual(sc["icon"], f"{prog / 'app.ico'},0")

        values = self.reg()
        self.assertEqual(values["DisplayName"][0], "Hold to talk")
        import settings
        self.assertEqual(values["DisplayVersion"][0], settings.APP_VERSION)
        self.assertEqual(values["Publisher"][0], "Hold to talk contributors")
        self.assertEqual(values["InstallLocation"][0], str(prog))
        self.assertEqual(values["DisplayIcon"][0], str(prog / "app.ico"))
        self.assertEqual(values["UninstallString"][0],
                         f'"{prog / "uninstall.bat"}"')
        for name in ("NoModify", "NoRepair", "EstimatedSize"):
            self.assertEqual(values[name][1], winreg.REG_DWORD, name)
        self.assertEqual(values["NoModify"][0], 1)
        self.assertGreater(values["EstimatedSize"][0], 0)

        # -- the user's data, a stale file and a changed file
        (self.data / "logs").mkdir(parents=True)
        (self.data / "config.json").write_text('{"dummy": true}')
        (self.prog / "old_module.py").write_text("# left by an old version")
        (self.prog / "stt.py").write_text("# damaged")
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertFalse((self.prog / "old_module.py").exists())
        self.assertEqual((self.prog / "stt.py").read_bytes(),
                         (ROOT / "stt.py").read_bytes())
        self.assertEqual((self.data / "config.json").read_text(),
                         '{"dummy": true}')
        self.assertIn("found and working", out)       # the environment check

        # -- a broken environment is made again
        (self.env_dir / "Scripts" / "python.exe").unlink()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("Making a new one", out)
        self.assertTrue((self.env_dir / "Scripts" / "python.exe").is_file())

        # -- uninstall (as Settings > Apps does), keeping the data
        env = dict(self.env, HTT_INSTALL_ROOT=str(self.prog),
                   HTT_ENV_DIR=str(self.env_dir), HTT_START_MENU_DIR=str(self.sm),
                   HTT_DESKTOP_DIR=str(self.dt), HTT_REG_KEY=self.key)
        code, out = run_bat(self.prog / "uninstall.bat", "-Yes", env)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.prog.exists(), "install folder is gone")
        self.assertFalse(self.env_dir.exists())
        self.assertFalse((self.sm / "Hold to talk.lnk").exists())
        self.assertFalse((self.dt / "Hold to talk.lnk").exists())
        with self.assertRaises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key)
        self.assertEqual((self.data / "config.json").read_text(),
                         '{"dummy": true}', "user data is kept by default")

    def test_2_uninstall_can_delete_the_data(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "config.json").write_text("{}")
        self.assertFalse((self.data / "terms-accepted.json").exists())
        fakes.accept_terms_in(self.data)            # as if the person agreed
        self.assertTrue((self.data / "terms-accepted.json").is_file())
        env = dict(self.env, HTT_INSTALL_ROOT=str(self.prog),
                   HTT_ENV_DIR=str(self.env_dir), HTT_START_MENU_DIR=str(self.sm),
                   HTT_DESKTOP_DIR=str(self.dt), HTT_REG_KEY=self.key)
        code, out = run_bat(self.prog / "uninstall.bat", "-Yes -DeleteData", env)
        self.assertEqual(code, 0, out)
        self.assertFalse((self.data / "terms-accepted.json").exists(),
                         "delete-my-data removes the acceptance record")
        self.assertFalse(self.data.exists())
        self.assertFalse(self.env_dir.parent.exists())

    def uninstall_env(self, **extra):
        return dict(self.env, HTT_INSTALL_ROOT=str(self.prog),
                    HTT_ENV_DIR=str(self.env_dir),
                    HTT_START_MENU_DIR=str(self.sm),
                    HTT_DESKTOP_DIR=str(self.dt), HTT_REG_KEY=self.key,
                    **extra)

    def test_5_uninstall_removes_the_start_with_windows_entry(self):
        # W7c: the installed copy's entry, in a throw-away stand-in for the
        # Run key (HTT_RUN_KEY), through dictate.py --uninstall and the
        # uninstaller's own check afterwards. Never the real Run key.
        code, out = self.install()
        self.assertEqual(code, 0, out)
        run = fr"Software\HoldToTalkTest\{uuid.uuid4().hex[:8]}\Run"
        approved = run + r"\StartupApproved"
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "HoldToTalk", 0, winreg.REG_SZ,
                                  f'"{self.env_dir}\\Scripts\\pythonw.exe" x')
                winreg.SetValueEx(k, "SomeOtherProgram", 0, winreg.REG_SZ, "y")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, approved, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "HoldToTalk", 0, winreg.REG_BINARY,
                                  b"\x03" + b"\0" * 11)
            code, out = run_bat(self.prog / "uninstall.bat", "-Yes",
                                self.uninstall_env(HTT_RUN_KEY=run))
            self.assertEqual(code, 0, out)
            self.assertIn("Start with Windows", out)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run) as k:
                names = [winreg.EnumValue(k, i)[0]
                         for i in range(winreg.QueryInfoKey(k)[1])]
            self.assertEqual(names, ["SomeOtherProgram"], out)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, approved) as k:
                self.assertEqual(winreg.QueryInfoKey(k)[1], 0)
        finally:
            for key in (approved, run, run.rsplit("\\", 1)[0],
                        r"Software\HoldToTalkTest"):     # (fails if in use)
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
                except OSError:
                    pass

    def test_6_deleting_data_removes_only_what_the_app_made(self):
        # W7b: HOLDTOTALK_HOME could point at any folder.
        code, out = self.install()
        self.assertEqual(code, 0, out)
        home = self.tmp / "elsewhere"
        (home / "logs").mkdir(parents=True)
        (home / "Photos").mkdir()
        # Every name settings.py, textrules.py and dictate.py can create in
        # the data folder (see Remove-AppDataFiles).
        mine = ["config.json", "config.json.bad", "config.json.bad-20260101-000000",
                "config.json.bad-20260101-000000-2", "config.json.copying",
                "config.json.migrated", "config.json.migrated-2",
                ".cfg-ab12cd.tmp",
                "history.sqlite3", "history.sqlite3-wal",
                "history.sqlite3-shm", "history.sqlite3-journal",
                "history.sqlite3.migrated", "history.sqlite3.migrated-wal",
                "history.sqlite3.copying", "history.sqlite3.copying-journal",
                "history.sqlite3.corrupt-20260101-000000",
                "history.sqlite3.corrupt-20260102-123456",
                "terms-accepted.json", ".terms-ab12cd34.tmp",
                "audio-muted.json", ".audio-muted-4321.tmp",
                "logs/dictate.log", "logs/dictate.log.1"]
        theirs = ["notes.docx", "Photos/me.jpg", "logs/other.log",
                  "history.sqlite3.backup-by-me", "config.json.old",
                  "history.sqlite3.corrupt", "my-config.json.copying",
                  "terms-accepted.json.bak", "my-terms-accepted.json",
                  "audio-muted.json.bak", "my-audio-muted.json"]
        for rel in mine + theirs:
            (home / rel).write_text("x", encoding="utf-8")
        code, out = run_bat(self.prog / "uninstall.bat", "-Yes -DeleteData",
                            self.uninstall_env(HOLDTOTALK_HOME=str(home)))
        self.assertEqual(code, 0, out)
        for rel in mine:
            self.assertFalse((home / rel).exists(), rel)
        for rel in theirs:
            self.assertTrue((home / rel).exists(), f"{rel} was deleted")
        self.assertIn("Left in place", out)
        self.assertIn("notes.docx", out)

    def test_6b_a_data_folder_with_only_app_files_goes_entirely(self):
        home = self.tmp / "only-mine"
        (home / "logs").mkdir(parents=True)
        for rel in ("config.json", "history.sqlite3", "logs/dictate.log",
                    "terms-accepted.json", "audio-muted.json"):
            (home / rel).write_text("x", encoding="utf-8")
        script = ("& { . $env:HTT_COMMON; $l = @(Remove-AppDataFiles "
                  "$env:HTT_DIR); 'left=' + $l.Count }")
        out = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            env=dict(os.environ, HTT_COMMON=str(ROOT / "htt-common.ps1"),
                     HTT_DIR=str(home)),
            capture_output=True, text=True, timeout=60)
        self.assertIn("left=0", out.stdout, out.stdout + out.stderr)
        self.assertFalse(home.exists())

    def ask_install(self, name, answer, extra=""):
        """install.bat without -Yes into its own temporary places, with
        `answer` typed on standard input (None: input closed). pip, the model
        download and the launch stay off. Returns (code, output, places)."""
        base = self.tmp / name
        places = dict(prog=base / "prog", env=base / "venv", sm=base / "sm",
                      dt=base / "dt", key=f"{self.key}-{name}")
        line = (f'{CMD} /s /c ""{self.src / "install.bat"}" -NoDeps -SkipModel '
                f'-NoLaunch {extra} -InstallRoot "{places["prog"]}" '
                f'-EnvDir "{places["env"]}" -StartMenuDir "{places["sm"]}" '
                f'-DesktopDir "{places["dt"]}" -RegKey "{places["key"]}""')
        out = subprocess.run(
            line, env=self.env, capture_output=True, timeout=600,
            input=None if answer is None else answer.encode("ascii"),
            stdin=subprocess.DEVNULL if answer is None else None)
        text = out.stdout.decode("utf-8", "replace") + \
            out.stderr.decode("utf-8", "replace")
        return out.returncode, text, places

    def test_7_anything_but_the_words_i_agree_stops_having_changed_nothing(self):
        answers = ("y\r\n", "yes\r\n", "Y\r\n", "n\r\n", "no\r\n", "\r\n",
                   "agree\r\n", "I agree to\r\n", "I  agree\r\n", "iagree\r\n",
                   "ok\r\n", None)
        for i, answer in enumerate(answers):
            with self.subTest(answer=answer):
                code, out, p = self.ask_install(f"decline{i}", answer)
                self.assertEqual(code, 3, out)
                self.assertIn("Please read the Terms of Use", out)
                self.assertIn("TERMS.md", out)
                self.assertIn("I agree", out)
                self.assertIn("Nothing was changed", out)
                self.assertNotIn("Looking for Python", out)
                self.assertNotIn("Recorded that", out)
                for key in ("prog", "env", "sm", "dt"):
                    self.assertFalse(p[key].exists(), key)
                # no folder at all, so no data folder and no acceptance record
                self.assertFalse((self.tmp / f"decline{i}").exists())
                with self.assertRaises(OSError):
                    winreg.OpenKey(winreg.HKEY_CURRENT_USER, p["key"])

    def test_7b_reading_the_terms_here_does_not_accept_them(self):
        code, out, p = self.ask_install("readfirst", "read\r\n")
        self.assertEqual(code, 3, out)
        self.assertIn("HOLD TO TALK: TERMS OF USE", out)      # the full text
        self.assertIn("Nothing was changed", out)
        self.assertFalse((self.tmp / "readfirst").exists())

    def test_8_the_words_i_agree_install_in_any_letter_case(self):
        for i, answer in enumerate(("I agree", "i agree", "I AGREE",
                                    "  I Agree  ")):
            with self.subTest(answer=answer):
                code, out, p = self.ask_install(f"accept{i}", answer + "\r\n")
                try:
                    self.assertEqual(code, 0, out)
                    self.assertIn("Please read the Terms of Use", out)
                    self.assertNotIn("Using the -Yes switch", out)
                    self.assertTrue((p["prog"] / "LICENSE").is_file())
                    self.assertTrue((p["prog"] / "TERMS.md").is_file())
                    self.assertTrue(
                        (p["prog"] / "THIRD-PARTY-NOTICES.md").is_file())
                    self.assertTrue((p["prog"] / "SECURITY.md").is_file())
                    self.assertTrue((p["sm"] / "Hold to talk.lnk").is_file())
                    rec = json.loads((p["env"].parent / "data" /
                                      "terms-accepted.json")
                                     .read_text(encoding="utf-8"))
                    self.assertEqual(rec["how"], "installer")
                    self.assertEqual(rec["version"], settings_terms_version())
                    self.assertEqual(
                        [f.name for f in (p["env"].parent / "data").iterdir()],
                        ["terms-accepted.json"])
                finally:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p["key"])
                    except OSError:
                        pass

    def test_9_the_script_keeps_the_acceptance_rules(self):
        install = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("-ieq 'I agree'", install)
        self.assertIn("exit 3", install)
        self.assertIn("'unattended'", install)
        self.assertIn("'installer'", install)
        # the unattended switch never writes a record
        self.assertIn("if ($how -eq 'unattended')", install)
        self.assertLess(install.index("if ($how -eq 'unattended')"),
                        install.index("Write-TermsRecord $layout.DataDir"))
        self.assertIn("type the words:  I agree", install)
        # the record is written only after the terms were accepted and
        # before the first change to the PC
        self.assertLess(install.index("$how = Confirm-Terms"),
                        install.index("Step '1. Looking for Python'"))
        self.assertLess(install.index("Write-TermsRecord $layout.DataDir"),
                        install.index("Step '3. Making sure the app is not "
                                      "running'"))

    def test_8b_the_prompt_says_what_is_being_agreed_to(self):
        code, out, p = self.ask_install("prompt", "no\r\n")
        self.assertEqual(code, 3, out)
        flat = " ".join(out.split())
        self.assertIn("This includes the sections on no warranty, limits of "
                      "liability, assumption of risk, indemnity and disputes "
                      "(sections 9 to 14).", flat)
        self.assertIn("To accept the terms and install, type the words: "
                      "I agree", flat)
        self.assertLess(flat.index("(sections 9 to 14)"),
                        flat.index("type the words: I agree"))

    def test_8c_the_nvidia_runtime_is_asked_about_and_no_is_respected(self):
        for name, answer, installs in (("nv_no", "n", False),
                                       ("nv_yes", "y", True),
                                       ("nv_default", "", True)):
            with self.subTest(answer=answer):
                code, out, p = self.ask_install(
                    name, f"I agree\r\n{answer}\r\n", "-Gpu yes")
                try:
                    self.assertEqual(code, 0, out)
                    flat = " ".join(out.split())
                    self.assertIn("An NVIDIA graphics card was found", flat)
                    self.assertIn("Installing it means accepting NVIDIA's "
                                  "licence for those files.", flat)
                    self.assertIn("If you answer no, the app is installed "
                                  "without it and recognises speech on the "
                                  "processor, which is slower. You can run "
                                  "the installer again later to add it.", flat)
                    self.assertEqual(
                        "Not installing the NVIDIA runtime" in flat,
                        not installs)
                    self.assertTrue((p["sm"] / "Hold to talk.lnk").is_file())
                finally:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p["key"])
                    except OSError:
                        pass

    def test_8d_without_an_nvidia_card_nothing_is_asked(self):
        code, out, p = self.ask_install("nv_none", "I agree\r\n", "-Gpu no")
        try:
            self.assertEqual(code, 0, out)
            self.assertNotIn("NVIDIA", out)
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, p["key"])
            except OSError:
                pass

    def test_8e_yes_installs_the_runtime_and_prints_the_notice(self):
        base = self.tmp / "nv_yes_switch"
        code, out = run_bat(
            self.src / "install.bat",
            f'-Yes -NoDeps -SkipModel -NoLaunch -Gpu yes -InstallRoot "{base / "prog"}" '
            f'-EnvDir "{base / "venv"}" -StartMenuDir "{base / "sm"}" '
            f'-DesktopDir "{base / "dt"}" -RegKey "{self.key}-nvyes"', self.env)
        try:
            self.assertEqual(code, 0, out)
            flat = " ".join(out.split())
            self.assertIn("Installing it means accepting NVIDIA's licence for "
                          "those files.", flat)
            self.assertIn("-Yes: installing the NVIDIA runtime", flat)
            self.assertNotIn("Install the NVIDIA runtime? [Y/n]", flat)
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                                 f"{self.key}-nvyes")
            except OSError:
                pass

    def run_resolve(self, cases):
        """Resolve-GpuChoice (taken out of install.ps1 with the PowerShell
        parser, so nothing else of the installer runs) for each case:
        (detected, hash kept in the environment: None/"cuda"/"nocuda",
        -Yes, -EnvOnly, the answer to the question: True/False/None).
        Returns the results and whether the question was asked."""
        work = self.tmp / "resolve"
        work.mkdir(exist_ok=True)
        script = work / "resolve.ps1"
        script.write_text(r"""
param([string]$Install, [string]$Cases)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Install, [ref]$null, [ref]$null)
$want = 'Get-ReqFile', 'Get-RequirementsHash', 'Resolve-GpuChoice'
foreach ($fn in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
function Ask-YesNo([string]$Question, [bool]$Default = $true) { $script:asked = $true; return $script:answer }
$UseRanges = $false
$out = @()
foreach ($c in (Get-Content -Raw $Cases | ConvertFrom-Json)) {
    $env1 = Join-Path $c.dir 'venv'
    New-Item -ItemType Directory -Force -Path $env1 | Out-Null
    $hf = Join-Path $env1 'htt-requirements.sha256'
    if (Test-Path $hf) { Remove-Item $hf }
    if ($c.hash -eq 'cuda') { Set-Content $hf (Get-RequirementsHash $Install.Replace('install.ps1','') $true) }
    if ($c.hash -eq 'nocuda') { Set-Content $hf (Get-RequirementsHash $Install.Replace('install.ps1','') $false) }
    $Yes = [bool]$c.yes; $EnvOnly = [bool]$c.envonly
    $script:asked = $false; $script:answer = [bool]$c.answer
    $layout = [pscustomobject]@{ EnvDir = $env1 }
    $r = Resolve-GpuChoice ($Install.Replace('install.ps1','')) $layout ([bool]$c.detected)
    $out += [pscustomobject]@{ result = [bool]$r; asked = $script:asked }
}
ConvertTo-Json -InputObject @($out) -Compress
""", encoding="ascii")
        cases_file = work / "cases.json"
        rows = []
        for i, (detected, kept, yes, envonly, answer) in enumerate(cases):
            rows.append({"dir": str(work / f"c{i}"), "detected": detected,
                         "hash": kept, "yes": yes, "envonly": envonly,
                         "answer": bool(answer)})
        cases_file.write_text(json.dumps(rows), encoding="ascii")
        out = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(script), str(ROOT / "install.ps1"), str(cases_file)],
            capture_output=True, text=True, timeout=120)
        json_line = [l for l in out.stdout.splitlines() if l.startswith("[")]
        self.assertTrue(json_line, out.stdout + out.stderr)
        return json.loads(json_line[-1])

    def test_8f_the_recorded_choice_is_respected_or_asked_again_sensibly(self):
        #      detected, kept hash, -Yes, -EnvOnly, answer
        res = self.run_resolve([
            (False, None, False, False, True),      # no card: no runtime, no question
            (True, None, False, False, False),      # asked, "no"
            (True, None, False, False, True),       # asked, "yes"
            (True, None, True, False, False),       # -Yes: yes, no question
            (True, "cuda", False, False, False),    # already installed: no question
            (True, "nocuda", False, False, True),   # an earlier "no": asked again
            (True, "nocuda", False, True, True),    # start.bat keeps the "no"
            (True, None, False, True, True),        # start.bat, first time: as before
        ])
        got = [(r["result"], r["asked"]) for r in res]
        self.assertEqual(got, [(False, False), (False, True), (True, True),
                               (True, False), (True, False), (True, True),
                               (False, False), (True, False)])

    def test_8g_without_the_runtime_the_requirements_hash_differs(self):
        text = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("'--no-cuda--'", text)
        self.assertIn("'--cuda--'", text)
        # a "no" is installed with the plain requirements only
        self.assertIn("if ($UseGpu) {", text)
        self.assertLess(text.index("function Resolve-GpuChoice"),
                        text.index("function Ensure-Environment"))
        stt = (ROOT / "stt.py").read_text(encoding="utf-8")
        self.assertIn("def cuda_runtime_installed", stt)
        self.assertIn("if not cuda_runtime_installed():", stt)

    def test_8h_the_model_download_uses_the_pinned_revision(self):
        text = (ROOT / "install.ps1").read_text(encoding="ascii")
        self.assertIn("MODEL_REVISIONS in stt.py", text)
        self.assertIn("$name, 'check', $rev", text)
        self.assertIn("$name, 'get', $rev", text)
        self.assertIn("revision=rev", text)
        import stt
        for name, rev in stt.MODEL_REVISIONS.items():
            self.assertRegex(
                (ROOT / "stt.py").read_text(encoding="utf-8"),
                rf'"{name}":\s*"{rev}"')

    def test_3_half_overridden_runs_are_refused(self):
        code, out = run_bat(self.src / "install.bat",
                            f'-Yes -NoDeps -InstallRoot "{self.tmp / "x"}"',
                            self.env)
        self.assertEqual(code, 1)
        self.assertIn("all together", out)
        self.assertFalse((self.tmp / "x").exists())

    def test_4_no_suitable_python_says_what_to_install(self):
        fake = self.tmp / "fakepath" / "Microsoft" / "WindowsApps"
        fake.mkdir(parents=True)
        shutil.copy(SYSROOT / "System32" / "whoami.exe", fake / "python.exe")
        env = dict(self.env, PATH=str(fake))
        target = self.tmp / "nopython"
        code, out = run_bat(
            self.src / "install.bat",
            f'-Yes -NoDeps -NoLaunch -InstallRoot "{target / "prog"}" '
            f'-EnvDir "{target / "venv"}" -StartMenuDir "{target / "sm"}" '
            f'-DesktopDir "{target / "dt"}" -RegKey "{self.key}-none"', env)
        self.assertEqual(code, 1)
        self.assertIn("python.org", out)
        self.assertIn("64-bit", out)
        self.assertIn("Microsoft Store", out)
        self.assertFalse(target.exists(), "nothing is created without Python")


STUB_UNINSTALL = r"""
# Stands in for uninstall.ps1: reports who may open the folder it runs from.
$d = Split-Path -Parent $MyInvocation.MyCommand.Path
$acl = Get-Acl -LiteralPath $d
$ids = @($acl.Access | ForEach-Object {
    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value })
$info = @{ dir = $d; protected = $acl.AreAccessRulesProtected; ids = $ids;
           files = @(Get-ChildItem -LiteralPath $d -Name | ForEach-Object { [string]$_ }) }
ConvertTo-Json $info -Compress | Set-Content -Encoding ASCII $env:HTT_STUB_OUT
exit 2
"""


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class UninstallTempFolder(unittest.TestCase):
    """G2: uninstall.bat runs a copy of its scripts from a folder in %TEMP%.
    The folder must be private to this account, like the installer's own
    helper folder (Get-WorkDir). The real uninstall.bat runs here, with a
    stub in place of uninstall.ps1 (nothing is uninstalled) and a temporary
    %TEMP%."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-u-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        shutil.copy(ROOT / "uninstall.bat", self.src / "uninstall.bat")
        (self.src / "uninstall.ps1").write_text(STUB_UNINSTALL,
                                                encoding="ascii")
        (self.src / "htt-common.ps1").write_text("# stub\n", encoding="ascii")
        self.temp = self.tmp / "temp"
        self.temp.mkdir()
        self.out = self.tmp / "out.json"

    def my_sid(self):
        who = subprocess.run([str(SYSROOT / "System32" / "whoami.exe"),
                              "/user", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=60)
        return who.stdout.strip().split(",")[1].strip('"')

    def run_it(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HTT_")}
        env.update(TEMP=str(self.temp), TMP=str(self.temp),
                   HTT_NO_PAUSE="1", HTT_STUB_OUT=str(self.out))
        return run_bat(self.src / "uninstall.bat", "", env)

    def test_the_temporary_folder_is_private_and_removed_afterwards(self):
        code, text = self.run_it()
        self.assertEqual(code, 0, text)
        info = json.loads(self.out.read_text(encoding="ascii"))
        folder = Path(info["dir"])
        self.assertEqual(folder.parent.resolve(), self.temp.resolve())
        self.assertTrue(folder.name.startswith("htt-uninstall-"), folder)
        # inherited access removed, this account the only one let in
        self.assertTrue(info["protected"], info)
        self.assertEqual(set(info["ids"]), {self.my_sid()}, info)
        # both scripts were copied there, and the folder is gone afterwards
        self.assertEqual(sorted(info["files"]),
                         ["htt-common.ps1", "uninstall.ps1"])
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_it_does_not_run_from_a_folder_it_could_not_protect(self):
        # Without icacls the folder cannot be made private: nothing runs.
        env = {k: v for k, v in os.environ.items() if not k.startswith("HTT_")}
        env.update(TEMP=str(self.temp), TMP=str(self.temp),
                   HTT_NO_PAUSE="1", HTT_STUB_OUT=str(self.out),
                   SystemRoot=str(self.tmp / "nowhere"))
        # (cmd.exe itself is started by its full path, so it still runs.)
        out = subprocess.run(
            f'{CMD} /s /c ""{self.src / "uninstall.bat"}""', env=env,
            capture_output=True, timeout=120)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertFalse(self.out.exists(), "the script ran anyway")
        self.assertEqual(list(self.temp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
