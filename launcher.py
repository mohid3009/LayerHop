"""Hold-to-open pop-up bar: a macOS-style pill of buttons, drawn with PIL onto a per-pixel-alpha layered window.
Hold the button, slide sideways to pick, let go to run it. Letting go off the bar (or without moving) cancels."""
import ctypes
import functools
import math
import os
import time
import uuid
from ctypes import wintypes as w

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ctypes.WinDLL("ole32").CoInitialize(None)  # shell image lists need COM on the GUI thread

WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x8, 0x80, 0x8000000
WS_POPUP, SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA = 0x80000000, 0, 4, 2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
SHGFI_DISPLAYNAME, SHGFI_SYSICONINDEX, SHIL_EXTRALARGE, SHIL_JUMBO = 0x200, 0x4000, 2, 4
EMPTY_TEXT = "This bar is empty. Add buttons to it under Bars in Layerhop."


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", w.BOOL), ("xHotspot", w.DWORD), ("yHotspot", w.DWORD),
                ("hbmMask", w.HBITMAP), ("hbmColor", w.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", w.LONG), ("bmWidth", w.LONG), ("bmHeight", w.LONG), ("bmWidthBytes", w.LONG),
                ("bmPlanes", w.WORD), ("bmBitsPixel", w.WORD), ("bmBits", w.LPVOID)]


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [("hIcon", w.HICON), ("iIcon", ctypes.c_int), ("dwAttributes", w.DWORD),
                ("szDisplayName", w.WCHAR * 260), ("szTypeName", w.WCHAR * 80)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", w.BYTE), ("BlendFlags", w.BYTE), ("SourceConstantAlpha", w.BYTE), ("AlphaFormat", w.BYTE)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", w.BYTE * 8)]


IID_IImageList = GUID.from_buffer_copy(uuid.UUID("46eb5926-582e-4017-9fdf-e8998daa0950").bytes_le)
GETICON = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_int, w.UINT, ctypes.POINTER(w.HICON))
RELEASE = ctypes.WINFUNCTYPE(w.ULONG, ctypes.c_void_p)

user32.CreateWindowExW.argtypes = (w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
user32.CreateWindowExW.restype = w.HWND
user32.UpdateLayeredWindow.argtypes = (w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE), w.HDC,
                                       ctypes.POINTER(w.POINT), w.COLORREF, ctypes.POINTER(BLENDFUNCTION), w.DWORD)
user32.SetWindowPos.argtypes = (w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)
user32.ShowWindow.argtypes = (w.HWND, ctypes.c_int)
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = (w.HWND, w.HDC)
user32.GetIconInfo.argtypes = (w.HICON, ctypes.POINTER(ICONINFO))
user32.DestroyIcon.argtypes = (w.HICON,)
user32.MonitorFromPoint.argtypes = (w.POINT, w.DWORD)
user32.MonitorFromPoint.restype = w.HMONITOR
user32.GetMonitorInfoW.argtypes = (w.HMONITOR, ctypes.POINTER(MONITORINFO))
gdi32.CreateCompatibleDC.argtypes = (w.HDC,)
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateDIBSection.argtypes = (w.HDC, ctypes.POINTER(BITMAPINFO), w.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   w.HANDLE, w.DWORD)
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.SelectObject.argtypes = (w.HDC, w.HGDIOBJ)
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteObject.argtypes = (w.HGDIOBJ,)
gdi32.GetObjectW.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID)
gdi32.GetDIBits.argtypes = (w.HDC, w.HBITMAP, w.UINT, w.UINT, w.LPVOID, ctypes.POINTER(BITMAPINFO), w.UINT)
shell32.SHGetFileInfoW.argtypes = (w.LPCWSTR, w.DWORD, ctypes.POINTER(SHFILEINFOW), w.UINT, w.UINT)
shell32.SHGetFileInfoW.restype = ctypes.c_size_t
shell32.SHGetImageList.argtypes = (ctypes.c_int, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
shell32.SHGetImageList.restype = ctypes.c_long


# ---- icons ------------------------------------------------------------------
def _hicon_to_image(hicon):
    ii = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(ii)):
        return None
    try:
        if not ii.hbmColor:
            return None  # monochrome icon; caller falls back to a letter tile
        bm = BITMAP()
        gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(bm), ctypes.byref(bm))
        wd, ht = bm.bmWidth, bm.bmHeight
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), wd, -ht, 1, 32)
        hdc = user32.GetDC(None)
        try:
            def bits(hbm):
                buf = ctypes.create_string_buffer(wd * ht * 4)
                gdi32.GetDIBits(hdc, hbm, 0, ht, buf, ctypes.byref(bmi), 0)
                return Image.frombuffer("RGBA", (wd, ht), buf.raw, "raw", "BGRA", 0, 1)
            img = bits(ii.hbmColor)
            if img.getextrema()[3][1] == 0:  # old-style icon without alpha: transparency lives in the mask
                img.putalpha(bits(ii.hbmMask).convert("L").point(lambda v: 0 if v else 255))
        finally:
            user32.ReleaseDC(None, hdc)
        return img
    finally:
        gdi32.DeleteObject(ii.hbmColor)
        gdi32.DeleteObject(ii.hbmMask)


