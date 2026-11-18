"""Text rules (dictionary, snippets, spoken formatting) and dictation history.

Pipeline stage
    apply(text, cfg) -> str
        1. Snippets: a spoken trigger phrase becomes its stored text.
        2. Formatting commands ("new line", "new paragraph"), if enabled.
        3. Dictionary corrections ("jason" -> "JSON"), including the legacy
           cfg["replacements"] dict.
        Snippet expansions are swapped out for private placeholders while
        steps 2 and 3 run, so an expansion is pasted exactly as stored and is
        never re-processed by a command, the dictionary or another snippet.
        Every step is a single pass, so rules never chain ("a"->"b", "b"->"c"
        turns "a" into "b", not "c").

    Snippet triggers, cfg["textrules"]["snippet_mode"]:
        "sentence" (default)  the trigger must be a sentence of its own:
                              "My address." / "my address," / "Thanks. My
                              signature." expand; "I changed my address last
                              week." is left alone.
        "whole"               only when the trigger is the entire dictation.
        "anywhere"            any whole-phrase occurrence expands, e.g. "Send
                              it to my address, please." When the trigger is
                              a sentence of its own it is treated as in
                              "sentence" mode (its trailing punctuation is
                              dropped); inside a sentence the surrounding
                              punctuation is kept.

    Formatting commands (cfg["textrules"]["formatting_commands"], off by
    default) are only obeyed when the command is a clause of its own:
    preceded by the start of the text or punctuation, and followed by
    punctuation or the end ("Dear John, new line. How are you?"). "We
    launched a new line of products." is never touched; "Hello new line how
    are you" (no punctuation from Whisper) is not obeyed either.

Glossary
    vocabulary_terms(cfg) -> list[str] for stt.py's Whisper prompt.

History
    History(path=None, cfg=None) - SQLite store: record(), recent(), search(),
    get(), at(), delete(), clear(), count(), flush(), close(); corrections:
    correct(), uncorrect(), corrections(); dashboard: stats().
    Learning from corrections lives in learn.py; the app window (api.py,
    ui/) is where the user browses and corrects it.

Snippets, by hand
    check_snippet / add_snippet / edit_snippet / remove_snippet: pure
    functions like learn.py's, raising ValueError with a message for the UI.

CLI
    python textrules.py apply "raw text" [--config config.json]
    python textrules.py vocab [--config config.json]
    python textrules.py history [--recent N] [--search WORD] [--delete ID]
                                [--clear] [--path FILE]
"""

import functools
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("holdtotalk.textrules")

DEFAULTS = {
    "snippet_mode": "sentence",       # sentence | whole | anywhere
    "formatting_commands": False,     # obey "new line" / "new paragraph"
    "history_limit": 2000,            # keep at most this many dictations
}

SNIPPET_MODES = ("sentence", "whole", "anywhere")
HISTORY_FILENAME = "history.sqlite3"
HISTORY_SCHEMA = 2
HISTORY_COLUMNS = frozenset(("id", "ts", "raw", "final", "seconds",
                             "corrected", "corrected_ts", "words"))
_ROW_COLS = "id, ts, raw, final, seconds, corrected, corrected_ts"
WRITE_RETRY_DELAYS = (0.25, 0.5, 1.0, 2.0, 4.0)   # background writer only
READ_FLUSH_WAIT = 0.25    # reads wait at most this long for queued writes
CHECKPOINT_TRIES = 4      # emptying the write-ahead log: attempts ...
CHECKPOINT_PAUSE = 0.1    # ... this many seconds apart

# Placeholders for snippet expansions: START, one index char, END. All are
# Unicode private-use characters, which are not \w, not punctuation and never
# produced by Whisper; any already in the input are removed first.
_PH_START, _PH_END, _PH_BASE = "\ue000", "\ue001", 0xE100
_PH_MAX = 0xF8FF - _PH_BASE
_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")
_PLACEHOLDER = re.compile(_PH_START + "(.)" + _PH_END, re.S)

_SENTENCE_END = ".!?\u2026"           # . ! ? ...
_CLAUSE_PUNCT = ",;:" + _SENTENCE_END
_APOSTROPHES = "'\u2019"
_SEP = r"(?:[ \t]*[,\-][ \t]*|[ \t]+)"   # between the words of a phrase
_WORD_SPLIT = re.compile(r"[\s\-]+")


def settings_of(cfg):
    """cfg["textrules"] layered over DEFAULTS (bad values fall back)."""
    out = dict(DEFAULTS)
    sub = cfg.get("textrules") if isinstance(cfg, dict) else None
    if isinstance(sub, dict):
        out.update(sub)
    if out.get("snippet_mode") not in SNIPPET_MODES:
        out["snippet_mode"] = DEFAULTS["snippet_mode"]
    try:
        out["history_limit"] = max(1, int(out["history_limit"]))
    except (TypeError, ValueError):
        out["history_limit"] = DEFAULTS["history_limit"]
    out["formatting_commands"] = bool(out.get("formatting_commands"))
    return out


# -- config entries -----------------------------------------------------------

