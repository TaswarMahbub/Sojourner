"""Independent regressions for the schema 3 Codex findings.

These tests drive the oracle with hostile inputs and judge the observable
stop, arm, and torque state. They do not treat the receiver's private
formulas as the expected result. Nothing here powers a motor or an ESP32.
"""

from __future__ import annotations

import json

from tests.contract_schema import validate_motor
from tests.control_auth import TEST_KEY, decode_strict, seal, sign_status
from tests.motor_receiver import MotorReceiver
from tests.status_gate import StatusGate

T0 = 1_795_276_800_000
OTHER_KEY = b"sojourner-other-key-not-for-hardware"


def torque_from_contract(snap: dict) -> bool:
    """Torque from the contract. A software deadline is not the hardware cutoff."""
    if snap["cutoff_open"] or not snap["bus_powered"] or not snap["driver_enabled"]:
        return False
    if snap["sensor_state"] != "released" or snap["physical_estop"]:
        return False
    if not snap["armed"]:
        return False
    if snap["linear_mps"] == 0 and snap["angular_rad_s"] == 0:
        return False
    if not snap["firmware_frozen"]:
        deadline = snap["motion_deadline_mono"]
        if deadline is not None and snap["monotonic_ms"] >= deadline:
            return False
    return True


def packet(receiver, **overrides):
    freshness_id = overrides.pop("freshness_id", None)
    rearm_generation = overrides.pop("rearm_generation", None)
    controller_id = overrides.pop("controller_id", None)
    status_challenge = overrides.pop("status_challenge", None)
    key = overrides.pop("key", None)
    message = {
        "schema_version": "4",
        "boot_nonce": receiver.boot_nonce,
        "session_id": 10,
        "sequence": 1,
        "issued_at_ms": T0,
        "expires_at_ms": T0 + 250,
        "task_state": "idle",
        "linear_mps": 0,
        "angular_rad_s": 0,
        "stop": True,
    }
    message.update(overrides)
    return seal(
        receiver,
        message,
        key=key,
        freshness_id=freshness_id,
        rearm_generation=rearm_generation,
        controller_id=controller_id,
        status_challenge=status_challenge,
    )


def ready(nonce=42):
    receiver = MotorReceiver(nonce)
    receiver.complete_startup()
    receiver.set_sensor("released")
    receiver.poll()
    return receiver


def arm_moving(receiver, lifetime=250, linear=0.2, challenge=41):
    assert receiver.handle(packet(receiver, status_challenge=challenge)) == "accepted_start"
    assert (
        receiver.handle(
            packet(receiver, sequence=2, task_state="rearm", status_challenge=challenge + 1)
        )
        == "accepted_rearm"
    )
    outcome = receiver.handle(
        packet(
            receiver,
            sequence=3,
            task_state="manual",
            linear_mps=linear,
            stop=False,
            issued_at_ms=T0,
            expires_at_ms=T0 + lifetime,
            status_challenge=challenge + 2,
        )
    )
    assert outcome == "accepted_motion", outcome
    assert receiver.motors_have_torque() is True
    assert torque_from_contract(receiver.snapshot()) is True
    return receiver


def test_untrusted_unix_time_is_not_freshness_and_watchdog_stays_monotonic():
    receiver = ready()
    receiver.set_unix(T0)
    assert receiver.snapshot()["clock_synchronized"] is False
    years_old = packet(
        receiver,
        sequence=1,
        issued_at_ms=T0 - 10_000_000,
        expires_at_ms=T0 - 10_000_000 + 100,
        status_challenge=11,
    )
    assert receiver.handle(years_old) == "accepted_start"
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["monotonic_ms"] == 0

    receiver.set_unix(T0 + 10**8)
    assert receiver.snapshot()["clock_synchronized"] is False
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["last_accept_ms"] == 0
    mono_before = receiver.snapshot()["monotonic_ms"]
    receiver.set_unix(0)
    assert receiver.snapshot()["monotonic_ms"] == mono_before

    receiver.set_monotonic(10)
    assert receiver.snapshot()["fault_latch"] is False
    receiver.set_monotonic(250)
    assert receiver.snapshot()["fault_latch"] is False
    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0

    synced = ready()
    synced.note_verified_clock(T0)
    assert synced.snapshot()["clock_synchronized"] is True
    stale_dated = packet(
        synced,
        issued_at_ms=T0 - 10_000_000,
        expires_at_ms=T0 - 10_000_000 + 100,
        status_challenge=12,
    )
    assert synced.handle(stale_dated) == "ignored"
    assert synced.snapshot()["session_id"] is None
    assert synced.snapshot()["armed"] is False


