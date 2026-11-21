# Tests

This directory holds the schema 3 checks for `docs/contracts.md`. Application areas will keep their own unit tests beside their code when that code is added.

`control_auth.py` contains the HMAC-SHA256 canonical encoding and a fixed test key. That key is a fixture for these tests. It is not a credential and it must not be provisioned onto an ESP32. Passing these tests does not verify firmware, radio timing, or the electrical cutoff.

Run the checks from the repository root:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m pytest tests
```

`contract_schema.py` checks message shape. `motor_receiver.py` is a mocked receiver for datagram ordering, session identity, watchdog expiration, and emergency-stop rules. The inputs are constructed in the test files. They are not recordings, model outputs, or logs from a rover.

A passing run means the mock matches the written contract. It does not mean the ESP32, the motors, or the physical emergency-stop control have been tested.
