"""Settings window, layer pop-up, bar editor and tray icon."""
import copy
import ctypes
import os
import queue
import sys
import tkinter as tk
import uuid
import winreg
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk

from hook import parse_keys
from launcher import Launcher, display_name, item_icon
from remap import BUTTONS, WHEEL, save_config

try:  # Windows 11 look; falls back to the stock theme if missing
    import sv_ttk
except ImportError:
    sv_ttk = None
try:  # tray icon is optional: without it, closing the window quits
    import pystray
except ImportError:
    pystray = None

APP_NAME = "Layerhop"
OLD_APP_NAME = "Mouse Layers"  # autostart entry written by earlier versions
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TITLE_FONT = ("Segoe UI Semibold", 20)
HEAD_FONT = ("Segoe UI Semibold", 10)
# Sun Valley's own accent colours, so the mouse drawing matches the widgets.
PALETTE = {
    True: {"body": "#2d2d2d", "part": "#3a3a3a", "line": "#5c5c5c", "mapped": "#0f4a73",
           "hot": "#57c8ff", "muted": "#9d9d9d"},
    False: {"body": "#ffffff", "part": "#f0f0f0", "line": "#b8b8b8", "mapped": "#cce4f7",
            "hot": "#005fb8", "muted": "#6b6b6b"},
}
BUTTON_LABELS = {"left": "Left click", "right": "Right click", "middle": "Middle (wheel click)",
                 "x1": "Back (side button)", "x2": "Forward (side button)",
                 "wheel_up": "Wheel up", "wheel_down": "Wheel down"}
MEDIA_LABELS = {"play_pause": "Play / Pause", "next_track": "Next track", "prev_track": "Previous track",
                "volume_up": "Volume up", "volume_down": "Volume down", "volume_mute": "Mute",
                "browser_back": "Browser back", "browser_forward": "Browser forward"}
TYPES = {"default": "Normal (no change)", "button": "Mouse button", "keys": "Key combo",
         "media": "Media key", "text": "Type text", "run": "Open program / file / website",
         "launcher": "Open a bar (hold)", "disabled": "Do nothing"}
HOLD_TYPES = TYPES | {"default": "No hold action"}
ITEM_TYPES = {k: v for k, v in TYPES.items() if k not in ("default", "launcher", "disabled")}  # what bar buttons do
CHOICES = {"button": BUTTON_LABELS, "media": MEDIA_LABELS}
# Left is excluded on purpose: delaying every left click to detect double-clicks would be miserable.
TOGGLE_LABELS = {b: BUTTON_LABELS[b] for b in ("middle", "right", "x1", "x2")} | {"": "Nothing (layers off)"}
START_MENU = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), r"Microsoft\Windows\Start Menu\Programs")
MAX_BAR_ITEMS = 12
BAR_HEIGHTS = (48, 140)  # bar height slider range, px
MOD_KEYS = {"Control_L": "ctrl", "Control_R": "ctrl", "Shift_L": "shift", "Shift_R": "shift",
            "Alt_L": "alt", "Alt_R": "alt", "Win_L": "win", "Win_R": "win"}
TK_KEYS = {"Return": "enter", "Escape": "esc", "Prior": "pageup", "Next": "pagedown", "BackSpace": "backspace",
           "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end", "Left": "left",
           "Right": "right", "Up": "up", "Down": "down", "Tab": "tab", "space": "space",
           "Print": "printscreen", "Caps_Lock": "capslock"}


def _style(name, fallback):
    return name if sv_ttk else fallback


def _key_of(labels, label):
    return next((k for k, v in labels.items() if v == label), label)


