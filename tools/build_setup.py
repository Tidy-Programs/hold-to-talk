"""Builds HoldToTalk-Setup-<version>.exe (Inno Setup carrier) and SHA256SUMS.txt.

    python -B tools\\build_setup.py --iscc C:\\path\\to\\ISCC.exe [--out dist]
                                    [--allow-dirty]

The compiler is taken from --iscc or the environment variable HTT_ISCC. Only
the files named in install-files.txt go in (never a wildcard over the project
folder), copied into a fresh staging folder together with files.sha256 (the
hash list install.ps1 checks). Standard library only.

The staging folder is made under a neutral path (C:\\htt-build-<random>, or the
temp folder if that cannot be created) and deleted afterwards. Every staged file
gets one fixed modification time (so the build machine's clock and time zone are
not recorded). BEFORE compiling, the plain staged files (and the generated .inc
files and the .iss) are searched for the Windows account name, the profile
folder's name, the computer name, the domain name and C:\\Users\\<anything but
"you"> (as UTF-8, as the ANSI code page and as UTF-16LE, ignoring case): any hit
fails the build. The compiled exe is LZMA-compressed, so searching it proves
little; it is searched afterwards only as a "loader only" second check.
"""

import argparse
import hashlib
import locale
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "holdtotalk.iss"
FORMAT_LINE = "# htt-sha256 1"
# Every staged file gets this modification time (2026-01-01 00:00:00 UTC).
FIXED_MTIME = 1767225600

# The hidden PowerShell child that removes Inno's temporary folder after the
# Setup.exe has exited. The Setup.exe writes this script to a file of its own in
# the temp folder and starts it with -File (never as -Command text, never through
# cmd). The folder to remove arrives in the environment variable HTT_CLEAN_TARGET,
# so no character of the path can be read as syntax. The script removes it only
# if it is a folder named is-<letters and digits>.tmp directly in the temp folder,
# tries for up to 30 seconds, and deletes itself at the end.
# Links are never followed: a target that is itself a reparse point (attribute
# 1024) is refused, and the tree is removed by a walk that deletes a junction or
# symbolic link inside it as the link (HttDel below, the same walk as
# Remove-TreeNoFollow in htt-common.ps1, inlined because this script cannot
# dot-source it), clearing ReadOnly on the way. Inno may give {tmp} in its 8.3
# short form (a name such as LONGNA~1 for a long folder name), so the parent
# folder and the temp folder are both turned into long names
# (GetLongPathNameW) before they are compared.
CLEANUP_SCRIPT = (
    "$self=$MyInvocation.MyCommand.Path;"
    "$t=$env:HTT_CLEAN_TARGET;"
    "Add-Type -Namespace H -Name L -MemberDefinition '"
    "[DllImport(\"kernel32.dll\",CharSet=CharSet.Unicode,EntryPoint=\"GetLongPathNameW\")]"
    "public static extern int G(string a,System.Text.StringBuilder b,int c);';"
    "function HttLong($x){$b=New-Object Text.StringBuilder 1024;"
    "if([H.L]::G($x,$b,1024) -gt 0){$b.ToString()}else{$x}};"
    "function HttDel($p){$a=[int][IO.File]::GetAttributes($p);"
    "if(-not($a -band 16)){[IO.File]::SetAttributes($p,[IO.FileAttributes]::Normal);[IO.File]::Delete($p);return};"
    "if(-not($a -band 1024)){[IO.File]::SetAttributes($p,[IO.FileAttributes]::Directory);"
    "foreach($e in @([IO.Directory]::GetFileSystemEntries($p))){HttDel $e}}"
    "else{try{[IO.File]::SetAttributes($p,[IO.FileAttributes]::Directory)}catch{}};"
    "[IO.Directory]::Delete($p,$false)};"
    "$ok=$false;"
    "if($t){"
    "$t=HttLong $t;"
    "$tmp=(HttLong ([IO.Path]::GetTempPath())).TrimEnd([char]92);"
    "$par=(HttLong (Split-Path -Parent $t)).TrimEnd([char]92);"
    "$leaf=Split-Path -Leaf $t;"
    "if(($par -ieq $tmp) -and ($leaf -match '^is-[A-Za-z0-9]+[.]tmp$')){$ok=$true}};"
    "$rc=1;"
    "if($ok){"
    "Start-Sleep -Seconds 3;"
    "$end=(Get-Date).AddSeconds(30);$rc=2;"
    "while((Get-Date) -lt $end){"
    "if(-not(Test-Path -LiteralPath $t)){$rc=0;break};"
    "$lk=$false;try{$lk=[bool]([int][IO.File]::GetAttributes($t) -band 1024)}catch{};"
    "if($lk){$rc=1;break};"
    "try{HttDel $t}catch{};"
    "Start-Sleep -Seconds 1}};"
    "try{Remove-Item -LiteralPath $self -Force}catch{};"
    "exit $rc")

