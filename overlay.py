"""The indicator pill: always on top, click-through, never takes focus.

    ov = Overlay(root)                 # root: the app's (withdrawn) Tk root
    ov.update("listening", level)      # call every tick; "hidden" | "listening" | "working"
    ov.destroy()

Must be used from the Tk thread. Shown/hidden by moving it on/off screen,
never by re-mapping, because mapping a window is what activates it.

The move is done with SetWindowPos, not only through Tk: a Tk geometry
change is applied later (at idle), and a z-order change in between (our own
HWND_TOPMOST re-raise when another topmost window is above the pill) makes
Tk drop the pending move, which left the pill off-screen for a whole hold.
While shown, the pill is re-raised to the top of the topmost band on every
state change and every REASSERT_SECONDS, without activating it.

Placement: centred above the work area (so above the taskbar, wherever it
is) of the monitor the mouse pointer is on, recomputed at every show. The
process is system-DPI-aware, so all coordinates here (pointer, monitor work
areas, the pill's size) are in the system-DPI logical space that Windows
presents to such processes; on a monitor with another scale factor Windows
bitmap-scales the pill but the maths stays in that one space.
"""

import collections
import ctypes
import time
import tkinter as tk
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080     # no taskbar button, not in Alt+Tab
WS_EX_TRANSPARENT = 0x00000020    # clicks go through
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
OVERLAY_EXSTYLE = (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
                   | WS_EX_LAYERED)
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SW_SHOWNOACTIVATE = 4
MONITOR_DEFAULTTONEAREST = 2
AWAY = (-20000, -20000)
REASSERT_SECONDS = 0.25   # re-raise period while shown (the tick is 15 ms)
MARGIN = 36               # gap above the work area, in 96-dpi pixels


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR,
                                   ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


def cursor_pos():
    """The mouse pointer (x, y), or None if Windows won't say."""
    pt = wintypes.POINT()
    return (pt.x, pt.y) if user32.GetCursorPos(ctypes.byref(pt)) else None