def test_receipt_of_an_old_command_does_not_make_it_fresh():
    receiver = ready()
    arm_moving(receiver, lifetime=250)
    current = receiver.snapshot()["freshness_id"]
    queued = packet(
        receiver,
        sequence=5,
        task_state="manual",
        linear_mps=-0.2,
        stop=False,
        freshness_id=current,
        status_challenge=80,
    )
    applied = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=81,
    )
    assert receiver.handle(applied) == "accepted_motion"
    accepted = receiver.snapshot()["last_accept_ms"]
    assert receiver.handle(queued) == "rejected_motion"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["linear_mps"] != -0.2
    assert receiver.snapshot()["last_sequence"] == 4
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["armed"] is True
    assert receiver.handle(queued) == "rejected_motion"
    assert receiver.snapshot()["linear_mps"] == 0

    receiver.set_monotonic(200)
    stale_stop = packet(
        receiver,
        sequence=5,
        task_state="idle",
        freshness_id=receiver.snapshot()["freshness_id"] + 25,
        status_challenge=82,
    )
    accepted = receiver.snapshot()["last_accept_ms"]
    assert receiver.handle(stale_stop) == "expired_stop"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["armed"] is True


def test_monotonic_time_does_not_move_backward():
    receiver = ready()
    arm_moving(receiver, lifetime=80)
    receiver.set_monotonic(100)
    assert receiver.snapshot()["linear_mps"] == 0
    receiver.set_monotonic(20)
    assert receiver.snapshot()["monotonic_ms"] == 100
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["fault_latch"] is False


def test_motion_deadline_expires_with_no_new_packet_before_the_watchdog():
    receiver = ready()
    arm_moving(receiver, lifetime=40)
    accepted = receiver.snapshot()["last_accept_ms"]
    receiver.set_monotonic(39)
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.motors_have_torque() is True
    receiver.set_monotonic(40)
    state = receiver.snapshot()
    assert state["linear_mps"] == 0
    assert state["angular_rad_s"] == 0
    assert state["armed"] is True
    assert state["fault_latch"] is False
    assert state["last_accept_ms"] == accepted
    assert state["last_sequence"] == 3
    assert receiver.motors_have_torque() is False
    assert torque_from_contract(state) is False
    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False


def test_synchronized_clock_stops_at_expires_at_without_waiting_for_a_packet():
    receiver = ready()
    receiver.note_verified_clock(T0)
    arm_moving(receiver, lifetime=90)
    receiver.set_monotonic(10)
    receiver.note_verified_clock(T0 + 89)
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["fault_latch"] is False
    receiver.note_verified_clock(T0 + 90)
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["armed"] is True
    ahead = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        issued_at_ms=T0 + 5000,
        expires_at_ms=T0 + 5200,
        status_challenge=90,
    )
    assert receiver.handle(ahead) == "invalid_stop"
    assert receiver.snapshot()["last_sequence"] == 3
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["armed"] is True


def test_rejected_rearm_cannot_replay_after_physical_release():
    receiver = ready()
    arm_moving(receiver)
    before = packet(receiver, sequence=4, task_state="rearm", status_challenge=101)
    receiver.set_physical_estop(True)
    assert receiver.snapshot()["physical_estop"] is True
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["cutoff_open"] is True
    during_rearm = packet(receiver, sequence=5, task_state="rearm", status_challenge=102)
    during_motion = packet(
        receiver,
        sequence=6,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=103,
    )
    assert receiver.handle(during_rearm) == "rejected_rearm"
    assert receiver.handle(during_motion) == "rejected_motion"
    assert receiver.snapshot()["armed"] is False
    receiver.set_physical_estop(False)
    released = receiver.snapshot()
    assert released["physical_estop"] is False
    assert released["sensor_state"] == "released"
    assert released["armed"] is False
    assert released["linear_mps"] == 0
    assert released["cutoff_open"] is True
    assert released["cutoff_hold"] is True
    assert receiver.acknowledge_cutoff() is True
    assert receiver.snapshot()["armed"] is False
    for replay in (before, during_rearm, during_motion):
        receiver.handle(replay)
        assert receiver.snapshot()["armed"] is False
        assert receiver.snapshot()["linear_mps"] == 0
        assert receiver.motors_have_torque() is False
    status = receiver.status()
    fresh = packet(
        receiver,
        sequence=7,
        task_state="rearm",
        freshness_id=status["freshness_id"],
        rearm_generation=status["rearm_generation"],
        status_challenge=104,
    )
    assert fresh["rearm_generation"] == status["rearm_generation"]
    assert fresh["rearm_generation"] != before["rearm_generation"]
    assert receiver.handle(fresh) == "accepted_rearm"
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.motors_have_torque() is False


