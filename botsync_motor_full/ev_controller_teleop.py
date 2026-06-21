#!/usr/bin/env python3
"""
EV Drive F710 controller teleop  (differential drive, SPEED mode)
=================================================================

Tele-operate the two-drive robot with a Logitech F710 gamepad (XInput mode),
with a tkinter UI to pick the control style and configure limits live.

Two control types (toggle in the UI, or with the controller's A button):

  1) JOYSTICK  — continuous/analog:
       * LEFT stick, Y axis only   -> forward / reverse velocity   (v)
       * RIGHT stick, X axis only  -> turn rate (yaw)               (w)
     Mixed as a differential "arcade" drive:  left = v + w,  right = v - w.

  2) D-PAD  — incremental, like the WASD teleop:
       * Up / Down  -> step forward-velocity accumulator  up / down
       * Left/Right -> step turn accumulator              left / right
     Each press adds one step (holding auto-repeats); A/B/X/Y still work.

Utility buttons (both modes):
       A : switch control mode  (Joystick <-> D-pad)
       B : cycle speed gear     (25 / 50 / 75 / 100 % of max velocity)
       X : HALT  — zero both motors immediately (user-requested soft stop)
       Y : ARM / DISARM         (disarmed = motors held stopped)
   Start : ARM        Back : DISARM + HALT
     Esc : quit

Configurable in the UI: max velocity (r/min), turn gain, joystick deadzone,
D-pad step size, and per-motor direction invert (mirroring / wiring fixes).

How motion reaches the drives (verified facts, see EV_DRIVE_CONTROL_GUIDE.md):
  - SPEED mode (08-01=0); servo enabled on power-up (no SVON).
  - Speed setpoint -> Speed No.0 RAM 0x3F08 (HARD MIN 60 r/min; lower is rejected).
  - Run/stop+direction -> NET-IO 0x1400: 0x0001=CW, 0x0003=CCW, 0x0000=stop.
  - The drive takes a *magnitude* + a *direction bit*; reverse = flip the bit.
    "Chassis-forward" maps to CW on the left wheels and (mirrored) CCW on the
    right wheels, so the right side is inverted by default. A signed wheel
    command's sign chooses the direction; its magnitude is floored at 60 and
    clamped to the max.

Architecture (per README): a worker thread owns the serial port (slow ~300 ms
reads must not freeze the UI). The UI thread polls the gamepad (fast, local) and
publishes a signed per-wheel command + config under a lock; the worker consumes
them, writes on change, and monitors alarms / bus voltage -> stop.

    python ev_controller_teleop.py --port COM3
    python ev_controller_teleop.py --dry-run        # UI + gamepad, no serial
"""

import argparse
import threading
import time
import tkinter as tk

from ev_modbus_test import (
    serial, read_holding_registers, write_single_register, to_signed16,
    ModbusError, ModbusTimeout, ALARM_CODES,
)

try:
    import XInput
except ImportError:
    raise SystemExit(
        "This script needs the 'XInput-Python' package for the gamepad.\n"
        "Install it with:\n    pip install XInput-Python"
    )

# --- drive registers / values (from EV_DRIVE_CONTROL_GUIDE.md) --------------
REG_SPEED0 = 0x3F08   # Speed No.0 setpoint (r/min), RAM
REG_ACC0   = 0x4000   # accel time No.0 (1 = 0.1 s)
REG_DEC0   = 0x4009   # decel time No.0
REG_TQ0    = 0x4300   # torque limit No.0 (1 = 0.1 %)
REG_NETIO  = 0x1400   # remote NET-IO run/direction
REG_STATE  = 0x4600   # monitor block base

NETIO_CW   = 0x0001   # run CW  (positive motor speed)
NETIO_CCW  = 0x0003   # run CCW (negative motor speed)
NETIO_STOP = 0x0000
SPEED_FLOOR = 60      # hard minimum setpoint; below this the drive STOPS

GEARS = (0.25, 0.50, 0.75, 1.00)   # speed scales cycled by the B button


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def apply_deadzone(x, dz):
    """Rescale an axis in [-1,1] so it is 0 inside +/-dz and ramps from 0 at
    the deadzone edge to 1 at full deflection (no step at the boundary)."""
    if dz >= 1.0 or abs(x) <= dz:
        return 0.0
    return (abs(x) - dz) / (1.0 - dz) * (1.0 if x > 0 else -1.0)


