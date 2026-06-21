#!/usr/bin/env python3
"""
EV Drive Modbus RTU communication test program
================================================

Target hardware : Trumman Technology Corp. EVDR-K045CQE drive / BL90 motor
Protocol ref     : UM-01-S0547 "EV Drive Communication Manual", Rev 1.1
Wire protocol    : Standard Modbus RTU - FC03 (read holding regs), FC06
                   (write single reg), FC10 (write multiple regs)

What this does
--------------
- "diag"  : pings each slave ID, then reads and decodes the Monitor Data
            block (motor state, alarm code, speed, voltage, current, etc.)
            and the alarm history. This is the first thing to run when
            troubleshooting a comms link to the drives.
- "read"  : raw FC03 read of N holding registers from one drive (for
            poking at any register in the manual, e.g. parameters).
- "write" : raw FC06 write of a single holding register to one drive.

SAFETY
------
This script only *reads* status in "diag" mode -- it cannot move the motor.
"read"/"write" let you touch any register in the manual, including control
mode/maintenance registers. Do not write to operation/control registers
(0x0800 control mode, 0x1400 NET-IO, 0x0A00 maintenance commands, etc.)
unless you are prepared for the motor to move and have an e-stop ready.

Requirements
------------
    pip install pyserial

Examples
--------
    # Full comms + status check on slave ID 1 and 2
    python3 ev_modbus_test.py --port /dev/ttyUSB0 --baud 115200 diag

    # Same, on Windows, only ID 2
    python3 ev_modbus_test.py --port COM5 --baud 115200 diag --ids 2

    # Raw read of the Monitor Data block (16 regs starting at 0x4600)
    python3 ev_modbus_test.py --port /dev/ttyUSB0 read --id 1 --addr 0x4600 --count 16

    # Raw write (e.g. 0x0A00 = alarm reset, write 1)
    python3 ev_modbus_test.py --port /dev/ttyUSB0 write --id 1 --addr 0x0A00 --value 1

Notes on the physical link (manual section 2 / "Setting Items" table)
-----------------------------------------------------------------------
- Slave ID is set by the drive's SW1 dial (1 = ID 1, 2 = ID 2, ... 0 = broadcast).
- Baud rate is set by the SW2 dial (9600 / 19200 / 38400 / 57600 / 115200) --
  this script's --baud must match the dial, it is not remotely settable.
- Default serial format is 8 data bits, no parity, 1 stop bit (param 09-16 = 0).
- A repower is needed after changing any dial/parameter for it to take effect.
- RS232 cannot be used for Multi-drive; for two drives on one bus use RS485.
"""

import argparse
import struct
import sys
import time

try:
    import serial
except ImportError:
    print("This script needs pyserial. Install it with:  pip install pyserial")
    sys.exit(1)


DEFAULT_TIMEOUT = 0.3  # seconds to wait for a response before declaring timeout
MAX_FRAME_LEN = 256    # generous upper bound on a single-drive response size

# ---------------------------------------------------------------------------
# CRC-16 (Modbus), per manual section 2.1 "CRC-16 calculation method"
# ---------------------------------------------------------------------------
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(frame: bytes) -> bytes:
    crc = crc16(frame)
    return frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])  # low byte first


def check_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    payload, recv_crc = frame[:-2], frame[-2:]
    return append_crc(payload)[-2:] == recv_crc


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ModbusError(Exception):
    pass


class ModbusTimeout(ModbusError):
    pass


EXCEPTION_CODES = {
    # 16-bit EC values (default, param 09-16 bit5 = 0) and 8-bit "standard
    # Modbus" EC values (param 09-16 bit5 = 1) both included, per section 2.2.3
    0x01: "Invalid function code",
    0x88: "Invalid function code",
    0x02: "Invalid register address",
    0x03: "Invalid data or data length out of range",
    0x8C: "Invalid data / parameter setting out of range",
    0x04: "Slave error (generic)",
    0x85: "Slave error: timeout",
    0x8D: "Slave error: command not allowed while motor is running",
}


