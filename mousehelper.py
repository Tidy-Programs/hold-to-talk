"""Mouse-button hook helper process (started by mousehook.MouseHook).

    mousehelper.py --parent PID --buttons 5[,6,4] [--keys 11.a2.a3+5b.5c]
                   [--capture]

A tiny single-threaded process whose only job is a WH_MOUSE_LL hook. It is
separate from the app on purpose: Windows calls a low-level hook for every
mouse event (moves too) and the pointer waits for it, so it must answer in
microseconds. Inside the app it would wait for Python's GIL whenever Whisper
or the pipeline is busy; here nothing else ever holds the GIL.

The hook swallows only the down/up events of the given buttons (VK 4 middle,
5 XBUTTON1/Back, 6 XBUTTON2/Forward), and a down only while every --keys
group has a key held (groups '+'-separated, alternatives '.'-separated, hex
VKs). An up is swallowed only if its down was. Everything else is passed on
untouched. --capture swallows the middle and both side buttons.

Output (stdout pipe, one ASCII line per event):
    READY <thread id> <pid> <desktop check 0/1>
    D <vk> / U <vk>          button swallowed down / up
    P <n>                    answer to a ping
    S <count> <total_ns> <max_ns> <buttons> <dropped> <b0> ... <b6>
                             callback timing (own code, not the rest of
                             the hook chain); buckets <20us <50us <100us
                             <250us <1ms <10ms >=10ms
    E <text>                 error (then exit)
Input: thread messages posted by the app (PostThreadMessage):
    WM_APP+1 ping (wParam = n)    WM_APP+2 quit       WM_APP+3 stats
    WM_APP+4 simulate (wParam = vk, lParam = 1 down / 0 up): runs the same
             decision code as a real button event (used by tests).
Exits when the app process ends, the pipe breaks, on quit, or after 30 s
without a ping.
"""

import ctypes
import gc
import sys
import time
from ctypes import wintypes

WM_TIMER = 0x0113
WM_APP = 0x8000
CMD_PING, CMD_QUIT, CMD_STATS, CMD_SIM = (WM_APP + 1, WM_APP + 2,
                                          WM_APP + 3, WM_APP + 4)
WH_MOUSE_LL = 14
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
TICK_MS = 250
REARM_S = 10.0
PING_TIMEOUT_S = 30.0
STALL_S = 5.0              # timer ticks this far apart: the system slept
BUCKETS_NS = (20_000, 50_000, 100_000, 250_000, 1_000_000, 10_000_000)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                              wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


PMSLL = ctypes.POINTER(MSLLHOOKSTRUCT)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                    wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                               ctypes.c_void_p]
kernel32.SetNamedPipeHandleState.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ctypes.c_void_p]


def _desktop_name(handle):
    buf = ctypes.create_unicode_buffer(256)
    need = wintypes.DWORD()
    if not user32.GetUserObjectInformationW(handle, 2, buf,   # UOI_NAME
                                            ctypes.sizeof(buf),
                                            ctypes.byref(need)):
        return None
    return buf.value


def input_desktop_name():
    """Name of the desktop receiving input, None if we may not open it
    (the lock screen / UAC secure desktop)."""
    handle = user32.OpenInputDesktop(0, False, 0x0001)   # DESKTOP_READOBJECTS
    if not handle:
        return None
    try:
        return _desktop_name(handle)
    finally:
        user32.CloseDesktop(handle)


class Helper:
    def __init__(self, buttons, key_groups, out):
        self.buttons = frozenset(buttons)
        self.key_groups = [tuple(g) for g in key_groups]
        self.out = out                    # fn(str) -> None
        self.swallowed = set()            # buttons whose down we ate
        self.count = self.total = self.max = self.button_events = 0
        self.dropped = 0
        self.buckets = [0] * (len(BUCKETS_NS) + 1)

    def combo_ok(self):
        state = user32.GetAsyncKeyState
        return all(any(state(vk) & 0x8000 for vk in g)
                   for g in self.key_groups)

    def button(self, vk, down):
        """Decide one button event; True = swallow it. Emits D/U lines."""
        if down:
            if vk in self.swallowed or (vk in self.buttons
                                        and self.combo_ok()):
                self.swallowed.add(vk)
                self.out(f"D {vk}\n")
                return True
            return False
        if vk in self.swallowed:
            self.swallowed.discard(vk)
            self.out(f"U {vk}\n")
            return True
        return False

    def release_all(self):
        for vk in sorted(self.swallowed):
            self.out(f"U {vk}\n")
        self.swallowed.clear()

    def stats_line(self):
        return ("S %d %d %d %d %d %s\n" % (
            self.count, self.total, self.max, self.button_events,
            self.dropped, " ".join(map(str, self.buckets))))


