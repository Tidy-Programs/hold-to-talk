"""Keyboard, mouse-button hotkey names, the clipboard and synthetic input.

Keyboard state is polled with GetAsyncKeyState, so there is no keyboard hook
that could add input lag or leave a key "stuck". Mouse side/middle buttons
can be part of the hotkey ("mouse4", "ctrl+mouse5", ...); while one is the
hotkey the app runs a separate low-level mouse hook process (mousehook.py)
so the button stops doing its normal job (e.g. browser Back).

Pasting (paste_text) puts the text on the clipboard, sends Ctrl+V and then
puts back everything the clipboard held before, in every format it held
(text, rich text, HTML, spreadsheet cells, images, copied files...). When
that cannot be done faithfully the text is typed instead and the clipboard
is not touched at all. The clipboard and the synthetic input are reached
through small backend objects so tests can replace both.
"""

import atexit
import contextlib
import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("holdtotalk.keys")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD,
                               ctypes.c_size_t]
user32.keybd_event.restype = None
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR,
                                           ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
    wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
HWND_MESSAGE = wintypes.HWND(-3)
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
gdi32.GetEnhMetaFileBits.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                     ctypes.c_void_p]
gdi32.GetEnhMetaFileBits.restype = wintypes.UINT
gdi32.SetEnhMetaFileBits.argtypes = [wintypes.UINT, ctypes.c_char_p]
gdi32.SetEnhMetaFileBits.restype = wintypes.HANDLE
gdi32.DeleteEnhMetaFile.argtypes = [wintypes.HANDLE]

VK_CONTROL, VK_V, VK_SHIFT, VK_RETURN = 0x11, 0x56, 0x10, 0x0D
VK_MASK = 0xE8          # unassigned; tapped to stop Win/Alt-release side effects
VK_PACKET = 0xE7        # what Unicode input (typed text) looks like
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
GMEM_MOVEABLE = 0x0002

# -- key names -------------------------------------------------------------------
# (canonical name, virtual-key code, aliases). The canonical name is what a
# captured hotkey is saved as; every alias is accepted when parsing. Names
# are lower case with single spaces and never contain "+" (the separator).
_NAMED = [
    ("space", 0x20, ()), ("tab", 0x09, ()), ("caps lock", 0x14, ("capslock",)),
    ("enter", 0x0D, ("return",)), ("backspace", 0x08, ()),
    ("esc", 0x1B, ("escape",)), ("menu", 0x5D, ("apps", "context menu")),
    ("pause", 0x13, ("break", "pause break")),
    ("scroll lock", 0x91, ("scrolllock",)),
    ("print screen", 0x2C, ("printscreen", "prtsc", "prtscn", "prt sc",
                            "print", "snapshot")),
    ("insert", 0x2D, ("ins",)), ("delete", 0x2E, ("del",)),
    ("home", 0x24, ()), ("end", 0x23, ()),
    ("page up", 0x21, ("pageup", "pgup", "pg up", "prior")),
    ("page down", 0x22, ("pagedown", "pgdn", "pg dn", "next")),
    ("up", 0x26, ("up arrow", "arrow up")),
    ("down", 0x28, ("down arrow", "arrow down")),
    ("left", 0x25, ("left arrow", "arrow left")),
    ("right", 0x27, ("right arrow", "arrow right")),
    ("num lock", 0x90, ("numlock",)),
    ("clear", 0x0C, ()),            # keypad 5 while Num Lock is off
    ("num multiply", 0x6A, ("num *", "numpad *", "num star")),
    ("num plus", 0x6B, ("num add", "numpad plus", "numpad add")),
    ("num minus", 0x6D, ("num -", "numpad -", "num subtract",
                         "numpad minus")),
    ("num decimal", 0x6E, ("num .", "numpad .", "num period", "num dot",
                           "numpad decimal")),
    ("num divide", 0x6F, ("num /", "numpad /", "num slash", "numpad divide")),
    # Punctuation keys: named after the US layout; other layouts put other
    # characters on the same keys (plain_hotkey shows the real one).
    ("semicolon", 0xBA, (";",)),
    ("equals", 0xBB, ("=", "plus", "equal")),
    ("comma", 0xBC, (",",)),
    ("minus", 0xBD, ("-", "dash", "hyphen")),
    ("period", 0xBE, (".", "dot", "full stop")),
    ("slash", 0xBF, ("/", "forward slash")),
    ("backtick", 0xC0, ("`", "grave", "tilde")),
    ("left bracket", 0xDB, ("[",)),
    ("backslash", 0xDC, ("\\",)),
    ("right bracket", 0xDD, ("]",)),
    ("quote", 0xDE, ("'", "apostrophe")),
    ("oem 8", 0xDF, ()),
    ("oem 102", 0xE2, ("iso backslash",)),
    # Media and browser keys (keyboards that send these as key codes).
    ("volume mute", 0xAD, ("mute",)),
    ("volume down", 0xAE, ()), ("volume up", 0xAF, ()),
    ("next track", 0xB0, ("media next",)),
    ("previous track", 0xB1, ("prev track", "media previous", "media prev")),
    ("stop media", 0xB2, ("media stop",)),
    ("play pause", 0xB3, ("media play pause", "play/pause", "media play")),
    ("browser back", 0xA6, ()), ("browser forward", 0xA7, ()),
    ("browser refresh", 0xA8, ()), ("browser stop", 0xA9, ()),
    ("browser search", 0xAA, ()), ("browser favorites", 0xAB, ()),
    ("browser home", 0xAC, ()),
    ("launch mail", 0xB4, ("mail",)),
    ("launch media", 0xB5, ("media select",)),
    ("launch app1", 0xB6, ()), ("launch app2", 0xB7, ("calculator",)),
]
for _i in range(10):                                  # keypad digits
    _NAMED.append((f"num {_i}", 0x60 + _i, (f"num{_i}", f"numpad {_i}",
                                            f"numpad{_i}", f"keypad {_i}")))

