"""
Config storage for the George commands application.

Everything lives in one JSON file (config.json by default):
  settings  - general settings (GUI role, bridge address, timing, ...)
  devices   - known input devices and whether they're enabled
  commands  - the George command catalog, per role, grouped. Command ids are
              GeorgeBridge.lua command names (sent as "cmd <id>"), or names
              from "virtual" (resolved against the sim at the moment of use).
  virtual   - mode/seat-aware commands, e.g. "climb" -> hover_up_10ft in H-B,
              increase_altitude in FLT
  state     - GUI state per role (active group, selected command per group)
  bindings  - role -> command id -> list of bindings; each binding is a list of
              input tokens (one key/button, or a combination). Any of a
              command's bindings triggers it.

Roles:
  gunner - George is the CP/G, you fly from the PILOT seat (bridge seat=pilot)
  pilot  - George flies, you sit in the CP/G seat (bridge seat=cpg)

An input token is a dict:
  {"guid": "...", "device": "...", "type": "button"|"hat"|"key"|"axis", "code": ..., "name": "..."}
Axis tokens ("code" = "<axis>:+" or "<axis>:-") also carry:
  "rest"      - raw axis value at rest when bound (-1..1)
  "threshold" - trigger point as a fraction of travel from rest to the end stop (0..1)
A binding fires when all of its tokens are active, in any order.

Command items may carry "hold_action": what happens while the binding is held.
  once   - fire one command per press (default)
  repeat - fire again each time the bridge finishes, until released
  ramp   - like repeat, but each step holds George's button longer (bigger
           steps); for rate-based commands such as altitude and course
  hold   - George's button goes down when you press and comes up when you let
           go (H-B moves, altitude, course). For tap commands (H-B 10 ft steps,
           speed) the bridge pulses the button itself while you hold, so the
           steps follow each other without a round trip each
"""
import copy
import json
import os
import sys
import tempfile


