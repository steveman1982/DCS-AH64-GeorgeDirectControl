# George AI Control for the DCS AH-64D

Command the AH-64D Apache's **George AI** directly from your HOTAS, gamepad, rudder pedals or keyboard — without voice software, and without cycling George's menu wheel by hand.

Bind any button, hat direction, axis deflection or key combination to a George command. When you press it, a small bridge inside DCS switches George's wheel to the right mode by itself and presses the right direction for you.

- **Both seats.** In the pilot seat your bindings command George as CP/G (TADS, targets, weapons). In the CP/G seat they command George as pilot (flight, hover, combat, countermeasures). The active set follows the seat you are in.
- **Mode-aware.** Asking for a FLT command while the wheel shows CMBT is fine: the bridge reads George's current mode and cycles to the right one. If a mode isn't offered right now (H-B above ~10 kt, for example), it gives up after one trip round the wheel and refuses repeats instantly for a few seconds.
- **Any input.** Buttons, hats, key combinations and *axes as buttons* (a stick, trigger or pedal past a trigger point you choose). Several bindings per command.
- **Hold behaviour per command.** One command per press, repeat while held, ramp, or keep George's button held down for as long as you hold yours (smooth H-B slides).
- **Altitude in steps.** One press of climb/descend is a fixed step (30 ft by default) in FLT and H-B; holding on in FLT hands over to George's own ramp-up.
- **Safe CP/G presses.** Context-dependent CP/G commands such as TADS zoom are only pressed when George's wheel actually offers them; otherwise nothing happens.
- **Wheel feedback.** For altitude, speed and course changes George's wheel can pop up briefly and is put away again afterwards (left alone if you opened it yourself).

> **Status:** a hobby project, tested by one person on one setup (WinWing HOTAS, Xbox 360 pad, a home-made Arduino rudder pedal, Windows 11). Expect rough edges.

---

## How it works

```
 your controllers / keyboard
            │
   george_control.py  ──UDP 127.0.0.1:7788──▶  GeorgeBridge.lua (inside DCS, Export.lua)
            ▲                                              │
            │                                   reads George's wheel, presses his buttons
   config.json  ◀── george_config_gui.py                   ▼
                                                   AH-64D George AI
```

| File | What it is |
|---|---|
| `george_config_gui.py` | GUI to make bindings: pick a command, press the input. Writes `config.json`. |
| `george_control.py` | The controller you run while flying. Reads your inputs and talks to the bridge. Picks up GUI changes live. |
| `config_store.py`, `input_manager.py` | Shared code: config file and controller input. |
| `dcs/GeorgeBridge.lua` | The bridge. Runs inside DCS's export environment and drives George. |
| `dcs/PrestonAI_page_common.snippet.lua` | A small addition to one of George's cockpit scripts so the bridge can read his wheel while it is hidden (see step 3). |
| `build_exe.bat` | Optional: builds `config.exe` and `george_ctrl.exe` with PyInstaller. |
| `f16ex_reader.py` | Small tool to watch a controller's raw axes, buttons and hats. |

---

## Requirements

- Windows 10/11, DCS World with the **AH-64D**.
- **Python 3.10+** from python.org (includes tkinter, which the GUI needs).
- Python packages: `pip install -r requirements.txt` (pygame for controllers, pynput for the keyboard).

---

## Installation

### 1. Get the code

```bat
git clone <this repository>
cd georgeaicontrol
pip install -r requirements.txt
```

### 2. Install the bridge in DCS

