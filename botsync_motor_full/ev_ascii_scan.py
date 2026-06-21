#!/usr/bin/env python3
"""
EV Drive Modbus ASCII probe  (READ-ONLY, cannot move the motor)
===============================================================

If the drive's protocol dial (SW2-5) got flipped to Modbus ASCII (ON = ASCII,
OFF = RTU), it will silently ignore every RTU frame -- giving exactly the
"zero bytes back + comms-error LED blinking" symptom we're seeing.

This sends Modbus *ASCII* FC03 reads (':' ... LRC ... CRLF) across every baud
to IDs 1..N and reports any reply. A clean ':' response means the drive is in
ASCII mode and the fix is to set SW2-5 back to OFF (RTU) and repower.

Read-only: FC03 reads only; never writes or commands motion.

    python ev_ascii_scan.py --port COM3 --ids 1-4
"""

import argparse
import sys
import time

from ev_modbus_test import serial

DEFAULT_BAUDS = [115200, 57600, 38400, 19200, 9600]
MONITOR_BASE = 0x4600


def parse_ids(spec):
    out = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def lrc(payload: bytes) -> int:
    """Modbus ASCII LRC: two's complement of the byte sum."""
    return (-sum(payload)) & 0xFF


def build_ascii_read(slave_id, addr, count):
    payload = bytes([slave_id, 0x03, (addr >> 8) & 0xFF, addr & 0xFF,
                     (count >> 8) & 0xFF, count & 0xFF])
    body = payload.hex().upper() + f"{lrc(payload):02X}"
    return b":" + body.encode("ascii") + b"\r\n"


def probe(ser, slave_id):
    req = build_ascii_read(slave_id, MONITOR_BASE, 1)
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.01)
    resp = ser.read_until(b"\r\n", 64)
    if not resp:
        return "timeout", resp
    if resp[:1] != b":":
        return "junk", resp
    return "ascii", resp


def main():
    p = argparse.ArgumentParser(description="Read-only Modbus ASCII probe for EV drives.")
    p.add_argument("--port", required=True)
    p.add_argument("--ids", default="1-4")
    p.add_argument("--bauds", default=None)
    p.add_argument("--timeout", type=float, default=0.3)
    args = p.parse_args()

    ids = parse_ids(args.ids)
    bauds = [int(b) for b in args.bauds.split(",")] if args.bauds else DEFAULT_BAUDS

    print("=" * 72)
    print(" EV Drive Modbus ASCII probe (read-only)")
    print(f" Port {args.port} | IDs {ids} | bauds {bauds}")
    print("=" * 72)

    found = False
    for baud in bauds:
        try:
            ser = serial.Serial(port=args.port, baudrate=baud,
                                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE, timeout=args.timeout)
        except serial.SerialException as e:
            print(f"\n[{baud:>6} bps] could not open port: {e}")
            continue

        print(f"\n[{baud:>6} bps]")
        try:
            for sid in ids:
                status, raw = probe(ser, sid)
                if status == "timeout":
                    print(f"  ID {sid:>2}: timeout")
                elif status == "ascii":
                    found = True
                    print(f"  ID {sid:>2}: *** ASCII RESPONSE *** {raw!r}")
                else:
                    print(f"  ID {sid:>2}: non-ASCII bytes {raw.hex(' ')}")
        finally:
            ser.close()

    print("\n" + "=" * 72)
    if found:
        print(" RESULT: drive answered in Modbus ASCII -> protocol dial SW2-5 is ON")
        print("         (ASCII). Set SW2-5 OFF for RTU and repower to restore the")
        print("         RTU tools, or note the baud/ID shown above.")
    else:
        print(" RESULT: no ASCII reply either. If RTU is also silent, the protocol")
        print("         dial is probably not the cause -- recheck SW2 dials and the")
        print("         A/B wiring, and confirm a repower happened after any change.")
    print("=" * 72)
    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
