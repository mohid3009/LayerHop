"""Windows side: a low-level mouse hook feeds Remapper; a worker thread replays its actions with SendInput."""
import ctypes
import os
import queue
import re
import threading
import time
from ctypes import wintypes as w

from remap import Remapper

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, w.WPARAM, w.LPARAM)

WH_MOUSE_LL, LLMHF_INJECTED = 14, 0x1
WM_HOTKEY, WM_TIMER = 0x312, 0x113
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, VK_F12 = 0x1, 0x2, 0x4000, 0x7B
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
MOUSEEVENTF_WHEEL = 0x800


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD)
user32.SetWindowsHookExW.restype = w.HHOOK
user32.CallNextHookEx.argtypes = (w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (w.HHOOK,)
user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.GetMessageW.argtypes = (ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
user32.SetTimer.argtypes = (w.HWND, ULONG_PTR, w.UINT, ctypes.c_void_p)
user32.SetTimer.restype = ULONG_PTR
user32.VkKeyScanW.argtypes = (w.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = (w.LPCWSTR,)
kernel32.GetModuleHandleW.restype = w.HMODULE

VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "menu": 0x5D,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "capslock": 0x14,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "next_track": 0xB0, "prev_track": 0xB1, "play_pause": 0xB3,
    "browser_back": 0xA6, "browser_forward": 0xA7,
}
EXTENDED = {0x5B, 0x5D, 0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x2C,
            0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3, 0xA6, 0xA7}
MOUSE_FLAGS = {"left": (0x2, 0x4, 0), "right": (0x8, 0x10, 0), "middle": (0x20, 0x40, 0),
               "x1": (0x80, 0x100, 1), "x2": (0x80, 0x100, 2)}
BUTTON_MSGS = {0x201: ("left", True), 0x202: ("left", False), 0x204: ("right", True),
               0x205: ("right", False), 0x207: ("middle", True), 0x208: ("middle", False)}


def parse_keys(combo):
    """'ctrl+shift+t' -> [vk, ...]. Raises ValueError naming the bad key."""
    if not combo.strip():
        raise ValueError("no keys entered")
    vks = []
    for name in (p.strip().lower() for p in combo.split("+")):
        if name in VK:
            vks.append(VK[name])
        elif re.fullmatch(r"f([1-9]|1\d|2[0-4])", name):
            vks.append(0x6F + int(name[1:]))
        elif len(name) == 1 and user32.VkKeyScanW(name) != -1:
            vks.append(user32.VkKeyScanW(name) & 0xFF)
        else:
            raise ValueError(f"unknown key '{name}'")
    return vks


def _key(vk, up=False, unicode_unit=0):
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki.wVk, i.ki.wScan = vk, unicode_unit
    i.ki.dwFlags = ((KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
                    | (KEYEVENTF_UNICODE if unicode_unit else 0))
    return i


def _mouse(flags, data=0):
    i = INPUT(type=INPUT_MOUSE)
    i.mi.dwFlags, i.mi.mouseData = flags, data & 0xFFFFFFFF
    return i


def _send(inputs):
    if inputs:
        user32.SendInput(len(inputs), (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT))


def _do(kind, a):
    """Perform one action: kind is "down", "up" or "click" (down then up)."""
    t, v = a.get("type"), a.get("value", "")
    press, release = kind in ("down", "click"), kind in ("up", "click")
    inputs = []
    if t == "button" and v in ("wheel_up", "wheel_down"):
        if press:
            inputs.append(_mouse(MOUSEEVENTF_WHEEL, 120 if v == "wheel_up" else -120))
    elif t == "button":
        down, up, data = MOUSE_FLAGS[v]
        inputs += [_mouse(down, data)] * press + [_mouse(up, data)] * release
    elif t in ("keys", "media"):
        vks = parse_keys(v)
        inputs += [_key(k) for k in vks] * press + [_key(k, True) for k in reversed(vks)] * release
    elif t == "text" and press:
        b = v.encode("utf-16-le")  # code units, so emoji (surrogate pairs) type correctly
        for n in range(0, len(b), 2):
            unit = int.from_bytes(b[n:n + 2], "little")
            inputs += [_key(0, False, unit), _key(0, True, unit)]
    elif t == "run" and press:
        os.startfile(v)
    _send(inputs)


def _decode(msg, data):
    hi = data >> 16
    if msg in BUTTON_MSGS:
        return BUTTON_MSGS[msg]
    if msg in (0x20B, 0x20C) and hi in (1, 2):
        return ("x1" if hi == 1 else "x2", msg == 0x20B)
    if msg == 0x20A:
        return ("wheel_up" if ctypes.c_short(hi).value > 0 else "wheel_down", True)
    return None


class Engine:
    PAUSE_HOTKEY = "Ctrl+Alt+F12"

    def __init__(self, config, notify):
        self.rm = Remapper(config)
        self.notify = notify  # notify(kind, value); called from background threads
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self._error = None

    def start(self):
        ready = threading.Event()
        threading.Thread(target=self._hook_loop, args=(ready,), daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        ready.wait()
        if self._error:
            raise self._error

    def load(self, config):
        with self.lock:
            self.q.put(self.rm.load(config))

    def run(self, action):
        """Do an action once, e.g. a bar item; runs on the worker thread like any remapped click."""
        self.q.put([("click", action)])

    def set_enabled(self, on):
        with self.lock:
            self.q.put(self.rm.set_enabled(on))
        self.notify("enabled", on)

    def _proc(self, code, wparam, lparam):
        if code == 0:
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            ev = _decode(wparam, info.mouseData)
            if ev and not info.flags & LLMHF_INJECTED:  # skip our own SendInput output
                with self.lock:
                    suppress, out = self.rm.on_event(*ev, time.monotonic())
                if ev[1]:
                    self.notify("press", ev[0])
                if suppress or out:  # also wakes the worker so it sees a new deadline
                    self.q.put(out)
                if suppress:
                    return 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _hook_loop(self, ready):
        proc = HOOKPROC(self._proc)  # must stay referenced while hooked
        hmod = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, hmod, 0)
        if not hook:
            self._error = ctypes.WinError(ctypes.get_last_error())
            ready.set()
            return
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F12):
            self.notify("error", f"{self.PAUSE_HOTKEY} is already used by another app, so the pause hotkey won't work.")
        user32.SetTimer(None, 0, 5000, None)
        ready.set()
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.set_enabled(not self.rm.enabled)
            elif msg.message == WM_TIMER:
                # ponytail: blind re-hook every 5s, because Windows silently drops hooks that answer too slowly.
                # Safe: hook callbacks only run while this thread pumps messages, so events can't hit both.
                new = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, hmod, 0)
                if new:
                    user32.UnhookWindowsHookEx(hook)
                    hook = new

    def _worker(self):
        while True:
            with self.lock:  # deadline scans a dict the hook thread may be changing
                dl = self.rm.deadline
            try:
                out = self.q.get(timeout=None if dl is None else max(0, dl - time.monotonic()))
            except queue.Empty:
                out = []
            with self.lock:
                out = out + self.rm.tick(time.monotonic())
            for kind, arg in out:
                if kind == "layer":
                    self.notify("layer", arg)
                    continue
                if arg.get("type") == "launcher":  # bars live on the GUI thread
                    self.notify("launcher", (kind, arg.get("value", "")))
                    continue
                try:
                    _do(kind, arg)
                except Exception as e:  # keep the worker alive; tell the user what failed
                    self.notify("error", f"Couldn't do {arg.get('type')} '{arg.get('value')}': {e}")
