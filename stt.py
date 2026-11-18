"""Speech to text with faster-whisper. Returns the raw transcript only."""

import importlib.util
import logging
import os
import site
import time
from pathlib import Path

import numpy as np

# Before faster-whisper / huggingface_hub is imported (they read it on import).
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import textrules
from audio import SAMPLE_RATE

log = logging.getLogger("holdtotalk.stt")

# The speech models are downloaded from Hugging Face by repository name. To
# be sure that what is downloaded is the files that were tested, and not
# whatever a repository holds later, each model the app uses is pinned to one
# exact commit (revision). install.ps1 reads this same table (the lines below
# of the form "name": "40 hex characters"), so the installer's optional
# pre-download and the app load the same revision. A model name that is not
# listed (a custom cfg["model"]) is loaded unpinned.
#
# To update (do this on purpose, per release, and test the new files):
#   1. open the model's page on huggingface.co (the repository is named in
#      faster_whisper.utils._MODELS) and note the commit hash of the version
#      to use (the API at /api/models/<repository> shows it as "sha"); or,
#      after downloading it, read the folder name under
#      %USERPROFILE%\.cache\huggingface\hub\models--<owner>--<name>\snapshots
#   2. replace the 40-character hash here, and say so in CHANGELOG.md.
# small           -> Systran/faster-whisper-small
# large-v3-turbo  -> mobiuslabsgmbh/faster-whisper-large-v3-turbo (also "turbo")
MODEL_REVISIONS = {
    "small": "536b0662742c02347bc0e980a01041f333bce120",
    "large-v3-turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
    "turbo": "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
}


def model_revision(name):
    """The pinned commit for a model name, or None (not pinned)."""
    rev = MODEL_REVISIONS.get(name.strip()) if isinstance(name, str) else None
    return rev or None


def model_kwargs(name):
    """{"revision": hash} for a pinned model, else {}."""
    rev = model_revision(name)
    return {"revision": rev} if rev else {}

# faster-whisper keeps only the LAST 223 tokens of initial_prompt (half the
# 448-token context, minus one), so an over-long glossary would silently lose
# its "Glossary:" prefix and the first (user-typed, most important) terms.
# Terms are therefore added in order until this budget is reached.
PROMPT_MAX_TOKENS = 200
PROMPT_PREFIX = "Glossary: "


def estimate_tokens(text):
    """Conservative token estimate when no tokenizer is available."""
    return len(text.encode("utf-8")) // 2 + 1


def glossary_prompt(terms, count_tokens=estimate_tokens,
                    max_tokens=PROMPT_MAX_TOKENS):
    """'Glossary: a, b, c.' with as many leading terms as fit in
    max_tokens; None when there are no terms."""
    chosen = []
    for term in terms:
        candidate = PROMPT_PREFIX + ", ".join(chosen + [term]) + "."
        if count_tokens(candidate) > max_tokens:
            break
        chosen.append(term)
    if len(chosen) < len(terms):
        log.debug("Glossary capped at %d of %d terms", len(chosen), len(terms))
    return PROMPT_PREFIX + ", ".join(chosen) + "." if chosen else None


_dll_dirs_added = False
_dll_handles = []      # keep add_dll_directory handles alive


def _nvidia_roots():
    """The folders that can hold the pip-installed NVIDIA runtime."""
    roots = []
    spec = importlib.util.find_spec("nvidia")
    if spec and spec.submodule_search_locations:
        roots += list(spec.submodule_search_locations)
    try:
        roots += [str(Path(p) / "nvidia") for p in site.getsitepackages()]
    except AttributeError:
        pass
    return [Path(r) for r in dict.fromkeys(roots)]


def cuda_runtime_installed():
    """True when the NVIDIA runtime (cuBLAS) is there: the pip packages the
    installer adds (it asks first, and a "no" leaves them out) or a CUDA
    toolkit whose folder is on PATH. Without it the graphics card is not
    tried, so the large GPU model is never downloaded for nothing."""
    for root in _nvidia_roots():
        if root.is_dir() and any(root.glob("cublas/bin/cublas64_*.dll")):
            return True
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        try:
            if folder and any(Path(folder).glob("cublas64_*.dll")):
                return True
        except OSError:
            continue
    return False


