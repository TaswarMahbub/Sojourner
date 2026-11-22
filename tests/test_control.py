"""control/ checked against the contract oracles in tests/.

control/ is written from docs/contracts.md. These tests drive it against
tests/control_auth.py, tests/status_gate.py and tests/motor_receiver.py, so a
misreading of the contract shows up as a disagreement with the oracle. No
ESP32 is involved; a passing run is not a hardware verification.
"""

from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import pytest

import control.durable as durable
import tests.control_auth as oracle_auth
import tests.status_gate as oracle_gate
from control import auth
from control.publisher import MotionRequest, decode_status
from control.reset import GATE_FILE, RESET_LOG, SESSION_FILE, ResetRefused, operator_reset
from control.session import SessionAllocator, SessionLocked, SessionStateError
from control.status_gate import GateLocked, PersistedStateError, PersistFailed, StatusGate
from control.transport import UdpTransport
from tests.contract_schema import SAFE_INT_MAX, SCHEMA, SEQUENCE_MAX
from tests.control_auth import CONTROLLER, TEST_KEY
from tests.control_rig import TEST_ENV, Rig, build_publisher, gesture, lost_frame, provision, tracking_frame

CONTROL_DIR = Path(__file__).resolve().parent.parent / "control"
MOVE = MotionRequest("manual", 0.1, 0.0)


# ----- isolation of production code ------------------------------------


def _control_sources():
    return {path.name: path.read_text(encoding="utf-8") for path in CONTROL_DIR.glob("*.py")}


def test_control_never_imports_the_oracles_or_the_test_key():
    for name, text in _control_sources().items():
        for forbidden in ("tests.control_auth", "tests.status_gate", "tests.motor_receiver", "control_rig", "TEST_KEY"):
            assert forbidden not in text, (name, forbidden)
        assert TEST_KEY.decode() not in text, name
        assert TEST_KEY.hex() not in text, name
        imports = re.findall(r"^\s*(?:from|import)\s+(tests\.\w+)", text, re.MULTILINE)
        assert set(imports) <= {"tests.contract_schema"}, (name, imports)


def test_no_network_facing_module_can_reach_the_operator_reset():
    for name, text in _control_sources().items():
        if name != "reset.py":
            assert "control.reset" not in text and "operator_reset" not in text, name


def test_no_schema_version_literal_in_control():
    for name, text in _control_sources().items():
        assert not re.search(r"schema_version\"?\s*[:=]\s*[\"']\d", text), name


# ----- credentials -----------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"SOJOURNER_CONTROLLER_ID": CONTROLLER},
        {"SOJOURNER_CONTROL_KEY": TEST_KEY.hex()},
        {"SOJOURNER_CONTROL_KEY": "00" * 31, "SOJOURNER_CONTROLLER_ID": CONTROLLER},
        {"SOJOURNER_CONTROL_KEY": "not hex", "SOJOURNER_CONTROLLER_ID": CONTROLLER},
        {"SOJOURNER_CONTROL_KEY": TEST_KEY.hex(), "SOJOURNER_CONTROLLER_ID": "bad id!"},
        {"SOJOURNER_CONTROL_KEY": TEST_KEY.hex(), "SOJOURNER_CONTROLLER_ID": "x" * 33},
    ],
)
def test_refuses_to_run_without_provisioned_credentials(environ):
    with pytest.raises(auth.MissingCredentials):
        auth.load_credentials(environ)


def test_credentials_do_not_print_the_key():
    credentials = auth.load_credentials(TEST_ENV)
    assert credentials.key == TEST_KEY
    assert TEST_KEY.hex() not in repr(credentials) and str(TEST_KEY) not in repr(credentials)


# ----- MAC equivalence -------------------------------------------------


def _command(**overrides):
    message = {
        "schema_version": SCHEMA,
        "controller_id": CONTROLLER,
        "boot_nonce": 7,
        "session_id": 1795276800100,
        "sequence": 1,
        "freshness_id": 3,
        "rearm_generation": 1,
        "status_challenge": 9,
        "issued_at_ms": 1795276800100,
        "expires_at_ms": 1795276800350,
        "task_state": "idle",
        "linear_mps": 0,
        "angular_rad_s": 0,
        "stop": True,
    }
    message.update(overrides)
    return message


def _status(**overrides):
    message = {
        "schema_version": SCHEMA,
        "controller_id": CONTROLLER,
        "timestamp_ms": 0,
        "boot_nonce": 7,
        "session_id": None,
        "last_sequence": None,
        "status_sequence": 1,
        "freshness_id": 1,
        "rearm_generation": 0,
        "status_challenge": 0,
        "clock_state": "unsynchronized",
        "sensor_state": "unknown",
        "cutoff_open": True,
        "physical_estop": True,
        "software_estop_latch": False,
        "fault_latch": False,
        "armed": False,
        "commanding_stop": True,
    }
    message.update(overrides)
    return message


