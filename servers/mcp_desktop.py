"""MCP server: real desktop control -- windows, keyboard, mouse, screen regions.

Pure ctypes/user32+user32-level SendInput (no third-party input libs). Pillow is
used only for the screen-region capture. Every tool here drives the *real*
mouse/keyboard, so the descriptions tell the model to inspect state first
(list_windows / cursor_pos) before typing, clicking or closing windows.
"""
import ctypes
import datetime
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mcpserver import Server  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

srv = Server("desktop")

# ---------------------------------------------------------------- user32 setup
LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ULONG_PTR]
user32.keybd_event.restype = None


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

INPUT_KEYBOARD = 1
INPUT_MOUSE = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WM_CLOSE = 0x0010
SW_RESTORE = 9
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# DPI awareness: keep ctypes pixel coordinates identical to what the user sees.
for _ctx in (2, 1):  # PER_MONITOR_AWARE_V2, SYSTEM_AWARE (fallback for old builds)
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_ctx)):
            break
    except Exception:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2 if _ctx == 2 else 1)
            break
        except Exception:
            pass

VK_NAMES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "win": 0x5B, "lwin": 0x5B,
    "rwin": 0x5C, "apps": 0x5D, "printscreen": 0x2C, "capslock": 0x14,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "shift": 0x10,
    "+": 0xBB, "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}
for _i in range(1, 13):
    VK_NAMES["f%d" % _i] = 0x6F + _i  # F1=0x70 .. F12=0x7B
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_NAMES[_c] = ord(_c.upper())
for _d in "0123456789":
    VK_NAMES[_d] = ord(_d)
VK_NAMES["ctrl"] = 0x11

_NAME_BY_VK = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift", 0x11: "ctrl", 0x12: "alt",
    0x1B: "esc", 0x20: "space", 0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x2D: "insert", 0x2E: "delete",
    0x5B: "win",
}

MOUSE_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _proc_name(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return "?"
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return "pid=%d" % pid.value
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value) or "pid=%d" % pid.value
        return "pid=%d" % pid.value
    finally:
        kernel32.CloseHandle(h)


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _visible_titled():
    """[(hwnd, title)] for top-level windows that are visible with a real title."""
    found = []

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            t = _title(hwnd)
            if t.strip():
                found.append((int(hwnd), t))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _find(match):
    needle = str(match or "").lower()
    if not needle:
        return None, ""
    for hwnd, title in _visible_titled():
        if needle in title.lower():
            return hwnd, title
    return None, ""


def _send_inputs(inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent


def _key_input(wVk, wScan, flags):
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.u.ki = KEYBDINPUT(wVk, wScan, flags, 0, 0)
    return item


def _mouse_input(dx, dy, flags, data=0):
    item = INPUT()
    item.type = INPUT_MOUSE
    item.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
    return item


# ---------------------------------------------------------------------- tools
@srv.tool("list_windows",
          "List this machine's visible top-level windows as 'hwnd processName title'. Read-only. "
          "Call this before focus_window/close_window/type_text/press_keys so you know the real target window.",
          {"type": "object", "properties": {"filter": {"type": "string", "default": ""},
                                            "limit": {"type": "integer", "default": 40}}, "required": []})
def list_windows(filter="", limit=40):
    needle = str(filter or "").lower()
    rows = []
    for hwnd, title in _visible_titled():
        if needle and needle not in title.lower():
            continue
        rows.append((hwnd, _proc_name(hwnd), title))
        if len(rows) >= int(limit):
            break
    if not rows:
        return "no matching windows"
    return "\n".join("%d  %s  %s" % (h, p, t) for h, p, t in rows)


@srv.tool("focus_window",
          "Bring the first visible window whose title contains 'match' (case-insensitive) to the foreground, "
          "restoring it if minimized. Returns the matched title. Changes real focus - inspect with list_windows first.",
          {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]})
def focus_window(match):
    hwnd, title = _find(match)
    if hwnd is None:
        return "no window matching: %s" % match
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    fg = int(user32.GetForegroundWindow() or 0)
    if fg == hwnd:
        return "focused: %s (hwnd=%d)" % (title, hwnd)
    return "restore+foreground requested for: %s (hwnd=%d); foreground is now hwnd=%d (Windows may refuse focus steals from a background process)" % (title, hwnd, fg)


