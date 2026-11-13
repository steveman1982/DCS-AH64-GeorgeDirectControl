-- GeorgeBridge.lua -- keyboard/UDP control of the AH-64D George AI (device 87).
--
-- Runs in the DCS Export.lua environment. Listens on 127.0.0.1:7788 for
-- single-line text datagrams and drives George's radial menu by issuing
-- performClickableAction() on device 87.
--
-- The current menu mode is never assumed: it is read back from the live George
-- overlay via list_indication(), so a command works no matter which wheel mode
-- or submenu was last used. The read works with the wheel hidden because
-- PrestonAI_page_common.lua carries an off-screen mirror element,
-- mode_text_bridge, that does not inherit show_compass visibility. No command
-- path presses show/hide, so the wheel stays down as it does under VAICOM.
--
-- Protocol (UDP, one command per datagram, reply sent to the sender):
--   cmd <name> [count] [hold=<ms>]
--                        execute a named command, optionally repeating its
--                        press `count` times (for the DEFN cycles) and/or
--                        overriding the hold duration for this call only
--   wheel [show|hide|restore]
--                        idempotent overlay visibility; restore undoes only
--                        what the bridge itself opened
--   mode <NAME>          switch the wheel to that mode and stop
--   press <button> [short|long]   raw button, e.g. "press down long"
--   list [filter]        reply with matching command names
--   labels               reply with the CP/G wheel's current labels
--   tune [key [ms]]      read or set a hold duration (alt, course, hover)
--   state                reply with seat / wheel / CMWS / flight state
--   probe                dump every list_indication() page to the log file
--   eval <lua expr>      evaluate an expression in the export environment
--   cancel               abort the running task (also releases a held button)
--   hold <name> [wheel]  continuous hold: seek the command's mode, press its
--                        button and keep it down. For a tap command (a short
--                        press, e.g. hover_up_10ft or speed_up) the bridge
--                        instead pulses the button every PULSE_PRESS+PULSE_GAP
--                        until released -- the steps keep coming with no
--                        round trip per step.
--                        button and keep it down (e.g. hover_left slides for
--                        as long as it is held). With "wheel", the overlay is
--                        raised before the button goes down and put back the
--                        way it was after release -- the menu button is never
--                        pressed while a George button is held
--   keep                 keep-alive for a hold; without one for HOLD_WATCHDOG
--                        seconds the button is released on its own
--   release              let go of the held button (never sooner than
--                        MIN_HOLD after it went down, so it can't read as a
--                        short press -- a short Left would cycle the wheel)

local GEORGE_DEVICE = 87
local CMWS_DEVICE   = 80   -- devices["CMWS"]; driven directly, bypassing George

local BTN = {
    control = 3001,  -- Request control
    menu    = 3002,  -- Show/hide overlay (long press = open DEFN submenu)
    up      = 3003,
    down    = 3004,
    left    = 3005,  -- short press = cycle wheel mode
    right   = 3006,
    mf      = 3008,  -- Multifunction / centre
    store   = 3009,
}

-- CMWS_commands (command_defs.lua, base 3000).
local CMWS = {
    pwr      = 3001,  -- PWR switch OFF/ON, arg 610
    arm_safe = 3005,  -- flare squibs ARM/SAFE, arg 614
    byp_auto = 3007,  -- operation BYPASS/AUTO, arg 616
}

local HOST, PORT = "127.0.0.1", 7788

-- Timing. DCS treats >0.5 s as a long press (PrestonAI.lua: long_press_time).
--
-- The FLT altitude and course commands are rate-based, not stepped: the
-- PrestonAI.lua constants alt_change_low_speed (50) and
-- course_change_low_speed (10) are per second, and they ramp to
-- alt_change_high_speed (250) and course_change_high_speed (40) once the press
-- runs past course_alt_change_transition_time, 2.0 s. So the amount you get is
-- roughly rate x (hold - long_press_time), and the hold has to stay under 2.0 s
-- to remain on the low rate:
--
--     altitude   700 ms -> 0.2 s x  50 ft/s ~=  10 ft
--     course    1500 ms -> 1.0 s x  10 deg/s ~= 10 deg
--
-- These are tunable at runtime -- "tune alt 750" -- so they can be calibrated
-- against the sim without restarting a mission. "press up hold 700" holds a
-- raw button for an exact duration, which is the quickest way to measure.
local TUNE = {
    alt    = 700,   -- FLT increase/decrease altitude; 10 ft, measured in sim
    course = 1500,  -- FLT come left/right; ~10 degrees
    hover  = 1900,  -- H-B translation: held longer, it travels further
}
local SHORT_HOLD  = 70
local LONG_HOLD   = 900
local POST_PRESS  = 170    -- settle time after releasing a button
local HOLD_WATCHDOG = 1.0  -- s: release a continuous hold with no keep-alive
-- Tap commands held down (H-B 10 ft steps, speed) are pulsed by the bridge
-- itself, frame-timed, for as long as the hold lasts: no round trip per step.
local PULSE_PRESS = 0.07   -- s: each pulse's press, well under long_press_time
local PULSE_GAP   = 0.18   -- s: between pulses ("tune pulse <ms>" changes it)
local MENU_TAP_MS = 70     -- quick menu press used to raise/hide the wheel
local MENU_GAP_MS = 60     -- gap after it before the next button goes down
local RESTORE_GRACE = 0.35 -- s: wait before hiding the wheel after a hold, so a
                           -- quick re-press keeps it up instead of flickering
local MIN_HOLD      = 0.65 -- s: a continuous hold is never shorter than this
local MODE_SETTLE = 330    -- extra settle after a mode change before re-reading
local MAX_MODE_STEPS = 8   -- bound on the mode-seek loop
local UNREACHABLE_CACHE = 5.0  -- s: a mode just found missing from the wheel is refused at once

-- Cockpit argument ids (AH-64D clickabledata.lua).
local ARG_APU           = 406  -- APU, 0 off / 1 on
local ARG_CMWS_PWR      = 610  -- CMWS PWR switch, OFF/ON/TEST
local ARG_CMWS_ARM_SAFE = 614  -- CMWS flare squibs, ARM/SAFE
local ARG_CMWS_BYP_AUTO = 616  -- CMWS operation, BYPASS/AUTO

local AIRBORNE_AGL = 1.5   -- metres; above this we treat the aircraft as flying

-- Wheel mode labels. Keys are the normalised text of the overlay's mode_text
-- element; values are our canonical name. Extend after running "probe".
local MODE_ALIAS = {
    GND = "GND", GROUND = "GND",
    HOV = "HOV", HOVER = "HOV", HB = "HOV", HBOBUP = "HOV", BOBUP = "HOV",
    FLT = "FLT", FLIGHT = "FLT", FLGT = "FLT",
    CMBT = "CMBT", COMBAT = "CMBT", CBT = "CMBT", COMB = "CMBT",
    DEFN = "DEFN", DEF = "DEFN", DEFENSE = "DEFN", DEFENCE = "DEFN",
}

-- ---------------------------------------------------------------- logging

local lfs_ok, lfs = pcall(require, "lfs")
local LOG_PATH = lfs_ok and (lfs.writedir() .. "Logs\\GeorgeBridge.log") or nil
-- Appended, not overwritten, so the previous missions are still there to read
-- when something went wrong; rolled over to GeorgeBridge.old.log past 2 MB.
-- DCS's export environment trims the io library (no file:seek), so the size
-- comes from lfs, and every step is guarded: a logging problem must never stop
-- the bridge from loading.
local function OpenLog()
    if not LOG_PATH then return nil end
    pcall(function()
        local size = lfs.attributes(LOG_PATH, "size")
        if size and size > 2 * 1024 * 1024 then
            local old = LOG_PATH:gsub("%.log$", ".old.log")
            os.remove(old)
            os.rename(LOG_PATH, old)
        end
    end)
    local ok, out = pcall(io.open, LOG_PATH, "a")
    if not ok or not out then
        ok, out = pcall(io.open, LOG_PATH, "w")
        if not ok then out = nil end
    end
    if out then
        pcall(function() out:write("\n======== ", os.date("%Y-%m-%d %H:%M:%S"), " ========\n") end)
    end
    return out
