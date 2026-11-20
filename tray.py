"""System-tray icon: state at a glance and quick actions.

    handle = tray.start(app, on_open=None)   # after App(...), before app.run()
    app.run()
    handle.stop()                            # removes the icon

Clicking the icon runs on_open (the app window); the menu has Open, Pause,
Microphone, AI cleanup, Start with Windows and Quit. Everything else lives
in the app window (api.py + ui/).

Threading
- The icon (pystray) runs its own Win32 message loop on the "tray" thread.
  Menu callbacks run there. Config changes run on a short-lived worker
  thread (settings.apply_changes waits for the Tk thread), so neither the
  tray thread nor the Tk thread ever waits for the other.
- A small "tray-poll" thread watches app.paused / the recorder and asks the
  tray thread to redraw the icon and rebuild the menu. Menu rebuilds are
  never done while the popup menu is open.
"""

import ctypes
import logging
import os
import queue
import sys
import threading
import winreg
from ctypes import wintypes
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

import audio
import keys
import settings

log = logging.getLogger("holdtotalk.tray")

AUTOSTART_NAME = "HoldToTalk"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\StartupApproved\Run")
# Test override (the installer's tests, see htt-common.ps1): a throw-away key
# under Software\HoldToTalkTest\ stands in for both keys, so removing the
# entry can be tried for real without touching the real one. Anything else
# is ignored.
_override = os.environ.get("HTT_RUN_KEY", "")
if _override.lower().startswith("software\\holdtotalktest\\"):
    RUN_KEY = _override
    APPROVED_KEY = _override.rstrip("\\") + "\\StartupApproved"
POLL_SECONDS = 0.25
# fn() called after the tray changed something the app window shows but
# that is not in config.json (Start with Windows); api.Server adds itself.
CHANGE_HOOKS = []

# The same words as the app window uses for the same things.
DEFAULT_MIC_LABEL = "Windows default microphone"
NOT_CONNECTED = "(not connected)"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
WM_TRAY_CALL = 0x8000 + 0x151          # WM_APP + n: "run queued tray calls"


# -- microphones ---------------------------------------------------------------

PSEUDO_DEVICES = ("microsoft sound mapper - input",
                  "primary sound capture driver")
MME_NAME_LEN = 31          # MME truncates device names to 31 characters
WDM_KS = "Windows WDM-KS"


def _same_device_name(a, b):
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    # A cut that ends in a space loses it when stripped, hence the -1.
    return len(short) >= MME_NAME_LEN - 1 and long_.startswith(short)


def dedupe_devices(devices):
    """audio.list_devices() -> one entry per physical microphone.

    Each mic is listed once per host API (MME with its name cut to 31
    characters, DirectSound, WASAPI, WDM-KS). Entries whose names are equal,
    or where one is the 31-character MME truncation of the other, are merged;
    the Windows pseudo-devices (Sound Mapper, Primary Sound Capture Driver)
    are dropped. WDM-KS only joins existing groups: it also lists disabled
    endpoints and inputs under driver-internal names ("Stereo Mix",
    "...Mic input"), which would otherwise show up as extra or duplicate mics.

    Returns [{"name", "indices", "names", "is_default"}] in device order;
    "name" is the longest (untruncated) name.
    """
    real = [d for d in devices
            if " ".join(d["name"].lower().split()) not in PSEUDO_DEVICES]
    primary = [d for d in real if d["hostapi"] != WDM_KS] or real
    groups = []
    for d in sorted(primary, key=lambda d: d["index"]):
        group = next((g for g in groups
                      if any(_same_device_name(n, d["name"])
                             for n in g["names"])), None)
        if group is None:
            group = {"name": d["name"].strip(), "indices": [], "names": [],
                     "is_default": False}
            groups.append(group)
        group["indices"].append(d["index"])
        group["names"].append(d["name"].strip())
        group["is_default"] = group["is_default"] or bool(d.get("is_default"))
        if len(d["name"].strip()) > len(group["name"]):
            group["name"] = d["name"].strip()
    for d in real:
        if d in primary:
            continue
        for g in groups:
            if any(_same_device_name(n, d["name"]) for n in g["names"]):
                g["indices"].append(d["index"])
                g["names"].append(d["name"].strip())
                break
    return groups


def device_config_value(group, resolve=audio.resolve_device):
    """The value to store as input_device for `group`.

    A name survives reboots and USB re-plugging (indices do not). Shortest
    name first, so the MME name (which resolve_device prefers) is used; a
    name is only chosen if it resolves back into this group. Falls back to
    the index.
    """
    for name in sorted(set(group["names"]), key=len):
        try:
            if resolve(name) in group["indices"]:
                return name
        except Exception:
            pass
    return group["indices"][0]