def _imagelist_icon(shil, index):
    il = ctypes.c_void_p()
    if shell32.SHGetImageList(shil, ctypes.byref(IID_IImageList), ctypes.byref(il)) != 0 or not il:
        return None
    vtbl = ctypes.cast(il, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    try:
        hicon = w.HICON()
        if GETICON(vtbl[10])(il, index, 0x1, ctypes.byref(hicon)) != 0 or not hicon:  # IImageList::GetIcon
            return None
        try:
            return _hicon_to_image(hicon)
        finally:
            user32.DestroyIcon(hicon)
    finally:
        RELEASE(vtbl[2])(il)


@functools.lru_cache(maxsize=64)
def file_info(path):
    """(display name, icon or None) for a program, shortcut, file or folder, the way Explorer shows it."""
    fallback = os.path.splitext(os.path.basename(path.rstrip("\\/")))[0] or path
    info = SHFILEINFOW()
    if not shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info),
                                  SHGFI_SYSICONINDEX | SHGFI_DISPLAYNAME):
        return fallback, None
    name = info.szDisplayName or fallback
    if name.lower().endswith(".exe"):
        name = name[:-4]
    for shil in (SHIL_JUMBO, SHIL_EXTRALARGE):
        img = _imagelist_icon(shil, info.iIcon)
        box = img.getbbox() if img else None
        if box and (shil == SHIL_EXTRALARGE or box[2] - box[0] > 96):  # jumbo pads small icons into 256px
            return name, img.crop(box)
    return name, None


def display_name(path):
    return file_info(path)[0]


def _font(name, px):
    try:
        return ImageFont.truetype(name, max(8, int(px)))
    except OSError:
        return ImageFont.load_default(max(8, int(px)))


@functools.lru_cache(maxsize=16)
def _icon_font(px):
    for name in ("SegoeIcons.ttf", "segmdl2.ttf"):  # Windows 11, Windows 10
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            pass
    return None


# Segoe icon-font symbols for actions that have no program icon.
MEDIA_GLYPHS = {"play_pause": "", "next_track": "", "prev_track": "", "volume_up": "",
                "volume_down": "", "volume_mute": "", "browser_back": "",
                "browser_forward": ""}
TYPE_GLYPHS = {"keys": "", "text": "", "button": "", "run": ""}
TILE_COLORS = {"media": ("#ff6482", "#e5244b"), "keys": ("#4aa3ff", "#0a6cdf"), "text": ("#c77dff", "#9137d8"),
               "button": ("#4cd97b", "#1c9e48"), "run": ("#9aa0a6", "#62676c")}  # (top, bottom) gradient


