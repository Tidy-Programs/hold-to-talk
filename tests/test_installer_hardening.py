"""Hardening of the installer and uninstaller, proved by running it.

Everything here happens inside a temporary folder made by the test: junctions
are made with `mklink /J` there, the delayed-cleanup scripts get that folder as
their temp folder (TMP/TEMP), and nothing touches the real install, the real
profile or the registry. The helper PowerShell script is written to the test's
folder and run with -File; paths and names reach it as arguments or in
environment variables, never as command text.
"""

import fakes  # noqa: F401  (sets HOLDTOTALK_HOME first)

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_setup  # noqa: E402

SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
POWERSHELL = SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
HAVE_PS = POWERSHELL.is_file()
NO_WINDOW = 0x08000000

INSTALL_GUI = ROOT / "install-gui.ps1"
UNINSTALL_GUI = ROOT / "uninstall-gui.ps1"
COMMON = ROOT / "htt-common.ps1"

DRIVER = r"""
param([string]$Mode, [string]$Src, [string]$Fn, [string]$Work)
$ErrorActionPreference = 'Stop'
function Get-FnText([string]$Path, [string]$Name) {
    $errs = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errs)
    $hit = $ast.FindAll({ param($n) ($n -is [System.Management.Automation.Language.FunctionDefinitionAst]) -and ($n.Name -eq $Name) }, $true)
    if (-not $hit -or $hit.Count -lt 1) { throw "function $Name not found in $Path" }
    return $hit[0].Extent.Text
}
switch ($Mode) {
    'reader' {
        # Add-LineReader must compile (Add-Type) under this PowerShell and work.
        Invoke-Expression (Get-FnText $Src 'Add-LineReader')
        Add-LineReader
        if (-not ('HttLfReader' -as [type])) { throw 'HttLfReader did not compile' }
        $text = ('y' * 4096) + '@@htt {"kind":"done"}' + "`n" + '@@htt {"kind":"warning"}' + "`r`n" + 'one' + "`r" + 'two' + "`nlast"
        $rd = New-Object HttLfReader((New-Object IO.StringReader($text)), 100)
        $rows = @()
        while ($true) { $l = $rd.ReadLineAsync().Result; if ($null -eq $l) { break }; $rows += ('{0}|{1}|{2}' -f $l.Length, $l.Substring(0, [Math]::Min(12, $l.Length)).Replace("`r", '<CR>'), $rd.Untrusted) }
        'ps=' + $PSVersionTable.PSVersion.Major
        $rows
        # the default limit still works for an ordinary line
        $rd = New-Object HttLfReader((New-Object IO.StringReader("a`nb")))
        'plain=' + $rd.ReadLineAsync().Result + $rd.Untrusted + $rd.ReadLineAsync().Result + $rd.Untrusted
    }
    'common' {
        . $Src
        Remove-TreeNoFollow $Work
        'removed'
    }
    'owntree' {
        Invoke-Expression (Get-FnText $Src 'Remove-OwnTree')
        Remove-OwnTree $Work
        'removed'
    }
    'delayed-text' {
        Invoke-Expression (Get-FnText $Src 'Get-DelayedCleanupScript')
        [IO.File]::WriteAllText($Work, (Get-DelayedCleanupScript), (New-Object Text.UTF8Encoding($false)))
        'written'
    }
    'share' {
        # A copy of a small Windows program, held open exactly as the Python path holds
        # the installer (FileAccess.Read, FileShare.Read), is started with Process.Start.
        $copy = Join-Path $Work 'where-copy.exe'
        Copy-Item -LiteralPath (Join-Path $env:SystemRoot 'System32\where.exe') -Destination $copy
        $lock = New-Object IO.FileStream($copy, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            $before = 'opened'
            try { $w = New-Object IO.FileStream($copy, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $w.Dispose() } catch { $before = 'denied' }
            $moved = 'moved'
            try { [IO.File]::Move($copy, $copy + '.moved') } catch { $moved = 'refused' }
            $psi = New-Object Diagnostics.ProcessStartInfo
            $psi.FileName = $copy
            $psi.Arguments = 'cmd.exe'
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $p = [Diagnostics.Process]::Start($psi)
            $out = $p.StandardOutput.ReadToEnd()
            [void]$p.WaitForExit(30000)
            $during = 'opened'
            try { $w = New-Object IO.FileStream($copy, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $w.Dispose() } catch { $during = 'denied' }
            'write-before-start=' + $before
            'move-while-held=' + $moved
            'exit=' + $p.ExitCode
            'ran=' + ($out -match 'cmd\.exe')
            'write-while-held=' + $during
        } finally { $lock.Dispose() }
        $after = 'opened'
        try { $w = New-Object IO.FileStream($copy, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite); $w.Dispose() } catch { $after = 'denied' }
        'write-after-release=' + $after
    }
    default { throw "unknown mode $Mode" }
}
"""


