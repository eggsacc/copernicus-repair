"""
Real-time visualizer for a Logitech F710 controller in XInput mode (Windows).

Reads the controller through the Windows XInput API (guaranteed mapping for an
XInput device) and draws live stick positions, analog triggers, and button
states with tkinter.

Run:
    pip install XInput-Python
    python f710_visualizer.py

Press Esc (or close the window) to quit.
"""

import sys
import tkinter as tk

try:
    import XInput
except ImportError:
    sys.exit(
        "This script needs the 'XInput-Python' package.\n"
        "Install it with:\n    pip install XInput-Python"
    )

PLAYER = 0      # controller slot 0-3 (use the first connected controller)
POLL_MS = 16    # update interval in ms (~60 Hz)

# --- colors -----------------------------------------------------------------
BG      = "#1e1e1e"
PANEL   = "#2d2d2d"
OUTLINE = "#555555"
TEXT    = "#dcdcdc"
DIM     = "#888888"
ON      = "#4ec9b0"   # generic "pressed" / active accent
DOT     = "#ff6b6b"   # stick dot
DOT_L3  = "#ffd166"   # stick dot while clicked (L3/R3)
BAR_BG  = "#333333"

# classic face-button colors (outline + fill-when-pressed)
FACE = {"A": "#6dbf67", "B": "#e06c5e", "X": "#5b9bd5", "Y": "#e6c14b"}


