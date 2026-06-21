# EV Drive Motor Control — Engineering Handoff Guide

**Purpose:** Everything needed to write code that controls the two Trumman EV
drive motors on this bench rig over Modbus. Written so a fresh agent (or human)
can start here without re-deriving any of it. All register addresses, values and
behaviours below were **verified empirically against the real hardware**, not
just read from the manual — where the manual and the hardware disagreed, the
hardware wins and that is called out.

Manual: `EV-Communication-Manual-EN_UM-01-S0547.pdf` (UM-01-S0547, Rev 1.1) in
this directory. Section numbers below refer to it.

---

## 1. Hardware & physical link

| Item | Value |
|---|---|
| Drives | Trumman Technology **EVDR-K045CQE** (encoder model) ×2 |
| Motors | **BL90** |
| Slave IDs | **1** and **2** (set by each drive's SW1 dial) |
| Serial adapter | USB-RS485, **CH340**, enumerates as **COM3** on this PC |
| Baud | **115200** (set by SW2 dial; the drive will NOT respond at any other baud) |
| Frame | **8 data bits, No parity, 1 stop bit (8-N-1)** |
| Protocol | **Modbus RTU** (SW2-5 OFF = RTU; ON would be ASCII) |
| Termination | 120 Ω, SW2-4 dial |

**Critical wiring note:** RS485 is a differential A/B pair. If A and B are
**swapped**, *both* directions corrupt and you get total silence at every baud
and ID, with the drive's comms LED **blinking** (it hears noise but can decode
nothing). This actually happened in this project — see the Diagnostic Playbook
(§7). The fix was swapping A/B, not any software change.

**Repower rule (manual §1):** any change to a dial OR certain parameters only
takes effect after a full power-cycle (power off until the PWR LED is dark, then
on). This is a common cause of "I changed it but nothing happened" — and of a
*previously working* link breaking right after a restart (a dial that was nudged
earlier becomes active on the repower).

**Python deps:** `pip install pyserial` (and `pypdf` only if you need to read the
manual PDF). On this machine the interpreter is `python` (Anaconda), not
`python3`.

---

## 2. THE most important fact: these drives are in SPEED mode, not multi-drive

The drive has a **control mode** parameter **08-01**:
- `0` = **speed mode**  ← **this rig is set to 0**
- `1` = duty mode
- `2` = position / multi-drive mode

Verified by reading the control-mode register directly:
- RAM `0x4400` = `0`, EEP `0x0800` = `0`.

**Pitfall that cost us a whole iteration:** the Monitor block reports motor
**state = 7 "MOVING (SERVO ON)"**, and the manual (§3.3.2) says state 7 is a
*position-mode* state. That is misleading — on this rig state 7 shows up in speed
mode too. **Do not infer the control mode from the motor-state code. Read 08-01
(0x4400) and trust it.**

Consequence: the **Multi-drive "JG" speed protocol (custom function code 0x65)
does NOT work here.** Every multi-drive command (JG/SVON/ISTOP/SVOFF) comes back
as exception **FC 0x67** with EC `0x01` "invalid function", because multi-drive
only exists when `08-01 = 2`. Don't use FC 0x65/0x66 on this rig unless you first
switch the drives to mode 2 (and that changes everything else too).

---

## 3. How to actually move the motors (SPEED mode) — verified recipe

The relevant parameters are already configured on both drives (verified by
reading 0x3E00..0x3E0F):

| Param | Reg (RAM) | Value | Meaning |
|---|---|---|---|
| 02-01 SC/CC mode | `0x3E00` | `0` | **SC mode** → START/STOP + CCW/CW inputs |
| 02-03 op-data source | `0x3E02` | `1` | **digital indexing** → speed comes from op-data regs (settable over comms) |
| 02-10 speed-ctrl method | `0x3E09` | `0` | analog / digital indexing |
| 02-14 enable method | `0x3E0D` | `0` | **enable on power-up** → servo always on, **no SVON needed** |

### Control registers (all RAM, written with FC06 "write single register")