def add_cuda_dll_dirs():
    """Put the pip-installed NVIDIA DLL folders (cuBLAS, cuDNN, NVRTC) on the
    DLL search path. Must run before faster_whisper/ctranslate2 is imported.

    Both mechanisms are needed: os.add_dll_directory for the first-level
    load, PATH for the DLLs that cuDNN itself loads with plain LoadLibrary.
    """
    global _dll_dirs_added
    if _dll_dirs_added:
        return []
    _dll_dirs_added = True

    roots = _nvidia_roots()
    added = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for bin_dir in sorted(root.glob("*/bin")):
            if str(bin_dir) in added or not any(bin_dir.glob("*.dll")):
                continue
            added.append(str(bin_dir))
            try:
                _dll_handles.append(os.add_dll_directory(str(bin_dir)))
            except OSError:
                pass
    if added:
        os.environ["PATH"] = os.pathsep.join(added + [os.environ.get("PATH", "")])
    log.debug("CUDA DLL folders: %s", added)
    return added


COMPUTE_TYPES = ("float16", "int8_float16", "int8")     # best first, for cuda


def pick_compute_type(supported, requested="auto"):
    """The compute type to load with: `requested` (cfg["compute_type"]) when
    the device supports it, else the first of COMPUTE_TYPES that `supported`
    offers (also when requested is "auto" or unset), else None. A request
    the device cannot do falls back to that choice with a logged warning."""
    supported = set(supported or ())
    choice = requested.strip().lower() if isinstance(requested, str) else ""
    if choice and choice != "auto":
        if choice in supported:
            return choice
        log.warning("compute_type %r is not supported here (it supports: "
                    "%s); using auto instead", choice,
                    ", ".join(sorted(supported)) or "nothing")
    return next((c for c in COMPUTE_TYPES if c in supported), None)


# What every dictation asks faster-whisper for. The start-up warm-up uses the
# very same options, so nothing (such as the voice-activity model) is first
# created by the first real dictation.
VAD_PARAMETERS = {"min_silence_duration_ms": 500}


def decode_options(language, prompt):
    return {"language": language, "beam_size": 5, "vad_filter": True,
            "vad_parameters": dict(VAD_PARAMETERS),
            "condition_on_previous_text": False, "initial_prompt": prompt}


def _is_missing_model(exc):
    """True when loading failed only because the model is not on disk."""
    names = {c.__name__ for c in type(exc).__mro__}
    return (isinstance(exc, FileNotFoundError)
            or "LocalEntryNotFoundError" in names
            or "outgoing traffic has been disabled" in str(exc))


def _why_gpu_failed(exc):
    """A short plain reason for the UI (no model name, no raw message)."""
    msg = str(exc).lower()
    if "out of memory" in msg:
        return "the graphics card ran out of memory"
    if any(w in msg for w in ("cublas", "cudnn", "nvrtc", ".dll", "cuda")):
        return "the NVIDIA libraries for the graphics card could not be loaded"
    return "the graphics card could not be used"


