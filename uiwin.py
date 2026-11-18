"""The app window: the UI (ui/, served by api.py) in a chromeless browser
window of its own.

    win = AppWindow(server.launch_url, profile_dir("..."))
    win.open()      # focus the window if it is open, otherwise start it
    win.close()     # close it (the app quitting)

Why a browser in app mode (msedge --app=URL) rather than an embedded web
view (pywebview + pythonnet + WebView2): Edge is part of Windows 11 and is
the same Chromium engine WebView2 uses, so it adds no Python packages, no
.NET bridge and no second Python process. Measured on Windows 11 (launch to
a loaded page, warm): Edge app mode 0.5 s / ~135 MB private memory,
pywebview 1.7 s (4.5 s cold) / ~195 MB. The window runs in its own
browser profile (profile_dir), so it never mixes with the user's browsing,
and it is a separate process: a stuck or closed window cannot affect
dictation. Fallbacks, in order: Edge, Chrome (same --app mode; e.g. where
Edge was uninstalled), then the default browser (a normal tab: works, but
cannot be focused or closed by the app).

Which window is ours: only windows of the browser process this object
started, identified by its process id, full program path and start time
(a title alone could be copied by any program, which would then be focused
by "Open" and sent the close message on quit). The same identity is kept in
OWNER_FILE in the window's profile folder, so that after the app was
restarted (or crashed) the next run recognises the window an earlier run
left open, which is no longer connected to anything, and replaces it with a
working one instead of focusing it. A window of this run whose page is not
connected (is_connected(), set by api.Server) is replaced too.
"""

import ctypes
import json
import logging
import os
import subprocess
import threading
import time
import webbrowser
import winreg
from ctypes import wintypes
from pathlib import Path

import settings

log = logging.getLogger("holdtotalk.uiwin")

WINDOW_SIZE = (1120, 780)       # device-independent pixels
START_GRACE = 20.0              # s a started browser may take to show a window
CLOSE_SECONDS = 3.0             # s a closed browser gets to exit before it is ended
WINDOW_WAIT = 2.0               # s a just-started browser may take to show its
                                # window before it is closed without one
EXTRA_ARGS = []                 # tests add e.g. a remote-debugging port
BROWSERS = ("msedge.exe", "chrome.exe")
OWNER_FILE = "htt-window.json"
# A quiet, lean browser for one local page: no first-run pages, sync,
# extensions, background downloads or crash uploads; one renderer; no GPU
# process (this UI does not need one). Measured on Windows 11: ~0.5 s to a
# loaded page and ~135 MB private memory with these, ~220 MB without.
BROWSER_FLAGS = (
    "--no-first-run", "--no-default-browser-check", "--disable-sync",
    "--disable-extensions", "--disable-default-apps",
    "--disable-background-networking", "--disable-component-update",
    "--disable-breakpad", "--no-pings", "--renderer-process-limit=1",
    "--disable-gpu", "--disable-features=Translate,msEdgeSidebarV2")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                             ctypes.POINTER(wintypes.DWORD)]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                 wintypes.LPARAM]
_user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
    ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

SW_RESTORE, SW_SHOW = 9, 5
WM_CLOSE = 0x0010
ASFW_ANY = 0xFFFFFFFF
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


class FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD)]


def profile_dir(suffix=""):
    """Where the window's browser profile lives: a short path outside the
    app folder (the profile nests deep; the app folder may be long)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "HoldToTalk" / f"window{suffix}"


def browser_candidates():
    """Installed Edge / Chrome executables, preferred first."""
    seen = []
    for exe in BROWSERS:
        paths = []
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = (r"Software\Microsoft\Windows\CurrentVersion\App Paths"
                       "\\" + exe)
                with winreg.OpenKey(hive, key) as k:
                    paths.append(winreg.QueryValue(k, None))
            except OSError:
                pass
        sub = (r"Microsoft\Edge\Application" if exe == "msedge.exe"
               else r"Google\Chrome\Application")
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            if os.environ.get(env):
                paths.append(os.path.join(os.environ[env], sub, exe))
        for p in paths:
            p = p.strip('"') if p else p
            if p and os.path.isfile(p) and p.lower() not in seen:
                seen.append(p.lower())
                yield p


# -- processes ----------------------------------------------------------------------

def _handle_info(handle):
    """(full program path, start time) of an open process handle, or None."""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                ctypes.byref(size)):
        return None
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _kernel32.GetProcessTimes(handle, *map(ctypes.byref, times)):
        return None
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return buf.value, created


def _process_info(pid):
    """(full program path, start time) of a running process, or None."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                   int(pid))
    if not handle:
        return None
    try:
        return _handle_info(handle)
    finally:
        _kernel32.CloseHandle(handle)