def arcade_mix(v_rpm, w_rpm, eff_max):
    """Differential 'arcade' mix -> (left, right) signed wheel r/min, clamped.
    +v = forward (both wheels), +w = turn right (left speeds up, right slows)."""
    left  = clamp(v_rpm + w_rpm, -eff_max, eff_max)
    right = clamp(v_rpm - w_rpm, -eff_max, eff_max)
    return left, right


# ===========================================================================
# Robot: shared state + serial worker thread (owns the port)
# ===========================================================================
class Robot:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.sid = {"L": args.left_id, "R": args.right_id}

        # commanded, signed wheel speed (r/min) published by the UI thread
        self.cmd = {"L": 0, "R": 0}
        # telemetry from the drives
        self.actual = {"L": 0, "R": 0}
        self.bus = {"L": 0.0, "R": 0.0}
        self.alarm = {"L": 0, "R": 0}
        # live config the worker honours
        self.max = max(SPEED_FLOOR, args.max)
        self.invert = {"L": args.invert_left, "R": not args.no_invert_right}
        self.armed = False          # start disarmed for safety
        self.connected = False      # gamepad present? (set by UI poll)
        self.status = "starting..."

        self.running = True
        self.ser = None
        self.worker = threading.Thread(target=self._worker, daemon=True)

    # ---- setters/getters used by the UI thread (all locked) ----
    def set_cmd(self, left, right):
        with self.lock:
            self.cmd["L"], self.cmd["R"] = int(left), int(right)

    def set_max(self, value):
        with self.lock:
            self.max = max(SPEED_FLOOR, int(value))

    def set_invert(self, side, value):
        with self.lock:
            self.invert[side] = bool(value)

    def set_connected(self, value):
        with self.lock:
            self.connected = bool(value)
            if not value:                     # lost gamepad -> drop command
                self.cmd["L"] = self.cmd["R"] = 0

    def set_armed(self, value):
        with self.lock:
            self.armed = bool(value)
            if not value:
                self.cmd["L"] = self.cmd["R"] = 0

    def halt(self):
        with self.lock:
            self.cmd["L"] = self.cmd["R"] = 0

    def set_status(self, msg):
        with self.lock:
            self.status = msg

    def snapshot(self):
        with self.lock:
            return {
                "cmd": dict(self.cmd), "actual": dict(self.actual),
                "bus": dict(self.bus), "alarm": dict(self.alarm),
                "invert": dict(self.invert), "armed": self.armed,
                "connected": self.connected, "max": self.max,
                "status": self.status,
            }

    # ---- worker thread ----
    def _worker(self):
        if self.args.dry_run:
            self.set_status("DRY RUN - UI + gamepad only, no serial/motion")
            while self.running:
                with self.lock:   # echo command -> actual so the UI shows motion
                    armed, conn = self.armed, self.connected
                    self.actual["L"] = self.cmd["L"] if (armed and conn) else 0
                    self.actual["R"] = self.cmd["R"] if (armed and conn) else 0
                time.sleep(0.05)
            return

        try:
            self.ser = serial.Serial(
                port=self.args.port, baudrate=self.args.baud,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=self.args.timeout)
        except serial.SerialException as e:
            self.set_status(f"serial open FAILED: {e}")
            self.running = False
            return

        # one-time shaping (RAM): gentle accel/decel, capped torque
        try:
            for side in ("L", "R"):
                write_single_register(self.ser, self.sid[side], REG_ACC0, self.args.accel)
                write_single_register(self.ser, self.sid[side], REG_DEC0, self.args.decel)
                write_single_register(self.ser, self.sid[side], REG_TQ0, self.args.torque_cap)
            self.set_status("ready - connect gamepad, then ARM to drive")
        except (ModbusError, ModbusTimeout) as e:
            self.set_status(f"setup write failed: {e}")

        last_speed = {"L": None, "R": None}
        last_netio = {"L": None, "R": None}
        tick = 0
        try:
            while self.running:
                with self.lock:
                    cmd = dict(self.cmd)
                    inv = dict(self.invert)
                    gated = not (self.armed and self.connected)
                    mx = self.max
                for side in ("L", "R"):
                    signed = 0 if gated else clamp(cmd[side], -mx, mx)
                    self._send_side(side, signed, inv[side], mx, last_speed, last_netio)
                tick += 1
                if tick % self.args.poll_every == 0:
                    self._read_telemetry()
                time.sleep(0.05)
        finally:
            self._emergency_stop()
            try:
                self.ser.close()
            except Exception:
                pass

    def _send_side(self, side, signed, invert, mx, last_speed, last_netio):
        """Translate a signed wheel r/min into setpoint + NET-IO direction.
        Magnitude < floor -> STOP; sign picks CW/CCW (mirrored by `invert`)."""
        sid = self.sid[side]
        try:
            if abs(signed) < SPEED_FLOOR:
                if last_netio[side] != NETIO_STOP:
                    write_single_register(self.ser, sid, REG_NETIO, NETIO_STOP)
                    last_netio[side] = NETIO_STOP
                last_speed[side] = 0          # force re-send on next engage
            else:
                mag = int(clamp(abs(signed), SPEED_FLOOR, mx))
                fwd = NETIO_CCW if invert else NETIO_CW   # "chassis forward" dir
                rev = NETIO_CW if invert else NETIO_CCW
                direction = fwd if signed > 0 else rev
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
                    self.set_armed(False)
                    self.set_status(f"{side} ALARM {alarm} "
                                    f"({ALARM_CODES.get(alarm, '?')}) -> DISARMED")
                elif busv < self.args.bus_floor:
                    self.set_armed(False)
                    self.set_status(f"{side} under-voltage {busv:.1f}V -> DISARMED")
            except (ModbusError, ModbusTimeout) as e:
                self.set_status(f"{side} read err: {e}")

    def _emergency_stop(self):
        for side in ("L", "R"):
            try:
                write_single_register(self.ser, self.sid[side], REG_NETIO, NETIO_STOP)
            except Exception:
                pass

    def shutdown(self):
        self.running = False
        if self.worker.is_alive():
            self.worker.join(timeout=2.0)


