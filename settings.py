"""Configuration: defaults, loading and atomic saving of config.json, and
applying changes to the running app (apply_changes)."""

import copy
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import cleanup
import keys
import learn
import sounds
import textrules

log = logging.getLogger("holdtotalk.settings")

APP_NAME = "Hold to talk"        # the one place the display name lives
APP_VERSION = "0.9.1"
TERMS_VERSION = "1.0"            # must match the first line of TERMS.md
APP_DIR = Path(__file__).resolve().parent     # the code folder only


def _default_data_dir():
    """HOLDTOTALK_HOME (used as the data directory itself) or
    %LOCALAPPDATA%\\HoldToTalk\\data."""
    override = os.environ.get("HOLDTOTALK_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(
        Path.home() / "AppData" / "Local")
    return Path(base) / "HoldToTalk" / "data"


# User data lives outside the source folder. Importing this module creates
# nothing; ensure_data_dirs() / save_config / History create folders on demand.
DATA_DIR = _default_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"        # dictate.py --config replaces this
HISTORY_PATH = DATA_DIR / "history.sqlite3"
LOG_PATH = DATA_DIR / "logs" / "dictate.log"


def ensure_data_dirs():
    """Create the data folder and the log folder if they are missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)

# The listening pill (overlay.py). Colours are "#rrggbb"; anything else
# falls back to these defaults (see overlay_colors).
OVERLAY_DEFAULTS = {"color": "#f4f4f5", "background": "#17181c"}

DEFAULTS = {
    "hotkey": "ctrl+windows",      # hold to talk
    "model": "large-v3-turbo",     # used on the GPU
    "cpu_model": "small",          # used when no GPU is available
    "device": "auto",              # auto | cuda | cpu
    "compute_type": "auto",        # auto, or one of COMPUTE_TYPES
    "language": None,              # None = detect, or "en", "es", ...
    "input_device": None,          # None = default mic, or a device index/name
    "vocabulary": [],              # names and jargon to spell correctly
    "dictionary": [],              # [{"heard": "jason", "wanted": "JSON"}]
    "snippets": [],                # [{"trigger": "my address", "text": "..."}]
    "replacements": {},            # legacy {"heard": "wanted"}; prefer dictionary
    "remove_fillers": True,
    "trailing_space": True,
    "restore_clipboard": True,
    "save_history": True,
    "min_seconds": 0.35,
    "pause_hotkey": None,          # hotkey spec that pauses dictation, or None
    "cleanup": dict(cleanup.DEFAULTS),       # local-LLM cleanup (Ollama)
    "textrules": dict(textrules.DEFAULTS),   # snippet mode, history limit, ...
    "overlay": dict(OVERLAY_DEFAULTS),       # listening pill colours
    "learning": dict(learn.DEFAULTS),        # automatic learning
    "sounds": dict(sounds.DEFAULTS),         # start / stop chimes
}

# The values "compute_type" accepts. "auto" picks the best type the device
# supports (float16 on a graphics card, int8 on the processor); another
# value is used when the device supports it, else "auto" is used and a
# warning is logged. int8_float16 needs about half the graphics memory of
# float16 (measured on one card: 1.3 GB instead of 2.4 GB, same speed).
COMPUTE_TYPES = ("auto", "float16", "int8_float16", "int8", "bfloat16",
                 "int8_bfloat16", "float32", "int8_float32", "int16")

# Settings sections: dicts whose keys are merged with the defaults on load
# and with the file on save, so a partial section never loses the others.
# (Data dicts such as "replacements" are replaced as a whole.)
SECTIONS = ("cleanup", "textrules", "overlay", "learning", "sounds")

_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def overlay_colors(cfg):
    """{"color", "background"} for the pill: cfg["overlay"] over the
    defaults, lower-cased; a missing or malformed value (not "#rrggbb")
    falls back to its default, so a bad config never breaks the pill."""
    out = dict(OVERLAY_DEFAULTS)
    sub = cfg.get("overlay") if isinstance(cfg, dict) else None
    if isinstance(sub, dict):
        for key in out:
            value = sub.get(key)
            if isinstance(value, str) and _HEX.fullmatch(value.strip()):
                out[key] = value.strip().lower()
    return out


def contrast_ratio(a, b):
    """WCAG contrast ratio (1..21) of two "#rrggbb" colours."""
    def lum(hexcolor):
        channels = [int(hexcolor[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
               for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# Below this the bars are hard to see on the pill (WCAG asks 3:1 for
# graphical objects).
MIN_OVERLAY_CONTRAST = 3.0


def check_overlay(color, background):
    """Validate a pill colour pair. Returns warnings (e.g. low contrast);
    raises ValueError for something that is not a "#rrggbb" colour."""
    for name, value in (("colour", color), ("background", background)):
        if not isinstance(value, str) or not _HEX.fullmatch(value.strip()):
            raise ValueError(f"The {name} must be a colour like #ff4fa3.")
    ratio = contrast_ratio(color.strip(), background.strip())
    if ratio < MIN_OVERLAY_CONTRAST:
        return [f"These colours are hard to tell apart (contrast {ratio:.1f}"
                f":1, aim for at least {MIN_OVERLAY_CONTRAST:.0f}:1): the "
                "bars may be hard to see on the pill."]
    return []


def merge_sections(base, update):
    """base.update(update), except SECTIONS dicts are merged key by key."""
    for key, value in update.items():
        if (key in SECTIONS and isinstance(value, dict)
                and isinstance(base.get(key), dict)):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return base


class ConfigError(ValueError):
    """config.json cannot be read or written safely; str(exc) is a plain
    message for the user. Nothing was changed."""


def read_file(path=None):
    """The raw JSON object in config.json (no defaults); {} if missing or
    unreadable."""
    try:
        return _read(Path(path or CONFIG_PATH))
    except (OSError, ValueError):
        return {}


def _read(path):
    """The JSON object stored at `path`; {} if missing or blank. Raises
    ValueError on bad JSON, OSError if it cannot be read."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("top level is not a JSON object")
    return data