def decode_exception_code(code: int) -> str:
    return EXCEPTION_CODES.get(code, f"Unknown exception code 0x{code:02X}")


class ModbusException(ModbusError):
    def __init__(self, function_code: int, exception_code: int):
        self.function_code = function_code
        self.exception_code = exception_code
        super().__init__(
            f"Slave returned exception on FC 0x{function_code:02X}: "
            f"{decode_exception_code(exception_code)} (code 0x{exception_code:02X})"
        )


# ---------------------------------------------------------------------------
# Frame builders (manual section 2.3)
# ---------------------------------------------------------------------------
def build_read_holding_registers(slave_id: int, start_addr: int, count: int) -> bytes:
    if not (1 <= count <= 16):
        raise ValueError("EV drive supports reading 1-16 holding registers per query")
    frame = struct.pack(">BBHH", slave_id, 0x03, start_addr, count)
    return append_crc(frame)


def build_write_single_register(slave_id: int, addr: int, value: int) -> bytes:
    frame = struct.pack(">BBHH", slave_id, 0x06, addr, value & 0xFFFF)
    return append_crc(frame)


def build_write_multiple_registers(slave_id: int, start_addr: int, values) -> bytes:
    if not (1 <= len(values) <= 16):
        raise ValueError("EV drive supports writing 1-16 holding registers per query")
    byte_count = len(values) * 2
    frame = struct.pack(">BBHHB", slave_id, 0x10, start_addr, len(values), byte_count)
    for v in values:
        frame += struct.pack(">H", v & 0xFFFF)
    return append_crc(frame)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def transact(ser: "serial.Serial", request: bytes) -> bytes:
    ser.reset_input_buffer()
    ser.write(request)
    time.sleep(0.005)  # Tb2 transmission-waiting allowance per manual section 2
    resp = ser.read(MAX_FRAME_LEN)
    if not resp:
        raise ModbusTimeout("No response from slave (timeout)")
    if len(resp) < 5:
        raise ModbusError(f"Response too short ({len(resp)} bytes): {resp.hex()}")
    if not check_crc(resp):
        raise ModbusError(f"CRC mismatch in response: {resp.hex()}")

    fc = resp[1]
    if fc & 0x80:
        ec_bytes = resp[2:-2]
        ec = int.from_bytes(ec_bytes, "big")
        raise ModbusException(fc & 0x7F, ec)
    return resp


def read_holding_registers(ser, slave_id: int, start_addr: int, count: int):
    req = build_read_holding_registers(slave_id, start_addr, count)
    resp = transact(ser, req)
    byte_count = resp[2]
    data = resp[3 : 3 + byte_count]
    if len(data) != byte_count:
        raise ModbusError("Incomplete register data in response")
    return list(struct.unpack(f">{byte_count // 2}H", data))


def write_single_register(ser, slave_id: int, addr: int, value: int):
    req = build_write_single_register(slave_id, addr, value)
    return transact(ser, req)


def write_multiple_registers(ser, slave_id: int, start_addr: int, values):
    req = build_write_multiple_registers(slave_id, start_addr, values)
    return transact(ser, req)


# ---------------------------------------------------------------------------
# Decoding tables (manual sections 3.3.1-3.3.3 and Annex A1)
# ---------------------------------------------------------------------------
def to_signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


MOTOR_STATE = {
    0: "STOP",
    2: "RUN",
    3: "EBRAKE",
    4: "FREE",
    5: "FAULT",
    6: "WAIT/INHIBIT (SERVO OFF)",
    7: "MOVING (SERVO ON)",
    8: "SLIGHT-POS-KEEPING",
}

ALARM_CODES = {
    0: "No alarm",
    1: "Overcurrent",
    2: "Overload",
    3: "Motor feedback fault",
    4: "Over voltage",
    5: "Under voltage",
    6: "Drive overheat",
    7: "Startup fault",
    8: "EEP data error",
    10: "Motor overheat",
    12: "Over speed",
    13: "Encoder signal fault",
    14: "Prevention of operation at power on",
    15: "External stop",
    20: "Hall sequence fault",
    21: "Communication error",
    22: "Parameter error",
}