def _symbol_tile(item, size):
    """Rounded gradient tile with a white symbol (or the first letter of the name if there's no symbol)."""
    a = item["action"]
    top, bottom = TILE_COLORS.get(a.get("type"), TILE_COLORS["run"])
    glyph = MEDIA_GLYPHS.get(a.get("value")) if a.get("type") == "media" else TYPE_GLYPHS.get(a.get("type"))
    big = size * 4  # draw large and shrink for smooth corners
    tile = Image.composite(Image.new("RGBA", (big, big), bottom), Image.new("RGBA", (big, big), top),
                           Image.linear_gradient("L").resize((big, big)))
    mask = Image.new("L", (big, big))
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 4, fill=255)
    tile.putalpha(mask)
    font = _icon_font(int(big * 0.46)) if glyph else None
    text = glyph if font else (item["name"][:1] or "?").upper()
    ImageDraw.Draw(tile).text((big / 2, big / 2), text, font=font or _font("seguisb.ttf", big * 0.5),
                              fill="white", anchor="mm")
    return tile.resize((size, size), Image.LANCZOS)


def item_icon(item, size):
    """Square RGBA tile for a bar item: the program's own icon, or a symbol tile for other actions."""
    a = item["action"]
    icon = file_info(a["value"])[1] if a.get("type") == "run" and a.get("value") else None
    if icon is None:
        return _symbol_tile(item, size)
    icon = ImageOps.contain(icon, (size, size), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size))
    tile.alpha_composite(icon, ((size - icon.width) // 2, (size - icon.height) // 2))
    return tile


# ---- drawing helpers ----------------------------------------------------------
@functools.lru_cache(maxsize=128)
def _circle(h):
    big = Image.new("L", (h * 4, h * 4))
    ImageDraw.Draw(big).ellipse((0, 0, h * 4 - 1, h * 4 - 1), fill=255)
    return big.resize((h, h), Image.LANCZOS)


def _pill_mask(wd, ht):
    """Anti-aliased stadium shape: two cached half-circles and a rectangle, so any width is cheap."""
    ht = max(1, int(round(ht)))
    wd = max(ht, int(round(wd)))
    m = Image.new("L", (wd, ht))
    c, r = _circle(ht), ht // 2
    m.paste(c.crop((0, 0, r, ht)), (0, 0))
    m.paste(c.crop((r, 0, ht, ht)), (wd - (ht - r), 0))
    if wd - (ht - r) > r:
        ImageDraw.Draw(m).rectangle((r, 0, wd - (ht - r) - 1, ht - 1), fill=255)
    return m


def _paste(img, layer, x, y):
    x, y = int(round(x)), int(round(y))
    if x < 0 or y < 0:  # alpha_composite refuses negative offsets
        layer = layer.crop((max(0, -x), max(0, -y), layer.width, layer.height))
        x, y = max(0, x), max(0, y)
    if layer.width and layer.height:
        img.alpha_composite(layer, (x, y))


def _paste_mask(img, rgb, alpha, x, y):
    layer = Image.new("RGBA", alpha.size, rgb + (0,))
    layer.putalpha(alpha)
    _paste(img, layer, x, y)


def _pill(img, x, y, wd, ht, rgb, fill_a, border_a, shadow_a, s):
    if wd < 2 or ht < 2:
        return
    if shadow_a > 0.01:  # blur a quarter-size copy: a soft shadow for a fraction of the cost
        q, p = 4, 6
        small = Image.new("L", (int(wd / q) + 2 * p, int(ht / q) + 2 * p))
        small.paste(_pill_mask(wd / q, ht / q), (p, p))
        soft = small.filter(ImageFilter.GaussianBlur(2.2)).resize((small.width * q, small.height * q), Image.BILINEAR)
        _paste_mask(img, (0, 0, 0), soft.point(lambda v: int(v * shadow_a)), x - p * q, y - p * q + 7 * s)
    mask = _pill_mask(wd, ht)
    _paste_mask(img, rgb, mask.point(lambda v: int(v * fill_a)), x, y)
    if border_a > 0.01:
        b = max(1, round(s))
        inner = Image.new("L", mask.size)
        inner.paste(_pill_mask(wd - 2 * b, ht - 2 * b), (b, b))
        _paste_mask(img, (255, 255, 255), ImageChops.subtract(mask, inner).point(lambda v: int(v * border_a)), x, y)


def _text(img, cx, cy, text, font, a):
    tw, th = int(font.getlength(text)) + 8, int(font.size * 1.8)
    layer = Image.new("RGBA", (tw, th))
    ImageDraw.Draw(layer).text((tw / 2, th / 2), text, font=font, fill=(255, 255, 255, int(255 * a)), anchor="mm")
    _paste(img, layer, cx - tw / 2, cy - th / 2)


def _ease_out_cubic(p):
    return 1 - (1 - p) ** 3


def _ease_in_cubic(p):
    return p ** 3


def _ease_out_back(p, c=1.7):  # overshoots a little, then settles: the "spring" in the intro
    p -= 1
    return 1 + (c + 1) * p ** 3 + c * p ** 2


def _cursor():
    pt = w.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _work_area(pt):
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(user32.MonitorFromPoint(w.POINT(*pt), 2), ctypes.byref(mi))  # 2 = nearest monitor
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


def pick(px, py, centers, bar_cy, item, reach):
    """Index of the item under the cursor, or None when the cursor is off the bar (letting go there cancels)."""
    if not centers or abs(py - bar_cy) > reach or not centers[0] - item <= px <= centers[-1] + item:
        return None
    return min(range(len(centers)), key=lambda i: abs(px - centers[i]))


class Launcher:
    INTRO, CANCEL, LAUNCH = 0.30, 0.18, 0.36  # animation lengths in seconds

    def __init__(self, root, run):
        self.root, self.run = root, run  # run(action) performs the chosen item's action
        s = self.s = root.winfo_fpixels("1i") / 96
        self.SH, self.LABEL = 32 * s, 48 * s  # canvas room for the shadow and the name label
        self.font = _font("seguisb.ttf", 13 * s)
        self.items, self.tiles, self.sized, self.pop = [], [], {}, []
        self.phase, self._ticking, self._shown, self.size, self.locked = None, False, False, None, False
        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "STATIC", None, WS_POPUP, 0, 0, 1, 1, None, None, None, None)
        self.dc = gdi32.CreateCompatibleDC(None)
        self.bmp = self.old = None
        self.bits = ctypes.c_void_p()

    def configure(self, items, height=76):
        """Programs to show and the bar height in px at 100% scaling; everything else scales from it."""
        if self.phase:  # never resize mid-animation
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.phase, self._shown = None, False
        self.BAR_H = max(40, height) * self.s
        self.PAD = self.GAP = round(self.BAR_H * 0.16)
        self.B = self.BAR_H - 2 * self.PAD  # icon size
        self.M = self.B * 1.6  # magnified icon size
        self.items = [dict(it) for it in items]
        self.tiles = [item_icon(it, int(self.M)) for it in self.items]
        self.sized = {}

    def preview(self, at):
        """Show the bar at a screen point and close it again; nothing can be launched."""
        self.press(at)
        self.root.after(1400, self.release)

    # ---- input --------------------------------------------------------------
    def press(self, at=None):
        if self.phase in ("in", "open"):
            return
        s, n = self.s, len(self.items)
        self.locked = at is not None
        self.start = at or _cursor()
        px, py = self.start
        self.armed, self.sel, self.chosen = False, None, None
        self.sizes, self.pop = [self.B] * n, [0.0] * n
        self.empty_w = self.font.getlength(EMPTY_TEXT) + 4 * self.PAD
        bw0 = self._base_width()
        left, top, right, bottom = _work_area(self.start)
        self.cx = min(max(px, left + bw0 / 2 + 12 * s), right - bw0 / 2 - 12 * s)
        tall = self.BAR_H + (self.M - self.B) + self.LABEL + self.SH
        # Above the cursor if it fits, otherwise below it.
        self.bar_bottom = py - 24 * s if py - 24 * s - tall > top else py + 32 * s + self.BAR_H
        cw = int(max(bw0, n * (self.M + self.GAP), 260 * s) + 2 * (self.SH + self.PAD))
        ch = int(self.SH + self.LABEL + (self.M - self.B) + self.BAR_H + self.SH)
        self.wx, self.wy = int(self.cx - cw / 2), int(self.bar_bottom - (ch - self.SH))
        self._canvas(cw, ch)
        self.phase = "in"
        self.t0 = self._last = time.perf_counter()
        if not self._ticking:
            self._ticking = True
            self._frame()

    def release(self):
        if self.phase not in ("in", "open"):
            return
        self.chosen = self.sel
        self.phase = "cancel" if self.sel is None else "launch"
        self.t0 = time.perf_counter()
        if self.chosen is not None:
            self.run(self.items[self.chosen]["action"])

    # ---- animation ----------------------------------------------------------
    def _base_width(self):
        n = len(self.items)
        return n * self.B + (n - 1) * self.GAP + 2 * self.PAD if n else self.empty_w

    def _centers(self):
        left = self.cx - self._base_width() / 2 + self.PAD + self.B / 2
        return [left + i * (self.B + self.GAP) for i in range(len(self.items))]

    def _track(self, dt):
        px, py = _cursor()
        if not self.armed and math.hypot(px - self.start[0], py - self.start[1]) > 12 * self.s:
            self.armed = True  # a tap without moving never launches anything
        centers = self._centers()
        self.sel = (pick(px, py, centers, self.bar_bottom - self.BAR_H / 2, self.B + self.GAP, 150 * self.s)
                    if self.armed and not self.locked else None)
        ease = 1 - math.exp(-dt * 20)  # frame-rate independent smoothing
        for i, c in enumerate(centers):
            target = self.B
            if self.sel is not None:  # dock-style magnification, strongest right under the cursor
                target += (self.M - self.B) * math.exp(-((px - c) / (1.2 * (self.B + self.GAP))) ** 2)
            self.sizes[i] += (target - self.sizes[i]) * ease

    def _frame(self):
        if self.phase is None:  # reconfigured mid-animation
            self._ticking = False
            return
        now = time.perf_counter()
        dt, self._last = min(0.05, now - self._last), now
        el, n, s = now - self.t0, len(self.items), self.s
        if self.phase == "in" and el >= self.INTRO:
            self.phase = "open"
        if self.phase in ("in", "open"):
            self._track(dt)
            p = min(1, el / self.INTRO)
            # Icons pop in from the middle outwards.
            self.pop = [_ease_out_back(min(1, max(0, (el - abs(i - (n - 1) / 2) * 0.035) / 0.24))) for i in range(n)]
            frame = dict(k=0.8 + 0.2 * _ease_out_back(p), dy=(1 - _ease_out_cubic(p)) * 16 * s,
                         bar_a=_ease_out_cubic(min(1, p * 1.4)), scale=self.pop, alpha=[1] * n,
                         lift=[0] * n, label=self.sel)
            done = False
        elif self.phase == "cancel":
            p = min(1, el / self.CANCEL)
            fade = (1 - p) ** 2
            frame = dict(k=1 - 0.12 * _ease_in_cubic(p), dy=12 * s * _ease_in_cubic(p), bar_a=fade,
                         scale=self.pop, alpha=[fade] * n, lift=[0] * n, label=None)
            done = p >= 1
        else:  # "launch": the chosen icon swells and floats up while everything else fades away
            p, c = min(1, el / self.LAUNCH), self.chosen
            fade = 1 - _ease_out_cubic(min(1, p * 1.7))
            frame = dict(k=1, dy=0, bar_a=fade,
                         scale=[1 + 0.45 * _ease_out_cubic(p) if i == c else self.pop[i] for i in range(n)],
                         alpha=[1 - _ease_in_cubic(p) if i == c else fade for i in range(n)],
                         lift=[-22 * s * _ease_out_cubic(p) if i == c else 0 for i in range(n)], label=None)
            done = p >= 1
        self._push(self.render(**frame))
        if done:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.phase, self._ticking, self._shown = None, False, False
            return
        self.root.after(15, self._frame)

    # ---- drawing ------------------------------------------------------------
    def _tile(self, i, size):
        key = (i, size)
        if key not in self.sized:
            if len(self.sized) > 300:
                self.sized.clear()
            self.sized[key] = self.tiles[i].resize((size, size), Image.BICUBIC)
        return self.sized[key]

    def render(self, k, dy, bar_a, scale, alpha, lift, label):
        """One frame. k scales the whole bar about its bottom centre; the rest is per element."""
        s, n = self.s, len(self.items)
        img = Image.new("RGBA", self.size)
        ax, ay = self.cx - self.wx, self.size[1] - self.SH + dy
        sizes = [z * k for z in self.sizes]
        pad, gap, bh = self.PAD * k, self.GAP * k, self.BAR_H * k
        bw = sum(sizes) + gap * (n - 1) + 2 * pad if n else self.empty_w * k
        _pill(img, ax - bw / 2, ay - bh, bw, bh, (28, 28, 32), 0.80 * bar_a, 0.16 * bar_a, 0.5 * bar_a, s)
        if not n:
            _text(img, ax, ay - bh / 2, EMPTY_TEXT, self.font, bar_a)
            return img
        x, label_at = ax - bw / 2 + pad, None
        for i, z in enumerate(sizes):
            zz = z * scale[i]
            if zz >= 2 and alpha[i] > 0.01:
                tile = self._tile(i, int(zz))
                if alpha[i] < 0.99:
                    tile = tile.copy()
                    tile.putalpha(tile.getchannel("A").point(lambda v, a=alpha[i]: int(v * a)))
                bottom = ay - pad - (z - zz) / 2 + lift[i]  # icons sit on the bar and grow upwards
                _paste(img, tile, x + z / 2 - tile.width / 2, bottom - tile.height)
            if i == label:
                label_at = (x + z / 2, ay - pad - z)
            x += z + gap
        if label_at:
            name = self.items[label]["name"]
            lw, lh = self.font.getlength(name) + 26 * s, 28 * s
            lx = min(max(label_at[0] - lw / 2, 2), img.width - lw - 2)
            ly = label_at[1] - 10 * s - lh
            _pill(img, lx, ly, lw, lh, (24, 24, 28), 0.92 * bar_a, 0.16 * bar_a, 0, s)
            _text(img, lx + lw / 2, ly + lh / 2, name, self.font, bar_a)
        return img

    def _canvas(self, cw, ch):
        if self.size == (cw, ch):
            return
        if self.bmp:
            gdi32.SelectObject(self.dc, self.old)
            gdi32.DeleteObject(self.bmp)
        bmi = BITMAPINFO()
        bmi.bmiHeader = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), cw, -ch, 1, 32)
        self.bmp = gdi32.CreateDIBSection(self.dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bmp)
        self.size = (cw, ch)

    def _push(self, img):
        r, g, b, a = img.convert("RGBa").split()  # layered windows want premultiplied BGRA
        data = Image.merge("RGBA", (b, g, r, a)).tobytes()
        ctypes.memmove(self.bits.value, data, len(data))
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, per-pixel alpha
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(w.POINT(self.wx, self.wy)),
                                   ctypes.byref(w.SIZE(*img.size)), self.dc, ctypes.byref(w.POINT(0, 0)),
                                   0, ctypes.byref(blend), ULW_ALPHA)
        if not self._shown:
            user32.SetWindowPos(self.hwnd, w.HWND(-1), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self._shown = True
