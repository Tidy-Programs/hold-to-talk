"""Learning the user's vocabulary from corrections, and managing it by hand.

Everything here is pure: functions take the current config dict and return
new values; nothing is read from or written to disk, so every function may
be called from any thread. The caller saves what comes back, e.g.

    result = learn.apply_suggestions(app.cfg, accepted)
    settings.save_config(result["changes"])        # one atomic write
    app.cfg.update(result["changes"])

From a correction to a rule
    suggest(original, corrected, cfg=None, history_id=None) -> [suggestion]
        original  = the text that was pasted (History row "final"),
        corrected = what the user says it should have been.
    apply_suggestions(cfg, suggestions) -> {"changes", "applied", "skipped"}
    learn_from_correction(cfg, original, corrected, history_id=None)
        suggest() plus, when cfg["learning"]["auto_apply"] is on, applying
        the high-confidence ones: {"suggestions", "auto"}.
    learned_items(cfg) -> [record], undo(cfg, learned_id) -> {...}

By hand
    add_term / edit_term / remove_term, add_entry / edit_entry /
    remove_entry, check_term / check_entry (live validation),
    preview_entry (what an entry does to a sample sentence).
    Errors raise LearnError (a ValueError) with a message fit for the UI;
    non-fatal concerns come back as "warnings".

Rules (why they are this strict: a wrong dictionary entry silently changes
every later dictation, a missed one costs the user one more correction)
    * Both texts are split into words; punctuation at word edges and case
      are ignored for alignment (difflib on case-folded words).
    * Nothing is learned when the correction looks like an edit rather than
      a fix: more than MAX_REGIONS changed places, a changed place longer
      than MAX_SPAN words on either side, or more than MAX_CHANGED_SHARE of
      the words changed (always allowed: up to 3 changed words).
    * A changed place is learned only if it is a 1-3 word replacement whose
      two sides sound alike (letter / phonetic similarity >= SIMILAR_ONE for
      one word against one, SIMILAR_SPLIT when a word was split or merged,
      "zor vex" <-> "Zorvex"). Inserted or deleted words teach nothing, and
      a replacement right next to one is skipped (uncertain boundaries).
    * Same letters, different case/spacing ("github" -> "GitHub", "git hub"
      -> "GitHub") is a spelling fix; differing only by punctuation
      ("its" -> "it's", "e-mail" -> "email") or by sentence capitals is not.
    * Common-word protection: a rule never rewrites a common English word
      (COMMON_WORDS, plus its inflections). If the heard side is one common
      word, only the vocabulary term is suggested (kind "term"), with the
      rule offered as "manual_rule" for the user to add by hand. A phrase
      of 2+ words ("pie torch" -> "PyTorch") may be learned unless both
      sides are only common words ("a part" -> "apart").
    * If the heard phrase is still in the corrected text elsewhere, the user
      did not want it changed everywhere: no rule.
    * A vocabulary term (Whisper glossary) is suggested only for a wanted
      spelling containing an uncommon word: a name or term.

Where learned records live: cfg["learned"] (a list in config.json), next to
the vocabulary and dictionary they describe, so one save_config() writes the
entries and their record together (two files cannot be updated atomically),
and undo keeps working when history is turned off, cleared, pruned to its
retention limit, or kept in memory after a damaged file.
"""

import difflib
import hashlib
import re
import time
import uuid

import textrules

DEFAULTS = {
    # Apply "high" confidence suggestions without asking. Only spelling
    # fixes qualify (see _confidence); they cannot change which word is
    # written, only its case or spacing.
    "auto_apply": False,
}

SECTION = "learning"
MAX_SPAN = 3              # words on either side of a learnable replacement
MAX_REGIONS = 3           # changed places in one correction
MAX_CHANGED_SHARE = 0.34  # of the original's words, beyond the first 3
SIMILAR_ONE = 0.6         # one word replaced by one word
SIMILAR_SPLIT = 0.7       # word counts differ (split / merged words)
MAX_TERM_CHARS = 60
MAX_TERM_WORDS = 6

KINDS = ("misheard", "spelling", "term")


class LearnError(ValueError):
    """A request the UI should refuse; str(exc) is the message to show."""


def settings_of(cfg):
    """cfg["learning"] layered over DEFAULTS."""
    out = dict(DEFAULTS)
    sub = cfg.get(SECTION) if isinstance(cfg, dict) else None
    if isinstance(sub, dict):
        out.update(sub)
    out["auto_apply"] = bool(out.get("auto_apply"))
    return out


# -- words --------------------------------------------------------------------

_EDGE = ",.;:!?\"'()[]{}<>«»“”‘’…*_~`"
_CORE = re.compile("^[" + re.escape(_EDGE) + "]*(.*?)[" + re.escape(_EDGE)
                   + "]*$", re.S)
_LETTERS = re.compile(r"[^\W\d_]")
_ALNUM = re.compile(r"[^\W_]")


class _Tok:
    __slots__ = ("text", "start", "end", "key")

    def __init__(self, text, start, end):
        self.text, self.start, self.end = text, start, end
        self.key = text.replace("’", "'").casefold()


def _tokens(text):
    out = []
    for m in re.finditer(r"\S+", text):
        core = _CORE.match(m.group(0))
        word = core.group(1)
        if not _ALNUM.search(word):
            continue                      # "-", "--", "..." etc.
        start = m.start() + core.start(1)
        out.append(_Tok(word, start, start + len(word)))
    return out


def _alnum(s):
    return "".join(_ALNUM.findall(s)).casefold()


def is_common(word):
    """True for a common English word or a regular inflection of one
    ("servers", "walked", "running", "it's"). Errs towards True."""
    w = word.replace("’", "'").casefold().strip(_EDGE)
    if not w:
        return False
    if w in COMMON_WORDS:
        return True
    if any(ch.isdigit() for ch in w):
        return False
    if "'" in w:
        return is_common(w.split("'", 1)[0])
    if "-" in w:
        return all(is_common(p) for p in w.split("-") if p)
    for suffix, adds in (("ies", ("y",)), ("ied", ("y",)), ("es", ("", "e")),
                         ("s", ("",)), ("ed", ("", "e")), ("d", ("",)),
                         ("ing", ("", "e")), ("ers", ("", "e")),
                         ("er", ("", "e")), ("est", ("", "e")),
                         ("ly", ("",)), ("ness", ("",)), ("ful", ("",))):
        if not w.endswith(suffix) or len(w) - len(suffix) < 3:
            continue
        base = w[:-len(suffix)]
        for add in adds:
            if base + add in COMMON_WORDS:
                return True
        if len(base) >= 3 and base[-1] == base[-2] and base[:-1] in COMMON_WORDS:
            return True                     # running -> run
    return False