1. Copy `dcs\GeorgeBridge.lua` to `%USERPROFILE%\Saved Games\DCS\Scripts\`.
2. Open `%USERPROFILE%\Saved Games\DCS\Scripts\Export.lua` (create it if it doesn't exist) and add this line **at the end**, after any lines from other tools (WinWing, Tacview, SRS, …):

   ```lua
   local gb = require('lfs'); dofile(gb.writedir()..[[Scripts\GeorgeBridge.lua]])
   ```

   The bridge chains onto whatever was registered before it, so the other exports keep working.

The bridge writes a log to `Saved Games\DCS\Logs\GeorgeBridge.log` (kept across missions, rolled over at 2 MB). It is the first place to look when something doesn't happen.

### 3. Let the bridge read George's wheel while it is hidden

George's mode label and his CP/G wheel labels are only visible to scripts while the wheel is drawn. The bridge needs to read them with the wheel hidden, so one of George's cockpit scripts gets off-screen copies of those labels:

1. Open `<DCS install>\Mods\aircraft\AH-64D\Cockpit\Scripts\AI\PrestonAI_page_common.lua` in a text editor. **Make a backup first.**
2. Find this block:

   ```lua
   local mode_text 			= CreateElement "ceStringPoly"
   mode_text.name 				= "mode_text"
   ...
   AddElement(mode_text)
   ```

3. Paste the contents of `dcs\PrestonAI_page_common.snippet.lua` directly after `AddElement(mode_text)` (before the `--CPG AI` comment).

> ⚠️ This changes a DCS file. DCS logs an integrity-check warning (`IC fail`) for it, **multiplayer servers that enforce pure scripts may not let you in**, and **every DCS update can overwrite it** — re-apply it after updating. Without it, commands that need a particular wheel mode only work while George's wheel is visible.

### 4. Make your bindings

```bat
python george_config_gui.py
```

1. At the top, choose **George as Gunner** (you fly) or **George as Pilot** (you sit in front).
2. Pick a command from one of the group pull-downs.
3. Click **Add binding… (F2)** and press the button, hat, key combination, or push an axis past halfway. Release everything to finish — or, for switches that always hold a button (three-way switches), keep it in position and click **OK**.
4. For axes, set the exact trigger point with the slider under *Axis trigger points*.
5. Choose what the command does **while held**, and whether George's wheel should be shown.

Bindings are saved to `config.json` next to the scripts straight away.

### 5. Fly

Start a mission in the AH-64D, then:

```bat
python george_control.py
```

It prints every command it sends and the bridge's reply. Leave it running; changes made in the GUI are picked up within a second.

Useful one-off commands:

```bat
python george_control.py check                 :: are all bound commands known to the bridge?
python george_control.py state                 :: seat, wheel mode, wheel visibility, ...
python george_control.py send "cmd break_left" :: send any bridge command by hand
python george_control.py --dry-run             :: print what would be sent, no DCS needed
python george_control.py --role gunner         :: force a binding set instead of following the seat
```

---

## "While held" options

| Option | What happens |
|---|---|
| **once** | One command per press. |
| **repeat** | The command is sent again each time George finishes, until you let go. |
| **ramp** | Altitude: one step (`alt_step_ft`), then in FLT George's button stays down so he ramps up himself; in H-B the steps grow. Speed and other tap commands: one step per press, auto-repeat after holding ~0.7 s. |
| **hold** | George's button goes down when you press and comes up when you let go. Best for H-B slides. Never shorter than 0.65 s, so it can't turn into a short press (a short FLT Up/Down is a *speed* change; a short Left cycles the wheel). |

## Settings (`config.json` → `settings`)

Most people never need these. All are picked up live.

| Setting | Default | Meaning |
|---|---|---|
| `alt_step_ft` | 30 | One climb/descend press, in feet (FLT and H-B). `0` = George's own 10 ft. |
| `ramp_step_ms` | 200 | How much each further H-B/ramp step grows. |
| `repeat_first_delay_ms` | 700 | Speed etc.: how long to hold before auto-repeat starts. |
| `repeat_gap_ms` | 250 | Pause between repeats. |
| `wheel_min_visible_ms` | 1000 | Once raised, the wheel stays up at least this long. |
| `runtime_role` | `auto` | `auto` follows your seat; `gunner` or `pilot` forces one binding set. |
| `input_debounce_ms` | 60 | A binding must stay released this long to count as released (bouncy switches). |
| `chord_window_ms` | 50 | Inputs landing within this window count as one combination. |
| `max_queue_wait_ms` | 1000 | A released press that waited longer than this behind a slow command is dropped. |
| `axis_hysteresis` | 0.05 | An axis must fall this far below its trigger point to release. |
| `bridge_host` / `bridge_port` | 127.0.0.1 / 7788 | Where the bridge listens. |

The command catalog (`commands`) and mode-aware commands (`virtual`, e.g. *climb* → H-B step or FLT altitude) live in the same file and can be edited too.

## Building stand-alone .exe files (optional)

```bat
build_exe.bat
```

Produces `dist\config.exe` (GUI) and `dist\george_ctrl.exe` (controller). Both read and write `config.json` in the folder they are in, so keep the three files together. Some antivirus programs flag one-file PyInstaller builds; allow them or exclude the `dist` folder.

---

## Known limitations

- **George's reaction delay.** George waits about a second without new input before he flies to a new altitude or course (`lag_time_for_ai_pilot` in his scripts, which DCS protects). While a button is held he doesn't move yet — that is George, not this tool.
- **CP/G wheel label wording.** The TADS zoom check looks for the word "ZOOM" on the wheel. The bridge logs every CP/G label it sees (`label cpg_...` in the log) to help refine this.
- **DCS updates** can change George's scripts or button ids and break the bridge until it is updated.
- **Keyboard bindings** use a global keyboard hook (pynput) so they work while DCS has focus. Keys are only compared with your bindings; nothing is recorded or sent anywhere. The only network traffic is UDP to `127.0.0.1`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `no reply from the bridge` | Is a mission running in the AH-64D? Look in `dcs.log` for `GeorgeBridge` errors and in `GeorgeBridge.log`. The bridge only loads at mission start. |
| `menu mode unreadable` | Step 3 (the cockpit-script addition) is missing or was overwritten by an update. |
| `mode X not reachable` | George doesn't offer that mode right now (e.g. H-B at speed, GND in the air). |
| Wrong binding set active | `python george_control.py state` shows the seat; use `--role` to force one. |
| A switch fires twice | Raise `input_debounce_ms`. |

---

## Credits

- George's command mapping (which wheel mode and direction does what, and the CP/G macros) was transcribed from [VAICOM PRO Community Edition](https://github.com/Penecruz/VAICOMPRO-Community) (`CPGCommandHandler.cs`). Thanks to its maintainers.
- DCS World and the AH-64D are products of Eagle Dynamics. This project is **not affiliated with or endorsed by Eagle Dynamics**.

## AI disclaimer

Most of the code and documentation in this repository was written with **Anthropic's Claude (Claude Code)**, directed, reviewed and tested in DCS by a human. It was tested on a single setup and is provided **as is, without warranty**. Read the code before you run it, back up any DCS file you change, and use it at your own risk — particularly in multiplayer, where modified game files can be refused.