def test_motor_power_restoration_requires_a_new_rearm():
    receiver = ready()
    arm_moving(receiver)
    queued_rearm = packet(receiver, sequence=4, task_state="rearm", status_challenge=111)
    queued_motion = packet(
        receiver,
        sequence=5,
        task_state="manual",
        linear_mps=0.25,
        stop=False,
        status_challenge=112,
    )
    generation = receiver.snapshot()["rearm_generation"]
    receiver.lose_motor_power()
    assert receiver.snapshot()["bus_powered"] is False
    assert receiver.snapshot()["cutoff_open"] is True
    assert receiver.snapshot()["armed"] is False
    assert receiver.motors_have_torque() is False
    receiver.restore_motor_power()
    restored = receiver.snapshot()
    assert restored["bus_powered"] is True
    assert restored["armed"] is False
    assert restored["linear_mps"] == 0
    assert restored["boot_nonce"] == 42
    assert restored["rearm_generation"] == generation + 1
    receiver.poll()
    assert receiver.snapshot()["cutoff_open"] is True
    assert receiver.snapshot()["cutoff_hold"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.acknowledge_cutoff() is True
    assert receiver.snapshot()["cutoff_open"] is False
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.handle(queued_rearm) == "rejected_rearm"
    assert receiver.handle(queued_motion) == "rejected_motion"
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.motors_have_torque() is False
    status = receiver.status()
    renewed = packet(
        receiver,
        sequence=6,
        task_state="rearm",
        freshness_id=status["freshness_id"],
        rearm_generation=status["rearm_generation"],
        status_challenge=113,
    )
    assert receiver.handle(renewed) == "accepted_rearm"
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.motors_have_torque() is False


def test_startup_unknown_sensor_and_brownout_stay_stopped_and_disarmed():
    cold = MotorReceiver(42)
    cold_state = cold.snapshot()
    assert cold_state["sensor_state"] == "unknown"
    assert cold_state["physical_estop"] is True
    assert cold_state["startup_latch"] is True
    assert cold_state["cutoff_open"] is True
    assert cold_state["armed"] is False
    assert cold.motors_have_torque() is False
    assert torque_from_contract(cold_state) is False
    cold.complete_startup()
    assert cold.snapshot()["startup_latch"] is False
    assert cold.snapshot()["armed"] is False
    assert cold.snapshot()["cutoff_open"] is True
    assert cold.snapshot()["sensor_state"] == "unknown"
    assert cold.status()["sensor_state"] == "unknown"
    assert cold.status()["armed"] is False

    receiver = ready()
    arm_moving(receiver)
    old_motion = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=121,
    )
    receiver.set_sensor("unknown")
    assert receiver.snapshot()["physical_estop"] is True
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["cutoff_open"] is True
    assert receiver.motors_have_torque() is False

    live = ready()
    arm_moving(live)
    stolen = packet(
        live,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=122,
    )
    nonce = live.boot_nonce
    live.brownout_reset(nonce + 5)
    booted = live.snapshot()
    assert booted["boot_nonce"] == nonce + 5
    assert booted["session_id"] is None
    assert booted["armed"] is False
    assert booted["linear_mps"] == 0
    assert booted["sensor_state"] == "unknown"
    assert booted["cutoff_open"] is True
    assert booted["startup_latch"] is True
    assert booted["clock_synchronized"] is False
    assert live.handle(stolen) == "ignored"
    assert live.handle(old_motion) == "ignored"
    live.complete_startup()
    assert live.snapshot()["armed"] is False
    assert live.motors_have_torque() is False
    live.set_sensor("released")
    live.poll()
    assert live.snapshot()["armed"] is False
    assert live.snapshot()["linear_mps"] == 0