class ControllerViz:
    def __init__(self, root):
        self.root = root
        root.title("Logitech F710 - XInput Monitor")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.bind("<Escape>", lambda e: root.destroy())

        self.canvas = tk.Canvas(root, width=780, height=600, bg=BG,
                                highlightthickness=0)
        self.canvas.pack()
        c = self.canvas

        c.create_text(20, 18, text="Logitech F710  —  XInput Monitor",
                      anchor="w", fill=TEXT, font=("Segoe UI", 16, "bold"))
        self.status = c.create_text(20, 48, text="Searching for controller…",
                                    anchor="w", fill=DIM, font=("Segoe UI", 11))

        # buttons keyed by XInput name -> (canvas_item_id, on_color)
        self.btn = {}

        # analog sticks
        self.left_stick = self._make_stick(160, 235, 100, "Left Stick")
        self.right_stick = self._make_stick(430, 235, 100, "Right Stick")

        # shoulder bumpers (above each stick)
        self._make_pill("LEFT_SHOULDER", 110, 95, 210, 122, "LB")
        self._make_pill("RIGHT_SHOULDER", 380, 95, 480, 122, "RB")

        # analog triggers (horizontal fill bars)
        self.lt = self._make_bar(610, 120, 150, 26, "LT")
        self.rt = self._make_bar(610, 165, 150, 26, "RT")

        # face buttons (diamond)
        self._make_circle_button("Y", 660, 290, 24, "Y", FACE["Y"])
        self._make_circle_button("X", 620, 330, 24, "X", FACE["X"])
        self._make_circle_button("B", 700, 330, 24, "B", FACE["B"])
        self._make_circle_button("A", 660, 370, 24, "A", FACE["A"])

        # back / start
        self._make_pill("BACK", 575, 470, 645, 498, "BACK")
        self._make_pill("START", 655, 470, 725, 498, "START")

        # d-pad (plus)
        c.create_text(180, 425, text="D-PAD", fill=DIM, font=("Segoe UI", 10, "bold"))
        self._make_square("DPAD_UP",    165, 445, 195, 475)
        self._make_square("DPAD_LEFT",  135, 475, 165, 505)
        self._make_square("DPAD_RIGHT", 195, 475, 225, 505)
        self._make_square("DPAD_DOWN",  165, 505, 195, 535)

        c.create_text(20, 575,
                      text="Stick ring glows on L3/R3 click  ·  triggers are analog  "
                           "·  no deadzone applied  ·  Esc to quit",
                      anchor="w", fill=DIM, font=("Segoe UI", 9))

    # --- builders -----------------------------------------------------------
    def _make_stick(self, cx, cy, R, label):
        c = self.canvas
        ring = c.create_oval(cx - R, cy - R, cx + R, cy + R, outline=OUTLINE, width=3)
        c.create_line(cx - R, cy, cx + R, cy, fill="#3a3a3a")
        c.create_line(cx, cy - R, cx, cy + R, fill="#3a3a3a")
        r = 14
        dot = c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=DOT, outline="")
        val = c.create_text(cx, cy + R + 24, text=f"{label}\n( +0.00, +0.00 )",
                            fill=TEXT, font=("Consolas", 11), justify="center")
        return {"cx": cx, "cy": cy, "R": R, "r": r,
                "ring": ring, "dot": dot, "val": val, "label": label}

    def _make_bar(self, x0, y, w, h, label):
        c = self.canvas
        c.create_text(x0 - 12, y + h / 2, text=label, anchor="e",
                      fill=TEXT, font=("Segoe UI", 11, "bold"))
        c.create_rectangle(x0, y, x0 + w, y + h, outline=OUTLINE, fill=BAR_BG)
        fill = c.create_rectangle(x0, y, x0, y + h, outline="", fill=ON)
        pct = c.create_text(x0 + w / 2, y + h / 2, text="0%",
                            fill=TEXT, font=("Consolas", 10))
        return {"x0": x0, "y": y, "w": w, "h": h, "fill": fill, "pct": pct}

    def _make_circle_button(self, name, cx, cy, r, label, color):
        c = self.canvas
        oid = c.create_oval(cx - r, cy - r, cx + r, cy + r,
                            outline=color, width=3, fill=PANEL)
        c.create_text(cx, cy, text=label, fill=TEXT, font=("Segoe UI", 13, "bold"))
        self.btn[name] = (oid, color)

    def _make_pill(self, name, x0, y0, x1, y1, label):
        c = self.canvas
        rid = c.create_rectangle(x0, y0, x1, y1, outline=OUTLINE, width=2, fill=PANEL)
        c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text=label,
                      fill=TEXT, font=("Segoe UI", 10, "bold"))
        self.btn[name] = (rid, ON)

    def _make_square(self, name, x0, y0, x1, y1):
        rid = self.canvas.create_rectangle(x0, y0, x1, y1,
                                           outline=OUTLINE, width=2, fill=PANEL)
        self.btn[name] = (rid, ON)

    # --- updates ------------------------------------------------------------
    def _update_stick(self, s, x, y, pressed):
        cx, cy, R, r = s["cx"], s["cy"], s["R"], s["r"]
        px = cx + x * R
        py = cy - y * R   # invert Y so pushing up moves the dot up
        self.canvas.coords(s["dot"], px - r, py - r, px + r, py + r)
        self.canvas.itemconfig(s["dot"], fill=DOT_L3 if pressed else DOT)
        self.canvas.itemconfig(s["ring"], outline=ON if pressed else OUTLINE)
        self.canvas.itemconfig(s["val"], text=f'{s["label"]}\n( {x:+.2f}, {y:+.2f} )')

    def _update_bar(self, b, v):
        v = max(0.0, min(1.0, v))
        self.canvas.coords(b["fill"], b["x0"], b["y"],
                           b["x0"] + v * b["w"], b["y"] + b["h"])
        self.canvas.itemconfig(b["pct"], text=f"{int(round(v * 100))}%")

    def _set_buttons(self, buttons):
        for name, (item_id, on_color) in self.btn.items():
            self.canvas.itemconfig(item_id,
                                   fill=on_color if buttons.get(name) else PANEL)

    def _show_connected(self, state):
        self.canvas.itemconfig(self.status,
                               text=f"Controller {PLAYER + 1}: Connected", fill=ON)
        buttons = XInput.get_button_values(state)
        lt, rt = XInput.get_trigger_values(state)
        (lx, ly), (rx, ry) = XInput.get_thumb_values(state)

        self._update_stick(self.left_stick, lx, ly, buttons.get("LEFT_THUMB"))
        self._update_stick(self.right_stick, rx, ry, buttons.get("RIGHT_THUMB"))
        self._update_bar(self.lt, lt)
        self._update_bar(self.rt, rt)
        self._set_buttons(buttons)

    def _show_disconnected(self):
        self.canvas.itemconfig(
            self.status,
            text=f"Controller {PLAYER + 1}: Not connected — turn it on (press a "
                 "button) and check the dongle.",
            fill=DIM)
        self._update_stick(self.left_stick, 0, 0, False)
        self._update_stick(self.right_stick, 0, 0, False)
        self._update_bar(self.lt, 0)
        self._update_bar(self.rt, 0)
        self._set_buttons({})

    # --- main loop ----------------------------------------------------------
    def poll(self):
        try:
            connected = XInput.get_connected()
            if PLAYER < len(connected) and connected[PLAYER]:
                self._show_connected(XInput.get_state(PLAYER))
            else:
                self._show_disconnected()
        except XInput.XInputNotConnectedError:
            self._show_disconnected()
        self.root.after(POLL_MS, self.poll)


def main():
    # Disable deadzones so the display reflects the true raw stick/trigger signal.
    for dz in ("DEADZONE_LEFT_THUMB", "DEADZONE_RIGHT_THUMB", "DEADZONE_TRIGGER"):
        try:
            XInput.set_deadzone(getattr(XInput, dz), 0)
        except Exception:
            pass

    root = tk.Tk()
    app = ControllerViz(root)
    app.poll()
    root.mainloop()


if __name__ == "__main__":
    main()
