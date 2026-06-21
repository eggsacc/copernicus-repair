#!/usr/bin/env python3
"""
EV Drive Multi-drive JG single-motor test  (echo mode, reads the drive's reply)
===============================================================================

The profile run sent non-echo JG commands and the motors didn't move. Non-echo
gives no feedback, so this sends ECHO multi-drive commands (FC 0x65, slaves
reply with FC 0x66 normal / 0x67 exception) to ONE drive at a low speed, prints
each reply, and polls the actual motor speed (0x4604) so we can see whether the
drive accepts JG and actually spins.

Sequence: SVON -> JG +speed (hold) -> ISTOP -> SVOFF, all echoed.

    python ev_jg_test.py --port COM3 --id 1 --rpm 60 --secs 3 --arm
"""

import argparse
import sys
import time

from ev_modbus_test import (
    serial, append_crc, check_crc, read_holding_registers, to_signed16,
)

MD_FC = 0x65
# Echo command codes (sec 4.7)
ISTOP, JG, FREE, SVON, SVOFF = 0x00, 0x0A, 0x05, 0x06, 0x07
SPEED_REG = 0x4604


def build_md(sub_id, code, speed=0):
    upper, lower = 0x0000, speed & 0xFFFF
    frame = bytes([0x00, MD_FC, 1, sub_id, code,
                   (upper >> 8) & 0xFF, upper & 0xFF,
                   (lower >> 8) & 0xFF, lower & 0xFF])
    return append_crc(frame)


def send_echo(ser, sub_id, code, speed, label):
    ser.reset_input_buffer()
    ser.write(build_md(sub_id, code, speed))
    time.sleep(0.01)
    resp = ser.read(32)
    if not resp:
        print(f"  {label:<14}: NO REPLY (drive ignored the frame)")
        return None
    ok = check_crc(resp)
    fc = resp[1] if len(resp) > 1 else None
    tag = {0x66: "normal (0x66)", 0x67: "EXCEPTION (0x67)"}.get(fc, f"FC=0x{fc:02X}")
    print(f"  {label:<14}: {tag}  crc={'ok' if ok else 'BAD'}  raw={resp.hex(' ')}")
    return resp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--id", type=int, default=1)
    p.add_argument("--rpm", type=int, default=60)
    p.add_argument("--secs", type=float, default=3.0)
    p.add_argument("--timeout", type=float, default=0.3)
    p.add_argument("--arm", action="store_true", help="actually move the motor")
    args = p.parse_args()

    if not args.arm:
        print("Dry run -- pass --arm to actually send commands. Nothing sent.")
        sys.exit(0)

    ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                        timeout=args.timeout)
    print("=" * 60)
    print(f" Multi-drive JG echo test: ID {args.id} @ {args.rpm} rpm for {args.secs}s")
    print("=" * 60)
    try:
        before = read_holding_registers(ser, args.id, SPEED_REG, 1)[0]
        print(f"  motor speed before: {to_signed16(before)} rpm\n")

        send_echo(ser, args.id, SVON, 0, "SVON")
        time.sleep(0.1)
        send_echo(ser, args.id, JG, args.rpm, f"JG +{args.rpm}")

        t0 = time.time()
        while time.time() - t0 < args.secs:
            spd = to_signed16(read_holding_registers(ser, args.id, SPEED_REG, 1)[0])
            print(f"    t={time.time()-t0:4.1f}s  actual speed = {spd:+5d} rpm")

        send_echo(ser, args.id, ISTOP, 0, "ISTOP")
        time.sleep(0.1)
        send_echo(ser, args.id, SVOFF, 0, "SVOFF")
    finally:
        try:
            ser.write(build_md(args.id, ISTOP, 0)); time.sleep(0.02)
        except Exception:
            pass
        ser.close()


if __name__ == "__main__":
    main()
