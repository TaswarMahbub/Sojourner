"""Test rig: control/ publisher wired to the MotorReceiver oracle in-process.

Used by tests/test_control.py and tests/run_control_e2e.py. The rig owns the
test key, which comes from tests/control_auth.py and is passed to control/
through the environment, the same way a provisioned key would be. Nothing in
control/ imports this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from control.auth import load_credentials
from control.publisher import Publisher
from control.reset import GATE_FILE, SESSION_FILE, operator_reset
from control.session import SessionAllocator
from control.status_gate import StatusGate
from tests.contract_schema import SCHEMA
from tests.control_auth import CONTROLLER, TEST_KEY
from tests.motor_receiver import MotorReceiver

UNIX_EPOCH_MS = 1795276800000
TEST_ENV = {"SOJOURNER_CONTROL_KEY": TEST_KEY.hex(), "SOJOURNER_CONTROLLER_ID": CONTROLLER}


class FakeClock:
    """Sender clocks. Monotonic and Unix advance together unless a test splits them."""

    def __init__(self, mono: int = 1000, unix: int = UNIX_EPOCH_MS):
        self.mono = mono
        self.unix = unix

    def mono_ms(self) -> int:
        return self.mono

    def unix_ms(self) -> int:
        return self.unix

    def advance(self, ms: int) -> None:
        self.mono += ms
        self.unix += ms


def provision(state_dir) -> None:
    """First provisioning goes through the same operator reset path."""
    operator_reset(state_dir, CONTROLLER, operator="test-rig", reason="initial provisioning")


def build_publisher(state_dir, send, clock, environ=None) -> Publisher:
    credentials = load_credentials(TEST_ENV if environ is None else environ)
    gate = StatusGate(credentials.key, credentials.controller_id, state_dir / GATE_FILE)
    gate.restore_from_file()
    allocator = SessionAllocator(credentials.controller_id, state_dir / SESSION_FILE)
    allocator.restore_from_file()
    return Publisher(credentials, gate, allocator, send, clock)


TRACKING_FIXTURE = Path(__file__).resolve().parent.parent / "perception" / "fixtures" / "frame_tracking.json"


def tracking_frame(timestamp_ms: int) -> dict:
    """The committed actionable perception fixture, re-stamped."""
    frame = json.loads(TRACKING_FIXTURE.read_text(encoding="utf-8"))
    frame["timestamp_ms"] = timestamp_ms
    return frame


def lost_frame(timestamp_ms: int) -> dict:
    return {
        "schema_version": SCHEMA,
        "timestamp_ms": timestamp_ms,
        "tracking_state": "lost",
        "features": {},
        "head_pose": None,
        "confidence": None,
    }


def gesture(end_ms: int, label: str = "neutral", start_ms: int | None = None, frame_count: int = 1) -> dict:
    """A gesture message. Section 2: a one-frame window has start == end and
    window_frame_count 1, so a wider window must say how many frames it used."""
    start = end_ms if start_ms is None else start_ms
    return {
        "schema_version": SCHEMA,
        "timestamp_ms": end_ms,
        "source_timestamp_ms": end_ms,
        "window_start_timestamp_ms": start,
        "window_end_timestamp_ms": end_ms,
        "window_frame_count": frame_count,
        "label": label,
        "confidence": 0.5,
        "model_version": "none",
    }


class Rig:
    """Publisher and receiver sharing one simulated millisecond timeline."""

    def __init__(self, state_dir, *, boot_nonce: int = 7, provisioned: bool = True, verbose: bool = False):
        self.state_dir = state_dir
        if provisioned:
            provision(state_dir)
        self.clock = FakeClock()
        self.receiver = MotorReceiver(boot_nonce)
        self.receiver.set_monotonic(self.clock.mono)
        self.link_up = True
        self.sent: list[dict] = []
        self.results: list[str] = []
        self.verbose = verbose
        self.publisher = build_publisher(state_dir, self._deliver, self.clock)

    def _deliver(self, payload: bytes) -> None:
        message = json.loads(payload)
        self.sent.append(message)
        if not self.link_up:
            self.results.append("lost-in-transit")
        else:
            self.results.append(self.receiver.handle_raw(payload))
        if self.verbose:
            print(describe(message, self.results[-1]))

    def ready_receiver(self) -> None:
        """Startup finished, operator releases the physical stop, cutoff restored."""
        self.receiver.complete_startup()
        self.receiver.set_physical_estop(False)
        assert self.receiver.acknowledge_cutoff()

    def advance(self, ms: int) -> None:
        self.clock.advance(ms)
        self.receiver.set_monotonic(self.clock.mono)

    def status(self) -> str:
        """Receiver publishes a status; the publisher observes it if the link is up."""
        status = self.receiver.status()
        if status is None or not self.link_up:
            return "not-delivered"
        result = self.publisher.observe_status(json.dumps(status).encode("utf-8"))
        if self.verbose:
            print(f"    status seq={status['status_sequence']} echo={status['status_challenge']} "
                  f"armed={status['armed']} fault={status['fault_latch']} estop={status['software_estop_latch']} "
                  f"-> gate {result}")
        return result

    def fresh_perception(self) -> None:
        self.publisher.observe_frame(tracking_frame(self.clock.unix))
        self.publisher.observe_gesture(gesture(self.clock.unix))

    def tick(self, **kwargs):
        before = len(self.results)
        message = self.publisher.tick(**kwargs)
        result = self.results[-1] if len(self.results) > before else None
        return message, result

    def handshake(self) -> None:
        """Status, session start, echo status: leaves a trusted session."""
        self.status()
        message, result = self.tick()
        assert result == "accepted_start", (message, result)
        self.advance(10)
        assert self.status() == "accepted"
        assert self.publisher.session.trusted

    def arm(self) -> None:
        self.advance(10)
        message, result = self.tick(operator_rearm=True)
        assert result == "accepted_rearm", (message, result, self.publisher.last_decision)
        self.advance(10)
        assert self.status() == "accepted"


def describe(message: dict, result: str) -> str:
    return (
        f"  -> {message['task_state']:<6} session={message['session_id']} seq={message['sequence']:<3} "
        f"stop={str(message['stop']).lower():<5} lin={message['linear_mps']:.6f} ang={message['angular_rad_s']:.6f} "
        f"fresh={message['freshness_id']} gen={message['rearm_generation']} chal={message['status_challenge']} "
        f"mac={message['mac'][:12]}...  receiver: {result}"
    )