# Words that are no use as a search needle (they would hit ordinary text).
GENERIC_NEEDLES = {
    "user", "users", "admin", "administrator", "pc", "owner", "home", "default",
    "public", "guest", "desktop", "laptop", "computer", "localhost", "windows",
    "system", "workgroup", "you", "root", "test", "temp", "local", "main", "app",
}


def manifest_entry_problem(entry):
    """Why a line of install-files.txt is not allowed, or None. Refused: a rooted
    path (leading slash or backslash), a drive letter or any colon, "..", a quote
    character and braces (Inno would read them as constants). install.ps1
    (Test-ManifestEntry) applies the same rules."""
    if entry[:1] in ("/", "\\"):
        return "it starts with a slash or backslash"
    if re.match(r"^[A-Za-z]:", entry):
        return "it starts with a drive letter"
    if ":" in entry:
        return "it contains a colon"
    if ".." in entry:
        return 'it contains ".."'
    if '"' in entry or "'" in entry:
        return "it contains a quote character"
    if "{" in entry or "}" in entry:
        return "it contains a brace"
    return None


def read_manifest(root=ROOT):
    files = []
    for line in (root / "install-files.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            bad = manifest_entry_problem(line)
            if bad:
                raise SystemExit(f"build_setup: install-files.txt has an entry that "
                                 f"is not allowed ({line}): {bad}")
            files.append(line.replace("\\", "/"))
    return files


def read_setting(name, root=ROOT):
    text = (root / "settings.py").read_text(encoding="utf-8")
    m = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit(f"build_setup: {name} not found in settings.py")
    return m.group(1)


def numeric_version(version):
    nums = re.findall(r"\d+", version)[:4]
    if not nums:
        raise SystemExit(f"build_setup: cannot make a number of version {version!r}")
    return ".".join(nums + ["0"] * (4 - len(nums)))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def git_dirty(files):
    """Problems of the listed files with git, as lines (empty list: all clean):
    uncommitted changes, untracked files ("??"), files that git ignores ("!!",
    which main() refuses even with --allow-dirty) and listed files that git does
    not track."""
    try:
        out = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all",
             "--ignored", "--"] + files,
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return [f"(git status failed: {out.stderr.strip()})"]
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        tracked = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(ROOT), "ls-files", "--"] + files,
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return [f"(git could not be run: {e})"]
    if tracked.returncode != 0:
        return [f"(git ls-files failed: {tracked.stderr.strip()})"]
    known = {l.strip() for l in tracked.stdout.splitlines()}
    have = {l[3:].strip().strip('"') for l in lines}
    for f in files:
        if f not in known and f not in have:
            lines.append(f"-- {f} (not tracked by git)")
    return lines


def make_staging():
    name = "htt-build-" + secrets.token_hex(4)
    for base in (Path(os.environ.get("SystemDrive", "C:") + "\\"),
                 Path(tempfile.gettempdir())):
        path = base / name
        try:
            path.mkdir()
            return path
        except OSError:
            continue
    raise SystemExit("build_setup: cannot create a staging folder")


def mangle(rel):
    """The flat name a file has inside the exe (ExtractTemporaryFile is flat)."""
    return rel.replace("/", "__")