KEY_NAMES = {
    "ctrl": (0x11,), "control": (0x11,), "shift": (0x10,), "alt": (0x12,),
    "windows": (0x5B, 0x5C), "win": (0x5B, 0x5C),
    "left ctrl": (0xA2,), "right ctrl": (0xA3,),
    "left shift": (0xA0,), "right shift": (0xA1,),
    "left alt": (0xA4,), "right alt": (0xA5,),
    "left windows": (0x5B,), "right windows": (0x5C,),
    "left win": (0x5B,), "right win": (0x5C,),
}
for _name, _vk, _aliases in _NAMED:
    for _alias in (_name,) + tuple(_aliases):
        KEY_NAMES[_alias] = (_vk,)
KEY_NAMES.update({f"f{i}": (0x6F + i,) for i in range(1, 25)})   # F1 = 0x70

MODIFIER_FAMILIES = [
    frozenset({0x11, 0xA2, 0xA3}),  # ctrl
    frozenset({0x10, 0xA0, 0xA1}),  # shift
    frozenset({0x12, 0xA4, 0xA5}),  # alt
    frozenset({0x5B, 0x5C}),        # windows
]
ALL_MODIFIERS = frozenset().union(*MODIFIER_FAMILIES)
# Keys whose bare release does something (Start menu, menu bar) unless
# another key event happens while they are down.
RELEASE_SENSITIVE = frozenset({0x12, 0xA4, 0xA5, 0x5B, 0x5C})

# -- mouse buttons -------------------------------------------------------------
# Virtual-key codes: VK_MBUTTON, VK_XBUTTON1 (Back), VK_XBUTTON2 (Forward).
VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2 = 0x04, 0x05, 0x06
MOUSE_VKS = frozenset({VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2})
MOUSE_CANONICAL = {VK_MBUTTON: "mouse3", VK_XBUTTON1: "mouse4",
                   VK_XBUTTON2: "mouse5"}
