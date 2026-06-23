# Raspberry Pi Setup — botsync motor control

Setup commands for running the EV Drive control scripts on a Raspberry Pi
(Raspberry Pi OS / Debian-based). Run these in a terminal on the Pi.

---

## 1. Install system packages (apt)

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-serial python3-tk
```

This covers every script EXCEPT the gamepad teleop. What each provides:

| Package          | Why it's needed                                          |
|------------------|----------------------------------------------------------|
| `python3`        | the interpreter                                          |
| `python3-pip`    | pip, in case you install anything via pip later          |
| `python3-serial` | `pyserial` — used by ALL scripts (Modbus over RS485)     |
| `python3-tk`     | `tkinter` — GUI for ev_teleop.py & ev_controller_teleop.py |

Standard-library modules (argparse, threading, time, math, sys, struct) need
no install.

---

## 2. Serial port access

The scripts open a USB-RS485 adapter (e.g. /dev/ttyUSB0). Add your user to the
`dialout` group so you can open the port without sudo:

```bash
sudo usermod -aG dialout $USER
```

**Log out and back in** (or reboot) for this to take effect.

Find your adapter's device name with:

```bash
ls /dev/ttyUSB*       # or:  dmesg | grep tty   (after plugging it in)
```

Then pass it with `--port`, e.g. `--port /dev/ttyUSB0`.

---

## 3. GUI scripts need a display

`ev_teleop.py` and `ev_controller_teleop.py` use tkinter, so they need the Pi
desktop, or X forwarding over SSH (`ssh -X pi@<address>`).

The headless diagnostic scripts run fine over plain SSH (no display needed):
`ev_modbus_test.py`, `ev_bus_scan.py`, `ev_ascii_scan.py`, `ev_format_scan.py`,
`ev_speed_test.py`, `ev_jg_test.py`, `ev_drive_profile.py`.

---

## 4. Gamepad script (ev_controller_teleop.py) — NEEDS A CODE CHANGE

`ev_controller_teleop.py` imports `XInput` (the XInput-Python package), which is
**Windows-only** — it loads a Windows DLL and will NOT work on the Pi, even if
you pip-install it.

On Linux the Logitech F710 appears as a standard input device, so the script
must be rewritten against a Linux input library. Install one of:

```bash
# Option A: evdev (lightweight, works headless)
sudo apt install -y python3-evdev

# Option B: pygame (heavier, also does joystick)
sudo apt install -y python3-pygame
```

...then the `import XInput` block and all `XInput.*` calls need porting to that
library. The other 8 scripts (including the WASD teleop ev_teleop.py) need no
change.

---

## Quick test once set up

```bash
# Headless comms/status check on drive IDs 1 and 2:
python3 ev_modbus_test.py --port /dev/ttyUSB0 --baud 115200 diag

# Scan the RS485 bus if drives are silent:
python3 ev_bus_scan.py --port /dev/ttyUSB0
```
