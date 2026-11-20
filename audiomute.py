"""Mute every other program's sound while the hold-to-talk key is held.

The setting is `sounds.mute_others` (off by default). While the key is held
the app's own recording is the only thing that matters, so every audio
session of every OTHER program on every active output device is muted, and
exactly those sessions are unmuted when the key is let go. A complete mute,
not a volume duck.

How it works (Windows Core Audio, through ctypes only, no extra package):

    IMMDeviceEnumerator -> every ACTIVE render endpoint -> IAudioSessionManager2
    -> IAudioSessionEnumerator -> per session: IAudioSessionControl2
    (GetSessionInstanceIdentifier, GetProcessId) and ISimpleAudioVolume
    (GetMute, SetMute).

Rules:
  * a session that is already muted (by the person) is never touched, so it is
    never unmuted either;
  * the app's own process and its child processes are never muted (the chimes
    are played by this process, so they stay audible);
  * a session is identified for the restore by its instance identifier (as a
    hash, see ident_of) AND its process id, never by an index; a session whose
    program exited meanwhile is skipped quietly;
  * sessions that start playing while the key is held are not muted (first
    version).

Never leave the PC silent:
  * restore() is called on key release, cancel, pause, an error, a settings
    change, shutdown and Quit (app.py);
  * a safety timer restores after MAX_HOLD_SECONDS;
  * while sessions are muted a small file in the data folder (STATE_NAME)
    lists them (hash of the session id, process id, process start time: no
    window titles, no paths, nothing about what was said); it is deleted on
    restore, and at the next start recover() unmutes the sessions listed that
    still exist with the same process id and start time, then deletes it.

Threads: every audio call runs on one worker thread ("audio-mute") that
initialises COM for itself, so the key handler and the recorder only ever do
a queue put (mute(), restore(): microseconds). Failure is quiet: one short
log line (the exception's class only), nothing muted, dictation unaffected.

    m = Muter()
    m.recover()      # once at start: undo a crash's mute
    m.mute()         # key down (returns at once)
    m.restore()      # key up   (returns at once)
    m.close()        # shutdown: restore and stop the worker, waits briefly

Importing this module is safe anywhere; only WasapiBackend touches Windows.
"""

import atexit
import ctypes
import hashlib
import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("holdtotalk.audiomute")

STATE_NAME = "audio-muted.json"        # in settings.DATA_DIR
STATE_VERSION = 1
MAX_HOLD_SECONDS = 300.0               # the app has no recording limit: 5 min
RETRY_SECONDS = 5.0                    # a failed restore is retried this often
RESTORE_TRIES = 3
RESTORE_PAUSE = 0.2
GIVE_UP_SECONDS = 120.0                # a session that refuses to be unmuted
                                       # is retried this long, then dropped
MAX_STATE_AGE = 24 * 3600.0            # an older state file is not acted on
LOG_EVERY_SECONDS = 30.0               # one line per kind of failure, this often
CLOSE_SECONDS = 3.0


class AudioError(Exception):
    """A Core Audio call failed (the HRESULT is in the message)."""


def ident_of(instance):
    """The stored form of a session instance identifier. The identifier holds
    the full path of the program's executable, which must not be written down,
    so only a hash of it is kept (it is still unique per session)."""
    return hashlib.sha256(str(instance).encode("utf-8", "replace")
                          ).hexdigest()[:32]


class MuteResult(set):
    """What set_mute returns: the keys that were set (the set itself) and, in
    `failed`, the keys that were FOUND but whose SetMute raised (a refused
    unmute must be retried; a session that is not found is simply gone)."""
    failed = frozenset()


class Session:
    """One audio session as the muter sees it. `instance` is the raw
    identifier (memory only); `ident` its hash; `pid` 0 for the system sounds
    session or a session that spans several processes."""
    __slots__ = ("instance", "pid", "muted", "system")

    def __init__(self, instance, pid, muted, system=False):
        self.instance = instance
        self.pid = int(pid)
        self.muted = bool(muted)
        self.system = bool(system)

    @property
    def ident(self):
        return ident_of(self.instance)

    @property
    def key(self):
        return (self.ident, self.pid)


