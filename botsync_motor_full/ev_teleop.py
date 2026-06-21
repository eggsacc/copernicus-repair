#!/usr/bin/env python3
"""
EV Drive WASD teleop  (differential drive, SPEED mode)
======================================================

Keyboard tele-operation for the two-drive robot:
  ID 1 = LEFT wheels, ID 2 = RIGHT wheels  (configurable).

Keys (each press adds a constant velocity step; see --step):
  w  : increase ALL wheels        (drive forward / speed up)
  a  : increase RIGHT wheels only (turn left)
  d  : increase LEFT wheels only  (turn right)
  s  : STOP all wheels (set both sides to 0)
  space : same as 's' (emergency stop)
  q  : quit (stops motors and closes)

A small UI window shows the commanded and actual left/right wheel speeds, bus
voltage and status. The window must have focus for keys to register.

Control model (from EV_DRIVE_CONTROL_GUIDE.md):
  - Drives are in SPEED mode (08-01=0); servo enabled on power-up (no SVON).
  - Speed setpoint -> Speed No.0 RAM 0x3F08 (HARD MIN 60 r/min; lower is rejected).
  - Run/stop+direction -> NET-IO 0x1400: 0x0001=CW, 0x0003=CCW, 0x0000=stop.
  - Keys only add positive velocity; per-side spin direction is set by the
    invert state. ID2 (right) is REVERSED by default (spins CCW for positive) so
    both wheels drive the robot "forward"; toggle either side live with the UI
    "Flip ... dir" buttons, or at launch with --invert-left / --no-invert-right.

Architecture: a worker thread owns the serial port (slow ~300ms reads must not
freeze the UI); the tkinter UI only mutates shared target speeds under a lock.

    python ev_teleop.py --port COM3
    python ev_teleop.py --port COM3 --dry-run     # UI only, no motion
"""

import argparse
import threading
import time
import tkinter as tk

from ev_modbus_test import (
    serial, read_holding_registers, write_single_register, to_signed16,
    ModbusError, ModbusTimeout, ALARM_CODES,
)

REG_SPEED0 = 0x3F08
REG_ACC0 = 0x4000
REG_DEC0 = 0x4009
REG_TQ0 = 0x4300
REG_NETIO = 0x1400
REG_STATE = 0x4600

NETIO_CW = 0x0001
NETIO_CCW = 0x0003
NETIO_STOP = 0x0000
SPEED_FLOOR = 60


