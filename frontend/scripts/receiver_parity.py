"""Generate the simulator's receiver parity fixture.

Runs seeded random scenarios through the Python reference receiver, tests/motor_receiver.py,
and records every step's outcome and the receiver state after it. The frontend test
src/sim/motorReceiver.parity.test.js replays the same steps through the JavaScript port and
requires identical results.

Run from the repository root:

    python3 frontend/scripts/receiver_parity.py

The fixture is gzipped JSON. The same seed always writes the same file.

The datagrams are synthetic test traffic signed with the labelled test key in
tests/control_auth.py. They are not recordings and the key is not a rover secret.
"""

from __future__ import annotations

import gzip
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.control_auth import CONTROLLER, TEST_KEY, sign_command  # noqa: E402
from tests.motor_receiver import MotorReceiver  # noqa: E402

OUT = ROOT / "frontend" / "src" / "sim" / "__fixtures__" / "receiver-parity.json.gz"
SCENARIOS = 150
STEPS = (40, 160)
UNIX_BASE = 1795276800000
SEED = 4
PAD_BYTES = 8200


def padded(text: str) -> str:
    return text[:-1] + ', "pad": "' + "x" * PAD_BYTES + '"}'


class Sender:
    """A sender that copies the receiver's current challenge, as tests/control_auth.seal does."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.session = rng.randint(1, 50)
        self.sequence = 0
        self.challenge = 0

    def command(self, rx: MotorReceiver, task: str, stop: bool, linear=0, angular=0, *, new_session=False):
        if new_session:
            self.session += self.rng.choice((1, 1, 1, 2, 7))
            self.sequence = 0
        self.sequence += 1
        self.challenge += 1
        issued = UNIX_BASE + rx.monotonic_ms + self.rng.choice((0, 0, 0, -40, 30, 2500))
        lifetime = self.rng.choice((250, 250, 120, 40, 1))
        message = {
            "schema_version": "4",
            "controller_id": CONTROLLER,
            "boot_nonce": rx.boot_nonce,
            "session_id": self.session,
            "sequence": 1 if new_session else self.sequence,
            "freshness_id": rx.freshness_id,
            "rearm_generation": rx.rearm_generation,
            "status_challenge": self.challenge,
            "issued_at_ms": issued,
            "expires_at_ms": issued + lifetime,
            "task_state": task,
            "linear_mps": linear,
            "angular_rad_s": angular,
            "stop": stop,
        }
        return message


def sign(message, key=TEST_KEY):
    message = dict(message)
    message.pop("mac", None)
    try:
        message["mac"] = sign_command(key, message)
    except Exception:
        message["mac"] = "0" * 64
    return message


def mutate(rng: random.Random, rx: MotorReceiver, message: dict) -> str:
    """Return datagram text: usually valid, sometimes one of the hostile cases the contract names."""
    m = dict(message)
    choice = rng.random()
    resign = True
    if choice < 0.65:
        pass
    elif choice < 0.60:
        m["freshness_id"] = max(1, m["freshness_id"] - rng.choice((1, 2)))
    elif choice < 0.64:
        m["rearm_generation"] = m["rearm_generation"] + rng.choice((-1, 1))
    elif choice < 0.68:
        m["sequence"] = max(1, m["sequence"] - rng.choice((1, 2, 5)))
    elif choice < 0.70:
        m["boot_nonce"] = m["boot_nonce"] + 1
    elif choice < 0.72:
        m["session_id"] = max(1, m["session_id"] - 1)
    elif choice < 0.75:
        resign = False
        m = sign(m)
        m["mac"] = ("f" if m["mac"][0] != "f" else "e") + m["mac"][1:]
    elif choice < 0.77:
        m["controller_id"] = "intruder"
    elif choice < 0.79:
        m["extra"] = 1
    elif choice < 0.81:
        m["expires_at_ms"] = m["issued_at_ms"] + rng.choice((0, 251))
    elif choice < 0.83:
        m["linear_mps"] = rng.choice((0.1234567, 0.31, -0.300001, 1e-7))
    elif choice < 0.85:
        signed = sign(m)
        text = json.dumps(signed)
        return text.replace(f'"sequence": {signed["sequence"]}', f'"sequence": {signed["sequence"]}.0')
    elif choice < 0.87:
        signed = sign(m)
        text = json.dumps(signed)
        return text.replace('"linear_mps": 0', '"linear_mps": -0', 1)
    elif choice < 0.88:
        signed = sign(m)
        text = json.dumps(signed)
        return text.replace('"linear_mps": 0', '"linear_mps": -0.0', 1)
    elif choice < 0.90:
        signed = sign(m)
        text = json.dumps(signed)
        return text[:-1] + ', "stop": true}'
    elif choice < 0.91:
        # Too large: the replay appends PAD_BYTES of padding to this text (see padded()).
        return ("PAD", json.dumps(sign(m)))
    elif choice < 0.92:
        return json.dumps({"boot_nonce": rx.boot_nonce, "session_id": m["session_id"], "x": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]})
    elif choice < 0.94:
        return '{"boot_nonce": %d, "session_id": %d, "sequence": ' % (rx.boot_nonce, m["session_id"])
    elif choice < 0.95:
        return rng.choice(("", "null", "[]", "42", '"text"', "{", '{"a": NaN}', '{"a": 1e400}'))
    elif choice < 0.97:
        m["task_state"] = rng.choice(("idle", "hold", "estop", "fault", "rearm", "manual", "shared", "drive"))
    else:
        m["stop"] = not m["stop"]
    if resign:
        m = sign(m)
    return json.dumps(m)


def scenario(rng: random.Random) -> dict:
    initial_nonce = rng.randint(1, 10_000)
    rx = MotorReceiver(initial_nonce)
    sender = Sender(rng)
    steps = []
    previous = {}
    mono = 0

    def record(op: dict, result):
        # Store only the state fields that changed since the previous step, to keep the fixture small.
        snap = rx.snapshot()
        changes = {k: v for k, v in snap.items() if k not in previous or previous[k] != v or type(previous[k]) is not type(v)}
        previous.clear()
        previous.update(snap)
        steps.append({"op": op, "result": result, "changes": changes, "torque": rx.motors_have_torque()})

    def send(message, *, hostile=True):
        out = mutate(rng, rx, message) if hostile else json.dumps(sign(message))
        if isinstance(out, tuple):
            record({"op": "datagram", "raw": out[1], "pad": PAD_BYTES}, rx.handle_raw(padded(out[1])))
        else:
            record({"op": "datagram", "raw": out}, rx.handle_raw(out))

    record({"op": "begin"}, None)
    # Usually bring the receiver to the armed state the way a real sender would, so motion is exercised.
    if rng.random() < 0.8:
        rx.complete_startup()
        record({"op": "startup"}, None)
        rx.set_sensor("released")
        record({"op": "sensor", "state": "released"}, None)
        record({"op": "ack_cutoff"}, rx.acknowledge_cutoff())
        send(sender.command(rx, "idle", True, new_session=True), hostile=False)
        record({"op": "status"}, rx.status())
        send(sender.command(rx, "rearm", True), hostile=False)

    small = (0, 1, 5, 10, 20, 30, 40, 60, 100)
    large = (249, 250, 251, 300)
    for _ in range(rng.randint(*STEPS)):
        r = rng.random()
        if r < 0.25:
            mono += rng.choice(large) if rng.random() < 0.12 else rng.choice(small)
            rx.set_monotonic(mono)
            record({"op": "mono", "ms": mono}, None)
        elif r < 0.265:
            state = rng.choice(("released", "released", "released", "asserted", "unknown"))
            rx.set_sensor(state)
            record({"op": "sensor", "state": state}, None)
        elif r < 0.27:
            rx.complete_startup()
            record({"op": "startup"}, None)
        elif r < 0.29:
            record({"op": "ack_cutoff"}, rx.acknowledge_cutoff())
        elif r < 0.35:
            record({"op": "status"}, rx.status())
        elif r < 0.355:
            rx.lose_motor_power()
            record({"op": "lose_power"}, None)
        elif r < 0.36:
            rx.restore_motor_power()
            record({"op": "restore_power"}, None)
        elif r < 0.367:
            rx.freeze_firmware()
            record({"op": "freeze"}, None)
        elif r < 0.387:
            rx.unfreeze_firmware()
            record({"op": "unfreeze"}, None)
        elif r < 0.407:
            rx.poll()
            record({"op": "poll"}, None)
        elif r < 0.417:
            unix = UNIX_BASE + mono + rng.choice((-500, 0, 100, 3000))
            rx.note_verified_clock(unix)
            record({"op": "sync", "ms": unix}, None)
        elif r < 0.42:
            nonce = rng.randint(1, 10_000)
            rx.brownout_reset(nonce)
            mono = 0
            record({"op": "brownout", "nonce": nonce}, None)
        else:
            kind = rng.random()
            if kind < 0.08 or (rx.active_session is None and kind < 0.6):
                message = sender.command(rx, "idle", True, new_session=True)
            elif kind < 0.2 or (not rx.armed and kind < 0.35):
                message = sender.command(rx, "rearm", True)
            elif kind < 0.75:
                linear = rng.choice((0, 0.1, 0.25, 0.3, -0.2, 0.05))
                angular = rng.choice((0, 0.5, -1, 1, -0.25))
                message = sender.command(rx, rng.choice(("manual", "shared")), False, linear, angular)
            else:
                message = sender.command(rx, rng.choice(("idle", "hold", "estop", "fault")), True)
            send(message)
    return {"boot_nonce": initial_nonce, "steps": steps}


def main() -> None:
    rng = random.Random(SEED)
    scenarios = []
    for _ in range(SCENARIOS):
        scenarios.append(scenario(rng))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "about": "Generated by frontend/scripts/receiver_parity.py from tests/motor_receiver.py. Synthetic test traffic, not recordings.",
        "controller_id": CONTROLLER,
        "key": TEST_KEY.decode("ascii"),
        "scenarios": scenarios,
    }
    # mtime=0 keeps the file identical between runs with the same seed.
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with open(OUT, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(data)
    steps = sum(len(s["steps"]) for s in scenarios)
    outcomes = {}
    for s in scenarios:
        for step in s["steps"]:
            if step["op"]["op"] == "datagram":
                outcomes[step["result"]] = outcomes.get(step["result"], 0) + 1
    print(f"wrote {OUT.relative_to(ROOT)}: {len(scenarios)} scenarios, {steps} steps")
    print("datagram outcomes:", dict(sorted(outcomes.items())))


if __name__ == "__main__":
    main()
