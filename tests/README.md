# Tests

This directory holds the schema 4 checks for `docs/contracts.md`. Application areas will keep their own unit tests beside their code when that code is added.

`control_auth.py` contains the HMAC-SHA256 canonical encoding and a fixed test key. That key is a fixture for these tests. It is not a credential and it must not be provisioned onto an ESP32. Passing these tests does not verify firmware, radio timing, or the electrical cutoff.

Run the checks from the repository root:

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests
```

`status_gate.py` is the publisher-side status gate. Its persisted state is the JSON object described under "Persisted status-gate state" in `docs/contracts.md`: durable challenge and status-sequence high-water marks only. A restored gate never resumes an outstanding challenge or a previously accepted status, because a sender restart may reset its monotonic clock. It needs a newly issued challenge and a new authenticated status before it authorizes motion or `rearm`. Missing, corrupted, or inconsistent saved state locks the gate instead of resetting the replay marks. A snapshot older than the marks a gate already holds is rejected and locks the gate. The controller's `session_id` high-water mark is a separate record and is not part of this object. The optional `persist` hook lets the tests check that a challenge mark is written before the challenge is used and a status-sequence mark before the status authorizes. The tests store that state only in memory and as JSON strings. They do not exercise a real file system, atomic writes, a flush, or a laptop reboot.

`contract_schema.py` checks message shape. `motor_receiver.py` is a mocked receiver for datagram ordering, session identity, watchdog expiration, and emergency-stop rules. The inputs are constructed in the test files. They are not recordings, model outputs, or logs from a rover.

A passing run means the mock matches the written contract. It does not mean the ESP32, the motors, or the physical emergency-stop control have been tested.