def _clean_str(value):
    return value.strip() if isinstance(value, str) else ""


def _dictionary_pairs(cfg):
    """[(heard, wanted)] from cfg["dictionary"] then cfg["replacements"].
    Bad entries (non-dict, empty/non-string heard, non-string or empty
    wanted) are skipped; the first entry for a given heard phrase wins."""
    pairs = []
    entries = cfg.get("dictionary") if isinstance(cfg, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                pairs.append((entry.get("heard"), entry.get("wanted")))
    legacy = cfg.get("replacements") if isinstance(cfg, dict) else None
    if isinstance(legacy, dict):
        pairs.extend(legacy.items())
    out, seen = [], set()
    for heard, wanted in pairs:
        heard = _clean_str(heard)
        if not heard or not isinstance(wanted, str) or not wanted.strip():
            continue
        key = _phrase_key(heard)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((heard, wanted))
    return out


def _snippet_pairs(cfg):
    """[(trigger, text)] from cfg["snippets"]; bad entries skipped."""
    out, seen = [], set()
    entries = cfg.get("snippets") if isinstance(cfg, dict) else None
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        trigger, text = entry.get("trigger"), entry.get("text")
        if not isinstance(text, str):
            continue
        words = _trigger_words(_clean_str(trigger))
        if not words:
            continue
        key = " ".join(w.lower() for w in words)
        if key in seen:
            continue
        seen.add(key)
        out.append((tuple(words), text))
    return out


def _phrase_key(phrase):
    return " ".join(w.lower() for w in _WORD_SPLIT.split(phrase) if w)


def _trigger_words(trigger):
    # Punctuation in a trigger is ignored: it is spoken, Whisper decides it.
    return re.findall(r"[^\W_]+(?:['\u2019][^\W_]+)*", trigger)


def _word_pattern(word):
    return "".join("['\u2019]" if ch in _APOSTROPHES else re.escape(ch)
                   for ch in word)


def _phrase_pattern(words):
    """Whole-phrase, case-insensitive-ready pattern for a word sequence."""
    body = _SEP.join(_word_pattern(w) for w in words)
    # Lookarounds instead of \b so entries that start or end with a symbol
    # ("c++", ".net") still get a correct boundary.
    return r"(?<!\w)" + body + r"(?!\w)"


# -- dictionary ---------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def _compile_dictionary(pairs):
    """pairs: tuple of (heard, wanted). Returns (regex, [wanted]) or None."""
    items = []
    for heard, wanted in pairs:
        words = [w for w in _WORD_SPLIT.split(heard) if w]
        if words:
            items.append((words, wanted))
    if not items:
        return None
    # Longest heard phrase first so it wins over its own sub-phrases.
    items.sort(key=lambda it: (-len(it[0]), -len("".join(it[0]))))
    parts = [f"(?P<d{i}>{_phrase_pattern(words)})"
             for i, (words, _) in enumerate(items)]
    return (re.compile("|".join(parts), re.IGNORECASE),
            [wanted for _, wanted in items])


def _at_sentence_start(text, pos):
    before = text[:pos].rstrip(" \t")
    return not before or before[-1] in _SENTENCE_END + "\n" + _PH_END


def apply_dictionary(text, cfg):
    """Dictionary corrections only (see module docstring)."""
    compiled = _compile_dictionary(tuple(_dictionary_pairs(cfg)))
    if not compiled:
        return text
    rx, wanted_list = compiled

    def sub(m):
        wanted = wanted_list[int(m.lastgroup[1:])]
        # An all-lowercase replacement ("gonna" -> "going to") keeps the
        # capital Whisper gave a sentence's first word. Mixed-case wanted
        # spellings ("iPhone", "JSON") are always used exactly as stored.
        if (wanted and wanted == wanted.lower() and m.group(0)[:1].isupper()
                and _at_sentence_start(m.string, m.start())):
            wanted = wanted[0].upper() + wanted[1:]
        return wanted
    return rx.sub(sub, text)


# -- snippets -----------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def _compile_snippets(pairs):
    """pairs: tuple of (words, text). Returns (any_regex, [(regex, text)])."""
    if not pairs:
        return None
    items = sorted(pairs, key=lambda it: (-len(it[0]), -len("".join(it[0]))))
    singles = [(re.compile(_phrase_pattern(words), re.IGNORECASE), text)
               for words, text in items]
    combined = re.compile("|".join(rx.pattern for rx, _ in singles),
                          re.IGNORECASE)
    return combined, singles


_TRAIL_PUNCT = re.compile(r"[ \t]*([" + re.escape(_CLAUSE_PUNCT) + r"]*)")


def _sentence_span(text, start, end):
    """If text[start:end] is a sentence of its own, the end of it including
    its trailing punctuation; otherwise None."""
    if not _at_sentence_start(text, start):
        return None
    m = _TRAIL_PUNCT.match(text, end)
    punct, rest = m.group(1), text[m.end():]
    if not rest.strip(" \t") or rest[:1] in ("\n", "\r", _PH_START):
        return m.end()
    if punct and punct[-1] in _SENTENCE_END and rest[:1] in " \t":
        return m.end()
    return None


def _apply_snippets(text, cfg, store):
    """Replace triggers with placeholders; expansions go into `store`."""
    compiled = _compile_snippets(tuple(_snippet_pairs(cfg)))
    if not compiled:
        return text
    combined, singles = compiled
    mode = settings_of(cfg)["snippet_mode"]
    out, pos, scan = [], 0, 0
    while len(store) < _PH_MAX:
        hit = combined.search(text, scan)
        if not hit:
            break
        at = hit.start()
        done = False
        for rx, expansion in singles:         # longest trigger first
            m = rx.match(text, at)
            if not m:
                continue
            end = _sentence_span(text, at, m.end())
            if mode == "whole" and end is not None and text[:at].strip():
                end = None
            if mode == "whole" and end is not None and text[end:].strip():
                end = None
            if end is None and mode == "anywhere":
                end = m.end()
            if end is None:
                continue
            out.append(text[pos:at])
            out.append(_PH_START + chr(_PH_BASE + len(store)) + _PH_END)
            store.append(expansion)
            pos = scan = end
            done = True
            break
        if not done:
            scan = at + 1
    out.append(text[pos:])
    return "".join(out)


def _restore(text, store):
    return _PLACEHOLDER.sub(lambda m: store[ord(m.group(1)) - _PH_BASE], text)


# -- formatting commands ------------------------------------------------------

_COMMAND = re.compile(r"(?<!\w)new[ \t\-]?(line|paragraph)(?!\w)", re.I)


def _apply_formatting(text):
    """Obey "new line" / "new paragraph" when spoken as a clause of their
    own. The command's own trailing punctuation and the spaces around it are
    dropped; punctuation before it is kept; the next letter is capitalised."""
    done, pos = "", 0
    while True:
        m = _COMMAND.search(text, pos)
        if not m:
            break
        head = done + text[pos:m.start()]
        stripped = head.rstrip(" \t")
        tail = _TRAIL_PUNCT.match(text, m.end())
        rest = text[tail.end():].lstrip(" \t")
        ok_before = (not stripped
                     or stripped[-1] in _CLAUSE_PUNCT + "\n" + _PH_END)
        ok_after = (tail.group(1) or not rest
                    or rest[:1] in ("\n", _PH_START)
                    or _COMMAND.match(rest))
        if not (ok_before and ok_after):
            done, pos = head + m.group(0), m.end()
            continue
        done = stripped + ("\n\n" if m.group(1).lower() == "paragraph"
                           else "\n")
        if rest[:1].islower():
            rest = rest[0].upper() + rest[1:]
        text, pos = rest, 0
    return done + text[pos:]


# -- the stage ----------------------------------------------------------------

def apply(text, cfg):
    """Pipeline stage: snippets, formatting commands, dictionary. Never raises
    for bad config; returns `text` unchanged if it is not a string."""
    if not isinstance(text, str) or not text:
        return text
    if not isinstance(cfg, dict):
        cfg = {}
    text = _PRIVATE_USE.sub("", text)
    store = []
    text = _apply_snippets(text, cfg, store)
    if settings_of(cfg)["formatting_commands"]:
        text = _apply_formatting(text)
    text = apply_dictionary(text, cfg)
    return _restore(text, store)


def rule_hits(text, cfg):
    """How much apply() would act on: snippet triggers it would expand plus
    line breaks spoken formatting commands would insert. An earlier stage
    (the LLM) must not lower this, or the user's rule silently stops
    working."""
    if not isinstance(text, str) or not text or not isinstance(cfg, dict):
        return 0
    store = []
    text = _apply_snippets(_PRIVATE_USE.sub("", text), cfg, store)
    hits = len(store)
    if settings_of(cfg)["formatting_commands"]:
        hits += _apply_formatting(text).count("\n") - text.count("\n")
    return hits


# -- snippets, by hand ----------------------------------------------------------

MAX_SNIPPET_CHARS = 10000


def _snippet_key(trigger):
    return " ".join(w.lower() for w in _trigger_words(_clean_str(trigger)))


def _snippet_list(cfg):
    entries = cfg.get("snippets") if isinstance(cfg, dict) else None
    return [dict(e) if isinstance(e, dict) else e
            for e in entries] if isinstance(entries, list) else []


def _snippet_index(entries, trigger):
    key = _snippet_key(trigger) if isinstance(trigger, str) else ""
    for i, e in enumerate(entries):
        if (key and isinstance(e, dict) and isinstance(e.get("trigger"), str)
                and _snippet_key(e["trigger"]) == key):
            return i
    return None


def check_snippet(cfg, trigger, text, editing=None):
    """Validate a snippet. Returns warnings; raises ValueError with a
    message for the UI. editing: the trigger of the snippet being edited."""
    if not isinstance(trigger, str) or not trigger.strip():
        raise ValueError("Type the phrase you will say.")
    if "\n" in trigger or "\r" in trigger:
        raise ValueError("The phrase must fit on one line.")
    words = _trigger_words(trigger.strip())
    if not words:
        raise ValueError("The phrase needs at least one word.")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Type the text to insert.")
    if len(text) > MAX_SNIPPET_CHARS:
        raise ValueError("That text is too long for a snippet (keep it under "
                         f"{MAX_SNIPPET_CHARS:,} characters).")
    skip = _snippet_key(editing) if isinstance(editing, str) else None
    i = _snippet_index(_snippet_list(cfg), trigger)
    if i is not None and _snippet_key(trigger) != skip:
        other = _snippet_list(cfg)[i]["trigger"].strip()
        raise ValueError(f"“{other}” is already a snippet. Edit that one "
                         "instead.")
    if len(words) == 1:
        return ["A one-word phrase is easy to say by accident; two or three "
                "words (“my signature”) are safer."]
    return []


def add_snippet(cfg, trigger, text):
    """{"changes": {"snippets": [...]}, "warnings": [...]}; ValueError."""
    warnings = check_snippet(cfg, trigger, text)
    entries = _snippet_list(cfg)
    entries.append({"trigger": trigger.strip(), "text": text})
    return {"changes": {"snippets": entries}, "warnings": warnings}


def edit_snippet(cfg, old_trigger, trigger, text):
    """Replace the snippet for `old_trigger`, in place."""
    entries = _snippet_list(cfg)
    i = _snippet_index(entries, old_trigger)
    if i is None:
        raise ValueError(f"There is no snippet “{old_trigger}”.")
    warnings = check_snippet(cfg, trigger, text, editing=old_trigger)
    entries[i] = {"trigger": trigger.strip(), "text": text}
    return {"changes": {"snippets": entries}, "warnings": warnings}


def remove_snippet(cfg, trigger):
    entries = _snippet_list(cfg)
    i = _snippet_index(entries, trigger)
    if i is None:
        raise ValueError(f"There is no snippet “{trigger}”.")
    entries.pop(i)
    return {"changes": {"snippets": entries}, "warnings": []}


def vocabulary_terms(cfg):
    """De-duplicated glossary for Whisper: cfg["vocabulary"], then the
    dictionary's wanted spellings (single-line ones). Case-insensitive
    de-duplication; the first spelling seen is kept."""
    if not isinstance(cfg, dict):
        return []
    terms = []
    vocab = cfg.get("vocabulary")
    if isinstance(vocab, (list, tuple)):
        terms += [v for v in vocab if isinstance(v, str)]
    terms += [wanted for _, wanted in _dictionary_pairs(cfg)]
    out, seen = [], set()
    for term in terms:
        term = term.strip()
        if not term or "\n" in term or "\r" in term:
            continue
        if term.casefold() in seen:
            continue
        seen.add(term.casefold())
        out.append(term)
    return out


# -- history ------------------------------------------------------------------

def default_history_path():
    import settings
    return Path(settings.HISTORY_PATH)


class History:
    """Local store of past dictations (SQLite, standard library).

    History(path=None, cfg=None, busy_timeout=0.5)
        path: database file (default: settings.HISTORY_PATH).
        cfg:  the config dict, or a zero-argument callable returning the
              current one (e.g. `lambda: app.cfg`, since reload_config
              replaces app.cfg). Read on every record() for
              cfg["save_history"] and cfg["textrules"]["history_limit"].

    record(raw, final, audio_seconds) matches app.on_result and returns at
    once: the write happens on a background thread. Every other method waits
    (briefly) for queued writes first, so results include them. Rows are
    dicts {id, ts (epoch seconds), raw, final, seconds}, newest first.

    Errors never propagate: a locked or unopenable file makes a call return
    an empty result / False (logged; the next call tries again, a queued
    record is retried 3 times then dropped); a corrupt file is renamed to
    "<name>.corrupt-<time>" and a fresh one started, or, if it cannot be
    moved, an in-memory database is used for this session.

    Why SQLite rather than JSON Lines: delete-by-id, search and the
    retention cap are single statements instead of whole-file rewrites;
    writes are atomic, so a crash never leaves a half-written record; and
    its file locking makes several threads or processes (the app and the
    CLI) safe without a lock file of our own.
    """

    def __init__(self, path=None, cfg=None, busy_timeout=0.5):
        self.path = Path(path) if path else default_history_path()
        self._cfg = cfg if cfg is not None else {}
        self._timeout = float(busy_timeout)
        self._lock = threading.RLock()
        self._writer_lock = threading.Lock()
        self._conn = None
        self._memory = False
        self._queue = queue.Queue()
        self._writer = None
        self._closed = False

    # -- config ---------------------------------------------------------------

    def _current_cfg(self):
        cfg = self._cfg
        try:
            cfg = cfg() if callable(cfg) else cfg
        except Exception as exc:
            log.error("History: reading the config failed (%s)",
                        type(exc).__name__)
            cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    @property
    def enabled(self):
        return bool(self._current_cfg().get("save_history", True))

    @property
    def limit(self):
        return settings_of(self._current_cfg())["history_limit"]

    # -- connection -----------------------------------------------------------

    def _connect(self):
        if self._conn is not None:
            return self._conn
        # A file that cannot be opened right now (locked by another program,
        # folder not writable) raises: the caller logs it and returns its
        # default, and the next call tries again. Only a corrupt file that
        # cannot be moved aside falls back to memory for this session.
        try:
            self._conn = self._open(str(self.path))
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt(exc):
                raise
            if not self._quarantine(exc):
                self._fallback(exc)
                return self._conn
            self._conn = self._open(str(self.path))
        return self._conn

    def _open(self, target):
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, timeout=self._timeout,
                               check_same_thread=False,
                               isolation_level=None)   # autocommit
        try:
            conn.execute("PRAGMA busy_timeout = %d" % int(self._timeout * 1000))
            # Deleted dictations are overwritten, not just unlinked.
            conn.execute("PRAGMA secure_delete = ON")
            conn.create_function("htt_words", 1, word_count,
                                 deterministic=True)
            if target != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, raw TEXT NOT NULL, final TEXT NOT NULL,"
                " seconds REAL, corrected TEXT, corrected_ts REAL,"
                " words INTEGER)")
            conn.execute("CREATE INDEX IF NOT EXISTS history_ts ON history(ts)")
            self._migrate(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS history_totals ("
                " id INTEGER PRIMARY KEY CHECK (id = 1),"
                " dictations INTEGER NOT NULL DEFAULT 0,"
                " words INTEGER NOT NULL DEFAULT 0,"
                " seconds REAL NOT NULL DEFAULT 0,"
                " corrections INTEGER NOT NULL DEFAULT 0)")
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _migrate(conn):
        """Bring a database made by an older version up to date, in place.

        Version 1 (no user_version) had id, ts, raw, final, seconds. Version
        2 adds corrected / corrected_ts (NULL = never corrected) and words
        (NULL = not counted yet; stats() counts those on the fly). Only
        nullable columns are added, so an older copy of the app still
        running against the same file keeps working: it names its columns
        in every statement. Runs in an IMMEDIATE transaction so two
        processes opening the file at once cannot both add a column.
        """
        cols = {r[1] for r in conn.execute("PRAGMA table_info(history)")}
        if HISTORY_COLUMNS <= cols:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(history)")}
            for name, decl in (("corrected", "TEXT"),
                               ("corrected_ts", "REAL"),
                               ("words", "INTEGER")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE history ADD COLUMN {name} {decl}")
            conn.execute("PRAGMA user_version = %d" % HISTORY_SCHEMA)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        log.info("History file upgraded to version %d", HISTORY_SCHEMA)

    def _quarantine(self, exc):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        log.warning("History file %s is damaged (%s); moved to %s",
                    self.path, exc, dest.name)
        try:
            os.replace(self.path, dest)
        except OSError as move_exc:
            log.warning("Could not move the damaged history file: %s",
                        move_exc)
            return False
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.remove(str(self.path) + suffix)
            except OSError:
                pass
        return True

    def _fallback(self, exc):
        log.warning("History file %s cannot be used (%s); history is kept "
                    "in memory for this session only", self.path, exc)
        self._memory = True
        self._conn = self._open(":memory:")

    def _run(self, fn, default):
        """fn(conn) under the lock; any database error -> logged, default.
        A corrupt database is quarantined and fn retried once."""
        for attempt in (1, 2):
            with self._lock:
                if self._closed:
                    return default
                try:
                    return fn(self._connect())
                except sqlite3.DatabaseError as exc:
                    if _is_corrupt(exc) and attempt == 1 and not self._memory:
                        self._reset()
                        if not self._quarantine(exc):
                            self._fallback(exc)
                        continue
                    log.warning("History unavailable: %s", exc)
                    return default
                except OSError as exc:
                    log.warning("History unavailable: %s", exc)
                    return default
                except Exception as exc:
                    # Type only: a message may quote the text being stored.
                    log.error("History operation failed (%s)",
                                type(exc).__name__)
                    return default
        return default

    def _reset(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = None

    # -- writing --------------------------------------------------------------

    def record(self, raw, final, audio_seconds=None, ts=None):
        """Queue one dictation (on_result signature). Returns immediately.
        Does nothing when cfg["save_history"] is false. ts: its time
        (default now); at(ts) finds the row again."""
        if self._closed or not self.enabled:
            return
        try:
            seconds = (None if audio_seconds is None
                       else round(float(audio_seconds), 3))
        except (TypeError, ValueError):
            seconds = None
        item = (time.time() if ts is None else float(ts),
                "" if raw is None else str(raw),
                "" if final is None else str(final), seconds, self.limit)
        self._queue.put(item)
        self._ensure_writer()

    def _ensure_writer(self):
        # Not self._lock: the writer holds that while SQLite waits out a
        # busy file, and record() (on the Tk thread) must never wait.
        with self._writer_lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(
                    target=self._write_loop, name="history-writer",
                    daemon=True)
                self._writer.start()

    def _write_loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._insert(item)
            finally:
                self._queue.task_done()

    def _insert(self, item):
        ts, raw, final, seconds, limit = item

        def write(conn):
            trimmed = False
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("INSERT INTO history (ts, raw, final, seconds,"
                             " words) VALUES (?, ?, ?, ?, ?)",
                             (ts, raw, final, seconds, word_count(final)))
                # Rows past the retention cap still count in all-time stats.
                old = ("FROM history WHERE id NOT IN"
                       " (SELECT id FROM history ORDER BY id DESC LIMIT ?)")
                n, words, secs, fixes = conn.execute(
                    "SELECT COUNT(*),"
                    " COALESCE(SUM(COALESCE(words, htt_words(final))), 0),"
                    " COALESCE(SUM(seconds), 0), COUNT(corrected) " + old,
                    (limit,)).fetchone()
                if n:
                    trimmed = True
                    conn.execute(
                        "INSERT INTO history_totals (id, dictations, words,"
                        " seconds, corrections) VALUES (1, ?, ?, ?, ?)"
                        " ON CONFLICT(id) DO UPDATE SET"
                        " dictations = dictations + excluded.dictations,"
                        " words = words + excluded.words,"
                        " seconds = seconds + excluded.seconds,"
                        " corrections = corrections + excluded.corrections",
                        (n, words, secs, fixes))
                    conn.execute("DELETE " + old, (limit,))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            if trimmed:
                self._checkpoint(conn)      # dropped rows leave the WAL too
            return True
        # Background thread, so it can afford to wait out a lock (~8 s).
        for delay in WRITE_RETRY_DELAYS + (None,):
            if self._run(write, False):
                return True
            if delay is None or self._closed:
                break
            time.sleep(delay)
        log.warning("A dictation could not be saved to history")
        return False

    def flush(self, timeout=2.0):
        """Wait up to `timeout` s for queued records. True if all written."""
        deadline = time.time() + timeout
        while self._queue.unfinished_tasks:
            if time.time() >= deadline:
                return False
            time.sleep(0.005)
        return True

    # -- reading / deleting ---------------------------------------------------

    @staticmethod
    def _row(r):
        return {"id": r[0], "ts": r[1], "raw": r[2], "final": r[3],
                "seconds": r[4], "corrected": r[5], "corrected_ts": r[6],
                "text": r[3] if r[5] is None else r[5]}

    @classmethod
    def _rows(cls, cursor):
        return [cls._row(r) for r in cursor.fetchall()]

    def recent(self, n=50, corrected=None):
        """The n newest dictations, newest first. corrected=True: only
        corrected ones; False: only untouched ones; None: all."""
        self.flush(READ_FLUSH_WAIT)
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = 50
        where = {True: " WHERE corrected IS NOT NULL",
                 False: " WHERE corrected IS NULL"}.get(corrected, "")
        return self._run(lambda c: self._rows(c.execute(
            "SELECT " + _ROW_COLS + " FROM history" + where +
            " ORDER BY id DESC LIMIT ?", (n,))), [])

    def search(self, query, limit=200):
        """Dictations whose final, corrected or raw text contains `query`
        (case-insensitive for ASCII), newest first. An empty query =
        recent()."""
        query = "" if query is None else str(query).strip()
        if not query:
            return self.recent(limit)
        self.flush(READ_FLUSH_WAIT)
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", query) + "%"
        return self._run(lambda c: self._rows(c.execute(
            "SELECT " + _ROW_COLS + " FROM history"
            " WHERE final LIKE ? ESCAPE '\\' OR raw LIKE ? ESCAPE '\\'"
            " OR corrected LIKE ? ESCAPE '\\'"
            " ORDER BY id DESC LIMIT ?",
            (pattern, pattern, pattern, int(limit)))), [])

    def get(self, entry_id):
        """One dictation as a row dict, or None."""
        self.flush(READ_FLUSH_WAIT)
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            return None

        def read(conn):
            r = conn.execute("SELECT " + _ROW_COLS + " FROM history"
                             " WHERE id = ?", (entry_id,)).fetchone()
            return None if r is None else self._row(r)
        return self._run(read, None)

    def at(self, ts):
        """The dictation recorded with exactly this ts, or None."""
        self.flush(READ_FLUSH_WAIT)
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return None

        def read(conn):
            r = conn.execute("SELECT " + _ROW_COLS + " FROM history"
                             " WHERE ts = ? ORDER BY id DESC LIMIT 1",
                             (ts,)).fetchone()
            return None if r is None else self._row(r)
        return self._run(read, None)

    # -- corrections ----------------------------------------------------------

    def correct(self, entry_id, text):
        """Store the user's corrected version of a dictation; "final" (what
        was pasted) is kept unchanged so the two can be compared. Text equal
        to "final" removes the correction. Returns the updated row, or None
        (no such row, text not a non-blank string, or database unavailable).
        """
        self.flush(READ_FLUSH_WAIT)
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            return None
        if not isinstance(text, str) or not text.strip():
            return None

        def write(conn):
            changed = conn.execute(
                "UPDATE history SET"
                " corrected = CASE WHEN final = ?1 THEN NULL ELSE ?1 END,"
                " corrected_ts = CASE WHEN final = ?1 THEN NULL ELSE ?2 END"
                " WHERE id = ?3", (text, time.time(), entry_id)).rowcount > 0
            if changed:
                self._checkpoint(conn)     # the replaced text leaves the WAL
            r = conn.execute("SELECT " + _ROW_COLS + " FROM history"
                             " WHERE id = ?", (entry_id,)).fetchone()
            return None if r is None else self._row(r)
        return self._run(write, None)

    def uncorrect(self, entry_id):
        """Drop a dictation's correction (back to what was pasted). True if
        there was one."""
        self.flush(READ_FLUSH_WAIT)
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            return False
        def undo(conn):
            undone = conn.execute(
                "UPDATE history SET corrected = NULL, corrected_ts = NULL"
                " WHERE id = ? AND corrected IS NOT NULL",
                (entry_id,)).rowcount > 0
            if undone:
                self._checkpoint(conn)     # the undone text leaves the WAL
            return undone
        return self._run(undo, False)

    def corrections(self, limit=200):
        """Corrected dictations, most recently corrected first."""
        self.flush(READ_FLUSH_WAIT)
        return self._run(lambda c: self._rows(c.execute(
            "SELECT " + _ROW_COLS + " FROM history"
            " WHERE corrected IS NOT NULL"
            " ORDER BY corrected_ts DESC, id DESC LIMIT ?", (int(limit),))),
            [])

    # -- dashboard ------------------------------------------------------------

    def stats(self, now=None):
        """Counts for a dashboard; one indexed pass, cheap enough to call
        every few seconds (about a millisecond for 2000 rows).

        {"today": {"dictations", "words", "seconds"},   # since local midnight
         "week":  {...},                     # since Monday 00:00 local time
         "all":   {...},     # stored rows + rows dropped by history_limit
         "audio_seconds": float,             # == all["seconds"]
         "corrections": int,                 # corrected dictations, all time
         "learned": int,                     # items in cfg["learned"]
         "since": ts of the oldest stored dictation or None}
        Deleting a dictation (or clear()) removes it from the counts; rows
        dropped by the retention cap stay counted. On a database error the
        counts are zero."""
        self.flush(READ_FLUSH_WAIT)
        now = time.time() if now is None else float(now)
        day = _local_midnight(now)
        week = _local_midnight(now, monday=True)

        def read(conn):
            r = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(w), 0),"
                " COALESCE(SUM(seconds), 0), COUNT(corrected),"
                " COALESCE(SUM(ts >= ?1), 0),"
                " COALESCE(SUM(CASE WHEN ts >= ?1 THEN w END), 0),"
                " COALESCE(SUM(CASE WHEN ts >= ?1 THEN seconds END), 0),"
                " COALESCE(SUM(ts >= ?2), 0),"
                " COALESCE(SUM(CASE WHEN ts >= ?2 THEN w END), 0),"
                " COALESCE(SUM(CASE WHEN ts >= ?2 THEN seconds END), 0),"
                " MIN(ts)"
                " FROM (SELECT ts, seconds, corrected,"
                " COALESCE(words, htt_words(final)) AS w FROM history)",
                (day, week)).fetchone()
            t = conn.execute("SELECT dictations, words, seconds, corrections"
                             " FROM history_totals WHERE id = 1").fetchone()
            return r, t or (0, 0, 0.0, 0)
        got = self._run(read, None)
        r, t = got if got else ((0,) * 10 + (None,), (0, 0, 0.0, 0))
        all_secs = round(float(r[2]) + float(t[2]), 3)
        return {
            "today": {"dictations": r[4], "words": r[5],
                      "seconds": round(float(r[6]), 3)},
            "week": {"dictations": r[7], "words": r[8],
                     "seconds": round(float(r[9]), 3)},
            "all": {"dictations": r[0] + t[0], "words": r[1] + t[1],
                    "seconds": all_secs},
            "audio_seconds": all_secs,
            "corrections": r[3] + t[3],
            "learned": self._learned_count(),
            "since": r[10],
        }

    def _learned_count(self):
        items = self._current_cfg().get("learned")
        if not isinstance(items, list):
            return 0
        return sum(1 for i in items if isinstance(i, dict) and i.get("id"))

    def count(self):
        self.flush(READ_FLUSH_WAIT)
        return self._run(
            lambda c: c.execute("SELECT COUNT(*) FROM history").fetchone()[0],
            0)

    def delete(self, entry_id):
        """Delete one dictation. True if a row was deleted."""
        self.flush(READ_FLUSH_WAIT)
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            return False
        def delete(conn):
            gone = conn.execute("DELETE FROM history WHERE id = ?",
                                (entry_id,)).rowcount > 0
            if gone:
                self._checkpoint(conn)
            return gone
        return self._run(delete, False)

    @staticmethod
    def _checkpoint(conn, tries=None, pause=None, sleep=time.sleep):
        """Fold the write-ahead log into the database and empty it, so text
        that was deleted or replaced does not linger in the -wal file.
        Another connection that is reading holds the log back: SQLite then
        reports "busy" (it does not raise), so the answer is checked and the
        checkpoint retried a few times. True when the log was emptied; False
        (and one logged line) when it could not be, so the text stays in the
        log until the reader goes away."""
        tries = CHECKPOINT_TRIES if tries is None else tries
        pause = CHECKPOINT_PAUSE if pause is None else pause
        why = "another program has the file open"
        for attempt in range(max(1, tries)):
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if not row or not row[0]:
                    return True               # busy flag clear: emptied
            except sqlite3.Error as exc:
                why = type(exc).__name__
            if attempt + 1 < tries:
                sleep(pause)
        log.warning("History: the write-ahead log could not be emptied (%s); "
                    "deleted or replaced text may stay in it until then", why)
        return False

    def clear(self):
        """Delete every dictation, and the damaged-file copies
        (<name>.corrupt-*) an earlier repair left beside the database (they
        hold old dictations too). Falsy on failure; on success a Cleared:
        truthy, with .removed (names of the copies deleted) and .failed
        (names that could not be)."""
        self.flush(READ_FLUSH_WAIT)

        def wipe(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM history")
                conn.execute("DELETE FROM history_totals")
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            # Nothing of the deleted text may stay on disk: free pages are
            # rebuilt (VACUUM) and the write-ahead log is emptied.
            try:
                conn.execute("VACUUM")
            except sqlite3.Error as exc:
                log.warning("History: VACUUM failed (%s)", type(exc).__name__)
            self._checkpoint(conn)
            return True
        if not self._run(wipe, False):
            return False
        return self._remove_quarantined()

    def _remove_quarantined(self):
        removed, failed = [], []
        prefix = self.path.name + ".corrupt-"
        try:
            names = sorted(p.name for p in self.path.parent.iterdir()
                           if p.name.startswith(prefix))
        except OSError:
            names = []
        for name in names:
            try:
                os.remove(self.path.parent / name)
                removed.append(name)
            except OSError as exc:
                failed.append(name)
                log.warning("History: could not delete %s (%s)", name,
                            type(exc).__name__)
        if removed:
            log.info("History: deleted %d damaged-file copy(ies)",
                     len(removed))
        return Cleared(removed, failed)

    def close(self, timeout=2.0):
        """Write what is queued (up to `timeout` s) and close the file."""
        self.flush(timeout)
        if self._writer is not None and self._writer.is_alive():
            self._queue.put(None)
            self._writer.join(timeout)
        with self._lock:
            self._closed = True
            self._reset()


class Cleared:
    """What History.clear() returns on success: truthy, plus the names of
    the damaged-file copies it deleted (removed) or could not (failed)."""

    def __init__(self, removed=(), failed=()):
        self.removed, self.failed = list(removed), list(failed)

    def __bool__(self):
        return True

    def __repr__(self):
        return f"Cleared(removed={self.removed!r}, failed={self.failed!r})"


_COUNTED_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def word_count(text):
    """Words in a dictation, as the dashboard counts them ("don't" = 1)."""
    return len(_COUNTED_WORD.findall(text)) if isinstance(text, str) else 0


def _local_midnight(ts, monday=False):
    """Epoch seconds of local midnight on ts's day (or that week's Monday)."""
    import datetime
    d = datetime.datetime.fromtimestamp(ts).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if monday:
        d -= datetime.timedelta(days=d.weekday())
    return d.timestamp()


def _is_corrupt(exc):
    msg = str(exc).lower()
    return ("not a database" in msg or "malformed" in msg
            or "file is encrypted" in msg)


# -- CLI ----------------------------------------------------------------------

def _main(argv=None):
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Text rules and history")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_apply = sub.add_parser("apply", help="run apply() on some text")
    p_apply.add_argument("text")
    p_apply.add_argument("--config", help="config.json to use")
    p_vocab = sub.add_parser("vocab", help="print vocabulary_terms()")
    p_vocab.add_argument("--config")
    p_hist = sub.add_parser("history", help="show or edit history")
    p_hist.add_argument("--path", help="history file (default: the data folder)")
    p_hist.add_argument("--recent", type=int, default=20)
    p_hist.add_argument("--search")
    p_hist.add_argument("--delete", type=int, metavar="ID")
    p_hist.add_argument("--clear", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd in ("apply", "vocab"):
        import settings
        cfg = settings.load_config(args.config)
        if args.cmd == "apply":
            print(apply(args.text, cfg))
        else:
            for term in vocabulary_terms(cfg):
                print(term)
        return 0

    hist = History(args.path)
    try:
        if args.clear:
            print("cleared" if hist.clear() else "clear failed")
            return 0
        if args.delete is not None:
            print("deleted" if hist.delete(args.delete) else "not found")
            return 0
        rows = (hist.search(args.search) if args.search
                else hist.recent(args.recent))
        for r in rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
            print(f"{r['id']:>5}  {when}  {r['final']!r}")
        print(f"({len(rows)} shown, {hist.count()} stored in {hist.path})")
        return 0
    finally:
        hist.close()


if __name__ == "__main__":
    raise SystemExit(_main())