# Monitor Data block, registers 0x4600-0x4616 (section 3.3.3), read-only.
# (offset from 0x4600, field name, decode function)
MONITOR_BASE = 0x4600
MONITOR_FIELDS = [
    (0x00, "Motor state", lambda v: MOTOR_STATE.get(v, f"Unknown ({v})")),
    (0x01, "Alarm code", lambda v: ALARM_CODES.get(v, f"Unknown ({v})")),
    (0x02, "Operation data No.", lambda v: v),
    (0x03, "Command speed (r/min)", lambda v: to_signed16(v)),
    (0x04, "Motor speed (r/min)", lambda v: to_signed16(v)),
    (0x05, "Direct I/O status", lambda v: f"0b{v:016b}"),
    (0x06, "Output power (W)", lambda v: v),
    (0x07, "DC bus voltage (V)", lambda v: round(v * 0.01, 2)),
    (0x08, "Output (%)", lambda v: round(to_signed16(v) * 0.1, 1)),
    (0x09, "Output current (A)", lambda v: round(v * 0.01, 2)),
    (0x0A, "Torque limit current (A)", lambda v: round(v * 0.01, 2)),
    (0x0B, "Acceleration time (s)", lambda v: round(v * 0.1, 1)),
    (0x0C, "Deceleration time (s)", lambda v: round(v * 0.1, 1)),
    (0x0D, "A1 input voltage (V)", lambda v: round(v * 0.01, 2)),
    (0x0F, "X5(XH) duty (%)", lambda v: round(v * 0.1, 1)),
    (0x10, "X5(XH) frequency", lambda v: v),
    (0x12, "Hall count", lambda v: to_signed16(v)),
    (0x13, "Target position idx (multi-drive)", lambda v: to_signed16(v)),
    (0x14, "Target position step (multi-drive)", lambda v: v),
    (0x15, "Motor position idx (multi-drive)", lambda v: to_signed16(v)),
    (0x16, "Motor position step (multi-drive)", lambda v: v),
]

ALARM_HISTORY_BASE = 0x3300  # 10 registers, most recent first (section 3.3.4)


def read_monitor_block(ser, slave_id: int) -> dict:
    # FC03 is limited to 16 registers/query, monitor block is 0x17 (23) regs long
    regs = read_holding_registers(ser, slave_id, MONITOR_BASE, 16)        # 4600h-460Fh
    regs += read_holding_registers(ser, slave_id, MONITOR_BASE + 0x10, 7)  # 4610h-4616h
    return {name: decoder(regs[offset]) for offset, name, decoder in MONITOR_FIELDS}


def read_alarm_history(ser, slave_id: int):
    regs = read_holding_registers(ser, slave_id, ALARM_HISTORY_BASE, 10)
    return [ALARM_CODES.get(r, f"Unknown ({r})") for r in regs]


def ping(ser, slave_id: int, retries: int = 2):
    last_err = "unknown error"
    for _ in range(retries + 1):
        try:
            t0 = time.time()
            read_holding_registers(ser, slave_id, MONITOR_BASE, 1)
            rtt_ms = (time.time() - t0) * 1000
            return True, rtt_ms, None
        except ModbusTimeout as e:
            last_err = str(e)
            continue
        except ModbusError as e:
            return False, None, str(e)
    return False, None, last_err


