"""Refresh the release lock files (requirements.lock.txt and
requirements-cuda.lock.txt) from a working environment.

    <the app's python> tools\\make_locks.py            # write both lock files
    <the app's python> tools\\make_locks.py --check    # only say if they are stale

requirements.txt / requirements-cuda.txt are the developer-facing ranges.
The lock files pin EXACT versions of everything those ranges pull in (the
transitive packages too: ctranslate2, huggingface_hub, tokenizers, av,
onnxruntime, ...), each with the sha256 of every Windows 64-bit wheel for
Python 3.10 - 3.13 that PyPI lists for that version. The installer installs
from the locks with `--only-binary :all:` (nothing is built from source) and
pip then refuses any file whose hash is not listed.

How the numbers are found
- the versions: the packages installed in THIS environment, following the
  requirements' dependency tree (so it must be an environment where the app
  works; run it with the app's own python);
- the hashes: PyPI's JSON listing of each pinned version (a few kilobytes
  per package: no wheel is downloaded, so the NVIDIA wheels are not either).
  Only the package names and versions go to pypi.org.

To refresh: update the environment (pip install -U -r requirements.txt
[-r requirements-cuda.txt]), test the app, then run this. A test fails while
a lock file and its range file disagree (the lock records a digest of the
range file it was made from).
"""

import hashlib
import json
import re
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCKS = (("requirements.txt", "requirements.lock.txt"),
         ("requirements-cuda.txt", "requirements-cuda.lock.txt"))
PYTHONS = (10, 11, 12, 13)          # the Python 3.x the installer accepts
DIGEST_PREFIX = "# ranges-sha256: "


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def range_lines(path):
    """The requirement lines of a range file, comments and blanks removed."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def range_digest(path):
    """sha256 of the requirements in a range file (order, comments and
    spacing do not matter)."""
    text = "\n".join(sorted(re.sub(r"\s+", "", l) for l in range_lines(path)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_lock(path):
    """{canonical name: (version, [hashes])} and the recorded digest."""
    pins, digest, current = {}, None, None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith(DIGEST_PREFIX):
            digest = line[len(DIGEST_PREFIX):].strip()
            continue
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)", line)
        if m:
            current = canon(m.group(1))
            pins[current] = (m.group(2), [])
            line = line[m.end():]
        for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if current:
                pins[current][1].append(h)
    return pins, digest


def wheel_ok(filename):
    """Is this wheel installable on 64-bit Windows with Python 3.10-3.13?"""
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    py, abi, plat = parts[-3].split("."), parts[-2].split("."), parts[-1].split(".")
    if not ({"any", "win_amd64"} & set(plat)):
        return False
    for p in py:
        for a in abi:
            if a == "none" and re.fullmatch(r"py3\d*|py2|cp3", p):
                m = re.fullmatch(r"py3(\d+)", p)
                if not m or int(m.group(1)) <= PYTHONS[-1]:
                    return True
            m = re.fullmatch(r"cp3(\d+)", p)
            if not m:
                continue
            n = int(m.group(1))
            if a == "abi3" and n <= PYTHONS[-1]:
                return True
            if a == f"cp3{n}" and n in PYTHONS:
                return True
    return False


def closure(root_names):
    """{canonical name: version} of the packages installed in this
    environment that the given requirements need, transitively."""
    from packaging.requirements import Requirement
    dists = {canon(d.metadata["Name"]): d for d in metadata.distributions()}
    seen, todo = {}, [canon(Requirement(r).name) for r in root_names]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        dist = dists.get(name)
        if dist is None:
            raise SystemExit(f"{name} is not installed in this environment")
        seen[name] = dist.version
        for r in dist.requires or []:
            req = Requirement(r)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            todo.append(canon(req.name))
    return seen


def hashes_for(name, version):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as r:
        files = json.load(r)["urls"]
    found = sorted({f["digests"]["sha256"] for f in files
                    if f["packagetype"] == "bdist_wheel"
                    and wheel_ok(f["filename"])})
    if not found:
        raise SystemExit(f"{name} {version}: no 64-bit Windows wheel on PyPI "
                         "(the installer never builds from source)")
    return found


def make(range_file, lock_file, with_hashes=True):
    pins = closure(range_lines(ROOT / range_file))
    pins.pop("pip", None)
    lines = [
        f"# {lock_file}: every package, at an exact version, that "
        f"{range_file} needs.",
        "# Generated by tools/make_locks.py from a working environment: do "
        "not edit by hand",
        "# (see the README: Refreshing the lock files). Installed with "
        "pip --only-binary :all:;",
        "# each package lists the sha256 of its Windows 64-bit wheels for "
        "Python 3.10 - 3.13.",
        f"{DIGEST_PREFIX}{range_digest(ROOT / range_file)}",
        "",
    ]
    for name in sorted(pins):
        hs = hashes_for(name, pins[name]) if with_hashes else []
        lines.append(f"{name}=={pins[name]}" + (" \\" if hs else ""))
        for i, h in enumerate(hs):
            lines.append(f"    --hash=sha256:{h}" + (" \\" if i < len(hs) - 1
                                                     else ""))
    return "\n".join(lines) + "\n"


def problems(range_path, lock_path):
    """What is wrong with a lock file as a lock of a range file (a list of
    sentences; empty when they agree)."""
    from packaging.requirements import Requirement
    from packaging.version import Version
    out = []
    pins, digest = parse_lock(lock_path)
    if not pins:
        return ["the lock file pins nothing"]
    if digest != range_digest(range_path):
        out.append("the range file changed since the lock file was made")
    for line in range_lines(range_path):
        req = Requirement(line)
        name = canon(req.name)
        if name not in pins:
            out.append(f"{name} is in the range file but not in the lock")
        elif not req.specifier.contains(Version(pins[name][0]),
                                        prereleases=True):
            out.append(f"{name} is locked at {pins[name][0]}, outside "
                       f"{req.specifier}")
    for name, (version, hashes) in pins.items():
        if not hashes:
            out.append(f"{name} has no hash")
        if not re.fullmatch(r"\d+(\.\d+)*", version):
            out.append(f"{name}=={version} is not a plain release version")
    text = Path(lock_path).read_text(encoding="utf-8")
    if re.search(r"[A-Za-z]:\\|file:|@ |\\\\", text.replace(" \\\n", "\n")):
        out.append("the lock file holds a local path or URL")
    return out


def main(argv):
    if "--check" in argv:
        bad = [f"{l}: {p}" for r, l in LOCKS
               for p in problems(ROOT / r, ROOT / l)]
        print("\n".join(bad) if bad else "lock files are current")
        return 1 if bad else 0
    for range_file, lock_file in LOCKS:
        text = make(range_file, lock_file)
        (ROOT / lock_file).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {lock_file} ({text.count('==')} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