def write_staging(stage, files):
    names = {}
    for rel in files:
        src = ROOT / rel
        if not src.is_file():
            raise SystemExit(f"build_setup: {rel} is listed in install-files.txt "
                             "but does not exist")
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        flat = mangle(rel)
        if flat.lower() in names:
            raise SystemExit(f"build_setup: {rel} and {names[flat.lower()]} "
                             "would have the same flat name")
        names[flat.lower()] = rel
    lines = [FORMAT_LINE] + [f"{sha256_of(stage / rel)}  {rel}" for rel in files]
    (stage / "files.sha256").write_bytes(("\n".join(lines) + "\n").encode("ascii"))
    # The [Files] lines and the Pascal procedure that puts each one in place.
    all_files = list(files) + ["files.sha256"]
    inc = []
    place = ["procedure PlaceFiles();", "var", "  Dir: String;", "begin",
             "  Dir := ExpandConstant('{tmp}\\app');",
             "  ForceDirectories(Dir);"]
    for rel in all_files:
        flat = mangle(rel)
        inc.append(f'Source: "{rel.replace("/", chr(92))}"; DestDir: "{{tmp}}"; '
                   f'DestName: "{flat}"; Flags: dontcopy')
        target = rel.replace("/", "\\")
        place.append(f"  ExtractTemporaryFile('{flat}');")
        place.append(f"  ForceDirectories(ExtractFilePath(Dir + '\\{target}'));")
        place.append(f"  RenameFile(ExpandConstant('{{tmp}}\\{flat}'), Dir + '\\{target}');")
    place.append("end;")
    (stage / "files.inc").write_bytes(("\n".join(inc) + "\n").encode("ascii"))
    (stage / "place.inc").write_bytes(("\n".join(place) + "\n").encode("ascii"))
    pascal = CLEANUP_SCRIPT.replace("'", "''")
    (stage / "cleanup.inc").write_bytes(
        (f"const\n  CleanupScript = '{pascal}';\n").encode("ascii"))
    fix_times(stage)


def fix_times(stage):
    """One fixed modification time for every staged file and folder."""
    paths = [p for p in stage.rglob("*")]
    for p in paths:
        os.utime(p, (FIXED_MTIME, FIXED_MTIME))
    os.utime(stage, (FIXED_MTIME, FIXED_MTIME))


def find_bytes(data, needle):
    """Count of `needle` in `data` as ASCII and as UTF-16LE (case-insensitive)."""
    low = data.lower()
    n = needle.lower()
    return (low.count(n.encode("ascii", "ignore")),
            low.count(n.encode("utf-16le")))


def private_needles(env=None):
    """([(label, value)], [skipped lines]): the values of this machine that must
    not appear in anything built. A needle shorter than 3 characters or equal to
    a generic word is skipped (and reported); an empty needle never counts."""
    env = os.environ if env is None else env
    raw = [("USERNAME", env.get("USERNAME", "")),
           ("USERPROFILE folder name", os.path.basename(
               (env.get("USERPROFILE", "") or "").rstrip("\\/"))),
           ("COMPUTERNAME", env.get("COMPUTERNAME", "")),
           ("USERDOMAIN", env.get("USERDOMAIN", ""))]
    use, skipped, seen = [], [], set()
    for label, value in raw:
        value = (value or "").strip()
        if not value:
            skipped.append(f"{label}: empty, skipped")
        elif len(value) < 3:
            skipped.append(f"{label}: shorter than 3 characters, skipped")
        elif value.lower() in GENERIC_NEEDLES:
            skipped.append(f"{label}: a generic word, skipped")
        elif value.lower() in seen:
            skipped.append(f"{label}: same as an earlier needle, skipped")
        else:
            seen.add(value.lower())
            use.append((label, value))
    return use, skipped


def _texts(data):
    """The bytes read three ways: UTF-8, the ANSI code page, UTF-16LE (both
    byte alignments), each lower-cased."""
    ansi = locale.getpreferredencoding(False) or "cp1252"
    out = [data.decode("utf-8", "replace"), data.decode(ansi, "replace"),
           data.decode("utf-16-le", "replace"), data[1:].decode("utf-16-le", "replace")]
    return [t.lower() for t in out]


# "you" is the placeholder only when it is the whole folder name: followed by a path
# separator, a quote, white space or the end (not "you.x", "youth", "you_1").
USERS_RE = re.compile(r"[a-z]:[\\/]+users[\\/]+(?![\\/])(?!you(?:[\\/\"'\s]|$))")


def scan_bytes(data, needles):
    """Hits in one blob: a list of labels (one per needle that appears, and
    "C:\\Users\\<name>" for a user folder other than the placeholder 'you')."""
    hits = []
    texts = _texts(data)
    for label, value in needles:
        if not value:
            continue
        n = value.lower()
        if any(n in t for t in texts):
            hits.append(label)
    if any(USERS_RE.search(t) for t in texts):
        hits.append("C:\\Users\\ followed by something other than 'you'")
    return hits


def scan_files(paths, needles):
    """{path: [labels]} for the files that hold a private value."""
    found = {}
    for path in paths:
        hits = scan_bytes(Path(path).read_bytes(), needles)
        if hits:
            found[str(path)] = hits
    return found