def _same(info, expected):
    return (info is not None and expected is not None
            and str(info[0]).lower() == str(expected[0]).lower()
            and int(info[1]) == int(expected[1]))


def _end_process(pid, expected, timeout):
    """Wait up to `timeout` s for the process to exit, then end it, but only
    while it is still the process described by `expected` (path + start
    time: a reused process id is never touched). True when it is gone."""
    handle = _kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
        False, int(pid))
    if not handle:
        return True
    try:
        if not _same(_handle_info(handle), expected):
            return True             # that process is gone; the id was reused
        if _kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0:
            return True
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESSENTRY32W)]
TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _parent_table():
    """{pid: parent pid} of every process (a parent that has exited still
    shows as the parent of its children)."""
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}
    table = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return table


def _descendants(pid, info):
    """{pid: (program path, start time)} of the processes that descend from
    the browser `pid` (identity `info`) and are that same program (a
    browser's renderers, crash handler and helpers are all its own
    executable) and started after it: a recycled process id is not taken
    for a child."""
    kids, frontier = {}, {int(pid): info}
    table = _parent_table()
    while frontier:
        nxt = {}
        for parent, pinfo in frontier.items():
            for child, ppid in table.items():
                if ppid != parent or child in kids or child == int(pid):
                    continue
                cinfo = _process_info(child)
                if (cinfo is not None
                        and _norm(cinfo[0]) == _norm(info[0])
                        and int(cinfo[1]) >= int(pinfo[1])):
                    kids[child] = cinfo
                    nxt[child] = cinfo
        frontier = nxt
    return kids


def _norm(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _trusted_image(image):
    """Is this program path one of the browsers this app can start?"""
    want = _norm(image)
    return any(_norm(c) == want for c in browser_candidates())


_popen = subprocess.Popen          # tests replace it


def _post_close(hwnd):
    _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _top_windows():
    """[(hwnd, pid, title)]: visible top-level Chromium windows on this
    desktop (EnumWindows)."""
    found = []

    def visit(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        _user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1" or not _user32.IsWindowVisible(hwnd):
            return True
        text = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, text, 256)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((hwnd, pid.value, text.value))
        return True
    _user32.EnumWindows(WNDENUMPROC(visit), 0)
    return found


def find_windows(title=None, owners=None):
    """Visible top-level windows titled exactly `title` (default
    settings.APP_NAME) that belong to one of `owners` ({pid: (program path,
    start time)}), as [(hwnd, pid)]. A window counts only while its process
    is still that very program: the title alone is never trusted. With no
    owners nothing is found."""
    title = settings.APP_NAME if title is None else title
    owners = owners or {}
    if not owners:
        return []
    out, checked = [], {}
    for hwnd, pid, text in _top_windows():
        if text != title or pid not in owners:
            continue
        if pid not in checked:
            checked[pid] = _same(_process_info(pid), owners[pid])
        if checked[pid]:
            out.append((hwnd, pid))
    return out


def focus(hwnd):
    """Bring a window to the front (restoring it if minimised). Windows
    only allows that while this process may set the foreground (the user
    just clicked the tray icon, or a second launch allowed it); otherwise
    the taskbar button flashes."""
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        _user32.ShowWindow(hwnd, SW_SHOW)
    _user32.SetForegroundWindow(hwnd)
    if _user32.GetForegroundWindow() != hwnd:
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 3, 0)
        _user32.FlashWindowEx(ctypes.byref(info))


def allow_any_foreground():
    """Let another process (the running app) bring its window forward:
    called by a second launch, which has the foreground."""
    _user32.AllowSetForegroundWindow(ASFW_ANY)