def parse_args(argv):
    parent, buttons, groups, capture = None, set(), [], False
    it = iter(argv)
    for arg in it:
        if arg == "--parent":
            parent = int(next(it))
        elif arg == "--buttons":
            buttons = {int(b) for b in next(it).split(",") if b}
        elif arg == "--keys":
            spec = next(it)
            groups = [tuple(int(v, 16) for v in g.split(".") if v)
                      for g in spec.split("+") if g]
        elif arg == "--capture":
            capture = True
        else:
            raise SystemExit(f"unknown argument {arg!r}")
    if capture:
        buttons, groups = {4, 5, 6}, []
    if not buttons or not buttons <= {4, 5, 6}:
        raise SystemExit("--buttons must be some of 4,5,6")
    return parent, buttons, groups


def main(argv):
    gc.disable()          # no collector pauses inside the hook callback
    hout = kernel32.GetStdHandle(-11)
    mode = wintypes.DWORD(0x00000001)              # PIPE_NOWAIT
    nowait = bool(kernel32.SetNamedPipeHandleState(hout, ctypes.byref(mode),
                                                   None, None))
    written = wintypes.DWORD()
    broken = []
    helper = None

    def out(line):
        data = line.encode("ascii")
        if not kernel32.WriteFile(hout, data, len(data), ctypes.byref(written),
                                  None):
            if not broken:
                broken.append(ctypes.get_last_error())
                user32.PostQuitMessage(0)
        elif written.value < len(data) and helper is not None:
            helper.dropped += 1                    # app not reading: drop

    try:
        parent_pid, buttons, groups = parse_args(argv)
    except SystemExit as exc:
        out(f"E {exc}\n")
        return 2
    helper = Helper(buttons, groups, out)

    parent = None
    if parent_pid:
        parent = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNC
        if not parent:
            out("E parent process not found\n")
            return 3

    perf = time.perf_counter_ns
    cast = ctypes.cast
    call_next = user32.CallNextHookEx
    watched = (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP)
    buckets = BUCKETS_NS

    def proc(code, wparam, lparam):
        t0 = perf()
        swallow = False
        try:
            if code == 0 and wparam in watched:
                helper.button_events += 1
                if wparam >= WM_XBUTTONDOWN:
                    which = cast(lparam, PMSLL)[0].mouseData >> 16
                    vk = 5 if which == 1 else 6 if which == 2 else 0
                else:
                    vk = 4
                if vk:
                    swallow = helper.button(vk, wparam in (WM_MBUTTONDOWN,
                                                           WM_XBUTTONDOWN))
        except Exception:
            swallow = False
        # Own time only: CallNextHookEx runs the rest of the hook chain
        # (other programs' hooks) synchronously, which is not ours.
        dt = perf() - t0
        helper.count += 1
        helper.total += dt
        if dt > helper.max:
            helper.max = dt
        i = 0
        while i < 6 and dt >= buckets[i]:
            i += 1
        helper.buckets[i] += 1
        if swallow:
            return 1
        return call_next(None, code, wparam, lparam)

    callback = HOOKPROC(proc)
    module = kernel32.GetModuleHandleW(None)

    def install():
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, callback, module, 0)
        if not hook:
            out(f"E SetWindowsHookEx failed ({ctypes.get_last_error()})\n")
        return hook

    hook = install()
    if not hook:
        return 4

    msg = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # create queue
    tid = kernel32.GetCurrentThreadId()
    own_desktop = _desktop_name(user32.GetThreadDesktop(tid))
    desktop_check = own_desktop is not None and \
        input_desktop_name() == own_desktop
    user32.SetTimer(None, 0, TICK_MS, None)
    out(f"READY {tid} {kernel32.GetCurrentProcessId()} {int(desktop_check)}"
        f" {int(nowait)}\n")

    last_ping = last_rearm = last_tick = time.monotonic()
    code = 0
    try:
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0 or broken:
                break
            m = msg.message
            if m == WM_TIMER:
                now = time.monotonic()
                if now - last_tick > STALL_S:
                    # The whole machine slept (or we were not scheduled):
                    # the app could not ping either; give it time again.
                    last_ping = now
                last_tick = now
                if parent and kernel32.WaitForSingleObject(parent, 0) == 0:
                    break                              # the app is gone
                if now - last_ping > PING_TIMEOUT_S:
                    out("E no ping from the app\n")
                    code = 5
                    break
                if (helper.swallowed and desktop_check
                        and input_desktop_name() != own_desktop):
                    # Locked / UAC / desktop switch: the release will never
                    # reach us, so don't leave the button "held".
                    helper.release_all()
                if now - last_rearm > REARM_S:
                    # Windows silently drops a hook that once took too long;
                    # re-installing (new first, then old off) undoes that.
                    new = install()
                    if new:
                        user32.UnhookWindowsHookEx(hook)
                        hook = new
                    last_rearm = now
            elif m == CMD_PING:
                last_ping = time.monotonic()
                out(f"P {msg.wParam}\n")
            elif m == CMD_QUIT:
                break
            elif m == CMD_STATS:
                out(helper.stats_line())
            elif m == CMD_SIM:
                helper.button(int(msg.wParam), bool(msg.lParam))
    finally:
        user32.UnhookWindowsHookEx(hook)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