class Muter:
    """Mutes and restores other programs' sound on a worker thread.

    backend: an object with begin(), end(), own_pids(), list_sessions(),
    set_mute(keys, muted) -> set of keys done (a MuteResult also names the
    keys found but refused, in .failed), start_time(pid). Default
    WasapiBackend (made on the worker thread). state_path: the state file
    (default settings.DATA_DIR / STATE_NAME, read at use)."""

    def __init__(self, backend=None, state_path=None,
                 max_hold=MAX_HOLD_SECONDS, retry_seconds=RETRY_SECONDS,
                 retry_pause=RESTORE_PAUSE, give_up=GIVE_UP_SECONDS,
                 max_age=MAX_STATE_AGE):
        self._backend = backend
        self._state_path = state_path
        self.max_hold = float(max_hold)
        self.retry_seconds = float(retry_seconds)
        self.retry_pause = float(retry_pause)
        self.give_up = float(give_up)
        self.max_age = float(max_age)
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._wanted = False        # the key is held (set by mute/restore)
        self._dirty = False         # something may still be muted by us
        self._closed = False
        self._thread = None
        self._muted = []            # worker only: entries we muted
        self._deadline = 0.0        # worker only: when the safety timer fires
        self._noted = {}            # failure kind -> last logged (monotonic)
        self._refused = {}          # worker only: key -> first refusal time
        self._failing = None        # worker only: when a restore first failed

    # -- the caller's side: never waits for audio ---------------------------------

    @property
    def active(self):
        """True while mute() was called and restore() has not been."""
        return self._wanted

    def mute(self):
        """Ask for other programs' sound to be muted. Returns at once (a queue
        put); False if already asked for or closed."""
        with self._lock:
            if self._closed or self._wanted:
                return False
            self._wanted = True
            self._ensure_thread()
        self._q.put(("mute",))
        return True

    def restore(self, why="done"):
        """Ask for what was muted to be unmuted. Returns at once; False when
        there was nothing to restore."""
        with self._lock:
            if not (self._wanted or self._dirty):
                return False
            self._wanted = False
            self._ensure_thread()
        self._q.put(("restore", why))
        return True

    def recover(self):
        """After a crash: unmute what the state file lists. Does nothing (and
        starts nothing) when there is no state file."""
        try:
            if not self.state_path().exists():
                return False
        except Exception:
            return False
        with self._lock:
            if self._closed:
                return False
            self._ensure_thread()
        self._q.put(("recover",))
        return True

    def flush(self, timeout=5.0):
        """Wait until the worker has handled everything asked so far."""
        with self._lock:
            if self._thread is None:
                return True
            self._ensure_thread()
        done = threading.Event()
        self._q.put(("flush", done))
        return done.wait(timeout)

    def close(self, timeout=CLOSE_SECONDS):
        """Restore and stop the worker; waits at most `timeout` seconds (the
        worker is a daemon, and the state file still covers a stuck call).
        True if everything was restored in time."""
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            self._wanted = False
            if self._dirty:
                self._ensure_thread()         # a dead worker is replaced
            thread = self._thread
        if thread is None:
            return True
        done = threading.Event()
        self._q.put(("stop", done))
        ok = done.wait(timeout)
        try:
            atexit.unregister(self._at_exit)
        except Exception:
            pass
        return ok

    def state_path(self):
        if self._state_path is not None:
            return Path(self._state_path)
        import settings
        return Path(settings.DATA_DIR) / STATE_NAME

    # -- the worker -------------------------------------------------------------------

    def _ensure_thread(self):          # called with the lock held
        """Start the worker; start a new one if the old one has died."""
        if self._thread is not None and self._thread.is_alive():
            return
        first = self._thread is None
        self._thread = threading.Thread(target=self._run, name="audio-mute",
                                        daemon=True)
        self._thread.start()
        if first:
            atexit.register(self._at_exit)

    def _at_exit(self):
        try:
            self.close(2.0)
        except Exception:
            pass

    def _run(self):
        backend = self._backend
        if backend is None:
            try:
                backend = WasapiBackend()
            except Exception as exc:
                self._note("start", exc)
        if backend is not None:
            try:
                backend.begin()
            except Exception as exc:
                self._note("start", exc)
                backend = None
        self._backend = backend
        try:
            while True:
                timeout = None
                if self._muted:
                    timeout = max(0.0, self._deadline - time.monotonic())
                kind, cmd = "timer", None
                try:
                    try:
                        cmd = self._q.get(timeout=timeout)
                    except queue.Empty:
                        with self._lock:
                            self._wanted = False
                        self._do_restore("safety timer")
                        continue
                    kind = cmd[0]
                    if kind == "mute":
                        self._do_mute()
                    elif kind == "restore":
                        self._do_restore(cmd[1])
                    elif kind == "recover":
                        self._do_recover()
                    elif kind == "flush":
                        cmd[1].set()
                    elif kind == "stop":
                        self._do_restore("shutdown")
                        cmd[1].set()
                        return
                except Exception as exc:       # never let the worker die
                    self._note(kind, exc)
                    if cmd is not None and kind in ("flush", "stop"):
                        cmd[1].set()
                        if kind == "stop":
                            return
        finally:
            if backend is not None:
                try:
                    backend.end()
                except Exception:
                    pass

    def _set_dirty(self, value):
        with self._lock:
            self._dirty = bool(value)

    def _do_mute(self):
        backend = self._backend
        if backend is None or not self._wanted:
            return
        if self._muted:                     # a restore failed earlier
            self._do_restore("before muting again")
            if self._muted:
                return
        old = []
        if self.state_path().exists():      # a recovery that failed at start
            self._do_recover()
            old = self._read_rows()[0]      # still there: keep those rows too
        own = set(backend.own_pids())
        own.add(os.getpid())
        seen, fresh = set(), []
        for s in backend.list_sessions():
            if s.muted or s.pid in own or s.key in seen:
                continue
            seen.add(s.key)
            fresh.append(s)
        if not fresh:
            return
        entries = [{"id": s.ident, "pid": s.pid,
                    "started": backend.start_time(s.pid)} for s in fresh]
        have = {(e["id"], e["pid"]) for e in entries}
        entries = [e for e in old if (e["id"], e["pid"]) not in have] + entries
        if not self._write_state(entries):
            return                           # cannot promise a way back: skip
        self._muted = entries
        self._deadline = time.monotonic() + self.max_hold
        self._set_dirty(True)
        try:
            done = backend.set_mute([(e["id"], e["pid"]) for e in entries],
                                    True)
        except Exception as exc:
            self._note("mute", exc)
            self._do_restore("a mute failed")
            return
        kept = [e for e in entries if (e["id"], e["pid"]) in done
                or e in old]
        if len(kept) != len(entries):
            self._muted = kept
            if kept:
                self._write_state(kept)
        if not kept:
            self._muted = []
            self._remove_state()
            self._set_dirty(False)
            return
        log.info("Other sound muted (%d sessions)", len(kept))
        if not self._wanted:                  # let go before we finished
            self._do_restore("released early")

    def _do_restore(self, why):
        # Whatever we muted is unmuted, even if the person muted it again
        # themselves during the hold: we cannot tell the two apart.
        entries = self._muted
        if not entries:
            self._remove_state()
            self._set_dirty(False)
            return
        keys = [(e["id"], e["pid"]) for e in entries]
        result = None
        for attempt in range(RESTORE_TRIES):
            try:
                # Sessions that are gone are simply not found: skipped.
                result = self._backend.set_mute(keys, False)
                break
            except Exception as exc:
                self._note("restore", exc)
                if attempt + 1 < RESTORE_TRIES and self.retry_pause > 0:
                    time.sleep(self.retry_pause)
        now = time.monotonic()
        if result is None:
            # The whole call failed: keep the list and the file, try again
            # soon (and at the next start); give up in memory after a while.
            if self._failing is None:
                self._failing = now
            if now - self._failing >= self.give_up:
                self._muted, self._failing = [], None
                self._set_dirty(False)
                log.warning("Other sound could not be unmuted; the next "
                            "start will try again")
                return
            self._deadline = now + self.retry_seconds
            log.warning("Other sound could not be unmuted yet; trying again")
            return
        self._failing = None
        failed = set(getattr(result, "failed", ()))
        if not failed:
            self._muted = []
            self._refused.clear()
            self._remove_state()
            self._set_dirty(False)
            log.info("Other sound unmuted (%d sessions, %s)", len(keys), why)
            return
        # Some sessions were found but refused: retry exactly those.
        left = []
        for e in entries:
            key = (e["id"], e["pid"])
            if key not in failed:
                self._refused.pop(key, None)
                continue
            first = self._refused.setdefault(key, now)
            if now - first < self.give_up:
                left.append(e)
            else:
                self._refused.pop(key, None)
                log.warning("A program refused to be unmuted; giving up on it")
        self._muted = left
        if left:
            self._write_state(left)
            self._deadline = now + self.retry_seconds
            log.warning("Other sound could not be unmuted for %d sessions "
                        "yet; trying again", len(left))
        else:
            self._remove_state()
            self._set_dirty(False)

    def _read_rows(self):
        """(rows, state): the valid rows of the state file and "ok", "none"
        (no file) or "bad" (unreadable)."""
        try:
            data = json.loads(self.state_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [], "none"
        except (OSError, ValueError):
            return [], "bad"
        rows = data.get("muted") if isinstance(data, dict) else None
        out = []
        for row in rows if isinstance(rows, list) else []:
            if (isinstance(row, dict) and isinstance(row.get("id"), str)
                    and isinstance(row.get("pid"), int)
                    and not isinstance(row.get("pid"), bool)):
                started = row.get("started")
                if started is not None and (not isinstance(started, int)
                                            or isinstance(started, bool)):
                    continue
                out.append({"id": row["id"], "pid": row["pid"],
                            "started": started})
        return out, "ok"

    def _do_recover(self):
        rows, state = self._read_rows()
        if state == "none":
            return
        if state == "bad":
            self._remove_state()             # unreadable: nothing to act on
            return
        try:
            age = time.time() - self.state_path().stat().st_mtime
        except OSError:
            age = 0.0
        if age > self.max_age:
            # Long ago: the person may have changed the sound since; never
            # unmute on a stale list.
            self._remove_state()
            log.info("An old list of muted sound was dropped without "
                     "unmuting anything")
            return
        wanted = {(r["id"], r["pid"]): r["started"] for r in rows}
        backend = self._backend
        if backend is None:
            return                            # keep the file for next time
        try:
            keys = [s.key for s in backend.list_sessions()
                    if s.key in wanted
                    and backend.start_time(s.pid) == wanted[s.key]]
            if keys:
                result = backend.set_mute(keys, False)
                if getattr(result, "failed", None):
                    raise AudioError("a session refused")
        except Exception as exc:
            self._note("recover", exc)
            return                            # keep the file for next time
        self._remove_state()
        if keys:
            log.info("Other sound unmuted after an unclean stop "
                     "(%d sessions)", len(keys))

    # -- the state file -----------------------------------------------------------------

    def _write_state(self, entries):
        path = self.state_path()
        tmp = path.parent / f".audio-muted-{os.getpid()}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"version": STATE_VERSION,
                                       "muted": entries}), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as exc:
            self._note("state file", exc)
            try:
                tmp.unlink()
            except OSError:
                pass
            return False

    def _remove_state(self):
        try:
            self.state_path().unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._note("state file", exc)

    def _note(self, what, exc):
        """One quiet log line per kind of failure, at most every 30 s. Only the
        exception's class is logged."""
        now = time.monotonic()
        last = self._noted.get(what)
        if last is not None and now - last < LOG_EVERY_SECONDS:
            return
        self._noted[what] = now
        log.warning("Muting other sound: %s did not work (%s)", what,
                    type(exc).__name__)