def test_frozen_firmware_loses_torque_through_the_cutoff():
    receiver = ready()
    arm_moving(receiver, lifetime=250)
    receiver.freeze_firmware()
    receiver.set_monotonic(251)
    state = receiver.snapshot()
    assert state["firmware_frozen"] is True
    assert state["cutoff_open"] is True
    assert state["linear_mps"] == 0.2
    assert state["armed"] is True
    assert state["monotonic_ms"] - state["last_pet_mono"] > 250
    assert torque_from_contract(state) is False
    assert receiver.motors_have_torque() is False
    assert receiver.handle(packet(receiver, sequence=4, task_state="rearm")) == "frozen"
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.motors_have_torque() is False


def test_unauthorized_controller_cannot_take_the_session_or_forge_status():
    receiver = ready()
    arm_moving(receiver)
    session = receiver.snapshot()["session_id"]
    takeover = packet(
        receiver,
        session_id=session + 1,
        sequence=1,
        key=OTHER_KEY,
        controller_id="intruder",
        status_challenge=201,
    )
    assert receiver.handle(takeover) == "ignored"
    assert receiver.snapshot()["session_id"] == session
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["armed"] is True

    same_key_other_name = packet(
        receiver,
        session_id=session + 2,
        sequence=1,
        controller_id="intruder",
        status_challenge=202,
    )
    assert receiver.handle(same_key_other_name) == "ignored"
    assert receiver.snapshot()["session_id"] == session
    assert receiver.snapshot()["linear_mps"] == 0.2

    forged_motion = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=-0.2,
        stop=False,
        key=OTHER_KEY,
        status_challenge=203,
    )
    accepted = receiver.snapshot()["last_accept_ms"]
    assert receiver.handle(forged_motion) == "invalid_stop"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_sequence"] == 3
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["session_id"] == session
    assert receiver.snapshot()["armed"] is True
    assert receiver.motors_have_torque() is False

    status = receiver.status()
    flipped = dict(status)
    flipped["armed"] = True
    flipped["mac"] = "0" + status["mac"][1:]
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    gate.expect(status["status_challenge"], receiver.monotonic_ms)
    assert gate.observe(flipped, receiver.monotonic_ms) == "spoofed"
    assert gate.motion_authorized(receiver.monotonic_ms, session, 4, status["freshness_id"]) is False

    unsigned = {
        "schema_version": "4",
        "controller_id": "laptop",
        "boot_nonce": receiver.boot_nonce,
        "session_id": session + 3,
        "sequence": 1,
        "issued_at_ms": T0,
        "expires_at_ms": T0 + 250,
        "task_state": "idle",
        "linear_mps": 0,
        "angular_rad_s": 0,
        "stop": True,
    }
    assert receiver.handle(unsigned) == "ignored"
    assert receiver.snapshot()["session_id"] == session


