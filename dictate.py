"""Hold-to-talk dictation for Windows.

Hold the hotkey, speak, release: the speech is transcribed locally with
Whisper, optionally tidied by a local LLM (Ollama), and pasted into whatever
window has focus. A tray icon shows the state and quick actions; clicking it
opens the app window (api.py serves it, uiwin.py shows it).

    pythonw dictate.py                     run the app with no console (normal);
                                           log in the data folder's logs\\
                                           dictate.log. If it is already
                                           running, this opens its window.
    pythonw dictate.py --open              start (if needed) and open the window
    python dictate.py                      run it with the log in this console
    python dictate.py --quit               stop the running app
    python dictate.py --list-devices       show microphones
    python dictate.py --mic-test           record 0.5 s from the configured mic
    python dictate.py --selftest a.wav     transcribe a WAV file and exit (with
                                           no console the text is not printed,
                                           only word counts, unless --log-text)
    python dictate.py --smoke [SECONDS] [--wav a.wav] [--tray]
                                           run the app loop in dry-run mode
                                           (never pastes) and exit by itself
    python dictate.py --uninstall          remove the Start with Windows entry
                                           and list what else can be deleted
                                           (deletes nothing else)
User data (config.json, history.sqlite3, logs\\dictate.log) lives in
settings.DATA_DIR (%LOCALAPPDATA%\\HoldToTalk\\data, or HOLDTOTALK_HOME);
data from older versions next to this file is moved there at start-up.

Testing options: --dry-run (never paste), --config FILE, --history FILE,
--log FILE, --ui-port N (fixed port for the window server; default: any free
port). Any of --config/--history/--log makes a test instance: every file it
uses that was not given is put next to the one that was (never the real
data), nothing is migrated, it has its own single-instance lock, --quit /
--open signals and window profile, and no Start with Windows entry, so a
test run can coexist with the real app.
Developer option: --log-text writes dictated text into the log (off by
default; never use it with real dictations you want to keep private).
"""

import argparse
import ctypes
import hashlib
import io
import logging
import os
import sys
import threading
import time
import types
from ctypes import wintypes
from pathlib import Path

import settings

log = logging.getLogger("holdtotalk")
BASE_NAME = "Local\\HoldToTalkDictation"
ERROR_ALREADY_EXISTS = 183
LOG_PATH = settings.LOG_PATH       # the real log (data folder)
DEFAULT_CONFIG = settings.CONFIG_PATH
LOG_MAX_BYTES = 1_000_000          # then dictate.log -> dictate.log.1
# fn(parts) after wiring, parts.app / .tray / .history / .server / .window;
# for tests.
STARTUP_HOOKS = []

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                   wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                   wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                 wintypes.LPCWSTR]
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
    wintypes.DWORD]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def instance_suffix(config_path=None):
    """'' for the normal installation (the data folder's config.json, or
    the legacy config.json next to the app), else a short stable tag derived
    from the config file's path."""
    path = Path(config_path or settings.CONFIG_PATH).resolve()
    for normal in (DEFAULT_CONFIG, settings.APP_DIR / "config.json"):
        if str(path).lower() == str(Path(normal).resolve()).lower():
            return ""
    return "-" + hashlib.sha1(str(path).lower().encode(),
                                     usedforsecurity=False).hexdigest()[:8]


def _peek(raw_args, flag):
    """The value of `--flag VALUE` / `--flag=VALUE` before argparse runs."""
    for i, arg in enumerate(raw_args):
        if arg == flag and i + 1 < len(raw_args):
            return raw_args[i + 1]
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:]
    return None


def data_paths(config=None, history=None, log_file=None):
    """(config, history, log, test) for this run. With none of the test
    overrides: the real files in settings' data folder. With any of them:
    a test instance whose missing files go next to the given one, so it can
    never read or write the real data."""
    given = [Path(p).resolve() for p in (config, history, log_file) if p]
    if not given:
        return (Path(settings.CONFIG_PATH), Path(settings.HISTORY_PATH),
                Path(settings.LOG_PATH), False)
    base = given[0].parent
    return (Path(config).resolve() if config else base / "config.json",
            Path(history).resolve() if history else base / "history.sqlite3",
            Path(log_file).resolve() if log_file else base / "dictate.log",
            True)


