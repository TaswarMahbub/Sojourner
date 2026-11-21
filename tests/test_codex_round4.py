"""Regressions for the Codex FAIL on dc0a176.

These checks use mocked datagrams. They do not power a motor and they are not
a bench test of the hardware cutoff.
"""

from __future__ import annotations

from tests.control_auth import TEST_KEY
from tests.status_gate import StatusGate
from tests.test_codex_regressions import arm_moving, packet, ready
from tests.test_codex_round3 import _stopped_motion

T0 = 1_795_276_800_000


# Finding 1: hardware-pet timeout ordering.


def _frozen_until(at_ms):
    receiver = ready()
    arm_moving(receiver, lifetime=250)
    assert receiver.snapshot()["last_pet_mono"] == 0
    receiver.freeze_firmware()
    receiver.set_monotonic(250)
    frozen = receiver.snapshot()
    assert frozen["cutoff_open"] is False
    assert frozen["cutoff_hold"] is False
    receiver.unfreeze_firmware()
    if at_ms > 250:
        receiver.set_monotonic(at_ms)
    else:
        receiver.poll()
    return receiver


def test_first_execution_at_251_ms_after_freeze_latches_the_cutoff():
    receiver = _frozen_until(251)
    state = receiver.snapshot()
    assert state["monotonic_ms"] == 251
    assert state["cutoff_hold"] is True
    assert state["cutoff_open"] is True
    assert state["driver_enabled"] is False
    assert state["linear_mps"] == 0
    assert state["angular_rad_s"] == 0
    assert state["armed"] is False
    assert receiver.motors_have_torque() is False
    assert receiver.status()["cutoff_open"] is True

    for later in (252, 300, 1000):
        receiver.set_monotonic(later)
        receiver.poll()
        assert receiver.snapshot()["cutoff_hold"] is True
        assert receiver.snapshot()["cutoff_open"] is True
        assert receiver.snapshot()["driver_enabled"] is False

    status = receiver.status()
    rearm = packet(
        receiver,
        sequence=4,
        task_state="rearm",
        freshness_id=status["freshness_id"],
        rearm_generation=status["rearm_generation"],
        status_challenge=1201,
    )
    assert receiver.handle(rearm) == "rejected_rearm"
    assert receiver.snapshot()["armed"] is False

    assert receiver.acknowledge_cutoff() is True
    restored = receiver.snapshot()
    assert restored["cutoff_hold"] is False
    assert restored["cutoff_open"] is False
    assert restored["driver_enabled"] is True
    assert restored["linear_mps"] == 0
    assert restored["armed"] is False
    assert receiver.motors_have_torque() is False


def test_first_execution_at_exactly_250_ms_after_freeze_does_not_trip():
    receiver = _frozen_until(250)
    state = receiver.snapshot()
    assert state["monotonic_ms"] == 250
    assert state["cutoff_hold"] is False
    assert state["cutoff_open"] is False
    assert state["driver_enabled"] is True
    assert state["last_pet_mono"] == 250


def test_restoration_as_the_first_execution_still_sees_the_late_pet():
    receiver = ready()
    arm_moving(receiver, lifetime=250)
    receiver.freeze_firmware()
    receiver.set_monotonic(250)
    receiver.unfreeze_firmware()
    receiver.monotonic_ms = 251
    assert receiver.acknowledge_cutoff() is True
    state = receiver.snapshot()
    assert state["linear_mps"] == 0
    assert state["armed"] is False
    assert state["fault_latch"] is True


# Finding 2: trusted UTC expiration boundary for incoming motion.


def _synced_moving():
    receiver = ready()
    receiver.note_verified_clock(T0)
    arm_moving(receiver, lifetime=250)
    assert receiver.snapshot()["linear_mps"] == 0.2
    return receiver


def _incoming(receiver, challenge):
    return packet(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        issued_at_ms=T0 - 200,
        expires_at_ms=T0 + 50,
        status_challenge=challenge,
    )