end
local logfile = OpenLog()

local function Log(fmt, ...)
    local msg = select("#", ...) > 0 and string.format(fmt, ...) or tostring(fmt)
    if logfile then
        logfile:write(os.date("%H:%M:%S "), msg, "\n")
        logfile:flush()
    end
    if log and log.write then log.write("GEORGE_BRIDGE", log.INFO, msg) end
end

-- ---------------------------------------------------------------- socket

local socket_ok, socket = pcall(require, "socket")
local sock, reply_to = nil, nil

local function OpenSocket()
    if not socket_ok then
        Log("luasocket unavailable; bridge disabled")
        return
    end
    local s = socket.udp()
    s:setoption("reuseaddr", true)
    local ok, err = s:setsockname(HOST, PORT)
    if not ok then
        Log("cannot bind %s:%d (%s)", HOST, PORT, tostring(err))
        return
    end
    s:settimeout(0)
    sock = s
    Log("listening on %s:%d", HOST, PORT)
end

local function Reply(fmt, ...)
    local msg = select("#", ...) > 0 and string.format(fmt, ...) or tostring(fmt)
    Log("-> %s", msg)
    if sock and reply_to then
        pcall(function() sock:sendto(msg, reply_to.ip, reply_to.port) end)
    end
end

-- ---------------------------------------------------------------- reading

-- list_indication() returns dashed blocks of "name\ntext". Parse to a table.
local function ParseIndication(str)
    if type(str) ~= "string" or #str == 0 then return nil end
    local t, name, value = {}, nil, nil
    local function flush()
        if name then t[name] = value or "" end
        name, value = nil, nil
    end
    for line in ("\n" .. str):gmatch("\n([^\n]*)") do
        if line:match("^%-%-%-%-") then
            flush()
        elseif name == nil then
            if line ~= "" then name = line end
        else
            value = value and (value .. "\n" .. line) or line
        end
    end
    flush()
    if next(t) == nil then return nil end
    return t
end

local indicator_index = nil

local function ReadOverlay()
    if not list_indication then return nil end
    if indicator_index then
        local ok, raw = pcall(list_indication, indicator_index)
        if not ok then return nil end
        local t = ParseIndication(raw)
        if t and (t.mode_text_bridge ~= nil or t.mode_text ~= nil) then return t end
        return nil
    end
    for i = 0, 40 do
        local ok, raw = pcall(list_indication, i)
        if ok and type(raw) == "string"
           and (raw:find("mode_text_bridge", 1, true) or raw:find("mode_text", 1, true)) then
            indicator_index = i
            Log("George overlay found at list_indication(%d)", i)
            return ParseIndication(raw)
        end
    end
    return nil
end

local function NormaliseMode(text)
    if type(text) ~= "string" then return nil end
    local key = text:upper():gsub("[^A-Z0-9]", "")
    if key == "" then return nil end
    return MODE_ALIAS[key] or key
end

-- mode_text_bridge is the off-screen mirror; it reports whether or not the
-- wheel is drawn. mode_text is the unpatched fallback, readable only while the
-- wheel is visible.
local function CurrentMode()
    local overlay = ReadOverlay()
    if not overlay then return nil, false end
    local raw = overlay.mode_text_bridge
    local mirrored = raw ~= nil
    if raw == nil or raw == "" then raw = overlay.mode_text end
    return NormaliseMode(raw), mirrored
end

-- The CP/G wheel's labels. The near ring is each direction's short-press
-- function, the far ring its long-press function. Unlike the pilot wheel, the
-- CP/G wheel changes what a direction does with context -- whether a list is
-- open, which sensor is live -- so a command that is not currently on the wheel
-- would press whatever is there instead. Reading the label first is the only
-- way to know.
local WHEEL_SLOT = {
    up     = { short = "weapon_up",    long = "weapon_far_up"    },
    down   = { short = "weapon_down",  long = "weapon_far_down"  },
    left   = { short = "weapon_left",  long = "weapon_far_left"  },
    right  = { short = "weapon_right", long = "weapon_far_right" },
    centre = { short = "weapon_center" },
}

local BTN_SLOT = { [3003] = "up", [3004] = "down", [3005] = "left",
                   [3006] = "right", [3008] = "centre" }

local function Trim(v)
    if type(v) ~= "string" then return nil end
    v = v:gsub("^%s+", ""):gsub("%s+$", "")
    return v ~= "" and v or nil
end

-- Reads a label through its mirror, falling back to the original element,
-- which is only populated while the wheel is drawn.
local function Label(overlay, element)
    if not overlay or not element then return nil end
    return Trim(overlay[element .. "_bridge"]) or Trim(overlay[element])
end

-- The label currently sitting on the slot a command would press.
local function SlotLabel(button, press)
    local slot = WHEEL_SLOT[BTN_SLOT[button] or ""]
    if not slot then return nil, nil end
    local element = (press == "long" or press == "hold") and slot.long or slot.short
    return Label(ReadOverlay(), element), element
end

-- mode_text (unmirrored) only appears while the wheel is drawn, so its
-- presence is a visibility test -- when it works. In some sessions it never
-- reports the wheel as drawn, not even straight after the bridge raised it,
-- and trusting it then leaves the wheel up after a hold (or hides it on the
-- next one). So the bridge also keeps its own model, flipped by every menu
-- press it makes, and uses the read-back only once it has seen it report
-- "drawn" at least once this session.
local wheel_read_works = false
local wheel_model = false      -- believed visibility, from the bridge's own presses

-- The drawn wheel's own elements (mode_text, compass_*) are not always on the
-- same list_indication page as the bridge's mirrors: in some sessions the
-- mirrors sit on one page and the wheel on the next, which is why reading only
-- the mirror page reported "hidden" all the time. So look for an exact
-- "mode_text" element (not mode_text_bridge) on any page, and remember where
-- it was found. It only exists while the wheel is drawn.
local wheel_page = nil

local function PageHasWheel(i)
    local ok, raw = pcall(list_indication, i)
    return ok and type(raw) == "string" and raw:find("\nmode_text\n", 1, true) ~= nil
end

local function WheelDrawnRead()
    if not list_indication then return false end
    if wheel_page and PageHasWheel(wheel_page) then return true end
    for i = 0, 40 do
        if i ~= wheel_page and PageHasWheel(i) then
            if wheel_page ~= i then Log("George wheel drawn on list_indication(%d)", i) end
            wheel_page = i
            return true
        end
    end
    return false
end

local function WheelDrawn()
    local read = WheelDrawnRead()
    if read then wheel_read_works = true end
    if wheel_read_works then
        wheel_model = read
        return read
    end
    return wheel_model
end

-- Called for every menu-button press the bridge makes.
local function NoteMenuPress(kind)
    if kind == "short" then
        wheel_model = not wheel_model
    elseif kind == "long" then
        wheel_model = true      -- opens the DEFN submenu, which draws the wheel
    end
end

local function Arg(id)
    if not GetDevice then return nil end
    local ok, dev = pcall(GetDevice, 0)
    if not ok or not dev then return nil end
    local okv, v = pcall(function() return dev:get_argument_value(id) end)
    if okv then return v end
    return nil
end

local function AGL()
    if not LoGetAltitudeAboveGroundLevel then return nil end
    local ok, v = pcall(LoGetAltitudeAboveGroundLevel)
    if ok and type(v) == "number" then return v end
    return nil
end

local function Airborne()
    local agl = AGL()
    if agl == nil then return nil end
    return agl > AIRBORNE_AGL
end