class Teleop:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        # commanded magnitudes (r/min, >= 0); 'L' = left (id1), 'R' = right (id2)
        self.target = {"L": 0, "R": 0}
        self.actual = {"L": 0, "R": 0}
        self.bus = {"L": 0.0, "R": 0.0}
        self.alarm = {"L": 0, "R": 0}
        self.sid = {"L": args.left_id, "R": args.right_id}
        # per-side max speed (fall back to --max) and direction-invert state
        self.max = {"L": args.max_left or args.max, "R": args.max_right or args.max}
        self.invert = {"L": args.invert_left, "R": not args.no_invert_right}
        self.status = "starting..."
        self.running = True
        self.ser = None
        self.worker = threading.Thread(target=self._worker, daemon=True)

    # ---- shared-state helpers (UI thread calls these) ----
    def bump(self, side, delta):
        with self.lock:
            self.target[side] = max(0, min(self.max[side], self.target[side] + delta))

    def stop_all(self):
        with self.lock:
            self.target["L"] = 0
            self.target["R"] = 0

    def flip(self, side):
        """Toggle the spin direction of one side (live)."""
        with self.lock:
            self.invert[side] = not self.invert[side]

    def snapshot(self):
        with self.lock:
            return (dict(self.target), dict(self.actual), dict(self.bus),
                    dict(self.alarm), dict(self.invert), self.status)

    def set_status(self, msg):
        with self.lock:
            self.status = msg

    # ---- worker thread (owns the serial port) ----
    def _worker(self):
        if self.args.dry_run:
            self.set_status("DRY RUN — UI only, no serial")
            while self.running:
                with self.lock:  # echo targets to actual so the UI shows something
                    self.actual["L"], self.actual["R"] = self.target["L"], self.target["R"]
                time.sleep(0.1)
            return

        try:
            self.ser = serial.Serial(
                port=self.args.port, baudrate=self.args.baud, bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                timeout=self.args.timeout)
        except serial.SerialException as e:
            self.set_status(f"serial open FAILED: {e}")
            self.running = False
            return

        # One-time operation data: gentle accel, capped torque.
        try:
            for side in ("L", "R"):
                write_single_register(self.ser, self.sid[side], REG_ACC0, self.args.accel)
                write_single_register(self.ser, self.sid[side], REG_DEC0, self.args.decel)
                write_single_register(self.ser, self.sid[side], REG_TQ0, self.args.torque_cap)
            self.set_status("ready — focus window, use WASD")
        except (ModbusError, ModbusTimeout) as e:
            self.set_status(f"setup write failed: {e}")

        last_speed = {"L": None, "R": None}
        last_netio = {"L": None, "R": None}
        tick = 0
        try:
            while self.running:
                with self.lock:
                    tgt = dict(self.target)
                    inv = dict(self.invert)
                for side in ("L", "R"):
                    self._send_side(side, tgt[side], inv[side], last_speed, last_netio)
                tick += 1
                if tick % self.args.poll_every == 0:
                    self._read_telemetry()
                time.sleep(0.05)
        finally:
            self._emergency_stop(last_netio)
            try:
                self.ser.close()
            except Exception:
                pass

    def _send_side(self, side, target, invert, last_speed, last_netio):
        sid = self.sid[side]
        try:
            if target <= 0:
                if last_netio[side] != NETIO_STOP:
                    write_single_register(self.ser, sid, REG_NETIO, NETIO_STOP)
                    last_netio[side] = NETIO_STOP
                last_speed[side] = 0
            else:
                mag = max(SPEED_FLOOR, min(self.max[side], target))
                direction = NETIO_CCW if invert else NETIO_CW
                if last_speed[side] != mag:
                    write_single_register(self.ser, sid, REG_SPEED0, mag)
                    last_speed[side] = mag
                if last_netio[side] != direction:
                    write_single_register(self.ser, sid, REG_NETIO, direction)
                    last_netio[side] = direction
        except (ModbusError, ModbusTimeout) as e:
            self.set_status(f"{side} write err: {e}")

    def _read_telemetry(self):
        for side in ("L", "R"):
            try:
                r = read_holding_registers(self.ser, self.sid[side], REG_STATE, 8)
                alarm, busv, spd = r[1], r[7] * 0.01, to_signed16(r[4])
                with self.lock:
                    self.actual[side] = spd
                    self.alarm[side] = alarm
                    self.bus[side] = busv
                if alarm != 0:
                    self.stop_all()
                    self.set_status(f"{side} ALARM {alarm} "
                                    f"({ALARM_CODES.get(alarm, '?')}) -> STOPPED")
                elif busv < self.args.bus_floor:
                    self.stop_all()
                    self.set_status(f"{side} under-voltage {busv:.1f}V -> STOPPED")
            except (ModbusError, ModbusTimeout) as e:
                self.set_status(f"{side} read err: {e}")

    def _emergency_stop(self, last_netio):
        for side in ("L", "R"):
            try:
                write_single_register(self.ser, self.sid[side], REG_NETIO, NETIO_STOP)
            except Exception:
                pass

    def shutdown(self):
        self.running = False
        if self.worker.is_alive():
            self.worker.join(timeout=2.0)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def build_ui(tele):
    args = tele.args
    root = tk.Tk()
    root.title("EV Drive WASD Teleop")
    root.configure(bg="#101418")
    root.geometry("560x420")

    fg, accent, dim = "#e8eef2", "#39d3a6", "#7d8a93"

    banner = "DRY RUN (no motion)" if args.dry_run else "LIVE — motors will move"
    tk.Label(root, text=banner, fg=("#f0c040" if args.dry_run else "#ff6b6b"),
             bg="#101418", font=("Consolas", 12, "bold")).pack(pady=(10, 0))
    tk.Label(root, text=f"ID{args.left_id}=LEFT   ID{args.right_id}=RIGHT   "
                        f"step {args.step}   max  L {tele.max['L']} / R {tele.max['R']} r/min",
             fg=dim, bg="#101418", font=("Consolas", 10)).pack()

    grid = tk.Frame(root, bg="#101418")
    grid.pack(pady=14)

    def wheel_panel(col, name):
        f = tk.Frame(grid, bg="#1a2026", padx=24, pady=14)
        f.grid(row=0, column=col, padx=12)
        tk.Label(f, text=name, fg=accent, bg="#1a2026",
                 font=("Consolas", 14, "bold")).pack()
        cmd = tk.Label(f, text="0", fg=fg, bg="#1a2026", font=("Consolas", 40, "bold"))
        cmd.pack()
        tk.Label(f, text="commanded r/min", fg=dim, bg="#1a2026",
                 font=("Consolas", 9)).pack()
        act = tk.Label(f, text="act —", fg=dim, bg="#1a2026", font=("Consolas", 12))
        act.pack(pady=(6, 0))
        dirn = tk.Label(f, text="dir —", fg=accent, bg="#1a2026",
                        font=("Consolas", 11, "bold"))
        dirn.pack(pady=(2, 0))
        return cmd, act, dirn

    left_cmd, left_act, left_dir = wheel_panel(0, "LEFT")
    right_cmd, right_act, right_dir = wheel_panel(1, "RIGHT")

    # Direction-flip buttons (takefocus=0 + refocus root so WASD keeps working)
    btns = tk.Frame(root, bg="#101418")
    btns.pack(pady=(6, 0))

    def make_flip(side):
        def cb():
            tele.flip(side)
            root.focus_set()
        return cb

    tk.Button(btns, text="Flip LEFT dir", command=make_flip("L"), takefocus=0,
              font=("Consolas", 10)).grid(row=0, column=0, padx=8)
    tk.Button(btns, text="Flip RIGHT dir", command=make_flip("R"), takefocus=0,
              font=("Consolas", 10)).grid(row=0, column=1, padx=8)

    bus_lbl = tk.Label(root, text="bus —", fg=dim, bg="#101418", font=("Consolas", 10))
    bus_lbl.pack()
    status_lbl = tk.Label(root, text="", fg=fg, bg="#101418", font=("Consolas", 11),
                          wraplength=520)
    status_lbl.pack(pady=(8, 0))

    tk.Label(root, text="W = all up    A = right up    D = left up    "
                        "S / Space = STOP    Q = quit",
             fg=dim, bg="#101418", font=("Consolas", 10)).pack(side="bottom", pady=10)

    def on_key(event):
        k = event.keysym.lower()
        if k == "w":
            tele.bump("L", args.step); tele.bump("R", args.step)
        elif k == "a":
            tele.bump("R", args.step)         # 'a' -> right wheels
        elif k == "d":
            tele.bump("L", args.step)         # 'd' -> left wheels
        elif k in ("s", "space"):
            tele.stop_all()
        elif k == "q":
            on_close()

    def refresh():
        tgt, act, bus, alarm, invert, status = tele.snapshot()
        left_cmd.config(text=str(tgt["L"]))
        right_cmd.config(text=str(tgt["R"]))
        left_act.config(text=("act —" if args.dry_run else f"act {act['L']:+d}"))
        right_act.config(text=("act —" if args.dry_run else f"act {act['R']:+d}"))
        left_dir.config(text=("CCW (rev)" if invert["L"] else "CW (fwd)"))
        right_dir.config(text=("CCW (rev)" if invert["R"] else "CW (fwd)"))
        if not args.dry_run:
            bus_lbl.config(text=f"bus  L {bus['L']:.1f}V   R {bus['R']:.1f}V")
        alarmed = (alarm["L"] or alarm["R"])
        status_lbl.config(text=status, fg=("#ff6b6b" if alarmed else fg))
        if tele.running:
            root.after(100, refresh)

    def on_close():
        tele.shutdown()
        root.destroy()

    root.bind("<KeyPress>", on_key)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, refresh)
    root.focus_set()
    return root