# ===========================================================================
# App: tkinter UI + gamepad polling (UI thread)
# ===========================================================================
BG, PANEL, FG, DIM = "#101418", "#1a2026", "#e8eef2", "#7d8a93"
ACCENT, WARN, BAD, GOOD = "#39d3a6", "#f0c040", "#ff6b6b", "#39d3a6"

# auto-repeat for held D-pad (in UI-poll ticks; ~40 Hz -> ~0.4 s delay, ~8/s)
DPAD_REPEAT_DELAY = 16
DPAD_REPEAT_EVERY = 5


class App:
    def __init__(self, root, robot, args):
        self.root = root
        self.robot = robot
        self.args = args
        self.player = args.player

        # control-mapping config (UI-owned)
        self.mode_var = tk.StringVar(value="joy")            # "joy" | "dpad"
        self.max_var = tk.IntVar(value=robot.max)
        self.turn_var = tk.IntVar(value=args.turn_gain)      # %
        self.dz_var = tk.IntVar(value=args.deadzone)         # %
        self.step_var = tk.IntVar(value=args.dpad_step)      # % of max per press
        self.invL_var = tk.BooleanVar(value=robot.invert["L"])
        self.invR_var = tk.BooleanVar(value=robot.invert["R"])
        self.invfwd_var = tk.BooleanVar(value=False)         # flip fwd/back axis
        self.invturn_var = tk.BooleanVar(value=False)        # flip turn axis
        self.gear_idx = args.gear

        # D-pad incremental accumulators (r/min) + edge/hold tracking
        self.v_acc = 0.0
        self.w_acc = 0.0
        self.prev_btn = {}
        self.hold = {}

        # last gamepad snapshot for the input monitor
        self.in_ly = self.in_rx = 0.0
        self.in_btn = {}

        XInput.set_deadzone(XInput.DEADZONE_LEFT_THUMB, 0)   # we apply our own
        XInput.set_deadzone(XInput.DEADZONE_RIGHT_THUMB, 0)
        XInput.set_deadzone(XInput.DEADZONE_TRIGGER, 0)

        self._build_ui()

    # ----------------------------------------------------------------- UI ---
    def _build_ui(self):
        root = self.root
        root.title("EV Drive - F710 Controller Teleop")
        root.configure(bg=BG)
        root.geometry("760x760")
        root.bind("<Escape>", lambda e: self._on_close())
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        live = "DRY RUN (no motion)" if self.args.dry_run else "LIVE - motors will move"
        tk.Label(root, text=live, bg=BG, font=("Consolas", 13, "bold"),
                 fg=(WARN if self.args.dry_run else BAD)).pack(pady=(10, 0))

        # top status row: gamepad + armed
        top = tk.Frame(root, bg=BG); top.pack(pady=(6, 2))
        self.lbl_pad = tk.Label(top, text="gamepad: ?", bg=BG, fg=DIM,
                                font=("Consolas", 11, "bold"))
        self.lbl_pad.grid(row=0, column=0, padx=12)
        self.lbl_arm = tk.Label(top, text="DISARMED", bg=BG, fg=WARN,
                                font=("Consolas", 11, "bold"))
        self.lbl_arm.grid(row=0, column=1, padx=12)
        self.lbl_gear = tk.Label(top, text="gear --", bg=BG, fg=ACCENT,
                                 font=("Consolas", 11, "bold"))
        self.lbl_gear.grid(row=0, column=2, padx=12)

        # mode selector
        mode = tk.Frame(root, bg=BG); mode.pack(pady=(4, 6))
        tk.Label(mode, text="Control mode:", bg=BG, fg=FG,
                 font=("Consolas", 11)).grid(row=0, column=0, padx=(0, 8))
        for i, (val, txt) in enumerate((("joy", "Joystick"), ("dpad", "D-pad")), 1):
            tk.Radiobutton(mode, text=txt, value=val, variable=self.mode_var,
                           bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                           activeforeground=ACCENT, takefocus=0,
                           font=("Consolas", 11)).grid(row=0, column=i, padx=4)

        # wheel readouts
        grid = tk.Frame(root, bg=BG); grid.pack(pady=6)
        self.wheel = {}
        self.wheel["L"] = self._wheel_panel(grid, 0, "LEFT (ID%d)" % self.args.left_id)
        self.wheel["R"] = self._wheel_panel(grid, 1, "RIGHT (ID%d)" % self.args.right_id)
        self.lbl_bus = tk.Label(root, text="bus --", bg=BG, fg=DIM,
                                font=("Consolas", 10))
        self.lbl_bus.pack()

        self._build_input_monitor(root)
        self._build_config(root)
        self._build_buttons(root)

        self.lbl_status = tk.Label(root, text="", bg=BG, fg=FG, wraplength=720,
                                   font=("Consolas", 11))
        self.lbl_status.pack(pady=(6, 0))
        tk.Label(root, bg=BG, fg=DIM, font=("Consolas", 9), justify="left",
                 text="A: switch mode   B: gear   X: HALT   Y: arm/disarm   "
                      "Start: arm   Back: disarm   Esc: quit").pack(side="bottom",
                                                                    pady=8)

    def _wheel_panel(self, parent, col, name):
        f = tk.Frame(parent, bg=PANEL, padx=22, pady=12)
        f.grid(row=0, column=col, padx=12)
        tk.Label(f, text=name, fg=ACCENT, bg=PANEL,
                 font=("Consolas", 13, "bold")).pack()
        cmd = tk.Label(f, text="0", fg=FG, bg=PANEL, font=("Consolas", 34, "bold"))
        cmd.pack()
        tk.Label(f, text="commanded r/min", fg=DIM, bg=PANEL,
                 font=("Consolas", 9)).pack()
        act = tk.Label(f, text="act --", fg=DIM, bg=PANEL, font=("Consolas", 12))
        act.pack(pady=(4, 0))
        dirn = tk.Label(f, text="STOP", fg=DIM, bg=PANEL, font=("Consolas", 11, "bold"))
        dirn.pack()
        return {"cmd": cmd, "act": act, "dir": dirn}

    def _build_input_monitor(self, root):
        f = tk.Frame(root, bg=BG); f.pack(pady=(4, 2))
        self.cv = tk.Canvas(f, width=520, height=64, bg=BG, highlightthickness=0)
        self.cv.pack()
        c = self.cv
        # left-stick fwd/back bar (vertical-ish shown horizontally) + right-stick turn bar
        c.create_text(8, 12, text="L-stick fwd/back", anchor="w", fill=DIM,
                      font=("Consolas", 8))
        c.create_rectangle(10, 18, 250, 34, outline="#3a3a3a")
        c.create_line(130, 16, 130, 36, fill="#3a3a3a")
        self.bar_v = c.create_rectangle(130, 19, 130, 33, outline="", fill=ACCENT)
        c.create_text(8, 44, text="R-stick turn", anchor="w", fill=DIM,
                      font=("Consolas", 8))
        c.create_rectangle(10, 50, 250, 62, outline="#3a3a3a")
        c.create_line(130, 48, 130, 64, fill="#3a3a3a")
        self.bar_w = c.create_rectangle(130, 51, 130, 61, outline="", fill=ACCENT)
        # button lights
        self.dots = {}
        layout = [("Y", 300, 14), ("X", 300, 40), ("A", 340, 14), ("B", 340, 40),
                  ("UP", 420, 8), ("DOWN", 420, 46), ("LEFT", 400, 27),
                  ("RIGHT", 440, 27)]
        for name, x, y in layout:
            d = c.create_oval(x, y, x + 16, y + 16, fill=PANEL, outline=DIM)
            c.create_text(x + 24, y + 8, text=name, anchor="w", fill=DIM,
                          font=("Consolas", 8))
            self.dots[name] = d

    def _build_config(self, root):
        f = tk.LabelFrame(root, text=" limits / mapping ", bg=BG, fg=DIM,
                          font=("Consolas", 9))
        f.pack(pady=6, padx=12, fill="x")

        def slider(col, label, var, lo, hi, cb=None):
            cell = tk.Frame(f, bg=BG); cell.grid(row=0, column=col, padx=10, pady=4)
            tk.Label(cell, text=label, bg=BG, fg=FG,
                     font=("Consolas", 9)).pack()
            s = tk.Scale(cell, from_=lo, to=hi, orient="horizontal", variable=var,
                         length=150, bg=BG, fg=FG, troughcolor=PANEL, takefocus=0,
                         highlightthickness=0, command=cb)
            s.pack()

        slider(0, "max vel (r/min)", self.max_var, SPEED_FLOOR, 1000,
               lambda v: self.robot.set_max(int(float(v))))
        slider(1, "turn gain (%)", self.turn_var, 0, 100)
        slider(2, "deadzone (%)", self.dz_var, 0, 40)
        slider(3, "D-pad step (%)", self.step_var, 5, 100)

        checks = tk.Frame(f, bg=BG); checks.grid(row=1, column=0, columnspan=4, pady=(2, 6))
        def check(text, var, cb=None):
            tk.Checkbutton(checks, text=text, variable=var, bg=BG, fg=FG,
                           selectcolor=PANEL, activebackground=BG, takefocus=0,
                           activeforeground=ACCENT, font=("Consolas", 9),
                           command=cb).pack(side="left", padx=6)
        check("invert LEFT motor", self.invL_var,
              lambda: self.robot.set_invert("L", self.invL_var.get()))
        check("invert RIGHT motor", self.invR_var,
              lambda: self.robot.set_invert("R", self.invR_var.get()))
        check("flip fwd axis", self.invfwd_var)
        check("flip turn axis", self.invturn_var)

    def _build_buttons(self, root):
        f = tk.Frame(root, bg=BG); f.pack(pady=4)
        self.btn_arm = tk.Button(f, text="ARM", width=10, takefocus=0,
                                 font=("Consolas", 11, "bold"),
                                 command=self._toggle_arm)
        self.btn_arm.grid(row=0, column=0, padx=6)
        tk.Button(f, text="HALT", width=10, takefocus=0, fg=BAD,
                  font=("Consolas", 11, "bold"),
                  command=self._halt).grid(row=0, column=1, padx=6)
        tk.Button(f, text="Quit", width=8, takefocus=0, font=("Consolas", 11),
                  command=self._on_close).grid(row=0, column=2, padx=6)

    # ------------------------------------------------------------- actions ---
    def _toggle_arm(self):
        self.robot.set_armed(not self.robot.snapshot()["armed"])

    def _halt(self):
        self.v_acc = self.w_acc = 0.0
        self.robot.halt()
        self.robot.set_status("HALT")

    def _cycle_gear(self):
        self.gear_idx = (self.gear_idx + 1) % len(GEARS)

    # -------------------------------------------------------- gamepad poll ---
    def poll(self):
        if not self.robot.running:
            return
        try:
            connected = XInput.get_connected()[self.player]
        except Exception:
            connected = False

        if connected:
            self.robot.set_connected(True)
            try:
                state = XInput.get_state(self.player)
                self._handle_input(state)
            except XInput.XInputNotConnectedError:
                self.robot.set_connected(False)
        else:
            self.robot.set_connected(False)
            self.in_btn = {}
            self.prev_btn = {}           # avoid phantom edges on reconnect
            self.in_ly = self.in_rx = 0.0

        self._refresh()
        self.root.after(self.args.ui_ms, self.poll)

    def _handle_input(self, state):
        btn = XInput.get_button_values(state)
        (lx, ly), (rx, ry) = XInput.get_thumb_values(state)
        self.in_ly, self.in_rx, self.in_btn = ly, rx, btn

        # ---- edge-triggered utility buttons ----
        def rose(name):
            return btn.get(name) and not self.prev_btn.get(name)

        if rose("A"):
            self.mode_var.set("dpad" if self.mode_var.get() == "joy" else "joy")
        if rose("B"):
            self._cycle_gear()
        if rose("X"):
            self._halt()
        if rose("Y"):
            self._toggle_arm()
        if rose("START"):
            self.robot.set_armed(True)
        if rose("BACK"):
            self._halt(); self.robot.set_armed(False)

        # ---- effective limits ----
        maxv = self.max_var.get()
        eff_max = max(SPEED_FLOOR, maxv * GEARS[self.gear_idx])
        dz = self.dz_var.get() / 100.0

        if self.mode_var.get() == "joy":
            v_norm = apply_deadzone(ly, dz) * (-1 if self.invfwd_var.get() else 1)
            w_norm = apply_deadzone(rx, dz) * (-1 if self.invturn_var.get() else 1)
            v = v_norm * eff_max
            w = w_norm * (self.turn_var.get() / 100.0) * eff_max
            left, right = arcade_mix(v, w, eff_max)
        else:
            step = self.step_var.get() / 100.0 * maxv
            self._dpad_repeat("UP", btn, "v_acc", +step)
            self._dpad_repeat("DOWN", btn, "v_acc", -step)
            self._dpad_repeat("RIGHT", btn, "w_acc", +step)
            self._dpad_repeat("LEFT", btn, "w_acc", -step)
            self.v_acc = clamp(self.v_acc, -eff_max, eff_max)
            self.w_acc = clamp(self.w_acc, -eff_max, eff_max)
            left, right = arcade_mix(self.v_acc, self.w_acc, eff_max)

        self.robot.set_cmd(left, right)
        self.prev_btn = dict(btn)

    def _dpad_repeat(self, name, btn, field, delta):
        """Step `field` by `delta` on press, then auto-repeat while held."""
        key = "DPAD_" + name
        held = btn.get(key)
        n = self.hold.get(name, 0)
        if held:
            n += 1
            if n == 1 or (n > DPAD_REPEAT_DELAY
                          and (n - DPAD_REPEAT_DELAY) % DPAD_REPEAT_EVERY == 0):
                setattr(self, field, getattr(self, field) + delta)
        else:
            n = 0
        self.hold[name] = n

    # ----------------------------------------------------------- display ----
    def _refresh(self):
        snap = self.robot.snapshot()
        dry = self.args.dry_run

        # gamepad / armed / gear
        if snap["connected"]:
            self.lbl_pad.config(text=f"gamepad {self.player + 1}: connected", fg=GOOD)
        else:
            self.lbl_pad.config(text=f"gamepad {self.player + 1}: NOT connected", fg=BAD)
        if snap["armed"]:
            self.lbl_arm.config(text="ARMED", fg=GOOD)
            self.btn_arm.config(text="DISARM")
        else:
            self.lbl_arm.config(text="DISARMED", fg=WARN)
            self.btn_arm.config(text="ARM")
        self.lbl_gear.config(text=f"gear {int(GEARS[self.gear_idx] * 100)}%")

        # wheels
        for side in ("L", "R"):
            c = snap["cmd"][side]
            w = self.wheel[side]
            w["cmd"].config(text=f"{c:+d}", fg=(FG if snap["armed"] else DIM))
            w["act"].config(text=("act --" if dry else f"act {snap['actual'][side]:+d}"))
            if abs(c) < SPEED_FLOOR:
                w["dir"].config(text="STOP", fg=DIM)
            else:
                w["dir"].config(text=("FWD" if c > 0 else "REV"), fg=ACCENT)
        if not dry:
            self.lbl_bus.config(text=f"bus  L {snap['bus']['L']:.1f}V   "
                                     f"R {snap['bus']['R']:.1f}V")

        # input monitor bars + lights
        self.cv.coords(self.bar_v, 130, 19, 130 + self.in_ly * 118, 33)
        self.cv.coords(self.bar_w, 130, 51, 130 + self.in_rx * 118, 61)
        names = {"A": "A", "B": "B", "X": "X", "Y": "Y", "UP": "DPAD_UP",
                 "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT"}
        for dot, key in names.items():
            on = self.in_btn.get(key)
            self.cv.itemconfig(self.dots[dot], fill=(ACCENT if on else PANEL))

        alarmed = snap["alarm"]["L"] or snap["alarm"]["R"]
        self.lbl_status.config(text=snap["status"], fg=(BAD if alarmed else FG))

    def _on_close(self):
        self.robot.shutdown()
        self.root.destroy()