def test_parser_rejects_hostile_json_without_disabling_the_watchdog():
    receiver = ready()
    arm_moving(receiver, lifetime=250, challenge=301)
    accepted = receiver.snapshot()["last_accept_ms"]
    moving = receiver.snapshot()["linear_mps"]

    hostile_values = [
        {"linear_mps": 10**10000},
        {"linear_mps": float("nan")},
        {"linear_mps": float("inf")},
        {"linear_mps": float("-inf")},
        {"linear_mps": True},
        {"sequence": 10**10000},
        {"expires_at_ms": 10**10000},
        "not-json",
        None,
        [],
        {"stop": "yes"},
    ]
    for value in hostile_values:
        try:
            errors = validate_motor(value)
        except Exception as exc:
            raise AssertionError(f"validate_motor raised on {value!r}: {exc!r}") from exc
        assert isinstance(errors, list) and errors
    huge_motion = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        status_challenge=390,
    )
    huge_motion["linear_mps"] = 10**10000
    try:
        huge_errors = validate_motor(huge_motion)
    except Exception as exc:
        raise AssertionError(f"validate_motor raised on a huge velocity: {exc!r}") from exc
    assert huge_errors

    raw_samples = [
        "{",
        "",
        "[]",
        "null",
        '{"schema_version":',
        b"\xff\xfe",
        12345,
        float("nan"),
    ]
    for raw in raw_samples:
        try:
            outcome = receiver.handle_raw(raw)
        except Exception as exc:
            raise AssertionError(f"handle_raw raised on {raw!r}: {exc!r}") from exc
        assert outcome in {"malformed", "ignored", "invalid_stop"}
    assert receiver.snapshot()["linear_mps"] == moving
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["fault_latch"] is False

    duplicate = (
        '{"schema_version":"3","boot_nonce":42,"session_id":10,'
        '"sequence":4,"sequence":4,"task_state":"manual","stop":false,'
        '"linear_mps":0.2,"linear_mps":-0.2}'
    )
    decoded, error = decode_strict(duplicate)
    assert decoded is None and error == "malformed"
    try:
        outcome = receiver.handle_raw(duplicate)
    except Exception as exc:
        raise AssertionError(f"duplicate key raised {exc!r}") from exc
    assert outcome == "invalid_stop"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_sequence"] == 3
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["armed"] is True

    nan_body = (
        '{"schema_version":"3","boot_nonce":42,"session_id":10,'
        '"sequence":5,"linear_mps":NaN,"stop":false}'
    )
    try:
        nan_outcome = receiver.handle_raw(nan_body)
    except Exception as exc:
        raise AssertionError(f"NaN payload raised {exc!r}") from exc
    assert nan_outcome == "invalid_stop"
    assert receiver.snapshot()["fault_latch"] is False

    huge = (
        '{"schema_version":"3","boot_nonce":42,"session_id":10,'
        '"sequence":6,"linear_mps":' + ("9" * 4000) + ',"stop":false}'
    )
    try:
        huge_outcome = receiver.handle_raw(huge)
    except Exception as exc:
        raise AssertionError(f"huge number raised {exc!r}") from exc
    assert huge_outcome == "invalid_stop"
    over_limit = (
        '{"schema_version":"3","boot_nonce":42,"session_id":10,'
        '"linear_mps":' + ("9" * 6000) + "}"
    )
    try:
        over_outcome = receiver.handle_raw(over_limit)
    except Exception as exc:
        raise AssertionError(f"over-limit number raised {exc!r}") from exc
    assert over_outcome == "invalid_stop"
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["fault_latch"] is False

    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0


