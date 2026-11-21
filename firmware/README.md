# Firmware

Owner: Hardware/ML.

This directory will hold the ESP32 firmware for the four-motor rover.

Planned responsibilities:

- Receive schema 2 motor datagrams from `docs/contracts.md`. Identity is the boot nonce plus the sender session, not a UDP connection.
- Apply `linear_mps` and `angular_rad_s` only when the datagram is accepted.
- Enforce the session rule, the sequence rule, the velocity limits, the task states, and the stop flag.
- Apply an older safety stop from the active session even when a newer motion datagram arrived first. That older stop must not refresh the watchdog or clear a latch.
- Keep the physical emergency stop independent of the software estop latch and the watchdog fault latch. An `idle` datagram clears none of them. `rearm` clears only the two software latches, and only after the physical control is released.
- Stop the motors when the watchdog goes more than 250 ms without an accepted datagram.
- Map body-frame linear and angular motion to the four motors. That wheel mapping is firmware-owned and is not part of the motor-command schema.

The ESP32 does not run MediaPipe or the gesture classifier. Limits in firmware may be stricter than the contract limits. They must not be looser.

No firmware, pin map, or motor driver is included in this revision. A later change needs a safety review under `CONTRIBUTING.md` before it can command motion.