def main():
    p = argparse.ArgumentParser(description="WASD teleop for the two-drive EV robot.")
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--left-id", type=int, default=1, help="slave ID of left wheels")
    p.add_argument("--right-id", type=int, default=2, help="slave ID of right wheels")
    p.add_argument("--step", type=int, default=60, help="r/min added per key press")
    p.add_argument("--max", type=int, default=300, help="default max r/min (per-side fallback)")
    p.add_argument("--max-left", type=int, default=None, help="max r/min for LEFT wheels")
    p.add_argument("--max-right", type=int, default=None, help="max r/min for RIGHT wheels")
    p.add_argument("--torque-cap", type=int, default=500, help="1=0.1%% (default 50%%)")
    p.add_argument("--accel", type=int, default=3, help="1=0.1s")
    p.add_argument("--decel", type=int, default=3, help="1=0.1s")
    p.add_argument("--bus-floor", type=float, default=24.0)
    p.add_argument("--poll-every", type=int, default=10,
                   help="telemetry read every N control ticks (~50ms each)")
    p.add_argument("--invert-left", action="store_true",
                   help="spin LEFT wheels CCW for positive velocity")
    p.add_argument("--no-invert-right", action="store_true",
                   help="do NOT reverse the right wheels (default: ID2 is reversed)")
    p.add_argument("--timeout", type=float, default=0.3)
    p.add_argument("--dry-run", action="store_true", help="UI only, no serial/motion")
    args = p.parse_args()

    if args.step < SPEED_FLOOR:
        print(f"Note: step {args.step} < {SPEED_FLOOR} r/min floor; any nonzero "
              f"side is clamped up to {SPEED_FLOOR} when sent to the drive.")

    tele = Teleop(args)
    tele.worker.start()
    root = build_ui(tele)
    try:
        root.mainloop()
    finally:
        tele.shutdown()


if __name__ == "__main__":
    main()