def current_device_group(groups, value, resolve=audio.resolve_device):
    """The group the configured input_device points at (None = default)."""
    if value is None or value == "":
        return None
    try:
        index = resolve(value)
    except Exception:
        return None
    return next((g for g in groups if index in g["indices"]), None)


# -- start with Windows -----------------------------------------------------------

def pythonw_path():
    """The windowless interpreter next to the running one (venv's pythonw)."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def autostart_command(pythonw=None, script=None):
    pythonw = Path(pythonw) if pythonw else pythonw_path()
    script = Path(script) if script else settings.APP_DIR / "dictate.py"
    return f'"{pythonw}" "{script}"'


def get_autostart(name=AUTOSTART_NAME):
    """The command registered under HKCU Run for `name`, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None


def _startup_disabled(name):
    """Task Manager's 'Disabled' flag for a Run entry (odd first byte)."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return False
    return bool(value) and isinstance(value, bytes) and value[0] & 1 == 1


def _delete_approved(name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def autostart_enabled(name=AUTOSTART_NAME, command=None):
    value = get_autostart(name)
    if value is None:
        return False
    command = command or autostart_command()
    return (value.strip().lower() == command.lower()
            and not _startup_disabled(name))


def set_autostart(enabled, name=AUTOSTART_NAME, command=None):
    """Add/remove the per-user Run entry (no admin rights needed).

    Enabling also clears a Task Manager 'Disabled' flag for this entry,
    since the user just asked for it to run.
    """
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ,
                              command or autostart_command())
        if _startup_disabled(name):
            _delete_approved(name)
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
        _delete_approved(name)


# -- icon images --------------------------------------------------------------------

# The app's mark: three sound bars on a disc. Teal = ready (the brand
# colour, also the app window's), coral = listening, amber = loading the
# model, grey with a slash = paused or failed.
_ICON_COLORS = {"ready": "#137a70", "paused": "#7c7f86", "listening": "#e0533d",
                "loading": "#c98a12", "failed": "#7c7f86"}
_icon_cache = {}


def make_icon(state, size=64):
    """size x size RGBA image of the mark in `state`'s colour."""
    key = (state, size)
    if key in _icon_cache:
        return _icon_cache[key]
    big = 256                               # drawn large, then downscaled
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, big - 5, big - 5), fill=_ICON_COLORS[state])
    white = (255, 255, 255, 255)
    for x, half in ((80, 34), (128, 66), (176, 46)):
        d.rounded_rectangle((x - 14, 128 - half, x + 14, 128 + half),
                            radius=14, fill=white)
    if state in ("paused", "failed"):
        d.line((56, 56, 200, 200), fill=(30, 30, 30, 255), width=28)
        d.line((56, 56, 200, 200), fill=white, width=12)
    img = img.resize((size, size), Image.LANCZOS)
    _icon_cache[key] = img
    return img


# -- the tray icon ------------------------------------------------------------------

def _changed():
    for fn in list(CHANGE_HOOKS):
        try:
            fn()
        except Exception:
            log.exception("Tray change hook failed")


