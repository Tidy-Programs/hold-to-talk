"""Text post-processing: raw transcript -> text to paste.

STAGES is an ordered list of functions `(text: str, cfg: dict) -> str`:

    normalize       filler words, whitespace, first capital (always cheap;
                    first, so short utterances the LLM skips still get it)
    cleanup.clean   local-LLM cleanup via Ollama (skipped when disabled,
                    unreachable, too short/long, or too slow)
    textrules.apply snippets, spoken formatting, dictionary (last, so the
                    LLM can never rewrite a snippet or undo a spelling)

A stage that raises or returns a non-string is logged and skipped; the text
from before that stage carries on, so the user's words are never lost to a
broken stage.
"""

import inspect
import logging
import re
import time

import cleanup
import textrules

log = logging.getLogger("holdtotalk.pipeline")

# A filler word with an optional comma before and after it:
# "Um, this" -> "this", "I, uh, think" -> "I think", "it, um." -> "it."
_FILLERS = re.compile(
    r",?[ \t]*\b(?:u+m+|u+h+m*|e+r+m+|h+m+)\b,?", re.IGNORECASE)


def effective_language(language, cfg):
    """The language to treat a dictation as: `language` if given, else
    cfg["language"], else None (not known)."""
    if language is None and isinstance(cfg, dict):
        language = cfg.get("language")
    if isinstance(language, str) and language.strip():
        return language.strip().lower().replace("_", "-")
    return None


def is_english(language, cfg=None):
    """True for English or an unknown language (today's behaviour)."""
    lang = effective_language(language, cfg)
    return lang is None or lang.split("-")[0] == "en"


def normalize(text, cfg, language=None):
    """Filler removal (if cfg['remove_fillers'] and the language is English
    or unknown), whitespace cleanup, and a capital first letter. The English
    filler list is not applied to other languages: "um" means "at" in
    German."""
    if cfg.get("remove_fillers", True) and is_english(language, cfg):
        text = _FILLERS.sub("", text)
        # A filler that started the text may leave stray punctuation behind.
        text = re.sub(r"^[\s,.;:]+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = text.strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text

STAGES = [normalize, cleanup.clean, textrules.apply]


def stage_name(stage):
    module = getattr(stage, "__module__", "") or ""
    name = getattr(stage, "__name__", repr(stage))
    return f"{module}.{name}" if module not in ("", __name__) else name


class TraceEntry(tuple):
    """One trace line: (stage name, text after it, seconds), so existing
    `for name, text, secs in trace` code keeps working, plus attributes (and
    dict-style access) `failed` (True when the stage raised or returned
    something that is not text and was skipped) and `error` (a short
    "ExceptionClass: message" with dictated words removed, or "")."""

    def __new__(cls, name, text, seconds, failed=False, error=""):
        self = super().__new__(cls, (name, text, seconds))
        self.failed, self.error = bool(failed), error
        return self

    _FIELDS = ("stage", "text", "seconds")

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.as_dict()[key]
        return super().__getitem__(key)

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def as_dict(self):
        out = dict(zip(self._FIELDS, tuple(self)))
        out["failed"] = self.failed
        if self.failed:
            out["error"] = self.error
        return out


def _safe_error(exc, text, limit=120):
    """"ClassName: message" for a trace, with every word of the dictated text
    that occurs in the message replaced by an ellipsis, cut to `limit`."""
    msg = " ".join(str(exc).split())
    for word in sorted(set(re.findall(r"\w{3,}", text or "")), key=len,
                       reverse=True):
        msg = re.sub(re.escape(word), "\u2026", msg, flags=re.IGNORECASE)
    msg = msg[:limit]
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


_accepts_cache = {}


def _accepts_language(stage):
    """True if `stage` can take a `language` keyword (stages written as
    `(text, cfg)` are called without it)."""
    key = getattr(stage, "__code__", None) or stage
    try:
        return _accepts_cache[key]
    except (KeyError, TypeError):
        pass
    try:
        params = inspect.signature(stage).parameters.values()
        ok = any(p.name == "language" or p.kind is p.VAR_KEYWORD
                 for p in params)
    except (TypeError, ValueError):
        ok = False
    try:
        _accepts_cache[key] = ok
    except TypeError:
        pass
    return ok


def run(raw, cfg, trace=None, language=None, stages=None):
    """Apply every stage in STAGES (or in `stages`), in order, skipping any
    that fail.

    language: the language of the dictation ("en", "de", ...; e.g. the
    engine's last_language). When it is None, cfg["language"] is used, and
    when that is None too the text is treated as English (today's
    behaviour). It is passed on to stages that accept a `language` keyword.

    trace: optional list; a TraceEntry (stage name, text after it, seconds,
    plus .failed / .error) is appended for each stage (the text is unchanged
    for a skipped stage).
    """
    text = raw
    lang = effective_language(language, cfg)
    for stage in list(STAGES if stages is None else stages):
        name = stage_name(stage)
        started = time.perf_counter()
        failed, error = False, ""
        try:
            if lang is not None and _accepts_language(stage):
                result = stage(text, cfg, language=lang)
            else:
                result = stage(text, cfg)
        except Exception as exc:
            # The class only: a message may quote the dictation.
            log.error("Pipeline stage %s failed (%s); skipped", name,
                      type(exc).__name__)
            result, failed, error = None, True, _safe_error(exc, text)
        else:
            if not isinstance(result, str):
                log.error("Pipeline stage %s returned %s, not str; skipped",
                          name, type(result).__name__)
                failed = True
                error = f"returned {type(result).__name__}, not text"
                result = None
        if result is not None:
            text = result
        if trace is not None:
            trace.append(TraceEntry(name, text, time.perf_counter() - started,
                                    failed, error))
    return text

def text_to_paste(final, cfg):
    """The exact text pasted for `final`: a trailing space is added when
    cfg['trailing_space'] is on, unless the text already ends in whitespace
    (a multi-line snippet ending in a newline must not get a stray space at
    the start of the next line)."""
    if cfg.get("trailing_space", True) and final and not final[-1].isspace():
        return final + " "
    return final
