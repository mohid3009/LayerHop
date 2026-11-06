"""Layerhop: remap mouse buttons; double-click a button to switch every button to another layer.
Run: python main.py   (add --tray to start hidden in the tray)"""
import ctypes
import os
import queue
import shutil
import sys
import tkinter as tk
from tkinter import messagebox

from gui import APP_NAME, App
from hook import Engine
from remap import load_config

APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
CONFIG = os.path.join(APPDATA, "Layerhop", "config.json")
OLD_CONFIG = os.path.join(APPDATA, "MouseLayers", "config.json")  # where earlier versions saved it


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    # Two copies would both hook the mouse and fight. The mutex lives until this process exits.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, "LayerhopSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        root.withdraw()
        messagebox.showinfo(APP_NAME, f"{APP_NAME} is already running. Look for it in the system tray.")
        return
    if not os.path.exists(CONFIG) and os.path.exists(OLD_CONFIG):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        shutil.copyfile(OLD_CONFIG, CONFIG)
    config, warning = load_config(CONFIG)
    events = queue.Queue()
    engine = Engine(config, lambda kind, value=None: events.put((kind, value)))
    try:
        engine.start()
    except OSError as e:
        root.withdraw()
        messagebox.showerror(APP_NAME, f"Couldn't hook the mouse: {e}")
        return
    App(root, engine, config, CONFIG, events, start_hidden="--tray" in sys.argv)
    if warning:
        events.put(("error", warning))
    root.mainloop()


if __name__ == "__main__":
    main()