MOUSE_DESCRIPTIONS = {
    VK_MBUTTON: "middle button",
    VK_XBUTTON1: "Back side button, XBUTTON1",
    VK_XBUTTON2: "Forward side button, XBUTTON2",
}
MOUSE_NAMES = {
    "mouse3": VK_MBUTTON, "mouse 3": VK_MBUTTON, "mb3": VK_MBUTTON,
    "middle mouse": VK_MBUTTON, "mouse middle": VK_MBUTTON,
    "middle button": VK_MBUTTON, "middle click": VK_MBUTTON,
    "mbutton": VK_MBUTTON, "mmb": VK_MBUTTON,
    "mouse4": VK_XBUTTON1, "mouse 4": VK_XBUTTON1, "mb4": VK_XBUTTON1,
    "xbutton1": VK_XBUTTON1, "xbutton 1": VK_XBUTTON1, "x1": VK_XBUTTON1,
    "mouse back": VK_XBUTTON1, "back button": VK_XBUTTON1,
    "mouse5": VK_XBUTTON2, "mouse 5": VK_XBUTTON2, "mb5": VK_XBUTTON2,
    "xbutton2": VK_XBUTTON2, "xbutton 2": VK_XBUTTON2, "x2": VK_XBUTTON2,
    "mouse forward": VK_XBUTTON2, "forward button": VK_XBUTTON2,
}
KEY_NAMES.update({name: (vk,) for name, vk in MOUSE_NAMES.items()})
# Never bindable: holding a primary button is how people click and drag.
FORBIDDEN_NAMES = frozenset({
    "mouse1", "mouse 1", "mouse2", "mouse 2", "mb1", "mb2", "lbutton",
    "rbutton", "left click", "right click", "left mouse", "right mouse",
    "mouse left", "mouse right", "left button", "right button", "lmb", "rmb",
    "click"})


def _parse_part(part, spec):
    if part in FORBIDDEN_NAMES:
        raise ValueError(f"The left and right mouse buttons cannot be used "
                         f"in the hotkey ({part!r} in {spec!r}); use mouse3, "
                         f"mouse4 or mouse5")
    if part in KEY_NAMES:
        return KEY_NAMES[part]
    if len(part) == 1 and part.isascii() and part.isalnum():
        return (ord(part.upper()),)
    if not part:
        raise ValueError(f"Empty key in hotkey {spec!r}")
    raise ValueError(f"Unknown key {part!r} in hotkey {spec!r}")


def parse_hotkey(spec):
    """'ctrl+windows' -> [(0x11,), (0x5B, 0x5C)]; 'ctrl+mouse4' ->
    [(0x11,), (0x05,)].

    The hotkey is held when every group has at least one key down. Names are
    case- and whitespace-insensitive; single letters/digits are allowed, and
    the named keys (KEY_NAMES: navigation, arrows, keypad, punctuation,
    media keys...) and the middle/side mouse buttons (see MOUSE_NAMES).
    Raises ValueError for anything unknown, and for the left/right mouse
    buttons.
    """
    if not isinstance(spec, str):
        raise ValueError(f"Hotkey must be a string, not {spec!r}")
    groups = []
    for part in spec.lower().split("+"):
        part = " ".join(part.split())
        group = _parse_part(part, spec)
        if group in groups:
            raise ValueError(f"Key {part!r} repeated in hotkey {spec!r}")
        groups.append(group)
    return groups


def mouse_buttons(groups):
    """The mouse-button VKs in parsed hotkey groups (a frozenset)."""
    return frozenset(vk for g in groups for vk in g if vk in MOUSE_VKS)


def canonical_hotkey(spec):
    """Normalised spec: lower case, single spaces, mouse aliases replaced by
    mouse3/mouse4/mouse5 ('Mouse Back + Ctrl' -> 'mouse4+ctrl'). Raises
    ValueError like parse_hotkey."""
    parse_hotkey(spec)
    parts = []
    for part in spec.lower().split("+"):
        part = " ".join(part.split())
        vk = MOUSE_NAMES.get(part)
        parts.append(MOUSE_CANONICAL[vk] if vk else part)
    return "+".join(parts)


def describe_hotkey(spec):
    """Human text: 'mouse4' -> 'mouse4 (Back side button, XBUTTON1)'.
    Keyboard-only hotkeys come back unchanged; invalid ones as given."""
    try:
        groups = parse_hotkey(spec)
    except ValueError:
        return str(spec)
    notes = [MOUSE_DESCRIPTIONS[g[0]] for g in groups if g[0] in MOUSE_VKS]
    text = canonical_hotkey(spec)
    return f"{text} ({'; '.join(notes)})" if notes else text


_PLAIN_MOUSE = {VK_MBUTTON: "Middle mouse button",
                VK_XBUTTON1: "Mouse Back button",
                VK_XBUTTON2: "Mouse Forward button"}