def _window_rect():
    """--window-size / --window-position (DIPs) centred on the primary
    screen's work area."""
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    work = RECT()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
    try:
        scale = _user32.GetDpiForSystem() / 96.0
    except Exception:
        scale = 1.0
    ww, wh = (work.r - work.l) / scale, (work.b - work.t) / scale
    w, h = min(WINDOW_SIZE[0], int(ww * 0.92)), min(WINDOW_SIZE[1],
                                                    int(wh * 0.92))
    x = int(work.l / scale + (ww - w) / 2)
    y = int(work.t / scale + (wh - h) / 2)
    return w, h, x, y


class AppWindow:
    """Opens, focuses and closes the app window. Thread-safe; open() returns
    within a fraction of a second unless it has to close a window left over
    from an earlier run first (up to CLOSE_SECONDS).

    is_connected: None, or fn() -> True while a page of this run is
    connected (api.Server sets it); a window of ours whose page is not
    connected is replaced on open()."""

    def __init__(self, url_factory, profile, title=None):
        self.url_factory = url_factory          # () -> one-time launch URL
        self.profile = Path(profile)
        self.title = title
        self.mode = None            # "app" (Edge/Chrome window) | "browser"
        self.last_open = None       # (result, monotonic time)
        self.is_connected = None
        self._lock = threading.Lock()
        self._procs = []            # Popen objects of browsers we started
        self._owned = {}            # pid -> (program path, start time)
        self._started_at = 0.0

    # -- which windows are ours
    def _prune(self):
        for pid, info in list(self._owned.items()):
            if not _same(_process_info(pid), info):
                del self._owned[pid]

    def windows(self):
        """[(hwnd, pid)]: the app windows of browsers this object started."""
        self._prune()
        return find_windows(self.title, self._owned)

    @property
    def is_open(self):
        return bool(self.windows())

    def _starting(self):
        alive = [p for p in self._procs if p.poll() is None]
        return bool(alive) and (time.monotonic() - self._started_at
                                < START_GRACE)

    def _page_ok(self):
        check = self.is_connected
        if check is None or time.monotonic() - self._started_at < START_GRACE:
            return True
        try:
            return bool(check())
        except Exception:
            return True

    # -- the owner record (survives a restart of the app)
    @property
    def _record_path(self):
        return self.profile / OWNER_FILE

    def _read_record(self):
        try:
            data = json.loads(self._record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        trusted = {_norm(c) for c in browser_candidates()}
        for item in data if isinstance(data, list) else []:
            try:
                pid, image, created = (int(item["pid"]), str(item["image"]),
                                       int(item["created"]))
            except (KeyError, TypeError, ValueError):
                continue
            # The file is writable by anything running as this user: only a
            # record of a browser this app could have started counts (the
            # start time is checked against the live process by the caller).
            if _norm(image) in trusted:
                out[pid] = (image, created)
            else:
                log.warning("Ignoring a window record for a program that is "
                            "not a supported browser")
        return out

    def _write_record(self):
        try:
            if self._owned:
                self._record_path.write_text(json.dumps(
                    [{"pid": pid, "image": img, "created": created}
                     for pid, (img, created) in self._owned.items()]),
                    encoding="utf-8")
            else:
                self._record_path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("Window record not written: %s", exc)

    def _leftovers(self):
        """Browsers an earlier run of the app started for this profile that
        are still running (same program, same start time)."""
        out = {}
        title = settings.APP_NAME if self.title is None else self.title
        for pid, info in self._read_record().items():
            if pid in self._owned or not _same(_process_info(pid), info):
                continue
            if any(p == pid and text != title
                   for _, p, text in _top_windows()):
                # A browser that shows some other window is somebody's own
                # browsing, whatever the record says: never touched.
                log.warning("Not closing a browser that shows other windows")
                continue
            out[pid] = info
        return out

    def _close_windows(self, owners):
        """Send the close message to the windows of these browsers (only
        those still the program described); True if there was one."""
        sent = False
        for hwnd, pid, _ in _top_windows():
            if pid in owners and _same(_process_info(pid), owners[pid]):
                _post_close(hwnd)
                sent = True
        return sent

    def _end(self, owners, timeout=CLOSE_SECONDS):
        """Close the windows of these browsers, wait for them to exit, end
        the ones that do not, and the helper processes they started (a
        browser that is killed before its window exists, or before it has
        shut its own helpers down, leaves those running)."""
        if not owners:
            return
        start = time.monotonic()
        deadline = start + timeout
        tree = {}
        for pid, info in owners.items():
            tree.update(_descendants(pid, info))
        # A browser started a moment ago has no window yet and would ignore
        # a close message sent now: wait for the window (or its exit).
        sent = self._close_windows(owners)
        while (not sent and time.monotonic() < min(deadline,
                                                   start + WINDOW_WAIT)
               and any(_same(_process_info(p), i) for p, i in owners.items())):
            time.sleep(0.05)
            sent = self._close_windows(owners)
        for pid, info in owners.items():
            _end_process(pid, info, max(0.0, deadline - time.monotonic()))
            self._owned.pop(pid, None)
        # The helpers: let them go with their browser, then end the rest
        # (helpers found again now too: some start late).
        for pid, info in owners.items():
            tree.update(_descendants(pid, info))
        grace = max(deadline, time.monotonic() + 1.0)
        for pid, info in tree.items():
            _end_process(pid, info, max(0.0, grace - time.monotonic()))
        for proc in self._procs:
            if proc.pid in owners:
                try:
                    proc.wait(1)
                except subprocess.TimeoutExpired:
                    pass
        self._procs = [p for p in self._procs if p.poll() is None]

    # -- opening and closing
    def open(self):
        """Focus the window, or start it. Returns "focused", "starting"
        (started moments ago, not shown yet), "launched", "replaced" (a
        window that was not connected to this run, e.g. left open by an
        earlier run, was closed and a new one started), "browser" (no
        Edge/Chrome: opened in the default browser) or "failed"."""
        with self._lock:
            found = self.windows()
            if found and self._page_ok():
                focus(found[0][0])
                result = "focused"
            elif found:
                log.info("App window is not connected; replacing it")
                self._end(dict(self._owned))
                result = self._launch()
                result = "replaced" if result == "launched" else result
            elif self._starting():
                result = "starting"
            else:
                leftovers = self._leftovers()
                if leftovers:
                    log.info("Closing the window an earlier run left open")
                    self._end(leftovers)
                result = self._launch()
                if leftovers and result == "launched":
                    result = "replaced"
            self.last_open = (result, time.monotonic())
            log.info("App window: %s", result)
            return result

    def open_async(self):
        """open() on a short-lived thread (for callers that must not wait)."""
        threading.Thread(target=self.open, name="open-window",
                         daemon=True).start()

    def _launch(self):
        url = self.url_factory()
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Window profile folder unavailable: %s", exc)
        w, h, x, y = _window_rect()
        for exe in browser_candidates():
            cmd = [exe, f"--app={url}", f"--user-data-dir={self.profile}",
                   *BROWSER_FLAGS,
                   f"--window-size={w},{h}", f"--window-position={x},{y}",
                   *EXTRA_ARGS]
            try:
                proc = _popen(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, close_fds=True)
            except OSError as exc:
                log.warning("Could not start %s: %s", os.path.basename(exe),
                            exc)
                continue
            self._procs = [p for p in self._procs if p.poll() is None]
            self._procs.append(proc)
            info = _process_info(proc.pid)
            if info is not None:
                self._owned[proc.pid] = info
                self._write_record()
            self._started_at = time.monotonic()
            self.mode = "app"
            return "launched"
        log.info("Neither Edge nor Chrome was found: opening the window as a tab "
                 "in the default browser, where that browser's own extensions "
                 "and settings apply")
        try:
            ok = webbrowser.open(url)
        except Exception:
            ok = False
        self.mode = "browser" if ok else None
        return "browser" if ok else "failed"

    def close(self, timeout=CLOSE_SECONDS):
        """Close the window (asks politely, then ends the browser processes
        this object started; nothing else is ever sent the close message)."""
        with self._lock:
            self._prune()
            # A window an earlier run left open goes too (it shows nothing).
            self._end({**self._leftovers(), **self._owned}, timeout)
            for proc in self._procs:
                if proc.poll() is None:
                    try:
                        proc.wait(0.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            self._procs = []
            self._owned = {}
            self._write_record()
