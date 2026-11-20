"""Speech to text with faster-whisper. Returns the raw transcript only."""

import importlib.util
import logging
import os
import re
import site
import threading
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


# -- is a model folder complete? ---------------------------------------------
#
# huggingface_hub hands back a snapshot folder AS IS when the Hub cannot be
# reached (or local_files_only is set) and no cached file listing proves that
# something is missing. A folder left half-filled by an interrupted or failed
# download therefore looks like "the model", and ctranslate2 then fails with
# "Unable to open file 'model.bin'". So the app decides for itself whether a
# folder is complete, before ctranslate2 is involved.
#
# THE SAME RULE IS IN install.ps1 ($ModelCode: folder_is_complete), which runs
# standalone and cannot import this file. Change both together;
# tests/test_model_complete.py fails if they drift apart.
MODEL_MIN_BYTES = 1_000_000          # a model.bin smaller than this is broken
# The files faster-whisper reads (faster_whisper.utils.download_model asks for
# exactly these): model.bin and config.json for ctranslate2, tokenizer.json
# for the tokenizer (without it faster-whisper would go to the network for
# one), one vocabulary.* file, and, for the models that ship one,
# preprocessor_config.json (the 128-mel setting of large-v3-turbo).
# A pinned model (MODEL_REVISIONS) must have all of them; a custom, unpinned
# repository only model.bin and config.json (its other files are unknown).
CORE_FILES = ("model.bin", "config.json")
EXTRA_FILES = ("tokenizer.json", "vocabulary.*")
EXTRA_BY_MODEL = {"large-v3-turbo": ("preprocessor_config.json",),
                  "turbo": ("preprocessor_config.json",)}
VOCABULARY_FILES = ("vocabulary.json", "vocabulary.txt")

# Per pinned model: (repository, size of model.bin in bytes). Only used to
# say how big the download is and for a rough percentage.
MODEL_INFO = {
    "small": ("Systran/faster-whisper-small", 483_546_902),
    "large-v3-turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                       1_617_884_929),
    "turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", 1_617_884_929),
}