# -- Windows Core Audio through ctypes ------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    h = text.strip("{}").replace("-", "")
    g = _GUID()
    g.Data1 = int(h[0:8], 16)
    g.Data2 = int(h[8:12], 16)
    g.Data3 = int(h[12:16], 16)
    for i in range(8):
        g.Data4[i] = int(h[16 + 2 * i:18 + 2 * i], 16)
    return g


_CLSID_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_IID_MANAGER2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
_IID_CONTROL2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
_IID_VOLUME = "87CE5498-68D6-44E5-9215-6DA47EF883D8"

_CLSCTX_ALL = 23
_RENDER = 0                      # EDataFlow eRender
_ACTIVE = 1                      # DEVICE_STATE_ACTIVE
_EXPIRED = 2                     # AudioSessionStateExpired
_RPC_E_CHANGED_MODE = 0x80010106
_PPVOID = ctypes.POINTER(ctypes.c_void_p)

_dlls = {}


def _dll(name):
    found = _dlls.get(name)
    if found is None:
        if sys.platform != "win32":
            raise AudioError("not Windows")
        found = _dlls[name] = ctypes.WinDLL(name, use_last_error=True)
        if name == "ole32":
            found.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            found.CoInitializeEx.restype = ctypes.c_long
            found.CoUninitialize.argtypes = []
            found.CoUninitialize.restype = None
            found.CoCreateInstance.argtypes = [
                ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(_GUID), _PPVOID]
            found.CoCreateInstance.restype = ctypes.c_long
            found.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            found.CoTaskMemFree.restype = None
        elif name == "kernel32":
            found.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                          ctypes.c_ulong]
            found.OpenProcess.restype = ctypes.c_void_p
            found.CloseHandle.argtypes = [ctypes.c_void_p]
            found.CloseHandle.restype = ctypes.c_int
            found.GetProcessTimes.argtypes = [ctypes.c_void_p] + [
                ctypes.POINTER(ctypes.c_ulonglong)] * 4
            found.GetProcessTimes.restype = ctypes.c_int
            found.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong,
                                                       ctypes.c_ulong]
            found.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
            found.Process32FirstW.argtypes = [ctypes.c_void_p,
                                              ctypes.c_void_p]
            found.Process32FirstW.restype = ctypes.c_int
            found.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            found.Process32NextW.restype = ctypes.c_int
    return found


