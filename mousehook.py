"""App side of the mouse-button hook: starts and talks to mousehelper.py.

    hook = MouseHook({5}, key_groups=[(0x11, 0xA2, 0xA3)])   # ctrl+mouse4
    hook.poll()            # every tick: heartbeat, start/hang detection
    hook.state             # 'starting' | 'active' | 'dead' | 'stopped'
    hook.is_down(5)        # held, as reported by the hook
    hook.stop()

Why a separate process: see mousehelper.py. The helper is put in a Job
object that kills it when this process ends (even by TerminateProcess), and
it also exits by itself when this process is gone, the pipe breaks, or it
gets no ping for 30 s. While the hook is 'starting' or 'dead', is_down()
says False and callers fall back to GetAsyncKeyState.
"""

import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger("holdtotalk.mousehook")

HELPER = Path(__file__).resolve().with_name("mousehelper.py")
WM_APP = 0x8000
CMD_PING, CMD_QUIT, CMD_STATS, CMD_SIM = (WM_APP + 1, WM_APP + 2,
                                          WM_APP + 3, WM_APP + 4)
START_TIMEOUT = 5.0
PING_EVERY = 1.0
PONG_TIMEOUT = 3.0
STALL_GAP = 1.0          # poll() not called for this long: we were stalled
CREATE_NO_WINDOW = 0x08000000

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
_kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                               wintypes.HANDLE]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                  wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _EXTENDED_LIMITS(ctypes.Structure):
    _fields_ = [("Basic", _BASIC_LIMITS), ("Io", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_job = None
_job_lock = threading.Lock()


def _kill_on_close_job():
    """A Job object that kills its processes when our last handle to it
    closes, i.e. when this process ends however it ends."""
    global _job
    with _job_lock:
        if _job is None:
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject")
            info = _EXTENDED_LIMITS()
            info.Basic.LimitFlags = 0x2000     # KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                    job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                err = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                raise OSError(err, "SetInformationJobObject")
            _job = job
        return _job


def helper_python():
    """The interpreter for the helper: the base (non-venv) pythonw.exe when
    available, so there is no venv launcher process in between and no
    console. The helper only uses the standard library."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    for exe in (base.with_name("pythonw.exe"), base,
                Path(sys.executable)):
        if exe.exists():
            return str(exe)
    return sys.executable


def key_groups_arg(groups):
    return "+".join(".".join(f"{vk:x}" for vk in g) for g in groups)


class MouseHook:
    """One helper process. Not restartable: make a new one instead.

    Thread-safety: state/is_down() may be read from any thread; poll(),
    stop(), simulate() and stats() from one thread (the app's Tk thread).
    """

    def __init__(self, buttons, key_groups=(), capture=False):
        self.buttons = frozenset(buttons)
        self.key_groups = [tuple(g) for g in key_groups]
        self.capture = bool(capture)
        self.state = "starting"
        self.error = ""
        self.tid = self.helper_pid = None
        self.desktop_check = False
        self.pipe_nowait = False
        self.started_at = time.monotonic()
        self.active_since = None
        self._down = set()
        self._lock = threading.Lock()
        self._ping_seq = 0
        self._last_ping = 0.0
        self._last_pong = None
        self._last_poll = None
        self._unanswered_since = None    # send time of the oldest open ping
        self._unanswered_seq = None
        self._stats = None
        self._stats_event = threading.Event()
        cmd = [helper_python(), "-B", "-I", str(HELPER),
               "--parent", str(os.getpid()),
               "--buttons", ",".join(str(b) for b in sorted(self.buttons))]
        if self.key_groups:
            cmd += ["--keys", key_groups_arg(self.key_groups)]
        if self.capture:
            cmd.append("--capture")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            cwd=str(HELPER.parent))
        self.in_job = False
        try:
            if not _kernel32.AssignProcessToJobObject(
                    _kill_on_close_job(), int(self.proc._handle)):
                raise OSError(ctypes.get_last_error(),
                              "AssignProcessToJobObject")
            self.in_job = True
        except Exception as exc:     # it still exits when we do (parent watch)
            log.warning("Mouse helper not in a kill-on-close job: %s", exc)
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._read, daemon=True,
                                        name="mousehook-reader")
        self._reader.start()

    # -- state
    def is_down(self, vk):
        with self._lock:
            return self.state == "active" and vk in self._down

    def held(self):
        with self._lock:
            return set(self._down) if self.state == "active" else set()

    @property
    def alive(self):
        return self.state in ("starting", "active")

    # -- reading the helper's output (reader thread)
    def _read(self):
        try:
            for raw in self.proc.stdout:
                self._line(raw.decode("ascii", "replace").split())
        except Exception as exc:
            self.error = self.error or f"reading the helper failed: {exc}"
        finally:
            code = None
            try:
                code = self.proc.wait(2)
            except Exception:
                pass
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            with self._lock:
                self._down.clear()
                if self.state != "stopped":
                    self.state = "dead"
                    if not self.error:
                        self.error = f"helper exited (code {code})"
            self._stats_event.set()

    def _line(self, parts):
        if not parts:
            return
        kind = parts[0]
        try:
            if kind in ("D", "U"):
                vk = int(parts[1])
                with self._lock:
                    (self._down.add if kind == "D" else self._down.discard)(vk)
            elif kind == "P":
                self._pong(int(parts[1]) if len(parts) > 1 else None)
            elif kind == "READY":
                self.tid, self.helper_pid = int(parts[1]), int(parts[2])
                self.desktop_check = parts[3] == "1"
                self.pipe_nowait = parts[4:5] == ["1"]
                self._last_pong = time.monotonic()
                with self._lock:
                    if self.state == "starting":
                        self.state = "active"
                        self.active_since = time.monotonic()
            elif kind == "S":
                n = [int(p) for p in parts[1:]]
                self._stats = {"count": n[0], "total_ns": n[1], "max_ns": n[2],
                               "button_events": n[3], "dropped": n[4],
                               "buckets": n[5:]}
                self._stats_event.set()
            elif kind == "E":
                self.error = " ".join(parts[1:])
                log.warning("Mouse helper: %s", self.error)
        except (IndexError, ValueError):
            log.debug("Mouse helper sent a bad line: %r", parts)

    # -- control (one thread)
    def _post(self, msg, wparam=0, lparam=0):
        if self.tid is None:
            return False
        return bool(_user32.PostThreadMessageW(self.tid, msg, wparam, lparam))

    def _fail(self, why):
        with self._lock:
            if self.state in ("starting", "active"):
                self.state = "dead"
                self.error = why
                self._down.clear()
        self._kill()

    def poll(self, now=None):
        """Heartbeat; call often (every app tick). Returns self.state.

        The helper is judged only on pings it has had PONG_TIMEOUT to
        answer: dead when the oldest unanswered ping is older than that. If
        poll() itself was not called for a while (our thread stalled: a
        slow callback, sleep/resume), the wait so far does not count against
        the helper; it gets a fresh PONG_TIMEOUT from now."""
        now = time.monotonic() if now is None else now
        last_poll, self._last_poll = self._last_poll, now
        if self.state == "starting":
            if now - self.started_at > START_TIMEOUT:
                self._fail("helper did not start within "
                           f"{START_TIMEOUT:.0f} s")
        elif self.state == "active":
            stalled = last_poll is not None and now - last_poll > STALL_GAP
            with self._lock:
                if stalled and self._unanswered_since is not None:
                    self._unanswered_since = now
                if now - self._last_ping >= PING_EVERY or stalled:
                    self._ping_seq += 1
                    self._last_ping = now
                    if self._unanswered_since is None:
                        self._unanswered_since = now
                        self._unanswered_seq = self._ping_seq
                    seq = self._ping_seq
                else:
                    seq = None
                late = (self._unanswered_since is not None
                        and now - self._unanswered_since > PONG_TIMEOUT)
            if seq is not None:
                self._post(CMD_PING, seq)
            if late:
                self._fail("helper stopped answering")
        return self.state

    def _pong(self, seq):
        """A pong arrived (reader thread)."""
        with self._lock:
            self._last_pong = time.monotonic()
            if seq is None or (self._unanswered_seq is not None
                               and seq >= self._unanswered_seq):
                self._unanswered_since = self._unanswered_seq = None

    def simulate(self, vk, down):
        """Feed a button event through the helper's decision code (the same
        code a real press runs, minus the OS). For tests."""
        return self._post(CMD_SIM, int(vk), 1 if down else 0)

    def stats(self, timeout=2.0):
        """The helper's hook-callback timing counters, or None."""
        self._stats_event.clear()
        if not self._post(CMD_STATS):
            return None
        self._stats_event.wait(timeout)
        return self._stats

    def stop(self, timeout=1.0):
        """Unhook and end the helper. Safe to call twice."""
        with self._lock:
            already = self.state == "stopped"
            self.state = "stopped"
            self._down.clear()
        if already:
            return
        self._post(CMD_QUIT)
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._kill()
        if threading.current_thread() is not self._reader:
            self._reader.join(1)

    def _kill(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except Exception:
            pass
        if self.helper_pid and self.helper_pid != self.pid:
            handle = _kernel32.OpenProcess(0x0001, False, self.helper_pid)
            if handle:
                _kernel32.TerminateProcess(handle, 1)
                _kernel32.CloseHandle(handle)
