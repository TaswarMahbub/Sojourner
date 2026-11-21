"""Regressions for the Codex FAIL on 1c88de7: persisted publisher state.

These checks drive the publisher status gate with mocked statuses. They do not
power a motor and do not verify a laptop's real storage or clock.
"""

from __future__ import annotations

import json

import pytest

from tests.contract_schema import SAFE_INT_MAX
from tests.control_auth import TEST_KEY, sign_status
from tests.status_gate import (
    ChallengeExhausted,
    PersistedStateError,
    StatusGate,
    dump_state,
    load_state,
)
from tests.test_codex_regressions import arm_moving, packet, ready


def _authorized(gate, mono, status, sequence=None):
    sequence = status["last_sequence"] + 1 if sequence is None else sequence
    motion = gate.motion_authorized(mono, status["session_id"], sequence, status["freshness_id"])
    rearm = gate.rearm_authorized(
        mono,
        status["session_id"],
        sequence,
        status["freshness_id"],
        status["rearm_generation"],
        operator_authorized=True,
    )
    return motion, rearm


def _armed_gate(at=10_000):
    """A gate that accepted an armed status echoing challenge 43 at `at`."""
    receiver = ready()
    arm_moving(receiver, challenge=41)
    first = receiver.status()
    delayed = receiver.status()
    assert first["status_challenge"] == delayed["status_challenge"] == 43
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    assert gate.expect(43, at) is True
    assert gate.observe(first, at) == "accepted"
    assert _authorized(gate, at, first) == (True, True)
    return receiver, gate, first, delayed


def _json_round_trip(gate):
    text = dump_state(gate.export_state())
    restarted = StatusGate(TEST_KEY, gate.controller_id)
    restarted.restore_state(json.loads(text))
    return restarted, text


def _valid_state():
    return {
        "version": 1,
        "controller_id": "laptop-a",
        "challenge_high_water": 43,
        "boot_nonce": 42,
        "sequences": [[42, 2]],
    }


# A. Old signed response after a clock-resetting sender restart.


def test_old_response_after_clock_resetting_restart_is_not_fresh():
    receiver, gate, first, delayed = _armed_gate(at=10_000)
    restarted, _ = _json_round_trip(gate)
    snap = restarted.export_state()
    assert snap["challenge_high_water"] == 43
    assert restarted.challenge is None
    assert restarted.challenge_mono is None
    assert restarted.latest is None
    assert restarted.received_mono is None

    # The laptop's monotonic clock restarted at 0 and has reached the old
    # issuance time again. Neither old status is fresh.
    for mono in (0, 100, 10_000, 10_100):
        assert restarted.observe(delayed, mono) == "stale_challenge"
        assert restarted.observe(first, mono) == "stale_challenge"
        assert _authorized(restarted, mono, delayed) == (False, False)
    assert restarted.expect(43, 10_000) is False
    assert restarted.observe(delayed, 10_000) == "stale_challenge"
    assert _authorized(restarted, 10_000, delayed) == (False, False)


# B. A previously accepted status replayed after export -> JSON -> restore.


def test_accepted_status_replayed_after_json_round_trip_is_rejected():
    receiver, gate, first, delayed = _armed_gate(at=0)
    restarted, text = _json_round_trip(gate)
    assert '"sequences":[[42,' in text
    assert restarted.sequences == {42: first["status_sequence"]}
    assert all(type(boot) is int for boot in restarted.sequences)

    challenge = restarted.issue_challenge(5)
    assert challenge == 44
    # An authenticated status echoing the new challenge but carrying an old
    # status_sequence must hit the restored replay mark.
    for old in (first, delayed):
        replay = dict(old)
        if old is first:
            replay["status_challenge"] = challenge
            replay["mac"] = sign_status(TEST_KEY, replay)
            assert restarted.observe(replay, 5) == "stale_or_reordered"
        else:
            assert restarted.observe(replay, 5) == "stale_challenge"
        assert _authorized(restarted, 5, replay) == (False, False)


# C. Fresh challenge and status establish authorization after restart.


def test_fresh_challenge_and_status_authorize_after_restart():
    receiver, gate, first, delayed = _armed_gate(at=10_000)
    restarted, _ = _json_round_trip(gate)
    challenge = restarted.issue_challenge(3)
    assert challenge == 44
    assert receiver.handle(
        packet(receiver, sequence=4, task_state="hold", status_challenge=challenge)
    ) == "accepted_stop"
    current = receiver.status()
    assert current["status_challenge"] == challenge
    assert current["armed"] is True
    assert restarted.observe(current, 10) == "accepted"
    assert _authorized(restarted, 10, current) == (True, True)
    assert _authorized(restarted, 3 + 251, current) == (False, False)