def run_driver(work_root, mode, src="", fn="", work="", env=None, timeout=240):
    script = Path(work_root) / "driver.ps1"
    script.write_text(DRIVER, encoding="ascii")
    out = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-Mode", mode, "-Src", str(src), "-Fn", fn, "-Work", str(work)],
        capture_output=True, timeout=timeout, env=env or os.environ.copy(),
        creationflags=NO_WINDOW)
    return (out.returncode, out.stdout.decode("utf-8", "replace"),
            out.stderr.decode("utf-8", "replace"))


def make_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    return r.returncode == 0


def short_path(path):
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if 0 < n < 1024 else str(path)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="htt-hd-")).resolve()
        self.addCleanup(self.forget)
        self.outside = self.tmp / "outside"
        (self.outside / "sub").mkdir(parents=True)
        (self.outside / "keep.txt").write_text("precious")
        (self.outside / "sub" / "keep2.txt").write_text("precious too")

    def forget(self):
        # links first (removed as links), read-only flags off, then the folder
        def is_link(p):
            try:
                return bool(os.lstat(p).st_file_attributes & 0x400)
            except (OSError, AttributeError):
                return False
        for p in sorted(self.tmp.rglob("*"), key=lambda q: -len(str(q))):
            if is_link(p):
                try:
                    os.rmdir(p)
                except OSError:
                    pass
        subprocess.run(["cmd", "/c", "attrib", "-R", str(self.tmp / "*"), "/S", "/D"],
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_outside_intact(self):
        self.assertEqual((self.outside / "keep.txt").read_text(), "precious")
        self.assertEqual((self.outside / "sub" / "keep2.txt").read_text(), "precious too")

    def junction(self, link):
        link = Path(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if not make_junction(link, self.outside):
            self.skipTest("mklink /J is not available")
        return link

    def read_only_tree(self, root):
        """root/ro-dir (read-only folder) holding a read-only file and a sub folder."""
        (root / "ro-dir" / "inner").mkdir(parents=True)
        f = root / "ro-dir" / "inner" / "f.txt"
        f.write_text("x")
        (root / "plain.txt").write_text("x")
        subprocess.run(["attrib", "+R", str(f)], capture_output=True, timeout=60,
                       creationflags=NO_WINDOW, check=True)
        subprocess.run(["attrib", "+R", str(root / "ro-dir" / "inner")], capture_output=True,
                       timeout=60, creationflags=NO_WINDOW, check=True)
        subprocess.run(["attrib", "+R", str(root / "ro-dir")], capture_output=True,
                       timeout=60, creationflags=NO_WINDOW, check=True)
        self.assertTrue(os.stat(root / "ro-dir").st_file_attributes & 1, "folder is not read-only")


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class LineReader(unittest.TestCase):
    """HttLfReader compiles under Windows PowerShell 5.1 and marks a cut line and the
    line after it as untrusted. A failing Add-Type fails these tests."""

    def check(self, gui):
        tmp = Path(tempfile.mkdtemp(prefix="htt-hd-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        code, out, err = run_driver(tmp, "reader", src=gui)
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "ps=5", out)
        rows = [l for l in lines if re.match(r"^\d+\|", l)]
        # the piece cut at the limit, the tail of the same long line, then ordinary lines
        self.assertEqual(len(rows), 5, out)
        self.assertTrue(rows[0].startswith("4096|yyyyyyyyyyyy|True"), rows)
        self.assertTrue(rows[1].endswith("|True"), rows)
        self.assertIn("@@htt {\"kind", rows[1], "the line after a cut piece is the forged marker")
        for r in rows[2:]:
            self.assertTrue(r.endswith("|False"), rows)
        self.assertIn("one<CR>two", rows[3], "a lone carriage return does not end a line")
        self.assertIn("plain=aFalsebFalse", out)

    def test_install_window_reader(self):
        self.check(INSTALL_GUI)

    def test_uninstall_window_reader(self):
        self.check(UNINSTALL_GUI)

    def test_both_windows_use_the_flag_when_they_read(self):
        for gui in (INSTALL_GUI, UNINSTALL_GUI):
            text = gui.read_text(encoding="ascii")
            self.assertIn("-not $r.Readers[$i].Untrusted", text, gui.name)
            self.assertIn("public bool Untrusted", text, gui.name)


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class NoFollowRemoval(Sandbox):
    def test_a_junction_inside_a_tree_is_removed_as_a_link(self):
        tree = self.tmp / "tree"
        (tree / "a" / "b").mkdir(parents=True)
        (tree / "a" / "b" / "f.txt").write_text("x")
        link = self.junction(tree / "a" / "link")
        self.assertTrue((link / "keep.txt").exists())
        code, out, err = run_driver(self.tmp, "common", src=COMMON, work=tree)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(tree.exists())
        self.assert_outside_intact()

    def test_directory_delete_of_a_junction_does_not_touch_its_target(self):
        link = self.junction(self.tmp / "link")
        code, out, err = run_driver(self.tmp, "common", src=COMMON, work=link)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(link))
        self.assert_outside_intact()
        # the plain .NET call the scripts rely on, without our function
        link = self.junction(self.tmp / "link2")
        os.rmdir(link)            # Python's rmdir on a junction: removes the link only
        self.assert_outside_intact()

    def test_read_only_folders_and_files_do_not_stop_the_removal(self):
        tree = self.tmp / "tree"
        self.read_only_tree(tree)
        code, out, err = run_driver(self.tmp, "common", src=COMMON, work=tree)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(tree.exists(), "a read-only folder was left behind")

    def test_the_window_s_own_removal_is_the_same_walk(self):
        tree = self.tmp / "tree"
        self.read_only_tree(tree)
        link = self.junction(tree / "plainlink")
        self.assertTrue((link / "keep.txt").exists())
        code, out, err = run_driver(self.tmp, "owntree", src=UNINSTALL_GUI, work=tree)
        self.assertEqual(code, 0, out + err)
        self.assertFalse(tree.exists())
        self.assert_outside_intact()


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class DelayedCleanupScripts(Sandbox):
    """The two scripts a hidden PowerShell runs after the program has ended: the one the
    Setup.exe writes for its own temp folder and the one uninstall-gui.ps1 starts. Each
    gets the test's folder as its temp folder, so only a folder there can be removed."""

    def setUp(self):
        super().setUp()
        self.temp = self.tmp / "t"
        self.temp.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def script_for(self, kind):
        path = self.runs / (kind + "-" + uuid.uuid4().hex[:6] + ".ps1")
        if kind == "setup":
            path.write_text(build_setup.CLEANUP_SCRIPT, encoding="ascii")
        else:
            code, out, err = run_driver(self.runs, "delayed-text", src=UNINSTALL_GUI, work=path)
            self.assertEqual(code, 0, out + err)
        return path

    def run_script(self, kind, target, temp=None):
        script = self.script_for(kind)
        env = os.environ.copy()
        for k in [k for k in env if k.startswith("HTT_")]:
            del env[k]
        t = str(temp or self.temp)
        env["TMP"] = env["TEMP"] = t
        env["HTT_CLEAN_TARGET"] = str(target)
        r = subprocess.run([str(POWERSHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(script)], env=env, capture_output=True, timeout=180,
                           creationflags=NO_WINDOW)
        return r.returncode, r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")

    def name(self, kind):
        return ("is-" if kind == "setup" else "htt-uninstall-") + (
            uuid.uuid4().hex[:8] if kind == "setup" else str(uuid.uuid4().int)[:8])

    def new_target(self, kind):
        target = self.temp / (self.name(kind) + (".tmp" if kind == "setup" else ""))
        (target / "app").mkdir(parents=True)
        (target / "app" / "x.txt").write_text("x")
        return target

    def test_a_junction_inside_is_removed_as_a_link_and_its_files_survive(self):
        for kind in ("setup", "uninstall"):
            with self.subTest(script=kind):
                target = self.new_target(kind)
                link = self.junction(target / "app" / "link")
                self.assertTrue((link / "sub" / "keep2.txt").exists())
                rc, out = self.run_script(kind, target)
                self.assertEqual(rc, 0, out)
                self.assertFalse(target.exists(), "the folder was not removed")
                self.assert_outside_intact()

    def test_a_target_that_is_itself_a_junction_is_refused(self):
        for kind in ("setup", "uninstall"):
            with self.subTest(script=kind):
                link = self.temp / (self.name(kind) + (".tmp" if kind == "setup" else ""))
                self.junction(link)
                rc, out = self.run_script(kind, link)
                self.assertEqual(rc, 1, out)
                self.assertTrue(os.path.lexists(link), "the link itself was removed")
                self.assert_outside_intact()
                os.rmdir(link)

    def test_read_only_folders_inside_do_not_stop_it(self):
        for kind in ("setup", "uninstall"):
            with self.subTest(script=kind):
                target = self.new_target(kind)
                self.read_only_tree(target)
                rc, out = self.run_script(kind, target)
                self.assertEqual(rc, 0, out)
                self.assertFalse(target.exists())

    def test_other_names_and_places_are_refused(self):
        for kind in ("setup", "uninstall"):
            with self.subTest(script=kind):
                wrong = [self.temp / "htt-other-1", self.temp / "sub" / self.name(kind)]
                if kind == "setup":
                    wrong.append(self.temp / (self.name(kind) + ".tmp.bak"))
                else:
                    wrong.append(self.temp / "htt-uninstall-abc")
                for victim in wrong:
                    victim.mkdir(parents=True, exist_ok=True)
                    rc, out = self.run_script(kind, victim)
                    self.assertEqual(rc, 1, f"{victim.name}: {out}")
                    self.assertTrue(victim.exists())

    def test_the_target_in_8_3_short_form_still_matches(self):
        # Inno's {tmp} may be a short name (LONGNA~1); the parent and the temp folder are
        # compared as long names. Needs a volume with short names.
        long_temp = self.tmp / "a-really-long-temporary-folder-name"
        long_temp.mkdir()
        short = short_path(long_temp)
        if short.lower() == str(long_temp).lower():
            self.skipTest("this volume has no 8.3 short names")
        for kind in ("setup", "uninstall"):
            with self.subTest(script=kind):
                leaf = self.name(kind) + (".tmp" if kind == "setup" else "")
                (long_temp / leaf / "app").mkdir(parents=True)
                (long_temp / leaf / "app" / "x.txt").write_text("x")
                # target short, temp folder long
                rc, out = self.run_script(kind, Path(short) / leaf, temp=long_temp)
                self.assertEqual(rc, 0, out)
                self.assertFalse((long_temp / leaf).exists())
                # target long, temp folder short
                (long_temp / leaf / "app").mkdir(parents=True)
                rc, out = self.run_script(kind, long_temp / leaf, temp=short)
                self.assertEqual(rc, 0, out)
                self.assertFalse((long_temp / leaf).exists())

    def test_the_scripts_never_use_remove_item_recurse_or_the_recursive_delete(self):
        text = build_setup.CLEANUP_SCRIPT
        self.assertNotRegex(text, r"Directory\]::Delete\(\$t,\$true\)|-Recurse")
        self.assertIn("1024", text)
        func = re.search(r"function Get-DelayedCleanupScript.*?\n}\n", UNINSTALL_GUI.read_text(
            encoding="ascii"), re.S)
        self.assertIsNotNone(func)
        self.assertNotRegex(func.group(0), r"Delete\(\$t, \$true\)|-Recurse")
        self.assertIn("1024", func.group(0))


@unittest.skipUnless(HAVE_PS, "needs Windows PowerShell")
class HeldFileStillRuns(unittest.TestCase):
    def test_a_program_held_open_with_fileshare_read_can_be_started_but_not_written(self):
        tmp = Path(tempfile.mkdtemp(prefix="htt-hd-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        code, out, err = run_driver(tmp, "share", work=tmp)
        self.assertEqual(code, 0, out + err)
        facts = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        self.assertEqual(facts.get("write-before-start"), "denied", out)
        self.assertEqual(facts.get("move-while-held"), "refused", out)
        self.assertEqual(facts.get("exit"), "0", out)
        self.assertEqual(facts.get("ran"), "True", out)
        self.assertEqual(facts.get("write-while-held"), "denied", out)
        self.assertEqual(facts.get("write-after-release"), "opened", out)

    def test_the_python_path_holds_the_file_that_way(self):
        text = INSTALL_GUI.read_text(encoding="ascii")
        self.assertGreaterEqual(text.count("[IO.FileAccess]::Read, [IO.FileShare]::Read)"), 2)
        self.assertIn("only now, with the process started, may the file be released", text)


class BuildScript(unittest.TestCase):
    USERS = "C:" + "\\Users"

    def test_only_exactly_you_is_the_placeholder(self):
        u = self.USERS.encode("ascii")
        for ok in (b"\\you\\x", b"\\you", b'\\you"', b"\\you'", b"\\you ", b"\\you\n", b"/you/x",
                   b"\\you\t"):
            self.assertEqual(build_setup.scan_bytes(u + ok, []), [], ok)
        for bad in (b"\\you.x", b"\\youth", b"\\you_1", b"\\you-x", b"\\you1", b"\\your",
                    b"\\you,x", b"\\You.Smith"):
            self.assertTrue(build_setup.scan_bytes(u + bad, []), bad)

    def test_git_gets_literal_pathspecs(self):
        text = (ROOT / "tools" / "build_setup.py").read_text(encoding="utf-8")
        self.assertEqual(text.count('"--literal-pathspecs"'), 2)

    def test_a_pathspec_with_a_glob_character_is_not_expanded(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available")
        repo = Path(tempfile.mkdtemp(prefix="htt-hd-"))

        def forget(path):
            for p in path.rglob("*"):
                try:
                    os.chmod(p, 0o666)
                except OSError:
                    pass
            shutil.rmtree(path, ignore_errors=True)
        self.addCleanup(forget, repo)

        def g(*a):
            subprocess.run([git, "-C", str(repo), "-c", "user.name=t",
                            "-c", "user.email=t" + "@example.invalid", *a],
                           check=True, capture_output=True, timeout=60)
        g("init", "-q")
        (repo / "a1.txt").write_text("a")
        g("add", ".")
        g("commit", "-q", "-m", "x")
        old = build_setup.ROOT
        build_setup.ROOT = repo
        try:
            # "a[1].txt" would match the tracked a1.txt as a glob; literally it is not tracked
            lines = build_setup.git_dirty(["a[1].txt"])
        finally:
            build_setup.ROOT = old
        self.assertTrue(any("not tracked" in l or l.startswith("??") for l in lines) or lines == [],
                        lines)
        self.assertNotEqual(lines, ["(git status failed: )"])
        self.assertTrue(any("a[1].txt" in l for l in lines), lines)


if __name__ == "__main__":
    unittest.main()