def _all_common(words):
    return all(is_common(w) for w in words)


def _phonetic(s):
    s = "".join(_LETTERS.findall(s)).casefold()
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "kw"), ("x", "ks"),
                 ("c", "k"), ("q", "k"), ("z", "s"), ("y", "i"),
                 ("dg", "j"), ("wr", "r"), ("kn", "n")):
        s = s.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", s)


def _skeleton(p):
    return p[:1] + re.sub(r"[aeiou]", "", p[1:])


def similarity(heard, wanted):
    """0..1: how alike two spellings look and roughly sound (best of plain
    letters, a crude phonetic key, and its consonant skeleton)."""
    a, b = _alnum(heard), _alnum(wanted)
    if not a or not b:
        return 0.0
    scores = [difflib.SequenceMatcher(None, a, b).ratio()]
    pa, pb = _phonetic(heard), _phonetic(wanted)
    if pa and pb:
        scores.append(difflib.SequenceMatcher(None, pa, pb).ratio())
        sa, sb = _skeleton(pa), _skeleton(pb)
        if len(sa) >= 4 and len(sb) >= 4:     # shorter ones match by chance
            scores.append(difflib.SequenceMatcher(None, sa, sb).ratio())
    return max(scores)


# -- suggestions --------------------------------------------------------------

def _context(text, toks, i, j, pad=4):
    """A short excerpt of `text` around tokens[i:j]."""
    if not toks:
        return text.strip()
    a = max(0, i - pad)
    b = min(len(toks), max(j, i + 1) + pad)
    start = toks[a].start if a < len(toks) else len(text)
    end = toks[b - 1].end if b > 0 else 0
    s = text[start:end]
    return ("…" if a > 0 else "") + s + ("…" if b < len(toks) else "")


def _phrase(text, toks):
    """The original spelling of a run of tokens (inner punctuation kept)."""
    return text[toks[0].start:toks[-1].end]


def _strip_possessive(heard, wanted):
    """"server's" -> "Zorvex's" teaches "server" -> "Zorvex"."""
    def poss(s):
        return len(s) > 2 and s[-2] in "'’" and s[-1] in "sS"
    if poss(heard) and poss(wanted):
        return heard[:-2], wanted[:-2]
    return heard, wanted


def _entries(cfg):
    entries = cfg.get("dictionary") if isinstance(cfg, dict) else None
    return entries if isinstance(entries, list) else []


def _vocab(cfg):
    vocab = cfg.get("vocabulary") if isinstance(cfg, dict) else None
    return vocab if isinstance(vocab, list) else []


def _find_entry(cfg, heard):
    """(index, entry) of the dictionary entry with this heard phrase."""
    key = textrules._phrase_key(heard)
    for i, e in enumerate(_entries(cfg)):
        if (isinstance(e, dict) and isinstance(e.get("heard"), str)
                and textrules._phrase_key(e["heard"].strip()) == key):
            return i, e
    return None, None


def _legacy(cfg, heard):
    legacy = cfg.get("replacements") if isinstance(cfg, dict) else None
    if not isinstance(legacy, dict):
        return None
    key = textrules._phrase_key(heard)
    for h, w in legacy.items():
        if isinstance(h, str) and textrules._phrase_key(h.strip()) == key:
            return w
    return None


def _has_term(cfg, term):
    t = term.strip().casefold()
    return any(isinstance(v, str) and v.strip().casefold() == t
               for v in _vocab(cfg))


def _left_unchanged(text, heard, wanted):
    """True if `heard` still occurs in the corrected `text` other than as
    the wanted spelling itself (the user left it alone there)."""
    words = [w for w in textrules._WORD_SPLIT.split(heard) if w]
    if not words:
        return False
    rx = re.compile(textrules._phrase_pattern(words), re.IGNORECASE)
    return any(m.group(0) != wanted for m in rx.finditer(text))


def _sid(kind, heard, wanted):
    raw = f"{kind}\x00{heard.casefold()}\x00{wanted}".encode("utf-8")
    return hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:12]


def _quote(s):
    return "“" + s + "”"


def _candidate(orig, corr, ot, ct, oi, oj, ci, cj, single_region):
    """Classify one aligned change; returns a partial suggestion or None."""
    h_toks, w_toks = ot[oi:oj], ct[ci:cj]
    heard, wanted = _phrase(orig, h_toks), _phrase(corr, w_toks)
    heard, wanted = _strip_possessive(heard, wanted)
    if not _LETTERS.search(heard) or not _LETTERS.search(wanted):
        return None                         # numbers, symbols: formatting
    if any(ch.isdigit() for ch in heard + wanted) and \
            _alnum(heard) != _alnum(wanted):
        return None                         # "2" vs "two" style edits
    if heard == wanted or "\n" in wanted:
        return None
    h_words = [t.text for t in h_toks]
    w_words = [t.text for t in w_toks]
    same_letters = _alnum(heard) == _alnum(wanted)
    if same_letters:
        # Only case or spacing differs. Learn it if the wanted form is a
        # term with its own casing; punctuation and capitals alone are not.
        joined = _alnum(wanted)
        if is_common(joined) or _all_common(w_words):
            return None
        if wanted == wanted.lower():
            return None                     # lowercasing / spacing only
        if (len(w_words) == 1 and len(h_words) == 1
                and wanted == heard[:1].upper() + heard[1:]
                and _at_start(orig, h_toks[0].start)
                and _at_start(corr, w_toks[0].start)):
            return None                     # sentence capital
        kind = "spelling"
    else:
        score = similarity(heard, wanted)
        need = SIMILAR_ONE if len(h_words) == len(w_words) == 1 \
            else SIMILAR_SPLIT
        if score < need:
            return None                     # a different word: an edit
        if _all_common(w_words) and _all_common(h_words):
            return None                     # "their" -> "there"
        kind = "misheard"
    heard_rule = heard.casefold() if kind == "misheard" else heard.lower()
    common_heard = len(h_words) == 1 and is_common(h_words[0])
    term = wanted if (not _all_common(w_words)
                      and len(wanted) <= MAX_TERM_CHARS) else None
    rule = {"heard": heard_rule, "wanted": wanted}
    manual = None
    if common_heard:
        manual, rule = rule, None
        if term is None:
            return None
        kind = "term"
    return {"kind": kind, "heard": heard_rule, "wanted": wanted,
            "rule": rule, "term": term, "manual_rule": manual,
            "score": 1.0 if same_letters else round(similarity(heard, wanted),
                                                     3),
            "before": _context(orig, ot, oi, oj),
            "after": _context(corr, ct, ci, cj),
            "single": single_region}


