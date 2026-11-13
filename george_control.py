"""
George control: drives the AH-64D George AI from your bound controllers and keys,
through GeorgeBridge.lua (UDP 127.0.0.1:7788) inside DCS.

Bindings come from config.json, made with george_config_gui.py. Edits made in the
GUI while this runs are picked up automatically.

Which binding set is live follows your seat in DCS:
  pilot seat -> "gunner" bindings (George runs the TADS and weapons)
  CP/G seat  -> "pilot" bindings  (George flies)
Override with --role gunner|pilot, or "runtime_role" in config.json.

Install:  pip install pygame pynput
Run:      python george_control.py              run the controller
          python george_control.py --dry-run    print what would be sent, no DCS needed
          python george_control.py state        one-shot: ask the bridge for its state
          python george_control.py send "cmd break_left"
          python george_control.py check        compare bound commands with the bridge's list
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import socket
import sys
import threading
import time

from config_store import ROLES, SEAT_TO_ROLE, Config, combo_text, default_config_path, token_key
from input_manager import InputManager, axis_token_active

# Windows virtual-key codes for modifiers -> the Tk keysyms the GUI stores.
_VK_MODIFIERS = {160: "Shift_L", 161: "Shift_R", 162: "Control_L", 163: "Control_R",
                 164: "Alt_L", 165: "Alt_R", 91: "Win_L", 92: "Win_R"}

MAX_RESOLVE_DEPTH = 4
QUEUE_DEPTH = 4


def log(msg):
    print(time.strftime("%H:%M:%S "), msg, flush=True)


# ------------------------------------------------------------------ bridge

class Bridge:
    """One UDP socket. Not thread-safe: only the Dispatcher thread uses it."""

    def __init__(self, host, port, timeout):
        self.addr = (host, port)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))

    def _drain(self):
        self.sock.setblocking(False)
        try:
            while True:
                self.sock.recvfrom(4096)       # late replies from timed-out requests
        except (BlockingIOError, ConnectionResetError, OSError):
            pass
        finally:
            self.sock.setblocking(True)

    def send(self, line, timeout=None):
        self._drain()
        try:
            self.sock.sendto(line.encode(), self.addr)
            self.sock.settimeout(timeout or self.timeout)
            data, _ = self.sock.recvfrom(65535)
            return data.decode(errors="replace")
        except (socket.timeout, ConnectionResetError, OSError):
            return None   # Windows reports "connection reset" when nothing listens


def parse_state(reply):
    if not reply or not reply.startswith("state"):
        return None
    fields = dict(re.findall(r"(\w+)=(\S+)", reply))
    for key in ("seat", "mode"):
        if fields.get(key) in ("nil", "None", "unknown", ""):
            fields[key] = None
    return fields


# ------------------------------------------------------------------ dispatcher

class Job:
    def __init__(self, label, command, hold_action, show_wheel=False):
        self.label = label
        self.show_wheel = show_wheel
        self.command = command
        self.hold_action = hold_action
        self.held = threading.Event()
        self.held.set()
        self.created = time.monotonic()


class Dispatcher(threading.Thread):
    """Serialises everything sent to the bridge, which runs one task at a time."""

    def __init__(self, cfg: Config, dry_run=False):
        super().__init__(daemon=True, name="george-dispatch")
        self.cfg = cfg
        self.dry_run = dry_run
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self.bridge = Bridge(cfg.setting("bridge_host"), cfg.setting("bridge_port"),
                             cfg.setting("reply_timeout_s"))
        self.seat = None          # "pilot" / "cpg" / None
        self.connected = False
        self._stop = threading.Event()

    def submit(self, job: Job):
        # The same command already waiting adds nothing: hammering a button
        # shouldn't stack up presses that run long after you've stopped.
        with self.q.mutex:
            if any(j.command == job.command and j.hold_action == job.hold_action
                   for j in self.q.queue):
                log(f"  {job.label}: already queued, ignored")
                return
        try:
            self.q.put_nowait(job)
        except queue.Full:
            log(f"  {job.label}: backlog full, dropped")

    def stop(self):
        self._stop.set()

    # ---- bridge helpers
    def send(self, line, timeout=None):
        if self.dry_run:
            return f"(dry run) {line}"
        reply = self.bridge.send(line, timeout)
        # The bridge runs one task at a time; hiding the wheel after a hold
        # takes ~0.6 s, so give it a few short tries rather than one.
        for _ in range(4):
            if not (reply and reply.startswith("busy")):
                break
            time.sleep(0.25)
            reply = self.bridge.send(line, timeout)
        return reply

    def poll_state(self):
        if self.dry_run:
            return None
        state = parse_state(self.bridge.send("state", timeout=1.0))
        was = self.connected
        self.connected = state is not None
        if self.connected != was:
            log("DCS bridge connected" if self.connected else "DCS bridge not responding")
        if state:
            if state.get("seat") != self.seat:
                self.seat = state.get("seat")
                log(f"seat: {self.seat}")
        return state

    def resolve(self, command):
        """Follow the virtual chain. Returns (candidates, reason-if-refused):
        bridge commands to try in order. A virtual entry can map to a list,
        e.g. climb from CMBT: FLT altitude first, the H-B step if FLT isn't
        offered."""
        if not self.cfg.virtual(command):
            return [command], ""
        state = self.poll_state()
        if state is None and not self.dry_run:
            return None, "no reply from the bridge; is DCS running?"
        state = state or {}
        name = command
        for _ in range(MAX_RESOLVE_DEPTH):
            if isinstance(name, list):
                return name, ""
            table = self.cfg.virtual(name)
            if table is None:
                return [name], ""
            key = state.get(table["by"])
            if table["by"] == "seat" and key not in ("pilot", "cpg"):
                return None, f"{name}: seat is {key!r}, refusing to guess"
            nxt = table.get(key) or table.get("default")
            if nxt is None:
                return None, f"{name}: nothing mapped for {table['by']}={key!r}"
            name = nxt
        return None, f"{command}: virtual chain did not terminate"

    # ---- thread
    def run(self):
        poll_s = self.cfg.setting("state_poll_s")
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=poll_s)
            except queue.Empty:
                self.poll_state()
                continue
            waited = time.monotonic() - job.created
            if waited > self.cfg.setting("max_queue_wait_ms") / 1000 and not job.held.is_set():
                # Waited behind something slow and you've already let go:
                # running it now would be late and probably unwanted.
                log(f"  {job.label}: waited {waited:.1f} s and released, skipped")
                continue
            try:
                self._run_job(job)
            except Exception as exc:   # keep the thread alive whatever happens
                log(f"  {job.label}: {exc!r}")

    def _run_hold(self, job: Job, resolved, label, min_ms=None):
        """George's button stays down while the binding is held -- and, with
        min_ms, at least that long, so a quick press is still a full step."""
        # The bridge raises the wheel itself, before George's button goes down,
        # and hides it again after release if it was hidden to begin with.
        wheel = job.show_wheel and self.cfg.setting("hold_show_wheel")
        reply = self.send(f"hold {resolved}" + (" wheel" if wheel else ""))
        if reply and "not a held command" in reply:
            log(f"  {label}: tap command, repeating while held")
            return "tap"
        log(f"  {label} (hold): {reply or 'no reply'}")
        if self._unreachable(reply):
            return "unreachable"
        if reply is None or reply.startswith("error"):
            return
        interval = self.cfg.setting("hold_keepalive_ms") / 1000
        release_after = time.monotonic() + (min_ms or 0) / 1000   # button went down ~now
        try:
            while job.held.is_set() or time.monotonic() < release_after:
                t_end = time.monotonic() + interval
                while time.monotonic() < t_end and (job.held.is_set() or time.monotonic() < release_after):
                    time.sleep(0.01)
                if not job.held.is_set() and time.monotonic() >= release_after:
                    break
                if not (self.send("keep", timeout=1.0) or "").startswith("ok"):
                    log(f"  {label}: keep-alive lost, bridge will let go")
                    return
        finally:
            reply = self.send("release", timeout=1.0)
            log(f"  {label}: {reply or 'no reply to release'}")

    def _run_job(self, job: Job):
        candidates, why = self.resolve(job.command)
        if candidates is None:
            log(f"  {job.label}: {why}")
            return
        for i, resolved in enumerate(candidates):
            label = job.label if resolved == job.command else f"{job.label} -> {resolved}"
            outcome = self._execute(job, resolved, label)
            if outcome != "unreachable" or i + 1 == len(candidates):
                return
            log(f"  {job.label}: {resolved} not offered here, trying {candidates[i + 1]}")

    @staticmethod
    def _unreachable(reply):
        return bool(reply) and reply.startswith("error") and "not reachable" in reply

    def _step_hold_ms(self, resolved):
        """Hold time for one alt_step_ft FLT step, or None."""
        ft = self.cfg.setting("alt_step_ft") or 0
        if ft <= 0 or resolved not in ("increase_altitude", "decrease_altitude"):
            return None
        return max(520, min(1990, round(700 + (ft - 10) * 20)))

    def _step_taps(self, resolved):
        """Number of 10 ft taps for one alt_step_ft H-B step, or None."""
        ft = self.cfg.setting("alt_step_ft") or 0
        if ft <= 0 or resolved not in ("hover_up_10ft", "hover_down_10ft"):
            return None
        return max(1, min(12, round(ft / 10)))

    def _step_args(self, resolved):
        """Arguments that make one press an altitude step of alt_step_ft.
        FLT altitude is rate-based: a 0.7 s hold is ~10 ft and every further
        0.2 s adds ~10 ft (George's slow rate, under the 2 s cliff). H-B steps
        are 10 ft per tap, so the tap is repeated."""
        ft = self.cfg.setting("alt_step_ft") or 0
        if ft <= 0:
            return ""
        if resolved in ("increase_altitude", "decrease_altitude"):
            ms = round(700 + (ft - 10) * 20)
            return f" hold={max(520, min(1990, ms))}"
        if resolved in ("hover_up_10ft", "hover_down_10ft"):
            return f" {max(1, min(12, round(ft / 10)))}"
        return ""

    def _execute(self, job: Job, resolved, label):

        if job.hold_action == "hold":
            outcome = self._run_hold(job, resolved, label)
            if outcome != "tap":
                return outcome
            # A tap command (speed, H-B 10 ft steps) can't be held down: in FLT,
            # holding the speed button is a climb. Repeat it instead.
            job.hold_action = "repeat"
            job.from_hold = True

        if job.hold_action == "once":
            reply = self.send(f"cmd {resolved}{self._step_args(resolved)}")
            log(f"  {label}: {reply or 'no reply'}")
            return "unreachable" if self._unreachable(reply) else None

        ramp = job.hold_action == "ramp"
        # FLT altitude: one continuous hold of at least one alt_step_ft step. A
        # quick press is stretched to exactly that step; held longer, the button
        # just stays down and George ramps up himself (50 ft/s, 250 ft/s past 2 s).
        alt_hold = self._step_hold_ms(resolved)
        if ramp and alt_hold is not None:
            return self._run_hold(job, resolved, f"{label} (step, ramps while held)", min_ms=alt_hold)
        # H-B altitude: steps of alt_step_ft, growing while held.
        taps = self._step_taps(resolved)
        altitude = alt_hold is not None or taps is not None
        hold = alt_hold or self.cfg.setting("ramp_first_ms")
        step, max_ms = self.cfg.setting("ramp_step_ms"), self.cfg.setting("ramp_max_ms")
        stages = self.cfg.setting("ramp_max_stages") if ramp else 200
        gap = self.cfg.setting("repeat_gap_ms") / 1000
        first_delay = self.cfg.setting("repeat_first_delay_ms") / 1000
        wheel_on = job.show_wheel and (
            self.cfg.setting("ramp_show_wheel") if ramp else
            getattr(job, "from_hold", False) and self.cfg.setting("hold_show_wheel"))
        shown_at = None
        try:
            for stage in range(stages):
                if stage and not job.held.is_set():
                    break                  # always fire once, so a tap is one step
                # Altitude: the wheel comes up with the first step, so even a
                # single 30 ft press shows it. Anything else (speed): only once
                # it is really being held, so a tap doesn't flash it.
                if wheel_on and shown_at is None and stage == (0 if altitude else 1):
                    self.send("wheel show")
                    shown_at = time.monotonic()
                if ramp and taps:          # H-B: 10 ft taps, one more each step
                    line = f"cmd {resolved} {min(12, taps + stage)}"
                elif ramp and altitude:
                    line = f"cmd {resolved} hold={hold}"
                else:
                    line = f"cmd {resolved}{self._step_args(resolved)}"
                reply = self.send(line)
                log(f"  {label} [{stage + 1}]: {reply or 'no reply'}")
                if stage == 0 and self._unreachable(reply):
                    return "unreachable"
                if reply is None or reply.startswith("error"):
                    break
                if ramp and altitude:
                    hold = min(hold + step, max_ms)
                    continue
                # Tap commands (speed): one press is one step. Keep repeating
                # only once the button has been held past first_delay, then
                # every repeat gap -- like a keyboard's auto-repeat.
                wait = first_delay if stage == 0 else gap
                t_end = time.monotonic() + wait
                while time.monotonic() < t_end and job.held.is_set():
                    time.sleep(0.01)
        finally:
            if shown_at is not None:
                # Leave it up long enough to read, even after a single step.
                rest = self.cfg.setting("wheel_min_visible_ms") / 1000 - (time.monotonic() - shown_at)
                if rest > 0:
                    time.sleep(rest)
                self.send("wheel restore")


# ------------------------------------------------------------------ keyboard

class KeyboardWatcher:
    """Global keyboard state (works while DCS has focus). Uses pynput."""

    def __init__(self):
        self.down = set()
        self.lock = threading.Lock()
        self.listener = None
        try:
            from pynput import keyboard
        except ImportError:
            log("pynput not installed, keyboard bindings disabled (pip install pynput)")
            return
        self._kb = keyboard
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self.listener.daemon = True
        self.listener.start()

    def _key(self, key):
        vk = getattr(key, "vk", None)
        if vk is None and hasattr(key, "value"):
            vk = getattr(key.value, "vk", None)
        if vk is None:
            return None
        code = _VK_MODIFIERS.get(vk, vk)
        return ("keyboard", "key", str(code))

    def _press(self, key):
        k = self._key(key)
        if k:
            with self.lock:
                self.down.add(k)

    def _release(self, key):
        k = self._key(key)
        if k:
            with self.lock:
                self.down.discard(k)

    def snapshot(self):
        with self.lock:
            return set(self.down)

    def stop(self):
        if self.listener:
            self.listener.stop()


# ------------------------------------------------------------------ bindings

class LiveBinding:
    def __init__(self, role, cmd_id, tokens, item):
        self.role = role
        self.cmd_id = cmd_id
        self.tokens = tokens
        self.keys = frozenset(token_key(t) for t in tokens)
        self.hold_action = (item or {}).get("hold_action", "once")
        self.show_wheel = bool((item or {}).get("show_wheel", False))
        self.name = (item or {}).get("name", cmd_id)
        self.text = combo_text(tokens)
        self.axis_on = [False] * len(tokens)
        self.complete = False
        self.pending_since = None
        self.released_since = None
        self.has_superset = False
        self.fired: Job | None = None


class Controller:
    def __init__(self, cfg_path, role_override=None, dry_run=False):
        self.cfg_path = cfg_path
        self.role_override = role_override
        self.cfg = Config(cfg_path)
        self.mtime = os.path.getmtime(cfg_path)
        self.inputs = InputManager()
        self.keyboard = KeyboardWatcher()
        self.dispatcher = Dispatcher(self.cfg, dry_run)
        self.bindings = {r: [] for r in ROLES}
        self.role = None
        self._load_bindings()

    # ---- config
    def _load_bindings(self):
        self.bindings = {r: [] for r in ROLES}
        for role in ROLES:
            for cid, combos in self.cfg.all_bindings(role).items():
                _, item = self.cfg.command(role, cid)
                for tokens in combos:
                    if tokens:
                        self.bindings[role].append(LiveBinding(role, cid, tokens, item))
            for b in self.bindings[role]:
                b.has_superset = any(o is not b and b.keys < o.keys for o in self.bindings[role])
        n = {r: len(b) for r, b in self.bindings.items()}
        log(f"bindings loaded: gunner={n['gunner']} pilot={n['pilot']}")

    def _check_reload(self):
        try:
            mtime = os.path.getmtime(self.cfg_path)
        except OSError:
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        self._release_all()
        try:
            self.cfg = Config(self.cfg_path)
        except Exception as exc:     # half-edited by hand, etc.
            log(f"config reload failed, keeping previous bindings: {exc}")
            return
        self.dispatcher.cfg = self.cfg
        log("config.json changed, reloading")
        self._load_bindings()

    def _release_all(self):
        for role in ROLES:
            for b in self.bindings[role]:
                if b.fired:
                    b.fired.held.clear()
                b.fired, b.complete, b.pending_since = None, False, None

    # ---- role
    def _current_role(self):
        forced = self.role_override or self.cfg.setting("runtime_role")
        if forced in ROLES:
            return forced
        return SEAT_TO_ROLE.get(self.dispatcher.seat) or self.cfg.role

    # ---- input evaluation
    def _held_tokens(self):
        held = self.keyboard.snapshot() if self.cfg.device_enabled("keyboard") else set()
        for dev in self.inputs.devices.values():
            if not self.cfg.device_enabled(dev.guid):
                continue
            for kind, code in dev.state:
                held.add((dev.guid, kind, str(code)))
        return held

    def _binding_complete(self, b: LiveBinding, held):
        hyst = self.cfg.setting("axis_hysteresis")
        for i, tok in enumerate(b.tokens):
            if tok["type"] == "axis":
                dev = self.inputs.devices.get(tok["guid"])
                if not dev or not self.cfg.device_enabled(dev.guid):
                    b.axis_on[i] = False
                    return False
                axis = int(tok["code"].split(":")[0])
                b.axis_on[i] = axis_token_active(tok, dev.joy.get_axis(axis), b.axis_on[i], hyst)
                if not b.axis_on[i]:
                    return False
            elif token_key(tok) not in held:
                return False
        return True

    def _tick(self):
        _, rescanned = self.inputs.poll(lambda guid: False)   # buttons/hats; axes read per binding
        if rescanned:
            log("controllers changed: " + ", ".join(d.name for d in self.inputs.devices.values()))

        role = self._current_role()
        if role != self.role:
            self._release_all()
            self.role = role
            log(f"active bindings: {role}")

        live = self.bindings[role]
        held = self._held_tokens()
        now = time.monotonic()
        window = self.cfg.setting("chord_window_ms") / 1000

        debounce = self.cfg.setting("input_debounce_ms") / 1000
        for b in live:
            was = b.complete
            now_complete = self._binding_complete(b, held)
            if now_complete:
                b.released_since = None
                if not was:
                    b.complete = True
                    b.pending_since = now
            elif was:
                # Contacts bounce: a hat or button can drop out for a few ms
                # mid-press. Only count it as released once it has stayed
                # released for the debounce time; a quick re-make continues
                # the same press instead of starting a second command.
                if b.released_since is None:
                    b.released_since = now
                if now - b.released_since >= debounce:
                    b.complete = False
                    b.released_since = None
                    b.pending_since = None
                    if b.fired:
                        b.fired.held.clear()
                        b.fired = None

        complete = [b for b in live if b.complete]
        for b in complete:
            if b.pending_since is None:
                continue
            # Wait out the chord window only when a larger combination could
            # still complete around this one; otherwise fire straight away.
            if now - b.pending_since < window and b.has_superset:
                continue
            b.pending_since = None
            # A larger combination that contains this one wins (Ctrl+G beats G).
            if any(o is not b and b.keys < o.keys for o in complete):
                continue
            # Another binding of the same command is already holding it: one is enough.
            if any(o.fired and o.cmd_id == b.cmd_id for o in live):
                continue
            job = Job(f"{b.text} -> {b.name}", b.cmd_id, b.hold_action, b.show_wheel)
            b.fired = job
            self.dispatcher.submit(job)

    # ---- main
    def run(self):
        self.dispatcher.start()
        log("controllers: " + (", ".join(d.name for d in self.inputs.devices.values()) or "none"))
        log("running, Ctrl+C to stop")
        period = self.cfg.setting("controller_poll_ms") / 1000
        last_reload = 0.0
        try:
            while True:
                self._tick()
                t = time.monotonic()
                if t - last_reload > 1.0:
                    last_reload = t
                    self._check_reload()
                time.sleep(period)
        except KeyboardInterrupt:
            log("stopping")
        finally:
            self._release_all()
            self.dispatcher.stop()
            self.keyboard.stop()
            self.inputs.close()


# ------------------------------------------------------------------ one-shots

def one_shot(cfg_path, line):
    cfg = Config(cfg_path)
    b = Bridge(cfg.setting("bridge_host"), cfg.setting("bridge_port"), cfg.setting("reply_timeout_s"))
    reply = b.send(line)
    print(reply or "no reply (is DCS running with GeorgeBridge.lua loaded?)")
    return 0 if reply else 1


def check(cfg_path):
    cfg = Config(cfg_path)
    b = Bridge(cfg.setting("bridge_host"), cfg.setting("bridge_port"), cfg.setting("reply_timeout_s"))
    reply = b.send("list")
    if not reply or not reply.startswith("ok"):
        print("no reply from the bridge; start a mission in the AH-64D first")
        return 1
    names = set(reply.split(":", 1)[1].split())
    bad = 0
    for role in ROLES:
        for cid in cfg.all_bindings(role):
            if cid in names:
                continue
            if cfg.virtual(cid):
                targets = set()
                for k, v in cfg.virtual(cid).items():
                    if k != "by":
                        targets.update(v if isinstance(v, list) else [v])
                missing = targets - names
                if not missing:
                    continue
                print(f"{role}: {cid} is virtual but targets unknown {sorted(missing)}")
            else:
                print(f"{role}: {cid} is not a bridge command")
            bad += 1
    print("all bound commands are known to the bridge" if not bad else f"{bad} problem(s)")
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="run", choices=["run", "state", "send", "check"])
    ap.add_argument("line", nargs="?", default="", help="bridge line for 'send'")
    ap.add_argument("--config", default=default_config_path())
    ap.add_argument("--role", choices=ROLES, help="force a binding set instead of following the seat")
    ap.add_argument("--dry-run", action="store_true", help="don't talk to DCS, just print")
    args = ap.parse_args(argv)

    if args.action == "state":
        return one_shot(args.config, "state")
    if args.action == "send":
        if not args.line:
            ap.error("send needs a line, e.g. send \"cmd break_left\"")
        return one_shot(args.config, args.line)
    if args.action == "check":
        return check(args.config)

    cfg = Config(args.config)
    for note in cfg.migration_notes:
        log(f"migrated {note}")
    Controller(args.config, args.role, args.dry_run).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