_PLAIN_KEYS = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift",
               "alt": "Alt", "windows": "Windows", "win": "Windows",
               "apps": "Menu", "esc": "Esc", "page up": "Page Up",
               "page down": "Page Down", "caps lock": "Caps Lock",
               "scroll lock": "Scroll Lock"}
_PLAIN_VK = {0x26: "Up arrow", 0x28: "Down arrow", 0x25: "Left arrow",
             0x27: "Right arrow", 0x2C: "Print Screen", 0x2E: "Delete",
             0x6A: "Num *", 0x6B: "Num +", 0x6D: "Num -", 0x6E: "Num .",
             0x6F: "Num /", 0xB3: "Play/Pause"}
_US_CHARS = {0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".",
             0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]",
             0xDE: "'"}


def _layout_char(vk):
    """The character a punctuation key types on the current keyboard
    layout (e.g. 'ö' for VK_OEM_3 on a German layout), or None."""
    try:
        code = user32.MapVirtualKeyW(vk, 2) & 0x7FFFFFFF   # MAPVK_VK_TO_CHAR
    except Exception:
        return None
    char = chr(code & 0xFFFF) if code else ""
    return char if char.isprintable() and char.strip() else None


def plain_hotkey(spec):
    """Words for people: 'ctrl+windows' -> 'Ctrl + Windows', 'mouse4' ->
    'Mouse Back button', 'right alt' -> 'Right Alt', 'ctrl+semicolon' ->
    'Ctrl + ;'. Invalid specs come back as given."""
    try:
        canonical = canonical_hotkey(spec)
    except ValueError:
        return str(spec)
    words = []
    for part in canonical.split("+"):
        vk = MOUSE_NAMES.get(part)
        key = KEY_NAMES.get(part, (None,))
        if vk:
            words.append(_PLAIN_MOUSE[vk])
        elif part in _PLAIN_KEYS:
            words.append(_PLAIN_KEYS[part])
        elif len(part) <= 3 and part[:1] == "f" and part[1:].isdigit():
            words.append(part.upper())
        elif len(part) == 1 and part.isalnum():
            words.append(part.upper())
        elif len(key) == 1 and key[0] in _US_CHARS:
            words.append((_layout_char(key[0]) or _US_CHARS[key[0]]).upper())
        elif len(key) == 1 and key[0] in _PLAIN_VK:
            words.append(_PLAIN_VK[key[0]])
        elif len(key) == 1 and PLAIN_KEYS.get(key[0], "").startswith("num "):
            words.append("Num " + PLAIN_KEYS[key[0]][4:].capitalize())
        else:
            name = PLAIN_KEYS.get(key[0], part) if len(key) == 1 else part
            words.append(" ".join(_PLAIN_KEYS.get(w, w.capitalize())
                                  for w in name.split()))
    return " + ".join(words)


# -- turning held keys into a hotkey (Record keys...) ------------------------------

MODIFIER_NAMES = [("ctrl", {0x11, 0xA2, 0xA3}), ("shift", {0x10, 0xA0, 0xA1}),
                  ("alt", {0x12, 0xA4, 0xA5}), ("windows", {0x5B, 0x5C})]
# Every bindable keyboard key (not a modifier) -> its canonical name.
PLAIN_KEYS = {vk: name for name, vk, _ in _NAMED}
PLAIN_KEYS.update({0x6F + i: f"f{i}" for i in range(1, 25)})
PLAIN_KEYS.update({vk: chr(vk).lower() for vk in range(0x41, 0x5B)})
PLAIN_KEYS.update({vk: chr(vk) for vk in range(0x30, 0x3A)})
# Keyboard keys a capture looks at; the mouse buttons are MOUSE_VKS.
CAPTURE_VKS = sorted(set().union(*(s for _, s in MODIFIER_NAMES))
                     | set(PLAIN_KEYS))


def combo_from_vks(vks):
    """Virtual-key codes held together -> hotkey string ('ctrl+windows',
    'ctrl+mouse4', 'shift+page up').

    Left/right modifiers collapse to the generic name; modifiers come first,
    then keys, then mouse buttons. Left/right mouse buttons are ignored.
    Returns '' if none of the keys can be used in a hotkey.
    """
    vks = set(vks)
    parts = [name for name, family in MODIFIER_NAMES if vks & family]
    parts += [PLAIN_KEYS[vk] for vk in sorted(vks) if vk in PLAIN_KEYS]
    parts += [MOUSE_CANONICAL[vk] for vk in sorted(vks) if vk in MOUSE_VKS]
    return "+".join(parts)


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def no_modifier_held():
    return not any(key_down(vk) for vk in ALL_MODIFIERS)