def staging_scan(stage, extra_files=()):
    """Searches every staged file (the app, files.sha256, the generated .inc
    files) and the .iss. Returns (problem lines, report lines)."""
    needles, skipped = private_needles()
    paths = sorted(p for p in Path(stage).rglob("*") if p.is_file())
    paths += [Path(f) for f in extra_files]
    found = scan_files(paths, needles)
    report = [f"staging scan: {len(paths)} files, searched for "
              + ", ".join(label for label, _ in needles)
              + " and C:\\Users\\<not 'you'>, as UTF-8, ANSI and UTF-16LE, ignoring case"]
    report += [f"staging scan: {s}" for s in skipped]
    problems = []
    for path, hits in found.items():
        name = path
        try:
            name = str(Path(path).relative_to(stage))
        except ValueError:
            name = Path(path).name
        problems.append(f"{name}: {', '.join(hits)}")
    return problems, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--iscc", help="path to ISCC.exe (or set HTT_ISCC)")
    ap.add_argument("--out", default=str(ROOT / "dist"),
                    help="output folder (default: dist next to this project)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="build although files of install-files.txt have "
                         "uncommitted changes")
    args = ap.parse_args(argv)

    iscc = args.iscc or os.environ.get("HTT_ISCC")
    if not iscc or not Path(iscc).is_file():
        raise SystemExit("build_setup: ISCC.exe (Inno Setup compiler) not found. "
                         "Pass --iscc <path> or set HTT_ISCC.")

    files = read_manifest()
    name = read_setting("APP_NAME")
    version = read_setting("APP_VERSION")
    if not re.fullmatch(r"[A-Za-z0-9 ._-]+", name + version):
        raise SystemExit("build_setup: APP_NAME / APP_VERSION hold odd characters")

    dirty = git_dirty(files + ["install-files.txt"])
    ignored = [l for l in dirty if l.startswith("!!")]
    if ignored:
        sys.stderr.write("build_setup: refusing to build; git ignores these listed "
                         "files (--allow-dirty does not change that):\n  "
                         + "\n  ".join(ignored) + "\n")
        return 1
    if dirty:
        if not args.allow_dirty:
            sys.stderr.write("build_setup: refusing to build; these files of the app "
                             "have uncommitted changes:\n  " + "\n  ".join(dirty) +
                             "\nCommit them, or pass --allow-dirty.\n")
            return 1
        print("!" * 70)
        print("WARNING: --allow-dirty: building from UNCOMMITTED changes:")
        for l in dirty:
            print("   " + l)
        print("This exe is not a build of a committed version. Do not publish it.")
        print("!" * 70)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    base = f"HoldToTalk-Setup-{version}"
    exe = out / (base + ".exe")
    if exe.exists():
        exe.unlink()

    stage = make_staging()
    try:
        write_staging(stage, files)
        problems, report = staging_scan(stage, [ISS])
        for line in report:
            print(line)
        if problems:
            sys.stderr.write("build_setup: private values found in the staged files, "
                             "nothing was compiled:\n  " + "\n  ".join(problems) + "\n")
            return 1
        print("staging scan: nothing found")
        cmd = [str(iscc), "/Q",
               f"/DAppName={name}", f"/DAppVersion={version}",
               f"/DAppVersionNumeric={numeric_version(version)}",
               f"/DStageDir={stage}", f"/DOutDir={out}", f"/DOutName={base}",
               str(ISS)]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if run.returncode != 0 or not exe.is_file():
            sys.stderr.write(run.stdout + run.stderr)
            sys.stderr.write("build_setup: the Inno Setup compiler failed\n")
            return 1
        # Second, weaker check: the compiled exe is LZMA-compressed, so this only
        # covers the loader (the uncompressed part), not the files inside.
        data = exe.read_bytes()
        needles, _ = private_needles()
        loader = scan_bytes(data, needles)
        extra = [n for n in (str(stage),) if n and find_bytes(data, n) != (0, 0)]
        if loader or extra:
            exe.unlink()
            sys.stderr.write("build_setup: the exe's loader contains private values, "
                             "deleted: " + ", ".join(loader + ["the staging path"] * len(extra)) + "\n")
            return 1
        print("exe check (loader only; the files inside are compressed and are covered "
              "by the staging scan above): nothing found")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    digest = sha256_of(exe)
    (out / "SHA256SUMS.txt").write_bytes(
        f"{digest}  {exe.name}\n".encode("ascii"))
    print(f"Built {exe}")
    print(f"  size   {exe.stat().st_size:,} bytes")
    print(f"  sha256 {digest}")
    print(f"  {out / 'SHA256SUMS.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