def _check(hr):
    if hr < 0:
        raise AudioError(f"0x{hr & 0xFFFFFFFF:08X}")
    return hr


class _Iface:
    """A raw COM interface pointer: call a method by its vtable index."""

    def __init__(self, ptr):
        if not ptr:
            raise AudioError("no interface")
        self.ptr = ptr

    def call(self, index, argtypes, *args):
        vtbl = ctypes.cast(self.ptr, _PPVOID)[0]
        addr = ctypes.cast(vtbl, _PPVOID)[index]
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        return proto(addr)(self.ptr, *args)

    def query(self, iid_text):
        iid, out = _guid(iid_text), ctypes.c_void_p()
        _check(self.call(0, (ctypes.POINTER(_GUID), _PPVOID),
                         ctypes.byref(iid), ctypes.byref(out)))
        return _Iface(out.value)

    def child(self, index, argtypes, *args):
        """Call a method whose LAST argument is an out interface pointer."""
        out = ctypes.c_void_p()
        _check(self.call(index, argtypes, *args, ctypes.byref(out)))
        return _Iface(out.value)

    def string(self, index):
        out = ctypes.c_void_p()
        _check(self.call(index, (_PPVOID,), ctypes.byref(out)))
        try:
            return ctypes.wstring_at(out.value) if out.value else ""
        finally:
            if out.value:
                _dll("ole32").CoTaskMemFree(out.value)

    def release(self):
        try:
            self.call(2, ())
        except Exception:
            pass