# -- validating values --------------------------------------------------------

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def _type_name(v):
    return "nothing" if v is None else type(v).__name__


def _language_ok(code):
    if LANGUAGE_CODES:
        return code in LANGUAGE_CODES
    return bool(re.fullmatch(r"[a-z]{2,3}", code))


def _special(path, value):
    """(ok, value, why) for keys with rules beyond their type. Returns None
    when `path` has no special rule."""
    if path == ("language",):
        if value is None:
            return True, None, ""
        if isinstance(value, str) and _language_ok(value.strip().lower()):
            return True, value.strip().lower(), ""
        return False, None, ("not a Whisper language code (use e.g. \"en\" "
                             "or null to detect)")
    if path == ("input_device",):
        ok = (value is None or isinstance(value, str)
              or (isinstance(value, int) and not isinstance(value, bool)
                  and value >= 0))
        return ok, value, "expected a device number, a name or null"
    if path == ("pause_hotkey",):
        # Only the type here: whether the spec is usable is for the app (and
        # validate_change) to say, so a bad value is reported, not hidden.
        return (value is None or (isinstance(value, str) and bool(value.strip())),
                value, "expected a hotkey text or null")
    if path == ("hotkey",):
        return (isinstance(value, str) and bool(value.strip()), value,
                "expected a hotkey text")
    if path == ("device",):
        return (value in ("auto", "cuda", "cpu"), value,
                "expected auto, cuda or cpu")
    if path == ("compute_type",):
        ok = isinstance(value, str) and value.strip().lower() in COMPUTE_TYPES
        return (ok, value.strip().lower() if ok else value,
                "expected one of " + ", ".join(COMPUTE_TYPES))
    if path in (("model",), ("cpu_model",), ("cleanup", "model"),
                ("cleanup", "url")):
        return (isinstance(value, str) and bool(value.strip()), value,
                "expected a non-empty text")
    if path == ("min_seconds",):
        return (_is_number(value) and value >= 0, value,
                "expected a number of seconds (0 or more)")
    if path == ("cleanup", "timeout"):
        return (_is_number(value) and value > 0, value,
                "expected a number of seconds above 0")
    if path == ("cleanup", "min_words"):
        return (isinstance(value, int) and not isinstance(value, bool)
                and value >= 0, value, "expected a whole number")
    if path == ("cleanup", "max_words"):
        return (isinstance(value, int) and not isinstance(value, bool)
                and value >= 1, value, "expected a whole number above 0")
    if path == ("textrules", "history_limit"):
        return (isinstance(value, int) and not isinstance(value, bool)
                and value >= 1, value, "expected a whole number above 0")
    if path == ("textrules", "snippet_mode"):
        return (value in textrules.SNIPPET_MODES, value,
                "expected one of " + ", ".join(textrules.SNIPPET_MODES))
    if path in (("overlay", "color"), ("overlay", "background")):
        return (isinstance(value, str) and bool(_HEX.fullmatch(value.strip())),
                value, "expected a colour like #ff4fa3")
    if path == ("sounds", "volume"):
        if not _is_number(value):
            return False, None, "expected a number from 0 to 1"
        if not 0.0 <= value <= 1.0:
            return True, min(1.0, max(0.0, float(value))), ""
        return True, value, ""
    return None