# time.monotonic() of the last synthetic input this process sent (Ctrl+V or
# typed text); the app ignores those keys for its "another key was pressed"
# check (see App._poll).
last_injected = 0.0


def tap_mask_key():
    """Tap an unassigned key so releasing Win/Alt later opens nothing.

    Windows opens the Start menu (Win) or the menu bar (Alt) when that key is
    released with no other key pressed in between; this is the same trick
    AutoHotkey uses. Synthetic input: only call it from the real app loop.
    """
    user32.keybd_event(VK_MASK, 0, 0, 0)
    user32.keybd_event(VK_MASK, 0, KEYEVENTF_KEYUP, 0)


# -- synthetic input ----------------------------------------------------------------

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):       # only for the union's size
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                             ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def text_events(text):
    """The key events that type `text`: (vk, scan, flags) tuples.

    Characters are sent as Unicode input (VK_PACKET), which needs no
    keyboard layout and no modifier; surrogate pairs go as two units. A line
    break is Shift+Enter: a new line in chat boxes and editors, where a plain
    Enter would send the message. "\\r\\n" counts as one line break."""
    events = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = text.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = int.from_bytes(raw[i:i + 2], "little")
        if unit == 0x0A:
            events += [(VK_SHIFT, 0, 0), (VK_RETURN, 0, 0),
                       (VK_RETURN, 0, KEYEVENTF_KEYUP),
                       (VK_SHIFT, 0, KEYEVENTF_KEYUP)]
        else:
            events += [(0, unit, KEYEVENTF_UNICODE),
                       (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return events


class Win32Input:
    """The real synthetic keyboard (SendInput). Only the app loop and the
    worker use it, and never in dry-run mode."""

    CHUNK = 200              # events per SendInput call while typing

    def _send(self, events):
        arr = (_INPUT * len(events))()
        for i, (vk, scan, flags) in enumerate(events):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
        if sent != len(events):
            raise OSError(f"SendInput sent {sent} of {len(events)} key "
                          f"events (error {ctypes.get_last_error()})")

    def ctrl_v(self):
        self._send([(VK_CONTROL, 0, 0), (VK_V, 0, 0),
                    (VK_V, 0, KEYEVENTF_KEYUP),
                    (VK_CONTROL, 0, KEYEVENTF_KEYUP)])

    def type_text(self, text):
        events = text_events(text)
        for i in range(0, len(events), self.CHUNK):
            self._send(events[i:i + self.CHUNK])
            if i + self.CHUNK < len(events):
                time.sleep(0.005)       # let the target app keep up


# -- clipboard ---------------------------------------------------------------

CF_TEXT, CF_BITMAP, CF_METAFILEPICT, CF_OEMTEXT = 1, 2, 3, 7
CF_DIB, CF_PALETTE, CF_UNICODETEXT, CF_ENHMETAFILE = 8, 9, 13, 14
CF_HDROP, CF_LOCALE, CF_DIBV5 = 15, 16, 17
# Formats whose data is a plain memory block (copied byte for byte); every
# registered format (0xC000 and up) is one too. The rest are GDI or other
# handles: CF_BITMAP, CF_PALETTE and CF_METAFILEPICT are re-created by
# Windows from CF_DIB / CF_ENHMETAFILE, CF_ENHMETAFILE is copied through its
# bits, and anything else (owner-display, private and GDI-object formats)
# cannot be copied.
MEMORY_FORMATS = frozenset({1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16, 17,
                            0x81})
SYNTHESIZED_FROM = {CF_BITMAP: (CF_DIB, CF_DIBV5),
                    CF_PALETTE: (CF_DIB, CF_DIBV5),
                    CF_METAFILEPICT: (CF_ENHMETAFILE,)}
# OLE's own plumbing: they point at the copying program's live data object,
# not at data, and are rebuilt by OLE from the real formats.
OLE_PLUMBING = frozenset({"DataObject", "Ole Private Data"})
# Markers that keep an entry out of Windows clipboard history / cloud sync.
PRIVATE_MARKERS = (
    ("ExcludeClipboardContentFromMonitorProcessing", b"\0"),
    ("CanIncludeInClipboardHistory", b"\0\0\0\0"),
    ("CanUploadToCloudClipboard", b"\0\0\0\0"),
)
MAX_RESTORE_BYTES = 64 * 1024 * 1024


class Win32Clipboard:
    """The real clipboard. Each call opens it (owned by a throwaway
    message-only window: with a NULL owner SetClipboardData is documented to
    fail) and closes it again.

    Backend interface used by paste_text (tests provide a fake):
      sequence() -> int
      read_all(max_bytes) -> [(fmt, name, bytes or None)], every format on
          the clipboard in order; None = this format cannot be copied.
          Raises OSError if the clipboard cannot be opened.
      write(items) -> sequence number; items = [(fmt, name, bytes)]; fmt
          None means "register `name`". Replaces the whole clipboard.
    """

    @contextlib.contextmanager
    def _open(self):
        hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                      HWND_MESSAGE, None, None, None)
        try:
            for _ in range(50):           # another app may hold it briefly
                if user32.OpenClipboard(hwnd):
                    break
                time.sleep(0.01)
            else:
                raise OSError("Could not open the clipboard (error "
                              f"{ctypes.get_last_error()})")
            try:
                yield
            finally:
                user32.CloseClipboard()
        finally:
            if hwnd:
                user32.DestroyWindow(hwnd)

    def sequence(self):
        return user32.GetClipboardSequenceNumber()

    @staticmethod
    def _name(fmt):
        if fmt < 0xC000:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        n = user32.GetClipboardFormatNameW(fmt, buf, 256)
        return buf.value[:n] if n else ""

    @staticmethod
    def _read(fmt, budget):
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return None
        if fmt == CF_ENHMETAFILE:
            size = gdi32.GetEnhMetaFileBits(handle, 0, None)
            if not size or size > budget:
                return None
            buf = ctypes.create_string_buffer(size)
            if gdi32.GetEnhMetaFileBits(handle, size, buf) != size:
                return None
            return buf.raw
        if fmt not in MEMORY_FORMATS and fmt < 0xC000:
            return None
        size = kernel32.GlobalSize(handle)
        if size > budget:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None if size else b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)

    def read_all(self, max_bytes=MAX_RESTORE_BYTES):
        out = []
        with self._open():
            fmt, budget = 0, max_bytes
            while True:
                fmt = user32.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                data = self._read(fmt, budget)
                if data is not None:
                    budget -= len(data)
                out.append((fmt, self._name(fmt), data))
        return out

    @staticmethod
    def _global(data):
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
        if not handle:
            raise MemoryError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(handle)
        if data:
            ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return handle

    def write(self, items):
        with self._open():
            user32.EmptyClipboard()
            for fmt, name, data in items:
                if fmt is None:
                    fmt = user32.RegisterClipboardFormatW(name)
                    if not fmt:
                        continue
                if fmt == CF_ENHMETAFILE:
                    handle = gdi32.SetEnhMetaFileBits(len(data), data)
                    if not handle:
                        raise OSError("SetEnhMetaFileBits failed")
                    if not user32.SetClipboardData(fmt, handle):
                        gdi32.DeleteEnhMetaFile(handle)
                        raise OSError("SetClipboardData failed")
                    continue
                handle = self._global(data)
                if not user32.SetClipboardData(fmt, handle):
                    kernel32.GlobalFree(handle)
                    raise OSError(f"SetClipboardData failed for format {fmt}")
        return user32.GetClipboardSequenceNumber()