| Purpose | Reg | Notes |
|---|---|---|
| **Speed setpoint** (Speed No.0) | `0x3F08` | r/min. **HARD MINIMUM 60 r/min** — a value < 60 is rejected with exception `0x04`. Range 60..10000. |
| **Run / direction** (NET-IO, "remote NET-IN") | `0x1400` | bit0 = START/STOP, bit1 = CCW/CW (in SC mode). See values below. |
| Accel time No.0 | `0x4000` | `1` = 0.1 s; time for 0→3000 r/min. Range 2..100 (0.2..10.0 s). |
| Decel time No.0 | `0x4009` | same units |
| Torque limit No.0 | `0x4300` | `1` = 0.1 %. Default 2000 = 200 %. **Cap this low (e.g. 500 = 50 %) on this flaky board.** |

**NET-IO `0x1400` values (verified):**
- `0x0001` → **run CW (forward)**, positive motor speed
- `0x0003` → **run CCW (reverse)**, negative motor speed
- `0x0000` → **stop**

(NET-IO bit0 = function NET-X0 = START/STOP(FWD); bit1 = NET-X1 = CCW/CW(REV),
per the drive's default 09-01/09-02 assignment. An input function is active if
EITHER the physical input OR the NET-IO bit is on, manual §3.4.3, so writing
NET-IO drives the motor without any wiring to the X terminals.)

### Minimal control sequence (per drive, unicast)

```python
# pseudo / actual: uses helpers from ev_modbus_test.py
write_single_register(ser, sid, 0x4000, 2)     # accel 0.2 s
write_single_register(ser, sid, 0x4009, 2)     # decel 0.2 s
write_single_register(ser, sid, 0x4300, 500)   # torque cap 50 %
write_single_register(ser, sid, 0x3F08, 90)    # target 90 r/min (>= 60!)
write_single_register(ser, sid, 0x1400, 0x0001)# RUN CW
# ... motor spins up to 90 r/min ...
write_single_register(ser, sid, 0x1400, 0x0000)# STOP
```

Because `08-01 = 0`, there is **no SVON/servo command** — the servo is enabled on
power-up (02-14 = 0). Just set speed and toggle NET-IO.

### Speed units

`rad/s → r/min`: multiply by `60 / (2π)` ≈ 9.5493.
- 10 rad/s ≈ **95.5 r/min**
- the 60 r/min floor ≈ **6.28 rad/s**

### The 60 r/min floor — design implication

You **cannot** command a smooth ramp from 0; the drive refuses any setpoint
< 60 r/min. Practical pattern for a ramp profile: at START the drive snaps
0 → 60 quickly (via accel time), then you software-ramp the setpoint between
**floor (60) and peak**; at the end you ramp back down to the floor and then
STOP (drive decels 60 → 0). So the achievable "slow ramp" band is 60 r/min up to
your peak. If a true-from-zero slow ramp is required, you would need to change
min-speed/op-data parameters (not yet explored) or use a different control mode.

---

## 4. Reading status / telemetry (FC03 "read holding registers")

**Monitor block** starting at `0x4600` (read 8+ regs in one query; FC03 max 16/query):

| Offset | Reg | Field | Decode |
|---|---|---|---|
| 0 | `0x4600` | Motor state | see codes below |
| 1 | `0x4601` | Alarm code | see codes below; 0 = no alarm |
| 2 | `0x4602` | Operation data No. | which Speed No.X is selected (0 here) |
| 3 | `0x4603` | Command speed | signed r/min |
| 4 | `0x4604` | **Motor speed** | **signed** r/min, **+ = CW**, − = CCW |
| 5 | `0x4605` | Direct I/O status | bitfield |
| 6 | `0x4606` | Output power | W |
| 7 | `0x4607` | **DC bus voltage** | value × 0.01 = volts (~29 V here) |

(More fields 0x4608..0x4616: output %, output current ×0.01 A, torque-limit
current, accel/decel, A1 voltage, hall count, multi-drive positions. Full list in
`ev_modbus_test.py` `MONITOR_FIELDS` and manual §3.3.3.)

