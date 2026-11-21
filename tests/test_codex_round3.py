"""Regressions for the second Codex failure on schema 3.

These checks use adversarial inputs. They do not power a motor.
"""

from __future__ import annotations

from tests.control_auth import (
    TEST_KEY,
    canonical_velocity,
    decode_strict,
    sign_command,
    sign_status,
    verify_command,
)
from tests.motor_receiver import MotorReceiver
from tests.status_gate import StatusGate
from tests.test_codex_regressions import arm_moving, packet, ready

T0 = 1_795_276_800_000


def _stopped_motion(receiver, **overrides):
    before = receiver.snapshot()
    outcome = receiver.handle(packet(receiver, **overrides))
    after = receiver.snapshot()
    assert outcome == "rejected_motion"
    assert after["linear_mps"] == 0
    assert after["angular_rad_s"] == 0
    assert after["last_sequence"] == before["last_sequence"]
    assert after["last_accept_ms"] == before["last_accept_ms"]
    assert after["software_estop_latch"] is before["software_estop_latch"]
    assert after["fault_latch"] is before["fault_latch"]
    assert after["armed"] is before["armed"]
    assert after["rearm_generation"] == before["rearm_generation"]
    return after


def test_mac_rejects_zero_to_nonzero_and_boundary_aliases():
    assert canonical_velocity(0) == 0.0
    assert canonical_velocity(0.0) == 0.0
    assert canonical_velocity(-0.0) is None
    assert canonical_velocity(1e-7) is None
    assert canonical_velocity(4e-7) is None
    assert canonical_velocity(0.3000004) is None
    assert canonical_velocity(-1.0000004) is None
    assert canonical_velocity(0.3) == 0.3
    assert canonical_velocity(-0.3) == -0.3
    assert canonical_velocity(1) == 1.0
    assert canonical_velocity(-1.0) == -1.0

    receiver = ready()
    arm_moving(receiver, lifetime=250)
    zero = packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.0,
        angular_rad_s=0.0,
        stop=False,
        status_challenge=910,
    )
    assert verify_command(TEST_KEY, zero) is True
    forged = dict(zero)
    forged["linear_mps"] = 1e-7
    assert verify_command(TEST_KEY, forged) is False
    assert receiver.handle(forged) == "invalid_stop"
    assert receiver.snapshot()["linear_mps"] == 0.0

    edge = packet(
        receiver,
        sequence=5,
        task_state="manual",
        linear_mps=0.3,
        angular_rad_s=-1.0,
        stop=False,
        status_challenge=911,
    )
    assert receiver.handle(edge) == "accepted_motion"
    assert receiver.snapshot()["linear_mps"] == 0.3
    assert receiver.snapshot()["angular_rad_s"] == -1.0
    aliased = dict(edge)
    aliased["linear_mps"] = 0.3000004
    aliased["mac"] = edge["mac"]
    assert verify_command(TEST_KEY, aliased) is False
    try:
        sign_command(TEST_KEY, aliased)
        signed = True
    except ValueError:
        signed = False
    assert signed is False
    assert canonical_velocity(0.3) == receiver.snapshot()["linear_mps"]