def test_incoming_motion_before_trusted_expiry_is_accepted():
    receiver = _synced_moving()
    receiver.set_unix(T0 + 49)
    assert receiver.handle(_incoming(receiver, 1301)) == "accepted_motion"
    assert receiver.snapshot()["linear_mps"] == 0.1


def test_incoming_motion_exactly_at_trusted_expiry_is_rejected_and_stops():
    receiver = _synced_moving()
    receiver.set_unix(T0 + 50)
    _stopped_motion(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        issued_at_ms=T0 - 200,
        expires_at_ms=T0 + 50,
        status_challenge=1302,
    )
    assert receiver.motors_have_torque() is False


def test_incoming_motion_after_trusted_expiry_is_rejected_and_stops():
    receiver = _synced_moving()
    receiver.set_unix(T0 + 51)
    _stopped_motion(
        receiver,
        sequence=4,
        task_state="manual",
        linear_mps=0.1,
        stop=False,
        issued_at_ms=T0 - 200,
        expires_at_ms=T0 + 50,
        status_challenge=1303,
    )
    assert receiver.motors_have_torque() is False


def test_active_motion_stops_exactly_at_trusted_expiry():
    receiver = _synced_moving()
    receiver.set_unix(T0 + 249)
    receiver.poll()
    assert receiver.snapshot()["linear_mps"] == 0.2
    receiver.set_unix(T0 + 250)
    receiver.poll()
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.motors_have_torque() is False


# Finding 3: status challenge reuse.


def _armed_statuses():
    receiver = ready()
    arm_moving(receiver, challenge=41)
    first = receiver.status()
    delayed = receiver.status()
    assert first["status_challenge"] == delayed["status_challenge"] == 43
    assert first["armed"] is True and delayed["armed"] is True
    return receiver, first, delayed


def _authorized(gate, mono, status):
    motion = gate.motion_authorized(mono, 10, 4, status["freshness_id"])
    rearm = gate.rearm_authorized(
        mono,
        10,
        4,
        status["freshness_id"],
        status["rearm_generation"],
        operator_authorized=True,
    )
    return motion, rearm


def test_a_b_a_challenge_reuse_cannot_authorize_a_delayed_status():
    receiver, first, delayed = _armed_statuses()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    a, b = 43, 44
    assert gate.expect(a, 0) is True
    assert gate.observe(first, 0) == "accepted"
    assert _authorized(gate, 0, first) == (True, True)

    assert gate.expect(b, 200) is True
    assert _authorized(gate, 200, first) == (False, False)

    assert gate.expect(a, 300) is False
    assert gate.observe(delayed, 300) == "stale_challenge"
    assert _authorized(gate, 300, delayed) == (False, False)
    assert _authorized(gate, 300, first) == (False, False)

    assert gate.expect(b, 300) is False
    assert gate.issue_challenge(300) == 45


def test_a_b_a_challenge_reuse_is_refused_after_a_restart():
    receiver, first, delayed = _armed_statuses()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    assert gate.expect(43, 0) is True
    assert gate.observe(first, 0) == "accepted"
    assert gate.expect(44, 200) is True

    restarted = StatusGate(TEST_KEY, receiver.controller_id)
    restarted.restore_state(gate.export_state())
    assert restarted.expect(43, 300) is False
    assert restarted.observe(delayed, 300) == "stale_challenge"
    assert _authorized(restarted, 300, delayed) == (False, False)
    assert restarted.issue_challenge(300) == 45


# Physical stop release keeps the cutoff open until explicit restoration.


def test_physical_release_does_not_close_the_cutoff_by_itself():
    receiver = ready()
    arm_moving(receiver)
    receiver.set_physical_estop(True)
    assert receiver.snapshot()["cutoff_hold"] is True
    receiver.set_physical_estop(False)
    receiver.poll()
    released = receiver.snapshot()
    assert released["sensor_state"] == "released"
    assert released["cutoff_open"] is True
    assert released["driver_enabled"] is False
    assert released["armed"] is False
    assert receiver.acknowledge_cutoff() is True
    assert receiver.snapshot()["cutoff_open"] is False
    assert receiver.snapshot()["armed"] is False
    assert receiver.motors_have_torque() is False