def work_area_at(point):
    """(left, top, right, bottom) work area of the monitor nearest `point`
    (the primary monitor when point is None), or None."""
    x, y = point if point is not None else (0, 0)
    mon = user32.MonitorFromPoint(wintypes.POINT(x, y),
                                  MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    r = info.rcWork
    return (r.left, r.top, r.right, r.bottom)


def place(work, w, h, margin):
    """Top-left (x, y) of a w x h pill centred above the bottom of the work
    area `work` (left, top, right, bottom), `margin` above it, kept inside."""
    left, top, right, bottom = work
    x = left + (right - left - w) // 2
    y = max(top, bottom - h - margin)
    return x, y

TRANSPARENT = "#010203"
PILL_BG = "#17181c"      # defaults; see settings.OVERLAY_DEFAULTS
FG = "#f4f4f5"
BAR_COUNT = 11
STATES = ("hidden", "listening", "working", "paused", "resumed")
DIM_MIX = 0.35           # inactive dots: this much colour, rest background


def mix(a, b, t):
    """The colour t of the way from "#rrggbb" a to b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}"
                         for x, y in zip(ca, cb))


def safe_background(color):
    """The pill background, never the colour Tk keys out as transparent
    (that would punch a hole in the pill)."""
    return "#010204" if color.lower() == TRANSPARENT else color


def enable_dpi_awareness():
    """Call before creating the Tk root so the pill is crisp and sized right."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class Overlay:
    # Injectable for tests: pointer position, monitor lookup, clock.
    cursor_pos = staticmethod(cursor_pos)
    work_area_at = staticmethod(work_area_at)
    clock = staticmethod(time.monotonic)

    def __init__(self, root, color=FG, background=PILL_BG):
        self.root = root
        self.color, self.background = color, safe_background(background)
        self.dim = mix(background, color, DIM_MIX)
        self.win = win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-alpha", 0.94)
        win.attributes("-transparentcolor", TRANSPARENT)
        self.scale = s = root.winfo_fpixels("1i") / 96.0
        self.w, self.h = int(132 * s), int(34 * s)
        self.canvas = tk.Canvas(win, width=self.w, height=self.h,
                                bg=TRANSPARENT, highlightthickness=0,
                                takefocus=0)
        self.canvas.pack()
        self._build()
        self.pos = AWAY          # where the pill is meant to be now
        self.home = None         # Tk geometry of the last shown position
        self.away = f"{self.w}x{self.h}+{AWAY[0]}+{AWAY[1]}"
        win.geometry(self.away)
        win.update_idletasks()

        # Tk resets the extended style whenever it touches attributes, so
        # set ours last, then show the window ourselves without activating
        # it (Tk's own deiconify would activate it).
        self.hwnd = user32.GetParent(win.winfo_id())
        self._apply_style()
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        win.update_idletasks()
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

        self.levels = collections.deque([0.0] * BAR_COUNT, maxlen=BAR_COUNT)
        self.state = "hidden"
        self.frame = 0
        self.visible = False
        self._next_raise = 0.0

    def _check_handle(self):
        """Re-resolve the cached Win32 handle if Tk has replaced the window
        (or it is gone); the style is re-applied by the caller."""
        live = user32.GetParent(self.win.winfo_id())
        if live and live != self.hwnd and user32.IsWindow(live):
            self.hwnd = live

    def _home(self):
        """Where to show the pill: above the work area of the monitor the
        pointer is on; the primary screen as before if that fails."""
        try:
            work = self.work_area_at(self.cursor_pos())
        except Exception:
            work = None
        if not work:
            s = self.scale
            work = (0, 0, self.root.winfo_screenwidth(),
                    self.root.winfo_screenheight() - int(48 * s))
        return place(work, self.w, self.h, int(MARGIN * self.scale))

    def _move(self, pos, zorder):
        """Put the pill at pos, re-raising it to the top of the topmost band
        if zorder. Never activates it. Tk is told too, so a later Tk geometry
        update cannot move it back."""
        self._check_handle()
        self._apply_style()
        geom = f"{self.w}x{self.h}+{pos[0]}+{pos[1]}"
        if self.win.geometry() != geom:
            self.win.geometry(geom)
        flags = SWP_NOSIZE | SWP_NOACTIVATE | (0 if zorder else SWP_NOZORDER)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST if zorder else None,
                            pos[0], pos[1], 0, 0, flags)

    def _apply_style(self):
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        if style & OVERLAY_EXSTYLE != OVERLAY_EXSTYLE:
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                                  style | OVERLAY_EXSTYLE)

    def exstyle(self):
        return user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

    def _build(self):
        c, w, h = self.canvas, self.w, self.h
        bg, fg = self.background, self.color
        self.shapes = [
            c.create_oval(0, 0, h, h, fill=bg, outline=""),
            c.create_oval(w - h, 0, w, h, fill=bg, outline=""),
            c.create_rectangle(h // 2, 0, w - h // 2, h, fill=bg, outline="")]
        self.bars = [c.create_rectangle(0, 0, 0, 0, fill=fg, outline="")
                     for _ in range(BAR_COUNT)]
        self.dots = [c.create_oval(0, 0, 0, 0, fill=fg, outline="")
                     for _ in range(3)]
        # "resumed": a play triangle (hidden otherwise).
        self.icon = c.create_polygon(0, 0, 0, 0, 0, 0, fill=fg, outline="",
                                     state="hidden")

    def set_colors(self, color, background):
        """Recolour the pill ("#rrggbb" each); shows from the next frame."""
        self.color, self.background = color, safe_background(background)
        self.dim = mix(background, color, DIM_MIX)
        for item in self.shapes:
            self.canvas.itemconfigure(item, fill=self.background)
        for item in self.bars:
            self.canvas.itemconfigure(item, fill=self.color)
        icon = getattr(self, "icon", None)
        if icon is not None:
            self.canvas.itemconfigure(icon, fill=self.color)

    def update(self, state, level=0.0):
        """Advance one animation frame. `level` is mic RMS (0..~0.3).
        States: hidden, listening, working, and paused / resumed (a pause or
        play symbol, shown briefly when dictation is paused or resumed)."""
        if state not in STATES:
            raise ValueError(f"Unknown overlay state {state!r}")
        if state == "listening" and self.state != "listening":
            self.levels.extend([0.0] * BAR_COUNT)
        changed = state != self.state
        self.state = state
        self.frame += 1
        show = state != "hidden"
        now = self.clock()
        if show != self.visible:
            # If this raises, visible is unchanged and the next tick retries.
            pos = self._home() if show else AWAY
            self._move(pos, zorder=show)
            self.pos, self.visible = pos, show
            if show:
                self.home = f"{self.w}x{self.h}+{pos[0]}+{pos[1]}"
            self._next_raise = now + REASSERT_SECONDS
        elif show and (changed or now >= self._next_raise):
            self._next_raise = now + REASSERT_SECONDS
            self._move(self.pos, zorder=True)
        if not show or self.frame % 2:
            return

        c, w, h, s = self.canvas, self.w, self.h, self.scale
        icon = getattr(self, "icon", None)
        if icon is not None:
            c.itemconfigure(icon, state="normal" if state == "resumed"
                            else "hidden")
        if state in ("paused", "resumed"):
            for dot in self.dots:
                c.itemconfigure(dot, state="hidden")
            for bar in self.bars:
                c.itemconfigure(bar, state="hidden")
            half = h * 0.26
            if state == "paused":          # two upright bars
                bar_w, gap = 4 * s, 4 * s
                for i, bar in enumerate(self.bars[:2]):
                    x = w / 2 - gap / 2 - bar_w + i * (bar_w + gap)
                    c.coords(bar, x, h / 2 - half, x + bar_w, h / 2 + half)
                    c.itemconfigure(bar, state="normal")
            elif icon is not None:         # a play triangle
                x = w / 2 - half * 0.6
                c.coords(icon, x, h / 2 - half, x, h / 2 + half,
                         x + half * 1.6, h / 2)
            return
        if state == "listening":
            self.levels.append(min(1.0, level * 14) ** 0.6)
            bar_w = gap = 3 * s
            left = (w - (BAR_COUNT * bar_w + (BAR_COUNT - 1) * gap)) / 2
            for i, (bar, lv) in enumerate(zip(self.bars, self.levels)):
                half = max(1.5 * s, lv * (h * 0.36))
                x = left + i * (bar_w + gap)
                c.coords(bar, x, h / 2 - half, x + bar_w, h / 2 + half)
                c.itemconfigure(bar, state="normal")
            for dot in self.dots:
                c.itemconfigure(dot, state="hidden")
        else:
            for bar in self.bars:
                c.itemconfigure(bar, state="hidden")
            active = (self.frame // 14) % 3
            radius, gap = 2.6 * s, 10 * s
            for i, dot in enumerate(self.dots):
                x = w / 2 + (i - 1) * gap
                c.coords(dot, x - radius, h / 2 - radius,
                         x + radius, h / 2 + radius)
                c.itemconfigure(dot, state="normal",
                                fill=self.color if i == active else self.dim)

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