def _windows_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def _dwm(win, attr, value):
    """Windows 11 frame tweaks (20 = dark title bar, 33 = rounded corners); ignored on older Windows."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        v = ctypes.c_int(value)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
    except (AttributeError, OSError):
        pass


def _is_dark():
    return bool(sv_ttk) and sv_ttk.get_theme() == "dark"


def _icon_image():
    s = 256  # draw big, shrink for smooth edges
    img = Image.new("RGBA", (s, s))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((56, 16, 200, 240), 72, fill="#0a84ff")
    d.line((56, 112, 200, 112), fill="white", width=10)
    d.line((128, 16, 128, 112), fill="white", width=10)
    d.rounded_rectangle((112, 44, 144, 94), 16, fill="white")
    return img.resize((64, 64), Image.LANCZOS)


def _tk_key_name(e):
    ks = e.keysym
    if ks in TK_KEYS:
        return TK_KEYS[ks]
    if len(ks) == 1 or (ks[0] == "F" and ks[1:].isdigit()):
        return ks.lower()
    return e.char if e.char.strip() and e.char.isprintable() else ks.lower()


def record_keys(root):
    """Modal 'press a combo' dialog. Returns e.g. 'ctrl+shift+t', or None if cancelled."""
    win = tk.Toplevel(root)
    win.title("Record keys")
    win.transient(root)
    win.resizable(False, False)
    box = ttk.Frame(win, padding=24)
    box.pack(fill="both", expand=True)
    ttk.Label(box, text="Press the key combination now", font=("Segoe UI Semibold", 13)).pack()
    ttk.Label(box, justify="center", foreground=PALETTE[_is_dark()]["muted"],
              text="Combos Windows grabs for itself (like Win+D)\ncan be typed into the box instead, e.g. win+d"
              ).pack(pady=(6, 16))
    ttk.Button(box, text="Cancel", command=win.destroy, takefocus=False).pack()
    _dwm(win, 20, int(_is_dark()))
    mods, result = set(), []

    def press(e):
        if e.keysym in MOD_KEYS:
            mods.add(MOD_KEYS[e.keysym])
        else:
            result.append("+".join([m for m in ("ctrl", "shift", "alt", "win") if m in mods] + [_tk_key_name(e)]))
            win.destroy()
        return "break"

    win.bind("<KeyPress>", press)
    win.bind("<KeyRelease>", lambda e: mods.discard(MOD_KEYS.get(e.keysym)))
    win.grab_set()
    win.focus_force()
    root.wait_window(win)
    return result[0] if result else None


def _describe(a):
    t, v = a.get("type"), a.get("value", "")
    return f"{ITEM_TYPES.get(t, t)}: {CHOICES.get(t, {}).get(v, v)}"


def _auto_name(a):
    t, v = a.get("type"), a.get("value", "")
    if t in CHOICES:
        return CHOICES[t].get(v, v)
    if t == "run":
        return display_name(v)
    return f"Type “{v[:14]}”" if t == "text" else v


def edit_item(root, item=None):
    """Dialog for one bar button: a name and what it does. Returns the new item, or None if cancelled."""
    win = tk.Toplevel(root)
    win.title("Bar button")
    win.transient(root)
    win.resizable(False, False)
    box = ttk.Frame(win, padding=24)
    box.pack(fill="both", expand=True)
    ttk.Label(box, text="Edit button" if item else "New button", font=("Segoe UI Semibold", 15)
              ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
    ttk.Label(box, text="Name").grid(row=1, column=0, sticky="w")
    name = tk.StringVar(value=item["name"] if item else "")
    ttk.Entry(box, textvariable=name, width=28).grid(row=1, column=1, sticky="w", padx=4, pady=4)
    ttk.Label(box, text="Leave empty to name it after the action.", foreground=PALETTE[_is_dark()]["muted"]
              ).grid(row=1, column=2, columnspan=2, sticky="w", padx=4)
    row = Row(box, 2, "Does", item["action"] if item else {"type": "media"}, lambda: None, lambda: None, ITEM_TYPES)
    result = []

    def ok():
        a = row.action()
        try:
            if a["type"] in ("keys", "media"):
                parse_keys(a["value"])
            elif not a["value"]:
                raise ValueError("pick or type what it should do")
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"This button: {e}.", parent=win)
            return
        result.append({"name": name.get().strip() or _auto_name(a), "action": a})
        win.destroy()

    btns = ttk.Frame(box)
    btns.grid(row=3, column=0, columnspan=4, sticky="e", pady=(18, 0))
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
    ttk.Button(btns, text="OK", style=_style("Accent.TButton", "TButton"), command=ok).pack(side="right", padx=8)
    _dwm(win, 20, int(_is_dark()))
    win.grab_set()
    root.wait_window(win)
    return result[0] if result else None


def edit_bars(root, bars, preview):
    """Modal editor for pop-up bars: edits `bars` in place. preview(bar, screen_point) shows one for real."""
    win = tk.Toplevel(root)
    win.title("Bars")
    win.transient(root)
    muted = PALETTE[_is_dark()]["muted"]
    box = ttk.Frame(win, padding=24)
    box.pack(fill="both", expand=True)
    box.columnconfigure(1, weight=1)
    box.rowconfigure(2, weight=1)
    ttk.Label(box, text="Pop-up bars", font=("Segoe UI Semibold", 15)).grid(row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(box, foreground=muted, justify="left",
              text="Set a mouse button to “Open a bar (hold)”. Hold it, slide to a button on the bar, let go to run it.\n"
                   "Let go off the bar to cancel. Buttons appear left to right in the order shown here."
              ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 14))
    size = int(22 * root.winfo_fpixels("1i") / 96)
    ttk.Style().configure("Bars.Treeview", rowheight=size + 14)

    left = ttk.Frame(box)
    left.grid(row=2, column=0, sticky="ns", padx=(0, 20))
    ttk.Label(left, text="Bars", font=HEAD_FONT).pack(anchor="w", pady=(0, 4))
    bar_tree = ttk.Treeview(left, show="tree", height=9, selectmode="browse", style="Bars.Treeview")
    bar_tree.column("#0", width=170)
    bar_tree.pack(fill="y", expand=True)
    right = ttk.Frame(box)
    right.grid(row=2, column=1, sticky="nsew")
    ttk.Label(right, text="Buttons on this bar", font=HEAD_FONT).pack(anchor="w", pady=(0, 4))
    tree = ttk.Treeview(right, columns=("does",), show="tree", height=9, selectmode="browse", style="Bars.Treeview")
    tree.column("#0", width=230)
    tree.column("does", width=330)
    tree.pack(fill="both", expand=True)
    cur, photos = [0], []

    def bar():
        return bars[cur[0]]

    def items():
        return bar()["items"]

    def refresh_bars(select=0):
        bar_tree.delete(*bar_tree.get_children())
        for i, b in enumerate(bars):
            bar_tree.insert("", "end", iid=str(i), text="  " + b["name"])
        bar_tree.selection_set(str(min(select, len(bars) - 1)))  # fires on_bar_select

    def on_bar_select(_=None):
        sel = bar_tree.selection()
        if sel:
            cur[0] = int(sel[0])
            scale.set(bar()["height"])
            refresh_items(0)

    def refresh_items(select=None):
        tree.delete(*tree.get_children())
        photos.clear()
        for i, it in enumerate(items()):
            photos.append(ImageTk.PhotoImage(item_icon(it, size)))
            tree.insert("", "end", iid=str(i), text="  " + it["name"], image=photos[-1],
                        values=(_describe(it["action"]),))
        if items() and select is not None:
            tree.selection_set(str(min(select, len(items()) - 1)))
            tree.see(str(min(select, len(items()) - 1)))

    def current():
        sel = tree.selection()
        return int(sel[0]) if sel else None

    def ask_name(title, initial=""):
        name = (simpledialog.askstring(title, "Bar name:", initialvalue=initial, parent=win) or "").strip()
        if name.lower() in {b["name"].lower() for b in bars} - {initial.lower()}:
            messagebox.showinfo(APP_NAME, f"There's already a bar called “{name}”.", parent=win)
            return None
        return name or None

    def new_bar():
        name = ask_name("New bar")
        if name:
            bars.append({"id": uuid.uuid4().hex[:8], "name": name, "height": 76, "items": []})
            refresh_bars(len(bars) - 1)

    def rename_bar():
        name = ask_name("Rename bar", bar()["name"])
        if name:
            bar()["name"] = name
            refresh_bars(cur[0])

    def delete_bar():
        if len(bars) == 1:
            messagebox.showinfo(APP_NAME, "Keep at least one bar.", parent=win)
        elif messagebox.askyesno(APP_NAME, f"Delete the “{bar()['name']}” bar?\n\n"
                                 "Mouse buttons that open it will open the first bar instead.", parent=win):
            del bars[cur[0]]
            refresh_bars(0)

    def room():
        if len(items()) < MAX_BAR_ITEMS:
            return True
        messagebox.showinfo(APP_NAME, f"A bar holds up to {MAX_BAR_ITEMS} buttons.", parent=win)
        return False

    def add_programs():
        paths = filedialog.askopenfilenames(parent=win, title="Pick programs or Start menu shortcuts",
                                            initialdir=START_MENU,
                                            filetypes=[("Programs and shortcuts", "*.exe *.lnk"), ("All files", "*.*")])
        for p in paths:
            if not room():
                break
            p = os.path.normpath(p)
            items().append({"name": display_name(p), "action": {"type": "run", "value": p}})
        refresh_items(len(items()) - 1)

    def add_action():
        it = room() and edit_item(win)
        if it:
            items().append(it)
            refresh_items(len(items()) - 1)

    def edit(_=None):
        i = current()
        it = edit_item(win, items()[i]) if i is not None else None
        if it:
            items()[i] = it
            refresh_items(i)

    def remove():
        i = current()
        if i is not None:
            del items()[i]
            refresh_items(i)

    def move(d):
        i = current()
        if i is not None and 0 <= i + d < len(items()):
            items()[i], items()[i + d] = items()[i + d], items()[i]
            refresh_items(i + d)

    lb = ttk.Frame(left)
    lb.pack(fill="x", pady=(10, 0))
    ttk.Button(lb, text="+ New bar", command=new_bar).pack(fill="x")
    ttk.Button(lb, text="Rename", command=rename_bar).pack(fill="x", pady=(6, 0))
    ttk.Button(lb, text="Delete", command=delete_bar).pack(fill="x", pady=(6, 0))

    ib = ttk.Frame(right)
    ib.pack(fill="x", pady=(10, 0))
    ttk.Button(ib, text="+ Program…", style=_style("Accent.TButton", "TButton"), command=add_programs).pack(side="left")
    ttk.Button(ib, text="+ Action…", style=_style("Accent.TButton", "TButton"), command=add_action
               ).pack(side="left", padx=(8, 0))
    ttk.Button(ib, text="Edit", command=edit).pack(side="left", padx=(8, 0))
    ttk.Button(ib, text="Remove", command=remove).pack(side="left", padx=(8, 0))
    ttk.Button(ib, text="◀", width=3, command=lambda: move(-1)).pack(side="left", padx=(8, 0))
    ttk.Button(ib, text="▶", width=3, command=lambda: move(1)).pack(side="left", padx=(4, 0))

    look = ttk.Frame(right)
    look.pack(fill="x", pady=(16, 0))
    ttk.Label(look, text="Bar height", font=HEAD_FONT).pack(side="left")
    shown = ttk.Label(look, width=7)

    def slide(v):
        bar()["height"] = int(float(v))
        shown.configure(text=f"{bar()['height']} px")

    def show_preview(_=None):
        # Anchor near the bottom of this window so the bar rises over it.
        preview(bar(), (win.winfo_rootx() + win.winfo_width() // 2, win.winfo_rooty() + win.winfo_height() - 30))

    scale = ttk.Scale(look, from_=BAR_HEIGHTS[0], to=BAR_HEIGHTS[1], command=slide)
    scale.pack(side="left", fill="x", expand=True, padx=12)
    scale.bind("<ButtonRelease-1>", show_preview)  # let go of the slider to see the result
    shown.pack(side="left")
    ttk.Button(look, text="Preview", command=show_preview).pack(side="left", padx=(8, 0))
    ttk.Button(box, text="Done", command=win.destroy).grid(row=3, column=1, sticky="e", pady=(18, 0))

    bar_tree.bind("<<TreeviewSelect>>", on_bar_select)
    tree.bind("<Double-1>", edit)
    refresh_bars(0)
    _dwm(win, 20, int(_is_dark()))
    win.grab_set()
    root.wait_window(win)


def _autostart_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(sys.argv[0])}" --tray'


def _autostart_get():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            for name in (APP_NAME, OLD_APP_NAME):
                try:
                    winreg.QueryValueEx(k, name)
                    return True
                except FileNotFoundError:
                    pass
        return False
    except OSError:
        return False


def _autostart_set(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())
        for name in ((OLD_APP_NAME,) if on else (APP_NAME, OLD_APP_NAME)):
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass


def _round_rect(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class MouseView(tk.Canvas):
    """Clickable mouse drawing: tints remapped buttons and lights up on real presses."""

    def __init__(self, parent, colors, on_pick):
        super().__init__(parent, width=180, height=290, highlightthickness=0,
                         bg=str(ttk.Style().lookup(".", "background")) or "SystemButtonFace")
        self.c, self.parts, self.mapped = colors, {}, set()
        part = dict(fill=colors["part"], outline=colors["line"], width=1.5)
        self.parts["x2"] = _round_rect(self, 22, 138, 40, 172, 7, **part)
        self.parts["x1"] = _round_rect(self, 22, 178, 40, 212, 7, **part)
        _round_rect(self, 30, 20, 150, 280, 58, fill=colors["body"], outline=colors["line"], width=1.5)
        # Repeated points are sharp corners in a smooth polygon; the single outer point rounds into the body.
        self.parts["left"] = self.create_polygon(88, 22, 88, 22, 88, 118, 88, 118, 32, 118, 32, 118, 32, 22,
                                                 smooth=True, **part)
        self.parts["right"] = self.create_polygon(92, 22, 92, 22, 92, 118, 92, 118, 148, 118, 148, 118, 148, 22,
                                                  smooth=True, **part)
        self.parts["middle"] = _round_rect(self, 80, 44, 100, 96, 10, **part)
        arrow = dict(fill=colors["line"], outline="")
        self.parts["wheel_up"] = self.create_polygon(90, 28, 82, 38, 98, 38, **arrow)
        self.parts["wheel_down"] = self.create_polygon(90, 112, 82, 102, 98, 102, **arrow)
        for b, item in self.parts.items():
            self.tag_bind(item, "<Button-1>", lambda e, b=b: on_pick(b))
            self.tag_bind(item, "<Enter>", lambda e: self.configure(cursor="hand2"))
            self.tag_bind(item, "<Leave>", lambda e: self.configure(cursor=""))
        s = self.winfo_fpixels("1i") / 96  # keep the drawing in proportion on scaled displays
        self.scale("all", 0, 0, s, s)
        self.configure(width=180 * s, height=290 * s)

    def _fill(self, b):
        if b in self.mapped:
            return self.c["mapped"] if b not in WHEEL else self.c["hot"]
        return self.c["part"] if b not in WHEEL else self.c["line"]

    def paint(self, mapped):
        self.mapped = mapped
        for b, item in self.parts.items():
            self.itemconfigure(item, fill=self._fill(b))

    def select(self, b):
        """Outline the part being edited, so the drawing and the table point at the same button."""
        for k, item in self.parts.items():
            on = k == b
            self.itemconfigure(item, outline=self.c["hot"] if on else "" if k in WHEEL else self.c["line"],
                               width=2.5 if on else 1.5)

    def flash(self, b):
        self.itemconfigure(self.parts[b], fill=self.c["hot"])
        self.after(250, lambda: self.itemconfigure(self.parts[b], fill=self._fill(b)))


class Row:
    """One action editor line: label, action type, details and a helper button."""

    def __init__(self, parent, r, text, action, on_change, on_bars, types=TYPES, choices=CHOICES.get):
        self.on_bars, self.types, self.choices = on_bars, types, choices  # choices(type) -> {key: label} or None
        self.label = ttk.Label(parent, text=text, width=22)
        self.label.grid(row=r, column=0, sticky="w", padx=(0, 8), pady=4)
        t = action.get("type", "default")
        self.type = tk.StringVar(value=types.get(t) or next(iter(types.values())))
        self.type_box = ttk.Combobox(parent, textvariable=self.type, values=list(types.values()),
                                     state="readonly", width=26)
        self.type_box.grid(row=r, column=1, padx=4, pady=4)
        self.value = tk.StringVar()
        self.box = ttk.Combobox(parent, textvariable=self.value, width=30)
        self.box.grid(row=r, column=2, padx=4, pady=4)
        self.helper = ttk.Button(parent, width=9, command=self._help)
        self.helper.grid(row=r, column=3, padx=(4, 0), pady=4)
        self._refresh()
        v, options = action.get("value", ""), choices(t) or {}
        if t == "launcher" and v not in options and options:
            v = next(iter(options))  # bar was deleted (or an old config): fall back to the first bar
        self.value.set(options.get(v, v))
        self.type.trace_add("write", lambda *_: (self._refresh(clear=True), on_change()))
        self.value.trace_add("write", lambda *_: on_change())

    def kind(self):
        return _key_of(self.types, self.type.get())

    def show(self, on):
        for wdg in (self.label, self.type_box, self.box):
            wdg.grid() if on else wdg.grid_remove()
        if on:
            self._refresh()  # brings back the helper button if this action type has one
        else:
            self.helper.grid_remove()

    def _refresh(self, clear=False):
        t = self.kind()
        choices = self.choices(t)
        if clear:
            self.value.set(next(iter(choices.values())) if t == "launcher" and choices else "")
        self.box.configure(values=list(choices.values()) if choices else [],
                           state="readonly" if choices else "normal")
        # Nothing to fill in for these, so don't show an empty box.
        self.box.grid_remove() if t in ("default", "disabled") else self.box.grid()
        helper = {"keys": "Record…", "run": "Browse…", "launcher": "Bars…"}.get(t)
        if helper:
            self.helper.configure(text=helper)
            self.helper.grid()
        else:
            self.helper.grid_remove()

    def _help(self):
        if self.kind() == "launcher":
            self.on_bars()
            return
        if self.kind() == "keys":
            combo = record_keys(self.box.winfo_toplevel())
        else:
            combo = filedialog.askopenfilename(title="Pick a program or file")
        if combo:
            self.value.set(os.path.normpath(combo) if self.kind() == "run" else combo)

    def action(self):
        t = self.kind()
        if t == "default":
            return None
        v, choices = self.value.get().strip(), self.choices(t)
        return {"type": t, "value": _key_of(choices, v) if choices else v}


class App:
    def __init__(self, root, engine, config, path, events, start_hidden=False):
        self.root, self.engine, self.config, self.path, self.events = root, engine, config, path, events
        root.title(APP_NAME)
        root.protocol("WM_DELETE_WINDOW", self.close)
        dark = bool(sv_ttk) and _windows_dark()
        if sv_ttk:
            sv_ttk.set_theme("dark" if dark else "light")
            # The theme colours itself on <<ThemeChanged>>, which doesn't fire on Python 3.10's Tk; do it now.
            root.tk.call("configure_colors")
        self.c = c = PALETTE[dark]
        _dwm(root, 20, int(dark))  # title bar matches the theme
        self._icon = ImageTk.PhotoImage(_icon_image())
        root.iconphoto(True, self._icon)
        self.bars = copy.deepcopy(config["bars"])  # edited copy; applied on Save
        self.launchers, self._preview = {}, None  # bar id -> its pop-up window
        self._sync_bars(self.bars)

        header = ttk.Frame(root, padding=(24, 20, 24, 4))
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, font=TITLE_FONT).grid(row=0, column=0, sticky="w")
        ttk.Label(header, foreground=c["muted"],
                  text="Give every button a job. Double-click the switch button to flip them all to another layer."
                  ).grid(row=1, column=0, sticky="w")
        self.enabled = tk.BooleanVar(value=engine.rm.enabled)
        ttk.Checkbutton(header, text="Remapping", style=_style("Switch.TCheckbutton", "TCheckbutton"),
                        variable=self.enabled, command=lambda: engine.set_enabled(self.enabled.get())
                        ).grid(row=0, column=1, rowspan=2, sticky="e")

        body = ttk.Frame(root, padding=(24, 16, 24, 8))
        body.pack(fill="both", expand=True)
        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(0, 28))
        self.mouse = MouseView(side, c, self._pick)
        self.mouse.pack()
        ttk.Label(side, text="Click a part to edit it.\nBlue parts are changed on this layer.", justify="center",
                  foreground=c["muted"]).pack(pady=(8, 16))
        ttk.Separator(side).pack(fill="x", pady=(0, 16))
        ttk.Label(side, text="Switch layers with", font=HEAD_FONT).pack(anchor="w")
        self.toggle = tk.StringVar(value=TOGGLE_LABELS.get(config["toggle_button"], TOGGLE_LABELS[""]))
        ttk.Combobox(side, textvariable=self.toggle, values=list(TOGGLE_LABELS.values()),
                     state="readonly", width=22).pack(fill="x", pady=(4, 14))
        ttk.Label(side, text="Double-click speed (ms)", font=HEAD_FONT).pack(anchor="w")
        self.speed = tk.StringVar(value=str(config["double_click_ms"]))
        ttk.Spinbox(side, from_=150, to=900, increment=50, textvariable=self.speed, width=8
                    ).pack(fill="x", pady=(4, 14))
        ttk.Label(side, text="Hold counts after (ms)", font=HEAD_FONT).pack(anchor="w")
        self.hold_ms = tk.StringVar(value=str(config["hold_ms"]))
        ttk.Spinbox(side, from_=150, to=1500, increment=50, textvariable=self.hold_ms, width=8,
                    command=self._show_mode).pack(fill="x", pady=(4, 14))
        self.autostart = tk.BooleanVar(value=_autostart_get())
        ttk.Checkbutton(side, text="Start with Windows", style=_style("Switch.TCheckbutton", "TCheckbutton"),
                        variable=self.autostart, command=self._set_autostart).pack(anchor="w")
        ttk.Button(side, text="Pop-up bars…", command=self.edit_bars).pack(fill="x", pady=(16, 0))

        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        modebar = ttk.Frame(main)
        modebar.pack(fill="x", pady=(0, 12))
        self.mode = tk.StringVar(value="map")  # which action set the table shows: "map" (click) or "hold"
        for key, text in (("map", "When clicked"), ("hold", "When held")):
            ttk.Radiobutton(modebar, text=text, value=key, variable=self.mode, command=self._show_mode,
                            style=_style("Toggle.TButton", "TRadiobutton")).pack(side="left", padx=(0, 6))
        self.mode_hint = tk.StringVar()
        ttk.Label(modebar, textvariable=self.mode_hint, foreground=c["muted"]).pack(side="left", padx=10)
        ttk.Button(modebar, text="Remove layer", command=self.remove_layer).pack(side="right")
        ttk.Button(modebar, text="+ Add layer", command=self.add_layer).pack(side="right", padx=8)
        self.tabs = ttk.Notebook(main)
        self.tabs.pack(fill="both", expand=True)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self._paint())

        footer = ttk.Frame(root, padding=(24, 8, 24, 20))
        footer.pack(fill="x")
        self.status = tk.StringVar(value=f"{engine.PAUSE_HOTKEY} pauses remapping from anywhere. "
                                         "Admin apps need this app run as admin too.")
        ttk.Label(footer, textvariable=self.status, foreground=c["muted"]).pack(side="left")
        ttk.Button(footer, text="Save & apply", style=_style("Accent.TButton", "TButton"),
                   command=self.save).pack(side="right")
        self.unsaved = ttk.Label(footer, text="● Unsaved changes", foreground=c["hot"])

        self._build_tabs()
        self._set_dirty(False)
        for v in (self.toggle, self.speed, self.hold_ms):
            v.trace_add("write", lambda *_: self._set_dirty(True))
        self._make_osd()
        self.tray = self._make_tray()
        if start_hidden and self.tray:
            root.withdraw()
        root.after(50, self._poll)

    # ---- layer tabs -------------------------------------------------------
    def _choices(self, t):
        if t == "launcher":
            return {b["id"]: b["name"] for b in self.bars}
        return CHOICES.get(t)

    def _build_tabs(self):
        for t in self.tabs.tabs():
            self.root.nametowidget(t).destroy()
        self.rows = []  # per layer: (name_var, {"map": {button: Row}, "hold": {button: Row}})
        for i, layer in enumerate(self.config["layers"]):
            f = ttk.Frame(self.tabs, padding=(20, 16))
            name = tk.StringVar(value=layer["name"])
            top = ttk.Frame(f)
            top.grid(row=0, column=0, columnspan=4, sticky="we", pady=(0, 14))
            ttk.Label(top, text="Layer name").pack(side="left")
            ttk.Entry(top, textvariable=name, width=22).pack(side="left", padx=10)
            hint = ("Base layer: what your mouse does normally." if i == 0
                    else "Active after double-clicking the switch button.")
            ttk.Label(top, text=hint, foreground=self.c["muted"]).pack(side="left", padx=6)
            # Click and hold rows share grid cells; the mode switch shows one set at a time.
            sets = {kind: {b: Row(f, r, BUTTON_LABELS[b], layer.get(kind, {}).get(b, {}), self._changed,
                                  self.edit_bars, types, self._choices)
                           for r, b in enumerate(BUTTONS, start=2) if not (kind == "hold" and b in WHEEL)}
                    for kind, types in (("map", TYPES), ("hold", HOLD_TYPES))}
            for rows in sets.values():
                for b, row in rows.items():
                    row.type_box.bind("<FocusIn>", lambda e, b=b: self.mouse.select(b), add="+")
            self.rows.append((name, sets))
            self.tabs.add(f, text=f"  {layer['name']}  ")
            name.trace_add("write", lambda *_, f=f, name=name: (self.tabs.tab(f, text=f"  {name.get() or '?'}  "),
                                                                 self._set_dirty(True)))
        self._show_mode()

    def _show_mode(self):
        mode = self.mode.get()
        for _, sets in self.rows:
            for kind, rows in sets.items():
                for row in rows.values():
                    row.show(kind == mode)
        self.mode_hint.set("What a quick press does." if mode == "map" else
                           f"Starts after holding {self.hold_ms.get()} ms. Buttons with a hold action click on release.")
        self._paint()

    def _current_rows(self):
        try:
            return self.rows[self.tabs.index("current")][1][self.mode.get()]
        except (tk.TclError, IndexError, AttributeError):
            return {}

    def _changed(self):
        self._paint()
        self._set_dirty(True)

    def _set_dirty(self, on):
        self.unsaved.pack(side="right", padx=12) if on else self.unsaved.pack_forget()

    def _paint(self):
        self.mouse.paint({b for b, row in self._current_rows().items() if row.kind() != "default"})

    def _flash_row(self, button):
        row = self._current_rows().get(button)
        if row:
            row.label.configure(foreground=self.c["hot"])
            self.root.after(400, lambda: row.label.configure(foreground=""))
        return row

    def _pick(self, button):
        row = self._flash_row(button)
        self.mouse.select(button)
        if row:
            row.type_box.focus_set()
        when = "clicked" if self.mode.get() == "map" else "held"
        self.status.set(f"{BUTTON_LABELS[button]}: pick what it does when {when} on this layer.")

    def _collect(self):
        """Read the form into a new config. Raises ValueError with a message fit for the user."""
        toggle = _key_of(TOGGLE_LABELS, self.toggle.get())
        layers = []
        for i, (name, sets) in enumerate(self.rows):
            lname = name.get().strip() or f"Layer {i + 1}"
            layer = {"name": lname}
            for kind, rows in sets.items():
                m = {}
                for b, row in rows.items():
                    a = row.action()
                    if a is None:
                        continue
                    where = f"{lname} → {BUTTON_LABELS[b]}" + (" (when held)" if kind == "hold" else "")
                    if a["type"] in ("keys", "media"):
                        try:
                            parse_keys(a["value"])
                        except ValueError as e:
                            raise ValueError(f"{where}: {e}") from None
                    elif a["type"] != "disabled" and not a["value"]:
                        raise ValueError(f"{where}: pick or type a value.")
                    m[b] = a
                layer[kind] = m
            for b, a in layer["map"].items():
                # A bar stays open while the button is down, so it needs a button that reports holds.
                if a["type"] == "launcher" and (b in layer["hold"] or b == toggle or b in WHEEL):
                    raise ValueError(f"{lname} → {BUTTON_LABELS[b]}: a bar opens while you hold the button, "
                                     "so set it under “When held” for this button.")
            layers.append(layer)
        try:
            speed, hold = int(self.speed.get()), int(self.hold_ms.get())
        except ValueError:
            raise ValueError("Double-click speed and hold time must be numbers.") from None
        return {"toggle_button": toggle, "double_click_ms": max(150, min(900, speed)),
                "hold_ms": max(150, min(1500, hold)), "layers": layers, "bars": copy.deepcopy(self.bars)}

    def _try_collect(self):
        try:
            return self._collect()
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self.root)
            return None

    def _rebuild(self, cfg, select=None):
        tab = self.tabs.index("current") if select is None else select
        self.config = cfg
        self._build_tabs()
        self.tabs.select(min(tab, len(cfg["layers"]) - 1))

    def add_layer(self):
        cfg = self._try_collect()
        if cfg:
            cfg["layers"].append({"name": f"Layer {len(cfg['layers']) + 1}", "map": {}, "hold": {}})
            self._rebuild(cfg, len(cfg["layers"]) - 1)
            self._set_dirty(True)
            self.status.set("Layer added. Press Save & apply to use it.")

    def remove_layer(self):
        i = self.tabs.index("current")
        if i == 0:
            messagebox.showinfo(APP_NAME, "The base layer can't be removed.", parent=self.root)
            return
        cfg = self._try_collect()
        if cfg:
            del cfg["layers"][i]
            self._rebuild(cfg, 0)
            self._set_dirty(True)
            self.status.set("Layer removed. Press Save & apply to keep it that way.")

    def save(self):
        cfg = self._try_collect()
        if not cfg:
            return
        base = cfg["layers"][0]
        if (base["map"].get("left") or base["hold"].get("left")) and not messagebox.askyesno(
                "Change left click?",
                "You're changing Left click on the base layer.\n\nIf that goes wrong, "
                f"{self.engine.PAUSE_HOTKEY} pauses all remapping.\n\nContinue?", parent=self.root):
            return
        try:
            save_config(self.path, cfg)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't save settings: {e}", parent=self.root)
            return
        self.config = cfg
        self.engine.load(cfg)
        self._sync_bars(cfg["bars"])
        self._set_dirty(False)
        self.status.set("Saved and applied.")

    # ---- pop-up bars --------------------------------------------------------
    def _sync_bars(self, bars):
        """One pop-up window per saved bar, keyed by bar id."""
        for bar in bars:
            if bar["id"] not in self.launchers:
                self.launchers[bar["id"]] = Launcher(self.root, self.engine.run)
            self.launchers[bar["id"]].configure(bar["items"], bar["height"])
        self.live_bars = [b["id"] for b in bars]

    def edit_bars(self):
        cfg = self._try_collect()  # rows show bar names, so rebuild them afterwards in case bars were renamed
        if not cfg:
            return
        edit_bars(self.root, self.bars, self._preview_bar)
        cfg["bars"] = copy.deepcopy(self.bars)
        self._rebuild(cfg)
        self._set_dirty(True)
        self.status.set("Press Save & apply to use the updated bars.")

    def _preview_bar(self, bar, at):
        if not self._preview:  # separate from the live bars, and it never runs anything
            self._preview = Launcher(self.root, lambda action: None)
        self._preview.configure(bar["items"], bar["height"])
        self._preview.preview(at)

    def _set_autostart(self):
        try:
            _autostart_set(self.autostart.get())
        except OSError as e:
            self.autostart.set(not self.autostart.get())
            messagebox.showerror(APP_NAME, f"Couldn't change startup setting: {e}", parent=self.root)

    # ---- events from the engine / tray ------------------------------------
    def _poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "layer":
                layers = self.engine.rm.config["layers"]
                self.osd(layers[value]["name"], f"Layer {value + 1} of {len(layers)}")
                self.status.set(f"Active layer: {layers[value]['name']}")
            elif kind == "enabled":
                self.enabled.set(value)
                self.osd("Remapping on" if value else "Remapping paused", self.engine.PAUSE_HOTKEY)
            elif kind == "launcher":
                which, bar_id = value
                if bar_id not in self.live_bars:  # deleted bar or old config: use the first one
                    bar_id = self.live_bars[0] if self.live_bars else None
                bar = self.launchers.get(bar_id)
                if bar and which == "down":
                    bar.press()
                elif bar and which == "up":
                    bar.release()
            elif kind == "press":
                self.mouse.flash(value)
                self._flash_row(value)
                self.status.set(f"You pressed: {BUTTON_LABELS[value]}")
            elif kind == "error":
                messagebox.showwarning(APP_NAME, value, parent=self.root)
            elif kind == "show":
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif kind == "toggle_enabled":
                self.engine.set_enabled(not self.engine.rm.enabled)
            elif kind == "quit":
                self.quit()
                return
        self.root.after(15, self._poll)  # short, so bars open without a noticeable lag

    def _make_osd(self):
        # Shown once off-screen and only moved afterwards: showing a new window could steal focus from a game.
        bg = "#202020"
        self._osd = tk.Toplevel(self.root, bg=bg)
        self._osd.overrideredirect(True)
        self._osd.attributes("-topmost", True, "-alpha", 0.94)
        self._osd_title = tk.Label(self._osd, font=("Segoe UI Semibold", 18), fg="white", bg=bg)
        self._osd_title.pack(padx=36, pady=(14, 0))
        self._osd_sub = tk.Label(self._osd, font=("Segoe UI", 10), fg=PALETTE[True]["hot"], bg=bg)
        self._osd_sub.pack(padx=36, pady=(0, 14))
        self._osd.geometry("+-10000+-10000")
        _dwm(self._osd, 33, 2)  # rounded corners on Windows 11
        self._osd_hide = None

    def osd(self, title, sub=""):
        self._osd_title.configure(text=title)
        self._osd_sub.configure(text=sub)
        self._osd.update_idletasks()
        x = (self._osd.winfo_screenwidth() - self._osd.winfo_reqwidth()) // 2
        y = self._osd.winfo_screenheight() - self._osd.winfo_reqheight() - 140
        self._osd.geometry(f"+{x}+{y}")
        if self._osd_hide:
            self.root.after_cancel(self._osd_hide)
        self._osd_hide = self.root.after(1400, lambda: self._osd.geometry("+-10000+-10000"))

    def _make_tray(self):
        if not pystray:
            return None

        def put(kind):
            return lambda: self.events.put((kind, None))
        menu = pystray.Menu(
            pystray.MenuItem("Open settings", put("show"), default=True),
            pystray.MenuItem("Remapping on", put("toggle_enabled"), checked=lambda _: self.engine.rm.enabled),
            pystray.MenuItem("Quit", put("quit")))
        icon = pystray.Icon("Layerhop", _icon_image(), APP_NAME, menu)
        icon.run_detached()
        return icon

    def close(self):
        if self.tray:
            self.root.withdraw()
            self.osd("Still running", "Find Layerhop in the system tray")
        else:
            self.quit()

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()
