# EV Drive Robot — System Reference

A differential-drive robot powered by two Trumman EV servo drives, commanded from
a host PC over a single RS485 Modbus link. This document describes **how the
system is put together and how software controls it** — the integration layer is
where the real complexity lives, so that is the focus. Read top-to-bottom for a
general picture, then the later sections for the register-level detail needed to
write or modify control code.

---

## 1. System overview

```
   Host PC (Python)
        │  USB
   USB–RS485 adapter
        │  RS485 (A/B differential pair, one multi-drop bus)
   ┌────┴───────────────┐
   │                    │
 Drive ID 1           Drive ID 2
 (LEFT wheels)        (RIGHT wheels)
   │                    │
 motor + encoder     motor + encoder
```

- **Two independent drives** share one RS485 bus, addressed by slave ID. By
  convention **ID 1 = left wheel group, ID 2 = right wheel group**.
- Each drive is a closed-loop servo controller for one motor group; the host does
  **not** do current/velocity control — it sends *setpoints* and the drive runs
  its own internal loops.
- All host↔drive interaction is **Modbus RTU**: the host reads/writes drive
  registers. There is no other control channel. Understanding "which register
  means what" *is* understanding the robot.

The robot is a differential drive: translation comes from both sides turning
together, rotation from a left/right speed difference. Because the two motors are
physically mirrored, driving the chassis straight requires the two drives to spin
in **opposite electrical directions** — this is handled in software, not wiring.

---

## 2. Communication layer