**Motor state codes (`0x4600`):** 0 STOP · 2 RUN · 3 EBRAKE · 4 FREE ·
**5 FAULT** · 6 WAIT/INHIBIT(SERVO OFF) · 7 MOVING(SERVO ON) · 8 SLIGHT-POS-KEEPING.

**Alarm codes (`0x4601`):** 1 Overcurrent · 2 Overload · 3 Motor feedback fault ·
4 Over-voltage · 5 **Under-voltage** · 6 Drive overheat · 7 Startup fault ·
8 EEP data error · 10 Motor overheat · 12 Over-speed · 13 Encoder signal fault ·
14 Prevent-op-at-power-on · 15 External stop · 20 Hall seq fault ·
21 Comm error · 22 Parameter error. (Manual Annex A1.)

**Alarm history:** 10 registers at `0x3300`..`0x3309`, most-recent first.
On this rig the history is dominated by **Under-voltage** and **Overload** — the
supply is weak (~29 V bus), so monitor bus voltage while driving.

---

## 5. Maintenance commands (FC06, write 1)

| Reg | Action |
|---|---|
| `0x0A00` | Alarm reset (note: turn off all run inputs first; some alarms need a power-cycle) |
| `0x0A22` | Clear alarm history |
| `0x0A26` | Clear comm-error history |
| `0x0A27` | Configuration / recalc (needed to make "Effective: C" params take effect) |

---

## 6. Wire protocol essentials

- **Modbus RTU**, CRC-16 (poly 0xA001, init 0xFFFF, low byte first). Implemented
  in `ev_modbus_test.py` (`crc16`, `append_crc`, `check_crc`) — reuse it.
- FC03 read holding regs (1..16), FC06 write single reg, FC10 write multiple
  (1..16). Frame: `[id][fc][data...][crc_lo][crc_hi]`.
- Exception reply: FC has high bit set (e.g. 0x86 for a failed FC06). On this
  drive the EC field is 16-bit by default (param 09-16 bit5 = 0). Seen here:
  `0x04` "slave error / value out of range" for a sub-60 speed write.
- Inter-frame: a few ms of silence (C3.5). The transport sleeps ~5 ms; fine at
  115200. Per-query round-trip on this rig is ~300 ms (responses arrive right at
  the timeout), so monitoring two drives every loop costs ~0.6 s — design control
  loops to be **wall-clock-time based**, not fixed-step, so coarse polling still
  tracks the intended timing.

---

## 7. Diagnostic playbook (when the bus goes silent)

This happened twice; both times it was physical. Work top-down:

1. **Is the port there?** `[System.IO.Ports.SerialPort]::GetPortNames()` and check
   the CH340 shows `Status OK` in PnP. If gone → USB/driver/cable.
2. **Sweep baud × ID (RTU):** `ev_bus_scan.py --ids 1-15`. A valid/exception reply
   at some baud → note it (a dial moved). Silence at all → not a baud/ID issue.
3. **Try ASCII:** `ev_ascii_scan.py`. A `:`-framed reply → SW2-5 got set to ASCII;
   set it back to RTU (OFF) and repower.
4. **Sweep serial format:** `ev_format_scan.py` (parity/stop/data at fixed baud).
   A reply → param 09-16 changed away from 8-N-1.
5. **All of the above silent + PWR LED green + comms LED blinking** → the drive
   hears line activity but can't decode anything in any format → **physical RS485
   fault**. In this project the cause was **A/B polarity swapped**. Fix: swap
   A↔B at the terminal, reseat A/B/ground, verify SW2-4 termination, repower the
   drives, re-run `ev_modbus_test.py diag`.
6. **Loopback to split adapter vs. drive:** jumper the adapter's A↔B, run a scan;
   "bad CRC" bytes = adapter TX/RX works (fault downstream), still timeout =
   adapter/cable.

