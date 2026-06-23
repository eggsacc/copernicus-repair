# EV Controller Teleop — Session Notes & TODO

File of interest: `ev_controller_teleop.py` (F710 gamepad teleop, differential drive,
SPEED mode, tkinter UI + serial worker thread).

---

## Done this session (2026-06-23)

### 1. Fixed: UI froze / controller showed "?" when run without `--dry-run`
- **Symptom:** With `--dry-run` the controller worked; without flags the UI showed no
  updates on button presses and the gamepad label was stuck at `?`.
- **Root cause:** When the serial port failed to open, the worker set
  `self.running = False`. The UI poll loop's first line is `if not self.robot.running: return`,
  so `poll()` bailed before ever reading the gamepad or calling `_refresh()` — and never
  rescheduled itself. The `?` is the *initial* label text (line ~420) that `_refresh()`
  normally overwrites; since `_refresh()` never ran, it stayed frozen.
- **Fix applied:** In `_worker()`'s serial-open `except serial.SerialException`, removed
  `self.running = False` and just `return`. Now the worker exits cleanly but the UI keeps
  polling and can display the real error (`serial open FAILED: ...`) in the status label.

### 2. Diagnosed: `/dev/ttyUSB0` couldn't be opened — `brltty` hijacking the adapter
- **Cause:** `dmesg` showed the CH341 USB-serial chip (idVendor `1a86`, idProduct `7523`)
  attach as `ttyUSB0`, then `brltty` (braille-display daemon) claimed the interface and
  immediately disconnected it. brltty grabs CH340/CH341 chips by default.
- **Fix (worked):** Remove/disable brltty:
  ```bash
  sudo apt-get remove --purge brltty
  # or, if you must keep it:
  sudo systemctl mask brltty.service brltty.path
  sudo rm -f /usr/lib/udev/rules.d/85-brltty.rules
  sudo udevadm control --reload-rules
  ```
  Also ensure user is in `dialout` (`sudo usermod -aG dialout $USER`, then re-login).

---

## TODO next session: input→motor latency (esp. one side)

### Why it happens
One single-threaded serial worker does every Modbus op as a blocking request→response on
one shared RS-485 bus (loop: `_worker()` ~lines 283–295). Command→motion latency = all bus
traffic queued ahead + the 50 ms `time.sleep(0.05)`. Two main contributors:

1. **Periodic telemetry reads** (`_read_telemetry`, ~lines 327–344): every `poll_every`
   (default **10**) ticks it does two slow 8-register reads (L then R), the "~300 ms reads"
   the docstring warns about. No commands go out while they run.
2. **0.3 s timeout** (`--timeout`, ~line 712): any unanswered transaction blocks the whole
   loop up to 300 ms.

**Why one side worse:** ordering is fixed L-before-R for both writes and reads, so a slow/
timing-out drive (or worse RS-485 link / different drive response-delay or accel params on
that node) makes *that* side consistently sit behind more bus traffic.

### Confirm which cause (do first)
- [ ] Add per-side transaction timing logs to `_send_side` / `_read_telemetry` to see which
      ID is slow or timing out.
- [ ] Temporarily run `--poll-every 1000` (disables telemetry reads). If latency drops &
      becomes symmetric → telemetry reads were the bottleneck.
- [ ] Swap drive IDs/cables. Lag follows the physical drive → drive/wiring; stays on same
      ID/bus position → bus/ordering.

### Fixes (most impactful first)
- [ ] **Decouple telemetry from the command path** — read fewer registers, much less often
      (e.g. `--poll-every 40`), or move telemetry to its own slower cadence so commands stay
      tight.
- [ ] **Lower `--timeout`** (e.g. `0.1`) so a non-responding node stalls 100 ms not 300 ms
      (verify healthy responses are well under that at 115200).
- [ ] **Add a write deadband** in `_send_side` (~lines 318–319): only re-send `REG_SPEED0`
      when magnitude changes by > ~5 r/min. Analog stick currently rewrites speed almost
      every tick → lots of bus traffic.
- [ ] **Tighten the loop**: drop `time.sleep(0.05)` toward `0.02` once the bus has headroom.

### Possible follow-up (robustness)
- [ ] Consider decoupling the UI `poll()` from `robot.running` entirely (key off window
      close instead) so a stopped worker can never freeze the UI.
