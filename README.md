# BotSync — F710 Gamepad Control & Visualization

Host-PC tooling to **drive and inspect a differential-drive robot with a Logitech
F710 gamepad** (XInput mode) on Windows. This repo contains two self-authored
front ends plus the pre-existing motor-control package they build on:

1. **`f710_visualizer.py`** — a standalone real-time visualizer for the F710's
   sticks, triggers, and buttons. No robot required; use it to confirm the
   controller and its mapping.
2. **`botsync_motor_full/ev_controller_teleop.py`** — gamepad tele-operation of
   the two-motor robot, with a tkinter UI offering two control styles
   (analog joystick / incremental D-pad) and live limit configuration.
3. **`botsync_motor_full/`** — the RS485/Modbus motor-control layer and its WASD
   teleop, written earlier by another agent. It has its **own authoritative
   docs** (`botsync_motor_full/README.md`, `EV_DRIVE_CONTROL_GUIDE.md`); this
   file summarizes only the subset the gamepad teleop depends on.

This document is a **complete handoff**: an agent with access to this repo can
reproduce, modify, or extend everything here without further information.

---

## 1. Repository layout

```
botsync-repair/
├── README.md                      ← (this file) top-level handoff / start here
├── f710_visualizer.py             ← standalone gamepad visualizer (no robot)
└── botsync_motor_full/            ← motor-control package (has its own README)
    ├── README.md                  ← system reference (architecture, protocol)
    ├── EV_DRIVE_CONTROL_GUIDE.md  ← verified register/value cheat-sheet (authoritative)
    ├── ev_modbus_test.py          ← transport layer: Modbus RTU + decode tables (reused everywhere)
    ├── ev_controller_teleop.py    ← ★ F710 gamepad teleop (this project's main deliverable)
    ├── ev_teleop.py               ← original WASD keyboard teleop (template for the above)
    ├── ev_speed_test.py           ← single-motor speed-mode bench primitive
    ├── ev_drive_profile.py        ← automated mirrored ramp/hold/reverse profile
    ├── ev_bus_scan.py             ← RTU baud×ID sweep (link diagnostics)
    ├── ev_ascii_scan.py           ← Modbus-ASCII probe (link diagnostics)
    ├── ev_format_scan.py          ← serial parity/stop/data sweep (link diagnostics)
    └── ev_jg_test.py              ← proves drives are NOT in multi-drive mode
```

**Authorship note for the next agent:** `f710_visualizer.py` and
`ev_controller_teleop.py` are the two files documented in depth below (sections 4
and 5). Everything else under `botsync_motor_full/` predates this work and is
documented by its own README/guide — treat those two `.md` files as the source of
truth for the drive protocol; do not re-derive register addresses.

---

## 2. Environment & setup

| Item | Value |
|---|---|
| OS | Windows 11 |
| Python | **3.13** via the `py` launcher (includes tkinter). *Not* the Anaconda `python` that the older guide mentions. |
| Robot link | USB→RS485 (CH340) on **COM3**, 115200 8-N-1, Modbus RTU, drive IDs 1 (left) & 2 (right) |
| Gamepad | Logitech F710, front switch on **X** (XInput), wireless dongle |

Install the two third-party dependencies (tkinter ships with CPython):

```powershell
py -m pip install pyserial XInput-Python
```

| Package | Import name | Used by | Why |
|---|---|---|---|
| `XInput-Python` | `XInput` | both | read F710 via the Windows XInput API (guaranteed button/stick mapping) |
| `pyserial` | `serial` | teleop (via `ev_modbus_test`) | RS485 serial transport |

**Run commands:**

```powershell
# Visualizer (anywhere)
py f710_visualizer.py

# Gamepad teleop — MUST run from inside the package (imports ev_modbus_test)
cd botsync_motor_full
py ev_controller_teleop.py --dry-run     # UI + gamepad, no motion (start here)
py ev_controller_teleop.py --port COM3   # live; motors move once ARMED
```

