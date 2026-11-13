"""
George commands configurator.

Workflow:
  1. Pick the George role (gunner / pilot) at the top.
  2. In a command group, choose a command from its pull-down. That group becomes
     the active one (you can also click the group's radio button).
  3. Click "Bind…" (or press F2) and press the key/button or combination.
     Release everything to finish. Esc cancels.
     Axes work as buttons too: push a stick/trigger/pedal past halfway while binding,
     then set the exact trigger point with the slider under "Axis trigger points".
     Keep your hands off the controls when binding starts: that's when rest
     positions are measured.

Install: pip install pygame
Run:     python george_config_gui.py [path/to/config.json]
"""
import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from config_store import (HOLD_ACTIONS, default_config_path, ROLE_LABELS, ROLES, Config, bindings_text, combo_text,
                          token_key)
from input_manager import InputManager, axis_travel, key_token


class CaptureDialog(tk.Toplevel):
    """Collects a key/button combination: press order is kept, finishes when all
    released -- or when OK is clicked, for switches that never release (a
    three-way switch whose rest position is itself a button)."""

    def __init__(self, app, title):
        super().__init__(app)
        self.app = app
        self.title("Bind input")
        self.resizable(False, False)
        self.transient(app)
        self.order, self.held = [], set()
        self._finish_job = None
        self.result = None

        ttk.Label(self, text=title, font=("Segoe UI", 11, "bold")).pack(padx=20, pady=(16, 4))
        ttk.Label(self, text="Press the key/button or combination now.\n"
                             "Release everything to finish, or hold it and click OK.\n"
                             "Esc = cancel.",
                  justify="center").pack(padx=20)
        self.combo_var = tk.StringVar(value="…")
        ttk.Label(self, textvariable=self.combo_var, font=("Segoe UI", 12),
                  foreground="#0a6").pack(padx=20, pady=12)
        row = ttk.Frame(self)
        row.pack(pady=(0, 14))
        self.ok_btn = ttk.Button(row, text="OK", command=self.accept, state="disabled")
        self.ok_btn.pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=self.cancel).pack(side="left", padx=4)

        self.bind("<KeyPress>", self._key_down)
        self.bind("<KeyRelease>", self._key_up)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.grab_set()
        self.focus_force()

    # keyboard (only while this dialog has focus)
    def _key_down(self, e):
        if e.keysym == "Escape" and not self.order:
            self.cancel()
            return "break"
        if e.keysym == "Escape":
            return "break"
        self.press(key_token(e.keysym, e.keycode))
        return "break"

    def _key_up(self, e):
        if e.keysym != "Escape":
            self.release(key_token(e.keysym, e.keycode))
        return "break"

    # shared by keyboard and controllers
    def press(self, tok):
        k = token_key(tok)
        if self._finish_job:
            self.after_cancel(self._finish_job)
            self._finish_job = None
        if k not in self.held:
            self.held.add(k)
            if k not in {token_key(t) for t in self.order}:
                self.order.append(tok)
        self._show()

    def _selection(self):
        """What OK binds: what's held now, in press order; if nothing is held
        any more, everything that was pressed."""
        now = [t for t in self.order if token_key(t) in self.held]
        return now or list(self.order)

    def _show(self):
        self.combo_var.set(combo_text(self._selection()) if self.order else "…")
        self.ok_btn.configure(state="normal" if self.order else "disabled")

    def accept(self):
        if self.order:
            self.result = self._selection()
            self.destroy()

    def release(self, tok):
        self.held.discard(token_key(tok))
        if self.order:
            self._show()
        if not self.held and self.order:
            # small delay absorbs key auto-repeat release/press pairs
            ms = self.app.cfg.setting("release_debounce_ms", 80)
            self._finish_job = self.after(ms, self._finish)

    def _finish(self):
        if not self.held:
            self.result = list(self.order)
            self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self, config_path="config.json"):
        super().__init__()
        self.title("George Commands Configurator")
        self.geometry("1100x680")
        self.minsize(900, 560)

        self.cfg = Config(config_path)
        self.inputs = InputManager(self.cfg.setting("axis_detect", 0.5),
                                   self.cfg.setting("axis_hysteresis", 0.05),
                                   self.cfg.setting("axis_default_threshold", 0.5))
        self.axis_rows = []      # (token, progressbar, state var) for the axis panel
        self.capture = None
        self.group_widgets = {}  # group -> (combobox, binding label var, [command dicts])

        self.role_var = tk.StringVar(value=self.cfg.role)
        self.active_var = tk.StringVar()
        self.last_input_var = tk.StringVar(value="Last input: –")
        self.axis_live_var = tk.StringVar(value="Select a controller to see its axes.")
        self.status_var = tk.StringVar(value=f"Config: {self.cfg.path}")

        self._build()
        self._register_devices()
        self._refresh_devices()
        self._rebuild_groups()
        self.bind("<F2>", lambda e: self.start_bind())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(self.cfg.setting("poll_interval_ms", 16), self._poll)

    # ---------------- layout ----------------
    def _build(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Configure commands for:", font=("Segoe UI", 10, "bold")).pack(side="left")
        for r in ROLES:
            ttk.Radiobutton(top, text=ROLE_LABELS[r], value=r, variable=self.role_var,
                            command=self.on_role_change).pack(side="left", padx=10)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)

        # devices
        dev = ttk.Labelframe(body, text="Input devices (double-click a Yes/No to toggle)", padding=6)
        self.dev_tree = ttk.Treeview(dev, columns=("name", "inputs", "enabled", "axes"),
                                     show="headings", height=8, selectmode="browse")
        for col, text, w in (("name", "Device", 210), ("inputs", "Btn / Hat / Axes", 100),
                             ("enabled", "Enabled", 60), ("axes", "Axes as buttons", 100)):
            self.dev_tree.heading(col, text=text)
            self.dev_tree.column(col, width=w, stretch=(col == "name"))
        self.dev_tree.pack(fill="both", expand=True)
        self.dev_tree.bind("<Double-1>", self.on_toggle_device)
        drow = ttk.Frame(dev)
        drow.pack(anchor="w", pady=(6, 0))
        ttk.Button(drow, text="Rescan devices", command=self.on_rescan).pack(side="left")
        ttk.Button(drow, text="Calibrate axis rest (hands off)",
                   command=self.on_calibrate).pack(side="left", padx=6)
        ttk.Label(dev, textvariable=self.last_input_var, foreground="#555").pack(anchor="w", pady=(6, 0))
        ttk.Label(dev, textvariable=self.axis_live_var, foreground="#555", font=("Consolas", 9),
                  justify="left", wraplength=420).pack(anchor="w", pady=(4, 0))
        body.add(dev, weight=1)

        # commands
        right = ttk.Frame(body)
        self.groups_frame = ttk.Labelframe(right, text="Command groups", padding=8)
        self.groups_frame.pack(fill="x")
        self.axis_frame = ttk.Labelframe(right, text="Axis trigger points (active command)", padding=8)
        self.axis_frame.pack(fill="x", pady=(8, 0))

        btns = ttk.Frame(right, padding=(0, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Add binding…  (F2)", command=self.start_bind).pack(side="left")
        ttk.Button(btns, text="Remove binding", command=self.clear_bind).pack(side="left", padx=6)
        ttk.Label(btns, text="While held:").pack(side="left", padx=(18, 4))
        self.hold_box = ttk.Combobox(btns, state="readonly", width=8, values=HOLD_ACTIONS)
        self.hold_box.pack(side="left")
        self.hold_box.bind("<<ComboboxSelected>>", self.on_hold_action)
        self.wheel_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Show wheel while held", variable=self.wheel_var,
                        command=self.on_wheel_toggle).pack(side="left", padx=(10, 0))
        ttk.Button(btns, text="Save", command=self.save).pack(side="right")

        bl = ttk.Labelframe(right, text="Bindings for this role", padding=6)
        bl.pack(fill="both", expand=True)
        self.bind_tree = ttk.Treeview(bl, columns=("group", "command", "binding"),
                                      show="headings", selectmode="browse")
        for col, text, w in (("group", "Group", 170), ("command", "Command", 260),
                             ("binding", "Binding", 300)):
            self.bind_tree.heading(col, text=text)
            self.bind_tree.column(col, width=w)
        self.bind_tree.pack(fill="both", expand=True)
        self.bind_tree.bind("<<TreeviewSelect>>", self.on_binding_row)
        body.add(right, weight=3)

        ttk.Label(self, textvariable=self.status_var, relief="sunken",
                  anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")

    # ---------------- devices ----------------
    def _register_devices(self):
        for guid, d in self.inputs.devices.items():
            self.cfg.register_device(guid, d.name)

    def _refresh_devices(self):
        sel = self.dev_tree.selection()
        self.dev_tree.delete(*self.dev_tree.get_children())
        yn = lambda b: "Yes" if b else "No"
        self.dev_tree.insert("", "end", iid="keyboard",
                             values=("Keyboard", "–", yn(self.cfg.device_enabled("keyboard")), "–"))
        for guid, d in self.inputs.devices.items():
            axes = yn(self.cfg.device_axes(guid)) if d.axes else "–"
            self.dev_tree.insert("", "end", iid=guid,
                                 values=(d.name, f"{d.buttons} / {d.hats} / {d.axes}",
                                         yn(self.cfg.device_enabled(guid)), axes))
        if sel and self.dev_tree.exists(sel[0]):
            self.dev_tree.selection_set(sel[0])

    def on_toggle_device(self, e):
        guid = self.dev_tree.identify_row(e.y)
        if not guid:
            return
        if self.dev_tree.identify_column(e.x) == "#4":
            dev = self.inputs.devices.get(guid)
            if dev and dev.axes:
                self.cfg.set_device_axes(guid, not self.cfg.device_axes(guid))
        else:
            self.cfg.set_device_enabled(guid, not self.cfg.device_enabled(guid))
        self.save(quiet=True)
        self._refresh_devices()

    def on_rescan(self):
        self.inputs.scan()
        self._register_devices()
        self._refresh_devices()
        self.status_var.set(f"Found {len(self.inputs.devices)} controller(s).")

    def on_calibrate(self):
        self.inputs.snapshot_rest()
        self.status_var.set("Axis rest positions taken (applies to new axis bindings).")

    def _poll(self):
        changes, rescanned = self.inputs.poll(self.cfg.device_axes)
        if rescanned:
            self._register_devices()
            self._refresh_devices()
        for tok, pressed in changes:
            if not self.cfg.device_enabled(tok["guid"]):
                continue
            if pressed:
                self.last_input_var.set(f"Last input: {tok['name']}")
            if self.capture and self.capture.winfo_exists():
                (self.capture.press if pressed else self.capture.release)(tok)
        self._update_axis_live()
        self.after(self.cfg.setting("poll_interval_ms", 16), self._poll)

    def _update_axis_live(self):
        sel = self.dev_tree.selection()
        dev = self.inputs.devices.get(sel[0]) if sel else None
        if dev and dev.axes:
            vals = dev.axis_values()
            self.axis_live_var.set("Axes (rest → now):\n" + "\n".join(
                f"A{i}: {r:+.2f} → {v:+.2f}" for i, (r, v) in enumerate(zip(dev.rest, vals))))
        else:
            self.axis_live_var.set("Select a controller to see its axes.")

        hyst = self.cfg.setting("axis_hysteresis", 0.05)
        for tok, bar, state in self.axis_rows:
            d = self.inputs.devices.get(tok["guid"])
            if not d:
                state.set("not connected")
                continue
            i, sign = tok["code"].split(":")
            f = axis_travel(d.joy.get_axis(int(i)), tok.get("rest", 0.0), sign)
            bar["value"] = f * 100
            thr = tok.get("threshold", 0.5)
            on = f >= thr or (state.get() == "ON" and f >= thr - hyst)
            state.set("ON" if on else "off")

    # ---------------- axis panel ----------------
    def on_hold_action(self, _e=None):
        _, cid = self._active_command()
        _, cmd = self.cfg.command(self.role, cid) if cid else (None, None)
        if cmd:
            cmd["hold_action"] = self.hold_box.get()
            self.save(quiet=True)
            self.status_var.set(f"{cmd['name']}: while held = {cmd['hold_action']}")

    def on_wheel_toggle(self):
        _, cid = self._active_command()
        _, cmd = self.cfg.command(self.role, cid) if cid else (None, None)
        if cmd:
            cmd["show_wheel"] = bool(self.wheel_var.get())
            self.save(quiet=True)
            self.status_var.set(f"{cmd['name']}: show wheel while held = {cmd['show_wheel']}")

    def _refresh_hold_box(self):
        _, cid = self._active_command()
        _, cmd = self.cfg.command(self.role, cid) if cid else (None, None)
        self.hold_box.set(cmd.get("hold_action", "once") if cmd else "")
        self.wheel_var.set(bool(cmd.get("show_wheel", False)) if cmd else False)

    def _refresh_axis_panel(self):
        self._refresh_hold_box()
        for w in self.axis_frame.winfo_children():
            w.destroy()
        self.axis_rows = []
        _, cid = self._active_command()
        combos = self.cfg.bindings(self.role, cid) if cid else []
        axes = [t for combo in combos for t in combo if t["type"] == "axis"]
        if not axes:
            ttk.Label(self.axis_frame, foreground="#777",
                      text="The selected command has no axis inputs bound.").grid(sticky="w")
            return
        for r, tok in enumerate(axes):
            ttk.Label(self.axis_frame, text=tok["name"], width=28).grid(row=r, column=0, sticky="w")
            pct = tk.StringVar()

            def moved(v, tok=tok, pct=pct):
                tok["threshold"] = round(float(v)) / 100
                pct.set(f"trigger at {round(float(v))}%")

            sc = ttk.Scale(self.axis_frame, from_=5, to=95, orient="horizontal",
                           length=200, command=moved)
            sc.set(round(tok.get("threshold", 0.5) * 100))
            moved(sc.get())
            sc.grid(row=r, column=1, padx=6)
            sc.bind("<ButtonRelease-1>", lambda e: self._axis_threshold_done())
            ttk.Label(self.axis_frame, textvariable=pct, width=14).grid(row=r, column=2, sticky="w")
            bar = ttk.Progressbar(self.axis_frame, maximum=100, length=140)
            bar.grid(row=r, column=3, padx=6)
            state = tk.StringVar(value="off")
            ttk.Label(self.axis_frame, textvariable=state, width=12,
                      foreground="#0a6").grid(row=r, column=4, sticky="w")
            self.axis_rows.append((tok, bar, state))

    def _axis_threshold_done(self):
        self.save(quiet=True)
        for g in self.group_widgets:
            self._update_group_label(g)
        self._refresh_bindings()

    # ---------------- groups ----------------
    @property
    def role(self):
        return self.role_var.get()

    def on_role_change(self):
        self.cfg.role = self.role
        self.save(quiet=True)
        self._rebuild_groups()

    def _rebuild_groups(self):
        for w in self.groups_frame.winfo_children():
            w.destroy()
        self.group_widgets.clear()
        role = self.role
        groups = self.cfg.groups(role)
        active = self.cfg.active_group(role)
        if active not in groups:
            active = groups[0] if groups else ""
        self.active_var.set(active)

        self.groups_frame.columnconfigure(1, weight=1)
        for row, group in enumerate(groups):
            cmds = self.cfg.commands(role, group)
            ttk.Radiobutton(self.groups_frame, text=group, value=group, variable=self.active_var,
                            command=self.on_active_group).grid(row=row, column=0, sticky="w", pady=2)
            cb = ttk.Combobox(self.groups_frame, state="readonly", width=46,
                              values=[c["name"] for c in cmds])
            cb.grid(row=row, column=1, sticky="ew", padx=8, pady=2)
            var = tk.StringVar()
            ttk.Label(self.groups_frame, textvariable=var, width=34,
                      foreground="#0a6").grid(row=row, column=2, sticky="w")
            self.group_widgets[group] = (cb, var, cmds)

            sel = self.cfg.selected_command(role, group)
            ids = [c["id"] for c in cmds]
            if cmds:
                cb.current(ids.index(sel) if sel in ids else 0)
            cb.bind("<<ComboboxSelected>>", lambda e, g=group: self.on_command_selected(g))
            self._update_group_label(group)
        self._refresh_bindings()
        self._refresh_axis_panel()

    def _selected_id(self, group):
        cb, _, cmds = self.group_widgets[group]
        i = cb.current()
        return cmds[i]["id"] if i >= 0 else None

    def _update_group_label(self, group):
        _, var, _ = self.group_widgets[group]
        cid = self._selected_id(group)
        var.set(bindings_text(self.cfg.bindings(self.role, cid)) if cid else "")

    def on_command_selected(self, group):
        self.cfg.set_selected_command(self.role, group, self._selected_id(group))
        self.active_var.set(group)          # choosing a command makes its group active
        self.on_active_group()
        self._update_group_label(group)

    def on_active_group(self):
        self.bind_tree.selection_remove(*self.bind_tree.selection())
        self.cfg.set_active_group(self.role, self.active_var.get())
        self.save(quiet=True)
        self._refresh_axis_panel()

    # ---------------- binding ----------------
    def _active_command(self):
        group = self.active_var.get()
        if group not in self.group_widgets:
            return None, None
        cid = self._selected_id(group)
        return group, cid

    def start_bind(self):
        if self.capture and self.capture.winfo_exists():
            return
        group, cid = self._active_command()
        if not cid:
            messagebox.showinfo("Bind", "Select a command first.")
            return
        _, cmd = self.cfg.command(self.role, cid)
        self.inputs.snapshot_rest()   # rest positions for any axis about to be bound
        self.capture = CaptureDialog(self, f"{group}: {cmd['name']}")
        self.wait_window(self.capture)
        combo, self.capture = self.capture.result, None
        if not combo:
            self.status_var.set("Binding cancelled.")
            return

        hit = self.cfg.find_conflict(self.role, combo)
        if hit and hit[0] == cid:
            self.status_var.set(f"{combo_text(combo)} is already bound to {cmd['name']}.")
            return
        if hit:
            og, oc = self.cfg.command(self.role, hit[0])
            oname = f"{og}: {oc['name']}" if oc else hit[0]
            if not messagebox.askyesno(
                    "Already bound",
                    f"{combo_text(combo)} is already bound to\n{oname}\n\nMove it here?"):
                return
            self.cfg.remove_binding(self.role, *hit)

        self.cfg.add_binding(self.role, cid, combo)
        self.save(quiet=True)
        for g in self.group_widgets:
            self._update_group_label(g)
        self._refresh_bindings()
        self._refresh_axis_panel()
        self.status_var.set(f"Bound {cmd['name']} → {combo_text(combo)}")

    def clear_bind(self):
        """Remove the binding selected in the list; with none selected, offer to
        remove all of the active command's bindings."""
        sel = self.bind_tree.selection()
        if sel and "#" in sel[0]:
            cid, idx = sel[0].rsplit("#", 1)
            _, cmd = self.cfg.command(self.role, cid)
            text = combo_text(self.cfg.bindings(self.role, cid)[int(idx)])
            self.cfg.remove_binding(self.role, cid, int(idx))
            self.status_var.set(f"Removed {text} from {cmd['name'] if cmd else cid}")
        else:
            _, cid = self._active_command()
            combos = self.cfg.bindings(self.role, cid) if cid else []
            if not combos:
                self.status_var.set("Select a binding in the list to remove it.")
                return
            _, cmd = self.cfg.command(self.role, cid)
            if not messagebox.askyesno("Remove bindings",
                                       f"Remove all {len(combos)} binding(s) of {cmd['name']}?"):
                return
            self.cfg.clear_bindings(self.role, cid)
        self.save(quiet=True)
        for g in self.group_widgets:
            self._update_group_label(g)
        self._refresh_bindings()
        self._refresh_axis_panel()

    def _refresh_bindings(self):
        self.bind_tree.delete(*self.bind_tree.get_children())
        for cid, combos in self.cfg.all_bindings(self.role).items():
            group, cmd = self.cfg.command(self.role, cid)
            if not cmd:  # skip bindings whose command was removed from the catalog
                continue
            for i, combo in enumerate(combos):
                self.bind_tree.insert("", "end", iid=f"{cid}#{i}",
                                      values=(group, cmd["name"], combo_text(combo)))

    def on_binding_row(self, _e):
        sel = self.bind_tree.selection()
        if not sel:
            return
        group, cmd = self.cfg.command(self.role, sel[0].rsplit("#", 1)[0])
        if group in self.group_widgets:
            cb, _, cmds = self.group_widgets[group]
            cb.current([c["id"] for c in cmds].index(cmd["id"]))
            self.cfg.set_selected_command(self.role, group, cmd["id"])
            self.active_var.set(group)
            self.cfg.set_active_group(self.role, group)
            self._update_group_label(group)
            self._refresh_axis_panel()   # no save/rebuild here: keeps the row selected

    # ---------------- misc ----------------
    def save(self, quiet=False):
        try:
            self.cfg.save()
            if not quiet:
                self.status_var.set(f"Saved to {self.cfg.path}")
        except OSError as e:
            messagebox.showerror("Save failed", str(e))

    def on_close(self):
        self.save(quiet=True)
        self.inputs.close()
        self.destroy()


if __name__ == "__main__":
    default = default_config_path()
    App(sys.argv[1] if len(sys.argv) > 1 else default).mainloop()
