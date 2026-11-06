"""Pure remapping logic: physical mouse events in, actions out. No Windows calls, so it's testable anywhere."""
import copy
import json
import os

BUTTONS = ["left", "right", "middle", "x1", "x2", "wheel_up", "wheel_down"]
WHEEL = {"wheel_up", "wheel_down"}

MEDIA_BAR = {"id": "media", "name": "Media", "height": 76, "items": [
    {"name": "Previous", "action": {"type": "media", "value": "prev_track"}},
    {"name": "Play / Pause", "action": {"type": "media", "value": "play_pause"}},
    {"name": "Next", "action": {"type": "media", "value": "next_track"}},
    {"name": "Volume down", "action": {"type": "media", "value": "volume_down"}},
    {"name": "Volume up", "action": {"type": "media", "value": "volume_up"}},
    {"name": "Mute", "action": {"type": "media", "value": "volume_mute"}},
]}

DEFAULT_CONFIG = {
    "toggle_button": "middle",
    "double_click_ms": 300,
    "hold_ms": 300,  # how long a press counts as a click before the hold action starts
    # Each layer: "map" = click actions, "hold" = hold actions, keyed by button.
    "layers": [{"name": "Normal", "map": {}, "hold": {}}, {"name": "Layer 2", "map": {}, "hold": {}}],
    # Pop-up bars opened by holding a button ({"type": "launcher", "value": bar id}). Each item runs an action.
    "bars": [{"id": "programs", "name": "Programs", "height": 76, "items": []}, MEDIA_BAR],
}


def migrate(cfg):
    """Older configs had a single launcher of programs; it becomes the first bar, plus a Media bar."""
    if "bars" not in cfg and "launcher" in cfg:
        cfg["bars"] = [{"id": "programs", "name": "Programs", "height": cfg.get("launcher_height", 76),
                        "items": [{"name": it["name"], "action": {"type": "run", "value": it["path"]}}
                                  for it in cfg["launcher"]]},
                       copy.deepcopy(MEDIA_BAR)]
    cfg.pop("launcher", None)
    cfg.pop("launcher_height", None)
    return cfg


def load_config(path):
    """Returns (config, warning). A damaged file is kept as .bad instead of being overwritten."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_CONFIG), None
    except ValueError as e:
        os.replace(path, path + ".bad")
        return copy.deepcopy(DEFAULT_CONFIG), f"Settings file was damaged ({e}). It was kept as config.json.bad and defaults were loaded."
    migrate(cfg)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, copy.deepcopy(v))
    if not cfg["layers"]:
        cfg["layers"] = copy.deepcopy(DEFAULT_CONFIG["layers"])
    return cfg, None


def save_config(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


class Remapper:
    """Turns button events into actions. Outputs are (kind, arg):
    ("down"|"up"|"click", action_dict) or ("layer", new_layer_index).

    A button with a hold action (and the layer-switch button) waits when pressed: let go early and it does
    its click action, keep holding and it starts the hold action. Every other button acts immediately."""

    def __init__(self, config):
        self.layer = 0
        self.enabled = True
        self.held = {}  # button -> action it started (None = passed through), so release matches press
        self.pending = {}  # button -> [state, deadline]; state: "down1" | "up1" (switch button only) | "down2"
        self.load(config)

    @property
    def deadline(self):
        """When the earliest click-or-hold decision is due, or None."""
        due = [d for _, d in self.pending.values() if d is not None]
        return min(due) if due else None

    def load(self, config):
        out = self.release_all()
        self.config = config
        self.layer = min(self.layer, len(config["layers"]) - 1)
        return out

    def set_enabled(self, on):
        self.enabled = on
        return [] if on else self.release_all()

    def release_all(self):
        out = [("up", a) for a in self.held.values() if a]
        self.held.clear()
        self.pending.clear()
        return out

    def _get(self, kind, button):
        a = self.config["layers"][self.layer].get(kind, {}).get(button)
        return None if not a or a.get("type") == "default" else a

    def action(self, button):
        """Click action for button on the current layer; None = leave the click alone."""
        return self._get("map", button)

    def hold_action(self, button):
        return self._get("hold", button)

    def _click(self, button):
        # The original press was swallowed, so "no change" has to be replayed as the same button.
        return self.action(button) or {"type": "button", "value": button}

    def on_event(self, button, down, now):
        """Returns (suppress_original, outputs)."""
        if not self.enabled:
            return False, []
        if button in WHEEL:
            a = self.action(button)
            return (True, [("click", a)]) if a else (False, [])
        toggle = button == self.config.get("toggle_button")
        p = self.pending.get(button)
        if down:
            if p and p[0] == "up1":  # second press in time on the switch button: change layer
                self.layer = (self.layer + 1) % len(self.config["layers"])
                self.pending[button] = ["down2", None]
                return True, [("layer", self.layer)]
            if toggle or self.hold_action(button):
                ms = self.config["double_click_ms"] if toggle else self.config.get("hold_ms", 300)
                self.pending[button] = ["down1", now + ms / 1000]
                return True, []
            a = self.held[button] = self.action(button)
            return (True, [("down", a)]) if a else (False, [])
        if p:
            if p[0] == "down1" and toggle:  # wait: this may become a double-click
                p[0] = "up1"
                return True, []
            del self.pending[button]
            return True, [("click", self._click(button))] if p[0] == "down1" else []
        if button not in self.held:  # pressed before we started; let the release through
            return False, []
        a = self.held.pop(button)
        return (True, [("up", a)]) if a else (False, [])

    def tick(self, now):
        """Resolve waiting buttons whose click/hold/double-click window has passed."""
        out = []
        for b, (state, due) in list(self.pending.items()):
            if due is None or now < due:
                continue
            del self.pending[b]
            if state == "up1":
                out.append(("click", self._click(b)))
            else:  # still held: start the hold action now, release it with the button
                a = self.held[b] = self.hold_action(b) or self._click(b)
                out.append(("down", a))
        return out