def use_paths(config, history, log_path, test):
    """Point settings at this run's files (a test instance's folder becomes
    its data folder too)."""
    settings.CONFIG_PATH = config
    settings.HISTORY_PATH = history
    settings.LOG_PATH = log_path
    if test:
        settings.DATA_DIR = config.parent


def migrate_once():
    """Move data from older versions (next to the code) into the data
    folder; real instance only, after the single-instance lock. Skipped
    when HOLDTOTALK_HOME is set (a chosen data folder, or a test run)."""
    migrate = getattr(settings, "migrate_legacy_data", None)
    if migrate is None:
        return None
    if os.environ.get("HOLDTOTALK_HOME"):
        if any((settings.APP_DIR / n).exists()
               for n in ("config.json", "history.sqlite3")):
            log.info("HOLDTOTALK_HOME is set: data next to the program from "
                     "an older version was not moved")
        return None
    try:
        ensure = getattr(settings, "ensure_data_dirs", None)
        if ensure is not None:
            ensure()
        summary = migrate(app_dir=settings.APP_DIR,
                          data_dir=settings.DATA_DIR)
    except Exception as exc:
        log.warning("Moving old data to the data folder failed (%s)",
                    type(exc).__name__)
        return {"config": "failed", "history": "failed", "renamed": [],
                "errors": [type(exc).__name__]}
    if summary.get("errors"):
        log.warning("Moving old data: %d problem(s): %s",
                    len(summary["errors"]), "; ".join(summary["errors"]))
    else:
        if summary.get("renamed"):
            log.info("Moved old data to the data folder: config %s, "
                     "history %s", summary.get("config"),
                     summary.get("history"))
        _retire_old_log(summary)
    return summary


def terms_pending_message():
    """What to tell the user when the Terms of Use have not been accepted
    yet (this version of them): why nothing dictates, and what to do."""
    return (f"{settings.APP_NAME} will not dictate until you have read and "
            f"accepted its Terms of Use (version {settings.TERMS_VERSION}).\n\n"
            "Its window opens on the terms. If it does not, click the "
            f"{settings.APP_NAME} icon in the notification area (near the "
            "clock) and choose Open, then choose I agree. You can read the "
            f"terms first in the file TERMS.md in {settings.APP_DIR}.")


def _retire_old_log(summary):
    """The old dictate.log next to the code may contain dictated text from
    older versions: once the data has moved, rename it (never delete) so it
    no longer looks like a live log."""
    for name in ("dictate.log", "dictate.log.1"):
        old = settings.APP_DIR / name
        if not old.is_file():
            continue
        dest, n = old.with_name(name + ".migrated"), 1
        while dest.exists():
            n += 1
            dest = old.with_name(f"{name}.migrated-{n}")
        try:
            os.replace(old, dest)
            summary.setdefault("renamed", []).append(dest.name)
        except OSError as exc:
            log.warning("Could not rename the old %s (%s)", name,
                        type(exc).__name__)


def migration_problem(summary):
    """A plain sentence for the user when old data could not be moved, or
    None."""
    if not summary or not summary.get("errors"):
        return None
    failed = [k for k in ("config", "history")
              if summary.get(k) in ("failed",)]
    what = " and ".join({"config": "settings", "history": "history"}[k]
                        for k in failed) or "data"
    return (f"Your {what} from the previous version could not be moved to "
            f"the new data folder ({settings.DATA_DIR}). Nothing was "
            f"deleted: the old files are still in {settings.APP_DIR}. Quit "
            "the app, close any program that uses them, and start it again; "
            "until then the app runs with default settings and an empty "
            "history.")


INSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk"


