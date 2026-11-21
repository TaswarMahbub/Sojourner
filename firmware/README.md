# Firmware

Owner: Hardware/ML.

This directory will hold the ESP32 firmware for the four-motor rover.

Planned responsibilities:

- Receive motor commands of schema `"1"` from `docs/contracts.md`.
- Apply `linear_mps` and `angular_rad_s` only when the message is accepted.
- Enforce the sequence rule, the expiry time, the velocity limits, the task-state rules, and the stop flag.
- Stop the motors when a command is invalid, expired, out of order, or absent for the 250 ms watchdog.
- Keep `estop` and a watchdog fault latched until a newer accepted command has `task_state` `idle` and `stop` true.
- Map body-frame linear and angular motion to the four motors. That wheel mapping is firmware-owned and is not part of the motor-command schema.

The ESP32 does not run MediaPipe or the gesture classifier. Limits in firmware may be stricter than the contract limits. They must not be looser.

No firmware, pin map, or motor driver is included in this revision. A later change needs a safety review under `CONTRIBUTING.md` before it can command motion.
