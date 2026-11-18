"""The app: Tk root on the main thread, hotkey state machine, worker thread.

Threading model
- The Tk thread (the one that constructs App and calls run()) owns the Tk
  root, the overlay, the recorder and the hotkey polling (every 15 ms).
- One worker thread transcribes queued recordings, runs the pipeline and
  pastes. Recordings are processed strictly in order.
- Anything from another thread that touches Tk or App state goes through
  call_soon(fn). on_result callbacks run on the Tk thread.
- When the hotkey includes a mouse button (mouse3/4/5), a helper process
  (mousehook.py / mousehelper.py) hooks the mouse and swallows that button
  so it stops doing its normal job; the held state comes from the helper.
  It runs only while the hotkey is usable (model ready, not paused) or a
  hotkey capture is running. Without it (failed / died) the button is
  polled like a key and also does its normal action.

Pasting and the keyboard hotkey
- With a keyboard hotkey, a finished dictation is pasted only while no
  recording is in progress and the hotkey is not held (it waits, in order,
  until then): a Ctrl+V sent while the user holds e.g. Ctrl+Win would be
  Win+Ctrl+V, and its synthetic Ctrl key-up would make Windows report Ctrl
  as released and end the next recording. The synthetic keys are sent
  under a lock the hotkey poll also takes, so a recording can never start
  in the middle of them, and a release seen within INJECTED_GRACE of them
  is not taken as the end of a recording. Mouse-only hotkeys paste as
  soon as the text is ready (the button is not a keyboard key).

Privacy: the dictated text is never written to the log. Durations, word
counts and outcomes are. Only App.log_text = True (dictate.py --log-text, a
developer flag) adds the text.
"""

import collections
import contextlib
import ctypes
import functools
import inspect
import logging
import queue
import signal
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes

import numpy as np

import audio
import cleanup
import keys
import mousehook
import overlay
import pipeline
import settings
import terms

try:                                 # chimes are optional (F2)
    import sounds
except Exception:                    # pragma: no cover - not landed / broken
    sounds = None

log = logging.getLogger("holdtotalk.app")

TICK_MS = 15
SHOW_DELAY = 0.12        # don't flash the pill for accidental taps
DRAW_ERROR_LOG_SECONDS = 60.0   # log a repeating pill error at most this often
TICK_ERROR_LOG_SECONDS = 60.0   # ... and a repeating hotkey-loop error
SILENCE_PEAK = 0.003
RESTART_KEYS = ("model", "cpu_model", "device", "compute_type")
HOOK_MAX_FAILURES = 3    # helper restarts before staying on polling
HOOK_RETRY_SECONDS = 5.0
HOOK_STABLE_SECONDS = 60.0   # a helper that lived this long resets failures
INJECTED_GRACE = 0.3     # our own synthetic keys are not "another key"
CANCEL_WINDOW = 0.5      # another key this soon after the press = a shortcut
DEVICE_CHECK_SECONDS = 2.0   # how often (idle) to look for microphone changes
PAUSE_FLASH_SECONDS = 1.2    # the pill shows paused / resumed this long
CHIME_MARGIN = 0.08      # s of audio muted beyond the start chime itself
DEFAULT_MIN_SECONDS = 0.35
HELD_SECONDS = 300.0     # a failed recording is kept in memory this long
LANGUAGE_HINT_RUNS = 10          # same language this many dictations in a row
LANGUAGE_HINT_PROBABILITY = 0.9  # ... each detected with at least this
# Windows whose text goes to a shell: a line break pasted there runs a
# command. Window classes of the Windows console, Windows Terminal, ConEmu /
# Cmder, mintty (Git Bash, Cygwin, MSYS2), PuTTY, Alacritty and WezTerm.
CONSOLE_CLASSES = frozenset({
    "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS",
    "VirtualConsoleClass", "mintty", "PuTTY", "Alacritty",
    "org.wezfurlong.wezterm"})
LINE_BREAKS = ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
               " ", " ")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_kernel32 = ctypes.WinDLL("kernel32")