def install_dirs():
    """Where the installer puts the program: %LOCALAPPDATA%\\Programs\\
    HoldToTalk (htt-common.ps1), plus the InstallLocation it recorded under
    its Uninstall key (a different -InstallRoot). Lower-case, resolved."""
    found = []
    base = os.environ.get("LOCALAPPDATA")
    if base:
        found.append(Path(base) / "Programs" / "HoldToTalk")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, INSTALL_REG_KEY) as key:
            location, _ = winreg.QueryValueEx(key, "InstallLocation")
        if isinstance(location, str) and location.strip():
            found.append(Path(location.strip()))
    except OSError:
        pass
    out = []
    for path in found:
        try:
            out.append(str(path.resolve()).lower())
        except OSError:
            out.append(str(path).lower())
    return out


def _same_or_inside(folder, parent):
    folder, parent = str(folder).lower().rstrip("\\/"), str(parent).lower().rstrip("\\/")
    return folder == parent or folder.startswith(parent + "\\")


def _entry_folder(command):
    """The folder of the dictate.py an autostart command runs, or None."""
    for part in str(command).replace('"', "\n").splitlines():
        part = part.strip()
        if part.lower().endswith("dictate.py"):
            try:
                return str(Path(part).resolve().parent).lower()
            except OSError:
                return str(Path(part).parent).lower()
    return None


def fix_autostart(name):
    """If our Start with Windows entry runs dictate.py from another folder
    (the app was moved), point it at this one. Returns the new command, or
    None if nothing was changed. A Task Manager 'disabled' entry is left
    alone.

    Only the installed copy may correct the entry (this folder is the
    installer's per-user location, see install_dirs(), or the entry already
    points at this folder or the install it lives in): running start.bat
    from a source checkout must not take the entry over. Otherwise nothing
    is changed and one line is logged."""
    import tray
    try:
        current = tray.get_autostart(name)
    except OSError:
        return None
    if not current or "dictate.py" not in current.lower():
        return None
    want = tray.autostart_command()
    if current.strip().lower() == want.lower():
        return None
    here = str(settings.APP_DIR.resolve()).lower()
    entry = _entry_folder(current)
    if not (any(_same_or_inside(here, d) for d in install_dirs())
            or (entry is not None and _same_or_inside(here, entry))):
        log.info("Start with Windows runs another copy of the app; this "
                 "copy is not the installed one, so the entry was left "
                 "alone")
        return None
    disabled = getattr(tray, "_startup_disabled", None)
    try:
        if disabled is not None and disabled(name):
            log.info("Start with Windows points at another folder but is "
                     "disabled in Task Manager; left alone")
            return None
        tray.set_autostart(True, name, want)
    except OSError as exc:
        log.warning("Could not correct Start with Windows: %s", exc)
        return None
    log.info("Start with Windows pointed at another folder; it now starts "
             "this copy")
    return want


def names(suffix=""):
    """Kernel object names for an instance: mutex, quit event, open event."""
    base = BASE_NAME + suffix
    return base, base + ".Quit", base + ".Open"


# -- running without a console (pythonw) ----------------------------------------

class CappedLog(io.TextIOBase):
    """A text stream into a log file that cannot grow without bound: once it
    passes max_bytes it is moved to "<name>.1" (replacing the older one) and
    a new file is started. Thread-safe; write errors are swallowed, so a
    full disk never crashes the app."""

    def __init__(self, path, max_bytes=LOG_MAX_BYTES):
        super().__init__()
        self.path, self.max_bytes = Path(path), max_bytes
        self._lock = threading.Lock()
        self._file = None
        self._size = 0
        self._open()

    def _open(self):
        try:
            self._file = open(self.path, "a", encoding="utf-8",
                              errors="replace")
            self._size = self._file.tell()
        except OSError:
            self._file = None

    def _rotate(self):
        self._file.close()
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass        # e.g. a second instance has it open; try next time
        self._open()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, text):
        text = text if isinstance(text, str) else str(text)
        with self._lock:
            try:
                if self._file is not None and self._size > self.max_bytes:
                    self._rotate()
                if self._file is not None:
                    self._file.write(text)
                    self._file.flush()
                    self._size += len(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        with self._lock:
            try:
                if self._file is not None:
                    self._file.flush()
            except Exception:
                pass


def _prepare_streams(log_path):
    """Under pythonw sys.stdout/sys.stderr are None: anything that prints
    (a warning, a traceback, a download progress bar) would crash. Point
    them at a size-limited log file. Returns True when windowless."""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    # Progress bars are pointless without a console; disable them before
    # huggingface_hub / tqdm are imported.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    stream = CappedLog(log_path)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    return True


def message_box(text, error=False, timeout_ms=0):
    """A small Windows message box (used when there is no console).
    timeout_ms > 0 closes it by itself after that long."""
    flags = (0x10 if error else 0x40) | 0x10000     # icon | MB_SETFOREGROUND
    user32 = ctypes.WinDLL("user32")
    try:
        fn = user32.MessageBoxTimeoutW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.UINT, wintypes.WORD, wintypes.DWORD]
        fn(None, text, settings.APP_NAME, flags, 0, timeout_ms or 0xFFFFFFFF)
    except AttributeError:
        user32.MessageBoxW(None, text, settings.APP_NAME, flags)