def app_dir():
    """Folder that holds config.json: next to the .exe when built with
    PyInstaller (sys.frozen), next to the scripts otherwise."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_config_path():
    return os.path.join(app_dir(), "config.json")

CONFIG_VERSION = 11
ROLES = ("gunner", "pilot")
ROLE_LABELS = {
    "gunner": "George as Gunner (you are the pilot)",
    "pilot": "George as Pilot (you are the CP/G)",
}
SEAT_TO_ROLE = {"pilot": "gunner", "cpg": "pilot"}
HOLD_ACTIONS = ("once", "repeat", "ramp", "hold")

# Commands that raise George's wheel as feedback while held: the ones that
# change altitude, speed or direction. Stored per command as "show_wheel".
WHEEL_FEEDBACK = {"climb", "descend", "increase_altitude", "decrease_altitude",
                  "hover_up_10ft", "hover_down_10ft", "speed_up", "slow_down",
                  "come_left", "come_right"}

# Bridge commands that press exactly the same wheel slot. George shows one label
# or the other there depending on context, so one binding covers both: the
# second id is folded into the first. (The APU and DEFN CMWS pairs also share a
# slot but are kept apart: the bridge reads a cockpit switch first and only
# presses when the switch isn't already where that command wants it.)
MERGED = {
    "pilot": {
        "align_to_nts": ("align_to_tads", "Align to TADS / NTS (whichever is shown)"),
        "return_to_battle_position": ("hold_position", "Hold position / Return to battle position"),
        "set_groundspeed_ref": ("set_airspeed_ref", "Set airspeed / groundspeed reference"),
        "set_barometric_altitude": ("set_radar_altitude", "Set radar / barometric altitude hold"),
        "shutdown_engines": ("apu_only", "APU only / Shut down the engines"),
    },
}


def _items(*rows):
    out = []
    for row in rows:
        cid, name = row[0], row[1]
        item = {"id": cid, "name": name}
        if len(row) > 2:
            item["hold_action"] = row[2]
        if cid in WHEEL_FEEDBACK:
            item["show_wheel"] = True
        out.append(item)
    return out


_RAW_WHEEL = _items(
    ("show_hide", "Show/hide the George overlay"),
    ("up", "Wheel up"), ("down", "Wheel down"),
    ("left", "Wheel left (cycles mode)"), ("right", "Wheel right"),
    ("center", "Wheel select (centre)"),
    ("up_long", "Wheel up, held"), ("down_long", "Wheel down, held"),
    ("left_long", "Wheel left, held"), ("right_long", "Wheel right, held"),
    ("center_long", "Wheel select, held"),
    ("control_request", "Request control"),
    ("store_target", "Store target"),
)

_CMWS_PANEL = _items(
    ("cmws_on", "CMWS power ON"), ("cmws_off", "CMWS power OFF"),
    ("cmws_arm", "CMWS flare squibs ARM"), ("cmws_safe", "CMWS flare squibs SAFE"),
    ("cmws_auto", "CMWS AUTO"), ("cmws_bypass", "CMWS BYPASS"),
)

DEFAULT_COMMANDS = {
    "gunner": [
        {"group": "Search & sensor", "items": _items(
            ("cpg_startup", "George start-up"),
            ("cpg_shutdown", "George shutdown"),
            ("cpg_phs_search", "PHS search (where you look)"),
            ("cpg_area_search", "Area search"),
            ("cpg_point_search", "Point search"),
            ("cpg_area_select", "Area select"),
            ("cpg_point_select", "Point select"),
            ("cpg_tads_los", "TADS line of sight"),
            ("cpg_tads_sensor", "Cycle TADS sensor"),
            ("cpg_tads_fov", "Cycle TADS field of view"),
            ("cpg_tads_zoom_in", "TADS zoom in", "repeat"),
            ("cpg_tads_zoom_out", "TADS zoom out", "repeat"),
            ("cpg_adjust_aim", "Adjust aim"),
        )},
        {"group": "Targets & lists", "items": _items(
            ("cpg_next_target", "Next target"),
            ("cpg_previous_target", "Previous target"),
            ("cpg_select_target", "Select target"),
            ("cpg_track_target", "Track target"),
            ("cpg_last_found_target", "Last found target"),
            ("cpg_last_stored_target", "Last stored target"),
            ("cpg_next_item", "Next list item"),
            ("cpg_previous_item", "Previous list item"),
            ("cpg_list_item_select", "Select list item"),
            ("cpg_exit_list", "Exit list"),
            ("cpg_target_list_zoom_in", "Target list zoom in"),
            ("cpg_target_list_zoom_out", "Target list zoom out"),
            ("cpg_target_list_filter", "Target list filter"),
            ("cpg_point_list_filter_mode", "Point list filter mode"),
            ("cpg_point_list_filter_threat", "Point list filter threat"),
        )},
        {"group": "Laser & fire", "items": _items(
            ("cpg_lase_target", "Lase target"),
            ("cpg_laser_on", "Laser on"),
            ("cpg_laser_off", "Laser off"),
            ("cpg_cleared_fire", "Cleared to fire"),
            ("cpg_weapons_free", "Weapons free"),
            ("cpg_hold_fire", "Hold fire"),
        )},
        {"group": "Weapons", "items": _items(
            ("cpg_next_weapon", "Next weapon"),
            ("cpg_next_missile", "Next missile type"),
            ("cpg_next_rocket", "Next rocket type"),
            ("cpg_missile_traj", "Missile trajectory"),
            ("cpg_lobl", "LOBL"),
            ("cpg_loal", "LOAL"),
            ("cpg_burst_limit", "Gun burst limit"),
            ("cpg_rocket_quantity", "Rocket quantity"),
        )},
        {"group": "Macros", "items": _items(
            ("cpg_macro_track_engage", "Track and engage current target"),
            ("cpg_macro_add_two_targets", "Add and track the top 2 targets"),
            ("cpg_macro_add_three_targets", "Add and track the top 3 targets"),
            ("cpg_macro_add_four_targets", "Add and track the top 4 targets"),
            ("cpg_macro_next_search", "Next area search"),
            ("cpg_macro_previous_search", "Previous area search"),
            ("cpg_macro_next_point", "Next point search"),
            ("cpg_macro_previous_point", "Previous point search"),
        )},
        {"group": "CMWS panel (direct)", "items": copy.deepcopy(_CMWS_PANEL)},
        {"group": "Wheel (raw)", "items": copy.deepcopy(_RAW_WHEEL)},
    ],
    "pilot": [
        {"group": "Smart (mode-aware)", "items": _items(
            ("climb", "Climb: one step, more while held", "ramp"),
            ("descend", "Descend: one step, more while held", "ramp"),
        )},
        {"group": "Wheel modes", "items": _items(
            ("mode_ground", "Select GND mode"),
            ("mode_hover", "Select H-B mode"),
            ("mode_flight", "Select FLT mode"),
            ("mode_combat", "Select CMBT mode"),
            ("mode_next", "Step to the next wheel mode"),
            ("mode_defense", "Open the DEFN submenu"),
        )},
        {"group": "GND", "items": _items(
            ("startup_full", "Full start-up"),
            ("shutdown_full", "Full shutdown"),
            ("apu_start", "Start the APU"),
            ("apu_stop", "Stop the APU"),
            ("apu_only", "APU only / Shut down the engines"),
            ("start_engines_idle", "Start engines to idle"),
            ("start_engines_fly", "Start engines to fly"),
            ("cmws_power_toggle_george", "Toggle CMWS power via George"),
            ("takeoff", "Take off"),
        )},
        {"group": "FLT", "items": _items(
            ("speed_up", "Speed up", "ramp"),
            ("slow_down", "Slow down", "ramp"),
            ("increase_altitude", "Increase altitude: one step, more while held", "ramp"),
            ("decrease_altitude", "Decrease altitude: one step, more while held", "ramp"),
            ("come_left", "Come left", "ramp"),
            ("come_right", "Come right", "ramp"),
            ("follow_waypoints", "Follow waypoints"),
            ("set_airspeed_ref", "Set airspeed / groundspeed reference"),
            ("set_radar_altitude", "Set radar / barometric altitude hold"),
        )},
        {"group": "CMBT", "items": _items(
            ("hold_position", "Hold position / Return to battle position"),
            ("battle_position_toggle", "Add or delete a battle position"),
            ("mask_position", "Mask position"),
            ("align_to_tads", "Align to TADS / NTS (whichever is shown)"),
            ("turn_to_ghs", "Turn to GHS"),
            ("orbit_overhead", "Orbit overhead"),
            ("break180", "Break 180"),
            ("break_left", "Break left"),
            ("break_right", "Break right"),
        )},
        {"group": "H-B (hover)", "items": _items(
            ("hover_up_10ft", "Come up: one step, more while held", "ramp"),
            ("hover_down_10ft", "Come down: one step, more while held", "ramp"),
            ("hover_forward", "Move forwards", "hold"),
            ("hover_back", "Move back", "hold"),
            ("hover_left", "Move left", "hold"),
            ("hover_right", "Move right", "hold"),
        )},
        {"group": "DEFN", "items": _items(
            ("cmws_arm_george", "CMWS arm, via George"),
            ("cmws_safe_george", "CMWS safe, via George"),
            ("cmws_auto_george", "CMWS auto, via George"),
            ("cmws_bypass_george", "CMWS bypass, via George"),
            ("threat_warnings_toggle", "Toggle threat warnings"),
            ("roe_next", "Next rules of engagement"),
            ("evade_next", "Next evade mode"),
            ("dispense_next", "Next dispense mode"),
            ("extlights_next", "Next exterior light setting"),
        )},
        {"group": "CMWS panel (direct)", "items": copy.deepcopy(_CMWS_PANEL)},
        {"group": "Wheel (raw)", "items": copy.deepcopy(_RAW_WHEEL)},
    ],
}

# A value may be a list: candidates tried in order until one's mode is offered.
# From CMBT/GND/DEFN, altitude goes through FLT (offered at any speed); the H-B
# 10 ft step is the fallback for when FLT isn't on the wheel.
DEFAULT_VIRTUAL = {
    "climb": {"by": "mode", "HOV": "hover_up_10ft", "FLT": "increase_altitude",
              "default": ["increase_altitude", "hover_up_10ft"]},
    "descend": {"by": "mode", "HOV": "hover_down_10ft", "FLT": "decrease_altitude",
                "default": ["decrease_altitude", "hover_down_10ft"]},
}

DEFAULT_SETTINGS = {
    "role": "gunner",               # role shown in the GUI
    "runtime_role": "auto",         # controller: auto (from DCS seat), gunner or pilot
    "poll_interval_ms": 16,
    "release_debounce_ms": 80,
    "axis_detect": 0.5,             # travel needed to register an axis while binding
    "axis_default_threshold": 0.5,  # initial trigger point for a new axis binding
    "axis_hysteresis": 0.05,        # must fall this far below the trigger point to release
    "bridge_host": "127.0.0.1",
    "bridge_port": 7788,
    "reply_timeout_s": 8.0,
    "state_poll_s": 2.0,            # how often the controller asks DCS which seat you're in
    "controller_poll_ms": 10,
    "chord_window_ms": 50,
    "input_debounce_ms": 60,        # a binding must stay released this long to count as released          # inputs landing within this window count as one combination
    "repeat_gap_ms": 250,           # pause between repeats of a held "repeat" command
    "repeat_first_delay_ms": 700,   # tap commands (speed): held this long before repeating starts
    "wheel_min_visible_ms": 1000,   # once raised, the wheel stays up at least this long
    "ramp_first_ms": 700,           # first ramp step: ~10 ft / ~2 degrees
    "ramp_step_ms": 200,            # each further step holds this much longer (~+10 ft)
    "ramp_max_ms": 1990,            # bridge limit: under the 2.0 s high-rate cliff
    "ramp_max_stages": 40,          # steps per hold before it stops by itself
    "ramp_show_wheel": True,        # raise George's wheel as feedback while ramping
    "max_queue_wait_ms": 1000,      # a released press that waited longer than this is dropped
    "hold_show_wheel": True,        # raise George's wheel while a "hold" is held (not on taps)
    "hold_keepalive_ms": 300,
    "alt_step_ft": 30,              # one climb/descend press = this many feet (FLT and H-B); 0 = George's default       # "hold" commands: keep-alive interval (bridge lets go after 1 s without)
}

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "settings": DEFAULT_SETTINGS,
    "devices": {
        "keyboard": {"name": "Keyboard", "enabled": True, "axes_as_buttons": False},
    },
    "commands": DEFAULT_COMMANDS,
    "virtual": DEFAULT_VIRTUAL,
    "state": {r: {"active_group": None, "selected": {}} for r in ROLES},
    "bindings": {r: {} for r in ROLES},
}

# Version 1 used made-up ids; map the ones that have a bridge equivalent.
_V1_TO_V2 = {
    "gunner": {
        "g_menu_toggle": "show_hide", "g_menu_up_short": "up", "g_menu_up_long": "up_long",
        "g_menu_down_short": "down", "g_menu_down_long": "down_long",
        "g_menu_left_short": "left", "g_menu_left_long": "left_long",
        "g_menu_right_short": "right", "g_menu_right_long": "right_long",
        "g_search_phs": "cpg_phs_search", "g_tgt_next": "cpg_next_target",
        "g_tgt_prev": "cpg_previous_target", "g_tgt_select": "cpg_select_target",
        "g_tgt_zoom_in": "cpg_target_list_zoom_in", "g_tgt_zoom_out": "cpg_target_list_zoom_out",
        "g_tgt_filter_all": "cpg_target_list_filter", "g_wpn_missile_type": "cpg_next_missile",
        "g_multi_center": "cpg_cleared_fire", "g_lase": "cpg_lase_target",
        "g_store": "store_target", "g_cease_fire": "cpg_hold_fire",
    },
    "pilot": {
        "p_menu_toggle": "show_hide", "p_menu_up_short": "up", "p_menu_up_long": "up_long",
        "p_menu_down_short": "down", "p_menu_down_long": "down_long",
        "p_menu_left_short": "left", "p_menu_left_long": "left_long",
        "p_menu_right_short": "right", "p_menu_right_long": "right_long",
        "p_alt_up": "climb", "p_alt_down": "descend",
        "p_speed_up": "speed_up", "p_speed_down": "slow_down",
        "p_turn_left": "come_left", "p_turn_right": "come_right",
        "p_hover": "mode_hover", "p_next_wpt": "follow_waypoints",
        "p_hold_position": "hold_position", "p_hover_fwd": "hover_forward",
        "p_hover_back": "hover_back", "p_hover_left": "hover_left",
        "p_hover_right": "hover_right", "p_return_bp": "hold_position",
        "p_hide_terrain": "mask_position", "p_orbit": "orbit_overhead",
        "p_break_left": "break_left", "p_break_right": "break_right",
    },
}


def token_key(t):
    """Identity of an input token (display name and threshold ignored)."""
    return (t["guid"], t["type"], str(t["code"]))


def combo_key(combo):
    return frozenset(token_key(t) for t in combo)


def combo_text(combo):
    if not combo:
        return "(unbound)"

    def one(t):
        name = t.get("name", str(t["code"]))
        if t["type"] == "axis":
            name += f" @{round(t.get('threshold', 0.5) * 100)}%"
        return name
    return " + ".join(one(t) for t in combo)


def bindings_text(combos):
    """All of a command's bindings on one line."""
    return "  |  ".join(combo_text(c) for c in combos) if combos else "(unbound)"