def test_status_gate_rejects_cached_stale_reordered_and_wrong_boot_status():
    receiver = ready()
    arm_moving(receiver, challenge=401)
    status = receiver.status()
    assert status["status_challenge"] == 403
    assert status["armed"] is True
    sent = receiver.monotonic_ms
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    gate.expect(403, sent)
    assert gate.observe(status, sent) == "accepted"
    assert gate.motion_authorized(sent, 10, 4, status["freshness_id"]) is True
    assert gate.motion_authorized(sent + 251, 10, 4, status["freshness_id"]) is False

    cached = dict(status)
    assert (
        receiver.handle(
            packet(receiver, sequence=4, task_state="estop", status_challenge=404)
        )
        == "accepted_stop"
    )
    disarmed = receiver.status()
    assert disarmed["armed"] is False
    gate.expect(404, receiver.monotonic_ms)
    assert gate.observe(disarmed, receiver.monotonic_ms) == "accepted"
    assert gate.motion_authorized(receiver.monotonic_ms, 10, 5, disarmed["freshness_id"]) is False
    saved = gate.export_state()
    assert gate.expect(403, receiver.monotonic_ms) is False
    assert gate.observe(cached, receiver.monotonic_ms) == "stale_challenge"
    assert gate.motion_authorized(receiver.monotonic_ms, 10, 5, cached["freshness_id"]) is False

    restarted = StatusGate(TEST_KEY, receiver.controller_id)
    restarted.restore_state(saved)
    restarted.expect(404, receiver.monotonic_ms)
    assert restarted.observe(disarmed, receiver.monotonic_ms) == "stale_or_reordered"
    assert restarted.motion_authorized(receiver.monotonic_ms, 10, 5, disarmed["freshness_id"]) is False

    newer = receiver.status()
    restarted.expect(404, receiver.monotonic_ms)
    assert restarted.observe(newer, receiver.monotonic_ms) == "accepted"
    assert newer["armed"] is False
    assert restarted.motion_authorized(receiver.monotonic_ms, 10, 5, newer["freshness_id"]) is False

    other = ready(nonce=99)
    arm_moving(other, challenge=501)
    foreign = other.status()
    assert foreign["armed"] is True
    assert foreign["boot_nonce"] == 99
    gate.expect(foreign["status_challenge"], receiver.monotonic_ms)
    assert gate.observe(foreign, receiver.monotonic_ms) == "wrong_boot"
    assert gate.motion_authorized(
        receiver.monotonic_ms, 10, 4, foreign["freshness_id"]
    ) is False

    quiet = MotorReceiver(77)
    quiet.complete_startup()
    quiet.set_sensor("released")
    quiet.poll()
    quiet.handle(packet(quiet, status_challenge=601))
    quiet_status = quiet.status()
    assert quiet_status["armed"] is False
    gate.expect(601, receiver.monotonic_ms)
    assert gate.observe(quiet_status, receiver.monotonic_ms) == "accepted"
    assert gate.motion_authorized(receiver.monotonic_ms, 10, 4, quiet_status["freshness_id"]) is False
    assert gate.expect(cached["status_challenge"], receiver.monotonic_ms) is False
    assert gate.observe(cached, receiver.monotonic_ms) == "stale_challenge"
    assert gate.motion_authorized(receiver.monotonic_ms, 10, 4, cached["freshness_id"]) is False

    first, second = receiver.status(), receiver.status()
    order = StatusGate(TEST_KEY, receiver.controller_id)
    order.expect(second["status_challenge"], receiver.monotonic_ms)
    assert order.observe(second, receiver.monotonic_ms) == "accepted"
    assert order.observe(first, receiver.monotonic_ms) == "stale_or_reordered"


def test_sequence_exhaustion_sender_restart_and_session_replacement():
    receiver = ready()
    assert receiver.handle(packet(receiver, status_challenge=701)) == "accepted_start"
    original_rearm = packet(receiver, sequence=2, task_state="rearm", status_challenge=702)
    assert receiver.handle(original_rearm) == "accepted_rearm"
    assert (
        receiver.handle(
            packet(
                receiver,
                sequence=4294967295,
                task_state="manual",
                linear_mps=0.1,
                stop=False,
                status_challenge=703,
            )
        )
        == "accepted_motion"
    )
    overflow = packet(
        receiver,
        sequence=4294967296,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        status_challenge=704,
    )
    assert receiver.handle(overflow) == "invalid_stop"
    assert receiver.snapshot()["last_sequence"] == 4294967295
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.handle(original_rearm) == "ignored"
    assert receiver.snapshot()["last_sequence"] == 4294967295

    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False
    restart_start = packet(receiver, sequence=1, status_challenge=705)
    accepted = receiver.snapshot()["last_accept_ms"]
    assert receiver.handle(restart_start) == "stale_stop"
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.handle(original_rearm) == "ignored"
    assert receiver.snapshot()["armed"] is False

    assert (
        receiver.handle(packet(receiver, session_id=11, sequence=1, status_challenge=706))
        == "accepted_start"
    )
    replaced = receiver.snapshot()
    assert replaced["session_id"] == 11
    assert replaced["armed"] is False
    assert replaced["linear_mps"] == 0
    assert replaced["last_sequence"] == 1
    assert receiver.handle(original_rearm) == "ignored"
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["session_id"] == 11
    assert (
        receiver.handle(
            packet(receiver, session_id=11, sequence=2, task_state="rearm", status_challenge=707)
        )
        == "accepted_rearm"
    )
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["linear_mps"] == 0


def test_status_mac_covers_boot_session_and_arm_bit():
    receiver = ready()
    arm_moving(receiver, challenge=801)
    status = receiver.status()
    tampered = dict(status)
    tampered["armed"] = False
    tampered["mac"] = sign_status(OTHER_KEY, tampered)
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    gate.expect(status["status_challenge"], 0)
    assert gate.observe(tampered, 0) == "spoofed"
    assert gate.observe(status, 0) == "accepted"
    assert json.dumps(status)
