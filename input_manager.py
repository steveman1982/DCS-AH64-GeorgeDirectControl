"""
Reads game controllers through pygame and reports button/hat/axis changes as tokens.
Keyboard tokens are built from tkinter key events (see key_token).

Axes act as buttons: each axis has two virtual buttons, "<axis>:+" and "<axis>:-",
measured as travel from the axis' rest position towards its end stop (0..1).
That makes triggers resting at -1, centred sticks and single-axis pedals behave the same.
"""
import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
import pygame

_MOD_NAMES = {
    "Control_L": "LCtrl", "Control_R": "RCtrl",
    "Shift_L": "LShift", "Shift_R": "RShift",
    "Alt_L": "LAlt", "Alt_R": "RAlt",
    "Super_L": "LWin", "Super_R": "RWin",
    "Win_L": "LWin", "Win_R": "RWin",
}


def key_token(keysym, keycode):
    """Token for a keyboard key. Modifiers keep L/R distinction via keysym;
    other keys use the keycode so Shift doesn't change their identity."""
    if keysym in _MOD_NAMES:
        code, name = keysym, _MOD_NAMES[keysym]
    else:
        code = int(keycode)
        name = keysym.upper() if len(keysym) == 1 else keysym
    return {"guid": "keyboard", "device": "Keyboard", "type": "key",
            "code": code, "name": name}


def axis_travel(value, rest, direction):
    """Fraction (0..1) of travel from rest towards the +1 or -1 end stop."""
    end = 1.0 if direction == "+" else -1.0
    span = end - rest
    if abs(span) < 0.05:          # already resting at that end: no travel possible
        return 0.0
    return max(0.0, min(1.0, (value - rest) / span))


def axis_token_active(tok, value, was_active, hysteresis=0.05):
    """True if an axis token is 'pressed' for this raw value (with hysteresis)."""
    _, d = tok["code"].split(":")
    f = axis_travel(value, tok.get("rest", 0.0), d)
    thr = tok.get("threshold", 0.5)
    return f >= thr or (was_active and f >= thr - hysteresis)


def _hat_dirs(value):
    x, y = value
    dirs = set()
    if y == 1: dirs.add("up")
    if y == -1: dirs.add("down")
    if x == -1: dirs.add("left")
    if x == 1: dirs.add("right")
    return dirs


class Device:
    def __init__(self, joy, guid):
        self.joy = joy
        self.guid = guid
        self.name = joy.get_name()
        self.axes = joy.get_numaxes()
        self.buttons = joy.get_numbuttons()
        self.hats = joy.get_numhats()
        self.state = set()  # currently active (kind, code)
        self.rest = [0.0] * self.axes
        self.snapshot_rest()

    def snapshot_rest(self):
        self.rest = self.axis_values()

    def axis_values(self):
        return [self.joy.get_axis(i) for i in range(self.axes)]

    def short_name(self, n=18):
        return self.name if len(self.name) <= n else self.name[:n - 1] + "…"

    def token(self, kind, code, default_threshold=0.5):
        tok = {"guid": self.guid, "device": self.name, "type": kind, "code": code}
        if kind == "button":
            tok["name"] = f"{self.short_name()} Btn{code + 1}"
        elif kind == "hat":
            hat, d = code.split(":")
            tok["name"] = f"{self.short_name()} Hat{hat} {d}"
        else:
            i, d = code.split(":")
            tok["name"] = f"{self.short_name()} Axis{i} {d}"
            tok["rest"] = round(self.rest[int(i)], 4)
            tok["threshold"] = default_threshold
        return tok

    def read(self, use_axes, detect, hysteresis):
        active = set()
        for i in range(self.buttons):
            if self.joy.get_button(i):
                active.add(("button", i))
        for h in range(self.hats):
            for d in _hat_dirs(self.joy.get_hat(h)):
                active.add(("hat", f"{h}:{d}"))
        if use_axes:
            for i, v in enumerate(self.axis_values()):
                for d in "+-":
                    key = ("axis", f"{i}:{d}")
                    f = axis_travel(v, self.rest[i], d)
                    if f >= detect or (key in self.state and f >= detect - hysteresis):
                        active.add(key)
        return active


class InputManager:
    def __init__(self, axis_detect=0.5, axis_hysteresis=0.05, axis_default_threshold=0.5):
        pygame.display.init()   # needed for the event queue; no window is opened
        pygame.joystick.init()
        self.axis_detect = axis_detect
        self.axis_hysteresis = axis_hysteresis
        self.axis_default_threshold = axis_default_threshold
        self.devices = {}       # guid -> Device
        self.scan()

    def scan(self):
        self.devices.clear()
        seen = {}
        for i in range(pygame.joystick.get_count()):
            joy = pygame.joystick.Joystick(i)
            joy.init()
            guid = joy.get_guid()
            seen[guid] = seen.get(guid, 0) + 1
            if seen[guid] > 1:          # two identical devices share a GUID
                guid = f"{guid}#{seen[guid]}"
            self.devices[guid] = Device(joy, guid)
        pygame.event.pump()             # get real axis values before taking rest positions
        self.snapshot_rest()

    def snapshot_rest(self):
        """Take current axis positions as rest. Call with hands off the controls."""
        pygame.event.pump()
        for dev in self.devices.values():
            dev.snapshot_rest()
            dev.state = {k for k in dev.state if k[0] != "axis"}

    def poll(self, axes_enabled=lambda guid: True):
        """Returns (changes, rescanned). changes = list of (token, pressed)."""
        rescanned = False
        for e in pygame.event.get():
            if e.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                rescanned = True
        if rescanned:
            self.scan()
            return [], True

        changes = []
        for dev in self.devices.values():
            now = dev.read(axes_enabled(dev.guid), self.axis_detect, self.axis_hysteresis)
            for kind, code in now - dev.state:
                changes.append((dev.token(kind, code, self.axis_default_threshold), True))
            for kind, code in dev.state - now:
                changes.append((dev.token(kind, code, self.axis_default_threshold), False))
            dev.state = now
        return changes, False

    def close(self):
        pygame.quit()