class _ProcessEntry(ctypes.Structure):          # PROCESSENTRY32W
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260)]


def own_pids():
    """This process and every process below it (the window's browser and its
    helpers): none of them is ever muted. Never raises."""
    me = os.getpid()
    out = {me}
    try:
        k32 = _dll("kernel32")
        snap = k32.CreateToolhelp32Snapshot(2, 0)       # TH32CS_SNAPPROCESS
        if not snap or snap == ctypes.c_void_p(-1).value:
            return out
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            parent = {}
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                parent[entry.th32ProcessID] = entry.th32ParentProcessID
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
        grew = True
        while grew:
            grew = False
            for pid, ppid in parent.items():
                if ppid in out and pid not in out:
                    out.add(pid)
                    grew = True
    except Exception:
        pass
    return out


def process_start_time(pid):
    """When the process started (a FILETIME as an int), or None if it cannot
    be read (no such process, no access)."""
    if not pid:
        return None
    try:
        k32 = _dll("kernel32")
        handle = k32.OpenProcess(0x1000, 0, int(pid))   # QUERY_LIMITED_INFO
        if not handle:
            return None
        try:
            times = [ctypes.c_ulonglong() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *[ctypes.byref(t)
                                                 for t in times]):
                return None
            return int(times[0].value)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