def main():
    p = argparse.ArgumentParser(description="F710 gamepad teleop for the EV robot.")
    p.add_argument("--port", default="COM3", help="serial port (default COM3)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--player", type=int, default=0, help="XInput slot 0-3")
    p.add_argument("--left-id", type=int, default=1)
    p.add_argument("--right-id", type=int, default=2)
    p.add_argument("--max", type=int, default=300, help="default max r/min")
    p.add_argument("--turn-gain", type=int, default=60, help="%% of max used for turning")
    p.add_argument("--deadzone", type=int, default=12, help="joystick deadzone %%")
    p.add_argument("--dpad-step", type=int, default=20, help="D-pad step, %% of max/press")
    p.add_argument("--gear", type=int, default=3, help="initial gear index 0-3 (25/50/75/100%%)")
    p.add_argument("--torque-cap", type=int, default=500, help="1=0.1%% (default 50%%)")
    p.add_argument("--accel", type=int, default=3, help="1=0.1s")
    p.add_argument("--decel", type=int, default=3, help="1=0.1s")
    p.add_argument("--bus-floor", type=float, default=24.0)
    p.add_argument("--poll-every", type=int, default=10,
                   help="telemetry read every N control ticks (~50ms each)")
    p.add_argument("--invert-left", action="store_true",
                   help="spin LEFT wheels CCW for chassis-forward")
    p.add_argument("--no-invert-right", action="store_true",
                   help="do NOT reverse the right wheels (default: ID2 reversed)")
    p.add_argument("--ui-ms", type=int, default=25, help="gamepad/UI poll period ms")
    p.add_argument("--timeout", type=float, default=0.3)
    p.add_argument("--dry-run", action="store_true", help="UI + gamepad only, no serial")
    args = p.parse_args()
    args.gear = clamp(args.gear, 0, len(GEARS) - 1)

    robot = Robot(args)
    robot.worker.start()
    root = tk.Tk()
    app = App(root, robot, args)
    app.poll()
    try:
        root.mainloop()
    finally:
        robot.shutdown()


if __name__ == "__main__":
    main()
