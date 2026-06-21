#!/usr/bin/env python3
"""
EV Drive speed-profile driver  (SPEED mode, standard Modbus)
============================================================

Target hardware : Trumman EVDR-K045CQE drive / BL90 motor
Protocol ref     : UM-01-S0547 "EV Drive Communication Manual", Rev 1.1

The drives are in control mode 08-01 = 0 (SPEED mode), with:
  02-01 = 0  SC mode  -> START/STOP + CCW/CW inputs
  02-03 = 1  digital indexing -> speed taken from operation-data registers
  02-14 = 0  enable on power-up -> servo always on (no SVON needed)
so motion is commanded with plain FC06 writes:

  speed : write target r/min to Speed No.0 RAM 0x3F08   (HARD MIN 60 r/min)
  run   : NET-IO 0x1400  bit0 = START/STOP, bit1 = CCW/CW direction
          0x0001 = run CW (fwd) , 0x0003 = run CCW (rev) , 0x0000 = stop
  accel : ACC/DEC time No.0 RAM 0x4000 / 0x4009 (1 = 0.1 s, for 0->3000 r/min)
  torque: Tq limit No.0 RAM 0x4300 (1 = 0.1 %)

Profile (mirrored): ID 1 forward / ID 2 reverse, then flipped each cycle.
  snap 0->floor, ramp floor->peak over RAMP s, hold PEAK for HOLD s,
  ramp peak->floor over RAMP s, stop (drive decels floor->0), reverse, repeat.

NOTE on the 60 r/min floor: the drive rejects any setpoint < 60 r/min, so the
0..6.3 rad/s band is a quick step at start/stop, not a slow ramp. The slow ramp
covers floor..peak (6.3..10 rad/s at the defaults).

SAFETY
------
- Defaults to DRY RUN; pass --arm to actually move the motors.
- 50 % torque cap; aborts (stop both) on any alarm, FAULT state, or bus < floor.
- Reverses only after a confirmed stop. Ctrl-C / error -> stop both + NET-IO 0.

    python ev_drive_profile.py --port COM3            # dry run
    python ev_drive_profile.py --port COM3 --arm      # drive one cycle
    python ev_drive_profile.py --port COM3 --stop-only --arm
"""

import argparse
import math
import sys
import time

from ev_modbus_test import (
    serial, read_holding_registers, write_single_register, to_signed16,
    ModbusError, ModbusTimeout, ALARM_CODES, MOTOR_STATE,
)

RPM_PER_RAD = 60.0 / (2.0 * math.pi)

REG_SPEED0 = 0x3F08
REG_ACC0 = 0x4000
REG_DEC0 = 0x4009
REG_TQ0 = 0x4300
REG_NETIO = 0x1400
REG_STATE = 0x4600

NETIO_CW = 0x0001    # START + dir clear  -> CW  (forward, positive speed)
NETIO_CCW = 0x0003   # START + CCW/CW set -> CCW (reverse, negative speed)
NETIO_STOP = 0x0000
FAULT_STATE = 5
SPEED_FLOOR = 60     # drive rejects op-data speed < 60 r/min


class SafetyAbort(Exception):
    pass


def read_status(ser, sid):
    r = read_holding_registers(ser, sid, REG_STATE, 8)
    return {"state": r[0], "alarm": r[1], "speed": to_signed16(r[4]), "busv": r[7] * 0.01}


def check_status(ser, ids, bus_floor):
    out = {}
    for sid in ids:
        st = read_status(ser, sid)
        out[sid] = st
        if st["alarm"] != 0:
            raise SafetyAbort(f"drive {sid} alarm {st['alarm']} "
                              f"({ALARM_CODES.get(st['alarm'], 'unknown')})")
        if st["state"] == FAULT_STATE:
            raise SafetyAbort(f"drive {sid} motor state = FAULT")
        if st["busv"] < bus_floor:
            raise SafetyAbort(f"drive {sid} DC bus {st['busv']:.2f} V < floor {bus_floor}")
    return out