-- Which seat the player occupies. The AH-64D exposes it as the cockpit
-- parameter SEAT: 0 = pilot (George runs the CP/G station), 1 = CP/G (George
-- flies). VAICOM reads the same parameter. get_param_handle may not exist in
-- the export environment, so fall back to parsing list_cockpit_params.
local SEAT_NAME = { [0] = "pilot", [1] = "cpg" }

local function SeatParam()
    if get_param_handle then
        local ok, handle = pcall(get_param_handle, "SEAT")
        if ok and handle then
            local okv, v = pcall(function() return handle:get() end)
            if okv and type(v) == "number" then return v end
        end
    end
    if list_cockpit_params then
        local ok, raw = pcall(list_cockpit_params)
        if ok and type(raw) == "string" then
            local v = raw:match("SEAT:%s*(%-?[%d%.]+)")
            if v then return tonumber(v) end
        end
    end
    return nil
end

-- "pilot", "cpg", or nil when the parameter cannot be read.
local function Seat()
    local v = SeatParam()
    if v == nil then return nil end
    return SEAT_NAME[math.floor(v + 0.5)]
end

local function IsApache()
    if not LoGetSelfData then return true end
    local ok, self_data = pcall(LoGetSelfData)
    if not ok or not self_data or not self_data.Name then return true end
    return self_data.Name:find("AH%-64D") ~= nil
end

local function Now()
    if LoGetModelTime then
        local ok, t = pcall(LoGetModelTime)
        if ok and type(t) == "number" then return t end
    end
    return os.clock()
end