COMMAND_CASES = [
    {},
    {"sequence": SEQUENCE_MAX},
    {"boot_nonce": SAFE_INT_MAX, "session_id": SAFE_INT_MAX, "freshness_id": SAFE_INT_MAX, "status_challenge": SAFE_INT_MAX},
    {"rearm_generation": 0, "issued_at_ms": 0, "expires_at_ms": 1},
    {"task_state": "manual", "stop": False, "linear_mps": 0.3, "angular_rad_s": -1.0},
    {"task_state": "shared", "stop": False, "linear_mps": -0.123456, "angular_rad_s": 0.000001},
    {"task_state": "manual", "stop": False, "linear_mps": 0.1, "angular_rad_s": 1},
    {"task_state": "rearm"},
    {"task_state": "estop"},
    {"task_state": "fault"},
    {"task_state": "hold"},
    {"controller_id": "other_controller-01"},
]


@pytest.mark.parametrize("overrides", COMMAND_CASES)
def test_command_mac_is_byte_identical_to_the_oracle(overrides):
    message = _command(**overrides)
    assert auth.sign_command(TEST_KEY, message) == oracle_auth.sign_command(TEST_KEY, message)
    assert re.fullmatch(r"[0-9a-f]{64}", auth.sign_command(TEST_KEY, message))


def test_mac_input_is_the_contract_text():
    body = auth.command_mac_input(_command(linear_mps=0.1, task_state="manual", stop=False)).decode()
    assert body.startswith("sojourner-command-mac-v1\nschema_version=")
    assert "\nlinear_mps=0.100000\nangular_rad_s=0.000000\nstop=false\n" in body
    assert "mac=" not in body


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"session_id": 12, "last_sequence": SEQUENCE_MAX, "status_sequence": SAFE_INT_MAX},
        {"sensor_state": "released", "physical_estop": False, "cutoff_open": False, "armed": True, "session_id": 1, "last_sequence": 2},
        {"clock_state": "synchronized", "timestamp_ms": 1795276800000, "fault_latch": True, "software_estop_latch": True},
    ],
)
def test_status_mac_is_byte_identical_to_the_oracle(overrides):
    message = _status(**overrides)
    signed = auth.sign_status(TEST_KEY, message)
    assert signed == oracle_auth.sign_status(TEST_KEY, message)
    message["mac"] = signed
    assert auth.verify_status(TEST_KEY, message) and oracle_auth.verify_status(TEST_KEY, message)
    message["mac"] = signed.upper()
    assert not auth.verify_status(TEST_KEY, message)


@pytest.mark.parametrize("velocity", [-0.0, 0.1234565, 0.30000000000000004, 1e-7, float("nan"), float("inf"), True, "0.1", None, 10**400])
def test_noncanonical_velocity_is_rejected_by_both(velocity):
    message = _command(task_state="manual", stop=False, linear_mps=velocity)
    with pytest.raises(ValueError):
        oracle_auth.sign_command(TEST_KEY, message)
    with pytest.raises(auth.CanonicalError):
        auth.sign_command(TEST_KEY, message)


@pytest.mark.parametrize(
    "value",
    [0, 1, -1, 0.0, 0.1, -0.1, 0.123456, 0.1234565, 0.3, -0.0, 1e-6, 5e-7, 2.5, 10**16, 10**400, 1e308, float("nan"), True, False, "x"],
)
def test_canonical_velocity_matches_the_oracle(value):
    assert auth.canonical_velocity(value) == oracle_auth.canonical_velocity(value)


def test_status_with_a_bad_type_does_not_verify():
    message = _status(boot_nonce=True)
    message["mac"] = "0" * 64
    assert auth.verify_status(TEST_KEY, message) is False


# ----- status gate equivalence -----------------------------------------


def _signed(**overrides):
    message = _status(**overrides)
    message["mac"] = oracle_auth.sign_status(TEST_KEY, message)
    return message


ARMED = {"sensor_state": "released", "physical_estop": False, "cutoff_open": False, "armed": True, "session_id": 5, "last_sequence": 3, "freshness_id": 11, "rearm_generation": 2}


def _gates(tmp_path):
    provision(tmp_path)
    mine = StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE)
    mine.restore_from_file()
    theirs = oracle_gate.StatusGate(TEST_KEY, CONTROLLER)
    return mine, theirs


def _both(mine, theirs, method, *args, **kwargs):
    a = getattr(mine, method)(*args, **kwargs)
    b = getattr(theirs, method)(*args, **kwargs)
    assert a == b, (method, args, a, b)
    return a