# D. Invalid challenge ids, numeric aliases, timestamps, and exhaustion.


@pytest.mark.parametrize(
    "challenge",
    [True, False, 43.0, 44.0, 0, -1, SAFE_INT_MAX + 1, "44", None, 2**64],
)
def test_invalid_challenge_ids_and_aliases_are_refused(challenge):
    receiver, gate, first, delayed = _armed_gate(at=0)
    assert gate.expect(challenge, 0) is False
    assert gate.challenge is None
    assert gate.observe(delayed, 0) == "stale_challenge"
    assert _authorized(gate, 0, first) == (False, False)


def test_bool_alias_of_the_outstanding_challenge_is_not_a_repeat():
    receiver = ready()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    assert gate.expect(1, 0) is True
    assert gate.expect(True, 0) is False
    assert gate.challenge is None


@pytest.mark.parametrize("mono", [True, -1, 1.0, SAFE_INT_MAX + 1, "0", None])
def test_malformed_timestamps_are_refused(mono):
    receiver, gate, first, delayed = _armed_gate(at=0)
    assert gate.expect(44, mono) is False
    assert gate.challenge is None
    assert gate.expect(45, 0) is True
    assert gate.observe(delayed, mono) in {"stale_challenge", "stale"}
    assert _authorized(gate, mono, first) == (False, False)


def test_float_alias_in_a_signed_status_is_not_the_challenge():
    receiver, gate, first, delayed = _armed_gate(at=0)
    assert gate.expect(44, 0) is True
    honest = dict(delayed)
    honest["status_challenge"] = 44
    honest["mac"] = sign_status(TEST_KEY, honest)
    alias = dict(honest)
    alias["status_challenge"] = 44.0
    with pytest.raises(ValueError):
        sign_status(TEST_KEY, alias)
    assert gate.observe(alias, 0) in {"spoofed", "invalid_status", "stale_challenge"}
    assert gate.latest is None
    assert _authorized(gate, 0, alias) == (False, False)


def test_challenge_exhaustion_does_not_wrap_or_overflow():
    receiver = ready()
    gate = StatusGate(TEST_KEY, receiver.controller_id)
    assert gate.expect(SAFE_INT_MAX - 1, 0) is True
    assert gate.issue_challenge(1) == SAFE_INT_MAX
    with pytest.raises(ChallengeExhausted):
        gate.issue_challenge(2)
    assert gate.challenge is None
    assert gate.challenge_high_water == SAFE_INT_MAX
    assert gate.expect(1, 3) is False
    assert gate.expect(SAFE_INT_MAX, 3) is False
    assert gate.challenge is None

    saved = StatusGate(TEST_KEY, receiver.controller_id)
    saved.restore_json(dump_state(gate.export_state()))
    with pytest.raises(ChallengeExhausted):
        saved.issue_challenge(0)


# E. Malformed persisted state fails closed.


def _mutations():
    yield "missing", None
    yield "not an object", []
    yield "extra key", {**_valid_state(), "challenge": 44}
    yield "missing key", {k: v for k, v in _valid_state().items() if k != "sequences"}
    yield "old schema", {
        "sequences": {"42": 2},
        "boot_nonce": 42,
        "challenge": 43,
        "challenge_mono": 10_000,
        "challenge_high_water": 43,
    }
    yield "wrong version", {**_valid_state(), "version": 2}
    yield "bool version", {**_valid_state(), "version": True}
    yield "other controller", {**_valid_state(), "controller_id": "laptop-b"}
    yield "string key map", {**_valid_state(), "sequences": {"42": 2}}
    yield "string boot", {**_valid_state(), "sequences": [["42", 2]]}
    yield "float boot", {**_valid_state(), "sequences": [[42.0, 2]]}
    yield "bool boot", {**_valid_state(), "boot_nonce": True, "sequences": [[True, 2]]}
    yield "zero boot", {**_valid_state(), "boot_nonce": 0, "sequences": [[0, 2]]}
    yield "zero sequence", {**_valid_state(), "sequences": [[42, 0]]}
    yield "negative sequence", {**_valid_state(), "sequences": [[42, -5]]}
    yield "float sequence", {**_valid_state(), "sequences": [[42, 2.0]]}
    yield "oversized sequence", {**_valid_state(), "sequences": [[42, SAFE_INT_MAX + 1]]}
    yield "duplicate boot", {**_valid_state(), "sequences": [[42, 2], [42, 1]]}
    yield "triple entry", {**_valid_state(), "sequences": [[42, 2, 3]]}
    yield "orphan boot", {**_valid_state(), "boot_nonce": 7}
    yield "sequences without boot", {**_valid_state(), "boot_nonce": None}
    yield "negative high water", {**_valid_state(), "challenge_high_water": -1}
    yield "float high water", {**_valid_state(), "challenge_high_water": 43.0}
    yield "oversized high water", {**_valid_state(), "challenge_high_water": SAFE_INT_MAX + 1}