-- ---------------------------------------------------------------- commands
--
-- Transcribed from VAICOM-Community
-- VAICOM/Extensions/AICPG/CPGCommandHandler.cs. Two groups:
--
--   PILOT group  -- what you command from the CP/G seat, where George flies.
--                   Each entry names the wheel mode it lives in; the bridge
--                   cycles to that mode first, reading back as it goes.
--   CPG group    -- what you command from the PILOT seat, where George runs
--                   the TADS and weapons. These have no mode gating at all,
--                   so they are plain presses.
--
-- press: "short", "long" (held 900 ms, past DCS's 0.5 s threshold), "hold"
-- (held for TUNE[hold] ms in total, VAICOM's AddGeorgeLongHoldButton), or
-- "action" (a single value 1.0 with no release, VAICOM's AddGeorgeAction).

local function M(desc, mode, button, press, opts)
    local c = opts or {}
    c.desc, c.mode, c.button, c.press = desc, mode, button, press
    return c
end

local function R(desc, button, press, opts)
    local c = opts or {}
    c.desc, c.button, c.press = desc, button, press
    return c
end

local function D(desc, button, press, opts)
    local c = opts or {}
    c.desc, c.button, c.press, c.defn = desc, button, press, true
    return c
end

local function PANEL(desc, command, value, arg)
    return { desc = desc,
             set = { device = CMWS_DEVICE, command = command, value = value, arg = arg } }
end

local function SEQ(desc, steps) return { desc = desc, sequence = steps } end

local COMMANDS = {

    -- ============================================================ raw wheel
    -- Either seat. No mode handling, exactly as pressing the hat yourself.
    show_hide        = R("Show/hide the George overlay",   BTN.menu,    "short"),
    up               = R("Wheel up",                       BTN.up,      "short"),
    down             = R("Wheel down",                     BTN.down,    "short"),
    left             = R("Wheel left (cycles mode)",       BTN.left,    "short"),
    right            = R("Wheel right",                    BTN.right,   "short"),
    center           = R("Wheel select (centre)",          BTN.mf,      "short"),
    up_long          = R("Wheel up, held",                 BTN.up,      "long"),
    down_long        = R("Wheel down, held",               BTN.down,    "long"),
    left_long        = R("Wheel left, held",               BTN.left,    "long"),
    right_long       = R("Wheel right, held",              BTN.right,   "long"),
    center_long      = R("Wheel select, held",             BTN.mf,      "long"),
    control_request  = R("Request control",                BTN.control, "action"),
    store_target     = R("Store target",                   BTN.store,   "action"),

    -- ========================================================= menu modes
    mode_ground      = { desc = "Select GND mode",  seek = "GND"  },
    mode_hover       = { desc = "Select H-B mode",  seek = "HOV"  },
    mode_flight      = { desc = "Select FLT mode",  seek = "FLT"  },
    mode_combat      = { desc = "Select CMBT mode", seek = "CMBT" },
    mode_next        = R("Step to the next wheel mode", BTN.left, "short"),
    mode_defense     = { desc = "Open the DEFN submenu", defn_open = true },

    -- ===================================================== PILOT: GND menu
    apu_start        = M("Start the APU",              "GND", BTN.up,    "short",
                         { skip_if = { arg = ARG_APU, equals = 1 }, verify = { arg = ARG_APU, want = 1 } }),
    apu_stop         = M("Stop the APU",               "GND", BTN.up,    "short",
                         { skip_if = { arg = ARG_APU, equals = 0 }, verify = { arg = ARG_APU, want = 0 } }),
    apu_only         = M("APU only",                   "GND", BTN.down,  "short"),
    shutdown_engines = M("Shut down the engines",      "GND", BTN.down,  "short"),
    start_engines_idle = M("Start engines to idle",    "GND", BTN.right, "short"),
    start_engines_fly  = M("Start engines to fly",     "GND", BTN.up,    "short"),
    startup_full     = M("Full start-up",              "GND", BTN.up,    "long"),
    shutdown_full    = M("Full shutdown",              "GND", BTN.down,  "long"),
    -- GND long-Right toggles CMWS power. The silent cmws_off / cmws_on below
    -- do the same thing directly and absolutely; these ask George.
    cmws_power_toggle_george = M("Toggle CMWS power via George", "GND", BTN.right, "long",
                         { verify = { arg = ARG_CMWS_PWR } }),
    takeoff          = M("Take off",                   "GND", BTN.mf,    "short",
                         { modes = { "GND", "HOV", "DEFN" }, require_ground = true }),

    -- ===================================================== PILOT: FLT menu
    speed_up         = M("Speed up",                   "FLT", BTN.up,    "short"),
    slow_down        = M("Slow down",                  "FLT", BTN.down,  "short"),
    follow_waypoints = M("Follow waypoints",           "FLT", BTN.right, "short"),
    set_airspeed_ref = M("Set airspeed reference",     "FLT", BTN.mf,    "short"),
    set_groundspeed_ref = M("Set groundspeed reference", "FLT", BTN.mf,  "short"),
    -- One low-rate step each: 50 ft of altitude, 10 degrees of course. Pass a
    -- count for more, e.g. "cmd increase_altitude 3" for 150 ft.
    -- Rate-based, so the step size is the hold duration: see TUNE above.
    -- Pass a count for multiples, e.g. "cmd increase_altitude 3" for 30 ft.
    increase_altitude = M("Increase altitude 10 ft"  , "FLT", BTN.up,    "hold", { hold = "alt" }),
    decrease_altitude = M("Decrease altitude 10 ft"  , "FLT", BTN.down,  "hold", { hold = "alt" }),
    come_left        = M("Come left 10 degrees",         "FLT", BTN.left,  "hold", { hold = "course" }),
    come_right       = M("Come right 10 degrees",        "FLT", BTN.right, "hold", { hold = "course" }),
    set_radar_altitude = M("Set radar altitude hold",  "FLT", BTN.mf,    "long"),
    set_barometric_altitude = M("Set barometric altitude hold", "FLT", BTN.mf, "long"),

    -- ==================================================== PILOT: CMBT menu
    align_to_tads    = M("Align to TADS",              "CMBT", BTN.up,    "short"),
    align_to_nts     = M("Align to NTS",               "CMBT", BTN.up,    "short"),
    hold_position    = M("Hold position",              "CMBT", BTN.down,  "short"),
    return_to_battle_position = M("Return to battle position", "CMBT", BTN.down, "short"),
    turn_to_ghs      = M("Turn to GHS",                "CMBT", BTN.right, "short"),
    orbit_overhead   = M("Orbit overhead",             "CMBT", BTN.up,    "long"),
    break180         = M("Break 180",                  "CMBT", BTN.down,  "long"),
    break_left       = M("Break left",                 "CMBT", BTN.left,  "long"),
    break_right      = M("Break right",                "CMBT", BTN.right, "long"),
    -- Add/delete share one long MF press; VAICOM presses it twice to replace an
    -- existing battle position, which needs state we do not read. One press.
    battle_position_toggle = M("Add or delete a battle position", "CMBT", BTN.mf, "long"),
    mask_position    = M("Mask position",              "CMBT", BTN.mf,    "short",
                         { modes = { "CMBT", "HOV", "DEFN" }, require_airborne = true }),

    -- ===================================================== PILOT: H-B menu
    hover_up_10ft    = M("Come up 10 feet",            "HOV", BTN.up,    "short"),
    hover_down_10ft  = M("Come down 10 feet",          "HOV", BTN.down,  "short"),
    hover_forward    = M("Move forwards",              "HOV", BTN.up,    "hold", { hold = "hover" }),
    hover_back       = M("Move back",                  "HOV", BTN.down,  "hold", { hold = "hover" }),
    hover_left       = M("Move left",                  "HOV", BTN.left,  "hold", { hold = "hover" }),
    hover_right      = M("Move right",                 "HOV", BTN.right, "hold", { hold = "hover" }),

    -- ==================================================== PILOT: DEFN menu
    -- Toggles, so each reads its cockpit argument first and presses nothing if
    -- the switch is already where it should be.
    cmws_safe_george   = D("CMWS safe, via George",    BTN.up,   "short",
                           { arg = ARG_CMWS_ARM_SAFE, want = 0 }),
    cmws_arm_george    = D("CMWS arm, via George",     BTN.up,   "short",
                           { arg = ARG_CMWS_ARM_SAFE, want = 1 }),
    cmws_bypass_george = D("CMWS bypass, via George",  BTN.down, "short",
                           { arg = ARG_CMWS_BYP_AUTO, want = 0 }),
    cmws_auto_george   = D("CMWS auto, via George",    BTN.down, "short",
                           { arg = ARG_CMWS_BYP_AUTO, want = 1 }),
    threat_warnings_toggle = D("Toggle threat warnings", BTN.down, "long"),
    -- The remaining DEFN items are cycles with no cockpit argument behind them,
    -- so there is nothing to read back and absolute selection is not possible
    -- yet. Each press advances one step; pass a count, e.g. "cmd evade_next 3".
    roe_next         = D("Next rules of engagement (hold / return / free)", BTN.up,    "long"),
    evade_next       = D("Next evade mode (off / level / vertical / mask)",  BTN.left,  "long"),
    dispense_next    = D("Next dispense mode (none / chaff / flares / both)", BTN.right, "long"),
    extlights_next   = D("Next exterior light setting",                      BTN.right, "short"),

    -- ============================================== CMWS panel, direct
    -- Not George at all: absolute switch positions on device 80. Idempotent,
    -- silent, seat-independent, confirmed from the cockpit argument.
    cmws_safe   = PANEL("CMWS flare squibs SAFE", CMWS.arm_safe, 0.0, ARG_CMWS_ARM_SAFE),
    cmws_arm    = PANEL("CMWS flare squibs ARM",  CMWS.arm_safe, 1.0, ARG_CMWS_ARM_SAFE),
    cmws_off    = PANEL("CMWS power OFF",         CMWS.pwr,      0.0, ARG_CMWS_PWR),
    cmws_on     = PANEL("CMWS power ON",          CMWS.pwr,      1.0, ARG_CMWS_PWR),
    cmws_bypass = PANEL("CMWS BYPASS",            CMWS.byp_auto, 0.0, ARG_CMWS_BYP_AUTO),
    cmws_auto   = PANEL("CMWS AUTO",              CMWS.byp_auto, 1.0, ARG_CMWS_BYP_AUTO),

    -- ================================================== CPG: George as CP/G
    -- From the PILOT seat. No mode gating; the CP/G wheel has none.
    cpg_startup          = R("George start-up",            BTN.mf,    "long"),
    cpg_shutdown         = R("George shutdown",            BTN.mf,    "long"),
    cpg_area_search      = R("Area search",                BTN.mf,    "long"),
    cpg_point_search     = R("Point search",               BTN.mf,    "short"),
    cpg_phs_search       = R("PHS search",                 BTN.up,    "short"),
    cpg_tads_los         = R("TADS line of sight",         BTN.down,  "long"),
    cpg_area_select      = R("Area select",                BTN.left,  "long"),
    cpg_point_select     = R("Point select",               BTN.right, "long"),
    cpg_tads_sensor      = R("Cycle TADS sensor",          BTN.mf,    "long"),
    cpg_tads_fov         = R("Cycle TADS field of view",   BTN.mf,    "short"),
    cpg_tads_zoom_in     = R("TADS zoom in",               BTN.up,    "long", { expect = "ZOOM" }),
    cpg_tads_zoom_out    = R("TADS zoom out",              BTN.down,  "long", { expect = "ZOOM" }),
    cpg_adjust_aim       = R("Adjust aim",                 BTN.mf,    "long"),
    cpg_next_target      = R("Next target",                BTN.down,  "short"),
    cpg_previous_target  = R("Previous target",            BTN.up,    "short"),
    cpg_next_item        = R("Next list item",             BTN.down,  "short"),
    cpg_previous_item    = R("Previous list item",          BTN.up,    "short"),
    cpg_list_item_select = R("Select list item",           BTN.right, "short"),
    cpg_exit_list        = R("Exit list",                  BTN.left,  "short"),
    cpg_target_list_zoom_in  = R("Target list zoom in",    BTN.up,    "long", { expect = "ZOOM" }),
    cpg_target_list_zoom_out = R("Target list zoom out",   BTN.down,  "long", { expect = "ZOOM" }),
    cpg_target_list_filter   = R("Target list filter",     BTN.left,  "long"),
    cpg_point_list_filter_mode   = R("Point list filter mode",   BTN.left,  "long"),
    cpg_point_list_filter_threat = R("Point list filter threat", BTN.right, "long"),
    cpg_select_target    = R("Select target",              BTN.mf,    "short"),
    cpg_track_target     = R("Track target",               BTN.right, "short"),
    cpg_last_found_target = R("Last found target",         BTN.down,  "long"),
    cpg_last_stored_target = R("Last stored target",       BTN.mf,    "short"),
    cpg_lase_target      = R("Lase target",                BTN.right, "short"),
    cpg_laser_on         = R("Laser on",                   BTN.right, "short"),
    cpg_laser_off        = R("Laser off",                  BTN.right, "short"),
    cpg_cleared_fire     = R("Cleared to fire",            BTN.mf,    "short"),
    cpg_weapons_free     = R("Weapons free",               BTN.up,    "long"),
    cpg_hold_fire        = R("Hold fire",                  BTN.up,    "long"),
    cpg_next_weapon      = R("Next weapon",                BTN.left,  "short"),
    cpg_next_missile     = R("Next missile type",          BTN.left,  "long"),
    cpg_next_rocket      = R("Next rocket type",           BTN.left,  "long"),
    cpg_missile_traj     = R("Missile trajectory",         BTN.right, "long"),
    cpg_burst_limit      = R("Gun burst limit",            BTN.right, "short"),
    cpg_rocket_quantity  = R("Rocket quantity",            BTN.right, "short"),
    cpg_lobl             = R("LOBL",                       BTN.right, "short"),
    cpg_loal             = R("LOAL",                       BTN.right, "short"),

    -- CP/G macros, button-for-button from CPGCommandHandler.cs. Each step is
    -- {button, press, post-delay ms}; the trailing menu press hides the list.
    cpg_macro_next_search = SEQ("Next area search", {
        { BTN.left,  "long",  120 }, { BTN.down,  "short", 80 },
        { BTN.right, "short", 80  }, { BTN.menu,  "short", 0  } }),
    cpg_macro_previous_search = SEQ("Previous area search", {
        { BTN.left,  "long",  200 }, { BTN.up,    "short", 150 },
        { BTN.right, "short", 80  }, { BTN.menu,  "short", 0   } }),
    cpg_macro_next_point = SEQ("Next point search", {
        { BTN.right, "long",  200 }, { BTN.down,  "short", 150 },
        { BTN.right, "short", 80  }, { BTN.menu,  "short", 0   } }),
    cpg_macro_previous_point = SEQ("Previous point search", {
        { BTN.right, "long",  200 }, { BTN.up,    "short", 150 },
        { BTN.right, "short", 80  }, { BTN.menu,  "short", 0   } }),
    cpg_macro_track_engage = SEQ("Track and engage current target", {
        { BTN.right, "short", 100 }, { BTN.mf,    "short", 100 } }),
    cpg_macro_add_two_targets = SEQ("Add and track the top 2 targets", {
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.right, "short", 0  } }),
    cpg_macro_add_three_targets = SEQ("Add and track the top 3 targets", {
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.right, "short", 0  } }),
    cpg_macro_add_four_targets = SEQ("Add and track the top 4 targets", {
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.down, "short", 100 },
        { BTN.mf,   "short", 100 }, { BTN.right, "short", 0  } }),
}