def _at_start(text, pos):
    before = text[:pos].rstrip()
    return not before or before[-1] in ".!?…\n"


def _regions(ot, ct):
    sm = difflib.SequenceMatcher(None, [t.key for t in ot],
                                 [t.key for t in ct], autojunk=False)
    return sm.get_opcodes()


def analyse(original, corrected):
    """(verdict, candidates): verdict is None when the correction is a fix
    worth looking at, or a short reason it is treated as an edit. Mainly for
    tests and diagnostics; suggest() is the API."""
    if not isinstance(original, str) or not isinstance(corrected, str):
        return "not text", []
    if not original.strip() or not corrected.strip():
        return "empty", []
    if original == corrected:
        return "unchanged", []
    ot, ct = _tokens(original), _tokens(corrected)
    if not ot or not ct:
        return "no words", []
    ops = _regions(ot, ct)
    changed = [op for op in ops if op[0] != "equal"]
    case_only = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for k in range(i2 - i1):
                if ot[i1 + k].text != ct[j1 + k].text:
                    case_only.append((i1 + k, j1 + k))
    if len(changed) > MAX_REGIONS:
        return "rewritten (too many changes)", []
    if any(max(i2 - i1, j2 - j1) > MAX_SPAN for _, i1, i2, j1, j2 in changed):
        return "rewritten (long change)", []
    n_changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in changed)
    if n_changed > max(3, MAX_CHANGED_SHARE * len(ot)):
        return "rewritten (too much changed)", []
    places = len(changed) + len(case_only)
    single = places == 1
    out = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag != "replace":
            continue
        neighbours = [ops[k][0] for k in (idx - 1, idx + 1)
                      if 0 <= k < len(ops)]
        if "insert" in neighbours or "delete" in neighbours:
            continue
        cand = _candidate(original, corrected, ot, ct, i1, i2, j1, j2, single)
        if cand:
            out.append(cand)
    for i, j in case_only:
        cand = _candidate(original, corrected, ot, ct, i, i + 1, j, j + 1,
                          single)
        if cand:
            out.append(cand)
    return None, out


def _looks_like_term(wanted):
    """True for a spelling that looks like a product or term: a capital
    inside the word (GitHub, TypeScript, iOS) or a digit (mp3). A plain
    capital (Kernel) or all capitals (URGENT) are only a change of case of an
    ordinary word."""
    if any(ch.isdigit() for ch in wanted):
        return True
    return (any(ch.isupper() for ch in wanted[1:])
            and any(ch.islower() for ch in wanted))


def _confidence(c, conflict):
    if (c["kind"] == "spelling" and c["single"] and conflict is None
            and c["rule"] is not None and _looks_like_term(c["wanted"])):
        return "high"
    return "normal"