def set_speed(ser, sid, rpm, armed):
    if armed:
        write_single_register(ser, sid, REG_SPEED0, int(rpm))


def run(ser, sid, netio, armed):
    if armed:
        write_single_register(ser, sid, REG_NETIO, netio)


def stop_all(ser, ids, armed):
    for sid in ids:
        if armed:
            try:
                write_single_register(ser, sid, REG_NETIO, NETIO_STOP)
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] stop write to ID{sid} failed: {e}")


def write_op_data(ser, ids, accel, decel, torque_cap, armed):
    for sid in ids:
        if armed:
            write_single_register(ser, sid, REG_ACC0, accel)
            write_single_register(ser, sid, REG_DEC0, decel)
            write_single_register(ser, sid, REG_TQ0, torque_cap)
        print(f"  ID{sid}: accel={accel/10:.1f}s decel={decel/10:.1f}s "
              f"torque-cap={torque_cap/10:.1f}%" + ("" if armed else "   [dry-run]"))


def setpoint_at(t, floor, peak, ramp_s, hold_s):
    """floor->peak over ramp_s, hold peak, peak->floor over ramp_s."""
    span = peak - floor
    if t < ramp_s:
        return floor + span * (t / ramp_s)
    if t < ramp_s + hold_s:
        return peak
    if t < 2 * ramp_s + hold_s:
        return floor + span * (1.0 - (t - ramp_s - hold_s) / ramp_s)
    return floor


def confirm_stopped(ser, ids, bus_floor, armed, settle=5, timeout=4.0):
    if not armed:
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        if all(abs(s["speed"]) <= settle for s in check_status(ser, ids, bus_floor).values()):
            return
    print(f"  [WARN] no confirmed stop within {timeout:.0f}s")


def run_half(ser, ids, dirs, floor, peak, ramp_s, hold_s, bus_floor, armed):
    """dirs = {id: NETIO_CW|NETIO_CCW}. Snap to floor, run, ramp, stop."""
    for sid in ids:
        set_speed(ser, sid, floor, armed)
        run(ser, sid, dirs[sid], armed)
    total = 2 * ramp_s + hold_s
    t0 = time.time()
    last = -1.0
    while True:
        t = time.time() - t0
        if t >= total:
            break
        sp = round(setpoint_at(t, floor, peak, ramp_s, hold_s))
        for sid in ids:
            set_speed(ser, sid, sp, armed)
        sts = check_status(ser, ids, bus_floor) if armed else None
        if t - last >= 0.5:
            last = t
            desc = "  ".join(
                f"ID{sid} set {('+' if dirs[sid]==NETIO_CW else '-')}{sp:>3} rpm"
                + (f" (act {sts[sid]['speed']:+4d}, {sts[sid]['busv']:.1f}V)" if sts else "")
                for sid in ids)
            print(f"    t={t:4.1f}s  {desc}")
    stop_all(ser, ids, armed)


def emergency_off(ser, ids, armed):
    try:
        stop_all(ser, ids, armed)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] cleanup failed: {e}")