-- Seat gating. A command that lives in George's pilot wheel means nothing from
-- the pilot seat and vice versa -- sending it anyway would press whatever that
-- button does in the other wheel. Derive the requirement instead of annotating
-- 122 entries: anything with a wheel mode or a DEFN route is commanded from the
-- CP/G seat, and anything named cpg_* is commanded from the pilot seat. Raw
-- wheel presses and the CMWS panel work from either.
for name, spec in pairs(COMMANDS) do
    if spec.seat == nil then
        if name:sub(1, 4) == "cpg_" then
            spec.seat = "pilot"
        elseif spec.mode or spec.defn or spec.seek or spec.defn_open then
            spec.seat = "cpg"
        end
    end
end

local BUTTON_BY_NAME = {
    control = BTN.control, menu = BTN.menu, up = BTN.up, down = BTN.down,
    left = BTN.left, right = BTN.right, mf = BTN.mf, centre = BTN.mf,
    center = BTN.mf, store = BTN.store,
}

-- ---------------------------------------------------------------- task runner

-- Commands run as a coroutine so they can read state between button presses.
-- Yielding a number parks the task for that many milliseconds.
local task, wake_at, task_name = nil, 0, nil

local function Wait(ms) coroutine.yield(ms) end

local function RawPress(device, command, value)
    local ok, dev = pcall(GetDevice, device)
    if not ok or not dev or not dev.performClickableAction then
        return false, string.format("device %d unavailable", device)
    end
    local done, err = pcall(function() dev:performClickableAction(command, value) end)
    if not done then return false, tostring(err) end
    return true
end

-- ---------------------------------------------------------------- continuous hold
-- One George button at a time can be held down across frames. The frame hook
-- releases it once a release has been asked for and MIN_HOLD has passed, or
-- when keep-alives stop arriving (client crashed, socket lost).

local held = nil   -- { button, name, since, deadline, release_at }
local hold_restore_pending = false   -- hide the wheel the bridge raised, once released
local hold_restore_at = 0

-- True while the wheel is up because the bridge raised it, not the pilot.
-- Shared by "wheel show/restore" and wheel-raising holds, and kept across
-- back-to-back holds, so whoever comes last still knows to put it away.
local wheel_opened_by_bridge = false

local function ReleaseHeldNow(reason)
    if not held then return end
    if not held.pulse or held.down then RawPress(GEORGE_DEVICE, held.button, 0.0) end
    Log("released %s after %.2f s%s (%s)", held.name, Now() - held.since,
        held.pulse and string.format(", %d pulses", held.pulses) or "", reason)
    if held.wheel and wheel_opened_by_bridge then
        hold_restore_pending = true
        hold_restore_at = Now() + RESTORE_GRACE
    end
    held = nil
end

local function RequestRelease()
    if not held then return false end
    if held.pulse then
        held.release_at = Now()          -- short presses are the point here
    else
        held.release_at = math.max(Now(), held.since + MIN_HOLD)
    end
    return true
end

local function PumpPulse(t)
    if held.down then
        if t < held.next_at then return end      -- let every pulse be a full tap
        RawPress(GEORGE_DEVICE, held.button, 0.0)
        held.down, held.next_at = false, t + PULSE_GAP
        held.pulses = held.pulses + 1
        if held.release_at then ReleaseHeldNow("released") end
        return
    end
    if held.release_at then ReleaseHeldNow("released") return end
    if t > held.deadline then ReleaseHeldNow("watchdog: no keep-alive") return end
    if t >= held.next_at then
        RawPress(GEORGE_DEVICE, held.button, 1.0)
        held.down, held.next_at = true, t + PULSE_PRESS
    end
end

local function PumpHold()
    if not held then return end
    local t = Now()
    if held.pulse then return PumpPulse(t) end
    if held.release_at and t >= held.release_at then
        ReleaseHeldNow("released")
    elseif t > held.deadline then
        ReleaseHeldNow("watchdog: no keep-alive")
    end
end

local function Press(button, kind, hold_ms)
    if button == BTN.menu then NoteMenuPress(kind) end
    if kind == "action" then
        local ok, err = RawPress(GEORGE_DEVICE, button, 1.0)
        if not ok then error("action failed: " .. tostring(err), 0) end
        Wait(POST_PRESS)
        return
    end
    local hold = SHORT_HOLD
    if kind == "long" then hold = LONG_HOLD
    elseif kind == "hold" then
        hold = tonumber(hold_ms) or TUNE[hold_ms or ""] or TUNE.hover
    end

    local ok, err = RawPress(GEORGE_DEVICE, button, 1.0)
    if not ok then error("press failed: " .. tostring(err), 0) end
    Wait(hold)
    RawPress(GEORGE_DEVICE, button, 0.0)
    Wait(POST_PRESS)
end

-- Raise or hide the wheel quickly. Nothing is read back afterwards (the
-- visibility model tracks it), so there is no settle wait: just long enough a
-- tap to register and a short gap before the next button.
local function MenuTap()
    NoteMenuPress("short")
    RawPress(GEORGE_DEVICE, BTN.menu, 1.0)
    Wait(MENU_TAP_MS)
    RawPress(GEORGE_DEVICE, BTN.menu, 0.0)
    Wait(MENU_GAP_MS)
end

-- Cycle the wheel with short Left presses until the mode reports the target.
-- Reading back after every press means no assumption about mode ordering, about
-- which modes the current flight state offers, or about whether the DEFN
-- submenu was left open. Nothing here presses the menu button, so the wheel is
-- never raised: George processes hat commands whether or not it is drawn.
--
-- Stops as soon as the wheel comes round to a mode it has already shown: a full
-- cycle without the target means this flight state doesn't offer it (H-B above
-- ~10 kt, GND in the air), and the wheel is already back where it started. That
-- target is then refused instantly for UNREACHABLE_CACHE seconds, so repeated
-- presses of a wrong-mode command don't each cost another full cycle.
local unreachable = {}   -- target mode -> model time until which it's refused