def test_gate_agrees_with_the_oracle_on_a_scripted_run(tmp_path):
    mine, theirs = _gates(tmp_path)
    steps = [
        ("expect", (1, 100)),
        ("observe", (_signed(status_challenge=1, status_sequence=1, **ARMED), 150)),
        ("motion_authorized", (150, 5, 4, 11)),
        ("motion_authorized", (150, 5, 3, 11)),
        ("motion_authorized", (150, 6, 4, 11)),
        ("motion_authorized", (150, 5, 4, 12)),
        ("motion_authorized", (350, 5, 4, 11)),
        ("motion_authorized", (351, 5, 4, 11)),
        ("observe", (_signed(status_challenge=1, status_sequence=1, **ARMED), 160)),
        ("observe", (_signed(status_challenge=2, status_sequence=2, **ARMED), 160)),
        ("expect", (1, 400)),
        ("expect", (3, 400)),
        ("expect", (3, 450)),
        ("observe", (_signed(status_challenge=3, status_sequence=2, **dict(ARMED, armed=False, fault_latch=True)), 420)),
        ("motion_authorized", (420, 5, 4, 11)),
        ("expect", (2, 500)),
        ("observe", (_signed(status_challenge=2, status_sequence=3), 500)),
        ("expect", (True, 500)),
        ("expect", (4.0, 500)),
        ("expect", (0, 500)),
        ("expect", (SAFE_INT_MAX + 1, 500)),
        ("expect", (10, 600)),
        ("observe", (_signed(status_challenge=10, status_sequence=1, boot_nonce=8, **dict(ARMED, armed=True)), 610)),
        ("observe", (_signed(status_challenge=10, status_sequence=1, boot_nonce=8), 610)),
        ("observe", (_signed(status_challenge=10, status_sequence=4, schema_version="3"), 610)),
        ("observe", (dict(_signed(status_challenge=10, status_sequence=5), mac="0" * 64), 610)),
        ("observe", (_signed(status_challenge=10, status_sequence=5, controller_id="other"), 610)),
        ("expect", (11, 700)),
        ("observe", (_signed(status_challenge=11, status_sequence=6, **dict(ARMED, armed=False, fault_latch=True, software_estop_latch=True)), 710)),
        ("rearm_authorized", (710, 5, 4, 11, 2), {"operator_authorized": True}),
        ("rearm_authorized", (710, 5, 4, 11, 2), {"operator_authorized": False}),
        ("rearm_authorized", (710, 5, 4, 11, 2), {"operator_authorized": 1}),
        ("rearm_authorized", (710, 5, 4, 11, 1), {"operator_authorized": True}),
        ("motion_authorized", (710, 5, 4, 11)),
    ]
    for step in steps:
        method, args = step[0], step[1]
        kwargs = step[2] if len(step) > 2 else {}
        _both(mine, theirs, method, *args, **kwargs)
    assert mine.export_state() == theirs.export_state()


@pytest.mark.parametrize(
    "change",
    [
        {"armed": False},
        {"armed": False, "cutoff_open": True},
        {"armed": False, "sensor_state": "asserted", "physical_estop": True},
        {"armed": False, "software_estop_latch": True},
        {"armed": False, "fault_latch": True},
    ],
)
def test_gate_refuses_motion_for_each_stop_condition(tmp_path, change):
    mine, theirs = _gates(tmp_path)
    _both(mine, theirs, "expect", 1, 0)
    assert _both(mine, theirs, "observe", _signed(status_challenge=1, status_sequence=1, **dict(ARMED, **change)), 10) == "accepted"
    assert _both(mine, theirs, "motion_authorized", 10, 5, 4, 11) is False


def test_gate_challenge_lifetime_boundaries(tmp_path):
    mine, _ = _gates(tmp_path)
    challenge = mine.issue_challenge(1000)
    assert mine.observe(_signed(status_challenge=challenge, status_sequence=1, **ARMED), 1010) == "accepted"
    assert mine.remaining_ms(1249) == 1 and mine.motion_authorized(1249, 5, 4, 11)
    assert mine.remaining_ms(1250) == 0 and mine.motion_authorized(1250, 5, 4, 11)
    assert mine.motion_authorized(1251, 5, 4, 11) is False


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "not json",
        "[]",
        '{"version":1,"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":NaN,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null,"sequences":[],"session_id":3}',
        '{"version":2,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null,"sequences":[]}',
        '{"version":true,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"other","challenge_high_water":0,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":4.0,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":-1,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":9007199254740992,"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":null,"sequences":[[7,1]]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":[]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":[[7,1,2]]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":[[7,1],[7,2]]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":[[7,true]]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":[[7,0]]}',
        '{"version":1,"controller_id":"laptop","challenge_high_water":0,"boot_nonce":7,"sequences":{"7":1}}',
    ],
)
def test_invalid_persisted_state_locks_both_gates(tmp_path, text):
    mine, theirs = _gates(tmp_path)
    with pytest.raises(PersistedStateError):
        mine.restore_from_text(text)
    with pytest.raises(oracle_gate.PersistedStateError):
        theirs.restore_json(text)
    assert mine.locked and theirs.locked
    with pytest.raises(GateLocked):
        mine.issue_challenge(0)
    with pytest.raises(GateLocked):
        mine.export_state()