class WasapiBackend:
    """The real thing. begin() and end() must run on the thread that calls
    the rest (COM is initialised for that thread)."""

    def __init__(self):
        self._uninit = False

    def begin(self):
        hr = _dll("ole32").CoInitializeEx(None, 0)      # multithreaded
        if hr in (0, 1):
            self._uninit = True
        elif (hr & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
            _check(hr)

    def end(self):
        if self._uninit:
            self._uninit = False
            _dll("ole32").CoUninitialize()

    def own_pids(self):
        return own_pids()

    def start_time(self, pid):
        return process_start_time(pid)

    def list_sessions(self):
        found = []

        def visit(instance, pid, system, volume):
            muted = ctypes.c_int()
            _check(volume.call(6, (ctypes.POINTER(ctypes.c_int),),
                               ctypes.byref(muted)))           # GetMute
            found.append(Session(instance, pid, muted.value, system))
        self._scan(visit)
        return found

    def set_mute(self, keys, muted):
        """Set the mute flag of the sessions named by (ident, pid) keys;
        returns a MuteResult: the keys that were set, and in .failed the keys
        that were found but refused. Sessions that are gone are in neither."""
        want, done = set(keys), MuteResult()
        failed = set()

        def visit(instance, pid, system, volume):
            key = (ident_of(instance), pid)
            if key in want and key not in done:
                try:
                    _check(volume.call(5, (ctypes.c_int, ctypes.c_void_p),
                                       1 if muted else 0, None))  # SetMute
                except AudioError:
                    failed.add(key)          # found, but it refused
                    return
                done.add(key)
        self._scan(visit)
        done.failed = frozenset(failed - done)
        return done

    # -- walking the sessions -------------------------------------------------------

    def _scan(self, visit):
        enum = _Iface(self._create())
        try:
            devices = enum.child(3, (ctypes.c_ulong, ctypes.c_ulong, _PPVOID),
                                 _RENDER, _ACTIVE)    # EnumAudioEndpoints
            try:
                count = ctypes.c_uint()
                _check(devices.call(3, (ctypes.POINTER(ctypes.c_uint),),
                                    ctypes.byref(count)))
                failed, last = 0, None
                for i in range(count.value):
                    try:
                        self._scan_device(devices, i, visit)
                    except AudioError as exc:
                        failed, last = failed + 1, exc
                if count.value and failed == count.value:
                    raise last
            finally:
                devices.release()
        finally:
            enum.release()

    @staticmethod
    def _create():
        out = ctypes.c_void_p()
        clsid, iid = _guid(_CLSID_ENUMERATOR), _guid(_IID_ENUMERATOR)
        _check(_dll("ole32").CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_ALL, ctypes.byref(iid),
            ctypes.byref(out)))
        return out.value

    def _scan_device(self, devices, index, visit):
        device = devices.child(4, (ctypes.c_uint, _PPVOID), index)  # Item
        try:
            iid = _guid(_IID_MANAGER2)
            manager = device.child(3, (ctypes.POINTER(_GUID), ctypes.c_ulong,
                                       ctypes.c_void_p, _PPVOID),
                                   ctypes.byref(iid), _CLSCTX_ALL, None)
            try:
                sessions = manager.child(5, (_PPVOID,))    # GetSessionEnumerator
                try:
                    n = ctypes.c_int()
                    _check(sessions.call(3, (ctypes.POINTER(ctypes.c_int),),
                                         ctypes.byref(n)))
                    for j in range(n.value):
                        try:
                            control = sessions.child(4, (ctypes.c_int, _PPVOID), j)
                        except AudioError:
                            continue
                        try:
                            self._visit(control, visit)
                        except AudioError:
                            pass                  # refused: skip this session
                        finally:
                            control.release()
                finally:
                    sessions.release()
            finally:
                manager.release()
        finally:
            device.release()

    @staticmethod
    def _visit(control, visit):
        state = ctypes.c_int()
        _check(control.call(3, (ctypes.POINTER(ctypes.c_int),),
                            ctypes.byref(state)))             # GetState
        if state.value == _EXPIRED:
            return
        control2 = control.query(_IID_CONTROL2)
        try:
            instance = control2.string(13)     # GetSessionInstanceIdentifier
            pid = ctypes.c_ulong()
            _check(control2.call(14, (ctypes.POINTER(ctypes.c_ulong),),
                                 ctypes.byref(pid)))          # GetProcessId
            system = control2.call(15, ()) == 0     # IsSystemSoundsSession
            volume = control.query(_IID_VOLUME)
            try:
                visit(instance, pid.value, system, volume)
            finally:
                volume.release()
        finally:
            control2.release()
