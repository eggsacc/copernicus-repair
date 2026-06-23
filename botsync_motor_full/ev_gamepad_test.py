#!/usr/bin/env python3
"""
EV gamepad input monitor (headless)
===================================

Prints live F710 axis / button / hat values to the terminal so you can confirm
the gamepad is being received — no display, no serial, no motors. Uses pygame
(the same backend as ev_controller_teleop.py) and reuses that script's
AXIS_*/BTN_*/HAT_DPAD mapping, so it also verifies those constants match your
unit.

    python3 ev_gamepad_test.py            # first detected controller
    python3 ev_gamepad_test.py --player 1 # second controller

Set the F710 rear switch to 'X' (XInput). Move the sticks, press buttons, work
the D-pad; the line refreshes in place. Ctrl-C to quit. If nothing changes but
a controller is listed, the pad is detected but not sending — check batteries
and that the receiver/switch are on 'X'.
"""

import argparse
import os
import time

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:
    raise SystemExit("Needs pygame:  sudo apt install -y python3-pygame")

# Same mapping as ev_controller_teleop.py (XInput / in-kernel xpad layout).
AXIS_LX, AXIS_LY = 0, 1
AXIS_RX, AXIS_RY = 3, 4
BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
BTN_BACK, BTN_START = 6, 7
HAT_DPAD = 0


def main():
    p = argparse.ArgumentParser(description="Live F710 gamepad input monitor.")
    p.add_argument("--player", type=int, default=0, help="controller index (0=first)")
    p.add_argument("--hz", type=float, default=20.0, help="refresh rate")
    args = p.parse_args()

    pygame.init()
    pygame.joystick.init()

    joy = None
    period = 1.0 / max(1.0, args.hz)
    print("Monitoring gamepad — move sticks / press buttons. Ctrl-C to quit.\n")
    try:
        while True:
            pygame.event.get()  # pump SDL; refreshes hot-plug + latest values

            if args.player >= pygame.joystick.get_count():
                print("\rno controller at index %d — waiting...        "
                      % args.player, end="", flush=True)
                joy = None
                time.sleep(0.5)
                continue
            if joy is None:
                joy = pygame.joystick.Joystick(args.player)
                joy.init()
                print("connected: %r  (axes=%d buttons=%d hats=%d)\n"
                      % (joy.get_name(), joy.get_numaxes(),
                         joy.get_numbuttons(), joy.get_numhats()))

            nb = joy.get_numbuttons()

            def b(i):
                return 1 if (i < nb and joy.get_button(i)) else 0

            ly = -joy.get_axis(AXIS_LY)   # flip so up = +1 (matches teleop)
            rx = joy.get_axis(AXIS_RX)
            hx, hy = joy.get_hat(HAT_DPAD) if joy.get_numhats() > HAT_DPAD else (0, 0)

            held = [n for n, i in (("A", BTN_A), ("B", BTN_B), ("X", BTN_X),
                                   ("Y", BTN_Y), ("START", BTN_START),
                                   ("BACK", BTN_BACK)) if b(i)]
            line = ("L-fwd %+0.2f  R-turn %+0.2f  dpad(%+d,%+d)  btn[%s]"
                    % (ly, rx, hx, hy, ",".join(held)))
            print("\r%-78s" % line, end="", flush=True)
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