# ----- full handshake against MotorReceiver ------------------------------


def test_full_handshake_then_motion_is_accepted(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    rig.advance(10)
    rig.fresh_perception()
    message, result = rig.tick(motion=MOVE)
    assert result == "accepted_motion"
    assert message["stop"] is False and message["linear_mps"] == 0.1
    assert rig.receiver.motors_have_torque()
    assert [m["task_state"] for m in rig.sent] == ["idle", "rearm", "manual"]
    assert rig.sent[0]["sequence"] == 1 and rig.sent[0]["stop"] is True
    assert rig.sent[0]["linear_mps"] == 0.0 and rig.sent[0]["angular_rad_s"] == 0.0


def test_no_datagram_before_any_verified_status(tmp_path):
    rig = Rig(tmp_path)
    assert rig.tick(motion=MOVE) == (None, None)
    assert rig.sent == []


def test_session_start_waits_for_a_verified_status_and_is_persisted(tmp_path):
    rig = Rig(tmp_path)
    rig.status()
    message, _ = rig.tick()
    record = json.loads((tmp_path / SESSION_FILE).read_text())
    assert record["session_high_water"] == message["session_id"]
    assert json.loads((tmp_path / GATE_FILE).read_text())["challenge_high_water"] == message["status_challenge"]


def test_session_is_not_trusted_without_the_echo(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.status()
    rig.link_up = False
    rig.tick()
    rig.link_up = True
    rig.advance(10)
    rig.status()
    assert not rig.publisher.session.trusted
    assert rig.tick(motion=MOVE) == (None, None)
    rig.advance(260)
    rig.status()
    message, result = rig.tick()
    assert message["sequence"] == 1 and result == "accepted_start"
    assert message["session_id"] > rig.sent[0]["session_id"]


def _armed_rig(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    rig.advance(10)
    rig.fresh_perception()
    return rig


def _assert_stop_only(rig, start):
    assert rig.sent[start:], "a stop must be published, not silence"
    for message in rig.sent[start:]:
        assert message["stop"] is True
        assert message["linear_mps"] == 0.0 and message["angular_rad_s"] == 0.0
    assert not rig.receiver.motors_have_torque()


def test_motion_refused_when_the_physical_stop_is_asserted(tmp_path):
    rig = _armed_rig(tmp_path)
    start = len(rig.sent)
    rig.receiver.set_physical_estop(True)
    rig.status()
    for _ in range(3):
        rig.advance(10)
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        rig.status()
    _assert_stop_only(rig, start)


def test_motion_refused_after_a_software_estop(tmp_path):
    rig = _armed_rig(tmp_path)
    start = len(rig.sent)
    message, result = rig.tick(operator_estop=True)
    assert message["task_state"] == "estop" and result == "accepted_stop"
    assert rig.receiver.software_estop_latch
    for _ in range(3):
        rig.advance(10)
        rig.status()
        rig.fresh_perception()
        rig.tick(motion=MOVE)
    _assert_stop_only(rig, start)


def test_motion_refused_with_the_fault_latch_set(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.advance(260)
    assert rig.receiver.fault_latch
    start = len(rig.sent)
    for _ in range(4):
        rig.status()
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        rig.advance(10)
    _assert_stop_only(rig, start)
    assert rig.receiver.fault_latch


def test_motion_refused_with_the_cutoff_open(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.receiver.restore_motor_power()
    assert rig.receiver.cutoff_open
    start = len(rig.sent)
    for _ in range(3):
        rig.advance(10)
        rig.status()
        rig.fresh_perception()
        rig.tick(motion=MOVE)
    _assert_stop_only(rig, start)


def test_motion_refused_while_not_armed(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    start = len(rig.sent)
    for _ in range(3):
        rig.advance(10)
        rig.fresh_perception()
        rig.tick(motion=MOVE)
        rig.status()
    _assert_stop_only(rig, start)


def test_motion_refused_once_the_challenge_has_expired(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.advance(240)
    rig.fresh_perception()
    start = len(rig.sent)
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True
    assert rig.publisher.last_decision.endswith("no current gate-accepted status")
    _assert_stop_only(rig, start)


def test_motion_refused_with_under_one_ms_remaining(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    # The rearm challenge was issued 10 ms before arm() returns.
    rig.advance(240)
    rig.fresh_perception()
    assert rig.publisher.gate.remaining_ms(rig.clock.mono) == 0
    assert rig.publisher.gate.current_status(rig.clock.mono) is not None
    start = len(rig.sent)
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True
    assert rig.publisher.last_decision.startswith("idle: under 1 ms")
    _assert_stop_only(rig, start)


def test_motion_allowed_with_exactly_one_ms_remaining(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    rig.advance(239)
    rig.fresh_perception()
    assert rig.publisher.gate.remaining_ms(rig.clock.mono) == 1
    message, result = rig.tick(motion=MOVE)
    assert message["stop"] is False and result == "accepted_motion"
    assert message["expires_at_ms"] - message["issued_at_ms"] == 1


# ----- perception produces stops, not silence --------------------------


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda rig, now: rig.publisher.observe_frame(tracking_frame(now - 251)), "camera frame stale"),
        (lambda rig, now: rig.publisher.observe_frame(tracking_frame(now + 5)), "camera frame stale"),
        (lambda rig, now: rig.publisher.observe_frame(lost_frame(now)), "camera frame not actionable"),
        (lambda rig, now: rig.publisher.observe_frame({"tracking_state": "tracking"}), "camera frame not actionable"),
        (lambda rig, now: rig.publisher.observe_gesture(gesture(now - 251)), "gesture stale"),
        (lambda rig, now: rig.publisher.observe_gesture(gesture(now, label="unknown")), "gesture unknown"),
        (lambda rig, now: rig.publisher.observe_gesture(gesture(now, label="smile")), "gesture invalid"),
        (lambda rig, now: rig.publisher.observe_gesture({"label": "neutral"}), "gesture invalid"),
    ],
)
def test_bad_perception_publishes_a_stop(tmp_path, setup, reason):
    rig = _armed_rig(tmp_path)
    setup(rig, rig.clock.unix)
    start = len(rig.sent)
    message, result = rig.tick(motion=MOVE)
    assert message is not None and message["stop"] is True and result == "accepted_stop"
    assert reason in rig.publisher.last_decision
    _assert_stop_only(rig, start)


def test_missing_perception_publishes_a_stop(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True and "camera frame missing" in rig.publisher.last_decision


def test_gesture_window_with_a_non_actionable_frame_publishes_a_stop(tmp_path):
    from tests.contract_schema import validate_gesture

    rig = _armed_rig(tmp_path)
    now = rig.clock.unix
    rig.publisher.observe_frame(tracking_frame(now - 60))
    rig.publisher.observe_frame(lost_frame(now - 40))
    rig.publisher.observe_frame(tracking_frame(now))
    # Three input frames, oldest at the window start. The message is
    # schema-valid, so only the window check can produce this stop. (A
    # classifier that follows section 2 would have labelled this window
    # "unknown"; the publisher must not rely on that.)
    window = gesture(now, start_ms=now - 60, frame_count=3)
    assert validate_gesture(window) == []
    rig.publisher.observe_gesture(window)
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True and "non-actionable" in rig.publisher.last_decision


def test_no_motion_request_publishes_a_stop(tmp_path):
    rig = _armed_rig(tmp_path)
    message, result = rig.tick()
    assert message["stop"] is True and result == "accepted_stop"


# ----- cached armed status ---------------------------------------------


def test_cached_armed_status_is_void_after_a_newer_disarmed_status(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.tick()
    rig.advance(5)
    assert rig.status() == "accepted"
    rig.receiver.set_physical_estop(True)
    rig.advance(5)
    rig.status()
    rig.fresh_perception()
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True


def test_cached_armed_status_is_void_after_250_ms(tmp_path):
    rig = _armed_rig(tmp_path)
    cached = rig.publisher.gate.current_status(rig.clock.mono)
    assert cached is not None and cached["armed"] is True
    rig.clock.advance(241)
    rig.fresh_perception()
    assert rig.publisher.gate.current_status(rig.clock.mono) is None
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True


def test_cached_armed_status_is_void_after_a_restart(tmp_path):
    rig = _armed_rig(tmp_path)
    armed_status = rig.receiver.status()
    rig.publisher.observe_status(json.dumps(armed_status))
    restarted = build_publisher(tmp_path, rig._deliver, rig.clock)
    assert restarted.gate.current_status(rig.clock.mono) is None
    assert restarted.observe_status(json.dumps(armed_status)) == "stale_challenge"
    restarted.observe_frame(tracking_frame(rig.clock.unix))
    restarted.observe_gesture(gesture(rig.clock.unix))
    message = restarted.tick(motion=MOVE)
    assert message is None or (message["stop"] is True and message["sequence"] == 1)
    assert restarted.gate.challenge_high_water >= rig.publisher.gate.challenge_high_water


# ----- persistence -----------------------------------------------------


def test_restart_restores_marks_and_authorizes_nothing(tmp_path):
    rig = _armed_rig(tmp_path)
    before = rig.publisher.gate.export_state()
    gate = StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE)
    gate.restore_from_file()
    assert gate.export_state() == before
    assert gate.outstanding_challenge is None and gate.current_status(rig.clock.mono) is None
    assert gate.motion_authorized(rig.clock.mono, rig.publisher.session.session_id, 99, 1) is False
    assert gate.issue_challenge(rig.clock.mono) == before["challenge_high_water"] + 1


def test_rollback_snapshot_is_refused_and_locks(tmp_path):
    rig = _armed_rig(tmp_path)
    gate = rig.publisher.gate
    current = gate.export_state()
    for older in (
        dict(current, challenge_high_water=current["challenge_high_water"] - 1),
        dict(current, sequences=[[b, s - 1] for b, s in current["sequences"]]),
        dict(current, sequences=[], boot_nonce=None),
    ):
        with pytest.raises(PersistedStateError):
            gate.restore_from_text(json.dumps(older))
        assert gate.locked
    gate.restore_from_text(json.dumps(current))
    assert not gate.locked


def test_returning_to_an_earlier_boot_is_refused(tmp_path):
    mine, _ = _gates(tmp_path)
    mine.restore_from_text(json.dumps({"version": 1, "controller_id": CONTROLLER, "challenge_high_water": 5, "boot_nonce": 9, "sequences": [[7, 3], [9, 1]]}))
    with pytest.raises(PersistedStateError):
        mine.restore_from_text(json.dumps({"version": 1, "controller_id": CONTROLLER, "challenge_high_water": 5, "boot_nonce": 7, "sequences": [[7, 3], [9, 1]]}))


def test_corrupt_file_locks_rather_than_resets(tmp_path):
    rig = _armed_rig(tmp_path)
    marks = rig.publisher.gate.export_state()
    (tmp_path / GATE_FILE).write_text('{"version":1,', encoding="utf-8")
    gate = StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE)
    with pytest.raises(PersistedStateError):
        gate.restore_from_file()
    assert gate.locked and gate.challenge_high_water == 0
    with pytest.raises(GateLocked):
        gate.issue_challenge(0)
    gate.restore_from_text(json.dumps(marks))
    assert not gate.locked and gate.challenge_high_water == marks["challenge_high_water"]


def test_missing_file_locks(tmp_path):
    gate = StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE)
    with pytest.raises(PersistedStateError):
        gate.restore_from_file()
    assert gate.locked


def test_challenge_mark_is_durable_before_the_datagram_leaves(tmp_path, monkeypatch):
    events = []
    real_write = durable.atomic_write_json

    def recording_write(path, obj):
        real_write(path, obj)
        if Path(path).name == GATE_FILE:
            events.append(("durable", obj["challenge_high_water"]))

    monkeypatch.setattr(durable, "atomic_write_json", recording_write)
    rig = Rig(tmp_path)
    original = rig.publisher._send

    def recording_send(payload):
        message = json.loads(payload)
        on_disk = json.loads((tmp_path / GATE_FILE).read_text())["challenge_high_water"]
        assert on_disk >= message["status_challenge"]
        events.append(("sent", message["status_challenge"]))
        original(payload)

    rig.publisher._send = recording_send
    rig.ready_receiver()
    rig.handshake()
    rig.arm()
    assert events.index(("durable", 1)) < events.index(("sent", 1))
    assert events.index(("durable", 2)) < events.index(("sent", 2))


def test_failed_durable_write_blocks_motion_but_not_stops(tmp_path, monkeypatch):
    rig = _armed_rig(tmp_path)
    gate = rig.publisher.gate
    mark = gate.challenge_high_water

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(durable.os, "replace", failing_replace)
    start = len(rig.sent)
    message, result = rig.tick(motion=MOVE)
    assert message["stop"] is True and result == "accepted_stop"
    assert message["status_challenge"] == mark
    assert gate.challenge_high_water == mark + 2
    assert gate.outstanding_challenge is None
    _assert_stop_only(rig, start)
    with pytest.raises(PersistFailed):
        gate.issue_challenge(rig.clock.mono)
    assert not list(tmp_path.glob("*.tmp.*"))


def test_failed_status_mark_write_does_not_authorize(tmp_path, monkeypatch):
    mine, _ = _gates(tmp_path)
    challenge = mine.issue_challenge(0)

    def failing_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(durable.os, "replace", failing_replace)
    assert mine.observe(_signed(status_challenge=challenge, status_sequence=1, **ARMED), 5) == "persist_failed"
    assert mine.motion_authorized(5, 5, 4, 11) is False
    assert mine.sequences[7] == 1


def test_challenge_ids_exhaust_without_wrapping(tmp_path):
    mine, _ = _gates(tmp_path)
    mine.restore_from_text(json.dumps({"version": 1, "controller_id": CONTROLLER, "challenge_high_water": SAFE_INT_MAX, "boot_nonce": None, "sequences": []}))
    from control.status_gate import ChallengeExhausted

    with pytest.raises(ChallengeExhausted):
        mine.issue_challenge(0)


# ----- session allocator -----------------------------------------------


def test_session_ids_strictly_increase_across_restarts(tmp_path):
    provision(tmp_path)
    first = SessionAllocator(CONTROLLER, tmp_path / SESSION_FILE)
    first.restore_from_file()
    a = first.next_session_id()
    b = first.next_session_id(floor=5)
    second = SessionAllocator(CONTROLLER, tmp_path / SESSION_FILE)
    second.restore_from_file()
    c = second.next_session_id()
    d = second.next_session_id(floor=1795276800000)
    assert a < b < c < d == 1795276800000


def test_corrupt_or_rolled_back_session_record_locks(tmp_path):
    provision(tmp_path)
    allocator = SessionAllocator(CONTROLLER, tmp_path / SESSION_FILE)
    allocator.restore_from_file()
    allocator.next_session_id(floor=100)
    (tmp_path / SESSION_FILE).write_text(json.dumps({"version": 1, "controller_id": CONTROLLER, "session_high_water": 3}))
    with pytest.raises(SessionStateError):
        allocator.restore_from_file()
    assert allocator.locked
    with pytest.raises(SessionLocked):
        allocator.next_session_id()
    (tmp_path / SESSION_FILE).write_text("{")
    fresh = SessionAllocator(CONTROLLER, tmp_path / SESSION_FILE)
    with pytest.raises(SessionStateError):
        fresh.restore_from_file()
    (tmp_path / SESSION_FILE).write_text(json.dumps({"version": 1, "controller_id": CONTROLLER, "session_high_water": 3, "boot_nonce": 1}))
    with pytest.raises(SessionStateError):
        fresh.restore_from_file()


def test_session_id_is_not_part_of_the_gate_object(tmp_path):
    provision(tmp_path)
    assert "session_id" not in json.loads((tmp_path / GATE_FILE).read_text())
    assert set(json.loads((tmp_path / SESSION_FILE).read_text())) == {"version", "controller_id", "session_high_water"}


def test_sequence_exhaustion_opens_a_greater_session(tmp_path):
    rig = _armed_rig(tmp_path)
    old = rig.publisher.session.session_id
    rig.publisher.session.next_sequence = SEQUENCE_MAX + 1
    message, result = rig.tick(motion=MOVE)
    assert message["sequence"] == 1 and message["session_id"] > old and message["task_state"] == "idle"


# ----- operator reset --------------------------------------------------


def test_operator_reset_starts_a_fresh_identity_and_keeps_backups(tmp_path):
    provision(tmp_path)
    (tmp_path / GATE_FILE).write_text("corrupt", encoding="utf-8")
    with pytest.raises(PersistedStateError):
        StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE).restore_from_file()
    with pytest.raises(ResetRefused):
        operator_reset(tmp_path, CONTROLLER, operator="op", reason="corrupt gate")
    with pytest.raises(ResetRefused):
        operator_reset(tmp_path, "laptop-2", operator="", reason="corrupt gate")
    record = operator_reset(tmp_path, "laptop-2", operator="op", reason="corrupt gate")
    assert len(record["backups"]) == 2
    for name in record["backups"]:
        assert (tmp_path / name).exists()
    log_lines = (tmp_path / RESET_LOG).read_text().splitlines()
    assert json.loads(log_lines[-1])["new_controller_id"] == "laptop-2"
    gate = StatusGate(TEST_KEY, "laptop-2", tmp_path / GATE_FILE)
    gate.restore_from_file()
    assert gate.issue_challenge(0) == 1
    with pytest.raises(PersistedStateError):
        StatusGate(TEST_KEY, CONTROLLER, tmp_path / GATE_FILE).restore_from_file()
    with pytest.raises(ResetRefused):
        operator_reset(tmp_path, CONTROLLER, operator="op", reason="try to go back")


def test_reset_cli_refuses_without_a_terminal(tmp_path, monkeypatch, capsys):
    from control import reset

    monkeypatch.setattr(reset.sys.stdin, "isatty", lambda: False, raising=False)
    code = reset.main(["--state-dir", str(tmp_path), "--new-controller-id", "x1", "--operator", "op", "--reason", "r"])
    assert code == 2 and not (tmp_path / GATE_FILE).exists()


# ----- watchdog and estop paths ----------------------------------------


def test_watchdog_stops_the_receiver_after_250_ms_of_silence(tmp_path):
    rig = _armed_rig(tmp_path)
    message, result = rig.tick(motion=MOVE)
    assert result == "accepted_motion"
    rig.receiver.set_monotonic(rig.clock.mono + 250)
    assert not rig.receiver.fault_latch
    rig.advance(251)
    snap = rig.receiver.snapshot()
    assert snap["fault_latch"] and not snap["armed"] and snap["linear_mps"] == 0.0
    assert not rig.receiver.motors_have_torque()


def test_recovery_after_watchdog_needs_rearm(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.tick(motion=MOVE)
    rig.advance(300)
    rig.status()
    rig.fresh_perception()
    message, result = rig.tick(motion=MOVE)
    assert message["stop"] is True and result == "accepted_stop"
    rig.advance(10)
    assert rig.status() == "accepted"
    rig.fresh_perception()
    message, _ = rig.tick(motion=MOVE)
    assert message["stop"] is True
    rig.advance(10)
    rig.status()
    message, result = rig.tick(operator_rearm=True)
    assert result == "accepted_rearm"
    rig.advance(10)
    rig.status()
    rig.fresh_perception()
    message, result = rig.tick(motion=MOVE)
    assert result == "accepted_motion"


def test_rearm_needs_the_local_operator_boolean(tmp_path):
    rig = Rig(tmp_path)
    rig.ready_receiver()
    rig.handshake()
    rig.advance(10)
    message, result = rig.tick(operator_rearm=1)
    assert message["task_state"] == "idle"
    assert not rig.receiver.armed


def test_rearm_is_not_sent_while_the_physical_stop_is_asserted(tmp_path):
    rig = Rig(tmp_path)
    rig.receiver.complete_startup()
    rig.handshake()
    rig.advance(10)
    message, _ = rig.tick(operator_rearm=True)
    assert message["task_state"] == "idle"


def test_operator_estop_reaches_a_session_after_a_publisher_restart(tmp_path):
    rig = _armed_rig(tmp_path)
    rig.tick(motion=MOVE)
    assert rig.receiver.motors_have_torque()
    restarted = build_publisher(tmp_path, rig._deliver, rig.clock)
    restarted.observe_status(json.dumps(rig.receiver.status()))
    message = restarted.tick(operator_estop=True)
    assert message["task_state"] == "estop"
    assert rig.results[-1] in ("accepted_stop", "stale_stop", "expired_stop")
    assert rig.receiver.software_estop_latch and not rig.receiver.motors_have_torque()


# ----- decoding and inputs ---------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * 8193,
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b"[" * 33 + b"]" * 33,
        b"\xff\xfe",
        b"[]",
        b"not json",
    ],
)
def test_status_decoder_rejects_without_raising(raw):
    value, reason = decode_status(raw)
    assert value is None and reason


def test_publisher_ignores_a_malformed_status(tmp_path):
    rig = Rig(tmp_path)
    assert rig.publisher.observe_status(b'{"a":NaN}') == "malformed"


@pytest.mark.parametrize(
    "args",
    [("hold", 0.0, 0.0), ("manual", -0.0, 0.0), ("manual", 0.1234565, 0.0), ("manual", 0.31, 0.0), ("shared", 0.0, 1.000001), ("manual", True, 0.0)],
)
def test_motion_request_refuses_bad_values(args):
    with pytest.raises(ValueError):
        MotionRequest(*args)


def test_every_published_datagram_validates_and_stops_are_zero(tmp_path):
    from tests.contract_schema import validate_motor

    rig = _armed_rig(tmp_path)
    rig.tick(motion=MOVE)
    rig.tick(operator_estop=True)
    for message in rig.sent:
        assert validate_motor(message) == []
        assert oracle_auth.verify_command(TEST_KEY, message)
        if message["stop"]:
            assert message["linear_mps"] == 0.0 and message["angular_rad_s"] == 0.0


# ----- transport -------------------------------------------------------


def test_udp_transport_round_trip_and_timeout():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    with UdpTransport(("127.0.0.1", 0), probe.getsockname()) as transport:
        transport.send(b"hello")
        payload, sender = probe.recvfrom(64)
        assert payload == b"hello"
        probe.sendto(b"x" * 8193, sender)
        assert len(transport.recv(1.0)) == 8193
        assert transport.recv(0.01) is None
    probe.close()