def test_status_gate_bounds_challenge_age_schema_and_rearm():
    receiver = ready()
    arm_moving(receiver, challenge=920)
    status = receiver.status()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    gate.expect(status["status_challenge"], 0)
    assert gate.observe(status, 200) == "accepted"
    assert gate.motion_authorized(200, 10, 4, status["freshness_id"]) is True
    assert gate.motion_authorized(251, 10, 4, status["freshness_id"]) is False

    fresh = StatusGate(TEST_KEY, receiver.controller_id)
    fresh.expect(status["status_challenge"], 0)
    assert fresh.observe(status, 0) == "accepted"
    assert fresh.motion_authorized(250, 10, 4, status["freshness_id"]) is True
    assert fresh.motion_authorized(251, 10, 4, status["freshness_id"]) is False

    fresh.expect(status["status_challenge"] + 1, 10)
    assert fresh.motion_authorized(10, 10, 4, status["freshness_id"]) is False

    held = StatusGate(TEST_KEY, receiver.controller_id)
    held.expect(status["status_challenge"], 0)
    assert held.observe(status, 200) == "accepted"
    held.expect(status["status_challenge"], 200)
    assert held.motion_authorized(450, 10, 4, status["freshness_id"]) is False
    assert held.motion_authorized(200, 10, 4, status["freshness_id"]) is True

    wrong_schema = dict(status)
    wrong_schema["schema_version"] = "3"
    wrong_schema["mac"] = sign_status(TEST_KEY, wrong_schema)
    schema_gate = StatusGate(TEST_KEY, receiver.controller_id)
    schema_gate.expect(status["status_challenge"], 0)
    assert schema_gate.observe(wrong_schema, 0) == "invalid_status"
    assert schema_gate.motion_authorized(0, 10, 4, status["freshness_id"]) is False

    replay = StatusGate(TEST_KEY, receiver.controller_id)
    replay.expect(status["status_challenge"], 0)
    assert replay.observe(status, 0) == "accepted"
    assert replay.observe(status, 0) == "stale_or_reordered"
    newer = receiver.status()
    assert replay.observe(newer, 0) == "accepted"
    older = status
    assert replay.observe(older, 0) == "stale_or_reordered"


def test_deeply_nested_json_does_not_throw_or_disable_the_watchdog():
    receiver = ready()
    arm_moving(receiver, lifetime=250, challenge=930)
    accepted = receiver.snapshot()["last_accept_ms"]
    nested = "[" * 500 + "0" + "]" * 500
    try:
        decoded, error = decode_strict(nested)
        outcome = receiver.handle_raw(nested)
    except Exception as exc:
        raise AssertionError(f"500-deep JSON raised {exc!r}") from exc
    assert decoded is None and error == "malformed"
    assert outcome == "malformed"
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["last_accept_ms"] == accepted

    identified = (
        '{"boot_nonce":42,"session_id":10,"nested":'
        + ("{" * 500)
        + ("}" * 500)
        + "}"
    )
    try:
        identified_outcome = receiver.handle_raw(identified)
    except Exception as exc:
        raise AssertionError(f"identified deep JSON raised {exc!r}") from exc
    assert identified_outcome == "invalid_stop"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_sequence"] == 3
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["fault_latch"] is False
    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False


def test_hardware_cutoff_is_not_the_software_deadline():
    receiver = ready()
    arm_moving(receiver, lifetime=40)
    receiver.freeze_firmware()
    assert receiver.status() is None
    receiver.set_monotonic(40)
    held = receiver.snapshot()
    assert held["firmware_frozen"] is True
    assert held["linear_mps"] == 0.2
    assert held["cutoff_open"] is False
    assert held["cutoff_hold"] is False
    assert held["driver_enabled"] is True
    assert receiver.motors_have_torque() is True
    receiver.set_monotonic(251)
    opened = receiver.snapshot()
    assert opened["cutoff_open"] is True
    assert opened["cutoff_hold"] is True
    assert opened["driver_enabled"] is False
    assert opened["linear_mps"] == 0.2
    assert opened["armed"] is True
    assert receiver.motors_have_torque() is False
    assert receiver.handle(packet(receiver, sequence=4, task_state="rearm")) == "frozen"
    assert receiver.snapshot()["linear_mps"] == 0.2

    receiver.unfreeze_firmware()
    receiver.poll()
    caught_up = receiver.snapshot()
    assert caught_up["cutoff_open"] is True
    assert caught_up["linear_mps"] == 0
    assert caught_up["armed"] is False
    assert caught_up["fault_latch"] is True
    assert receiver.acknowledge_cutoff() is True
    restored = receiver.snapshot()
    assert restored["cutoff_open"] is False
    assert restored["driver_enabled"] is True
    assert restored["linear_mps"] == 0
    assert restored["armed"] is False
    assert receiver.motors_have_torque() is False