def _is_own_window(hwnd):
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _foreground_class():
    """The window class name of the foreground window ('' if unknown)."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        n = _user32.GetClassNameW(hwnd, buf, 256)
        return buf.value[:n] if n else ""
    except Exception:
        return ""


def _line_breaks(text):
    return sum(text.count(ch) for ch in LINE_BREAKS)


def _words(text):
    return len(str(text).split())


@functools.lru_cache(maxsize=None)
def _pipeline_takes_language():
    try:
        return "language" in inspect.signature(pipeline.run).parameters
    except (TypeError, ValueError):
        return False


class _DoneTake:
    """A finished recording for recorders without stop_async()."""
    done = True
    timed_out = False

    def __init__(self, samples):
        self._samples = samples

    def wait(self, timeout=None):
        return self._samples


class App:
    """Hold-to-talk app.

    Public: cfg, root, paused, engine, status, status_detail, activity,
    terms_pending, terms_record,
    capturing, mic_problem, language_hint, held_recording, log_text,
    on_result, on_dictation, on_notice, on_status, on_reload,
    mouse_hook_state, set_paused(), toggle_pause(), load_engine(),
    retry_engine(), retry_held_recording(), play_sound(), reload_config(),
    capture_hotkey(), cancel_capture(), call_soon(), submit(),
    submit_transcript(), preview_overlay(), quit(), run().
    dry_run=True never sends keystrokes, never touches the clipboard and
    never plays a chime.

    language_hint: the language code ("de") when the last 10 dictations were
    all detected as that language with high probability (only while the
    language setting is automatic), else None; the window can offer "always
    use German". held_recording: None, or {"seconds", "expires_in"} while the
    last recording that could not be transcribed is kept in memory (never on
    disk; 5 minutes at most; cleared when it is transcribed, replaced by a
    newer failure, and on quit); retry_held_recording() queues it again once
    the speech model works, and retry_engine() does so by itself when its
    load succeeds. foreground_class() (replaceable, for tests) names the
    foreground window's class.

    terms_pending / accept_terms(): the Terms of Use (terms.py). While they
    are pending the hotkey does nothing; accept_terms() records the
    acceptance and enables dictation at once.

    on_notice(kind, message) kinds: "terms" (the hotkey was pressed before
    the Terms of Use were accepted), "mic" (microphone problem), "silence",
    "empty", "discarded" (a recording was thrown away; the message says
    why), "short" (a press too short to be a dictation), "dropped" (no
    speech model), "error" (transcription failed), "paste" (not pasted),
    "hook" (mouse helper), "engine" (speech model reloaded / fell back).

    engine may be None: then call load_engine(factory) and the app starts in
    status "loading" (the hotkey is ignored) until the model is ready.
    """

    # fn(buttons, key_groups, capture) -> a mousehook.MouseHook-like object.
    # Tests replace it.
    hook_factory = staticmethod(
        lambda buttons, key_groups, capture: mousehook.MouseHook(
            buttons, key_groups, capture))

    # fn() -> window class name of the foreground window. Tests replace it.
    foreground_class = staticmethod(_foreground_class)

    def __init__(self, cfg, engine=None, dry_run=False):
        self._init_state(cfg, engine, dry_run)

        overlay.enable_dpi_awareness()
        before = _user32.GetForegroundWindow()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(settings.APP_NAME)
        self.root.report_callback_exception = self._report_tk_exception
        # Tk activates the first toplevel it realises. Realise the hidden
        # root now so that is the root, never the overlay.
        self.root.update_idletasks()
        colors = settings.overlay_colors(cfg)
        self.overlay = overlay.Overlay(self.root, colors["color"],
                                       colors["background"])
        self.root.update_idletasks()
        # Tk activates its (hidden) root window when it is created. If that
        # took the foreground (we were launched from the foreground console),
        # hand it back so the user's typing doesn't go to an invisible window.
        now = _user32.GetForegroundWindow()
        if before and now != before and _is_own_window(now):
            _user32.SetForegroundWindow(before)

        self.recorder = audio.Recorder(cfg.get("input_device"))
        self._worker_thread = threading.Thread(target=self._worker,
                                               name="transcribe", daemon=True)
        self._worker_thread.start()
        self.root.after(TICK_MS, self._tick)

    def _init_state(self, cfg, engine=None, dry_run=False):
        """Everything that does not need Tk (tests build an App from this)."""
        self.cfg = cfg
        self.engine = engine
        self.dry_run = dry_run
        self.log_text = False        # developer flag: dictate.py --log-text
        self.paused = False
        # The Terms of Use (terms.py): until a record for the current version
        # is in the data folder the hotkey does nothing (see _poll).
        self._terms_ok = terms.is_current()
        self._terms_told = False     # said "accept the terms" for this press
        self.status = "ready" if engine is not None else "loading"
        self.status_detail = ""
        self.on_result = []          # fn(raw, final, audio_seconds), Tk thread
        self.on_dictation = []       # fn(record dict, see _process), Tk thread
        self.on_notice = []          # fn(kind, message), Tk thread
        self.on_status = []          # fn(status, detail), Tk thread
        self.on_reload = []          # fn(old_cfg, new_cfg, result), Tk thread
        self._engine_ready = threading.Event()
        if engine is not None:
            self._engine_ready.set()
        self.loader_thread = None
        self._factory = None         # what load_engine() was given
        self._load_lock = threading.Lock()   # one model load at a time

        self._engine_cls = type(engine) if engine is not None else None

        self._set_hotkey(self._compile_hotkey(cfg["hotkey"]))
        self._pause_groups = None
        self._pause_down = False
        self._calls = queue.Queue()
        self._jobs = queue.Queue()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._input_lock = threading.Lock()   # recording start vs our keys
        self._state = "idle"         # idle | recording
        self._blocked = False        # wait for the hotkey to be released
        self._started_at = 0.0
        self._started_mono = 0.0
        self._snapshot = set()
        self._preview = None         # (state, until) for preview_overlay()
        self._flash = None           # (state, until): pause feedback
        self._chime_take = None      # stop chime once this take is complete
        self._tk_thread = threading.get_ident()
        self._closed = False
        self._capture = None         # see capture_hotkey()
        self._hook = None            # mousehook.MouseHook while wanted
        self._hook_key = None        # what self._hook was started for
        self._hook_failures = 0
        self._hook_retry_at = 0.0
        self._hook_announced = False
        self._next_device_check = 0.0
        self._mic_problem_seen = None
        self._tick_keys = None       # per-tick cache of key states (_step)
        self._recent_languages = collections.deque(maxlen=LANGUAGE_HINT_RUNS)
        self._held = None            # (samples, expires): see _hold_recording
        self._held_lock = threading.Lock()
        self._retry_held = False     # retry_engine(): queue the held audio
        try:
            self._set_pause_hotkey(cfg.get("pause_hotkey"))
        except ValueError as exc:
            log.warning("Pause shortcut not used: %s", exc)

    # -- public API -----------------------------------------------------------

    @property
    def activity(self):
        """'listening' (the hotkey is held and the mic records), 'working'
        (recordings being transcribed / pasted) or 'idle'. Any thread."""
        if self._state == "recording":
            return "listening"
        if self._pending > 0:
            return "working"
        return "idle"

    @property
    def terms_pending(self):
        """True while the Terms of Use of this version have not been accepted:
        dictation is off (the hotkey does nothing) until accept_terms()."""
        return not self._terms_ok

    @property
    def terms_record(self):
        """The accepted-terms record ({"version", "accepted_at", "how"}) or
        None. Reads the data folder's small file; not for the hot path."""
        return terms.read_record()

    def accept_terms(self, how="app"):
        """Record that the current Terms of Use were accepted and let the
        hotkey work at once (no restart). Returns the record. OSError if it
        could not be written (then nothing changes). Safe from any thread."""
        record = terms.accept(how)
        self._terms_ok = True
        return record

    @property
    def capturing(self):
        """True while capture_hotkey() waits for a key."""
        return self._capture is not None

    @property
    def mic_problem(self):
        """The last microphone problem as a short plain sentence (e.g. "The
        chosen microphone is not connected."), or None when the microphone
        is fine. Any thread."""
        rec = getattr(self, "recorder", None)
        return getattr(rec, "problem", None)

    @property
    def mouse_hook_state(self):
        """'off' (no mouse button in use: keyboard hotkey, paused, loading),
        'starting', 'active' (the button is swallowed), or 'fallback' (the
        hook is wanted but not working: the button is polled and also does
        its normal action)."""
        if self._hook_wanted() is None:
            return "off"
        hook = self._hook
        if hook is not None and hook.state in ("starting", "active"):
            return hook.state
        return "fallback"

    def capture_hotkey(self, callback, timeout=10.0):
        """Wait for the user to press and release a key combination and
        report it: callback(spec) on the Tk thread, exactly once.

        spec is a canonical hotkey string accepted by keys.parse_hotkey
        (e.g. 'ctrl+windows', 'f9', 'shift+page up', 'mouse4',
        'ctrl+mouse5'), or None when the capture was cancelled: Esc pressed
        alone, nothing pressed within `timeout` seconds (a combination still
        held then gets 10 s more to be released), cancel_capture(), or a
        newer capture_hotkey() call. Keys already held when the capture
        starts are ignored until they are released. The left and right
        mouse buttons are never captured.

        While capturing, the dictation hotkey and the pause shortcut are
        inactive (a recording in progress is discarded) and the middle and
        side mouse buttons are swallowed by the mouse hook, so pressing Back
        does not navigate. app.paused is not changed, so the pause state
        afterwards is what it was (or what the user set meanwhile).
        Afterwards the hotkey must be released before it can start a
        dictation. Safe from any thread; returns None.
        """
        def start():
            if self._capture is not None:
                self._end_capture(None)
            self._discard("a hotkey is being recorded")
            now = time.monotonic()
            self._capture = {
                "callback": callback, "seen": set(),
                "ignore": {vk for vk in self._capture_vks()
                           if self._key_down(vk)},
                "deadline": now + max(float(timeout), 0.0),
            }
            log.info("Recording a hotkey (press and release the keys or "
                     "mouse button)")
        self._on_tk(start)

    def cancel_capture(self):
        """Stop a capture_hotkey() in progress; its callback gets None.
        Safe from any thread; does nothing if none is running."""
        self._on_tk(lambda: self._capture is not None
                    and self._end_capture(None))

    def call_soon(self, fn):
        """Run fn() on the Tk thread at the next tick. Safe from any thread."""
        self._calls.put(fn)

    def set_paused(self, paused):
        """While paused the hotkey does nothing (a recording in progress is
        discarded) and a mouse-button hotkey does its normal job again (the
        hook is removed at the next tick). Safe from any thread."""
        self.paused = bool(paused)
        log.info("Paused" if self.paused else "Resumed")

    def toggle_pause(self, feedback=True):
        """Pause or resume (what the pause shortcut does); with feedback the
        pill shows the new state for a moment, without taking focus. Returns
        the new paused state. Safe from any thread."""
        self.set_paused(not self.paused)
        if feedback:
            self._flash = ("paused" if self.paused else "resumed",
                           time.monotonic() + PAUSE_FLASH_SECONDS)
        return self.paused

    def load_engine(self, factory):
        """Load the speech model on a background thread: factory() -> engine.
        Status goes 'loading' -> 'ready' (or 'failed', with status_detail
        set); on_status callbacks run on the Tk thread. The factory is kept
        for retry_engine() and for reloading after repeated errors."""
        self._factory = factory

        def run():
            started = time.time()
            with self._load_lock:
                try:
                    engine = factory()
                except Exception as exc:
                    self._log_error("Could not load the speech model", exc,
                                    always_message=True)
                    # Bind the message now: `exc` is gone after this block.
                    self.call_soon(functools.partial(
                        self._set_status, "failed", self._why(exc)))
                    return

            def finish():
                engine.cfg = self.cfg        # a reload may have happened
                self.engine = engine
                self._engine_cls = type(engine)
                self._engine_ready.set()
                self._set_status("ready", self._ready_detail(
                    engine, f"{time.time() - started:.1f}s"))
                if self._retry_held:          # retry_engine() was asked for
                    self._retry_held = False
                    self.retry_held_recording()
            self.call_soon(finish)
        self._engine_ready.clear()
        if self.status != "loading":
            self._on_tk(functools.partial(self._set_status, "loading",
                                          "Loading the speech model"))
        self.loader_thread = threading.Thread(target=run, name="load-model",
                                              daemon=True)
        self.loader_thread.start()
        return self.loader_thread

    def retry_engine(self):
        """Try loading the speech model again (after it failed). Safe from
        any thread. Returns True if a new attempt was started; False when
        there is nothing to retry with or a load is already running."""
        if self._factory is None or self._load_lock.locked():
            return False
        if self.loader_thread is not None and self.loader_thread.is_alive():
            return False
        if self.status == "ready" and self.engine is not None:
            return False                  # nothing to retry
        log.info("Loading the speech model again (retry)")
        self._retry_held = self._held is not None   # then transcribe it
        self.engine = None                # free a broken model's memory first
        self.load_engine(self._factory)
        return True

    def play_sound(self, kind):
        """Play the "start" or "stop" chime now with the current settings
        (the settings window's preview). True if one was played. Never
        raises; silent when the sounds module is missing or in dry-run."""
        if sounds is None or self.dry_run:
            return False
        try:
            return bool(sounds.play(kind, self.cfg))
        except Exception:
            return False

    def submit(self, samples):
        """Queue float32 mono 16 kHz audio as if it had just been dictated."""
        with self._pending_lock:
            self._pending += 1
        self._jobs.put(("audio", np.asarray(samples, dtype=np.float32)))

    def submit_transcript(self, raw, audio_seconds=0.0):
        """Queue a raw transcript as if Whisper had just returned it: it goes
        through the same pipeline, paste (or dry-run) and on_result path
        as a dictation. For tests and diagnostics."""
        with self._pending_lock:
            self._pending += 1
        self._jobs.put(("text", str(raw), float(audio_seconds)))

    def preview_overlay(self, state, seconds=1.0):
        """Show the overlay in `state` ('listening'/'working'/'paused'/
        'resumed') for a moment."""
        self.call_soon(lambda: setattr(
            self, "_preview", (state, time.time() + seconds)))

    def reload_config(self):
        """Re-read config.json and apply it without restarting.

        Returns {"applied": [keys], "restart_required": [keys],
        "errors": [messages]}. Hotkey, pause shortcut, microphone, language,
        vocabulary, dictionary, snippets, sounds, the cleanup and textrules
        sections and the text options apply immediately (the pipeline reads
        self.cfg for every dictation); model/cpu_model/device need a
        restart. An invalid hotkey, pause shortcut or microphone keeps the
        previous value. on_reload callbacks run after every reload.
        Can be called from any thread (it runs on the Tk thread and waits).
        """
        if threading.get_ident() != self._tk_thread:
            done = threading.Event()
            box = {}

            def run():
                try:
                    box["result"] = self._reload()
                except Exception as exc:          # pragma: no cover
                    box["error"] = exc
                finally:
                    done.set()
            self.call_soon(run)
            if not done.wait(10):
                raise TimeoutError("The Tk thread did not respond")
            if "error" in box:
                raise box["error"]
            return box["result"]
        return self._reload()

    def quit(self):
        """Stop the app; run() returns. Safe from any thread."""
        self.call_soon(self.root.quit)

    def run(self):
        """Run the Tk loop until quit() (or Ctrl+C in the console)."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, lambda *_: self.quit())
        try:
            self.root.mainloop()
        finally:
            self._shutdown()

    # -- internals --------------------------------------------------------------

    @staticmethod
    def _compile_hotkey(spec):
        groups = keys.parse_hotkey(spec)
        hotkey_vks = {vk for group in groups for vk in group}
        ignored = (set(hotkey_vks)
                   | {keys.VK_MASK, keys.VK_PACKET})
        for family in keys.MODIFIER_FAMILIES:
            if family & hotkey_vks:
                ignored |= family
        if hotkey_vks <= keys.MOUSE_VKS:
            # A swallowed mouse button forms no shortcut with other keys,
            # so typing while holding it is not a reason to cancel.
            other = []
        else:
            other = [vk for vk in range(0x08, 0xFF) if vk not in ignored]
        mask = bool(hotkey_vks & keys.RELEASE_SENSITIVE)
        return groups, other, mask

    def _set_hotkey(self, compiled):
        self._groups, self._other_keys, self._mask = compiled
        self._mouse_vks = keys.mouse_buttons(self._groups)
        self._key_groups = [g for g in self._groups
                            if not set(g) & keys.MOUSE_VKS]
        # What _hook_wanted() answers for a mouse hotkey, built once (it ran
        # on every tick).
        self._hook_hotkey = (
            ("hotkey", self._mouse_vks,
             tuple(tuple(g) for g in self._key_groups))
            if self._mouse_vks else None)

    def _set_pause_hotkey(self, spec):
        """Compile the pause shortcut (None / '' = none). ValueError for an
        invalid one, or one that is part of the dictation hotkey (pressing
        the hotkey would toggle pause)."""
        if spec is None or (isinstance(spec, str) and not spec.strip()):
            self._pause_groups = None
            return
        groups = keys.parse_hotkey(spec)
        if all(g in self._groups for g in groups):
            raise ValueError("it is (part of) the dictation hotkey")
        self._pause_groups = groups
        # Held right now (e.g. just captured): wait for a fresh press.
        self._pause_down = self._pause_combo_down()

    def _on_tk(self, fn):
        if threading.get_ident() == self._tk_thread:
            fn()
        else:
            self.call_soon(fn)

    def _reload(self):
        old, new = self.cfg, settings.load_config()
        result = {"applied": [], "restart_required": [], "errors": []}

        if new.get("hotkey") != old.get("hotkey"):
            try:
                compiled = self._compile_hotkey(new.get("hotkey"))
            except ValueError as exc:
                result["errors"].append(f"hotkey: {exc}")
                new["hotkey"] = old["hotkey"]
            else:
                self._discard("the hotkey was changed")
                self._set_hotkey(compiled)
                self._blocked = True
                self._sync_hook()        # start/stop the mouse hook now

        if (new.get("pause_hotkey") != old.get("pause_hotkey")
                or new.get("hotkey") != old.get("hotkey")):
            try:
                self._set_pause_hotkey(new.get("pause_hotkey"))
            except ValueError as exc:
                result["errors"].append(f"pause_hotkey: {exc}")
                new["pause_hotkey"] = old.get("pause_hotkey")
                try:
                    self._set_pause_hotkey(old.get("pause_hotkey"))
                except ValueError:
                    self._pause_groups = None

        if new.get("input_device") != old.get("input_device"):
            try:
                audio.resolve_device(new.get("input_device"))
            except Exception as exc:
                result["errors"].append(f"input_device: {exc}")
                new["input_device"] = old.get("input_device")
            else:
                self._discard("the microphone was changed")
                self.recorder.set_device(new.get("input_device"))

        for key in sorted(set(old) | set(new), key=str):
            if old.get(key) == new.get(key):
                continue
            if key in RESTART_KEYS:
                # cfg shows the new choice; engine.name/.device show what
                # is actually loaded until the app is restarted.
                result["restart_required"].append(key)
            else:
                result["applied"].append(key)

        colors = settings.overlay_colors(new)
        if colors != settings.overlay_colors(old):
            ov = getattr(self, "overlay", None)    # bare App in tests: none
            if ov is not None:
                ov.set_colors(colors["color"], colors["background"])

        if new.get("language") != old.get("language"):
            self._recent_languages.clear()
        self.cfg = new
        if self.engine is not None:
            self.engine.cfg = new
        for msg in result["errors"]:
            log.error("Config not applied: %s", msg)
        if result["applied"]:
            log.info("Config reloaded: %s", ", ".join(result["applied"]))
        if result["restart_required"]:
            log.warning("Restart needed for: %s",
                        ", ".join(result["restart_required"]))
        for fn in list(self.on_reload):
            try:
                fn(old, new, result)
            except Exception as exc:
                self._log_error(f"on_reload callback {fn!r} failed", exc)
        return result

    def _set_status(self, status, detail=""):
        self.status, self.status_detail = status, detail
        for fn in list(self.on_status):
            try:
                fn(status, detail)
            except Exception as exc:
                self._log_error(f"on_status callback {fn!r} failed", exc)

    @staticmethod
    def _why(exc):
        return str(exc) or type(exc).__name__

    @staticmethod
    def _ready_detail(engine, took=""):
        reason = getattr(engine, "fallback_reason", None)
        if reason:
            return (f"{took}; on the processor ({reason})" if took
                    else f"on the processor ({reason})")
        return took

    def _log_error(self, what, exc, always_message=False):
        """Log an error with its traceback but, unless log_text is on, without
        the exception's message when that message could quote dictated text
        (pipeline / paste errors). always_message: the message is safe (e.g.
        a model that failed to load)."""
        if self.log_text or always_message:
            log.error("%s", what, exc_info=(type(exc), exc,
                                            exc.__traceback__))
            return
        frames = "".join(traceback.format_tb(exc.__traceback__))
        log.error("%s (%s; message not logged)\nTraceback (most recent call "
                  "last):\n%s", what, type(exc).__name__, frames.rstrip())

    def _report_tk_exception(self, exc, val, tb):
        log.error("Error in Tk callback", exc_info=(exc, val, tb))

    def _notice(self, kind, message):
        """Tell on_notice listeners (the app window) about something the
        user should know that produced no dictation. Any thread."""
        def run():
            for fn in list(self.on_notice):
                try:
                    fn(kind, message)
                except Exception as exc:
                    self._log_error(f"on_notice callback {fn!r} failed", exc)
        self._on_tk(run)

    def _step(self):
        """One tick without drawing: queued calls, mouse hook, recorder,
        pause shortcut, hotkey."""
        calls = self._calls
        while not calls.empty():           # (get_nowait raising is slower)
            try:
                fn = calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as exc:
                self._log_error("call_soon callback failed", exc)
        self._sync_hook()
        self._service_recorder()
        if self._held is not None:
            self._expire_held()
        # One GetAsyncKeyState per key per tick, however many shortcuts
        # look at it.
        self._tick_keys = {}
        try:
            if self._capture is not None:
                self._pause_down = self._pause_combo_down()
                self._capture_poll()
            else:
                if self._pause_groups:       # nothing to poll without one
                    self._poll_pause()
                self._poll()
        finally:
            self._tick_keys = None

    _tick_error_at = None        # monotonic time of the last logged failure
    _tick_errors = 0             # failures not logged since then

    def _tick(self):
        try:
            try:
                self._step()
            except Exception:
                self._tick_failed()
            try:
                self._draw()
            except Exception:
                self._tick_failed()
        finally:
            if not self._closed:
                self.root.after(TICK_MS, self._tick)

    def _tick_failed(self):
        """Log a hotkey-loop error at most once a minute (with how many were
        not logged); the next tick simply runs again."""
        now = time.monotonic()
        if (self._tick_error_at is None
                or now - self._tick_error_at >= TICK_ERROR_LOG_SECONDS):
            log.exception("Error in the hotkey loop; %d similar errors not "
                          "logged since the last report", self._tick_errors)
            self._tick_error_at, self._tick_errors = now, 0
        else:
            self._tick_errors += 1

    # -- the microphone

    def _service_recorder(self):
        rec = getattr(self, "recorder", None)
        service = getattr(rec, "service", None)
        if service is not None:
            service()
        take = self._chime_take
        if take is not None and take.done:
            # The tail is in: the stop chime cannot end up in the recording
            # any more. Skipped if the next recording already started.
            self._chime_take = None
            if self._state != "recording":
                self._chime("stop")
        now = time.monotonic()
        check = getattr(rec, "check_devices", None)
        if (check is not None and self._state == "idle"
                and now >= self._next_device_check):
            self._next_device_check = now + DEVICE_CHECK_SECONDS
            try:
                check()
            except Exception as exc:
                log.warning("Checking the microphones failed: %s",
                            type(exc).__name__)
        problem = self.mic_problem
        if problem != self._mic_problem_seen:
            self._mic_problem_seen = problem
            if problem:
                log.warning("Microphone problem: %s", problem)
                self._notice("mic", problem)
            else:
                log.info("Microphone OK again")

    def _chimes_on(self):
        if sounds is None or self.dry_run:
            return False
        try:
            s = sounds.settings_of(self.cfg)
            return bool(s["enabled"]) and s["volume"] > 0
        except Exception:
            return False

    def _mute_start_wanted(self):
        """sounds.mute_start: silence the start of the recording so the
        start chime is not captured (default off)."""
        try:
            return sounds.settings_of(self.cfg).get("mute_start") is True
        except Exception:
            return False

    def _chime(self, kind):
        """Play a chime if they are on; True if one was played."""
        if not self._chimes_on():
            return False
        try:
            return bool(sounds.play(kind, self.cfg))
        except Exception:
            return False

    _chime_lengths = {}

    @staticmethod
    def _chime_seconds(kind="start"):
        """How long the chime sounds (from its own WAV); 0.2 s if unknown."""
        known = App._chime_lengths.get(kind)
        if known is not None:
            return known
        try:
            import io
            import wave
            with wave.open(io.BytesIO(sounds.wav_bytes(kind, 1.0))) as w:
                seconds = w.getnframes() / float(w.getframerate())
        except Exception:
            return 0.2
        App._chime_lengths[kind] = seconds
        return seconds

    # -- keys and mouse buttons

    def _key_down(self, vk):
        """Is vk held? Mouse buttons swallowed by the hook are invisible to
        GetAsyncKeyState, so for them the hook's report counts too."""
        cache = self._tick_keys
        if cache is not None:
            down = cache.get(vk)
            if down is None:
                down = cache[vk] = self._read_key(vk)
            return down
        return self._read_key(vk)

    def _read_key(self, vk):
        if vk in keys.MOUSE_VKS:
            hook = self._hook
            if hook is not None and hook.is_down(vk):
                return True
        return keys.key_down(vk)

    def _combo_down(self):
        return all(any(self._key_down(vk) for vk in group)
                   for group in self._groups)

    def _pause_combo_down(self):
        groups = self._pause_groups
        return bool(groups) and all(any(self._key_down(vk) for vk in g)
                                    for g in groups)

    def _poll_pause(self):
        """The pause shortcut: toggles once per press (edge), also while
        paused; ignored while a dictation is being recorded (unless the
        press is part of the very start of it: then it was a shortcut, and
        the recording is discarded)."""
        down = self._pause_combo_down()
        edge = down and not self._pause_down
        self._pause_down = down
        if not edge:
            return
        if self._state == "recording":
            if time.monotonic() - self._started_mono >= CANCEL_WINDOW:
                log.info("Pause shortcut ignored: a dictation is being "
                         "recorded")
                return
            self._discard("the pause shortcut was pressed")
            self._blocked = True
        paused = self.toggle_pause()
        log.info("Pause shortcut: %s", "paused" if paused else "resumed")

    def _poll(self):
        combo = self._combo_down()
        if not self._terms_ok:
            # Nothing is recorded until the Terms of Use are accepted. Say why
            # once per press (the tray and the window say it all the time).
            self._discard("the Terms of Use have not been accepted")
            self._blocked = combo        # a key held now is not a dictation
            if combo and not self._terms_told:
                self._terms_told = True
                self._notice("terms", "Dictation is off until you accept the "
                             "Terms of Use. Open the window from the icon in "
                             "the notification area and choose I agree.")
            elif not combo:
                self._terms_told = False
            return
        if self.paused or self.status != "ready":
            self._discard("dictation was paused" if self.paused else
                          "the speech model is not ready")
            self._blocked = True
            return
        if self._state == "idle":
            if not combo:
                self._blocked = False
            elif not self._blocked:
                # Not while our own Ctrl+V is going out (see _paste_guard).
                with self._input_lock:
                    self._start()
        elif self._state == "recording":
            now = time.monotonic()
            injected = now - keys.last_injected <= INJECTED_GRACE
            if not combo:
                if injected:
                    # A synthetic key-up (ours) makes Windows report the key
                    # as released; wait until that cannot be the reason.
                    return
                self._finish()
            elif (now - self._started_mono < CANCEL_WINDOW and not injected
                  and any(vk not in self._snapshot and keys.key_down(vk)
                          for vk in self._other_keys)):
                # Right at the start: the hotkey became part of some other
                # shortcut. Later a stray key is just typing: keep going.
                self._discard("another key was pressed with the hotkey "
                              "(a shortcut)")
                self._blocked = True

    # -- the mouse hook helper

    def _hook_wanted(self):
        """What the mouse hook should be doing now: None (no hook),
        ('capture',) or ('hotkey', buttons, key_groups)."""
        if self._closed:
            return None
        if self._capture is not None:
            return ("capture",)
        if (self._mouse_vks and not self.paused and self.status == "ready"
                and self._terms_ok):
            return self._hook_hotkey
        return None

    def _sync_hook(self):
        """Start, stop, health-check or replace the mouse hook helper so it
        matches _hook_wanted(). Tk thread, every tick (cheap)."""
        now = time.monotonic()
        want = self._hook_wanted()
        hook = self._hook
        if want != self._hook_key:
            if hook is not None:
                self._stop_hook()
                hook = None
            self._hook_key = want
            self._hook_failures = 0
            self._hook_retry_at = 0.0
            self._hook_announced = False
        if hook is not None:
            state = hook.poll(now)
            if state == "dead":
                self._hook_died(hook, now)
            elif state == "active" and not self._hook_announced:
                self._hook_announced = True
                log.info("Mouse hook active (helper pid %s): %s",
                         hook.helper_pid or hook.pid,
                         "capturing mouse buttons" if want == ("capture",)
                         else f"{keys.describe_hotkey(self.cfg['hotkey'])} "
                              "no longer does its normal action")
            return
        if (want is None or now < self._hook_retry_at
                or self._hook_failures >= HOOK_MAX_FAILURES):
            return
        if want == ("capture",):
            args = (keys.MOUSE_VKS, (), True)
        else:
            args = (want[1], want[2], False)
        try:
            self._hook = self.hook_factory(*args)
        except Exception as exc:
            self._hook = None
            self._note_hook_failure(f"could not start the helper: {exc}", now)

    def _hook_died(self, hook, now):
        self._hook = None
        lived = now - (hook.active_since or now)
        if lived >= HOOK_STABLE_SECONDS:
            self._hook_failures = 0
        try:
            hook.stop()
        except Exception:
            pass
        if self._state == "recording" and self._mouse_vks:
            # Its release can no longer be seen: don't paste half a sentence
            # the user is still speaking, and don't record forever.
            self._discard("the mouse hook stopped", kind="hook",
                          message="A recording was discarded because the "
                          "mouse button helper stopped.")
            self._blocked = True
        self._note_hook_failure(hook.error or "helper stopped", now)

    def _note_hook_failure(self, why, now):
        self._hook_failures += 1
        self._hook_announced = False
        self._hook_retry_at = now + HOOK_RETRY_SECONDS
        button = ", ".join(keys.MOUSE_CANONICAL[vk] for vk in
                           sorted(self._mouse_vks or keys.MOUSE_VKS))
        if self._hook_failures >= HOOK_MAX_FAILURES:
            log.warning("Mouse hook unavailable (%s); giving up and polling "
                        "%s instead: it still works for dictation but also "
                        "does its normal action (e.g. Back). Pause/resume or "
                        "change the hotkey to try again.", why, button)
        else:
            log.warning("Mouse hook unavailable (%s); polling %s instead "
                        "(it also does its normal action); retrying in "
                        "%.0f s", why, button, HOOK_RETRY_SECONDS)

    def _stop_hook(self):
        hook, self._hook = self._hook, None
        if hook is not None:
            try:
                hook.stop()
            except Exception:
                log.exception("Stopping the mouse hook failed")
            log.info("Mouse hook removed")

    # -- hotkey capture

    @staticmethod
    def _capture_vks():
        return list(keys.CAPTURE_VKS) + sorted(keys.MOUSE_VKS)

    def _capture_poll(self):
        cap = self._capture
        self._discard("a hotkey is being recorded")
        self._blocked = True
        down = {vk for vk in self._capture_vks() if self._key_down(vk)}
        cap["ignore"] &= down            # released once: counts from now on
        down -= cap["ignore"]
        cap["seen"] |= down
        now = time.monotonic()
        if cap["seen"] and not down:
            seen = cap["seen"]
            spec = None if seen == {0x1B} else keys.combo_from_vks(seen)
            try:
                spec = keys.canonical_hotkey(spec) if spec else None
            except ValueError:
                spec = None
            self._end_capture(spec)
        elif not cap["seen"] and now > cap["deadline"]:
            self._end_capture(None)
        elif now > cap["deadline"] + 10.0:
            self._end_capture(None)

    def _end_capture(self, spec):
        cap, self._capture = self._capture, None
        self._blocked = True
        log.info("Hotkey recorded: %s", keys.describe_hotkey(spec)
                 if spec else "(cancelled)")
        try:
            cap["callback"](spec)
        except Exception as exc:
            self._log_error("capture_hotkey callback failed", exc)
        return True

    # -- recording

    def _start(self):
        self._snapshot = {vk for vk in self._other_keys if keys.key_down(vk)}
        try:
            self.recorder.start()
        except Exception as exc:
            known = getattr(self.recorder, "problem", None)
            problem = known or "The microphone could not be opened."
            log.error("Microphone error: %s (%s)", problem,
                      type(exc).__name__)
            if known:
                self._mic_problem_seen = known    # this notice says it
            self._notice("mic", problem)
            self._blocked = True
            return
        if self._chime("start") and self._mute_start_wanted():
            # Optional (sounds.mute_start, off by default): the chime must
            # not end up in the recording when it comes out of speakers, so
            # the first chime-length of audio is replaced by silence. This
            # can clip the first syllable of someone who speaks as they press.
            mute = getattr(self.recorder, "mute_start", None)
            if mute is not None:
                mute(self._chime_seconds("start") + CHIME_MARGIN)
        if self._mask and not self.dry_run:
            keys.tap_mask_key()
        self._started_at = time.time()
        self._started_mono = time.monotonic()
        self._state = "recording"

    def _discard(self, why, kind="discarded", message=None):
        """Throw away the recording in progress, if any: logged (never the
        content) and reported to on_notice."""
        if self._state != "recording":
            return False
        held = time.monotonic() - self._started_mono
        cancel = getattr(self.recorder, "cancel", None)
        if cancel is not None:
            cancel()
        else:
            self.recorder.stop()
        self._state = "idle"
        log.info("Recording discarded after %.1f s: %s", held, why)
        if not self._closed:
            self._chime("stop")
            self._notice(kind, message or f"A recording was discarded: "
                         f"{why}.")
        return True

    # Old name; some callers and tests still use it.
    def _cancel_recording(self):
        self._discard("cancelled")

    def _finish(self):
        stop_async = getattr(self.recorder, "stop_async", None)
        if stop_async is not None:
            take = stop_async()
        else:
            take = _DoneTake(self.recorder.stop())
        self._state = "idle"
        self._chime_take = take
        held = time.monotonic() - self._started_mono
        with self._pending_lock:
            self._pending += 1
        self._jobs.put(("take", take, held))

    def _min_seconds(self):
        try:
            value = float(self.cfg.get("min_seconds", DEFAULT_MIN_SECONDS))
            if value != value:            # NaN
                raise ValueError
        except (TypeError, ValueError):
            value = DEFAULT_MIN_SECONDS
        return max(value, 0.1)

    def _take_samples(self, take, held):
        """The finished recording's audio, or None if it is not worth
        transcribing (too short, silent); worker thread."""
        samples = np.asarray(take.wait(), dtype=np.float32)
        if getattr(take, "timed_out", False):
            log.debug("The recording's tail did not arrive in time")
        seconds = len(samples) / audio.SAMPLE_RATE
        if seconds < self._min_seconds():
            log.info("Recording discarded: %.2f s is shorter than the "
                     "minimum of %.2f s", seconds, self._min_seconds())
            self._notice("short", "A very short press was ignored (hold the "
                         "hotkey while you speak).")
            return None
        if float(np.max(np.abs(samples))) < SILENCE_PEAK:
            log.warning("Heard only silence (%.1f s). Is the microphone "
                        "muted?", seconds)
            self._notice("silence", "Only silence was recorded. Is the "
                         "microphone muted?")
            return None
        return samples

    # -- a recording that could not be transcribed

    def _hold_recording(self, samples):
        """Keep the most recent recording whose transcription failed, in
        memory only (one slot), so retry_held_recording() can transcribe it
        once the speech model works. True if it is being held."""
        with self._held_lock:
            self._held = (samples, time.monotonic() + HELD_SECONDS)
        log.info("Keeping the last recording (%.1f s) in memory for up to "
                 "%d minutes", len(samples) / audio.SAMPLE_RATE,
                 HELD_SECONDS // 60)
        return True

    def _expire_held(self):
        with self._held_lock:
            held = self._held
            if held is not None and time.monotonic() >= held[1]:
                self._held = None
                log.info("The held recording was dropped (kept long enough)")

    @property
    def held_recording(self):
        """None, or {"seconds": audio length, "expires_in": seconds left}
        while a recording that could not be transcribed is kept in memory.
        Any thread."""
        self._expire_held()
        held = self._held
        if held is None:
            return None
        return {"seconds": round(len(held[0]) / audio.SAMPLE_RATE, 2),
                "expires_in": max(0.0, round(held[1] - time.monotonic(), 1))}

    def retry_held_recording(self):
        """Transcribe the held recording (see held_recording) as if it had
        just been dictated: same pipeline, same paste. Only when the speech
        model is ready; if it fails again it is held again. True if it was
        queued. Safe from any thread."""
        self._expire_held()
        with self._held_lock:
            held = self._held
            if (held is None or self._closed or self.status != "ready"
                    or self.engine is None):
                return False
            self._held = None
        log.info("Transcribing the held recording again")
        self.submit(held[0])
        return True

    # -- what language the dictations are in

    def _note_language(self, engine):
        """Remember what the engine detected for a dictation (worker
        thread); nothing while the language setting is fixed."""
        if self.cfg.get("language"):
            self._recent_languages.clear()
            return
        self._recent_languages.append((
            getattr(engine, "last_language", None),
            getattr(engine, "last_language_probability", None)))

    @property
    def language_hint(self):
        """The language code when the last 10 dictations were all detected
        as the same language with high probability (and the language setting
        is automatic), else None. The window can offer "always use <it>".
        Any thread."""
        if self.cfg.get("language"):
            return None
        try:
            runs = list(self._recent_languages)
        except RuntimeError:              # changed while copying
            return None
        if len(runs) < LANGUAGE_HINT_RUNS:
            return None
        codes = {code for code, _ in runs}
        if len(codes) != 1 or None in codes:
            return None
        for _, probability in runs:
            if (isinstance(probability, bool)
                    or not isinstance(probability, (int, float))
                    or probability < LANGUAGE_HINT_PROBABILITY):
                return None
        return codes.pop()

    # -- the worker

    def _wait_for_engine(self):
        while not self._engine_ready.wait(0.5):
            if self._closed or self.status == "failed":
                return False
        return True

    def _worker(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                if job[0] == "take":
                    samples = self._take_samples(job[1], job[2])
                    job = None if samples is None else ("audio", samples)
                if job is None:
                    pass
                elif job[0] == "text" or self._wait_for_engine():
                    self._process(job)
                else:
                    held = job[0] == "audio" and self._hold_recording(job[1])
                    log.error("Recording %s: no speech model",
                              "kept for a retry" if held else "dropped")
                    self._notice("dropped", (
                        "A recording is being kept for a few minutes "
                        "because the speech model is not loaded; try again "
                        "once it works." if held else
                        "A recording was dropped because the speech model "
                        "is not loaded."))
            except Exception as exc:
                self._log_error("Dictation failed", exc)
                self._notice("error", f"A dictation failed "
                             f"({type(exc).__name__}). Details are in the "
                             "log.")
            finally:
                with self._pending_lock:
                    self._pending -= 1

    def _transcribe(self, samples):
        """engine.transcribe with recovery; worker thread. The audio stays in
        memory (never on disk) until it is transcribed or given up:
        1st failure: try once more; 2nd: reload the model; 3rd: if it was on
        the GPU, load it on the processor; then give up (status 'failed',
        retry_engine() to try again). Returns the raw text, or None."""
        steps = ["retry", "reload", "cpu"]
        gpu = False
        while True:
            engine = self.engine
            try:
                if engine is None:
                    raise RuntimeError("no speech model")
                gpu = gpu or self._on_gpu(engine)
                return engine.transcribe(samples)
            except Exception as exc:
                self._log_error("Transcription failed", exc)
                why = type(exc).__name__
            engine = None                 # no reference may outlive a reload
            if not steps:
                break
            step = steps.pop(0)
            if step == "retry":
                log.info("Trying the transcription once more")
                continue
            if step == "reload":
                if self._reload_engine(cpu=False, why=why):
                    continue
                step = steps.pop(0) if steps else None
            if step == "cpu" and gpu:
                if self._reload_engine(cpu=True, why=why):
                    continue
            break
        msg = (f"Speech recognition keeps failing ({why}). "
               + ("The recording is being kept for a few minutes; try "
                  "again once the speech model works."
                  if self._hold_recording(samples) else
                  "The recording was dropped."))
        self.call_soon(functools.partial(self._set_status, "failed", msg))
        self._engine_ready.clear()
        self._notice("error", msg)
        return None

    @staticmethod
    def _on_gpu(engine):
        return str(getattr(engine, "device", "cpu")).lower() not in ("cpu",)

    def _reload_engine(self, cpu, why):
        """Load the speech model again on the worker thread (cpu=True: on
        the processor). True if a working engine is now in place."""
        factory = self._factory
        if cpu:
            cls = self._engine_cls
            cpu_cfg = dict(self.cfg, device="cpu")
            factory = (functools.partial(cls, cpu_cfg)
                       if cls is not None else None)
        if factory is None:
            return False
        what = "on the processor" if cpu else "again"
        log.warning("Speech recognition keeps failing (%s): loading the "
                    "model %s", why, what)
        self._engine_ready.clear()
        self.call_soon(functools.partial(
            self._set_status, "loading",
            f"Reloading the speech model {what} after errors"))
        with self._load_lock:
            self.engine = None               # free GPU memory first
            try:
                engine = factory()
            except Exception as exc:
                self._log_error("Reloading the speech model failed", exc,
                                always_message=True)
                return False
        engine.cfg = self.cfg
        self.engine = engine
        self._engine_cls = type(engine)
        self._engine_ready.set()
        detail = self._ready_detail(engine) or (
            "on the processor after GPU errors" if cpu else
            "reloaded after errors")
        if cpu and not getattr(engine, "fallback_reason", None):
            detail = "on the processor after GPU errors"
        self.call_soon(functools.partial(self._set_status, "ready", detail))
        self._notice("engine", "The speech model was reloaded "
                     + ("on the processor because the graphics card kept "
                        "failing." if cpu else "after errors."))
        return True

    def _process(self, job):
        cfg = self.cfg
        started = time.time()
        language = None
        if job[0] == "audio":
            samples = job[1]
            seconds = len(samples) / audio.SAMPLE_RATE
            raw = self._transcribe(samples)
            if raw is None:
                return
            took = time.time() - started
            language = getattr(self.engine, "last_language", None)
            self._note_language(self.engine)
        else:
            raw, seconds = job[1], job[2]
            took = None                   # no speech recognition ran
        if not raw.strip():
            log.info("Nothing recognised in %.1f s of audio", seconds)
            self._notice("empty", "Nothing was recognised in that recording.")
            return
        trace = []
        cleanup.take_status()             # forget an older run's status
        if _pipeline_takes_language():
            final = pipeline.run(raw, cfg, trace, language=language)
        else:                             # an older pipeline
            final = pipeline.run(raw, cfg, trace)
        cleanup_status = cleanup.take_status()
        if self.log_text:
            log.debug("raw: %r", raw)
            for name, text, secs in trace:
                log.debug("  after %-15s %.2fs  %r", name, secs, text)
        if not final.strip():
            log.info("Nothing left after clean-up (%d words recognised)",
                     _words(raw))
            if self.log_text:
                log.info("  raw text: %r", raw)
            self._notice("empty", "Nothing was left to paste after "
                         "filler words were removed.")
            return
        text = pipeline.text_to_paste(final, cfg)
        paste_error, method = None, None
        safe_final = self._console_safe_final(trace, raw, cfg, language)
        safe = {}
        if self.dry_run:
            method = "dry-run"
        else:
            try:
                method = self._deliver(
                    text, cfg.get("restore_clipboard", True),
                    None if safe_final is None
                    else (safe_final, pipeline.text_to_paste(safe_final, cfg)),
                    safe)
            except keys.PasteBlocked:
                paste_error = "Not pasted: a key was held down."
                log.warning("Paste skipped: a key stayed held (%d words "
                            "not pasted)", _words(final))
                self._notice("paste", "A dictation was not pasted because "
                             "a key was held down. It is in the list of "
                             "recent dictations.")
            except Exception as exc:
                self._log_error("Paste failed", exc)
                paste_error = type(exc).__name__
        stages = [self._stage(entry) for entry in trace]
        if safe:
            # What was pasted is the text from before the AI rewrite.
            final, text = safe["final"], safe["text"]
            stages.append({
                "name": "console_safety", "text": final, "seconds": 0.0,
                "note": "The AI rewrite added line breaks and the window "
                        "in front is a terminal, so the text from before "
                        "the rewrite was pasted."})
            log.warning("Pasting into a terminal: the AI rewrite added line "
                        "breaks, so the text from before the rewrite was "
                        "pasted (%d words)", _words(final))
        log.info("Dictation: %.1fs audio, %.2fs speech, %.2fs text, %d words, "
                 "%s", seconds, took or 0.0, sum(t for _, _, t in trace),
                 _words(final), "not pasted" if paste_error else method)
        if self.log_text:
            log.info("  text: %r", final)
        record = {
            "ts": time.time(), "raw": raw, "final": final, "pasted": text,
            "audio_seconds": round(float(seconds), 3),
            "speech_seconds": None if took is None else round(took, 3),
            "stages": stages, "console_safety": bool(safe),
            "cleanup": cleanup_status, "dry_run": self.dry_run,
            "paste_error": paste_error, "paste_method": method,
            "language": language,
            "total_seconds": round(time.time() - started, 3),
        }
        # Text that failed to paste still reaches on_result (e.g. history),
        # so it is never lost.
        self.call_soon(functools.partial(self._emit, record))

    @staticmethod
    def _stage(entry):
        name, out, secs = entry[0], entry[1], entry[2]
        stage = {"name": name, "text": out, "seconds": round(secs, 4)}
        if getattr(entry, "failed", False):
            stage["failed"] = True
            stage["error"] = getattr(entry, "error", "")
        return stage

    # -- pasting

    def _keyboard_busy(self):
        """True while pasting could collide with the keyboard hotkey: a
        recording is in progress or the hotkey is held. Never true for a
        mouse-only hotkey (it is not a keyboard key). Any thread."""
        if not self._key_groups:
            return False
        return self._state == "recording" or self._combo_down()

    def _paste_ready(self):
        return not self._keyboard_busy() and keys.no_modifier_held()

    @contextlib.contextmanager
    def _paste_guard(self):
        """Held while our synthetic keys go out: _poll cannot start a
        recording meanwhile (it takes the same lock)."""
        with self._input_lock:
            yield self._paste_ready()

    def _console_safe_final(self, trace, raw, cfg, language):
        """The final text as it would be without the AI clean-up stage, when
        that stage added line breaks (a rewrite from a hostile or confused
        model); else None. Line breaks of the user's own snippets and spoken
        formatting come in later (the rules stage) and are not affected."""
        name = pipeline.stage_name(cleanup.clean)
        for i, entry in enumerate(trace):
            if entry[0] != name:
                continue
            before = trace[i - 1][1] if i else raw
            if getattr(entry, "failed", False) or not (
                    isinstance(before, str) and isinstance(entry[1], str)
                    and _line_breaks(entry[1]) > _line_breaks(before)):
                return None
            later = pipeline.STAGES[pipeline.STAGES.index(cleanup.clean) + 1:] \
                if cleanup.clean in pipeline.STAGES else []
            try:
                kept = pipeline.run(before, cfg, None, language=language,
                                    stages=later) if later else before
            except Exception as exc:
                self._log_error("Console-safe text failed", exc)
                return None
            return kept if isinstance(kept, str) and kept.strip() else None
        return None

    def _in_console(self):
        """Is the foreground window a terminal? Fails towards "yes"."""
        try:
            return self.foreground_class() in CONSOLE_CLASSES
        except Exception:
            return True

    def _deliver(self, text, restore, console_safe=None, note=None):
        """Paste `text` as soon as nothing the user does can collide with
        it; worker thread. Waits (in order, without limit) while a
        recording is in progress or the keyboard hotkey is held.

        console_safe: (final, text to paste) without the AI rewrite. It is
        pasted instead when the window in front is a terminal (see
        CONSOLE_CLASSES), and then note gets {"final", "text"}."""
        while True:
            while self._keyboard_busy():
                if self._closed:
                    raise keys.PasteBlocked("the app is closing")
                time.sleep(0.02)
            if console_safe is not None and self._in_console():
                text = console_safe[1]
                if note is not None:
                    note.update(final=console_safe[0], text=text)
                console_safe = None          # decided; stays decided
            try:
                return keys.paste_text(text, restore, ready=self._paste_ready,
                                       guard=self._paste_guard)
            except keys.PasteBlocked:
                if self._closed or not self._keyboard_busy():
                    raise
                # A new recording began just before our keys: wait for it.

    def _emit(self, record):
        for fn in list(self.on_result):
            try:
                fn(record["raw"], record["final"], record["audio_seconds"])
            except Exception as exc:
                self._log_error(f"on_result callback {fn!r} failed", exc)
        for fn in list(self.on_dictation):
            try:
                fn(record)
            except Exception as exc:
                self._log_error(f"on_dictation callback {fn!r} failed", exc)

    # -- the pill

    _draw_error_at = None        # monotonic time of the last logged failure
    _draw_errors = 0             # failures not logged since then

    def _draw(self):
        flash = getattr(self, "_flash", None)
        if flash and time.monotonic() >= flash[1]:
            self._flash = flash = None
        if self._preview and time.time() < self._preview[1]:
            state, level = self._preview[0], 0.05
        else:
            self._preview = None
            if (self._state == "recording"
                    and time.time() - self._started_at > SHOW_DELAY):
                state, level = "listening", self.recorder.level
            elif flash:
                state, level = flash[0], 0.0
            elif self._pending > 0:
                state, level = "working", 0.0
            else:
                state, level = "hidden", 0.0
        try:
            self.overlay.update(state, level)
        except Exception:
            # Never silent, never fatal to the hotkey loop, never a flood:
            # the next tick simply tries again.
            now = time.monotonic()
            if (self._draw_error_at is None
                    or now - self._draw_error_at >= DRAW_ERROR_LOG_SECONDS):
                log.exception("Could not draw the listening pill (%s); "
                              "%d similar failures not logged since the last "
                              "report", state, self._draw_errors)
                self._draw_error_at, self._draw_errors = now, 0
            else:
                self._draw_errors += 1

    def _shutdown(self):
        if self._closed:
            return
        self._closed = True
        with self._held_lock:
            self._held = None            # a held recording never outlives us
        self._stop_hook()
        self._jobs.put(None)
        try:
            self.recorder.close()
        except Exception:
            pass
        try:                             # a paste's clipboard restore may
            keys.restore_pending_clipboard()   # still be waiting on its timer
        except Exception as exc:
            log.warning("Could not put the clipboard back (%s)",
                        type(exc).__name__)
        self.overlay.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