class TrayHandle:
    def __init__(self, app, on_open=None, config_path=None,
                 autostart_name=AUTOSTART_NAME):
        self.app = app
        self.on_open = on_open
        self.config_path = config_path
        self.autostart_name = autostart_name
        self._calls = queue.Queue()
        self._in_menu = False
        self._deferred = False
        self._hooked = False
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._groups = []
        self._state = self.state()
        self.icon = pystray.Icon("HoldToTalk", make_icon(self._state),
                                 f"{settings.APP_NAME}: {self._status_text()}",
                                 menu=self._build_menu())
        self._thread = threading.Thread(target=self._run, name="tray",
                                        daemon=True)
        self._poller = threading.Thread(target=self._poll, name="tray-poll",
                                        daemon=True)

    # -- lifecycle
    def _start(self):
        self._thread.start()
        if not self._ready.wait(10):
            log.error("The tray icon did not start")
        self._poller.start()

    def _run(self):
        try:
            self.icon.run(setup=self._setup)
        except Exception:
            log.exception("Tray icon failed")
        finally:
            self._ready.set()

    def _setup(self, icon):
        try:
            self._install_hooks()
            icon.visible = True
        finally:
            self._ready.set()
            self._post(lambda: None)        # flush calls queued before start

    def stop(self):
        """Remove the icon and end the tray threads. Safe from any thread,
        safe to call twice."""
        self._stopping.set()
        self._ready.wait(5)
        try:
            self.icon.stop()
        except Exception:
            log.exception("Stopping the tray icon failed")
        if threading.current_thread() is not self._thread:
            self._thread.join(5)
        if (self._poller.is_alive()
                and threading.current_thread() is not self._poller):
            self._poller.join(2)

    @property
    def running(self):
        return self._thread.is_alive()

    # -- running code on the tray thread
    def _install_hooks(self):
        handlers = getattr(self.icon, "_message_handlers", None)
        if not isinstance(handlers, dict):
            return
        from pystray._util import win32 as pwin32
        original = handlers.get(pwin32.WM_NOTIFY)

        def on_notify(wparam, lparam):
            self._in_menu = True
            try:
                return original(wparam, lparam) if original else 0
            finally:
                self._in_menu = False
                if self._deferred:
                    self._deferred = False
                    self._drain()
        if original:
            handlers[pwin32.WM_NOTIFY] = on_notify
        handlers[WM_TRAY_CALL] = lambda w, l: self._drain()
        self._hooked = True

    def _post(self, fn):
        """Run fn on the tray thread (never while the popup menu is open)."""
        self._calls.put(fn)
        hwnd = getattr(self.icon, "_hwnd", None)
        if self._hooked and hwnd:
            _user32.PostMessageW(hwnd, WM_TRAY_CALL, 0, 0)
        elif self._ready.is_set() and not self._hooked:
            self._drain()                   # pystray internals changed

    def _drain(self):
        if self._in_menu:
            self._deferred = True
            return
        while True:
            try:
                fn = self._calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("Tray call failed")

    def call_on_tray(self, fn, timeout=10):
        """Run fn on the tray thread and wait for its result (testing aid)."""
        done, box = threading.Event(), {}

        def run():
            try:
                box["value"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()
        self._post(run)
        if not done.wait(timeout):
            raise TimeoutError("tray thread did not respond")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    # -- state
    def state(self):
        status = getattr(self.app, "status", "ready")
        if status in ("loading", "failed"):
            return status
        if self._terms_pending() or self.app.paused:
            return "paused"       # grey with a slash; the text says which
        rec = getattr(self.app, "recorder", None)
        if getattr(rec, "recording", False):
            return "listening"
        return "ready"

    def _terms_pending(self):
        """The Terms of Use have not been accepted (app.terms_pending)."""
        return bool(getattr(self.app, "terms_pending", False))

    def _signature(self):
        cfg = self.app.cfg
        return (self.state(), self._terms_pending(), cfg.get("hotkey"),
                repr(cfg.get("input_device")),
                settings.cleanup_enabled(cfg),
                getattr(self.app, "mouse_hook_state", None),
                self._download_text(), self._failure_text())

    def _poll(self):
        last = None
        while not self._stopping.wait(POLL_SECONDS):
            try:
                sig = self._signature()
            except Exception:
                continue
            if sig != last:
                last = sig
                self._post(self.refresh)

    def refresh(self):
        """Redraw icon + tooltip and rebuild the menu. Tray thread."""
        state = self.state()
        previous, self._state = self._state, state
        if state != previous:
            self.icon.icon = make_icon(state)
            if previous == "loading" and state == "ready":
                if not getattr(self.app, "dry_run", False):
                    self.notify("Ready. Hold %s and speak." % keys.plain_hotkey(
                        self.app.cfg.get("hotkey", "the hotkey")))
        title = f"{settings.APP_NAME}: {self._status_text()}"[:127]
        if self.icon.title != title:
            self.icon.title = title
        self.icon.update_menu()

    def _download_text(self):
        """'Downloading the speech model… 42%' while the app downloads it,
        else None."""
        try:
            get = getattr(self.app, "download_progress", None)
            info = get() if callable(get) else None
            if getattr(self.app, "status", None) != "loading" or not info:
                return None
            percent = info.get("percent")
            return "Downloading the speech model…" + (
                f" {percent}%" if isinstance(percent, int)
                and not isinstance(percent, bool) else "")
        except Exception:
            return None

    def _failure_text(self):
        """Why the speech model could not be downloaded (plain, short), or
        None when it failed for another reason."""
        try:
            failure = getattr(self.app, "failure", None)
            if (getattr(self.app, "status", None) != "failed"
                    or not isinstance(failure, dict)):
                return None
            retry_in = getattr(self.app, "auto_retry_in", None)
            tail = (" Trying again soon."
                    if callable(retry_in) and retry_in() is not None else "")
            return ("Speech model not downloaded: "
                    f"{failure.get('reason', '')}.{tail}")
        except Exception:
            return None

    def _status_text(self, *_):
        state = self.state()
        if state == "loading":
            return self._download_text() or "Getting ready…"
        if state == "failed":
            return self._failure_text() or "Can’t dictate right now"
        if self._terms_pending():
            return "Off: accept the Terms of Use (open the window)"
        if state == "paused":
            return "Paused"
        if state == "listening":
            return "Listening..."
        hotkey = self.app.cfg.get("hotkey", "?")
        text = f"Ready (hold {keys.plain_hotkey(hotkey)})"
        if getattr(self.app, "mouse_hook_state", None) == "fallback":
            text += " - mouse button not blocked"
        return text

    def notify(self, message):
        try:
            self.icon.notify(message, settings.APP_NAME)
        except Exception:
            log.warning("%s", message)

    # -- menu
    def _build_menu(self):
        Item, Menu = pystray.MenuItem, pystray.Menu
        return Menu(
            Item(f"Open {settings.APP_NAME}", self.open,
                 default=True, visible=self.on_open is not None),
            Item(self._status_text, None, enabled=False),
            Item(lambda i: "Resume" if self.app.paused else "Pause",
                 self.toggle_pause),
            Menu.SEPARATOR,
            Item("Microphone", Menu(self._mic_items)),
            Item("AI cleanup", self.toggle_cleanup,
                 checked=lambda i: settings.cleanup_enabled(self.app.cfg)),
            Menu.SEPARATOR,
            Item("Start with Windows", self.toggle_autostart,
                 checked=lambda i: self.autostart_on(),
                 visible=self.autostart_name is not None),
            Item("Quit", self.quit),
        )

    def _mic_items(self):
        Item = pystray.MenuItem
        try:
            self._groups = dedupe_devices(audio.list_devices())
        except Exception:
            log.exception("Could not list microphones")
            self._groups = []
        value = self.app.cfg.get("input_device")
        current = current_device_group(self._groups, value)
        unresolved = value not in (None, "") and current is None
        # pystray wants callables for "checked" (a bool raises ValueError).
        yield Item(DEFAULT_MIC_LABEL, self._choose_mic(None), radio=True,
                   checked=lambda i: value in (None, ""))
        if unresolved:
            yield Item(f"{value} {NOT_CONNECTED}", None, enabled=False,
                       radio=True, checked=lambda i: True)
        if self._groups:
            yield pystray.Menu.SEPARATOR
        for g in self._groups:
            label = g["name"] + ("  (Windows default)" if g["is_default"]
                                 else "")
            yield Item(label, self._choose_mic(g), radio=True,
                       checked=lambda i, g=g: current is g)

    def _choose_mic(self, group):
        def choose(icon=None, item=None):
            value = None if group is None else device_config_value(group)
            self._apply_soon({"input_device": value}, "Microphone")
        return choose

    def _apply_soon(self, changes, what):
        """Save + apply on a worker thread (apply_changes waits for the Tk
        thread; the tray thread must not); problems become a balloon."""
        def run():
            result = settings.apply_changes(self.app, changes,
                                            self.config_path)
            if result.get("errors"):
                log.warning("%s not changed: %s", what,
                            "; ".join(map(str, result["errors"])))
                if any(str(e).startswith("Could not save")
                       for e in result["errors"]):
                    why = ("the settings file is read-only or in use by "
                           "another program")
                elif what == "Microphone":
                    why = "that microphone is not available right now"
                else:
                    why = "the app could not apply it"
                self.notify(f"{what} not changed: {why}. Details are in the "
                            "log.")
            self._post(self.refresh)
        threading.Thread(target=run, name="tray-apply", daemon=True).start()

    def open(self, icon=None, item=None):
        if self.on_open is not None:
            try:
                self.on_open()
            except Exception:
                log.exception("Opening the app window failed")

    def toggle_pause(self, icon=None, item=None):
        self.app.set_paused(not self.app.paused)
        self.refresh()

    def toggle_cleanup(self, icon=None, item=None):
        enabled = not settings.cleanup_enabled(self.app.cfg)
        self._apply_soon({"cleanup": {"enabled": enabled}}, "AI cleanup")

    def autostart_on(self):
        if self.autostart_name is None:
            return False
        try:
            return autostart_enabled(self.autostart_name)
        except OSError:
            return False

    def toggle_autostart(self, icon=None, item=None):
        if self.autostart_name is None:
            return
        want = not self.autostart_on()
        try:
            set_autostart(want, self.autostart_name)
        except OSError as exc:
            log.warning("Changing Start with Windows failed: %s", exc)
            self.notify("Windows did not allow changing Start with Windows. "
                        "Details are in the log.")
        _changed()

    def quit(self, icon=None, item=None):
        self.app.quit()
        self.stop()


def start(app, on_open=None, config_path=None,
          autostart_name=AUTOSTART_NAME):
    """Show the tray icon (own thread) and return a handle; handle.stop()
    removes it. Call from the Tk thread after App(...) and before app.run().

    on_open: fn() run on the tray thread for a click on the icon or "Open"
    (it must return quickly); the item is hidden when None.
    autostart_name: the Run entry "Start with Windows" manages; None hides
    the item (a test instance). config_path exists for tests only: it must
    be the file app.reload_config() reads.
    """
    handle = TrayHandle(app, on_open, config_path, autostart_name)
    handle._start()
    return handle