def test_power_restore_does_not_close_the_cutoff_or_restore_pwm():
    receiver = ready()
    arm_moving(receiver, linear=0.2)
    receiver.lose_motor_power()
    receiver.restore_motor_power()
    assert receiver.snapshot()["cutoff_open"] is True
    assert receiver.snapshot()["cutoff_hold"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["armed"] is False
    receiver.poll()
    assert receiver.snapshot()["cutoff_open"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.handle(packet(receiver, sequence=4, task_state="rearm")) == "rejected_rearm"
    assert receiver.acknowledge_cutoff() is True
    assert receiver.snapshot()["cutoff_open"] is False
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["linear_mps"] == 0


def test_motion_ends_by_challenge_issuance_plus_250_ms():
    receiver = ready()
    assert receiver.handle(packet(receiver, status_challenge=940)) == "accepted_start"
    assert (
        receiver.handle(packet(receiver, sequence=2, task_state="rearm", status_challenge=941))
        == "accepted_rearm"
    )
    issued = receiver.snapshot()["challenge_issued_mono"]
    receiver.set_monotonic(issued + 200)
    assert (
        receiver.handle(
            packet(
                receiver,
                sequence=3,
                task_state="manual",
                linear_mps=0.2,
                stop=False,
                issued_at_ms=T0,
                expires_at_ms=T0 + 250,
                status_challenge=942,
            )
        )
        == "accepted_motion"
    )
    state = receiver.snapshot()
    assert state["motion_deadline_mono"] == issued + 250
    assert state["motion_deadline_mono"] != issued + 200 + 250
    receiver.set_monotonic(issued + 249)
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["fault_latch"] is False
    receiver.set_monotonic(issued + 250)
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["armed"] is True

    late = ready()
    assert late.handle(packet(late, status_challenge=950)) == "accepted_start"
    assert late.handle(packet(late, sequence=2, task_state="rearm", status_challenge=951)) == "accepted_rearm"
    late.set_monotonic(250)
    _stopped_motion(
        late,
        sequence=3,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        issued_at_ms=T0,
        expires_at_ms=T0 + 250,
        status_challenge=952,
    )


def test_rejected_newer_motion_stops_without_refresh_and_replays_do_not():
    receiver = ready()
    arm_moving(receiver, lifetime=250)
    _stopped_motion(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=-0.2,
        stop=False,
        freshness_id=receiver.snapshot()["freshness_id"] + 9,
        status_challenge=960,
    )
    moving = ready()
    arm_moving(moving)
    _stopped_motion(
        moving,
        sequence=4,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        rearm_generation=moving.snapshot()["rearm_generation"] + 3,
        status_challenge=961,
    )

    latched = ready()
    arm_moving(latched)
    assert (
        latched.handle(packet(latched, sequence=4, task_state="estop", status_challenge=962))
        == "accepted_stop"
    )
    assert latched.snapshot()["software_estop_latch"] is True
    _stopped_motion(
        latched,
        sequence=5,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=963,
    )
    assert latched.snapshot()["software_estop_latch"] is True

    other = ready()
    arm_moving(other, linear=0.15)
    assert (
        other.handle(
            packet(
                other,
                sequence=2,
                task_state="manual",
                linear_mps=-0.2,
                stop=False,
                status_challenge=964,
            )
        )
        == "ignored"
    )
    assert other.snapshot()["linear_mps"] == 0.15
    assert (
        other.handle(
            packet(
                other,
                session_id=9,
                sequence=8,
                task_state="manual",
                linear_mps=0.2,
                stop=False,
                status_challenge=965,
            )
        )
        == "ignored"
    )
    assert other.snapshot()["linear_mps"] == 0.15
    assert other.snapshot()["session_id"] == 10
    foreign = MotorReceiver(99)
    foreign.complete_startup()
    foreign.set_sensor("released")
    foreign.poll()
    assert (
        other.handle(
            packet(
                foreign,
                sequence=4,
                task_state="manual",
                linear_mps=0.2,
                stop=False,
                status_challenge=966,
            )
        )
        == "ignored"
    )
    assert other.snapshot()["linear_mps"] == 0.15
    assert (
        other.handle(
            packet(
                other,
                session_id=12,
                sequence=4,
                task_state="manual",
                linear_mps=0.2,
                stop=False,
                status_challenge=967,
            )
        )
        == "ignored"
    )
    assert other.snapshot()["linear_mps"] == 0.15
    assert other.snapshot()["armed"] is True


def test_other_motion_rejection_branches_stop_only_the_active_session():
    synced = ready()
    synced.note_verified_clock(T0)
    arm_moving(synced, lifetime=250)
    synced.set_unix(T0 + 251)
    _stopped_motion(
        synced,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        issued_at_ms=T0,
        expires_at_ms=T0 + 250,
        status_challenge=980,
    )

    held = ready()
    arm_moving(held)
    held.set_physical_estop(True)
    _stopped_motion(
        held,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=982,
    )
    assert held.snapshot()["physical_estop"] is True
    held.set_physical_estop(False)
    held.set_sensor("unknown")
    _stopped_motion(
        held,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=983,
    )

    dark = ready()
    arm_moving(dark)
    dark.lose_motor_power()
    before = dark.snapshot()
    assert dark.handle(packet(dark, sequence=4, task_state="manual", linear_mps=0.2, stop=False)) == "rejected_motion"
    assert dark.snapshot()["linear_mps"] == 0
    assert dark.snapshot()["last_sequence"] == before["last_sequence"]
    assert dark.snapshot()["armed"] is False

    moving = ready()
    arm_moving(moving, linear=0.15)
    stranger = packet(
        moving,
        boot_nonce=99,
        sequence=4,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
        status_challenge=984,
    )
    stranger["mac"] = "ab" * 32
    assert moving.handle(stranger) == "ignored"
    assert moving.snapshot()["linear_mps"] == 0.15
    assert moving.snapshot()["last_accept_ms"] == 0


def test_rearm_is_allowed_while_software_latches_are_set():
    receiver = ready()
    arm_moving(receiver, challenge=970)
    assert (
        receiver.handle(packet(receiver, sequence=4, task_state="estop", status_challenge=973))
        == "accepted_stop"
    )
    receiver.set_monotonic(251)
    assert receiver.snapshot()["software_estop_latch"] is True
    assert receiver.snapshot()["fault_latch"] is True
    assert receiver.snapshot()["armed"] is False
    status = receiver.status()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    gate.expect(status["status_challenge"], receiver.monotonic_ms)
    assert gate.observe(status, receiver.monotonic_ms) == "accepted"
    assert gate.motion_authorized(
        receiver.monotonic_ms, 10, status["last_sequence"] + 1, status["freshness_id"]
    ) is False
    kwargs = dict(
        sender_mono=receiver.monotonic_ms,
        session_id=10,
        sequence=status["last_sequence"] + 1,
        freshness_id=status["freshness_id"],
        rearm_generation=status["rearm_generation"],
    )
    assert gate.rearm_authorized(**kwargs, operator_authorized=False) is False
    assert gate.rearm_authorized(**kwargs, operator_authorized=True) is True
    assert (
        receiver.handle(
            packet(
                receiver,
                sequence=status["last_sequence"] + 1,
                task_state="rearm",
                freshness_id=status["freshness_id"],
                rearm_generation=status["rearm_generation"],
                status_challenge=974,
            )
        )
        == "accepted_rearm"
    )
    assert receiver.snapshot()["software_estop_latch"] is False
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert (
        receiver.handle(
            packet(
                receiver,
                sequence=status["last_sequence"] + 2,
                task_state="manual",
                linear_mps=0.1,
                stop=False,
                status_challenge=975,
            )
        )
        == "accepted_motion"
    )
    assert receiver.snapshot()["linear_mps"] == 0.1
