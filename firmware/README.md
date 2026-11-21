# Firmware

Owner: Hardware/ML.

This directory will hold the ESP32 firmware for the four-motor rover.

Planned responsibilities:

- Receive schema 4 motor datagrams from `docs/contracts.md`. Identity is the authenticated controller, the boot nonce, and the sender session. A public boot nonce, a source address, or Wi-Fi association is not enough.
- Apply `linear_mps` and `angular_rad_s` only when the datagram is accepted, fresh, and still inside its monotonic deadline.
- Enforce the session rule, the sequence rule, the freshness challenge, the rearm generation, the velocity limits, the task states, and the stop flag.
- Apply an older safety stop from the active session even when a newer motion datagram arrived first. That older stop must not refresh the watchdog or clear a latch.
- Keep the physical emergency stop independent of the software estop latch and the watchdog fault latch. An `idle` datagram clears none of them. `rearm` clears only the two software latches, and only when it matches the generation published after the physical control was released.
- Stop the motors when the monotonic watchdog goes more than 250 ms without an accepted datagram, and when the active command's deadline arrives. That deadline is the earlier of the command lifetime and 250 ms after the authorizing challenge was issued.
- Hold motor power or the driver enable through a cutoff that opens if the firmware stops petting it. That electrical path is still unverified on a bench.
- Map body-frame linear and angular motion to the four motors. That wheel mapping is firmware-owned and is not part of the motor-command schema.

The ESP32 does not run MediaPipe or the gesture classifier. Limits in firmware may be stricter than the contract limits. They must not be looser.

No firmware, pin map, or motor driver is included in this revision. A later change needs a safety review under `CONTRIBUTING.md` before it can command motion.