def _by_default(value, default):
    """True if `value` has the type of `default` (a missing-type default,
    None, accepts anything)."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return _is_number(value)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _expected(default):
    return {bool: "true or false", int: "a whole number",
            float: "a number", str: "text", list: "a list",
            dict: "a section"}.get(type(default), "a different type")


def _check(path, value, default, problems):
    """The value to keep for `path`: `value` if it is fine, else a copy of
    `default` (and a problem is appended; values are never quoted)."""
    name = ".".join(path)
    special = _special(path, value)
    if special is not None:
        ok, kept, why = special
        if ok:
            if path[-1] == "volume" and kept != value:
                problems.append(f"{name}: out of range; clamped")
            return kept
        problems.append(f"{name}: {why or 'invalid value'} "
                        f"(found {_type_name(value)})")
        return copy.deepcopy(default)
    if not _by_default(value, default):
        problems.append(f"{name}: expected {_expected(default)} "
                        f"(found {_type_name(value)})")
        return copy.deepcopy(default)
    return value


def validate(cfg):
    """(clean_cfg, problems): `cfg` checked key by key against the types of
    DEFAULTS. A wrongly typed or out-of-range value is replaced by its
    default and named in `problems` (plain strings, e.g. "min_seconds:
    expected a number (found str)"; the bad value itself is never quoted).
    Keys without a default are kept untouched, missing keys are not added,
    `cfg` is not modified."""
    problems = []
    if not isinstance(cfg, dict):
        return copy.deepcopy(DEFAULTS), ["config: not a JSON object"]
    clean = {}
    for key, value in cfg.items():
        if key not in DEFAULTS:
            if key == "learned" and not isinstance(value, list):
                problems.append(f"learned: expected a list "
                                f"(found {_type_name(value)})")
                value = []
            clean[key] = copy.deepcopy(value)
            continue
        default = DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                problems.append(f"{key}: expected a section "
                                f"(found {_type_name(value)})")
                clean[key] = copy.deepcopy(default)
                continue
            if key == "replacements":
                clean[key] = copy.deepcopy(value)
                continue
            sub = {}
            for k, v in value.items():
                if k in default:
                    sub[k] = _check((key, k), v, default[k], problems)
                else:
                    sub[k] = copy.deepcopy(v)
            clean[key] = sub
        elif default is None:
            kept = _check((key,), value, None, problems)
            clean[key] = kept
        else:
            clean[key] = _check((key,), copy.deepcopy(value), default,
                                problems)
    return clean, problems


# -- load and save --------------------------------------------------------------

def load_config(path=None):
    """DEFAULTS overlaid with config.json. Unknown keys are kept; a partial
    settings section ("cleanup", "textrules", ...) keeps the other defaults.

    A value of the wrong type is replaced by its default, with one warning
    naming the key (see validate). A config file that cannot be parsed is
    copied to config.json.bad (so a later save cannot silently destroy the
    user's edits) and defaults are used. Excerpts of dictations that older
    versions stored in "learned" records are blanked (and the file is
    rewritten without them).
    """
    path = Path(path or CONFIG_PATH)
    cfg = copy.deepcopy(DEFAULTS)      # never share DEFAULTS' lists/dicts
    parsed = False
    try:
        merge_sections(cfg, _read(path))
        parsed = True
    except (OSError, ValueError) as exc:
        log.warning("%s could not be read (%s); using defaults", path.name, exc)
        try:
            shutil.copyfile(path, path.with_name(path.name + ".bad"))
        except OSError:
            pass
    cfg, problems = validate(cfg)
    for problem in problems:
        log.warning("Setting %s; using the default", problem)
    if isinstance(cfg.get("cleanup"), dict) and "allow_remote" in cfg["cleanup"]:
        # An option that no longer exists: AI cleanup only ever talks to
        # Ollama on this computer. Ignored here, and dropped on the next save.
        del cfg["cleanup"]["allow_remote"]
        log.warning("The setting cleanup.allow_remote is no longer supported "
                    "and is ignored: AI cleanup only talks to Ollama on this "
                    "computer")
    learned, changed = learn.scrub_excerpts(cfg.get("learned"))
    if changed:
        cfg["learned"] = learned
        if parsed:
            try:
                save_config({"learned": learned}, path)
            except Exception as exc:
                log.warning("Could not clear old excerpts from %s: %s",
                            path.name, type(exc).__name__)
    return cfg


def _backup_bad(path):
    """Copy an unparsable config next to itself as config.json.bad-<time>
    (an identical earlier copy is reused). Returns the backup's path."""
    raw = Path(path).read_bytes()
    for old in sorted(path.parent.glob(path.name + ".bad-*")):
        try:
            if old.read_bytes() == raw:
                return old
        except OSError:
            continue
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.bad-{stamp}")
    n = 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.bad-{stamp}-{n}")
    shutil.copyfile(path, dest)
    return dest


def save_config(cfg, path=None):
    """Atomically write `cfg` to config.json.

    Keys already in the file but absent from `cfg` are preserved, so callers
    may pass a partial dict; a partial section ({"cleanup": {"enabled":
    False}}) only changes the keys it names. Writes a temp file in the same
    folder and then os.replace()s it, so a crash never leaves a half-written
    config.

    If the file exists but cannot be parsed, nothing is written: it is copied
    to config.json.bad-<timestamp> and ConfigError is raised, so the user's
    vocabulary, dictionary and snippets are never replaced by an empty config.
    """
    path = Path(path or CONFIG_PATH)
    try:
        data = _read(path)
    except ValueError as exc:
        try:
            backup = _backup_bad(path).name
            kept = f"A copy was saved as {backup}. "
        except OSError:
            kept = ""
        raise ConfigError(
            f"{path.name} has an error ({exc}) and was not changed. {kept}"
            "Fix the file, or delete it to start again, then try again."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{path.name} could not be read ({exc}), so it was "
                          "not changed.") from exc
    merge_sections(data, copy.deepcopy(dict(cfg)))
    if isinstance(data.get("cleanup"), dict):
        data["cleanup"].pop("allow_remote", None)    # removed option
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cfg-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows refuses the replace while another process (an editor, an
        # antivirus scan) briefly holds the file; retry for a moment.
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# -- moving data out of the source folder --------------------------------------

BACKUP_WAIT = 3.0       # s to wait for a locked history before giving up


def _row_count(path, immutable=False):
    """Rows in the history table of the database at `path` (None if there is
    no such table). immutable: the file will not change meanwhile, so SQLite
    takes no locks and creates no -wal / -shm files next to it."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro"
                           + ("&immutable=1" if immutable else ""),
                           uri=True, timeout=1.0)
    try:
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                           " AND name='history'").fetchone()
        return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] \
            if has else None
    finally:
        conn.close()


def _free_name(path, suffix):
    """path + suffix, or path + suffix-2, ... : a name nothing uses yet."""
    dest = path.with_name(path.name + suffix)
    n = 1
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}{suffix}-{n}")
    return dest


def _retire(path, summary):
    """Rename an old file to <name>.migrated (never deletes, never
    overwrites). Its -wal/-shm companions follow it."""
    dest = _free_name(path, ".migrated")
    try:
        os.replace(path, dest)
    except OSError as exc:
        summary["errors"].append(f"{path.name}: could not rename "
                                 f"({type(exc).__name__})")
        return False
    summary["renamed"].append(dest.name)
    for tail in ("-wal", "-shm"):
        side = path.with_name(path.name + tail)
        if side.exists():
            try:
                os.replace(side, dest.with_name(dest.name + tail))
            except OSError:
                pass
    return True


def _copy_history(old, new):
    """Copy a history database with sqlite3's backup API (a file copy would
    miss rows still in the write-ahead log) and verify the row count. The new
    file only appears once it is complete."""
    new.parent.mkdir(parents=True, exist_ok=True)
    tmp = new.with_name(new.name + ".copying")
    if tmp.exists():
        tmp.unlink()
    src = sqlite3.connect(old.resolve().as_uri() + "?mode=ro", uri=True,
                          timeout=1.0)
    try:
        dst = sqlite3.connect(str(tmp))
        deadline = time.monotonic() + BACKUP_WAIT

        def progress(status, remaining, total):
            # backup() retries a busy source forever; give up instead.
            if (status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                    and time.monotonic() > deadline):
                raise sqlite3.OperationalError("database is locked")
        try:
            src.backup(dst, sleep=0.05, progress=progress)
        finally:
            dst.close()
        want = src.execute("SELECT COUNT(*) FROM sqlite_master WHERE "
                           "name='history'").fetchone()[0]
        expected = src.execute("SELECT COUNT(*) FROM history").fetchone()[0] \
            if want else None
    except BaseException:
        src.close()
        _remove_copy(tmp)
        raise
    src.close()
    try:
        got = _row_count(tmp, immutable=True)
    except BaseException:
        _remove_copy(tmp)
        raise
    # A copy of a WAL-mode database is itself marked WAL, and the final
    # rename moves only the one file: nothing may be left beside it.
    for tail in ("-wal", "-shm", "-journal"):
        try:
            Path(str(tmp) + tail).unlink()
        except OSError:
            pass
    if got != expected:
        _remove_copy(tmp)
        raise RuntimeError(f"row count differs ({got} copied, "
                           f"{expected} expected)")
    os.replace(tmp, new)
    return got or 0


def _remove_copy(tmp):
    """Delete a half-made copy and every side file SQLite made for it."""
    for tail in ("", "-journal", "-wal", "-shm"):
        try:
            Path(str(tmp) + tail).unlink()
        except OSError:
            pass


def migrate_legacy_data(app_dir=APP_DIR, data_dir=DATA_DIR):
    """Move config.json and history.sqlite3 from next to the code into the
    data folder. Idempotent, never deletes and never overwrites.

    For each file: if the new one is missing and an old one exists it is
    copied (history through sqlite3's backup API, then the row count is
    compared); only then is the old file renamed to <name>.migrated. If the
    new file already exists the old one is left alone.

    Returns {"config": status, "history": status, "renamed": [names],
             "errors": [messages]} where status is "migrated", "copied" (copy
    done, old file could not be renamed), "none" (nothing to move),
    "kept_new" (new file exists, old one untouched) or "failed".
    """
    app_dir, data_dir = Path(app_dir), Path(data_dir)
    summary = {"config": "none", "history": "none", "renamed": [],
               "errors": []}
    old_cfg, new_cfg = app_dir / "config.json", data_dir / "config.json"
    old_db, new_db = app_dir / "history.sqlite3", data_dir / "history.sqlite3"

    if old_cfg.is_file():
        if new_cfg.exists():
            summary["config"] = "kept_new"
        else:
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                tmp = new_cfg.with_name(new_cfg.name + ".copying")
                shutil.copyfile(old_cfg, tmp)
                os.replace(tmp, new_cfg)
                summary["config"] = "copied"
                if _retire(old_cfg, summary):
                    summary["config"] = "migrated"
            except OSError as exc:
                summary["config"] = "failed"
                summary["errors"].append(
                    f"config.json: {type(exc).__name__}")
    if old_db.is_file():
        if new_db.exists():
            summary["history"] = "kept_new"
        else:
            try:
                _copy_history(old_db, new_db)
                summary["history"] = "copied"
                if _retire(old_db, summary):
                    summary["history"] = "migrated"
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                summary["history"] = "failed"
                summary["errors"].append(
                    f"history.sqlite3: {type(exc).__name__}: {exc}")
    if summary["renamed"] or summary["errors"]:
        log.info("Data migration: config %s, history %s%s", summary["config"],
                 summary["history"],
                 f", {len(summary['errors'])} problem(s)"
                 if summary["errors"] else "")
    return summary


# -- changing the running app's settings -------------------------------------

# Held for every read-modify-write of the config by the tray and the app
# window, so two quick changes can never lose one another. Never taken on
# the Tk thread: apply_changes() waits for the Tk thread (reload_config).
CONFIG_LOCK = threading.RLock()


def apply_changes(app, changes, path=None):
    """Save `changes` (a partial config) and apply them via app.reload_config().

    A section dict ("cleanup", "textrules", "overlay", "learning") only
    changes the keys it names (see save_config); the rest of that section in
    the file is kept and defaults are not written into the file. Keys the
    app rejects are put back to the value the app kept, so file and app
    agree. Call from any thread except the Tk thread, under CONFIG_LOCK when
    the changes were computed from app.cfg. Returns reload_config()'s dict
    plus "rejected": [keys].
    """
    changes = dict(changes)
    with CONFIG_LOCK:
        try:
            save_config(changes, path)
        except Exception as exc:
            return {"applied": [], "restart_required": [],
                    "rejected": list(changes),
                    "errors": [f"Could not save config.json: {exc}"]}
        try:
            result = dict(app.reload_config())
        except Exception as exc:
            return {"applied": [], "restart_required": [], "rejected": [],
                    "errors": [f"Saved, but the app could not apply it: {exc}"]}
        rejected = []
        for msg in result.get("errors", []):
            key = str(msg).split(":", 1)[0].strip()
            if key in changes and key not in rejected:
                rejected.append(key)
        if rejected:
            try:
                save_config({k: app.cfg.get(k) for k in rejected}, path)
            except Exception as exc:
                result.setdefault("errors", []).append(
                    f"Could not restore the previous value in config.json: {exc}")
        result["rejected"] = rejected
        return result


def normalize_hotkey(text):
    """User text -> canonical hotkey string ('Mouse Back' -> 'mouse4').
    Raises ValueError if invalid (including left/right mouse buttons)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Hotkey is empty")
    return keys.canonical_hotkey(text)


def validate_change(key, value, cfg=None):
    """Validate one setting change before it is saved. Returns the partial
    config to save, e.g. {"pause_hotkey": "f9"} or {"sounds": {"volume":
    0.3}}; raises ValueError with a plain message. Returns None for a key
    this function does not know (the caller validates those itself).

    hotkey       a hotkey spec (see normalize_hotkey); not the pause hotkey
    pause_hotkey None (off), or a spec keys.parse_hotkey accepts that is not
                 the main hotkey
    language     see normalize_language
    sounds       {"enabled": bool, "volume": 0.0-1.0 (clamped),
                 "mute_others": bool}; only the keys named are changed
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if key == "hotkey":
        spec = normalize_hotkey(value)
        other = cfg.get("pause_hotkey")
        if isinstance(other, str) and _same_hotkey(spec, other):
            raise ValueError("That is already the pause hotkey. Choose a "
                             "different key, or turn the pause hotkey off.")
        return {"hotkey": spec}
    if key == "pause_hotkey":
        if value is None or (isinstance(value, str) and not value.strip()):
            return {"pause_hotkey": None}
        if not isinstance(value, str):
            raise ValueError("The pause hotkey must be a key combination, "
                             "or empty to turn it off.")
        try:
            spec = keys.canonical_hotkey(value)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"That is not a usable pause hotkey: {exc}")
        main = cfg.get("hotkey", DEFAULTS["hotkey"])
        if isinstance(main, str) and _same_hotkey(spec, main):
            raise ValueError("The pause hotkey must be different from the "
                             "dictation hotkey.")
        return {"pause_hotkey": spec}
    if key == "language":
        return {"language": normalize_language(value)}
    if key == "sounds":
        if not isinstance(value, dict) or set(value) - {
                "enabled", "volume", "mute_others"}:
            raise ValueError("Send the sounds setting as enabled, volume "
                             "and/or mute_others.")
        out = {}
        if "enabled" in value:
            if not isinstance(value["enabled"], bool):
                raise ValueError("Sounds on/off must be true or false.")
            out["enabled"] = value["enabled"]
        if "mute_others" in value:
            if not isinstance(value["mute_others"], bool):
                raise ValueError("Muting other sound must be true or false.")
            out["mute_others"] = value["mute_others"]
        if "volume" in value:
            vol = value["volume"]
            if not _is_number(vol):
                raise ValueError("The sound volume must be a number from 0 "
                                 "to 1.")
            out["volume"] = min(1.0, max(0.0, float(vol)))
        if not out:
            raise ValueError("Nothing to change in the sounds setting.")
        return {"sounds": out}
    return None


def _same_hotkey(a, b):
    try:                    # the same keys, whatever the order or spelling
        return frozenset(keys.parse_hotkey(a)) == frozenset(keys.parse_hotkey(b))
    except (ValueError, TypeError, KeyError):
        return str(a).strip().lower() == str(b).strip().lower()

def cleanup_enabled(cfg):
    """Same answer cleanup.clean() acts on (a missing section = default)."""
    return bool(cleanup.merged_settings(cfg).get("enabled"))


# -- languages ------------------------------------------------------------------

# Whisper's language codes, built in so that importing this module does not
# import faster-whisper (about 0.3 s, and PyAV, which the app never uses).
# tests/test_settings.py checks the list against the installed library.
LANGUAGE_CODES = (
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "zh", "yue")

LANGUAGE_NAMES = {
    "en": "English", "de": "German", "es": "Spanish", "fr": "French",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
    "cs": "Czech", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "el": "Greek", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "vi": "Vietnamese",
    "id": "Indonesian", "th": "Thai", "hu": "Hungarian", "ro": "Romanian"}


def languages():
    """[(code, name)]: the common languages by name, then the other codes
    Whisper knows (named by their code)."""
    common = [c for c in LANGUAGE_NAMES if not LANGUAGE_CODES
              or c in LANGUAGE_CODES]
    rest = sorted(c for c in LANGUAGE_CODES if c not in LANGUAGE_NAMES)
    return [(c, LANGUAGE_NAMES[c]) for c in common] + [(c, c) for c in rest]


def normalize_language(text):
    """None / '' / 'auto' -> None (detect); 'en - English' / 'EN' -> 'en'.
    Raises ValueError for an unknown code."""
    text = (text or "").strip() if isinstance(text, str) or text is None \
        else None
    if text is None:
        raise ValueError("Language must be a language code")
    if not text or text.lower() in ("auto", "auto-detect", "none", "null"):
        return None
    code = text.split(" - ")[0].strip().lower()
    if LANGUAGE_CODES:
        if code not in LANGUAGE_CODES:
            raise ValueError(f"Unknown language code {code!r}")
    elif not re.fullmatch(r"[a-z]{2,3}", code):
        raise ValueError(f"Unknown language code {code!r}")
    return code
