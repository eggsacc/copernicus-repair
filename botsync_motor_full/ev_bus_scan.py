#!/usr/bin/env python3
"""
EV Drive RS485 bus scanner / diagnostic  (READ-ONLY, cannot move the motor)
===========================================================================

When the drives go silent, this sweeps every supported baud rate and a range of
slave IDs, sending a harmless FC03 read of one Monitor register (0x4600) to each,
and reports exactly what (if anything) comes back -- including malformed/partial
byte bursts, which tell us the difference between "wrong baud" and "dead bus".

Read-only: only issues FC03 reads. It never writes or commands motion.

    python ev_bus_scan.py --port COM3
    python ev_bus_scan.py --port COM3 --ids 1-15 --bauds 115200,57600
"""

import argparse
import sys
import time

from ev_modbus_test import serial, build_read_holding_registers, check_crc

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


def probe(ser, slave_id):
    """Send one FC03 read; return ('ok'|'exception'|'partial'|'timeout', raw_bytes)."""
    req = build_read_holding_registers(slave_id, MONITOR_BASE, 1)
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.01)
    resp = ser.read(64)
    if not resp:
        return "timeout", resp
    if len(resp) < 5:
        return "partial", resp
    if not check_crc(resp):
        return "badcrc", resp
    if resp[1] & 0x80:
        return "exception", resp
    return "ok", resp


def main():
    p = argparse.ArgumentParser(description="Read-only RS485 scan for Trumman EV drives.")
    p.add_argument("--port", required=True)
    p.add_argument("--ids", default="1-4", help="IDs to probe, e.g. 1-15 or 1,2,5")
    p.add_argument("--bauds", default=None,
                   help="Comma-separated bauds (default: all 5 standard rates)")
    p.add_argument("--timeout", type=float, default=0.2)
    args = p.parse_args()

    ids = parse_ids(args.ids)
    bauds = [int(b) for b in args.bauds.split(",")] if args.bauds else DEFAULT_BAUDS

    print("=" * 72)
    print(" EV Drive RS485 bus scan (read-only)")
    print(f" Port {args.port} | IDs {ids} | bauds {bauds}")
    print("=" * 72)

    any_response = False
    any_valid = False

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
                    print(f"  ID {sid:>2}: timeout (no bytes)")
                    continue
                any_response = True
                hexs = raw.hex(" ")
                if status == "ok":
                    any_valid = True
                    print(f"  ID {sid:>2}: *** VALID RESPONSE *** {hexs}")
                elif status == "exception":
                    any_valid = True
                    print(f"  ID {sid:>2}: exception reply (drive alive!) {hexs}")
                elif status == "badcrc":
                    print(f"  ID {sid:>2}: bytes w/ bad CRC (baud/noise?) {hexs}")
                else:  # partial
                    print(f"  ID {sid:>2}: partial bytes ({len(raw)}) {hexs}")
        finally:
            ser.close()

    print("\n" + "=" * 72)
    if any_valid:
        print(" RESULT: at least one drive answered correctly -- note the baud/ID above.")
    elif any_response:
        print(" RESULT: bytes came back but never a clean frame -- suspect baud/parity")
        print("         mismatch or a noisy/half-broken RS485 line (not a dead bus).")
    else:
        print(" RESULT: total silence at every baud and ID -- the drives are not")
        print("         transmitting at all. Most likely drive power is down / faulted,")
        print("         or the A/B pair is open. Check drive power & repower, verify")
        print("         the bus wiring, then re-scan.")
    print("=" * 72)
    sys.exit(0 if any_valid else 1)


if __name__ == "__main__":
    main()