def main():
    p = argparse.ArgumentParser(description="Mirrored ramp/hold/reverse speed profile "
                                            "for Trumman EV drives (speed mode).")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--ids", default="1,2")
    p.add_argument("--peak-rad", type=float, default=10.0, help="Peak speed (rad/s)")
    p.add_argument("--ramp", type=float, default=5.0)
    p.add_argument("--hold", type=float, default=5.0)
    p.add_argument("--cycles", type=int, default=1, help="fwd+rev pairs; 0 = until Ctrl-C")
    p.add_argument("--floor", type=int, default=SPEED_FLOOR, help="min setpoint (>=60)")
    p.add_argument("--torque-cap", type=int, default=500, help="1=0.1%% (default 50%%)")
    p.add_argument("--accel", type=int, default=2, help="1=0.1s (2..100)")
    p.add_argument("--decel", type=int, default=2, help="1=0.1s (2..100)")
    p.add_argument("--bus-floor", type=float, default=24.0)
    p.add_argument("--timeout", type=float, default=0.3)
    p.add_argument("--arm", action="store_true")
    p.add_argument("--stop-only", action="store_true")
    args = p.parse_args()

    ids = [int(x) for x in args.ids.split(",")]
    peak_rpm = round(args.peak_rad * RPM_PER_RAD)
    armed = args.arm

    if args.floor < SPEED_FLOOR:
        p.error(f"--floor must be >= {SPEED_FLOOR} (drive rejects lower setpoints)")
    if peak_rpm < args.floor:
        p.error(f"peak {peak_rpm} rpm < floor {args.floor} rpm -- raise --peak-rad")
    if peak_rpm > 4000:
        p.error(f"peak {peak_rpm} rpm too high")

    try:
        ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=serial.EIGHTBITS,
                            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                            timeout=args.timeout)
    except serial.SerialException as e:
        print(f"Could not open {args.port}: {e}"); sys.exit(1)

    print("=" * 72)
    print(" EV Drive speed profile  (SPEED mode)")
    print(f" IDs {ids} | peak {args.peak_rad:.2f} rad/s = {peak_rpm} rpm | "
          f"floor {args.floor} rpm ({args.floor/RPM_PER_RAD:.1f} rad/s) | "
          f"ramp {args.ramp:.0f}s hold {args.hold:.0f}s | "
          f"cycles {'inf' if args.cycles == 0 else args.cycles}")
    print(f" Mode: {'ARMED -- MOTORS WILL MOVE' if armed else 'DRY RUN'}")
    print("=" * 72)

    try:
        if args.stop_only:
            print(" Emergency stop")
            emergency_off(ser, ids, armed)
            return

        print("\n-- Pre-flight --")
        try:
            sts = check_status(ser, ids, args.bus_floor)
        except SafetyAbort as e:
            print(f"  [ABORT] {e}"); sys.exit(1)
        for sid in ids:
            s = sts[sid]
            print(f"  ID{sid}: state={MOTOR_STATE.get(s['state'], s['state'])} "
                  f"alarm={ALARM_CODES.get(s['alarm'], s['alarm'])} "
                  f"bus={s['busv']:.2f}V speed={s['speed']}")

        print("\n-- Operation data (set No.0, RAM) --")
        write_op_data(ser, ids, args.accel, args.decel, args.torque_cap, armed)

        print("\n-- Run --")
        base = {sid: (NETIO_CW if i == 0 else NETIO_CCW) for i, sid in enumerate(ids)}

        cycle = 0
        tag = "inf" if args.cycles == 0 else args.cycles
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            print(f"\n  Cycle {cycle}/{tag} -- forward (" +
                  ", ".join(f"ID{s}:{'CW' if base[s]==NETIO_CW else 'CCW'}" for s in ids) + ")")
            run_half(ser, ids, base, args.floor, peak_rpm, args.ramp, args.hold,
                     args.bus_floor, armed)
            confirm_stopped(ser, ids, args.bus_floor, armed)

            rev = {sid: (NETIO_CCW if v == NETIO_CW else NETIO_CW) for sid, v in base.items()}
            print(f"  Cycle {cycle}/{tag} -- reverse (" +
                  ", ".join(f"ID{s}:{'CW' if rev[s]==NETIO_CW else 'CCW'}" for s in ids) + ")")
            run_half(ser, ids, rev, args.floor, peak_rpm, args.ramp, args.hold,
                     args.bus_floor, armed)
            confirm_stopped(ser, ids, args.bus_floor, armed)

        print("\n-- Profile complete --")

    except SafetyAbort as e:
        print(f"\n  [SAFETY ABORT] {e}\n  Stopping motors.")
        emergency_off(ser, ids, armed); sys.exit(2)
    except KeyboardInterrupt:
        print("\n  [Ctrl-C] Stopping motors.")
        emergency_off(ser, ids, armed); sys.exit(130)
    except (ModbusError, ModbusTimeout) as e:
        print(f"\n  [COMMS ERROR] {e}\n  Stopping motors.")
        emergency_off(ser, ids, armed); sys.exit(1)
    else:
        emergency_off(ser, ids, armed)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