def _good_file(path, min_bytes=1):
    """A regular file (a symlink is followed) of at least min_bytes whose
    first bytes can be read."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < min_bytes:
            return False
        with open(path, "rb") as f:
            return len(f.read(16)) > 0
    except (OSError, ValueError):
        return False


def required_files(name):
    """The files a complete folder of this model has ("vocabulary.*" stands
    for one of VOCABULARY_FILES); only model.bin and config.json for an
    unpinned model name."""
    if model_revision(name) is None:
        return CORE_FILES
    return CORE_FILES + EXTRA_FILES + EXTRA_BY_MODEL.get(name.strip(), ())


def model_folder_problems(folder, name):
    """The required files that are missing or broken in `folder` (an empty
    list means it is complete). model.bin must also be at least
    MODEL_MIN_BYTES long."""
    folder = os.fspath(folder)
    problems = []
    for fname in required_files(name):
        if fname == "vocabulary.*":
            ok = any(_good_file(os.path.join(folder, v))
                     for v in VOCABULARY_FILES)
        else:
            ok = _good_file(os.path.join(folder, fname),
                            MODEL_MIN_BYTES if fname == "model.bin" else 1)
        if not ok:
            problems.append(fname)
    return problems


def model_size_text(name):
    """'about 1.5 GB' for a pinned model, else None."""
    info = MODEL_INFO.get(name.strip()) if isinstance(name, str) else None
    if not info:
        return None
    return "about %.1f GB" % (info[1] / 2 ** 30)     # as Windows counts


# -- logging without the account name ---------------------------------------

def cache_root():
    """The Hugging Face cache folder (what huggingface_hub uses)."""
    env = os.environ
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"])
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]) / "hub"
    try:
        import huggingface_hub.constants as hc
        return Path(hc.HF_HUB_CACHE)
    except Exception:
        return Path.home() / ".cache" / "huggingface" / "hub"


# The account name may hold spaces ("First Last"): the first folder takes
# everything up to the next slash, a colon, a quote or the end of the line.
_USER_FOLDER = re.compile(
    r"[A-Za-z]:[\\/]+Users[\\/][^\\/:'\"\r\n]*(?:[\\/][^\s'\"]*)*",
    re.IGNORECASE)


def _home_folders():
    """The account's own folder as the system spells it (home and
    USERPROFILE), for the log scrubber."""
    out = []
    for value in (os.path.expanduser("~"), os.environ.get("USERPROFILE")):
        if value and len(value) > 3 and value not in out:
            out.append(value)
    return out


def scrub(text):
    """A text for the log without the cache folder or any path inside a Users
    folder (they contain the Windows account name)."""
    text = str(text)
    try:
        root = str(cache_root())
        if root:
            text = re.sub(re.escape(root), "<cache>", text,
                          flags=re.IGNORECASE)
    except Exception:
        pass
    for home in _home_folders():
        for spelling in {home, home.replace("\\", "/"),
                         home.replace("/", "\\")}:
            text = re.sub(re.escape(spelling), "<user path>", text,
                          flags=re.IGNORECASE)
    return _USER_FOLDER.sub("<user path>", text)


def _relative(folder):
    """The model folder as a path inside the cache, for the log."""
    try:
        return Path(folder).relative_to(cache_root()).as_posix()
    except (ValueError, OSError):
        return Path(folder).name


# -- why a download failed ----------------------------------------------------

class ModelDownloadError(RuntimeError):
    """The speech model could not be downloaded or completed. str() is one
    plain sentence (no path, no library text); `kind` is a short code,
    `reason` the end of that sentence and `advice` what to do about it. The
    technical detail is in the log only."""

    def __init__(self, kind, reason, advice):
        self.kind, self.reason, self.advice = kind, reason, advice
        super().__init__(f"The speech model could not be downloaded: "
                         f"{reason}.")


_CONNECTION_NAMES = {"ConnectError", "ConnectionError", "NameResolutionError",
                     "OfflineModeIsEnabled", "LocalEntryNotFoundError",
                     "NetworkError", "gaierror"}
_TIMEOUT_NAMES = {"TimeoutException", "ConnectTimeout", "ReadTimeout",
                  "WriteTimeout", "PoolTimeout", "Timeout", "TimeoutError"}
_INTERRUPTED_NAMES = {"RemoteProtocolError", "ReadError", "IncompleteRead",
                      "ChunkedEncodingError"}


def _chain(exc):
    seen = []
    while exc is not None and all(exc is not s for s in seen) \
            and len(seen) < 8:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def classify_download_error(exc, name=None):
    """A ModelDownloadError (not raised) for what went wrong. exc may be
    None (the download ended but the folder is still incomplete)."""
    size = model_size_text(name)
    disk = (f"there is not enough free disk space ({size} is needed)"
            if size else "there is not enough free disk space")
    for e in _chain(exc):
        names = {c.__name__ for c in type(e).__mro__}
        text = str(e).lower()
        if isinstance(e, OSError):
            if (getattr(e, "errno", None) == 28
                    or getattr(e, "winerror", None) in (112, 39)
                    or "no space left" in text
                    or "not enough space on the disk" in text):
                return ModelDownloadError(
                    "disk", disk, "Free up some disk space, then try again.")
            if (isinstance(e, PermissionError)
                    or getattr(e, "winerror", None) in (5, 32, 33)):
                return ModelDownloadError(
                    "permission",
                    "the model folder could not be written (access was "
                    "denied or a file is in use)",
                    "Close other programs that may be using it, such as "
                    "antivirus, then try again.")
        status = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            if status in (401, 403):
                return ModelDownloadError(
                    "http", f"huggingface.co refused the request (error "
                    f"{status})", "Try again later. If it keeps happening, "
                    "this network may block the download.")
            if status == 429:
                return ModelDownloadError(
                    "http", "huggingface.co is asking for fewer requests "
                    "(error 429)", "Wait a few minutes.")
            if status >= 500:
                return ModelDownloadError(
                    "http", f"huggingface.co has a problem (error "
                    f"{status})", "Try again later.")
        if "ProxyError" in names or ("proxy" in text and "connect" in text):
            return ModelDownloadError(
                "proxy", "the connection through a proxy failed",
                "Check your internet connection and proxy settings.")
        if "SSLError" in names or "certificate verify failed" in text:
            return ModelDownloadError(
                "ssl", "a secure connection to huggingface.co could not be "
                "made", "Check your internet connection, and whether "
                "security software or a proxy is interfering.")
        if names & _TIMEOUT_NAMES:
            return ModelDownloadError(
                "timeout", "huggingface.co did not answer in time",
                "Check your internet connection.")
        if names & _INTERRUPTED_NAMES or "consistency check failed" in text:
            return ModelDownloadError(
                "interrupted", "the download was interrupted",
                "Check your internet connection.")
        if names & _CONNECTION_NAMES:
            return ModelDownloadError(
                "connection", "no connection to huggingface.co",
                "Check your internet connection.")
    if exc is None:
        return ModelDownloadError(
            "interrupted", "the download was interrupted",
            "Check your internet connection.")
    return ModelDownloadError(
        "other", f"the download failed ({type(exc).__name__})",
        "The log has the details.")


# -- download progress (for the window and the tray) --------------------------

_dl_lock = threading.Lock()
_dl_active = None            # the model name being downloaded, or None


def download_progress():
    """None, or {"name", "size", "percent"} while this process downloads a
    speech model: size is 'about 1.5 GB' (or None), percent is 0-99 from the
    size of the model's cache folder against its known size (None for a model
    whose size is not known). Any thread; cheap."""
    with _dl_lock:
        name = _dl_active
    if name is None:
        return None
    percent = None
    info = MODEL_INFO.get(name.strip())
    if info:
        try:
            blobs = (cache_root() / ("models--" + info[0].replace("/", "--"))
                     / "blobs")
            have = sum(f.stat().st_size for f in blobs.iterdir()
                       if f.is_file())
            percent = max(0, min(99, int(have * 100 / info[1])))
        except OSError:
            percent = 0
    return {"name": name, "size": model_size_text(name), "percent": percent}


def _repo_of(folder):
    """(repository, commit, cache folder) from a snapshot folder
    <cache>/models--Owner--Name/snapshots/<commit>, else None."""
    p = Path(folder)
    repo_dir = p.parent.parent
    if p.parent.name != "snapshots" or not repo_dir.name.startswith("models--"):
        return None
    return (repo_dir.name[len("models--"):].replace("--", "/"), p.name,
            repo_dir.parent)


def _is_entry_not_found(exc):
    names = {c.__name__ for c in type(exc).__mro__}
    return "EntryNotFoundError" in names and "LocalEntryNotFoundError" \
        not in names


def _repair(folder, problems):
    """Fetch the missing or broken files of a snapshot folder one by one,
    pinned to the folder's own commit. Raises what huggingface_hub raises."""
    found = _repo_of(folder)
    if found is None:
        return
    repo, revision, cache = found
    import huggingface_hub
    try:
        from faster_whisper.utils import disabled_tqdm
        extra = {"tqdm_class": disabled_tqdm}
    except Exception:                    # pragma: no cover
        extra = {}
    for fname in problems:
        wanted = VOCABULARY_FILES if fname == "vocabulary.*" else (fname,)
        failure = None
        for candidate in wanted:
            target = os.path.join(os.fspath(folder), candidate)
            broken = os.path.lexists(target)
            if broken:
                # A zero-byte or dangling entry is in the way: only that
                # entry of the snapshot folder goes, then the file is
                # fetched again for real.
                try:
                    os.unlink(target)
                except OSError:
                    pass
            try:
                huggingface_hub.hf_hub_download(
                    repo, candidate, revision=revision, cache_dir=str(cache),
                    force_download=broken, **extra)
                failure = None
                break
            except Exception as exc:
                failure = exc
                if not _is_entry_not_found(exc):
                    raise
        if failure is not None:
            raise failure