@srv.tool("close_window",
          "Post WM_CLOSE to the first visible window whose title contains 'match'. "
          "This asks the application to close (it may prompt to save). Disruptive - confirm the target with list_windows first.",
          {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]})
def close_window(match):
    hwnd, title = _find(match)
    if hwnd is None:
        return "no window matching: %s" % match
    ok = user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return "WM_CLOSE posted to: %s (hwnd=%d, ok=%s)" % (title, hwnd, bool(ok))


@srv.tool("window_rect",
          "Get the screen rectangle (left, top, right, bottom, width, height) of the first window matching 'match'.",
          {"type": "object", "properties": {"match": {"type": "string"}}, "required": ["match"]})
def window_rect(match):
    hwnd, title = _find(match)
    if hwnd is None:
        return "no window matching: %s" % match
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return "GetWindowRect failed for: %s" % title
    return "title=%s hwnd=%d left=%d top=%d right=%d bottom=%d width=%d height=%d" % (
        title, hwnd, r.left, r.top, r.right, r.bottom, r.right - r.left, r.bottom - r.top)


@srv.tool("type_text",
          "Type arbitrary Unicode text (Chinese works) into whatever window currently has keyboard focus, "
          "via SendInput KEYEVENTF_UNICODE. Long input is chunked. REAL keystrokes go to the live desktop: "
          "call list_windows + focus_window first and make sure the intended field is focused.",
          {"type": "object", "properties": {"text": {"type": "string"},
                                            "interval_s": {"type": "number", "default": 0.02}}, "required": ["text"]})
def type_text(text, interval_s=0.02):
    import time
    s = str(text)
    if not s:
        return "nothing to type"
    chunk_size = 64
    sent = 0
    for i in range(0, len(s), chunk_size):
        chunk = s[i:i + chunk_size]
        items = []
        for ch in chunk:
            code = ord(ch)
            items.append(_key_input(0, code, KEYEVENTF_UNICODE))
            items.append(_key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        sent += _send_inputs(items)
        if interval_s and i + chunk_size < len(s):
            time.sleep(float(interval_s))
    return "typed %d chars (%d/%d input events) into hwnd=%d" % (
        len(s), sent, 2 * len(s), int(user32.GetForegroundWindow() or 0))


@srv.tool("press_keys",
          "Press a key combination on the live desktop, e.g. 'ctrl+shift+s', 'win+r', 'enter', 'alt+f4', "
          "'ctrl+alt+delete-ish'. Tokens are joined with '+'; keydowns go in order and keyups in reverse. "
          "REAL input: focus the right window first and be aware shortcuts can trigger destructive actions.",
          {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]})
def press_keys(keys):
    tokens = str(keys).split("+")
    vks = []
    for t in tokens:
        key = t.strip().lower()
        if not key:
            continue
        vk = VK_NAMES.get(key)
        if vk is None and len(key) == 1:
            vk = ord(key.upper())
        if vk is None:
            return ("unknown key name: %s (use letters, digits, f1-f12, enter, esc, tab, space, "
                    "backspace, delete, up/down/left/right, home/end, pageup/pagedown, ctrl/alt/shift/win)" % t)
        vks.append((key, vk))
    if not vks:
        return "no keys given"
    items = [_key_input(vk, 0, 0) for _n, vk in vks]
    items += [_key_input(vk, 0, KEYEVENTF_KEYUP) for _n, vk in reversed(vks)]
    sent = _send_inputs(items)
    return "pressed %s (%d/%d input events)" % ("+".join(n for n, _v in vks), sent, len(items))


@srv.tool("mouse_move",
          "Move the real mouse cursor to absolute screen coordinates (x, y). "
          "Coordinates are in physical pixels normalized over SM_CXSCREEN/SM_CYSCREEN.",
          {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
           "required": ["x", "y"]})
def mouse_move(x, y):
    return _do_move(int(x), int(y))


