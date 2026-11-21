# Backend

Owner: Bailey.

This directory will hold the FastAPI service and the TigerData schema for profiles and sessions.

Planned responsibilities:

- Store participant profiles and session metadata.
- Record feature, gesture, and motor-command messages for later review, using the timestamps and schema versions in `docs/contracts.md`.
- Keep database URLs, Vultr credentials, and other secrets in the environment. They are not committed.
- Serve local development before any Vultr deployment exists.

TigerData is the PostgreSQL database for this service. The firmware watchdog remains the real-time stop path. A slow or unavailable database must not be required for the ESP32 to stop the motors.

No API implementation, schema migration, or deployment file is included in this revision.