def _download_error(name, exc, folder=None):
    err = classify_download_error(exc, name)
    log.warning("Speech model download failed (%s%s): %s", err.kind,
                f"; folder {_relative(folder)}" if folder else "",
                scrub(f"{type(exc).__name__}: {exc}") if exc is not None
                else "the folder is still incomplete")
    return err


def _download(name, download_model, kwargs):
    """Download (or complete) the model: the folder, or ModelDownloadError."""
    global _dl_active
    with _dl_lock:
        _dl_active = name
    try:
        try:
            folder = download_model(name, local_files_only=False, **kwargs)
        except Exception as exc:
            raise _download_error(name, exc) from None
        problems = model_folder_problems(folder, name)
        if problems:
            log.warning("The download left %s incomplete (%s); fetching the "
                        "missing files", _relative(folder),
                        ", ".join(problems))
            try:
                _repair(folder, problems)
            except Exception as exc:
                raise _download_error(name, exc, folder) from None
            problems = model_folder_problems(folder, name)
        if problems:
            raise _download_error(name, None, folder)
        return folder
    finally:
        with _dl_lock:
            _dl_active = None


def resolve_model(name):
    """The folder to hand to WhisperModel for a model name: the local cache
    when it holds a COMPLETE copy (no network is used then, so a broken
    proxy cannot stop a cached model), else the model is downloaded or
    completed. A local folder path is returned unchanged. Raises
    ModelDownloadError when it cannot be had."""
    if not isinstance(name, str) or os.path.isdir(name):
        return name
    import faster_whisper
    download_model = faster_whisper.download_model
    kwargs = model_kwargs(name)
    folder = None
    try:
        folder = download_model(name, local_files_only=True, **kwargs)
    except Exception as exc:
        if not _is_missing_model(exc):
            raise
    if folder is not None:
        problems = model_folder_problems(folder, name)
        if not problems:
            return folder
        log.info("The speech model on this computer is incomplete (%s: %s)",
                 _relative(folder), ", ".join(problems))
    else:
        log.info("The speech model is not on this computer yet")
    log.info("Downloading the speech model")
    return _download(name, download_model, kwargs)

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

        errors, download_error = [], None
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
                if isinstance(exc, ModelDownloadError):
                    download_error = exc      # plain text, no paths
                    errors.append(f"{name} on {dev}: {exc}")
                    log.warning("Could not get %s for %s: %s", name, dev, exc)
                    if dev == "cuda":
                        gpu_problem = ("the speech model for the graphics "
                                       "card could not be downloaded")
                    continue
                errors.append(scrub(f"{name} on {dev}: {exc}"))
                log.warning("Could not use %s on %s: %s", name, dev,
                            scrub(exc))
                if dev == "cuda":
                    gpu_problem = _why_gpu_failed(exc)
        if self.model is None:
            if download_error is not None:
                raise download_error
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
        cannot stop a cached model); download only if the model is missing
        or incomplete. The folder is checked (resolve_model) BEFORE
        ctranslate2 sees it."""
        path = resolve_model(name)
        return WhisperModel(path, device=device, compute_type=compute_type)

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