# Raspberry Pi Setup — botsync motor control

Setup commands for running the EV Drive control scripts on a Raspberry Pi
(Raspberry Pi OS / Debian-based). Run these in a terminal on the Pi.

The gamepad script has been ported from the Windows-only `XInput` library to
**pygame**, so all scripts now run on the Pi (and still on Windows).

---

## 1. Install system packages (apt)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-serial python3-tk python3-pygame
```

What each provides:

| Package          | Why it's needed                                            |
|------------------|------------------------------------------------------------|
| `python3`        | the interpreter                                            |
| `python3-pip`    | pip, in case you install anything via pip later            |
| `python3-serial` | `pyserial` — used by ALL scripts (Modbus over RS485)       |
| `python3-tk`     | `tkinter` — GUI for ev_teleop.py & ev_controller_teleop.py |
| `python3-pygame` | gamepad input for ev_controller_teleop.py (F710)           |

Standard-library modules (argparse, os, threading, time, math, sys, struct)
need no install.

> Raspberry Pi OS Bookworm ships pygame 2 (SDL2), which is what you want. If
> `apt` gives you an older pygame, you can instead `pip install pygame` for 2.x.

---

## 2. Serial port access (all scripts)

The scripts talk to the drives through a USB-RS485 adapter (e.g. /dev/ttyUSB0).
Add your user to the `dialout` group so you can open the port without sudo:

```bash
sudo usermod -aG dialout $USER
```

Find the adapter's device name:

```bash
ls /dev/ttyUSB*           # or:  dmesg | grep tty   (right after plugging it in)
```

Pass it with `--port`, e.g. `--port /dev/ttyUSB0`. (The controller script now
defaults to `/dev/ttyUSB0`; the others require `--port` explicitly.)

---

## 3. Gamepad access (ev_controller_teleop.py only)

1. **Set the F710's rear switch to `X`** (XInput mode). Linux's in-kernel
   `xpad` driver then exposes it as a standard Xbox 360 pad — no extra driver
   needed.

2. **Group permission** — reading the controller needs access to /dev/input/*,
   which is the `input` group:

   ```bash
   sudo usermod -aG input $USER
   ```

   (On stock Raspberry Pi OS the default user is usually already in `input`.)

3. **Plug in the receiver and confirm it's detected:**

   ```bash
   ls /dev/input/js*                 # should list e.g. /dev/input/js0
   # optional, more detail:
   sudo apt install -y joystick && jstest /dev/input/js0
   ```

> **Log out and back in (or reboot)** after any `usermod` so the new group
> membership takes effect.

If a stick axis or the D-pad behaves wrong on your unit, tweak the
`AXIS_* / BTN_* / HAT_DPAD` index constants near the top of
`ev_controller_teleop.py` (the Gamepad backend section).

---

## 4. The GUI scripts need a display

`ev_teleop.py` and `ev_controller_teleop.py` use tkinter, so they need the Pi
desktop, or X forwarding over SSH (`ssh -X pi@<address>`). (The gamepad half of
the controller script does NOT need a display — only its tkinter window does.)

The headless diagnostic scripts run fine over plain SSH (no display):
`ev_modbus_test.py`, `ev_bus_scan.py`, `ev_ascii_scan.py`, `ev_format_scan.py`,
`ev_speed_test.py`, `ev_jg_test.py`, `ev_drive_profile.py`.

---

## 5. Quick test once set up

```bash
# Headless comms/status check on drive IDs 1 and 2:
python3 ev_modbus_test.py --port /dev/ttyUSB0 --baud 115200 diag

# Scan the RS485 bus if the drives are silent:
python3 ev_bus_scan.py --port /dev/ttyUSB0

# Gamepad teleop UI without touching the motors (verify the pad reads):
python3 ev_controller_teleop.py --dry-run
```
