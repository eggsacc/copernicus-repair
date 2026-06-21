#!/usr/bin/env python3
"""
EV Drive serial-format sweep  (READ-ONLY, cannot move the motor)
================================================================

Baud and protocol (RTU/ASCII) are already ruled out. This sweeps the remaining
serial-format variables -- parity (N/E/O), stop bits (1/2), data bits (8/7) --
at a fixed baud, sending an RTU FC03 read to each ID. If drive parameter 09-16
(RS485 physical settings) got changed away from the 8-N-1 default, one of these
combinations will get a valid reply.

Read-only: FC03 reads only.

    python ev_format_scan.py --port COM3 --baud 115200 --ids 1,2
"""

import argparse
import sys
import time

from ev_modbus_test import serial, build_read_holding_registers, check_crc

MONITOR_BASE = 0x4600


def probe(ser, slave_id):
    req = build_read_holding_registers(slave_id, MONITOR_BASE, 1)
    ser.reset_input_buffer()
    ser.write(req)
    time.sleep(0.01)
    resp = ser.read(64)
    if not resp:
        return "timeout", resp
    if len(resp) >= 5 and check_crc(resp) and not (resp[1] & 0x80):
        return "ok", resp
    if len(resp) >= 5 and check_crc(resp):
        return "exception", resp
    return "bytes", resp


def main():
    p = argparse.ArgumentParser(description="Read-only serial-format sweep for EV drives.")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--ids", default="1,2")
    p.add_argument("--timeout", type=float, default=0.2)
    args = p.parse_args()

    ids = [int(x) for x in args.ids.split(",")]
    parities = [("N", serial.PARITY_NONE), ("E", serial.PARITY_EVEN), ("O", serial.PARITY_ODD)]
    stopbits = [(1, serial.STOPBITS_ONE), (2, serial.STOPBITS_TWO)]
    databits = [(8, serial.EIGHTBITS), (7, serial.SEVENBITS)]

    print("=" * 72)
    print(f" EV Drive serial-format sweep (read-only) @ {args.baud} bps | IDs {ids}")
    print("=" * 72)

    found = False
    for dlabel, dbits in databits:
        for plabel, par in parities:
            for slabel, sbits in stopbits:
                fmt = f"{dlabel}-{plabel}-{slabel}"
                try:
                    ser = serial.Serial(port=args.port, baudrate=args.baud,
                                        bytesize=dbits, parity=par, stopbits=sbits,
                                        timeout=args.timeout)
                except (serial.SerialException, ValueError) as e:
                    print(f"  {fmt}: cannot open ({e})")
                    continue
                try:
                    results = []
                    for sid in ids:
                        status, raw = probe(ser, sid)
                        if status in ("ok", "exception"):
                            found = True
                            results.append(f"ID{sid}=*{status.upper()}* {raw.hex(' ')}")
                        elif status == "bytes":
                            results.append(f"ID{sid}=bytes:{raw.hex(' ')}")
                        else:
                            results.append(f"ID{sid}=timeout")
                    print(f"  {fmt}: " + "  ".join(results))
                finally:
                    ser.close()

    print("\n" + "=" * 72)
    if found:
        print(" RESULT: a non-default serial format replied -- param 09-16 was changed.")
        print("         Note the format above and use it (or reset 09-16 to 0 = 8-N-1).")
    else:
        print(" RESULT: no reply in any serial format. Combined with RTU+ASCII silence,")
        print("         this points away from serial settings and toward the physical")
        print("         line: swap A/B polarity, reseat the pair, check the SW2-4")
        print("         termination dial, then re-run the RTU scan.")
    print("=" * 72)
    sys.exit(0 if found else 1)


if __name__ == "__main__":
    main()
