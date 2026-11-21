# Tests

This directory holds the schema 2 checks for `docs/contracts.md`. Application areas will keep their own unit tests beside their code when that code is added.

Run the checks from the repository root:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m pytest tests
```

`contract_schema.py` checks message shape. `motor_receiver.py` is a mocked receiver for datagram ordering, session identity, watchdog expiration, and emergency-stop rules. The inputs are constructed in the test files. They are not recordings, model outputs, or logs from a rover.

A passing run means the mock matches the written contract. It does not mean the ESP32, the motors, or the physical emergency-stop control have been tested.