class _ErrorCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


def _setup_logging(verbose, windowless=False):
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    fmt = ("%(asctime)s %(levelname)-7s %(message)s" if windowless
           else "%(message)s")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, stream=sys.stdout)
    for noisy in ("faster_whisper", "httpx", "httpx2", "httpcore",
                  "httpcore2", "huggingface_hub", "PIL", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    counter = _ErrorCounter()
    logging.getLogger().addHandler(counter)
    return counter


# -- single instance and --quit ---------------------------------------------------

def _single_instance(suffix=""):
    """Hold a named mutex for the life of the process; False if taken."""
    handle = _kernel32.CreateMutexW(None, False, names(suffix)[0])
    if not handle:
        return True          # can't tell; don't block the user
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _listen(app, on_open, suffix=""):
    """`dictate.py --quit` signals the quit event (the app quits); starting
    the app a second time signals the open event (on_open() runs).
    Returns a function that stops listening."""
    _, quit_name, open_name = names(suffix)
    quit_ev = _kernel32.CreateEventW(None, True, False, quit_name)
    open_ev = _kernel32.CreateEventW(None, False, False, open_name)  # auto
    handles = [h for h in (quit_ev, open_ev) if h]
    if not handles:
        return lambda: None
    stop = threading.Event()
    arr = (wintypes.HANDLE * len(handles))(*handles)

    def wait():
        while not stop.is_set():
            r = _kernel32.WaitForMultipleObjects(len(handles), arr, False, 250)
            if r >= len(handles):
                continue
            if handles[r] == quit_ev:
                log.info("Quit requested (dictate.py --quit)")
                app.quit()
                return
            log.info("Window requested (the app was started again)")
            try:
                on_open()
            except Exception:
                log.exception("Opening the window failed")
    thread = threading.Thread(target=wait, name="signal-listener",
                              daemon=True)
    thread.start()

    def close():
        stop.set()
        thread.join(2)
        for h in handles:
            _kernel32.CloseHandle(h)
    return close


def _signal(name):
    handle = _kernel32.OpenEventW(0x0002, False, name)  # EVENT_MODIFY_STATE
    if not handle:
        return False
    _kernel32.SetEvent(handle)
    _kernel32.CloseHandle(handle)
    return True


def cmd_quit(suffix=""):
    if not _signal(names(suffix)[1]):
        print("Dictation is not running.")
        return 1
    print("Asked the running dictation app to quit.")
    return 0


def open_running(suffix=""):
    """Ask the running instance to show its window. False if it cannot be
    asked (not running, or an older version without a window)."""
    import uiwin
    uiwin.allow_any_foreground()        # we have the foreground; pass it on
    return _signal(names(suffix)[2])


# -- wiring ---------------------------------------------------------------------

def attach_history(app, path=None):
    """The history store, recording every dictation (app.on_dictation, with
    the dictation's own timestamp so the window can find its row). It reads
    the live config (save_history, history limit) on every record."""
    import textrules
    history = textrules.History(path, cfg=lambda: app.cfg)
    app.on_dictation.append(lambda d: history.record(
        d["raw"], d["final"], d["audio_seconds"], ts=d["ts"]))
    return history


def cleanup_reload_hook(app):
    """on_reload: when AI cleanup is switched on (or its server/model
    changes), start Ollama if needed and load the model."""
    import cleanup

    def hook(old, new, result):
        before, after = cleanup.merged_settings(old), cleanup.merged_settings(new)
        if after != before and after.get("enabled"):
            cleanup.prepare_in_background(lambda: app.cfg)
    return hook


def _preload_engine():
    """Import the speech libraries (faster-whisper, ctranslate2: about
    0.3 s) on a background thread while the window and the tray are being
    built; settings.py used to import them first thing, before anything
    else could start. Returns the thread. Problems are left for the engine
    load to report."""
    def run():
        try:
            import stt
            stt.add_cuda_dll_dirs()       # before ctranslate2 is imported
            import ctranslate2            # noqa: F401
            import faster_whisper         # noqa: F401
        except Exception as exc:
            log.debug("Preloading the speech libraries failed (%s)",
                      type(exc).__name__)
    thread = threading.Thread(target=run, name="preload-speech", daemon=True)
    thread.start()
    return thread


def run_app(args, windowless, log_path, history_path=None, test=False):
    """The app itself. Order matters: the single-instance lock first (a
    second start must not read, migrate or write anything), then moving
    old data (real instance only), then reading the config."""
    suffix = instance_suffix(settings.CONFIG_PATH if test else None)
    if not _single_instance(suffix):
        if open_running(suffix):
            log.info("Already running: asked it to open its window")
            return 0
        msg = ("Dictation is already running. Use its icon in the "
               "notification area (system tray) to change settings or quit.")
        log.warning(msg)
        if windowless:
            message_box(msg, timeout_ms=60000)
        else:
            print(msg)
        return 1

    problem = None
    if not test and not suffix:
        problem = migration_problem(migrate_once())
        if problem:
            log.error("%s", problem)
            if windowless:
                threading.Thread(target=message_box, args=(problem,),
                                 kwargs={"error": True},
                                 daemon=True).start()
    cfg = settings.load_config()
    _preload_engine()

    import api
    import app as app_mod
    import cleanup
    import stt
    import tray
    import uiwin

    autostart = tray.AUTOSTART_NAME if not suffix and not test else None
    if autostart:
        fix_autostart(autostart)
    app = app_mod.App(cfg, None, dry_run=args.dry_run)
    app.log_text = bool(getattr(args, "log_text", False))
    history = attach_history(app, history_path)
    if problem:
        app.call_soon(lambda: app._notice("data", problem))
    handle = server = window = None
    stop_listening = lambda: None   # noqa: E731

    told = {"download": False}

    def on_status(status, detail):
        if status == "ready":
            log.info("Ready (speech model loaded in %s). Hold %s and speak; "
                     "release to paste.%s", detail, app.cfg.get("hotkey"),
                     "" if windowless else " Ctrl+C here quits.")
        elif status == "failed":
            # Stay running: the window shows what went wrong and the tray
            # can still quit. A message box says so when there is no console.
            log.error("Speech model failed to load: %s", detail)
            if app.failure is not None:
                # A failed download is tried again by itself every few
                # seconds or minutes: say it once, not on every attempt.
                if told["download"]:
                    return
                told["download"] = True
                text = (f"{detail}\n\n{app.failure['advice']} Hold to talk "
                        "will try again by itself; you can also open its "
                        "window from the tray icon and choose Try again.")
            else:
                text = ("The speech model could not be loaded, so dictation "
                        f"cannot work:\n\n{detail}\n\nOpen the app window "
                        "from the tray icon for details.")
            if windowless:
                threading.Thread(target=message_box, args=(text,),
                                 kwargs={"error": True},
                                 daemon=True).start()

    try:
        server = api.Server(app, history, port=args.ui_port or 0,
                            log_path=log_path if windowless else None,
                            autostart_name=autostart)
        window = uiwin.AppWindow(server.launch_url,
                                 uiwin.profile_dir(suffix))
        server.window = window
        server.start()
        # Tray first, so the user sees "Loading..." while Whisper loads.
        handle = tray.start(app, on_open=window.open_async,
                            autostart_name=autostart)
        app.on_status.append(on_status)
        app.on_reload.append(cleanup_reload_hook(app))
        log.info("Loading the speech model (this can take a while)...")
        app.progress_source = stt.download_progress
        app.load_engine(lambda: stt.Engine(app.cfg))
        cleanup.prepare_in_background(lambda: app.cfg)
        stop_listening = _listen(app, window.open_async, suffix)
        if app.terms_pending:
            # Nothing dictates until the Terms of Use are accepted (terms.py).
            # The window opens on the terms screen; if no window can be shown
            # (or there is no console to read), say what to do in a message.
            text = terms_pending_message()
            log.warning("%s", text.replace("\n\n", " "))
            if windowless:
                threading.Thread(target=message_box, args=(text,),
                                 daemon=True).start()
            window.open_async()
        elif args.open:
            window.open_async()
        parts = types.SimpleNamespace(app=app, tray=handle, history=history,
                                      server=server, window=window)
        for hook in STARTUP_HOOKS:
            hook(parts)
        app.run()
    finally:
        if handle is not None:
            handle.stop()
        stop_listening()
        if window is not None:
            window.close()
        if server is not None:
            server.stop()
        history.close()
        log.info("Stopped.")
    if app.loader_thread is not None and app.loader_thread.is_alive():
        # Quit while Whisper was still loading: don't wait for it (a daemon
        # thread inside native code can crash interpreter shutdown).
        logging.shutdown()
        os._exit(0)
    return 0


# -- diagnostics ----------------------------------------------------------------

def cmd_list_devices():
    import audio
    for d in audio.list_devices():
        mark = "*" if d["is_default"] else " "
        print(f"{mark} {d['index']:3d}  {d['name']}  [{d['hostapi']}, "
              f"{d['channels']} ch, {d['default_rate']:.0f} Hz]")
    print("* = default input. Set \"input_device\" in config.json to an "
          "index or a name.")
    return 0


def cmd_mic_test(cfg, seconds=0.5):
    import numpy as np
    import audio
    rec = audio.Recorder(cfg.get("input_device"))
    rec.start()
    time.sleep(seconds)
    samples = rec.stop()
    rec.close()
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    print(f"Recorded {len(samples)} samples ({len(samples) / audio.SAMPLE_RATE:.2f}s "
          f"at 16 kHz, device rate {rec.rate} Hz), peak {peak:.4f}")
    return 0 if len(samples) else 1


def _model_cache_dir():
    """Where faster-whisper's models are downloaded (Hugging Face cache)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def cmd_uninstall(test=False, out=print):
    """Remove the Start with Windows entry (tray's own functions) and list
    what else the user may want to delete. Deletes nothing else."""
    import tray
    if test:
        out("This is a test instance (--config/--history/--log): it has no "
            "Start with Windows entry. Nothing was changed.")
    else:
        name = tray.AUTOSTART_NAME
        try:
            had = tray.get_autostart(name)
            tray.set_autostart(False, name)
            out("Start with Windows: entry removed." if had else
                "Start with Windows: there was no entry.")
        except OSError as exc:
            out(f"Start with Windows: could not be removed ({exc}). Turn it "
                "off in the app's settings or in Task Manager > Startup.")
            return 1
    out("")
    out("Nothing else was deleted. Quit the app first (its icon in the "
        "notification area > Quit), then delete what you no longer want:")
    data = Path(settings.DATA_DIR)
    out(f"  Your settings, history and logs:  {data}")
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        out(f"  The Python environment:           {Path(sys.prefix)}")
    cache = _model_cache_dir()
    models = sorted(p.name for p in cache.glob("models--*whisper*")) \
        if cache.is_dir() else []
    if models:
        out(f"  Speech models (in {cache}):")
        for name in models:
            out(f"      {name}")
    else:
        out(f"  Speech models: the Hugging Face cache, {cache} "
            "(folders named models--...whisper...)")
    old = [p.name for p in settings.APP_DIR.glob("*")
           if p.name.startswith(("config.json", "history.sqlite3",
                                 "dictate.log"))]
    if old:
        out(f"  Old data next to the program ({settings.APP_DIR}): "
            + ", ".join(sorted(old)))
    out(f"  The program itself:               {settings.APP_DIR}")
    out("  AI clean-up models, if you used it: Ollama's own list "
        "(ollama list / ollama rm NAME).")
    return 0


def cmd_selftest(cfg, path, windowless=False, log_text=False):
    """Transcribe a WAV file and print what came out. Windowless (pythonw),
    standard output is the log file: the text is then not printed (word
    counts only) unless log_text (--log-text) says it may be."""
    import audio
    import pipeline
    import stt
    show = log_text or not windowless
    engine = stt.Engine(cfg)
    samples = audio.read_wav(path)
    started = time.time()
    raw = engine.transcribe(samples)
    took = time.time() - started
    trace = []
    final = pipeline.run(raw, cfg, trace)
    print(f"device: {engine.device} ({engine.name}, {engine.compute_type})")
    print(f"audio: {len(samples) / audio.SAMPLE_RATE:.2f}s, "
          f"transcribed in {took:.2f}s")
    if show:
        print(f"raw:   {raw}")
        for name, text, secs in trace:
            print(f"  after {name:<16} {secs:5.2f}s  {text!r}")
        print(f"final: {final}")
    else:
        print(f"raw:   {len(raw.split())} words")
        for name, text, secs in trace:
            print(f"  after {name:<16} {secs:5.2f}s  {len(text.split())} words")
        print(f"final: {len(final.split())} words")
        print("(The text is not printed: there is no console, so it would "
              "land in the log file. Add --log-text to print it anyway.)")
    return 0 if final else 1


def cmd_smoke(cfg, seconds, wav, errors, with_tray=False):
    """Construct App + overlay in dry-run mode, pump Tk, exit by itself."""
    import app as app_mod
    import audio
    import stt

    engine = stt.Engine(cfg)
    app = app_mod.App(cfg, engine, dry_run=True)
    results = []
    app.on_result.append(lambda raw, final, secs: results.append(final))
    app.on_result.append(lambda *a: 1 / 0)        # a broken callback is survived
    handle = None
    if with_tray:
        import tray
        handle = tray.start(app, autostart_name=None)

    user32 = ctypes.WinDLL("user32")
    user32.GetActiveWindow.restype = wintypes.HWND
    checks = {}

    def check():
        ov = app.overlay
        checks["exstyle"] = hex(ov.exstyle())
        checks["noactivate+clickthrough"] = (
            ov.exstyle() & app_mod.overlay.OVERLAY_EXSTYLE
            == app_mod.overlay.OVERLAY_EXSTYLE)
        checks["overlay_is_active_window"] = checks.get(
            "overlay_is_active_window", False) or (
            user32.GetActiveWindow() == ov.hwnd)
        checks["overlay_shown_at_some_point"] = checks.get(
            "overlay_shown_at_some_point", False) or ov.visible

    app.preview_overlay("listening", 0.6)
    app.root.after(700, lambda: app.preview_overlay("working", 0.6))
    for ms in (100, 300, 1000, 1500, 2000):
        app.root.after(ms, check)
    app.root.after(200, lambda: app.call_soon(lambda: results.append("call_soon ok")))
    app.root.after(400, lambda: log.info("reload_config -> %s", app.reload_config()))
    if wav:
        app.submit(audio.read_wav(wav))
        deadline = time.time() + max(seconds, 30)

        def wait_for_result():
            if any(r != "call_soon ok" for r in results) or time.time() > deadline:
                app.root.after(int(seconds * 1000), app.quit)
            else:
                app.root.after(100, wait_for_result)
        app.root.after(100, wait_for_result)
    else:
        app.root.after(int(seconds * 1000), app.quit)

    started = time.time()
    try:
        app.run()
    finally:
        if handle is not None:
            handle.stop()
    texts = [r for r in results if r != "call_soon ok"]
    # Word counts only: under pythonw this output goes to the log file.
    print(f"loop ran {time.time() - started:.1f}s; call_soon "
          f"{'ok' if 'call_soon ok' in results else 'MISSING'}; "
          f"{len(texts)} dictation(s), words: "
          f"{[len(t.split()) for t in texts]}")
    print(f"checks: {checks}")
    # The deliberately broken on_result callback only fires with --wav.
    errors_expected = 1 if wav else 0
    ok = (checks.get("noactivate+clickthrough")
          and not checks.get("overlay_is_active_window")
          and "call_soon ok" in results
          and (not wav or len(results) >= 2)
          and errors.count == errors_expected)
    print(f"errors logged: {errors.count} (expected {errors_expected}); "
          f"smoke {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hold-to-talk dictation")
    parser.add_argument("--list-devices", action="store_true",
                        help="list input devices and exit")
    parser.add_argument("--mic-test", action="store_true",
                        help="record 0.5 s from the configured mic and exit")
    parser.add_argument("--selftest", metavar="WAV",
                        help="transcribe a WAV file and exit")
    parser.add_argument("--smoke", metavar="SECONDS", type=float, nargs="?",
                        const=3.0, help="run the app loop in dry-run mode "
                        "(no pasting, no keystrokes) and exit by itself")
    parser.add_argument("--wav", metavar="WAV",
                        help="with --smoke: feed this file through the app")
    parser.add_argument("--tray", action="store_true",
                        help="with --smoke: also show the tray icon")
    parser.add_argument("--quit", action="store_true",
                        help="stop the running app")
    parser.add_argument("--open", action="store_true",
                        help="open the app window (starting the app if needed)")
    parser.add_argument("--ui-port", metavar="N", type=int, default=0,
                        help="port for the window server (default: any free)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run normally but never paste")
    parser.add_argument("--config", metavar="FILE",
                        help="test instance: use this config file instead of "
                        "the one in the data folder")
    parser.add_argument("--history", metavar="FILE",
                        help="test instance: use this history database "
                        "instead of the one in the data folder")
    parser.add_argument("--log", metavar="FILE",
                        help="test instance: without a console, log here "
                        "instead of the data folder's log")
    parser.add_argument("--uninstall", action="store_true",
                        help="remove the Start with Windows entry and list "
                        "what else can be deleted (deletes nothing else)")
    parser.add_argument("--log-text", action="store_true",
                        help="DEVELOPERS ONLY: also write dictated text into "
                        "the log (off by default)")
    parser.add_argument("-v", "--verbose", action="store_true")
    # Paths and streams first (argparse itself may print); the test
    # overrides are looked up by hand.
    raw_args = sys.argv[1:] if argv is None else list(argv)
    config, history, log_path, test = data_paths(
        _peek(raw_args, "--config"), _peek(raw_args, "--history"),
        _peek(raw_args, "--log"))
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    windowless = _prepare_streams(log_path)
    args = parser.parse_args(argv)
    errors = _setup_logging(args.verbose, windowless)
    use_paths(config, history, log_path, test)
    if args.log_text:
        log.warning("*" * 66)
        log.warning("--log-text is ON: dictated text WILL be written to this "
                    "log. Developer use only; never with private dictations.")
        log.warning("*" * 66)

    if args.uninstall:
        return cmd_uninstall(test)
    if args.quit:
        return cmd_quit(instance_suffix(config if test else None))
    if args.list_devices:
        return cmd_list_devices()

    try:
        if args.mic_test or args.selftest or args.smoke is not None:
            cfg = settings.load_config()
            if args.mic_test:
                return cmd_mic_test(cfg)
            if args.selftest:
                return cmd_selftest(cfg, args.selftest, windowless,
                                    args.log_text)
            return cmd_smoke(cfg, args.smoke, args.wav, errors, args.tray)
        return run_app(args, windowless, log_path, history, test)
    except Exception as exc:
        log.exception("Fatal error")
        if windowless:
            message_box(f"{settings.APP_NAME} stopped because of an error:\n\n"
                        f"{exc}\n\nDetails are in {log_path}.", error=True)
        else:
            print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