def suggest(original, corrected, cfg=None, history_id=None):
    """What can be learned from correcting `original` (the pasted text) to
    `corrected`. Returns a list (often empty) of suggestion dicts:

        id          stable id (same change -> same id)
        kind        "misheard"  a different word was heard: rule + term
                    "spelling"  same letters, the term's casing/spacing
                    "term"      the heard word is common: vocabulary only
        heard, wanted       the change as seen
        dictionary  {"heard", "wanted"} entry to add, or None
        vocabulary  term to add to the glossary, or None
        replaces    the existing dictionary entry this one would replace
                    (same heard phrase, other spelling), or None
        manual_rule {"heard", "wanted"} the rule NOT suggested because it
                    rewrites a common word; for an explicit "add anyway"
                    (pass it to add_entry, which warns), or None
        confidence  "high" (eligible for auto-apply) or "normal"
        summary     one line: what will change
        reason      one line: why it is suggested
        warnings    list of strings
        before, after       excerpts of the two texts around the change
        history_id  passed through
    Already-known items are left out; `cfg` (optional) supplies what is
    known. Never raises for odd input.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    verdict, cands = analyse(original, corrected)
    if verdict:
        return []
    out, seen = [], set()
    for c in cands:
        rule, term = c["rule"], c["term"]
        warnings = []
        if rule and _left_unchanged(corrected, rule["heard"], rule["wanted"]):
            # They left another occurrence of it as it was.
            rule = None
            if c["kind"] == "spelling":
                continue
        replaces = None
        if rule:
            # The pasted text may already be a dictionary entry's output:
            # then that entry's heard phrase is what reaches the dictionary.
            for e in _entries(cfg):
                if (isinstance(e, dict) and isinstance(e.get("wanted"), str)
                        and e["wanted"].strip().casefold()
                        == rule["heard"].casefold()
                        and isinstance(e.get("heard"), str)):
                    rule = {"heard": e["heard"].strip(),
                            "wanted": rule["wanted"]}
                    break
            _, existing = _find_entry(cfg, rule["heard"])
            if existing is not None:
                if existing.get("wanted") == rule["wanted"]:
                    rule = None
                else:
                    replaces = {"heard": existing.get("heard"),
                                "wanted": existing.get("wanted")}
                    warnings.append(
                        f"Replaces your entry {_quote(str(existing.get('heard')))}"
                        f" → {_quote(str(existing.get('wanted')))}.")
        if rule:
            # The same warning as typing this entry by hand would give.
            warnings += _single_word_warnings(rule["heard"], rule["wanted"])
        if term and _has_term(cfg, term):
            term = None
        if rule is None and term is None:
            continue
        kind = c["kind"] if rule else "term"
        sid = _sid(kind, (rule or {}).get("heard", c["heard"]), c["wanted"])
        if sid in seen:
            continue
        seen.add(sid)
        manual = c["manual_rule"]
        if manual and _find_entry(cfg, manual["heard"])[1] is not None:
            manual = None
        out.append({
            "id": sid, "kind": kind, "heard": c["heard"], "wanted": c["wanted"],
            "dictionary": rule, "vocabulary": term, "replaces": replaces,
            "manual_rule": manual,
            "confidence": _confidence(dict(c, rule=rule, kind=kind), replaces),
            "summary": _summary(rule, term),
            "reason": _reason(c, kind, manual),
            "warnings": warnings,
            "before": c["before"], "after": c["after"],
            "history_id": history_id,
        })
    return out


def _summary(rule, term):
    parts = []
    if rule:
        parts.append(f"Write {_quote(rule['wanted'])} whenever "
                     f"{_quote(rule['heard'])} is heard")
    if term:
        parts.append(f"add {_quote(term)} to your vocabulary so speech "
                     "recognition expects it" if rule else
                     f"Add {_quote(term)} to your vocabulary so speech "
                     "recognition expects it")
    return " and ".join(parts) + "."


def _reason(c, kind, manual):
    why = (f"You changed {_quote(c['heard'])} to {_quote(c['wanted'])}"
           f" ({_quote(c['before'])} → {_quote(c['after'])}).")
    if kind == "spelling":
        why += " Same word, written differently."
    if manual:
        why += (f" No automatic replacement: {_quote(manual['heard'])} is an"
                " ordinary word, so replacing it everywhere could change"
                " other dictations. You can still add that rule by hand.")
    return why


def learn_from_correction(cfg, original, corrected, history_id=None,
                          now=None):
    """suggest(), then, if cfg["learning"]["auto_apply"] is on, apply the
    "high" confidence ones. Returns {"suggestions": [the rest],
    "auto": apply_suggestions() result or None}. Save auto["changes"]."""
    sugg = suggest(original, corrected, cfg, history_id)
    if not settings_of(cfg)["auto_apply"]:
        return {"suggestions": sugg, "auto": None}
    high = [s for s in sugg if s["confidence"] == "high"]
    rest = [s for s in sugg if s["confidence"] != "high"]
    auto = apply_suggestions(cfg, high, now=now, auto=True) if high else None
    return {"suggestions": rest, "auto": auto}


# -- applying and undoing -----------------------------------------------------

def _copy_list(value):
    return [dict(v) if isinstance(v, dict) else v for v in value] \
        if isinstance(value, list) else []


def learned_items(cfg):
    """Learned records, newest first, each with "active": True while what it
    added is still in the config. Record keys: id, ts, kind, heard, wanted,
    added_entry, added_term, replaced_entry, history_id, before, after,
    auto, summary."""
    items = cfg.get("learned") if isinstance(cfg, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for rec in items:
        if not isinstance(rec, dict) or not isinstance(rec.get("id"), str):
            continue
        rec = dict(rec)
        rec["before"] = rec["after"] = ""     # never shown, never stored
        entry, term = rec.get("added_entry"), rec.get("added_term")
        active = False
        if rec.get("inactive"):
            entry = term = None               # removed or edited by hand
        if isinstance(entry, dict):
            _, cur = _find_entry(cfg, str(entry.get("heard", "")))
            active = cur is not None and cur.get("wanted") == entry.get("wanted")
        if isinstance(term, str) and any(v == term for v in _vocab(cfg)):
            active = True
        rec["active"] = active
        out.append(rec)
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out


def apply_suggestions(cfg, suggestions, now=None, auto=False):
    """Add accepted suggestions to copies of the config's lists.

    Returns {"changes": {"vocabulary": [...], "dictionary": [...],
                         "learned": [...]},     # pass to save_config
             "applied": [learned record, ...],
             "skipped": [{"id", "reason"}, ...]}
    Duplicates are not added again. An existing entry for the same heard
    phrase is replaced only when the suggestion's "replaces" names exactly
    that entry (the UI showed the warning); otherwise the dictionary part is
    skipped with a reason. Never raises for malformed suggestions (skipped).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    vocab = _copy_list(cfg.get("vocabulary"))
    entries = _copy_list(cfg.get("dictionary"))
    learned = _copy_list(cfg.get("learned"))
    work = {"vocabulary": vocab, "dictionary": entries}
    applied, skipped = [], []
    now = time.time() if now is None else now
    for s in suggestions or []:
        sid = s.get("id") if isinstance(s, dict) else None
        if not isinstance(s, dict):
            skipped.append({"id": None, "reason": "not a suggestion"})
            continue
        rule, term = s.get("dictionary"), s.get("vocabulary")
        added_entry = replaced = added_term = None
        notes = []
        if isinstance(rule, dict) and _valid_rule(rule):
            rule = {"heard": rule["heard"].strip(), "wanted": rule["wanted"]}
            idx, existing = _find_entry(work, rule["heard"])
            if existing is None:
                entries.append(dict(rule))
                added_entry = rule
            elif existing.get("wanted") == rule["wanted"]:
                notes.append("dictionary entry already there")
            elif (isinstance(s.get("replaces"), dict)
                  and s["replaces"].get("heard") == existing.get("heard")
                  and s["replaces"].get("wanted") == existing.get("wanted")):
                entries[idx] = dict(rule)
                added_entry = rule
                replaced = {"heard": existing.get("heard"),
                            "wanted": existing.get("wanted")}
            else:
                notes.append(
                    f"you already have {_quote(str(existing.get('heard')))}"
                    f" → {_quote(str(existing.get('wanted')))}; left as is")
        if isinstance(term, str) and term.strip() and "\n" not in term:
            term = term.strip()
            if _has_term(work, term):
                notes.append("already in your vocabulary")
            else:
                vocab.append(term)
                added_term = term
        if added_entry is None and added_term is None:
            skipped.append({"id": sid, "reason": "; ".join(notes)
                            or "nothing to add"})
            continue
        rec = {"id": uuid.uuid4().hex[:12], "ts": now,
               "kind": s.get("kind") if s.get("kind") in KINDS else "term",
               "heard": str(s.get("heard", "")), "wanted": str(s.get("wanted", "")),
               "added_entry": added_entry, "added_term": added_term,
               "replaced_entry": replaced, "history_id": s.get("history_id"),
               "before": "", "after": "",     # kept for compatibility
               "auto": bool(auto), "summary": _summary(added_entry, added_term)}
        learned.append(rec)
        applied.append(rec)
    return {"changes": {"vocabulary": vocab, "dictionary": entries,
                        "learned": learned},
            "applied": applied, "skipped": skipped}