class Config:
    def __init__(self, path="config.json"):
        self.path = path
        self.migration_notes = []
        self.data = self._load()

    # ---------- file I/O ----------
    def _load(self):
        if not os.path.exists(self.path):
            data = copy.deepcopy(DEFAULT_CONFIG)
            self._write(data)
            return data
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version", 1) < 2:
            self._migrate_v1(data)
        if data.get("version", 1) < 3:
            self._migrate_v2(data)
        if data.get("version", 1) < 4:
            self._migrate_v3(data)
        if self._normalise_bindings(data):     # v4 -> v5 shape, also fixes hand edits
            self.migration_notes.append("bindings stored as lists (several per command)")
        if data.get("version", 1) < 5:
            data["version"] = 5
        if data.get("version", 1) < 6:
            self._migrate_v5(data)
        if data.get("version", 1) < 7:
            self._migrate_v6(data)
        if data.get("version", 1) < 8:
            self._migrate_v7(data)
        if data.get("version", 1) < 9:
            self._migrate_v8(data)
        if data.get("version", 1) < 10:
            self._migrate_v9(data)
        if data.get("version", 1) < 11:
            self._migrate_v10(data)
        # fill in anything missing (e.g. after an upgrade or hand edit)
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, copy.deepcopy(v))
        for k, v in DEFAULT_SETTINGS.items():
            data["settings"].setdefault(k, v)
        for r in ROLES:
            data["commands"].setdefault(r, [])
            data["bindings"].setdefault(r, {})
            data["state"].setdefault(r, {"active_group": None, "selected": {}})
            data["state"][r].setdefault("selected", {})
        data["devices"].setdefault("keyboard", {"name": "Keyboard", "enabled": True})
        if self.migration_notes:
            self._write(data)
        return data

    def _migrate_v1(self, data):
        old = data.get("bindings", {})
        new = {r: {} for r in ROLES}
        for role in ROLES:
            for old_id, combo in old.get(role, {}).items():
                new_id = _V1_TO_V2[role].get(old_id)
                if new_id:
                    new[role][new_id] = combo
                    self.migration_notes.append(f"{role}: {old_id} -> {new_id}")
                else:
                    self.migration_notes.append(f"{role}: {old_id} dropped (no bridge equivalent)")
        data["bindings"] = new
        data["commands"] = copy.deepcopy(DEFAULT_COMMANDS)
        data["virtual"] = copy.deepcopy(DEFAULT_VIRTUAL)
        data["state"] = {r: {"active_group": None, "selected": {}} for r in ROLES}
        data["version"] = CONFIG_VERSION

    def _migrate_v2(self, data):
        """Fold commands that press the same slot into one, keeping user edits."""
        for role, merges in MERGED.items():
            binds = data.get("bindings", {}).get(role, {})
            for old, (keep, name) in merges.items():
                if old in binds:
                    combo = binds.pop(old)
                    if keep not in binds:
                        binds[keep] = combo
                        self.migration_notes.append(f"{role}: {old} -> {keep} (same press)")
                    else:
                        self.migration_notes.append(
                            f"{role}: {old} binding dropped, {keep} (same press) is already bound")
            for group in data.get("commands", {}).get(role, []):
                group["items"] = [c for c in group["items"] if c["id"] not in merges]
                for c in group["items"]:
                    for keep, name in merges.values():
                        if c["id"] == keep:
                            c["name"] = name
        data["version"] = 3
        self.migration_notes.append("merged commands that share a wheel slot")

    def _migrate_v3(self, data):
        """H-B moves become continuous holds, unless the user changed them."""
        for group in data.get("commands", {}).get("pilot", []):
            for c in group["items"]:
                if c["id"] in ("hover_forward", "hover_back", "hover_left", "hover_right") \
                        and c.get("hold_action", "repeat") == "repeat":
                    c["hold_action"] = "hold"
        data["version"] = 4
        self.migration_notes.append("H-B moves now use 'hold' (George's button held while you hold)")

    def _migrate_v10(self, data):
        """Speed: one step per press, auto-repeat only after holding a moment."""
        for group in data.get("commands", {}).get("pilot", []):
            for c in group["items"]:
                if c["id"] in ("speed_up", "slow_down") and c.get("hold_action") in ("hold", "repeat"):
                    c["hold_action"] = "ramp"
                    self.migration_notes.append(f"{c['id']}: one step per press, repeats after holding")
        data["version"] = 11

    def _migrate_v9(self, data):
        """Altitude: first press one step, holding ramps on (with the wheel)."""
        ids = ("climb", "descend", "increase_altitude", "decrease_altitude",
               "hover_up_10ft", "hover_down_10ft")
        for group in data.get("commands", {}).get("pilot", []):
            for c in group["items"]:
                if c["id"] in ids and c.get("hold_action") == "once":
                    c["hold_action"] = "ramp"
                    c.setdefault("show_wheel", True)
                    self.migration_notes.append(f"{c['id']}: one step, ramps on while held")
        st = data.setdefault("settings", {})
        if st.get("ramp_step_ms", 90) == 90:
            st["ramp_step_ms"] = 200
        if st.get("ramp_max_stages", 10) == 10:
            st["ramp_max_stages"] = 40
        data["version"] = 10

    def _migrate_v8(self, data):
        """Altitude in fixed steps: one press = one alt_step_ft step."""
        ids = ("climb", "descend", "increase_altitude", "decrease_altitude",
               "hover_up_10ft", "hover_down_10ft")
        for group in data.get("commands", {}).get("pilot", []):
            for c in group["items"]:
                if c["id"] in ids and c.get("hold_action") != "once":
                    c["hold_action"] = "once"
                    self.migration_notes.append(f"{c['id']}: one step per press")
        data["version"] = 9

    def _migrate_v7(self, data):
        """H-B 10 ft steps: the bridge pulses them while held (much quicker)."""
        for group in data.get("commands", {}).get("pilot", []):
            for c in group["items"]:
                if c["id"] in ("hover_up_10ft", "hover_down_10ft") and c.get("hold_action") == "repeat":
                    c["hold_action"] = "hold"
                    self.migration_notes.append(f"{c['id']}: now 'hold' (bridge pulses the steps)")
        data["version"] = 8

    def _migrate_v6(self, data):
        """Climb/descend from other modes: FLT altitude first, H-B step as fallback."""
        virt = data.setdefault("virtual", copy.deepcopy(DEFAULT_VIRTUAL))
        for name, old_default in (("climb", "hover_up_10ft"), ("descend", "hover_down_10ft")):
            table = virt.get(name)
            if table and table.get("default") == old_default:
                table["default"] = copy.deepcopy(DEFAULT_VIRTUAL[name]["default"])
                self.migration_notes.append(f"{name}: from other modes now tries FLT first")
        data["version"] = 7

    def _migrate_v5(self, data):
        """Wheel feedback only for altitude, speed and direction commands."""
        for role in ROLES:
            for group in data.get("commands", {}).get(role, []):
                for c in group["items"]:
                    c.setdefault("show_wheel", c["id"] in WHEEL_FEEDBACK)
        data["version"] = 6
        self.migration_notes.append("wheel feedback now only for altitude, speed and direction")

    @staticmethod
    def _normalise_bindings(data):
        """Old files (and hand edits) store one combination per command:
        a list of tokens. Wrap those into a list of combinations."""
        changed = False
        for role, binds in data.get("bindings", {}).items():
            for cid, value in list(binds.items()):
                if value and isinstance(value[0], dict):
                    binds[cid] = [value]
                    changed = True
                elif not value:
                    del binds[cid]
                    changed = True
        return changed

    def _write(self, data):
        folder = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)  # atomic: never leaves a half-written config

    def save(self):
        self._write(self.data)

    # ---------- settings ----------
    @property
    def role(self):
        return self.data["settings"]["role"]

    @role.setter
    def role(self, value):
        assert value in ROLES
        self.data["settings"]["role"] = value

    def setting(self, key, default=None):
        return self.data["settings"].get(key, DEFAULT_SETTINGS.get(key, default))

    # ---------- devices ----------
    def device_enabled(self, guid):
        return self.data["devices"].get(guid, {}).get("enabled", True)

    def device_axes(self, guid):
        return self.data["devices"].get(guid, {}).get("axes_as_buttons", guid != "keyboard")

    def set_device_axes(self, guid, enabled):
        self.data["devices"].setdefault(guid, {"name": guid})["axes_as_buttons"] = enabled

    def register_device(self, guid, name):
        self.data["devices"].setdefault(guid, {"name": name, "enabled": True,
                                               "axes_as_buttons": True})
        self.data["devices"][guid]["name"] = name

    def set_device_enabled(self, guid, enabled):
        self.data["devices"].setdefault(guid, {"name": guid})["enabled"] = enabled

    # ---------- commands ----------
    def groups(self, role):
        return [g["group"] for g in self.data["commands"][role]]

    def commands(self, role, group):
        for g in self.data["commands"][role]:
            if g["group"] == group:
                return g["items"]
        return []

    def command(self, role, cmd_id):
        for g in self.data["commands"][role]:
            for c in g["items"]:
                if c["id"] == cmd_id:
                    return g["group"], c
        return None, None

    def virtual(self, name):
        return self.data.get("virtual", {}).get(name)

    # ---------- UI state ----------
    def active_group(self, role):
        return self.data["state"][role]["active_group"]

    def set_active_group(self, role, group):
        self.data["state"][role]["active_group"] = group

    def selected_command(self, role, group):
        return self.data["state"][role]["selected"].get(group)

    def set_selected_command(self, role, group, cmd_id):
        self.data["state"][role]["selected"][group] = cmd_id

    # ---------- bindings ----------
    def bindings(self, role, cmd_id):
        """All combinations bound to a command (possibly empty)."""
        return self.data["bindings"][role].get(cmd_id, [])

    def all_bindings(self, role):
        """command id -> list of combinations."""
        return {cid: list(v) for cid, v in self.data["bindings"][role].items() if v}

    def find_conflict(self, role, combo):
        """(command id, index) of a binding with exactly these inputs, or None."""
        key = combo_key(combo)
        for cid, combos in self.data["bindings"][role].items():
            for i, b in enumerate(combos):
                if combo_key(b) == key:
                    return cid, i
        return None

    def add_binding(self, role, cmd_id, combo):
        self.data["bindings"][role].setdefault(cmd_id, []).append(list(combo))

    def remove_binding(self, role, cmd_id, index):
        combos = self.data["bindings"][role].get(cmd_id, [])
        if 0 <= index < len(combos):
            combos.pop(index)
        if not combos:
            self.data["bindings"][role].pop(cmd_id, None)

    def clear_bindings(self, role, cmd_id):
        self.data["bindings"][role].pop(cmd_id, None)


if __name__ == "__main__":
    c = Config()
    print(f"Config at {os.path.abspath(c.path)}")
    for note in c.migration_notes:
        print("  migrated", note)