| Property | Value |
|---|---|
| Physical | RS485, half-duplex, single multi-drop pair (A/B) + ground |
| Protocol | Modbus RTU (binary; CRC-16, poly 0xA001, init 0xFFFF, low byte first) |
| Framing | 8 data bits, no parity, 1 stop bit |
| Baud | 115200 (must match the drive's dial; not settable over comms) |
| Addressing | slave ID per drive (broadcast ID 0 = all, no reply) |
| Function codes | 0x03 read holding regs (≤16), 0x06 write single, 0x10 write multiple (≤16) |

Two integration facts that shape host code:

- **RS485 is differential.** A and B must not be swapped and the pair must stay
  intact end-to-end; a reversed or broken pair corrupts traffic in *both*
  directions (the bus appears dead even though drives are powered). Bus
  termination (120 Ω) belongs at the physical ends of the line.
- **Many drive settings only apply after a power-cycle.** Dial changes (ID, baud,
  protocol, termination) and parameters marked "effective after power-on / after
  configuration" do not take effect live. A controller that writes such a
  parameter and expects immediate effect will be wrong — see §4.

### Round-trip latency (drives the control-loop design)

A single request/response transaction takes on the order of a few hundred
milliseconds on this bus. **This is the dominant real-time constraint.** Any
controller that both commands and monitors must assume that *reading* status from
both drives costs significant time, and therefore:

- Control loops should be **wall-clock-time based**, not fixed-step — compute the
  desired output from elapsed time so that slow/variable polling still tracks the
  intended trajectory.
- Interactive/GUI control must keep serial I/O **off the UI thread** (a dedicated
  worker thread owns the port) so the interface never freezes on a slow read.
- Write only on change; don't re-send identical setpoints every tick.

---

## 3. The control model (speed mode)

Each drive has a **control mode** that determines its entire command interface.
This robot operates in **speed mode**, in which the host commands a target wheel
speed and the drive ramps to and holds it.

> **Critical integration point.** The control mode is authoritative and must be
> read from its parameter register — *not* inferred from the motor-state
> telemetry. The drive can report a motor-state value that, per the manual,
> suggests a different mode; that telemetry is not a reliable indicator of the
> active control mode. Code that assumes the wrong mode will issue commands the
> drive rejects. In speed mode, the alternative "multi-drive" command set (custom
> function codes) is **not available** and is refused as an invalid function.

### Commanding motion

Motion is expressed through three things, all plain register writes:

1. **Speed setpoint** — write the target speed (r/min) to the active speed
   operation-data register. The drive accelerates to it using its configured
   acceleration time.
2. **Run / direction** — a digital-input control register ("NET-IO"): one bit
   starts/stops the motor, another selects rotation direction. Setting the run
   bit makes the wheel turn at the setpoint; clearing it stops the wheel.
3. **Limits/shaping (optional, set once)** — acceleration time, deceleration
   time, and torque limit are operation-data registers; cap torque for safety.

The servo is **enabled on power-up** in this configuration, so there is **no
separate "servo on" command** — set a speed and assert the run bit and the wheel
moves.

### Reading state

A contiguous **monitor block** of read-only registers exposes motor state, active
alarm code, command speed, **actual motor speed** (signed; sign = direction), I/O
status, **DC bus voltage**, output power/current, and more. A controller should
poll at least *actual speed*, *alarm code*, and *bus voltage* to close the loop
and to detect faults. A separate alarm-history block records recent faults.

---

## 4. Integration constraints that shape any controller

These are inherent to the hardware/firmware and are the things most likely to
trip up new control code. They are not optional details.

- **Minimum speed setpoint.** The drive enforces a hard lower bound on the speed
  setpoint (well above zero). A setpoint below that bound is **rejected**, not
  clamped. Consequences: you cannot smoothly ramp a single wheel from 0 through
  the low-speed region — motion necessarily *steps* through 0↔floor at start/stop,
  and any usable "slow ramp" lives between the floor and your peak. Velocity
  inputs that accumulate small increments must either use a step ≥ the floor or
  clamp the *transmitted* magnitude up to the floor (commanding "stop" for true
  zero).

- **RAM vs. stored parameters.** Most settings exist as both a volatile (RAM)
  copy used for live control and a persistent (EEP) copy. **Write the RAM copy for
  motion control**; the persistent copy is for configuration that should survive a
  power cycle (and is slower to write). Mixing them up either fails to take effect
  live or wears/locks the stored config.

- **"Effective timing" of parameters.** Each parameter becomes effective either
  immediately, after the motor stops, after an explicit *configuration* command,
  or only after a power cycle. A controller must respect this: e.g. writing a
  mode/configuration parameter and then commanding motion in the same breath will
  not behave as intended unless the parameter's effective-timing is honored.

- **Direction & input model.** In the active input mode, one input bit means
  start/stop and another means direction; the host drives these via the remote
  input-control register. An input function is active if **either** a physical
  terminal **or** the remote register asserts it — so software can command run/
  direction with no terminal wiring, but a stuck/asserted physical input can also
  override software. Direction may be changed while running (the drive ramps
  through zero), or you can stop-then-reverse for a cleaner transition.

- **Differential-drive mirroring lives in software.** Positive "wheel speed" must
  map to opposite electrical directions on the two sides for the chassis to move
  straight. Treat per-side direction inversion as a software parameter.

- **Weak-supply sensitivity.** These drives are sensitive to DC-bus sag;
  under-voltage faults stop the motor. Controllers should **monitor bus voltage**
  and keep speed/torque conservative, especially during acceleration (peak current
  draw). Treat an under-voltage or any active alarm as an immediate stop
  condition.

---

## 5. Software architecture

The code is layered so that applications never touch raw bytes:

```
ev_modbus_test.py      ← transport + decoding layer (reuse this everywhere)
      ▲   ▲   ▲
      │   │   └── ev_drive_profile.py   automated speed profile (both drives)
      │   └────── ev_teleop.py          interactive WASD differential teleop (GUI)
      └────────── ev_speed_test.py      single-motor speed-mode bench primitive
```

**Transport layer (`ev_modbus_test.py`)** owns everything wire-level: CRC,
frame building, the three function codes, exception decoding, the monitor/alarm
decode tables, and `serial` setup. It also provides a standalone `diag`
(ping + decode status/alarms) and raw `read`/`write` of any register — the first
tool to reach for when checking a link or poking a parameter. **All higher-level
code imports its primitives instead of re-implementing them.**

**Control primitives** (in the apps) are thin: write speed setpoint, set run/
direction, write limit registers, read the monitor block. Everything else is
policy.

Recurring design patterns worth preserving in new code:

- **Time-based trajectories** (not step counts) so behavior is correct regardless
  of bus latency.
- **A single owner thread for the serial port.** The port is not shared across
  threads; interactive front-ends mutate shared setpoints under a lock and let
  the worker do all I/O.
- **Monitor-and-abort.** Each control iteration re-reads alarm/state/bus-voltage
  and forces a stop on any fault; cleanup paths (exit, error, window close) always
  command stop.
- **Arming.** Tools that move the motor default to a no-motion/dry mode and
  require an explicit flag (or are inherently interactive) before energizing.

---

## 6. Controlling the motors — minimal recipe

Per drive (using the transport layer's helpers):

```python
# one-time shaping (RAM): gentle accel/decel, capped torque
write_single_register(ser, sid, REG_ACCEL, accel_01s)
write_single_register(ser, sid, REG_DECEL, decel_01s)
write_single_register(ser, sid, REG_TORQUE_LIMIT, torque_01pct)

# go: set speed (>= floor), then assert run+direction
write_single_register(ser, sid, REG_SPEED_SETPOINT, rpm)          # >= floor
write_single_register(ser, sid, REG_RUN_IO, RUN_FORWARD)          # or RUN_REVERSE

# stop
write_single_register(ser, sid, REG_RUN_IO, STOP)

# monitor (close the loop / watch for faults)
state, alarm, ..., actual_speed, ..., bus_v = read_holding_registers(ser, sid, MONITOR_BASE, N)
```

For the differential robot: command both drives, mirror direction so positive
speed drives forward, and derive each side's setpoint from the desired
translation + rotation. See `ev_teleop.py` for the interactive version and
`ev_drive_profile.py` for an automated trajectory.

---

## 7. Safety model

Driving real motors on a shared bus with a weak supply demands a consistent
safety posture, baked into every tool:

1. **Pre-flight read** before energizing: confirm comms, no active alarm, motor
   not faulted, bus voltage above a floor.
2. **Continuous monitoring** while moving: alarm, motor state, and bus voltage
   each loop → immediate stop on any fault or under-voltage.
3. **Conservative limits**: cap torque, keep speeds modest, prefer gentle accel.
4. **Deterministic stop on every exit path**: normal completion, exceptions,
   Ctrl-C/keyboard, and GUI close all command stop to both drives.
5. **Default to not moving**: non-interactive tools require explicit arming.

---

## 8. Register reference (concise)

Symbolic names map to the registers used above; consult the transport module and
the communication manual (`EV-Communication-Manual-EN_UM-01-S0547.pdf`) for exact
addresses, ranges, units, and the full monitor/alarm tables.

| Role | Notes |
|---|---|
| Control mode | read to confirm the drive is in speed mode (authoritative) |
| Speed setpoint (op-data, RAM) | target r/min; **hard minimum** applies |
| Run/direction (remote I/O, RAM) | run bit + direction bit; also stop |
| Accel / decel time (op-data, RAM) | ramp shaping (0→full units) |
| Torque limit (op-data, RAM) | cap for safety |
| Monitor block (read-only) | state, alarm, actual speed (signed), bus voltage, power, current |
| Alarm history (read-only) | recent fault codes, newest first |
| Maintenance (write) | alarm reset, clear history, apply configuration |

Speed units: `r/min = rad/s × 60 / (2π)`. Actual-speed sign indicates rotation
direction.

---

*Authoritative source for addresses, bit fields, and value ranges:
`EV-Communication-Manual-EN_UM-01-S0547.pdf`. For concrete, verified register
addresses and values as used by this codebase, see `EV_DRIVE_CONTROL_GUIDE.md`.*