---

## 3. Quick mental model

```
   F710 gamepad ──XInput──► Python front end ──► tkinter UI (visualize / configure)
                                   │
                                   ▼ (teleop only)
                          signed wheel setpoints
                                   │ Modbus RTU over RS485 (worker thread)
                          ┌────────┴────────┐
                       Drive ID1          Drive ID2
                      (LEFT wheels)      (RIGHT wheels)
```

- The **visualizer** stops at the first arrow: gamepad → UI.
- The **teleop** adds the control pipeline (section 5.3) and the serial layer.
- All gamepad reads use one small, verified XInput surface (section 4.2); the
  teleop adds one small, verified Modbus register surface (section 5.4).

---

## 4. `f710_visualizer.py` — gamepad visualizer

### 4.1 Purpose & shape
A single-file, single-thread tkinter app that draws the live state of one XInput
controller at ~60 Hz. Read-only; it never touches the robot. Useful as a sanity
check that the F710, dongle, and XInput mapping all work before driving.

- **Window:** 780×600 Canvas, dark theme. **Esc** or window-close quits.
- **Loop:** `root.after(POLL_MS=16, poll)` — no threads (XInput reads are fast,
  local ctypes calls; no blocking I/O).
- **Slot:** `PLAYER = 0` (first controller). Change the constant for slots 1–3.

### 4.2 XInput surface (the entire API this project relies on)
`XInput-Python` wraps `xinput1_*.dll` via ctypes. Verified call/return shapes:

| Call | Returns |
|---|---|
| `XInput.get_connected()` | tuple of 4 bools (slots 0–3) |
| `XInput.get_state(player)` | opaque state struct; raises `XInput.XInputNotConnectedError` if absent |
| `XInput.get_button_values(state)` | `dict[str, bool]` (keys below) |
| `XInput.get_trigger_values(state)` | `(LT, RT)` floats `0.0–1.0` |
| `XInput.get_thumb_values(state)` | `((LX, LY), (RX, RY))` floats `-1.0–1.0`, **+Y = up** |
| `XInput.set_deadzone(zone, value)` | sets the module deadzone used by the readers |

**Button dict keys:** `A B X Y`, `LEFT_SHOULDER RIGHT_SHOULDER`,
`LEFT_THUMB RIGHT_THUMB` (stick clicks = L3/R3), `START BACK`,
`DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT`. (The F710's MODE/vibration buttons are
not XInput buttons and never appear.)