# ---------------------------------------------------------------------------
# Diagnostic routine
# ---------------------------------------------------------------------------
def run_diagnostics(ser, ids):
    print("=" * 72)
    print(" EV Drive Modbus RTU Communication Test")
    print(" Drive: EVDR-K045CQE  |  Motor: BL90")
    print("=" * 72)

    results = {}
    for slave_id in ids:
        print(f"\n--- Slave ID {slave_id} ---")
        ok, rtt_ms, err = ping(ser, slave_id)
        if not ok:
            print(f"  [FAIL] No communication with slave {slave_id}: {err}")
            print("         Check: A/B polarity and termination resistor on the RS485")
            print("         bus, the SW1 slave-ID dial, the SW2 baud-rate dial (must")
            print("         match --baud), and that no other drive shares this ID.")
            results[slave_id] = False
            continue

        print(f"  [OK] Slave responded in {rtt_ms:.1f} ms")

        try:
            data = read_monitor_block(ser, slave_id)
        except ModbusError as e:
            print(f"  [FAIL] Could not read monitor data block: {e}")
            results[slave_id] = False
            continue

        for name, value in data.items():
            print(f"    {name:<38}: {value}")

        if data["Alarm code"] != "No alarm":
            print(f"  [ALARM ACTIVE] {data['Alarm code']}")

        try:
            history = [h for h in read_alarm_history(ser, slave_id) if h != "No alarm"]
            if history:
                print("  Alarm history (most recent first):")
                for i, h in enumerate(history, 1):
                    print(f"    {i}. {h}")
        except ModbusError as e:
            print(f"  [WARN] Could not read alarm history: {e}")

        results[slave_id] = True

    print("\n" + "=" * 72)
    print(" Summary")
    print("=" * 72)
    for slave_id, ok in results.items():
        print(f"  Slave ID {slave_id}: {'PASS' if ok else 'FAIL'}")
    print()
    return all(results.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Modbus RTU comms test for Trumman EVDR-K045CQE drives (BL90 motor)."
    )
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/ttyUSB0 or COM5")
    parser.add_argument(
        "--baud", type=int, default=115200,
        help="Baud rate -- must match the drive's SW2 dial (default 115200)",
    )
    parser.add_argument(
        "--parity", default="N", choices=["N", "E", "O"],
        help="Serial parity (default N, matches drive parameter 09-16 default)",
    )
    parser.add_argument(
        "--stopbits", type=int, default=1, choices=[1, 2],
        help="Stop bits (default 1, matches drive parameter 09-16 default)",
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="Per-query response timeout in seconds (default 0.3)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    diag = sub.add_parser("diag", help="Ping + read status/alarms from one or more drives")
    diag.add_argument("--ids", default="1,2", help="Comma-separated slave IDs (default: 1,2)")

    read_cmd = sub.add_parser("read", help="Raw FC03 read of N holding registers")
    read_cmd.add_argument("--id", type=int, required=True)
    read_cmd.add_argument("--addr", type=lambda x: int(x, 0), required=True, help="e.g. 0x4600")
    read_cmd.add_argument("--count", type=int, default=1)

    write_cmd = sub.add_parser("write", help="Raw FC06 write of a single holding register")
    write_cmd.add_argument("--id", type=int, required=True)
    write_cmd.add_argument("--addr", type=lambda x: int(x, 0), required=True)
    write_cmd.add_argument("--value", type=lambda x: int(x, 0), required=True)

    args = parser.parse_args()

    parity_map = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}
    stopbits_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=parity_map[args.parity],
            stopbits=stopbits_map[args.stopbits],
            timeout=args.timeout,
        )
    except serial.SerialException as e:
        print(f"Could not open serial port {args.port}: {e}")
        sys.exit(1)

    try:
        if args.cmd == "diag":
            ids = [int(x) for x in args.ids.split(",")]
            ok = run_diagnostics(ser, ids)
            sys.exit(0 if ok else 1)

        elif args.cmd == "read":
            regs = read_holding_registers(ser, args.id, args.addr, args.count)
            for i, r in enumerate(regs):
                print(f"  0x{args.addr + i:04X}: {r} (0x{r:04X})")

        elif args.cmd == "write":
            write_single_register(ser, args.id, args.addr, args.value)
            print(
                f"  Wrote {args.value} (0x{args.value:04X}) to register "
                f"0x{args.addr:04X} on slave {args.id}"
            )

    except ModbusError as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