local function SeekMode(target)
    local until_t = unreachable[target]
    if until_t and Now() < until_t then
        return false, CurrentMode(), true
    end
    local seen = {}
    for _ = 1, MAX_MODE_STEPS do
        local mode = CurrentMode()
        if mode == target then
            unreachable[target] = nil
            return true, mode
        end
        if mode ~= nil then
            if seen[mode] then
                unreachable[target] = Now() + UNREACHABLE_CACHE
                return false, mode
            end
            seen[mode] = true
        end
        Press(BTN.left, "short")
        Wait(MODE_SETTLE)
    end
    return false, CurrentMode()
end

local function InList(value, list)
    if not list then return false end
    for _, v in ipairs(list) do if v == value then return true end end
    return false
end

-- Absolute panel switch, straight to the owning device. No George, no menu.
local function RunSetCommand(name, set)
    local before = Arg(set.arg)
    local ok, err = RawPress(set.device, set.command, set.value)
    if not ok then
        Reply("error %s: %s", name, tostring(err))
        return
    end
    Wait(400)
    local after = Arg(set.arg)
    Reply("ok %s device=%d command=%d value=%s arg%d %s->%s",
          name, set.device, set.command, tostring(set.value), set.arg,
          tostring(before), tostring(after))
end

-- DEFN submenu: long-press the menu button to open it, press the item, short
-- press to close. The menu button is also show/hide, so opening DEFN may leave
-- the wheel drawn; we note whether it was drawn beforehand and put that back.
local function RunDefnCommand(name, spec, count)
    local before = spec.arg and Arg(spec.arg) or nil
    if before ~= nil and spec.want ~= nil and before == spec.want then
        Reply("ok %s: already arg%d=%s, George not asked", name, spec.arg, tostring(before))
        return
    end

    local drawn_before = WheelDrawn()

    Press(BTN.menu, "long")
    Wait(MODE_SETTLE)
    local mode = CurrentMode()
    if mode ~= "DEFN" then
        Log("warning: %s expected DEFN after long menu press, mode=%s", name, tostring(mode))
    end

    for _ = 1, count do
        Press(spec.button, spec.press, spec.hold_ms or spec.hold)
        Wait(120)
    end
    Wait(MODE_SETTLE)

    Press(BTN.menu, "short")
    Wait(MODE_SETTLE)

    -- Restore the overlay to the visibility we found it in.
    local restored = true
    if WheelDrawn() ~= drawn_before then
        Press(BTN.menu, "short")
        Wait(MODE_SETTLE)
        restored = (WheelDrawn() == drawn_before)
    end

    Wait(400)
    if spec.arg then
        Reply("ok %s via DEFN x%d arg%d %s->%s mode=%s wheel_drawn=%s%s",
              name, count, spec.arg, tostring(before), tostring(Arg(spec.arg)),
              tostring(CurrentMode()), tostring(WheelDrawn()),
              restored and "" or " (could not restore wheel visibility)")
    else
        Reply("ok %s via DEFN x%d mode=%s wheel_drawn=%s%s",
              name, count, tostring(CurrentMode()), tostring(WheelDrawn()),
              restored and "" or " (could not restore wheel visibility)")
    end
end