Key signals: **total silence (zero bytes)** points to wiring/power, not config;
**garbage/bad-CRC bytes** point to baud/parity/format; **a clean exception reply**
means the drive is alive and the link is good.

---

## 8. Script inventory (all in this directory)

All scripts share the transport/CRC/decoding in `ev_modbus_test.py` (import from
it rather than re-implementing). All take `--port COM3 --baud 115200`.

| Script | What it does | Moves motor? |
|---|---|---|
| `ev_modbus_test.py` | `diag` (ping + decode monitor + alarm history), `read`/`write` raw registers | No (read-only) |
| `ev_bus_scan.py` | RTU baud×ID sweep, reports any/partial bytes | No |
| `ev_ascii_scan.py` | Modbus-ASCII probe across bauds | No |
| `ev_format_scan.py` | parity/stop/data sweep at one baud | No |
| `ev_jg_test.py` | Multi-drive JG echo test — demonstrates the 0x67 rejection (proof we're not in mode 2) | attempts; rejected |
| `ev_speed_test.py` | **Single-motor speed-mode primitive** (set speed + NET-IO run/stop), reads actual speed | **Yes** (needs `--arm`) |
| `ev_drive_profile.py` | **Main driver:** mirrored ramp/hold/reverse profile, both motors, speed mode | **Yes** (needs `--arm`) |

### Main driver usage (`ev_drive_profile.py`)

- **Dry run (default, sends nothing but the read-only pre-flight):**
  `python ev_drive_profile.py --port COM3`
- **Arm and drive one cycle:** add `--arm`
- **Emergency stop everything:** `--stop-only --arm`
- Useful flags: `--peak-rad 10` (peak speed), `--ramp 5 --hold 5` (seconds),
  `--cycles N` (0 = loop until Ctrl-C), `--floor 60`, `--torque-cap 500`
  (1=0.1%), `--bus-floor 24.0` (abort if DC bus dips below, volts).

**Profile shape:** ID 1 forward (CW) / ID 2 reverse (CCW), mirrored; ramp
floor→peak, hold, ramp peak→floor, stop, then flip directions and repeat. The
ramp is wall-clock-time based. Direction is reversed only after a confirmed stop.

### Safety model baked into the drivers
- Dry-run by default; `--arm` required to energize.
- Read-only **pre-flight**: aborts before any motion if a drive can't be read, has
  an active alarm, is in FAULT, or DC bus < floor.
- **Per-loop monitoring**: every tick re-reads alarm/state/bus-voltage; raises a
  SafetyAbort (→ stop both, NET-IO 0) on any alarm, FAULT, or under-voltage.
- **Conservative torque cap** (default 50 %).
- **Ctrl-C / any error** → stop both drives (NET-IO 0) in a finally block.
- Reverse only after confirming motors actually stopped.

---

## 9. Quick verified facts cheat-sheet

- Port `COM3`, `115200 8-N-1 RTU`, IDs `1` & `2`.
- Control mode `08-01 = 0` (speed). Multi-drive JG (FC 0x65) is rejected here.
- Move: write r/min → `0x3F08` (min **60**), then `0x1400` = `0x0001` (CW) /
  `0x0003` (CCW) / `0x0000` (stop). No SVON needed (02-14 = 0).
- Tune: accel `0x4000`, decel `0x4009` (0.1 s), torque `0x4300` (0.1 %, cap it).
- Read: monitor block at `0x4600` (state, alarm, speed@+4, bus-V@+7 ×0.01).
- Bus ~29 V and weak; alarm history full of under-voltage — keep speeds/torque
  low and watch `0x4607`.
- If the bus goes silent: suspect **A/B polarity** and a needed **repower**, not
  software.

---

*Generated as a handoff after a session that: established comms (baud was the
first hurdle), lost and recovered the link (A/B polarity), discovered the drives
were in speed mode (not multi-drive as first assumed), found the 60 r/min
setpoint floor, and successfully ran both motors through a mirrored
ramp/hold/reverse profile with actual speed tracking setpoint to ±1 r/min.*