class Snapshot:
    """What the clipboard held: kind 'empty', 'data' (items, restorable in
    every format) or 'unrestorable' (reason says why, never the content)."""

    def __init__(self, kind, items=(), reason=""):
        self.kind, self.items, self.reason = kind, list(items), reason

    def __repr__(self):
        return (f"Snapshot({self.kind!r}, {len(self.items)} formats, "
                f"{self.reason!r})")


def take_snapshot(clipboard, max_bytes=MAX_RESTORE_BYTES):
    """Read every format on the clipboard and decide whether all of it can
    be put back exactly. Never raises; a clipboard that cannot be read is
    'unrestorable'."""
    try:
        found = clipboard.read_all(max_bytes)
    except Exception as exc:
        return Snapshot("unrestorable", reason=f"it could not be read "
                        f"({type(exc).__name__})")
    if not found:
        return Snapshot("empty")
    present = {fmt for fmt, _, _ in found}
    keep, total = [], 0
    for fmt, name, data in found:
        if name in OLE_PLUMBING:
            continue
        if fmt in SYNTHESIZED_FROM and present & set(SYNTHESIZED_FROM[fmt]):
            continue                      # Windows re-creates it
        if data is None:
            return Snapshot("unrestorable", reason=f"it holds data in a "
                            f"format that cannot be copied (format {fmt})")
        total += len(data)
        if total > max_bytes:
            return Snapshot("unrestorable", reason="it holds more than "
                            f"{max_bytes // (1024 * 1024)} MB")
        keep.append((fmt, name, data))
    if not keep:
        return Snapshot("unrestorable", reason="nothing on it can be copied")
    return Snapshot("data", keep)