**Deadzone gotcha (verified):** the deadzone sentinel `XInput.DEADZONE_DEFAULT`
is `-1`, so `set_deadzone(zone, 0)` genuinely means *zero* (not "reset to
default"). The visualizer zeroes all three zones at startup so the dots reflect
the raw stick signal (including real drift). The thumb readers radially
normalize, so with zero deadzone a value is just `raw/32767`.

### 4.3 Structure (`class ControllerViz`)
Items are **created once and updated in place** (no per-frame redraw → no
flicker). Builders return dicts/ids of the canvas items to mutate.

| Method | Role |
|---|---|
| `__init__` | builds all static + dynamic canvas items; stores button items in `self.btn[name] = (item_id, on_color)` |
| `_make_stick / _make_bar / _make_circle_button / _make_pill / _make_square` | construct each widget kind, return handles |
| `_update_stick(s, x, y, pressed)` | move dot to `(cx + x·R, cy − y·R)` (screen-invert Y); ring glows + dot recolors on L3/R3 |
| `_update_bar(b, v)` | fill width = `v·w`; show `%` |
| `_set_buttons(buttons)` | fill each button item with its `on_color` if pressed else `PANEL` |
| `_show_connected / _show_disconnected` | per-frame update or reset-to-neutral + status text |
| `poll` | `get_connected` → `get_state` → update; reschedules itself |

**Visual map:** two sticks (ring + dot; ring glows on stick-click), LT/RT fill
bars, LB/RB pills, A/B/X/Y circles (classic green/red/blue/yellow), BACK/START
pills, a D-pad plus, a connection/status line, and a one-line legend.

### 4.4 Reproduce / extend
To rebuild: create a Canvas, build static shapes + dynamic item handles in
`__init__`, and in `poll` map the XInput surface (4.2) onto those handles. To
extend: triggers are already `0–1` (add rumble via `XInput.set_vibration`); add a
numeric raw-axis panel by drawing extra text items and setting them in
`_show_connected`.

---

## 5. `ev_controller_teleop.py` — gamepad teleop

### 5.1 Purpose
Drive the differential robot with the F710. A tkinter UI selects the control
style and tunes limits live; a worker thread owns the serial port. Built by
generalizing `ev_teleop.py` (WASD) to signed/bidirectional wheel commands.

### 5.2 Controls

| Input | Joystick mode | D-pad mode |
|---|---|---|
| Left stick **Y** | forward / reverse velocity `v` | — |
| Right stick **X** | turn rate `w` (yaw) | — |
| **D-pad** Up/Down | — | step `v` accumulator ± (auto-repeats while held) |
| **D-pad** Left/Right | — | step `w` accumulator ∓ |

| Button | Action (both modes) |
|---|---|
| **A** | switch control mode (Joystick ↔ D-pad) |
| **B** | cycle speed gear: 25 → 50 → 75 → 100 % of max |
| **X** | **HALT** — zero both motors immediately |
| **Y** | ARM / DISARM (disarmed = motors held stopped) |
| **Start** / **Back** | arm / (disarm + halt) |
| **Esc** | quit (stops motors) |

UI also exposes: a mode radio, per-wheel commanded/actual r/min + direction, bus
voltage, a live input monitor (stick bars + button lights), ARM/HALT/Quit
buttons, and the config sliders/checkboxes in section 5.6.

### 5.3 Control pipeline (UI thread, every `--ui-ms`, default 25 ms)
The gamepad → wheel-setpoint math, in order:

```
1. deadzone   v_n = apply_deadzone(leftY, dz)   ; w_n = apply_deadzone(rightX, dz)
              (optional axis flips via "flip fwd/turn axis")
2. limits     eff_max = max(SPEED_FLOOR, max_vel · GEARS[gear])      # r/min
3a. joystick  v = v_n · eff_max ; w = w_n · (turn_gain) · eff_max
3b. d-pad     v, w = accumulators (stepped by D-pad, clamped to ±eff_max)
4. mix        left  = clamp(v + w, ±eff_max)     # differential "arcade"
              right = clamp(v − w, ±eff_max)     # +w = turn right
5. publish    robot.set_cmd(left, right)         # signed r/min, under lock
```

Pure, unit-tested helpers (module level): `clamp`, `apply_deadzone(x, dz)`
(0 inside ±dz, ramps to 1 at full deflection), `arcade_mix(v, w, eff_max)`.

### 5.4 Drive protocol subset (authoritative: `EV_DRIVE_CONTROL_GUIDE.md`)
The robot runs in **SPEED mode**; the drive accepts a *speed magnitude* plus a
*run/direction bit* (it cannot take a signed speed). Registers used here:

| Constant | Reg | Meaning / value |
|---|---|---|
| `REG_SPEED0` | `0x3F08` | speed setpoint r/min — **hard min 60**; `<60` is *rejected* |
| `REG_NETIO` | `0x1400` | run/dir: `NETIO_CW=0x0001` (fwd), `NETIO_CCW=0x0003` (rev), `NETIO_STOP=0x0000` |
| `REG_ACC0` / `REG_DEC0` | `0x4000` / `0x4009` | accel / decel time (1 = 0.1 s) — written once at startup |
| `REG_TQ0` | `0x4300` | torque cap (1 = 0.1 %), default 500 = 50 % |
| `REG_STATE` | `0x4600` | monitor block; read 8 regs: `[1]`=alarm, `[4]`=signed actual r/min, `[7]·0.01`=bus V |

`SPEED_FLOOR = 60`, `GEARS = (0.25, 0.50, 0.75, 1.00)`.

**Signed-command → drive translation** (`Robot._send_side`): if `|signed| < 60`
→ write STOP; else magnitude = `clamp(|signed|, 60, max)` and direction =
`forward` if `signed>0` else `reverse`, where **forward = CW unless the side is
inverted, then CCW**. Writes are **on-change only** (track `last_speed` /
`last_netio`).

**Mirroring:** because the two motors are physically mirrored, chassis-forward
must be CW on the left and CCW on the right. So **the right side is inverted by
default** (`invert = {L: False, R: True}`). A unit test asserts straight-forward →
left CW + right CCW. If the robot drives backward or spins instead of going
straight, toggle the per-motor invert checkboxes.

### 5.5 Architecture (threading)
Mirrors the package rule "serial I/O off the UI thread; one owner thread for the
port" (single ~300 ms round-trips must never freeze the UI).

```
UI thread (tkinter)                         Worker thread (Robot._worker)
─ poll(): read XInput, run pipeline         ─ owns serial.Serial(port)
─ _handle_input(): edge buttons, mix        ─ loop @ ~50 ms:
─ set_cmd / set_max / set_invert / ...  ──►     read shared cmd/invert/max/armed
─ _refresh(): redraw labels & monitor   ◄──     _send_side() per wheel (on change)
                                                every N ticks: _read_telemetry()
        shared state in `Robot`, guarded by `self.lock`
   cmd{L,R}  actual{L,R}  bus{L,R}  alarm{L,R}  max  invert{L,R}  armed  connected  status
```

- **`class Robot`** — shared state + worker. Locked setters (`set_cmd`,
  `set_max`, `set_invert`, `set_connected`, `set_armed`, `halt`, `set_status`),
  `snapshot()` for the UI, and `_worker` / `_send_side` / `_read_telemetry` /
  `_emergency_stop` / `shutdown`. `--dry-run` skips serial and echoes
  `cmd → actual` so the UI works with no robot.
- **`class App`** — owns `root`, the tk config vars, the D-pad accumulators
  (`v_acc`, `w_acc`) and edge/hold state (`prev_btn`, `hold`). `poll()` drives
  everything; `_handle_input` does the pipeline + edge-triggered buttons;
  `_dpad_repeat` implements press + auto-repeat (`DELAY=16`, `EVERY=5` ticks);
  `_refresh` paints the UI from `robot.snapshot()`.

### 5.6 Configuration

UI sliders/checkboxes (all live): **max velocity** (60–1000 r/min → `robot.max`),
**turn gain** (0–100 %), **deadzone** (0–40 %), **D-pad step** (5–100 % of max
per press), **invert LEFT/RIGHT motor**, **flip fwd/turn axis**. Gear is cycled
by **B**.

CLI flags (`main()`), all optional:

| Flag | Default | Flag | Default |
|---|---|---|---|
| `--port` | `COM3` | `--turn-gain` | `60` |
| `--baud` | `115200` | `--deadzone` | `12` |
| `--player` | `0` | `--dpad-step` | `20` |
| `--left-id` / `--right-id` | `1` / `2` | `--gear` | `3` (=100 %) |
| `--max` | `300` | `--torque-cap` | `500` (50 %) |
| `--accel` / `--decel` | `3` / `3` | `--bus-floor` | `24.0` |
| `--poll-every` | `10` | `--ui-ms` | `25` |
| `--timeout` | `0.3` | `--invert-left` | off |
| `--dry-run` | off | `--no-invert-right` | off |

### 5.7 Safety model (matches the package's posture)
- **Starts DISARMED.** Motion only when *armed* **and** a gamepad is connected.
- **Gamepad disconnect → command zeroed** (`set_connected(False)`), worker stops.
- **Per-poll fault watch:** any active alarm or bus voltage `< --bus-floor` →
  auto-DISARM + status message.
- **Deterministic stop on every exit:** `_emergency_stop` in the worker's
  `finally`, and `shutdown()` on window-close/Esc/Quit.
- **60 r/min floor is intrinsic:** below ~20 % stick (at full gear) a wheel will
  not engage — that is the hardware minimum, not a bug. Sweeping a stick through
  center naturally passes through STOP before reversing.

### 5.8 Reproduce / extend
Rebuild by following 5.3–5.5: copy `ev_teleop.py`'s `Robot`/worker pattern, swap
positive magnitudes for **signed** commands (add the sign→direction logic in
`_send_side`), and feed `set_cmd` from the gamepad pipeline on the UI thread.
Extension ideas: map LT/RT to a fine/turbo throttle scale; add stop-then-reverse
(stop, confirm `actual≈0`, then flip direction) for cleaner reversals; add
slew-rate limiting on `cmd`; expose accel/decel/torque as live sliders.

---

## 6. The motor-control package (`botsync_motor_full/`)
Treat its **`README.md`** (system reference) and **`EV_DRIVE_CONTROL_GUIDE.md`**
(empirically verified registers/values) as authoritative. Essentials reused by
the teleop:

- **Transport `ev_modbus_test.py`** owns the wire protocol; import its
  primitives instead of re-implementing: `serial`, `read_holding_registers`,
  `write_single_register`, `to_signed16`, `ModbusError`, `ModbusTimeout`,
  `ALARM_CODES`.
- **Verified facts:** speed mode (`08-01=0`), servo on at power-up (no SVON),
  60 r/min setpoint floor, NET-IO run/dir values, ~300 ms per round-trip (poll
  coarsely; keep loops wall-clock based), weak ~29 V bus (watch bus voltage).
- **`ev_teleop.py`** is the WASD predecessor and the direct template for
  `ev_controller_teleop.py`.

---

## 7. Consolidated invariants & gotchas
1. **Run the teleop from `botsync_motor_full/`** — it does `from ev_modbus_test
   import …`.
2. **60 r/min floor:** sub-60 setpoints are rejected; map "stop" to NET-IO STOP,
   not to a tiny speed.
3. **Mirroring:** right side inverted by default; straight-forward = left CW +
   right CCW.
4. **Serial off the UI thread**, single port owner, **write-on-change**.
5. **XInput:** `set_deadzone(zone, 0)` truly zeroes (sentinel is `-1`); thumb
   **+Y is up** (screen-invert when drawing).
6. **Use `py`** (3.13), not `python`/Anaconda, on this machine.

---

## 8. Verification performed
Both files: `py -m py_compile` clean. For the teleop, the pure control math is
unit-tested (re-runnable from `botsync_motor_full/`):

- `apply_deadzone` — zero inside the zone, ramps to ±1, correct midpoint.
- `arcade_mix` — forward, spin-left, spin-right, straight-reverse signs.
- direction resolution — left `+`→CW/`−`→CCW, right (inverted) `+`→CCW/`−`→CW,
  sub-floor→STOP, and straight-forward → opposite electrical directions.
- A headless tkinter smoke test builds the UI (withdrawn, no `mainloop`) and runs
  the poll/refresh + disconnected paths without error.

Not verified against real hardware (no robot/controller in the build
environment): exercise mapping in `--dry-run` first, then ARM and nudge gently on
hardware, using the invert checkboxes if direction is wrong.

---

## 9. Roadmap ideas for the next agent
- Stop-then-reverse and slew-rate limiting for gentler direction changes.
- Trigger-based throttle (LT brake / RT boost) and a rumble-on-fault cue.
- Persist UI config to a JSON file; add a config CLI for headless presets.
- Fold the gamepad teleop into `botsync_motor_full/README.md`'s script inventory.
- Optional: a holonomic mapping if the chassis ever gains strafing hardware.