def _valid_rule(rule):
    return (isinstance(rule.get("heard"), str) and rule["heard"].strip()
            and isinstance(rule.get("wanted"), str) and rule["wanted"].strip())


def undo(cfg, learned_id):
    """Remove exactly what learned item `learned_id` added (and put back an
    entry it replaced). Something the user has edited since is left alone
    and reported in "warnings".

    Returns {"changes": {"vocabulary", "dictionary", "learned"},
             "removed_entry": dict|None, "removed_term": str|None,
             "restored_entry": dict|None, "warnings": [str]}
    Raises LearnError if there is no such learned item.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    learned = _copy_list(cfg.get("learned"))
    pos = next((i for i, r in enumerate(learned)
                if isinstance(r, dict) and r.get("id") == learned_id), None)
    if pos is None:
        raise LearnError("That learned item no longer exists.")
    if learned[pos].get("inactive"):
        return {"changes": {"vocabulary": _copy_list(cfg.get("vocabulary")),
                            "dictionary": _copy_list(cfg.get("dictionary")),
                            "learned": learned},
                "removed_entry": None, "removed_term": None,
                "restored_entry": None,
                "warnings": ["You already removed or changed what this "
                             "learned item added by hand, so nothing was "
                             "undone."]}
    rec = learned.pop(pos)
    vocab = _copy_list(cfg.get("vocabulary"))
    entries = _copy_list(cfg.get("dictionary"))
    warnings = []
    removed_entry = removed_term = restored = None
    entry = rec.get("added_entry")
    if isinstance(entry, dict):
        idx, cur = _find_entry({"dictionary": entries},
                               str(entry.get("heard", "")))
        if cur is None:
            warnings.append(f"The entry for {_quote(str(entry.get('heard')))}"
                            " was already removed.")
        elif cur.get("wanted") != entry.get("wanted"):
            warnings.append(f"You changed the entry for "
                            f"{_quote(str(entry.get('heard')))} since; it was"
                            " left as is.")
        else:
            removed_entry = entries.pop(idx)
            old = rec.get("replaced_entry")
            if isinstance(old, dict) and _valid_rule(old):
                entries.insert(idx, dict(old))
                restored = dict(old)
    term = rec.get("added_term")
    if isinstance(term, str):
        if term in vocab:
            vocab.remove(term)
            removed_term = term
        else:
            warnings.append(f"{_quote(term)} was no longer in your vocabulary"
                            " (removed or edited).")
    return {"changes": {"vocabulary": vocab, "dictionary": entries,
                        "learned": learned},
            "removed_entry": removed_entry, "removed_term": removed_term,
            "restored_entry": restored, "warnings": warnings}


def scrub_excerpts(learned):
    """(records, changed): `learned` with the "before"/"after" excerpts of
    dictations that older versions stored blanked (the keys stay, empty)."""
    if not isinstance(learned, list):
        return learned, False
    out, changed = [], False
    for rec in learned:
        if isinstance(rec, dict) and (rec.get("before") or rec.get("after")):
            rec = dict(rec, before="", after="")
            changed = True
        out.append(rec)
    return out, changed


# -- manual management --------------------------------------------------------

def check_term(cfg, term, editing=None):
    """Validate a vocabulary term. Returns warnings; raises LearnError.
    editing: the term being edited (it does not count as a duplicate)."""
    if not isinstance(term, str) or not term.strip():
        raise LearnError("Type a word or name to add.")
    term = term.strip()
    if "\n" in term or "\r" in term:
        raise LearnError("A vocabulary term must fit on one line.")
    if not _ALNUM.search(term):
        raise LearnError("A vocabulary term needs at least one letter or "
                         "digit.")
    if len(term) > 100:
        raise LearnError("That is too long for a vocabulary term (keep it "
                         "under 100 characters).")
    skip = editing.strip().casefold() if isinstance(editing, str) else None
    for v in _vocab(cfg):
        if (isinstance(v, str) and v.strip().casefold() == term.casefold()
                and v.strip().casefold() != skip):
            raise LearnError(f"{_quote(v.strip())} is already in your "
                             "vocabulary.")
    warnings = []
    words = term.split()
    if len(term) > MAX_TERM_CHARS or len(words) > MAX_TERM_WORDS:
        warnings.append("Long terms use up the speech model's limited "
                        "glossary space; single names and terms work best.")
    if _all_common(words):
        warnings.append(f"{_quote(term)} is an ordinary word; the speech "
                        "model already knows it, so adding it rarely helps.")
    return warnings


def add_term(cfg, term):
    """{"changes": {"vocabulary": [...]}, "warnings": [...]}; LearnError."""
    warnings = check_term(cfg, term)
    vocab = _copy_list(cfg.get("vocabulary") if isinstance(cfg, dict)
                       else None)
    vocab.append(term.strip())
    return {"changes": {"vocabulary": vocab}, "warnings": warnings}


def _retire_learned(cfg, entry=None, term=None):
    """cfg["learned"] with the records that added this dictionary entry (a
    heard phrase) or this vocabulary term marked "inactive", so a later undo
    cannot remove what the user adds back by hand. {"learned": [...]} when
    something changed, else {}."""
    learned = _copy_list(cfg.get("learned") if isinstance(cfg, dict) else None)
    changed = False
    for rec in learned:
        if not isinstance(rec, dict) or rec.get("inactive"):
            continue
        added = rec.get("added_entry")
        hit = False
        if entry is not None and isinstance(added, dict):
            hit = (textrules._phrase_key(str(added.get("heard", "")).strip())
                   == textrules._phrase_key(entry.strip()))
        if term is not None and isinstance(rec.get("added_term"), str):
            hit = hit or (rec["added_term"].strip().casefold()
                          == term.strip().casefold())
        if hit:
            rec["inactive"] = True
            changed = True
    return {"learned": learned} if changed else {}


def edit_term(cfg, old, new):
    """Replace vocabulary term `old` (case-insensitive) with `new`, in place.
    A learned record that added `old` is marked inactive."""
    vocab = _copy_list(cfg.get("vocabulary") if isinstance(cfg, dict)
                       else None)
    idx = _term_index(vocab, old)
    warnings = check_term(cfg, new, editing=old)
    if new.strip() != vocab[idx].strip():
        changes = _retire_learned(cfg, term=vocab[idx])
    else:
        changes = {}
    vocab[idx] = new.strip()
    return {"changes": {"vocabulary": vocab, **changes}, "warnings": warnings}


def remove_term(cfg, term):
    vocab = _copy_list(cfg.get("vocabulary") if isinstance(cfg, dict)
                       else None)
    gone = vocab.pop(_term_index(vocab, term))
    return {"changes": {"vocabulary": vocab,
                        **_retire_learned(cfg, term=gone)}, "warnings": []}


def _term_index(vocab, term):
    t = term.strip().casefold() if isinstance(term, str) else None
    for i, v in enumerate(vocab):
        if isinstance(v, str) and v.strip().casefold() == t:
            return i
    raise LearnError(f"{_quote(str(term))} is not in your vocabulary.")


def _single_word_warnings(heard, wanted):
    """The warning for a dictionary entry whose heard side is one common or
    lowercase word: every such word, everywhere, will be rewritten. Shared
    by check_entry (typed by hand) and suggest (learned), so both say it."""
    heard = heard.strip()
    words = [w for w in textrules._WORD_SPLIT.split(heard) if w]
    if len(words) != 1:
        return []
    one_word = words[0].isalpha() and words[0] == words[0].lower()
    if not (is_common(words[0]) or one_word):
        return []
    what = ("an ordinary English word" if is_common(words[0])
            else "a single lowercase word")
    return [f"{_quote(heard)} is {what}: every {_quote(heard)} in every "
            f"dictation will become {_quote(wanted.strip())}."]


def check_entry(cfg, heard, wanted, editing=None):
    """Validate a dictionary entry. Returns warnings; raises LearnError.
    editing: the heard phrase of the entry being edited."""
    if not isinstance(heard, str) or not heard.strip():
        raise LearnError("Type what speech recognition writes (the "
                         "“heard” side).")
    if not isinstance(wanted, str) or not wanted.strip():
        raise LearnError("Type what you want written instead.")
    heard = heard.strip()
    if "\n" in heard or "\r" in heard:
        raise LearnError("The heard side must fit on one line.")
    if not _ALNUM.search(heard):
        raise LearnError("The heard side needs at least one letter or digit.")
    if heard == wanted.strip():
        raise LearnError("Heard and wanted are the same, so this entry would "
                         "change nothing.")
    key = textrules._phrase_key(heard)
    skip = textrules._phrase_key(editing.strip()) \
        if isinstance(editing, str) else None
    _, existing = _find_entry(cfg, heard)
    if existing is not None and key != skip:
        raise LearnError(
            f"{_quote(str(existing.get('heard')).strip())} already has an "
            f"entry (→ {_quote(str(existing.get('wanted')))}). Edit that "
            "one instead.")
    warnings = []
    words = [w for w in textrules._WORD_SPLIT.split(heard) if w]
    warnings += _single_word_warnings(heard, wanted)
    if len(_alnum(heard)) <= 2 and not (len(words) == 1
                                        and is_common(words[0])):
        warnings.append(f"{_quote(heard)} is very short and may match where "
                        "you do not expect it.")
    legacy = _legacy(cfg, heard)
    if legacy is not None:
        warnings.append(f"This overrides an older replacement "
                        f"{_quote(heard)} → {_quote(str(legacy))}.")
    return warnings


def add_entry(cfg, heard, wanted):
    """{"changes": {"dictionary": [...]}, "warnings": [...]}; LearnError."""
    warnings = check_entry(cfg, heard, wanted)
    entries = _copy_list(cfg.get("dictionary") if isinstance(cfg, dict)
                         else None)
    entries.append({"heard": heard.strip(), "wanted": wanted})
    return {"changes": {"dictionary": entries}, "warnings": warnings}


def edit_entry(cfg, old_heard, heard, wanted):
    """Replace the entry for `old_heard` with heard/wanted, in place."""
    entries = _copy_list(cfg.get("dictionary") if isinstance(cfg, dict)
                         else None)
    idx, _ = _find_entry({"dictionary": entries}, str(old_heard or ""))
    if idx is None:
        raise LearnError(f"There is no entry for {_quote(str(old_heard))}.")
    warnings = check_entry(cfg, heard, wanted, editing=old_heard)
    before = entries[idx]
    changes = {}
    if before != {"heard": heard.strip(), "wanted": wanted}:
        changes = _retire_learned(cfg, entry=str(before.get("heard", "")))
    entries[idx] = {"heard": heard.strip(), "wanted": wanted}
    return {"changes": {"dictionary": entries, **changes},
            "warnings": warnings}


def remove_entry(cfg, heard):
    entries = _copy_list(cfg.get("dictionary") if isinstance(cfg, dict)
                         else None)
    idx, _ = _find_entry({"dictionary": entries}, str(heard or ""))
    if idx is None:
        raise LearnError(f"There is no entry for {_quote(str(heard))}.")
    gone = entries.pop(idx)
    return {"changes": {"dictionary": entries,
                        **_retire_learned(cfg, entry=str(gone.get("heard", "")))},
            "warnings": []}


def preview_entry(heard, wanted, sample, cfg=None):
    """What an entry would do to `sample`. Never raises.

    Returns {"text": sample with only this entry applied,
             "matches": how many places it changed,
             "full": sample through the whole text-rules stage (snippets,
                     commands, dictionary) with this entry added or
                     replacing the one for the same heard phrase; equals
                     "text" when cfg is None}
    """
    sample = sample if isinstance(sample, str) else ""
    if (not isinstance(heard, str) or not heard.strip()
            or not isinstance(wanted, str) or not wanted.strip()):
        return {"text": sample, "matches": 0, "full": sample}
    one = {"dictionary": [{"heard": heard.strip(), "wanted": wanted}]}
    compiled = textrules._compile_dictionary(((heard.strip(), wanted),))
    matches = len(compiled[0].findall(sample)) if compiled else 0
    text = textrules.apply_dictionary(sample, one)
    full = text
    if isinstance(cfg, dict):
        entries = [e for e in _copy_list(cfg.get("dictionary"))
                   if not (isinstance(e, dict) and isinstance(e.get("heard"), str)
                           and textrules._phrase_key(e["heard"].strip())
                           == textrules._phrase_key(heard.strip()))]
        full = textrules.apply(sample, dict(cfg, dictionary=[
            {"heard": heard.strip(), "wanted": wanted}] + entries))
    return {"text": text, "matches": matches, "full": full}


# -- common words -------------------------------------------------------------

# The core of everyday written and spoken English: function words, the most
# frequent nouns, verbs, adjectives and adverbs, numbers, days and months,
# plus everyday words that names are often mis-heard as. Inflections are
# covered by is_common(). Erring towards "common" is the safe direction: it
# can only cost a suggestion, never damage a dictation.
COMMON_WORDS = frozenset("""
a about above across act action active actually add added address admit
adult affect after afternoon again against age ago agree ahead air all allow
almost alone along already also although always am among amount an and
anger angle animal animation another answer any anyone anything anyway
anywhere apart app apple apply appear area argue arm army around arrive art
article as ask at attack attention aunt author available avoid away awesome
baby back bad bag ball band bank bar base basic basically basis bat battle
be bear beat beautiful beauty became because become bed bee been beer
before began begin behind being believe bell belong below belt bench bend
best better between beyond big bike bill bird birth bit bite black blade
blame blank block blood blow blue board boat body boil bomb bone book boom
boost boot border born boss both bother bottle bottom bought bowl box boy
brain branch brand bread break breakfast breath bridge brief bright bring
broad broke broken brother brought brown buck budget bug build building
built bunch burn bus business busy but butter button buy by cake call calm
came camera camp campaign can cancel candy cap capital car card care career
careful carry case cash cat catch caught cause cell center central century
certain certainly chain chair challenge chance change channel chapter
charge chart chat cheap check cheese chicken chief child children choice
choose chose church circle city claim class clean clear clearly click
client climb clock close closed cloud club coach coast coat code coffee cold
collect college color column come comfortable comment common community
company compare complete computer concern condition conference confirm
consider contact contain content context continue contract control cook
cool copy corner correct cost could council count country couple course
court cousin cover cow crash crazy cream create credit crew crime cross
crowd cry cup current customer cut cute cycle dad daily damage dance danger
dark data date daughter day dead deal dear death debate decade decide
decision deep default define degree delay delete deliver demand depend
describe design desk detail determine develop device die diet difference
different difficult dig dinner direct direction director dirty discover
discuss discussion disease dish display distance do doctor document does
dog doing dollar domain done door double doubt down draft drag draw dream
dress drink drive driver drop drug dry due during dust duty each ear early
earn earth easily east easy eat economy edge edit effect effort egg eight
either election else email employee end enemy energy engine enjoy enough
enter entire entry environment equal error escape especially even evening
event ever every everybody everyone everything evidence exact exactly
example except exchange excited exercise exist expect expensive experience
expert explain express extra eye face fact factor fail fair fall false
family famous fan far farm fast fat father fault favorite fear feature
feed feel feeling fellow felt female few field fifteen fifth fifty fight
figure file fill film final finally find fine finger finish fire firm first
fish fit five fix flag flat flight floor flow flower fly focus folder follow
food foot for force foreign forest forget form format forward found four
fourth free fresh friday friend from front fruit full fun function fund
funny future gain game gap garden gas gate gather gave general generally
get gift girl give glad glass go goal god going gold golden gone gonna good
got government grab grade grand grant great green grew ground group grow
growth guard guess guest guide gun guy had hair half hall hand handle hang
happen happy hard has hat hate have he head health hear heard heart heat
heavy held hell hello help her here herself hey hi hide high hill him
himself hire his history hit hold hole holiday home honest hope horse
hospital host hot hotel hour house how however huge human hundred hung
hunt hurt husband i ice idea if image imagine impact important improve in
inch include income increase indeed index industry info information inside
instead interest internet into invite involve iron is island issue it item
its itself jacket job join joke journey judge juice jump june just keep key
kick kid kill kind king kitchen knee knew know knowledge lab labor lack
lady land language large last late later laugh launch law lawyer lay layer
lead leader learn least leave left leg legal less lesson let letter level
lie life lift light like likely limit line link list listen little live
load loan local lock long look loose lose loss lost lot loud love low luck
lunch machine mad made magic mail main major make male man manage manager
many map march mark market marriage married master match matter may maybe
me meal mean meaning meant measure meat media medical meet meeting member
memory mention menu mess message met method middle might mile milk mind
mine minute miss mission mistake mix mobile model modern mom moment monday
money monitor month mood moon more morning most mother motion mount mouse
mouth move movie much music must my myself name narrow nation natural
nature near nearly neck need negative neither nerve network never new news
next nice night nine no nobody none noon nor normal north nose not note
nothing notice now number nurse object obviously occur of off offer office
officer often oh oil ok okay old on once one online only open operation
opinion option or orange order other our ourselves out outside over own
owner pace pack package page paid pain paint pair panel paper parent park
part partner party pass past patch path patient pattern pause pay peace
people pepper per perfect perhaps period person phone photo pick picture
piece pie pig pin pink place plan plane plant plate platform play player
please plenty plus pocket point police policy pool poor pop popular port
position positive possible post pot pound power practice prepare present
president press pretty price print private probably problem process produce
product profile program project promise proof property protect proud prove
provide public pull purpose push put quality quarter queen question quick
quickly quiet quite race radio rain raise ran range rate rather reach read
ready real really reason receive recent recently record red reduce release
remain remember remove repeat reply report request require research respect
response rest result return review rich ride right ring rise risk river
road rock role roll roof room root rose round route row rule run rush sad
safe said sale salt same sat saturday save saw say scale scene school
science score screen sea search season seat second secret section see seem
seen select self sell send sense sent separate serious serve server service
session set settle seven several shall shape share sharp she sheet shift
ship shirt shoe shoot shop short shot should shoulder shout show shut sick
side sign signal silence silly simple simply since sing single sir sister
sit site situation six size skill skin sky sleep slide slightly slow small
smart smell smile snow so social soft software sold some somebody someone
something sometimes son song soon sorry sort sound soup source south space
speak special speech speed spend spent spot spring staff stage stand star
start state station stay step stick still stock stone stood stop store
storm story straight strange stream street strong student studio study
stuff style subject success such sudden suddenly sugar suggest suit summer
sun sunday super supply support suppose sure surface surprise sweet system
table take talk tall task taste tax tea teach teacher team tear tech
technology tell ten tend term terrible test text than thank thanks that the
their them theme themselves then there these they thing think third thirty
this those though thought thousand three through throw thursday thus ticket
tie till time tiny tip tired title to today together told tomorrow tone too
took tool top topic total touch tough toward town track trade traffic train
travel treat tree trial trip trouble truck true trust truth try tuesday
turn twelve twenty twice two type uncle under understand unit until up
update upon upset us use used useful user usual usually valley value van
various version very video view village visit voice vote wait wake walk
wall want war warm was wash watch water wave way we weak wear weather web
website wedding wednesday week weekend weight welcome well went were west
what whatever wheel when where whether which while white who whole whom
whose why wide wife will win wind window wine winter wish with within
without woman women won wonder wonderful wood word work worker world worry
worse worst worth would write writer wrong wrote yard yeah year yellow yes
yesterday yet you young your yours yourself zero
january february april july august september october november december
eleven thirteen fourteen sixteen seventeen eighteen nineteen forty sixty
seventy eighty ninety million billion
able absolutely accept access according account addition admin advance
advice afraid agent agenda alarm alert alive amazing angry annual apartment
approach approve arrange arrival assume attach attempt audience audio
average award aware awful background balance bath beach bean bedroom benefit
bet bin birthday blind blog bloom boring borrow bounce brave breeze brick
brush bubble bucket bulb bump burger cabin cable calendar cancer capable
captain carbon carpet cart castle casual ceiling celebrate champion charity
chase cheer chip chocolate chunk cinema citizen civil clever cliff climate
clip coin collapse colleague combine comfort command commit compete
complain concept confuse connect constant cookie cope core corn costume
cotton couch cough crack craft crisp crop crown cruise crush cure curious
curtain custom dash database deadline debt decent deck deer delight demo
dentist deny deploy deposit desert deserve desire dessert destroy diamond
dirt disaster disk doll donate dot dozen drama drawer drawing drill drum
duck dumb dump eager ease eastern edition elbow elder electric element
elephant elevator emergency empty encourage engineer enormous episode
equipment estimate evil exam excuse exit expand export extend fabric fade
faith fake fancy fantastic farmer fashion fee fence festival fever fiction
filter finance fitness flash flavor fleet flip float flood fold folk font
fork fortune forum frame freeze fridge frog frozen fry fuel gallery garage
garbage gear gene genius gentle ghost giant ginger glove glue goat grape
graph grass grave gray grey grid grill grocery guitar habit hammer hardware
harm harvest headline heaven height helpful hero hidden highlight hint hip
hobby holder honey hook horror hub humor hunger hurry ignore illegal
impress inbox indoor inner input insect install interview invoice jar jazz
jelly jet jewel joint junior jungle keen kettle keyboard kingdom kiss kit
kite knife knock label ladder lake lamp laptop laser lawn lazy leaf lean
league leather lecture lemon lend lens library license lid lion liquid
lizard lobby logo lonely loop lord lottery lounge luggage lyric magazine
magnet maid manner manual maple marble margin marine mask mass meadow merge
mesh metal midnight mild minor mirror modest monkey monster mortgage motor
mountain mud mug muscle museum mushroom mystery nail napkin neat needle
nephew nest net niece noise noodle novel nut oak ocean odd olive onion
opera orbit orchard organ oven owl oxygen pad palace palm pan pants parade
parcel parrot paste patio peach peak pear pen pencil penny pet piano pillow
pilot pine pipe pirate pizza planet plastic pleasant plug poem poet poison
pole polish pony porch portal poster potato pottery powder prayer prince
printer prison prize profit prompt pump pumpkin punch puppy purple purse
puzzle rabbit rack rail rainbow ranch rapid rare rat raw razor recipe reef
refund relax remote rent repair rescue resort retail rhythm ribbon rice
riddle rifle ripple rival robot rocket rope rough rug ruler rumor sack
saddle sail salad salmon sand sandwich sauce sausage scarf scissors scooter
scout scrap scratch script sector seed shadow shark shelf shell shelter
shield shiny shower shrimp sigh silk silver sink skate sketch ski skirt
slack sleeve slice slim slip slot smoke snack snake sock soda sofa soil
solar soldier solid solve spare spark spider spike spin spoon sport spray
square squad stable stadium stair stamp steak steam steel stem stew sticker
stomach stove strap straw strike string stripe stuck submit subway suitcase
sum sunny surf swamp swap sweater swim swing switch sword symbol syrup tab
tablet tag tail tank tap tape target taxi teen temple tennis tent terminal
thread thumb thunder tide tiger tile timber toast toe toilet tomato tongue
tooth torch tour towel tower toy tractor trail trailer trap tray treasure
trend tribe trick trophy tube tune tunnel turkey turtle twin umbrella
uniform universe upload upper urban vacuum valid vault velvet vendor venue
verb verse vest victim vinyl virus vision vitamin volcano wagon waist wallet
wander warrior wasp waste wax weapon weird whale wheat whip whisper wild
wing wire wise witch wolf wool worm wrap wrist yoga youth zone zoo
react swift rust ruby zoom chrome node angular notion flutter rails shell
pandas amazon java mercury jet bridge sage oracle hive jupiter mars saturn
venus apex echo alpha beta delta gamma omega nova pixel cloud fiber
""".split())