def _private(items):
    """items plus the markers that keep them out of clipboard history and
    cloud clipboard (unless items already carry their own)."""
    names = {name for _, name, _ in items}
    return list(items) + [(None, name, data) for name, data in PRIVATE_MARKERS
                          if name not in names]


def text_items(text):
    return [(CF_UNICODETEXT, "", (text + "\0").encode("utf-16-le"))]


class PasteBlocked(RuntimeError):
    """Nothing was sent: a key stayed held (see paste_text's ready)."""


def _wait(pred, timeout, sleep, clock):
    end = clock() + timeout
    while not pred():
        if clock() >= end:
            return False
        sleep(0.01)
    return True


@contextlib.contextmanager
def _check(ready):
    yield ready()


class _Restorer:
    """The user's clipboard, waiting to be put back after a paste.

    paste_text() puts our text on the clipboard, sends Ctrl+V and then hands
    the user's original snapshot to arm(); the restore itself runs a moment
    later on a timer, so the worker thread is free as soon as Ctrl+V is out.
    A paste that starts while a restore is pending takes the original over
    (take) instead of snapshotting our own temporary text, and the restore
    happens once, after the last of a run of quick pastes. If anything else
    copied to the clipboard meanwhile, the restore does nothing.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._clipboard = self._snap = self._seq = self._timer = None

    def _clear(self):
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        snap, self._snap = self._snap, None
        clipboard, self._clipboard = self._clipboard, None
        seq, self._seq = self._seq, None
        return clipboard, snap, seq

    def take(self, clipboard):
        """A paste is starting: the original snapshot that is still waiting
        to be restored, if nobody has copied anything since our text went on
        the clipboard; else None (the caller snapshots the clipboard). Nothing
        is pending afterwards."""
        with self._lock:
            owner, snap, seq = self._clear()
            if snap is None:
                return None
            if owner is clipboard and clipboard.sequence() == seq:
                return snap
            log.info("The clipboard changed since the last paste; what it "
                     "held before is not put back")
            return None

    def arm(self, clipboard, snap, seq, delay, schedule):
        """Put `snap` back after `delay` seconds: schedule(delay, fn) runs fn
        later (and returns something with cancel()); None = right now."""
        with self._lock:
            self._clear()
            self._clipboard, self._snap, self._seq = clipboard, snap, seq
            if schedule is not None:
                self._timer = schedule(delay, self.restore_now)
                return
        self.restore_now()

    def restore_now(self):
        """Put the pending original back now (unless the clipboard changed
        since). True if it was written. Safe to call with nothing pending."""
        with self._lock:
            clipboard, snap, seq = self._clear()
            if snap is None:
                return False
            if clipboard.sequence() != seq:
                log.info("The clipboard changed during the paste; it was "
                         "left as it is now")
                return False
            try:
                clipboard.write([] if snap.kind == "empty"
                                else _private(snap.items))
            except Exception as exc:
                log.warning("Could not put the clipboard back (%s)",
                            type(exc).__name__)
                return False
            return True


_restorer = _Restorer()


def restore_pending_clipboard():
    """Put the user's clipboard back now if a paste's restore is still
    waiting (app shutdown). True if something was written."""
    return _restorer.restore_now()


atexit.register(restore_pending_clipboard)


def _timer_schedule(delay, fn):
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.name = "clipboard-restore"
    timer.start()
    return timer


def paste_text(text, restore_clipboard=True, *, clipboard=None, sender=None,
               ready=None, guard=None, wait=10.0, settle=0.05, linger=0.5,
               sleep=time.sleep, clock=time.monotonic, defer=True,
               schedule=None):
    """Insert `text` into the focused window. Returns 'pasted' or 'typed'.

    Waits until ready() (default: no modifier key held), at most `wait`
    seconds, so the Ctrl+V can never turn into another shortcut such as
    Win+Ctrl+V; if it stays false nothing is sent and PasteBlocked is
    raised (the clipboard is then as it was). The keys are sent inside
    `with guard() as ok:` (default: ok = ready()), which lets the app make
    sure no recording can start while the synthetic keys go out.

    restore_clipboard=True: everything the clipboard held, in all its
    formats, is put back after the paste (unless something else changed the
    clipboard meanwhile, which is then left alone). The temporary text is
    marked so clipboard history and cloud clipboard skip it, and so is the
    restored copy (it is already in the history if it was ever going to
    be). If the clipboard holds something that cannot be put back exactly,
    the text is typed (Unicode key events) and the clipboard is not touched.
    The target app reads the clipboard asynchronously, so the restore waits
    `linger` seconds after Ctrl+V: with defer=True (the default) that wait
    happens on a timer (schedule(delay, fn), default a daemon threading.Timer)
    and this function returns right after Ctrl+V; a paste that follows
    quickly carries the original forward, and restore_pending_clipboard()
    does it at once. defer=False waits here (`sleep`) and restores before
    returning.
    restore_clipboard=False: the text is left on the clipboard, with the same
    markers, so clipboard history and cloud clipboard skip it (programs that
    ignore the markers can still read it).
    """
    global last_injected
    clipboard = clipboard or Win32Clipboard()
    sender = sender or Win32Input()
    ready = ready or no_modifier_held
    guard = guard or (lambda: _check(ready))
    if defer and schedule is None:
        schedule = _timer_schedule
    schedule = schedule if defer else None

    def inject(action):
        global last_injected
        end = clock() + wait
        while True:
            if _wait(ready, max(0.0, end - clock()), sleep, clock):
                with guard() as ok:
                    if ok:
                        last_injected = clock()
                        action()
                        return True
            if clock() >= end:
                return False
            sleep(0.01)

    if not _wait(ready, wait, sleep, clock):
        raise PasteBlocked("a key stayed held")

    if not restore_clipboard:
        _restorer.restore_now()          # an earlier paste's restore first
        clipboard.write(_private(text_items(text)))
        sleep(settle)
        if not inject(sender.ctrl_v):
            raise PasteBlocked("a key stayed held")
        return "pasted"

    snap = _restorer.take(clipboard)     # the user's, carried forward
    if snap is None:
        snap = take_snapshot(clipboard)
    if snap.kind == "unrestorable":
        log.info("The clipboard cannot be put back exactly (%s): typing the "
                 "text instead of pasting it", snap.reason)
        if not inject(lambda: sender.type_text(text)):
            raise PasteBlocked("a key stayed held")
        return "typed"

    try:
        seq = clipboard.write(_private(text_items(text)))
    except Exception as exc:
        log.warning("Could not put the text on the clipboard (%s); typing "
                    "it instead", type(exc).__name__)
        try:
            clipboard.write([] if snap.kind == "empty"
                            else _private(snap.items))
        except Exception:
            log.warning("Could not put the clipboard back either")
        if not inject(lambda: sender.type_text(text)):
            raise PasteBlocked("a key stayed held")
        return "typed"
    sent = False
    try:
        sleep(settle)
        sent = inject(sender.ctrl_v)
    finally:
        # The target app reads the clipboard asynchronously; slow apps
        # (Electron, remote desktops) need a moment before we swap it back.
        if sent and not defer:
            sleep(linger)
        _restorer.arm(clipboard, snap, seq, linger if sent else 0.0,
                      schedule if sent else None)
    if not sent:
        raise PasteBlocked("a key stayed held")
    return "pasted"