local function RunSequence(name, steps)
    for _, step in ipairs(steps) do
        Press(step[1], step[2])
        if step[3] and step[3] > 0 then Wait(step[3]) end
    end
    Reply("ok %s (%d steps)", name, #steps)
end

-- A CP/G command only does what its name says while that function is on the
-- wheel; the same slot carries something else in other contexts (Up long is
-- zoom with the TADS up, weapons free/hold fire elsewhere). Commands with an
-- "expect" word are pressed only when the slot's label contains it, and
-- refused -- nothing pressed -- otherwise, including when the label can't be
-- read. Every CP/G press logs the label it hit, to learn the exact wording.
local function CheckExpect(name, spec)
    if not spec.button or not BTN_SLOT[spec.button] then return true end
    local label, element = SlotLabel(spec.button, spec.press)
    if name:sub(1, 4) == "cpg_" then
        Log("label %s: %s=%q", name, tostring(element), tostring(label))
    end
    if not spec.expect then return true end
    if label == nil then
        Reply("error %s: no %s option on the wheel right now (%s is empty), not pressing",
              name, type(spec.expect) == "table" and spec.expect[1] or spec.expect, tostring(element))
        return false
    end
    local want = type(spec.expect) == "table" and spec.expect or { spec.expect }
    for _, w in ipairs(want) do
        if label:upper():find(w:upper(), 1, true) then return true end
    end
    Reply("error %s: wheel is offering %q there, not %s -- not available right now",
          name, label, table.concat(want, "/"))
    return false
end

local function RunNamedCommand(name, spec, count, hold_override)
    if not IsApache() then
        Reply("error %s: not flying an AH-64D", name)
        return
    end

    if spec.seat then
        local seat = Seat()
        if seat == nil then
            Log("warning: %s could not read the SEAT parameter, gate skipped", name)
        elseif seat ~= spec.seat then
            Reply("error %s: needs the %s seat, you are in the %s seat", name, spec.seat, seat)
            return
        end
    end

    if not CheckExpect(name, spec) then return end

    if spec.set then return RunSetCommand(name, spec.set) end
    if spec.sequence then return RunSequence(name, spec.sequence) end

    if spec.skip_if then
        local v = Arg(spec.skip_if.arg)
        if v ~= nil and v == spec.skip_if.equals then
            Reply("ok %s: already arg%d=%s, nothing sent", name, spec.skip_if.arg, tostring(v))
            return
        end
    end

    if spec.require_airborne or spec.require_ground then
        local flying = Airborne()
        if flying == nil then
            Log("warning: %s could not read AGL, gate skipped", name)
        elseif spec.require_airborne and not flying then
            Reply("error %s: needs to be airborne (agl=%.1f m)", name, AGL() or 0)
            return
        elseif spec.require_ground and flying then
            Reply("error %s: needs to be on the ground (agl=%.1f m)", name, AGL() or 0)
            return
        end
    end

    if spec.defn_open then
        Press(BTN.menu, "long")
        Wait(MODE_SETTLE)
        Reply("ok mode_defense mode=%s wheel_drawn=%s", tostring(CurrentMode()), tostring(WheelDrawn()))
        return
    end

    if spec.defn then return RunDefnCommand(name, spec, count) end

    local mode, mirrored = CurrentMode()
    if spec.seek or spec.mode then
        if mode == nil then
            Reply("error %s: menu mode unreadable -- %s", name,
                  mirrored and "mode_text_bridge is empty"
                            or "mode_text_bridge missing, is PrestonAI_page_common.lua patched?")
            return
        end
    end

    if spec.seek then
        local reached, now_mode = SeekMode(spec.seek)
        if reached then Reply("ok %s mode=%s", name, spec.seek)
        else Reply("error %s: mode %s not reachable from %s (now %s)",
                   name, spec.seek, mode, tostring(now_mode)) end
        return
    end

    if spec.mode and not InList(mode, spec.modes) then
        local start_mode = mode
        local reached, now_mode = SeekMode(spec.mode)
        if not reached then
            -- Put the wheel back where we found it rather than leaving it
            -- parked somewhere the pilot did not ask for.
            SeekMode(start_mode)
            Reply("error %s: mode %s not reachable from %s (now %s) -- not offered in this flight state",
                  name, spec.mode, start_mode, tostring(now_mode))
            return
        end
    end

    -- A caller may override the hold for one call, which is how the HOTAS
    -- ramp grows its step size. Meaningless for short presses, so ignored
    -- there rather than silently doing nothing different.
    local hold_arg = spec.hold_ms or spec.hold
    if hold_override and spec.press == "hold" then hold_arg = hold_override end

    for _ = 1, count do
        Press(spec.button, spec.press, hold_arg)
        if count > 1 then
            Wait(spec.press == "short" and 120 or 250)
        end
    end

    if spec.verify then
        Wait(500)
        Reply("ok %s mode=%s arg%d=%s%s", name, tostring(CurrentMode()),
              spec.verify.arg, tostring(Arg(spec.verify.arg)),
              mirrored and "" or " (wheel-visible read only)")
    else
        Reply("ok %s%s%s", name,
              spec.mode and (" mode=" .. spec.mode) or "",
              mirrored and "" or " (wheel-visible read only)")
    end
end

-- Same gating as RunNamedCommand (aircraft, seat, mode), then the button goes
-- down and stays down. A release that arrives while the mode is still being
-- sought is honoured as soon as the button has been down for MIN_HOLD.
local hold_release_pending = false

local function RunHoldCommand(name, spec, want_wheel)
    if not IsApache() then
        Reply("error %s: not flying an AH-64D", name)
        return
    end
    local pulse = spec.press == "short" and not spec.defn and not spec.set
                  and not spec.sequence and not spec.seek
    if not spec.button or not (pulse or spec.press == "hold" or spec.press == "long") then
        Reply("error hold %s: not a held command (press=%s)", name, tostring(spec.press))
        return
    end
    if not CheckExpect(name, spec) then return end
    if spec.seat then
        local seat = Seat()
        if seat ~= nil and seat ~= spec.seat then
            Reply("error %s: needs the %s seat, you are in the %s seat", name, spec.seat, seat)
            return
        end
    end
    if spec.mode then
        local mode, mirrored = CurrentMode()
        if mode == nil then
            Reply("error %s: menu mode unreadable", name)
            return
        end
        if not InList(mode, spec.modes) then
            local reached, now_mode = SeekMode(spec.mode)
            if not reached then
                SeekMode(mode)
                Reply("error %s: mode %s not reachable from %s (now %s) -- not offered in this flight state",
                      name, spec.mode, mode, tostring(now_mode))
                return
            end
        end
    end

    -- Raise the wheel first, while no George button is down. If it is already
    -- up because a previous hold raised it, take over that hold's pending hide
    -- instead of mistaking the wheel for one the pilot opened.
    if want_wheel then
        hold_restore_pending = false
        if not WheelDrawn() then
            MenuTap()
            wheel_opened_by_bridge = true   -- we pressed it; don't trust a slow read-back
        end
    end

    local ok, err = RawPress(GEORGE_DEVICE, spec.button, 1.0)
    if not ok then
        Reply("error hold %s: %s", name, tostring(err))
        return
    end
    local t = Now()
    held = { button = spec.button, name = name, since = t, deadline = t + HOLD_WATCHDOG,
             wheel = want_wheel }
    if pulse then
        held.pulse, held.down, held.next_at, held.pulses = true, true, t + PULSE_PRESS, 0
    end
    if hold_release_pending then RequestRelease() end
    hold_release_pending = false
    Reply("ok hold %s%s%s", name, spec.mode and (" mode=" .. spec.mode) or "", pulse and " pulse" or "")
end

local function StartTask(name, fn)
    if task then
        Reply("busy %s", tostring(task_name))
        return
    end
    task_name = name
    task = coroutine.create(fn)
    wake_at = 0
end

local function PumpTask()
    if not task then return end
    if Now() < wake_at then return end
    local ok, result = coroutine.resume(task)
    if not ok then
        Reply("error %s: %s", tostring(task_name), tostring(result))
        task, task_name = nil, nil
        return
    end
    if coroutine.status(task) == "dead" then
        task, task_name = nil, nil
        return
    end
    wake_at = Now() + (tonumber(result) or 0) / 1000
end

-- After a hold that raised the wheel is released, hide it again -- but only
-- once no other task is running, and only if it is still drawn.
local function PumpHoldRestore()
    if not hold_restore_pending or task or held or Now() < hold_restore_at then return end
    hold_restore_pending = false
    StartTask("wheel restore after hold", function()
        Wait(MENU_GAP_MS)
        if wheel_opened_by_bridge and WheelDrawn() then
            MenuTap()
        end
        wheel_opened_by_bridge = false
        Log("wheel restored after hold, drawn=%s", tostring(WheelDrawn()))
    end)
end

-- ---------------------------------------------------------------- handlers

local function DoProbe()
    if not list_indication then
        Reply("error probe: list_indication unavailable")
        return
    end
    Log("---- probe: list_indication(0..40) ----")
    local found = {}
    for i = 0, 40 do
        local ok, raw = pcall(list_indication, i)
        if ok and type(raw) == "string" and #raw > 0 then
            found[#found + 1] = i
            Log("=== list_indication(%d) ===\n%s", i, raw)
        end
    end
    Log("---- args: apu406=%s cmws610=%s cmws614=%s cmws616=%s agl=%s ----",
        tostring(Arg(ARG_APU)), tostring(Arg(ARG_CMWS_PWR)),
        tostring(Arg(ARG_CMWS_ARM_SAFE)), tostring(Arg(ARG_CMWS_BYP_AUTO)), tostring(AGL()))
    Reply("ok probe: non-empty pages [%s] -> %s", table.concat(found, ","), tostring(LOG_PATH))
end

local function DoState()
    local mode, mirrored = CurrentMode()
    Reply('state seat=%s seat_param=%s indicator=%s mode=%s mirror=%s wheel_drawn=%s agl=%s apu=%s cmws_pwr=%s cmws_arm=%s cmws_auto=%s busy=%s',
          tostring(Seat() or "unknown"), tostring(SeatParam()),
          tostring(indicator_index), tostring(mode), tostring(mirrored),
          tostring(WheelDrawn()) .. " wheel_read=" .. tostring(WheelDrawnRead()) .. " read_works="
              .. tostring(wheel_read_works), tostring(AGL()), tostring(Arg(ARG_APU)),
          tostring(Arg(ARG_CMWS_PWR)), tostring(Arg(ARG_CMWS_ARM_SAFE)),
          tostring(Arg(ARG_CMWS_BYP_AUTO)), tostring(task_name))
end

-- Wheel visibility, kept idempotent so a caller never has to guess which way a
-- toggle will go. "show" presses only if the wheel is down and remembers that
-- it did; "restore" undoes only what the bridge itself opened. Used by the
-- HOTAS ramp to put the wheel up as feedback while a setting is being driven.

local function DoWheel(what)
    if what == "" then
        Reply("ok wheel drawn=%s opened_by_bridge=%s",
              tostring(WheelDrawn()), tostring(wheel_opened_by_bridge))
        return
    end

    if what == "show" then
        if WheelDrawn() then
            if hold_restore_pending then
                hold_restore_pending = false     -- ours, from a hold just released
                wheel_opened_by_bridge = true
            end
            Reply("ok wheel already drawn opened_by_bridge=%s", tostring(wheel_opened_by_bridge))
            return
        end
        StartTask("wheel show", function()
            MenuTap()
            wheel_opened_by_bridge = true
            Reply("ok wheel opened=%s drawn=%s",
                  tostring(wheel_opened_by_bridge), tostring(WheelDrawn()))
        end)
    elseif what == "hide" then
        if not WheelDrawn() then
            Reply("ok wheel already hidden")
            return
        end
        StartTask("wheel hide", function()
            MenuTap()
            wheel_opened_by_bridge = false
            Reply("ok wheel hidden drawn=%s", tostring(WheelDrawn()))
        end)
    elseif what == "restore" then
        if not wheel_opened_by_bridge then
            Reply("ok wheel restore: bridge did not open it, left alone")
            return
        end
        StartTask("wheel restore", function()
            if WheelDrawn() then
                MenuTap()
            end
            wheel_opened_by_bridge = false
            Reply("ok wheel restored drawn=%s", tostring(WheelDrawn()))
        end)
    else
        Reply("error wheel: expected show, hide, restore or nothing")
    end
end

local function DoLabels()
    local overlay = ReadOverlay()
    if not overlay then
        Reply("error labels: George overlay unreadable")
        return
    end
    local parts = {}
    for _, slot in ipairs({ "up", "down", "left", "right", "centre" }) do
        local defs = WHEEL_SLOT[slot]
        local short = Label(overlay, defs.short) or "-"
        local long = defs.long and (Label(overlay, defs.long) or "-") or "n/a"
        parts[#parts + 1] = string.format("%s[short=%q long=%q]", slot, short, long)
    end
    Reply("ok labels seat=%s mode=%s mirror=%s %s",
          tostring(Seat() or "unknown"), tostring(CurrentMode()),
          tostring(overlay.weapon_up_bridge ~= nil), table.concat(parts, " "))
end

local function DoList(filter)
    local names = {}
    for name in pairs(COMMANDS) do
        if filter == "" or name:find(filter, 1, true) then names[#names + 1] = name end
    end
    table.sort(names)
    Reply("ok %d command(s): %s", #names, table.concat(names, " "))
end

local function HandleLine(line)
    line = tostring(line or ""):gsub("^%s+", ""):gsub("%s+$", "")
    if line == "" then return end
    Log("<- %s", line)

    local verb, rest = line:match("^(%S+)%s*(.*)$")
    verb = verb:lower()

    if verb == "state" then
        DoState()
    elseif verb == "probe" then
        DoProbe()
    elseif verb == "wheel" then
        DoWheel(rest:lower())
    elseif verb == "labels" then
        DoLabels()
    elseif verb == "list" then
        DoList(rest:lower())
    elseif verb == "cancel" then
        task, task_name = nil, nil
        hold_release_pending = false
        ReleaseHeldNow("cancel")
        Reply("ok cancel")
    elseif verb == "hold" then
        local name, opt = rest:lower():match("^(%S+)%s*(%S*)")
        local spec = name and COMMANDS[name]
        if not spec then
            Reply("error unknown command %q -- try \"list\"", tostring(name))
            return
        end
        -- The same command again while its button is still down (a quick
        -- re-press, or a switch contact that flickered): keep the button
        -- down and carry on. Releasing and pressing again would make two
        -- short presses -- and a short FLT Up/Down is a SPEED change, not
        -- altitude.
        if held and held.name == name then
            held.release_at = nil
            held.deadline = Now() + HOLD_WATCHDOG
            Reply("ok hold %s (continued)", name)
            return
        end
        -- A different command: let the current button go only once it has
        -- been down for MIN_HOLD, so it can never read as a short press.
        if task then
            Reply("busy %s", tostring(task_name))
            return
        end
        hold_release_pending = false
        StartTask("hold " .. name, function()
            if held then
                local wait = held.pulse and 0 or (held.since + MIN_HOLD - Now())
                if wait > 0 then Wait(wait * 1000) end
                ReleaseHeldNow("superseded by hold " .. name)
                Wait(POST_PRESS)
            end
            RunHoldCommand(name, spec, opt == "wheel")
        end)
    elseif verb == "keep" then
        if held then
            held.deadline = Now() + HOLD_WATCHDOG
            Reply("ok keep %s", held.name)
        elseif task_name and task_name:sub(1, 5) == "hold " then
            Reply("ok keep pending")
        else
            Reply("ok keep nothing held")
        end
    elseif verb == "release" then
        if RequestRelease() then
            Reply("ok release %s", held.name)
        elseif task_name and task_name:sub(1, 5) == "hold " then
            hold_release_pending = true
            Reply("ok release pending")
        else
            Reply("ok release nothing held")
        end
    elseif verb == "cmd" then
        local tokens = {}
        for token in rest:lower():gmatch("%S+") do tokens[#tokens + 1] = token end
        local name = tokens[1]
        local spec = name and COMMANDS[name]
        if not spec then
            Reply("error unknown command %q -- try \"list\"", tostring(name))
            return
        end
        local count, hold_override = 1, nil
        for i = 2, #tokens do
            local ms = tokens[i]:match("^hold=(%d+)$")
            if ms then
                hold_override = tonumber(ms)
            elseif tonumber(tokens[i]) then
                count = tonumber(tokens[i])
            end
        end
        if count < 1 or count > 12 then count = 1 end
        if hold_override and (hold_override < 520 or hold_override > 1990) then
            Reply("error %s: hold=%d must be 520-1990 ms", name, hold_override)
            return
        end
        StartTask(name, function() RunNamedCommand(name, spec, count, hold_override) end)
    elseif verb == "mode" then
        local target = NormaliseMode(rest)
        if not target then
            Reply("error mode: no target given")
            return
        end
        StartTask("mode " .. target, function()
            local reached, mode = SeekMode(target)
            if reached then Reply("ok mode %s", target)
            else Reply("error mode %s not reachable (now %s)", target, tostring(mode)) end
        end)
    elseif verb == "tune" then
        local key, value = rest:lower():match("^(%S*)%s*(%S*)$")
        if key == "" then
            Reply("ok tune alt=%d course=%d hover=%d pulse=%d", TUNE.alt, TUNE.course, TUNE.hover,
                  PULSE_GAP * 1000)
            return
        end
        if key == "pulse" then
            local ms = tonumber(value)
            if ms and ms >= 30 and ms <= 2000 then PULSE_GAP = ms / 1000 end
            Reply("ok tune pulse=%d (gap between pulses, ms)", PULSE_GAP * 1000)
            return
        end
        if TUNE[key] == nil then
            Reply("error tune: unknown %q (alt, course, hover)", key)
            return
        end
        local ms = tonumber(value)
        if ms == nil then
            Reply("ok tune %s=%d", key, TUNE[key])
            return
        end
        if ms < 520 or ms > 1990 then
            Reply("error tune %s=%d: must be 520-1990 ms (past long_press_time 0.5 s, under the 2.0 s high-rate cliff)", key, ms)
            return
        end
        TUNE[key] = ms
        Reply("ok tune %s=%d", key, ms)
    elseif verb == "eval" then
        local fn, err = loadstring("return " .. rest)
        if not fn then
            Reply("error eval: %s", tostring(err))
            return
        end
        local ok, v = pcall(fn)
        Reply(ok and ("ok " .. tostring(v)) or ("error eval: " .. tostring(v)))
    elseif verb == "press" then
        -- "press up", "press up long", "press up hold 1000", "press up 1000"
        local btn_name, a, b = rest:lower():match("^(%S+)%s*(%S*)%s*(%S*)$")
        local button = btn_name and BUTTON_BY_NAME[btn_name]
        if not button then
            Reply("error press: unknown button %q", tostring(btn_name))
            return
        end
        local ms = tonumber(b) or tonumber(a)
        local kind = "short"
        if ms then kind = "hold"
        elseif a == "long" then kind = "long" end
        StartTask("press " .. btn_name, function()
            Press(button, kind, ms)
            Reply("ok press %s %s%s", btn_name, kind, ms and (" " .. ms .. "ms") or "")
        end)
    else
        Reply("error unknown verb %q", verb)
    end
end

local function PumpSocket()
    if not sock then return end
    for _ = 1, 8 do
        local data, ip, port = sock:receivefrom()
        if not data then return end
        reply_to = { ip = ip, port = port }
        local ok, err = pcall(HandleLine, data)
        if not ok then Log("handler error: %s", tostring(err)) end
    end
end

-- ---------------------------------------------------------------- export hooks

-- Chain onto whatever Export.lua already registered (WinWing, Tacview, ...).
local prev_start = LuaExportStart
local prev_frame = LuaExportAfterNextFrame
local prev_stop  = LuaExportStop

LuaExportStart = function()
    OpenSocket()
    if prev_start then prev_start() end
end

LuaExportAfterNextFrame = function()
    PumpSocket()
    PumpTask()
    PumpHold()
    PumpHoldRestore()
    if prev_frame then prev_frame() end
end

LuaExportStop = function()
    ReleaseHeldNow("export stop")
    if sock then pcall(function() sock:close() end) end
    sock = nil
    if logfile then logfile:close() logfile = nil end
    if prev_stop then prev_stop() end
end

Log("GeorgeBridge loaded")
