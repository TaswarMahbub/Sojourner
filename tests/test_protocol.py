"""Protocol-state checks with mocked datagrams.

These tests do not power motors and do not verify ESP32 behavior.
"""

from tests.control_auth import seal
from tests.motor_receiver import MotorReceiver
from tests.status_gate import StatusGate

T0 = 1_795_276_800_000
NONCE = 42


def command(**overrides):
    message = {
        "schema_version": "4",
        "boot_nonce": NONCE,
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
    return message


def framed(receiver, **overrides):
    return seal(receiver, command(**overrides))


def boot(nonce=NONCE):
    receiver = MotorReceiver(nonce)
    receiver.complete_startup()
    receiver.set_sensor("released")
    receiver.set_monotonic(T0)
    return receiver


def start(receiver, session_id=10, at=T0):
    receiver.set_monotonic(at)
    outcome = receiver.handle(
        framed(
            receiver,
            session_id=session_id,
            sequence=1,
            issued_at_ms=at,
            expires_at_ms=at + 250,
        )
    )
    assert outcome == "accepted_start", outcome
    return outcome


def rearm(receiver, session_id=10, sequence=2, at=T0):
    receiver.set_monotonic(at)
    outcome = receiver.handle(
        framed(
            receiver,
            session_id=session_id,
            sequence=sequence,
            issued_at_ms=at,
            expires_at_ms=at + 250,
            task_state="rearm",
        )
    )
    assert outcome == "accepted_rearm", outcome
    return outcome


def motion(receiver, session_id=10, sequence=3, at=T0, linear=0.2, lifetime=250):
    receiver.set_monotonic(at)
    return receiver.handle(
        framed(
            receiver,
            session_id=session_id,
            sequence=sequence,
            issued_at_ms=at,
            expires_at_ms=at + lifetime,
            task_state="manual",
            linear_mps=linear,
            angular_rad_s=0,
            stop=False,
        )
    )


def test_session_start_is_stopped_and_disarmed():
    receiver = boot()
    start(receiver)
    state = receiver.snapshot()
    assert state["linear_mps"] == 0
    assert state["armed"] is False
    assert state["last_sequence"] == 1
    status = receiver.status()
    gate = StatusGate(receiver.key, receiver.controller_id)
    gate.expect(status["status_challenge"], receiver.monotonic_ms)
    assert gate.observe(status, receiver.monotonic_ms) == "accepted"
    assert gate.motion_authorized(receiver.monotonic_ms, 10, 2, status["freshness_id"]) is False


def test_motion_before_rearm_is_rejected():
    receiver = boot()
    start(receiver)
    assert motion(receiver) == "rejected_motion"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_sequence"] == 1


def test_rearm_then_motion_requires_observed_status():
    receiver = boot()
    start(receiver)
    rearm(receiver)
    status = receiver.status()
    gate = StatusGate(receiver.key, receiver.controller_id)
    gate.expect(status["status_challenge"], receiver.monotonic_ms)
    assert gate.observe(status, receiver.monotonic_ms) == "accepted"
    assert (
        gate.motion_authorized(
            receiver.monotonic_ms,
            10,
            status["last_sequence"] + 1,
            status["freshness_id"],
        )
        is True
    )
    assert StatusGate(receiver.key, receiver.controller_id).motion_authorized(0, 10, 3, 1) is False
    assert motion(receiver) == "accepted_motion"
    assert receiver.snapshot()["linear_mps"] == 0.2


def test_invalid_message_stops_without_refreshing_the_watchdog():
    receiver = boot()
    start(receiver)
    rearm(receiver, at=T0 + 10)
    assert motion(receiver, at=T0 + 20) == "accepted_motion"
    accepted = receiver.snapshot()["last_accept_ms"]
    receiver.set_monotonic(T0 + 30)
    bad = command(
        sequence=4,
        issued_at_ms=T0 + 30,
        expires_at_ms=T0 + 280,
        task_state="idle",
        linear_mps=0.2,
    )
    assert receiver.handle(bad) == "invalid_stop"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["last_sequence"] == 3
    assert receiver.snapshot()["last_accept_ms"] == accepted
    assert receiver.snapshot()["fault_latch"] is False


def test_out_of_order_motion_does_not_replace_the_applied_command():
    receiver = boot()
    start(receiver)
    rearm(receiver)
    assert motion(receiver, sequence=4, linear=0.2) == "accepted_motion"
    late = motion(receiver, sequence=3, linear=-0.2)
    assert late == "ignored"
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["last_sequence"] == 4


def test_older_stop_beats_a_newer_motion_without_refresh_or_latch_clear():
    receiver = boot()
    start(receiver)
    rearm(receiver, at=T0 + 10)
    assert motion(receiver, sequence=5, at=T0 + 20, linear=0.25) == "accepted_motion"
    accepted = receiver.snapshot()["last_accept_ms"]
    receiver.set_monotonic(T0 + 40)
    stale_estop = framed(receiver,
        sequence=4,
        issued_at_ms=T0 + 40,
        expires_at_ms=T0 + 290,
        task_state="estop",
    )
    assert receiver.handle(stale_estop) == "stale_stop"
    state = receiver.snapshot()
    assert state["linear_mps"] == 0
    assert state["software_estop_latch"] is True
    assert state["armed"] is False
    assert state["last_sequence"] == 5
    assert state["last_accept_ms"] == accepted
    receiver.set_monotonic(T0 + 50)
    stale_rearm = framed(receiver,
        sequence=2,
        issued_at_ms=T0 + 50,
        expires_at_ms=T0 + 300,
        task_state="rearm",
    )
    assert receiver.handle(stale_rearm) == "ignored"
    assert receiver.snapshot()["software_estop_latch"] is True
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["last_accept_ms"] == accepted


def test_stale_idle_stop_does_not_clear_a_fault_latch():
    receiver = boot()
    start(receiver)
    receiver.set_monotonic(T0 + 251)
    assert receiver.snapshot()["fault_latch"] is True
    accepted = receiver.snapshot()["last_accept_ms"]
    receiver.set_monotonic(T0 + 300)
    stale_idle = framed(receiver,
        sequence=1,
        issued_at_ms=T0 + 300,
        expires_at_ms=T0 + 550,
        task_state="idle",
    )
    assert receiver.handle(stale_idle) == "stale_stop"
    state = receiver.snapshot()
    assert state["fault_latch"] is True
    assert state["armed"] is False
    assert state["linear_mps"] == 0
    assert state["last_accept_ms"] == accepted


def test_new_session_cannot_be_rearmed_by_the_previous_session():
    receiver = boot()
    start(receiver, session_id=10)
    rearm(receiver, session_id=10, sequence=2, at=T0 + 10)
    assert motion(receiver, session_id=10, sequence=3, at=T0 + 20) == "accepted_motion"
    receiver.set_monotonic(T0 + 30)
    new_start = framed(receiver,
        session_id=11,
        sequence=1,
        issued_at_ms=T0 + 30,
        expires_at_ms=T0 + 280,
    )
    assert receiver.handle(new_start) == "accepted_start"
    state = receiver.snapshot()
    assert state["session_id"] == 11
    assert state["armed"] is False
    assert state["linear_mps"] == 0
    delayed_motion = framed(receiver,
        session_id=10,
        sequence=9,
        issued_at_ms=T0 + 40,
        expires_at_ms=T0 + 290,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
    )
    receiver.set_monotonic(T0 + 40)
    assert receiver.handle(delayed_motion) == "ignored"
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["armed"] is False
    assert receiver.snapshot()["session_id"] == 11
    delayed_rearm = framed(receiver,
        session_id=10,
        sequence=8,
        issued_at_ms=T0 + 40,
        expires_at_ms=T0 + 290,
        task_state="rearm",
    )
    assert receiver.handle(delayed_rearm) == "ignored"
    assert receiver.snapshot()["armed"] is False


def test_reboot_nonce_drops_packets_from_the_previous_boot():
    first = boot()
    start(first)
    rearm(first)
    assert motion(first) == "accepted_motion"
    old_motion = framed(first,
        sequence=4,
        issued_at_ms=T0 + 10,
        expires_at_ms=T0 + 260,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
    )
    rebooted = boot(nonce=99)
    rebooted.set_monotonic(T0 + 10)
    assert rebooted.handle(old_motion) == "ignored"
    assert rebooted.snapshot()["armed"] is False
    assert rebooted.snapshot()["linear_mps"] == 0
    assert rebooted.snapshot()["session_id"] is None


def test_watchdog_expiration_latches_a_fault():
    receiver = boot()
    start(receiver)
    rearm(receiver)
    assert motion(receiver, at=T0) == "accepted_motion"
    receiver.set_monotonic(T0 + 249)
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["linear_mps"] == 0.2
    receiver.set_monotonic(T0 + 250)
    assert receiver.snapshot()["fault_latch"] is False
    assert receiver.snapshot()["armed"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    receiver.set_monotonic(T0 + 251)
    state = receiver.snapshot()
    assert state["fault_latch"] is True
    assert state["armed"] is False
    assert state["linear_mps"] == 0
    assert state["physical_estop"] is False
    assert state["software_estop_latch"] is False


def test_physical_estop_is_not_cleared_by_idle_or_rearm():
    receiver = boot()
    start(receiver)
    rearm(receiver, at=T0 + 10)
    assert motion(receiver, at=T0 + 20) == "accepted_motion"
    receiver.set_physical_estop(True)
    assert receiver.snapshot()["physical_estop"] is True
    assert receiver.snapshot()["linear_mps"] == 0
    assert receiver.snapshot()["armed"] is False
    receiver.set_monotonic(T0 + 30)
    idle = framed(receiver,
        sequence=4,
        issued_at_ms=T0 + 30,
        expires_at_ms=T0 + 280,
        task_state="idle",
    )
    assert receiver.handle(idle) == "accepted_stop"
    assert receiver.snapshot()["physical_estop"] is True
    assert receiver.snapshot()["armed"] is False
    receiver.set_monotonic(T0 + 40)
    early_rearm = framed(receiver,
        sequence=5,
        issued_at_ms=T0 + 40,
        expires_at_ms=T0 + 290,
        task_state="rearm",
    )
    assert receiver.handle(early_rearm) == "rejected_rearm"
    assert receiver.snapshot()["physical_estop"] is True
    assert receiver.snapshot()["armed"] is False
    receiver.set_physical_estop(False)
    released = receiver.snapshot()
    assert released["physical_estop"] is False
    assert released["armed"] is False
    assert released["linear_mps"] == 0
    assert released["cutoff_open"] is True
    assert released["cutoff_hold"] is True
    assert receiver.acknowledge_cutoff() is True
    assert receiver.snapshot()["armed"] is False
    rearm(receiver, sequence=6, at=T0 + 50)
    assert receiver.snapshot()["armed"] is True
    assert motion(receiver, sequence=7, at=T0 + 60, linear=0.1) == "accepted_motion"


def test_active_motion_expires_without_another_packet():
    receiver = boot()
    start(receiver)
    rearm(receiver)
    assert motion(receiver, sequence=4, at=T0 + 20, linear=0.2, lifetime=40) == "accepted_motion"
    accepted = receiver.snapshot()["last_accept_ms"]
    receiver.set_monotonic(T0 + 20 + 39)
    assert receiver.snapshot()["linear_mps"] == 0.2
    assert receiver.snapshot()["fault_latch"] is False
    receiver.set_monotonic(T0 + 20 + 40)
    state = receiver.snapshot()
    assert state["linear_mps"] == 0
    assert state["last_sequence"] == 4
    assert state["last_accept_ms"] == accepted
    assert state["fault_latch"] is False
    assert state["armed"] is True


def test_early_packet_from_a_later_session_does_not_take_over():
    receiver = boot()
    start(receiver, session_id=10)
    rearm(receiver, session_id=10)
    assert motion(receiver, session_id=10, linear=0.15) == "accepted_motion"
    early = framed(receiver,
        session_id=12,
        sequence=3,
        task_state="manual",
        linear_mps=0.2,
        stop=False,
    )
    assert receiver.handle(early) == "ignored"
    assert receiver.snapshot()["session_id"] == 10
    assert receiver.snapshot()["linear_mps"] == 0.15
    assert receiver.snapshot()["armed"] is True


def test_ordinary_idle_does_not_clear_software_estop():
    receiver = boot()
    start(receiver)
    rearm(receiver, at=T0 + 10)
    receiver.set_monotonic(T0 + 20)
    assert (
        receiver.handle(
            framed(receiver,
                sequence=3,
                issued_at_ms=T0 + 20,
                expires_at_ms=T0 + 270,
                task_state="estop",
            )
        )
        == "accepted_stop"
    )
    receiver.set_monotonic(T0 + 30)
    assert (
        receiver.handle(
            framed(receiver,
                sequence=4,
                issued_at_ms=T0 + 30,
                expires_at_ms=T0 + 280,
                task_state="idle",
            )
        )
        == "accepted_stop"
    )
    assert receiver.snapshot()["software_estop_latch"] is True
    assert receiver.snapshot()["armed"] is False
    rearm(receiver, sequence=5, at=T0 + 40)
    assert receiver.snapshot()["software_estop_latch"] is False
    assert receiver.snapshot()["armed"] is True
