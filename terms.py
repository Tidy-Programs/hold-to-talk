"""The Terms of Use: the text (TERMS.md, a fixed file next to the code) and
the local record of which version of it was accepted.

    terms.is_current()        True when the data folder holds a record for
                              settings.TERMS_VERSION
    terms.read_record()       {"version", "accepted_at", "how"} or None
    terms.accept("app")       write the record (atomically); returns it
    terms.read_text()         the text of TERMS.md (OSError if it is missing)
    terms.summary(text)       the plain-words summary section of that text

The record is one small JSON file in the data folder (terms-accepted.json).
It holds the version accepted, the UTC time and how it was accepted
("installer" or "app"), and nothing personal. It is never sent anywhere. The
installer writes the same file (install.ps1), the uninstaller's
delete-my-data option removes it (htt-common.ps1), and a record for another
version counts as no record. A record whose how is "unattended" is understood but is NOT
an acceptance: only a person choosing "I agree" in the installer or in the
app accepts. install.bat -Yes writes no record at all.
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

import settings

log = logging.getLogger("holdtotalk.terms")

RECORD_NAME = "terms-accepted.json"
HOWS = ("installer", "app", "unattended")
ACCEPTING_HOWS = ("installer", "app")     # "unattended" never counts
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_VERSION_LINE = re.compile(r"^Version (\S+) — (\d{1,2} [A-Za-z]+ \d{4})\s*$")


def terms_path():
    """The one file the terms text is read from (never a request's path)."""
    return Path(settings.APP_DIR) / "TERMS.md"


def record_path():
    return Path(settings.DATA_DIR) / RECORD_NAME


def read_text():
    """TERMS.md as text. OSError (or UnicodeDecodeError) if it cannot be read."""
    return terms_path().read_text(encoding="utf-8-sig")


def version_line(text):
    """(version, date) from the first line of the terms, or None."""
    first = text.lstrip("﻿").split("\n", 1)[0].strip()
    m = _VERSION_LINE.match(first)
    return (m.group(1), m.group(2)) if m else None


def summary(text):
    """The plain-words summary: what follows the "## Summary" heading, up to
    the first line that is only "---". Empty when there is none."""
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if not inside:
            inside = line.strip().lower() == "## summary"
            continue
        if line.strip() == "---":
            break
        out.append(line)
    return "\n".join(out).strip()


def read_record():
    """The accepted-terms record, or None when there is none or it cannot be
    understood (an unreadable record counts as no record)."""
    try:
        raw = record_path().read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version, at, how = (data.get("version"), data.get("accepted_at"),
                        data.get("how"))
    if (not isinstance(version, str) or not version.strip()
            or not isinstance(at, str) or not at.strip()
            or how not in HOWS):
        return None
    return {"version": version.strip(), "accepted_at": at.strip(), "how": how}


def is_current():
    """True when the data folder holds a record of a person accepting this
    version (an "unattended" record does not count)."""
    rec = read_record()
    return (rec is not None and rec["version"] == settings.TERMS_VERSION
            and rec["how"] in ACCEPTING_HOWS)


def accept(how="app"):
    """Record that the terms of settings.TERMS_VERSION were accepted now.
    Returns the record; OSError if it could not be written."""
    if how not in ACCEPTING_HOWS:
        raise ValueError("unknown way of accepting")
    rec = {"version": settings.TERMS_VERSION,
           "accepted_at": time.strftime(TIME_FORMAT, time.gmtime()),
           "how": how}
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".terms-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:       # a scanner may hold it for a moment
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("Terms of Use version %s accepted (%s)", rec["version"], how)
    return rec