@pytest.mark.parametrize("label,state", list(_mutations()), ids=[m[0] for m in _mutations()])
def test_malformed_persisted_state_locks_the_gate(label, state):
    receiver, gate, first, delayed = _armed_gate(at=0)
    gate.controller_id = "laptop-a"
    gate.sequences = {42: 9}
    with pytest.raises(PersistedStateError):
        gate.restore_state(state)
    assert gate.locked is True
    assert gate.challenge is None and gate.latest is None
    assert gate.sequences == {42: 9}
    assert gate.expect(10**6, 0) is False
    with pytest.raises(PersistedStateError):
        gate.issue_challenge(0)
    with pytest.raises(PersistedStateError):
        gate.export_state()
    assert gate.observe(delayed, 0) == "locked"
    assert _authorized(gate, 0, first) == (False, False)

    gate.restore_state(_valid_state())
    assert gate.locked is False
    assert gate.sequences == {42: 2}


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "{",
        '{"version":1,"version":1,"controller_id":"laptop-a","challenge_high_water":1,'
        '"boot_nonce":null,"sequences":[]}',
        '{"version":1,"controller_id":"laptop-a","challenge_high_water":NaN,'
        '"boot_nonce":null,"sequences":[]}',
    ],
)
def test_corrupted_json_locks_the_gate(text):
    gate = StatusGate(TEST_KEY, "laptop-a")
    with pytest.raises(PersistedStateError):
        gate.restore_json(text)
    assert gate.locked is True
    assert gate.expect(1, 0) is False


def test_valid_state_survives_canonical_json():
    state = _valid_state()
    assert load_state(dump_state(state)) == state


# F. A -> B -> A prevention remains effective, including across JSON restart.


def test_a_b_a_is_still_refused_before_and_after_json_restart():
    receiver, gate, first, delayed = _armed_gate(at=0)
    assert gate.expect(44, 200) is True
    assert gate.expect(43, 300) is False
    assert gate.observe(delayed, 300) == "stale_challenge"
    assert _authorized(gate, 300, delayed) == (False, False)

    restarted, _ = _json_round_trip(gate)
    assert restarted.expect(43, 300) is False
    assert restarted.expect(44, 300) is False
    assert restarted.observe(delayed, 300) == "stale_challenge"
    assert _authorized(restarted, 300, delayed) == (False, False)
    assert restarted.issue_challenge(300) == 45


# G. Software-estop and watchdog-fault recovery still work after a restart.


def _cycle(gate, receiver, mono, sequence, task_state):
    """Issue a challenge, send one accepted datagram with it, observe the echo."""
    challenge = gate.issue_challenge(mono)
    outcome = receiver.handle(
        packet(receiver, sequence=sequence, task_state=task_state, status_challenge=challenge)
    )
    status = receiver.status()
    assert status["status_challenge"] == challenge
    assert gate.observe(status, mono) == "accepted"
    return outcome, status


def test_software_estop_recovers_through_a_restarted_gate():
    receiver, gate, first, delayed = _armed_gate(at=0)
    restarted, _ = _json_round_trip(gate)
    outcome, status = _cycle(restarted, receiver, 1, 4, "estop")
    assert outcome == "accepted_stop"
    assert status["software_estop_latch"] is True and status["armed"] is False
    assert _authorized(restarted, 1, status) == (False, True)
    outcome, status = _cycle(restarted, receiver, 2, 5, "rearm")
    assert outcome == "accepted_rearm"
    assert status["armed"] is True and status["software_estop_latch"] is False
    assert _authorized(restarted, 2, status) == (True, True)


def test_watchdog_fault_recovers_through_a_restarted_gate():
    receiver, gate, first, delayed = _armed_gate(at=0)
    receiver.set_monotonic(251)
    assert receiver.snapshot()["fault_latch"] is True
    restarted, _ = _json_round_trip(gate)
    outcome, status = _cycle(restarted, receiver, 1, 4, "idle")
    assert outcome == "accepted_stop"
    assert status["fault_latch"] is True and status["armed"] is False
    assert _authorized(restarted, 1, status) == (False, True)
    outcome, status = _cycle(restarted, receiver, 2, 5, "rearm")
    assert outcome == "accepted_rearm"
    assert status["armed"] is True and status["fault_latch"] is False
    assert _authorized(restarted, 2, status) == (True, True)
