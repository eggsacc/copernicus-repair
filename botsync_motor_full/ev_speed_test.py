#!/usr/bin/env python3
"""
EV Drive SPEED-mode single-motor test  (standard Modbus, control mode 08-01 = 0)
================================================================================

The drives are in SPEED mode (08-01 = 0), not multi-drive -- so motion is
commanded with plain FC06 writes, not the JG protocol:

    speed  : write target r/min to Speed No.0 RAM (0x3F08)
    run    : NET-IO (0x1400) bit0 = START/STOP, bit1 = CCW/CW direction (SC mode)
             0x0001 = run CW (fwd) , 0x0003 = run CCW (rev) , 0x0000 = stop
    accel  : ACC/DEC time No.0 RAM (0x4000 / 0x4009), 1 = 0.1 s (0->3000 r/min)
    torque : Tq limit No.0 RAM (0x4300), 1 = 0.1 % (cap for safety)

Enable method 02-14 = 0 (enable on power-up), so the servo is already on; no
SVON needed. This is a SHORT, low-speed validation of the control path before
building the full ramp profile.

    python ev_speed_test.py --port COM3 --id 1 --rpm 90 --secs 3 --arm
"""

import argparse
import sys
import time

from ev_modbus_test import (
    serial, read_holding_registers, write_single_register, to_signed16,
    ModbusError, ModbusTimeout,
)

REG_SPEED0 = 0x3F08
REG_ACC0 = 0x4000
REG_DEC0 = 0x4009
REG_TQ0 = 0x4300
REG_NETIO = 0x1400
REG_STATE = 0x4600
REG_ALARM = 0x4601
REG_MSPEED = 0x4604
REG_BUSV = 0x4607

NETIO_RUN_CW = 0x0001    # START/STOP on, dir bit clear -> CW (forward)
NETIO_RUN_CCW = 0x0003   # START/STOP on + CCW/CW on   -> CCW (reverse)
NETIO_STOP = 0x0000


def status(ser, sid):
    r = read_holding_registers(ser, sid, REG_STATE, 8)
    return {"state": r[0], "alarm": r[1], "speed": to_signed16(r[4]), "busv": r[7] * 0.01}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--id", type=int, default=1)
    p.add_argument("--rpm", type=int, default=90)
    p.add_argument("--secs", type=float, default=3.0)
    p.add_argument("--rev", action="store_true", help="run CCW (reverse) instead of CW")
    p.add_argument("--accel", type=int, default=2, help="ACC/DEC time, 1=0.1s")
    p.add_argument("--torque-cap", type=int, default=500, help="Tq limit, 1=0.1%")
    p.add_argument("--bus-floor", type=float, default=24.0)
    p.add_argument("--timeout", type=float, default=0.3)
    p.add_argument("--arm", action="store_true")
    args = p.parse_args()

    if not args.arm:
        print("Dry run -- pass --arm to move the motor. Nothing sent.")
        sys.exit(0)

    ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                        timeout=args.timeout)
    direction = NETIO_RUN_CCW if args.rev else NETIO_RUN_CW
    dname = "CCW (rev)" if args.rev else "CW (fwd)"
    print("=" * 60)
    print(f" SPEED-mode test: ID {args.id} @ {args.rpm} rpm {dname} for {args.secs}s")
    print("=" * 60)
    try:
        st = status(ser, args.id)
        print(f"  pre: state={st['state']} alarm={st['alarm']} "
              f"bus={st['busv']:.2f}V speed={st['speed']}")
        if st["alarm"] != 0:
            print("  [ABORT] active alarm -- not driving."); sys.exit(1)
        if st["busv"] < args.bus_floor:
            print(f"  [ABORT] bus {st['busv']:.2f}V below floor."); sys.exit(1)

        # Conservative operation data: gentle accel, capped torque.
        write_single_register(ser, args.id, REG_ACC0, args.accel)
        write_single_register(ser, args.id, REG_DEC0, args.accel)
        write_single_register(ser, args.id, REG_TQ0, args.torque_cap)
        write_single_register(ser, args.id, REG_SPEED0, args.rpm)
        print(f"  set: speed0={args.rpm} accel={args.accel/10:.1f}s "
              f"torque-cap={args.torque_cap/10:.1f}%")

        # Go.
        write_single_register(ser, args.id, REG_NETIO, direction)
        print("  RUN")
        t0 = time.time()
        peak = 0
        while time.time() - t0 < args.secs:
            st = status(ser, args.id)
            peak = max(peak, abs(st["speed"]))
            print(f"    t={time.time()-t0:4.1f}s  speed={st['speed']:+5d} rpm "
                  f"state={st['state']} bus={st['busv']:.2f}V")
            if st["alarm"] != 0:
                print(f"  [ABORT] alarm {st['alarm']} during run"); break
            if st["busv"] < args.bus_floor:
                print("  [ABORT] bus under floor during run"); break

        write_single_register(ser, args.id, REG_NETIO, NETIO_STOP)
        print("  STOP")
        time.sleep(0.5)
        st = status(ser, args.id)
        print(f"  post: speed={st['speed']} state={st['state']}")
        print(f"\n  Peak |speed| observed: {peak} rpm  -> "
              + ("MOTOR MOVED" if peak > 5 else "NO MOTION (still 0)"))
    except (ModbusError, ModbusTimeout) as e:
        print(f"  [ERROR] {e}")
    finally:
        try:
            write_single_register(ser, args.id, REG_NETIO, NETIO_STOP)
        except Exception:
            pass
        ser.close()


if __name__ == "__main__":
    main()