class Engine:
    """Loads Whisper once: GPU first (cfg['model'], best compute type the
    card supports), then CPU (cfg['cpu_model'], int8) when cfg['device'] is
    'auto'.

    cfg["compute_type"] ("auto" by default, or one of settings.COMPUTE_TYPES)
    asks for a compute type; one the device does not support is replaced by
    the automatic choice, with a logged warning.

    Attributes: name, device ('cuda'/'cpu'), compute_type (all None until a
    model is really loaded), cfg (read on every transcribe, so
    language/vocabulary changes apply immediately), last_language (the
    language the last transcribe() used or detected, or None),
    last_language_probability (how sure the detection was, 0-1; None when
    the language was chosen, not detected) and
    fallback_reason (None, or a short plain reason why the engine runs on the
    processor although a graphics card was wanted or expected; the model name
    is never part of it).
    """

    def __init__(self, cfg):
        add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        self.cfg = cfg
        self.model = None
        self.name = self.device = self.compute_type = None
        self.last_language = None
        self.last_language_probability = None
        self.fallback_reason = None
        requested = cfg.get("compute_type", "auto")
        device = cfg.get("device", "auto")
        if device not in ("auto", "cuda", "cpu"):
            raise ValueError(f"device must be auto, cuda or cpu, not {device!r}")
        attempts, gpu_problem = [], None
        if device in ("auto", "cuda"):
            compute_type, gpu_problem = self._cuda_plan(requested)
            if compute_type:
                attempts.append((cfg["model"], "cuda", compute_type))
        if device in ("auto", "cpu"):
            attempts.append((cfg["cpu_model"], "cpu",
                             self._cpu_compute_type(requested)))

        errors = []
        for name, dev, compute_type in attempts:
            try:
                log.info("Loading a speech model on %s ...", dev)
                started = time.time()
                model = self._load(WhisperModel, name, dev, compute_type)
                # Warm up; this is also where missing CUDA DLLs show up.
                if cfg.get("language"):
                    # With a chosen language the call below skips the
                    # speech model on silence, so run it once on its own.
                    segments, _ = model.transcribe(
                        np.zeros(SAMPLE_RATE, dtype=np.float32),
                        language="en", beam_size=1)
                    list(segments)
                # The warm-up asks for exactly what a dictation asks for, so
                # the voice-activity model is loaded now, not by the first
                # dictation (that cost it about 220 ms). With the language
                # on automatic it also runs the speech model (language
                # detection), so it is the DLL check as well.
                self.model = model
                self._warm_up_like_a_dictation()
                self.model, self.name = model, name
                self.device, self.compute_type = dev, compute_type
                log.info("Ready: %s on %s (%.1fs)", name, dev,
                         time.time() - started)
                break
            except Exception as exc:
                self.model = None
                errors.append(f"{name} on {dev}: {exc}")
                log.warning("Could not use %s on %s: %s", name, dev, exc)
                if dev == "cuda":
                    gpu_problem = _why_gpu_failed(exc)
        if self.model is None:
            raise RuntimeError("No speech model could be loaded: "
                               + "; ".join(errors or [gpu_problem or
                                                      "no usable device"]))
        if self.device == "cpu" and gpu_problem:
            self.fallback_reason = gpu_problem
            log.info("Running on the processor: %s", gpu_problem)

    def _warm_up_like_a_dictation(self):
        """One transcription of a second of silence with the options (and
        glossary) a real dictation uses."""
        cfg = self.cfg
        prompt = glossary_prompt(textrules.vocabulary_terms(cfg),
                                 self.count_tokens)
        segments, _ = self.model.transcribe(
            np.zeros(SAMPLE_RATE, dtype=np.float32),
            **decode_options(cfg.get("language") or None, prompt))
        list(segments)

    @staticmethod
    def _cpu_compute_type(requested="auto"):
        """int8 on the processor, or `requested` when the processor
        supports it."""
        choice = requested.strip().lower() if isinstance(requested, str) \
            else ""
        if not choice or choice == "auto":
            return "int8"
        try:
            import ctranslate2
            supported = set(ctranslate2.get_supported_compute_types("cpu"))
        except Exception:
            return "int8"
        if choice in supported:
            return choice
        log.info("compute_type %r is not supported on the processor; using "
                 "int8", choice)
        return "int8"

    @staticmethod
    def _cuda_plan(requested="auto"):
        """(compute_type, problem): how to load on the graphics card, or
        (None, why not). Asks ctranslate2 first, so a PC without an NVIDIA
        card never downloads the large GPU model."""
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() <= 0:
                return None, "no NVIDIA graphics card was found"
            if not cuda_runtime_installed():
                return None, ("the NVIDIA runtime is not installed (run the "
                              "installer again to add it)")
            compute_type = pick_compute_type(
                ctranslate2.get_supported_compute_types("cuda"), requested)
        except Exception as exc:
            log.warning("Could not query the graphics card (%s)",
                        type(exc).__name__)
            return None, "the graphics card could not be queried"
        if compute_type is None:
            return None, "the graphics card supports no suitable mode"
        return compute_type, None

    @staticmethod
    def _load(WhisperModel, name, device, compute_type):
        """Load from the local cache only (no network, so a broken proxy
        cannot stop a cached model); download only if it is missing."""
        try:
            return WhisperModel(name, device=device, compute_type=compute_type,
                                local_files_only=True, **model_kwargs(name))
        except Exception as exc:
            if not _is_missing_model(exc):
                raise
        log.info("The speech model is not on this computer yet; downloading")
        return WhisperModel(name, device=device, compute_type=compute_type,
                            local_files_only=False, **model_kwargs(name))

    def count_tokens(self, text):
        """Whisper tokens in `text` as faster-whisper encodes a prompt."""
        tok = getattr(self.model, "hf_tokenizer", None)
        if tok is None:
            return estimate_tokens(text)
        return len(tok.encode(" " + text.strip(),
                              add_special_tokens=False).ids)

    def transcribe(self, audio):
        """float32 mono 16 kHz array -> raw transcript (no post-processing).
        The language used (cfg["language"]) or detected is left in
        self.last_language."""
        cfg = self.cfg
        prompt = glossary_prompt(textrules.vocabulary_terms(cfg),
                                 self.count_tokens)
        wanted = cfg.get("language") or None
        segments, info = self.model.transcribe(
            np.asarray(audio, dtype=np.float32),
            **decode_options(wanted, prompt))
        detected = getattr(info, "language", None)
        self.last_language = wanted or (detected if isinstance(detected, str)
                                        and detected else None)
        probability = getattr(info, "language_probability", None)
        self.last_language_probability = (
            float(probability) if not wanted and self.last_language
            and isinstance(probability, (int, float)) else None)
        return " ".join(seg.text.strip() for seg in segments).strip()