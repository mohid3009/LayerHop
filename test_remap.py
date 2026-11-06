"""Run: python test_remap.py"""
from remap import Remapper

KEY_C = {"type": "keys", "value": "ctrl+c"}
MIDDLE = {"type": "button", "value": "middle"}


def cfg(**layer2):
    return {"toggle_button": "middle", "double_click_ms": 300,
            "layers": [{"name": "A", "map": {}}, {"name": "B", "map": layer2}]}


def double_click(r, t):
    r.on_event("middle", True, t)
    r.on_event("middle", False, t + 0.05)
    r.on_event("middle", True, t + 0.1)
    r.on_event("middle", False, t + 0.15)


def test_base_layer_passes_through():
    r = Remapper(cfg(x1=KEY_C))
    assert r.on_event("x1", True, 0) == (False, [])
    assert r.on_event("x1", False, 0.1) == (False, [])


def test_double_click_switches_layer_and_back():
    r = Remapper(cfg(x1=KEY_C))
    r.on_event("middle", True, 0)
    r.on_event("middle", False, 0.05)
    assert r.on_event("middle", True, 0.1) == (True, [("layer", 1)])
    assert r.on_event("middle", False, 0.15) == (True, [])
    assert r.tick(1) == []  # no stray click after a double-click
    assert r.on_event("x1", True, 1) == (True, [("down", KEY_C)])
    assert r.on_event("x1", False, 1.1) == (True, [("up", KEY_C)])
    double_click(r, 2)
    assert r.layer == 0
    assert r.on_event("x1", True, 3) == (False, [])


def test_single_click_waits_then_clicks():
    r = Remapper(cfg())
    assert r.on_event("middle", True, 0) == (True, [])
    r.on_event("middle", False, 0.05)
    assert r.tick(0.2) == []
    assert r.tick(0.31) == [("click", MIDDLE)]


def test_holding_toggle_button_still_works_as_button():
    r = Remapper(cfg())
    r.on_event("middle", True, 0)
    assert r.tick(0.31) == [("down", MIDDLE)]
    assert r.on_event("middle", False, 2) == (True, [("up", MIDDLE)])


def test_release_after_layer_switch_releases_original_action():
    r = Remapper(cfg(x1=KEY_C))
    r.layer = 1
    r.on_event("x1", True, 0)
    double_click(r, 0.1)  # back to layer A while x1 is still held
    assert r.on_event("x1", False, 1) == (True, [("up", KEY_C)])


def test_wheel_and_pause():
    up = {"type": "media", "value": "volume_up"}
    r = Remapper(cfg(wheel_up=up, x1=KEY_C))
    r.layer = 1
    assert r.on_event("wheel_up", True, 0) == (True, [("click", up)])
    r.on_event("x1", True, 0)
    assert r.set_enabled(False) == [("up", KEY_C)]  # pausing never leaves keys stuck
    assert r.on_event("x1", False, 1) == (False, [])
    assert r.on_event("wheel_up", True, 1) == (False, [])


def test_click_or_hold():
    paste = {"type": "keys", "value": "ctrl+v"}
    c = cfg(x1=KEY_C)
    c["hold_ms"] = 300
    c["layers"][1]["hold"] = {"x1": paste}
    r = Remapper(c)
    r.layer = 1
    assert r.on_event("x1", True, 0) == (True, [])  # waits to see which it is
    assert r.on_event("x1", False, 0.1) == (True, [("click", KEY_C)])  # quick press: click action
    assert r.tick(1) == []
    r.on_event("x1", True, 2)
    assert r.tick(2.2) == []
    assert r.tick(2.31) == [("down", paste)]  # still held: hold action starts...
    assert r.on_event("x1", False, 3) == (True, [("up", paste)])  # ...and ends on release
    r.layer = 0  # no hold action here, so no waiting
    assert r.on_event("x1", True, 4) == (False, [])


def test_switch_button_hold_action():
    launch = {"type": "launcher", "value": ""}
    c = cfg()
    c["layers"][0]["hold"] = {"middle": launch}
    r = Remapper(c)
    r.on_event("middle", True, 0)
    assert r.tick(0.31) == [("down", launch)]
    assert r.on_event("middle", False, 1) == (True, [("up", launch)])
    double_click(r, 2)  # double-click still switches layers
    assert r.layer == 1


def test_old_launcher_becomes_bars():
    from remap import migrate
    old = {"launcher": [{"name": "Notepad", "path": "C:\\notepad.exe"}], "launcher_height": 90, "layers": []}
    bars = migrate(old)["bars"]
    assert "launcher" not in old and "launcher_height" not in old
    assert bars[0] == {"id": "programs", "name": "Programs", "height": 90,
                       "items": [{"name": "Notepad", "action": {"type": "run", "value": "C:\\notepad.exe"}}]}
    assert bars[1]["id"] == "media"
    assert migrate({"bars": []}) == {"bars": []}  # already new: untouched


def test_launcher_pick():
    from launcher import pick
    centers = [100, 170, 240]
    assert pick(165, 60, centers, 50, 70, 140) == 1
    assert pick(35, 50, centers, 50, 70, 140) == 0  # a little past the end still counts
    assert pick(500, 60, centers, 50, 70, 140) is None  # off to the side: cancel
    assert pick(170, 400, centers, 50, 70, 140) is None  # dragged far away: cancel
    assert pick(170, 50, [], 50, 70, 140) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