@srv.tool("mouse_click",
          "Click the real mouse at absolute (x, y); x=0,y=0 clicks wherever the cursor already is. "
          "button is left|right|middle, clicks is the repeat count. Disruptive on a live desktop - "
          "check cursor_pos and the target window first.",
          {"type": "object", "properties": {"x": {"type": "integer", "default": 0}, "y": {"type": "integer", "default": 0},
                                            "button": {"type": "string", "default": "left"},
                                            "clicks": {"type": "integer", "default": 1}}, "required": []})
def mouse_click(x=0, y=0, button="left", clicks=1):
    btn = str(button).lower()
    if btn not in MOUSE_FLAGS:
        return "unknown button: %s (left|right|middle)" % button
    x, y = int(x), int(y)
    moved = ""
    if x != 0 or y != 0:
        moved = _do_move(x, y) + " | "
    down, up = MOUSE_FLAGS[btn]
    items = []
    for _i in range(max(1, int(clicks))):
        items.append(_mouse_input(0, 0, down))
        items.append(_mouse_input(0, 0, up))
    sent = _send_inputs(items)
    return "%sclicked %s x%d at (%d,%d) [%d/%d events]" % (moved, btn, max(1, int(clicks)), x, y, sent, len(items))


def _do_move(x, y):
    cx = user32.GetSystemMetrics(SM_CXSCREEN) or 1
    cy = user32.GetSystemMetrics(SM_CYSCREEN) or 1
    nx = int(round(x * 65535.0 / (cx - 1))) if cx > 1 else 0
    ny = int(round(y * 65535.0 / (cy - 1))) if cy > 1 else 0
    sent = _send_inputs([_mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE)])
    return "cursor moved to (%d,%d) [%d/1 event]" % (x, y, sent)


@srv.tool("cursor_pos", "Current mouse cursor position in screen pixels. Read-only.",
          {"type": "object", "properties": {}, "required": []})
def cursor_pos():
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return "GetCursorPos failed"
    return "cursor x=%d y=%d" % (pt.x, pt.y)


@srv.tool("screen_size", "Primary screen size plus the whole virtual desktop (multi-monitor) size. Read-only.",
          {"type": "object", "properties": {}, "required": []})
def screen_size():
    return ("primary=%dx%d virtual_origin=(%d,%d) virtual_size=%dx%d" % (
        user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN),
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))


@srv.tool("read_screen_region",
          "Capture the screen rectangle (left, top, right, bottom) to a PNG and return the file path and pixel size. "
          "Read-only; when 'out' is empty the file goes to %TEMP%. Take a small region, not the whole screen, unless asked.",
          {"type": "object", "properties": {"left": {"type": "integer"}, "top": {"type": "integer"},
                                            "right": {"type": "integer"}, "bottom": {"type": "integer"},
                                            "out": {"type": "string", "default": ""}}, "required": ["left", "top", "right", "bottom"]})
def read_screen_region(left, top, right, bottom, out=""):
    try:
        from PIL import ImageGrab
    except Exception as exc:
        return "Pillow ImageGrab unavailable: %s" % exc
    box = (int(left), int(top), int(right), int(bottom))
    if box[2] <= box[0] or box[3] <= box[1]:
        return "empty region: %s" % (box,)
    dest = out or os.path.join(os.environ.get("TEMP", "."),
                               "region_%s.png" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    dest = os.path.abspath(os.path.expanduser(str(dest)))
    try:
        img = ImageGrab.grab(bbox=box, all_screens=True)
    except TypeError:
        img = ImageGrab.grab(bbox=box)
    img.save(dest, "PNG")
    return "saved: %s pixels=%dx%d box=%s bytes=%d" % (
        dest, img.size[0], img.size[1], box, os.path.getsize(dest))


def build():
    return srv


# Safe self-test samples: read-only, or writing into %TEMP%. Mouse/keyboard/closing
# tools are intentionally absent -- they would hit whatever is on the live desktop.
SAMPLES = {
    "list_windows": {"filter": "", "limit": 10},
    "cursor_pos": {},
    "screen_size": {},
    "window_rect": {"match": "Program Manager"},
    "focus_window": {"match": "Program Manager"},
    "read_screen_region": {"left": 0, "top": 0, "right": 160, "bottom": 120},
}


if __name__ == "__main__":
    srv.run()
