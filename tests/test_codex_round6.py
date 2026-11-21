"""Regressions for the persisted-state rollback review of d989758.

The persist hook used here records snapshots in memory. It is a check of the
write-before-use ordering in docs/contracts.md. It is not durable storage, and
nothing here tests a file system, an atomic rename, or a flush.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.control_auth import TEST_KEY
from tests.status_gate import (
    PersistedStateError,
    PersistFailed,
    StatusGate,
    dump_state,
)
from tests.test_codex_regressions import arm_moving, packet, ready

CONTRACT = Path(__file__).resolve().parent.parent / "docs" / "contracts.md"


def _authorized(gate, mono, status):
    sequence = status["last_sequence"] + 1
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


def _live(persist=None):
    """A gate at challenge mark 44 and status sequence {42: 3}.

    Returns the receiver, the gate, the snapshot taken at mark 42 / {42: 1},
    a delayed status echoing 42 (sequence 2), and the current status (44, 3).
    """
    receiver = ready()
    arm_moving(receiver, challenge=40)
    gate = StatusGate(TEST_KEY, receiver.controller_id, persist=persist)
    assert gate.expect(42, 0) is True
    first = receiver.status()
    delayed = receiver.status()
    assert first["status_challenge"] == delayed["status_challenge"] == 42
    assert gate.observe(first, 0) == "accepted"
    older = gate.export_state()
    assert gate.expect(44, 10) is True
    assert receiver.handle(
        packet(receiver, sequence=4, task_state="hold", status_challenge=44)
    ) == "accepted_stop"
    current = receiver.status()
    assert current["status_sequence"] == 3 and current["armed"] is True
    assert gate.observe(current, 10) == "accepted"
    assert gate.challenge_high_water == 44
    assert gate.sequences == {42: 3}
    return receiver, gate, older, delayed, current


def _assert_locked_with_marks(gate, high_water=44, sequences=None):
    assert gate.locked is True
    assert gate.challenge_high_water == high_water
    assert gate.sequences == ({42: 3} if sequences is None else sequences)
    assert gate.challenge is None and gate.challenge_mono is None
    assert gate.latest is None and gate.received_mono is None


# 1 and 2. An older valid snapshot cannot lower either mark.


def test_older_snapshot_cannot_lower_challenge_44_to_42():
    receiver, gate, older, delayed, current = _live()
    assert older["challenge_high_water"] == 42
    with pytest.raises(PersistedStateError):
        gate.restore_json(dump_state(older))
    _assert_locked_with_marks(gate)
    with pytest.raises(PersistedStateError):
        gate.issue_challenge(20)
    assert gate.challenge_high_water == 44


def test_older_snapshot_cannot_lower_status_sequence_3_to_1():
    receiver, gate, older, delayed, current = _live()
    stale = {**gate.export_state(), "sequences": [[42, 1]]}
    assert stale["challenge_high_water"] == 44
    with pytest.raises(PersistedStateError):
        gate.restore_state(json.loads(dump_state(stale)))
    _assert_locked_with_marks(gate)


# 3. Delayed signed statuses do not authorize after a rejected restore.


def test_delayed_status_cannot_authorize_after_rejected_restore():
    receiver, gate, older, delayed, current = _live()
    with pytest.raises(PersistedStateError):
        gate.restore_state(older)
    for status in (delayed, current):
        for mono in (0, 10, 20, 10_000):
            assert gate.observe(status, mono) == "locked"
            assert _authorized(gate, mono, status) == (False, False)
    assert gate.expect(42, 20) is False
    assert gate.expect(45, 20) is False
    _assert_locked_with_marks(gate)


# 4. A stale snapshot cannot unlock a gate locked by malformed JSON.


def test_stale_snapshot_cannot_unlock_a_gate_locked_by_malformed_json():
    receiver, gate, older, delayed, current = _live()
    with pytest.raises(PersistedStateError):
        gate.restore_json('{"version":1,')
    _assert_locked_with_marks(gate)
    with pytest.raises(PersistedStateError):
        gate.restore_json(dump_state(older))
    _assert_locked_with_marks(gate)
    assert gate.observe(delayed, 20) == "locked"


# 5. A current snapshot recovers, but only through a new challenge exchange.


def test_current_snapshot_recovers_only_through_a_new_challenge():
    receiver, gate, older, delayed, current = _live()
    saved = dump_state(gate.export_state())
    with pytest.raises(PersistedStateError):
        gate.restore_state(older)
    gate.restore_json(saved)
    assert gate.locked is False
    assert gate.challenge_high_water == 44 and gate.sequences == {42: 3}
    assert gate.challenge is None and gate.latest is None
    for status in (delayed, current):
        assert gate.observe(status, 20) == "stale_challenge"
        assert _authorized(gate, 20, status) == (False, False)
    assert gate.expect(44, 20) is False

    challenge = gate.issue_challenge(20)
    assert challenge == 45
    assert receiver.handle(
        packet(receiver, sequence=5, task_state="hold", status_challenge=challenge)
    ) == "accepted_stop"
    fresh = receiver.status()
    assert fresh["status_challenge"] == 45
    assert gate.observe(fresh, 25) == "accepted"
    assert _authorized(gate, 25, fresh) == (True, True)


# 6. Omitting a known boot record fails closed.


def _two_boot_gate():
    gate = StatusGate(TEST_KEY, "laptop-a")
    gate.restore_state(
        {
            "version": 1,
            "controller_id": "laptop-a",
            "challenge_high_water": 44,
            "boot_nonce": 42,
            "sequences": [[42, 3], [99, 5]],
        }
    )
    return gate


@pytest.mark.parametrize(
    "label,sequences,boot",
    [
        ("omit old boot", [[42, 3]], 42),
        ("omit current boot", [[99, 5], [7, 1]], 7),
        ("omit all boots", [], None),
        ("return to an earlier boot", [[42, 3], [99, 5]], 99),
    ],
)
def test_snapshot_omitting_or_rewinding_a_known_boot_fails_closed(label, sequences, boot):
    gate = _two_boot_gate()
    with pytest.raises(PersistedStateError):
        gate.restore_state(
            {
                "version": 1,
                "controller_id": "laptop-a",
                "challenge_high_water": 44,
                "boot_nonce": boot,
                "sequences": sequences,
            }
        )
    _assert_locked_with_marks(gate, sequences={42: 3, 99: 5})


def test_snapshot_with_a_new_boot_and_equal_marks_is_accepted():
    gate = _two_boot_gate()
    gate.restore_state(
        {
            "version": 1,
            "controller_id": "laptop-a",
            "challenge_high_water": 50,
            "boot_nonce": 7,
            "sequences": [[7, 1], [42, 3], [99, 6]],
        }
    )
    assert gate.locked is False
    assert gate.sequences == {7: 1, 42: 3, 99: 6}
    assert gate.challenge_high_water == 50


# 7. Export -> JSON -> restore still works for current state.


def test_current_state_round_trips_through_json():
    receiver, gate, older, delayed, current = _live()
    text = dump_state(gate.export_state())
    gate.restore_json(text)
    assert gate.locked is False
    assert gate.sequences == {42: 3}
    restarted = StatusGate(TEST_KEY, receiver.controller_id)
    restarted.restore_json(text)
    assert restarted.export_state() == gate.export_state()
    assert all(type(boot) is int for boot in restarted.sequences)
    assert restarted.observe(current, 20) == "stale_challenge"


# Write-before-use ordering (specification level, in-memory hook only).


class Recorder:
    def __init__(self, gate_ref):
        self.gate_ref = gate_ref
        self.snapshots = []
        self.during_write = []
        self.fail = False

    def __call__(self, state):
        gate = self.gate_ref[0]
        if gate is not None:
            # What could be used while the write was still pending.
            self.during_write.append((gate.challenge, gate.latest))
        if self.fail:
            raise OSError("simulated write failure")
        self.snapshots.append(json.loads(dump_state(state)))


def test_challenge_is_persisted_before_it_can_be_transmitted():
    ref = [None]
    recorder = Recorder(ref)
    gate = StatusGate(TEST_KEY, "laptop-a", persist=recorder)
    ref[0] = gate
    transmitted = [gate.issue_challenge(0), gate.issue_challenge(1)]
    assert transmitted == [1, 2]
    assert [s["challenge_high_water"] for s in recorder.snapshots] == [1, 2]
    assert recorder.during_write == [(None, None), (None, None)]

    recorder.fail = True
    with pytest.raises(PersistFailed):
        gate.issue_challenge(2)
    assert gate.challenge is None
    assert gate.challenge_high_water == 3
    recorder.fail = False
    assert gate.issue_challenge(3) == 4
    transmitted.append(4)

    # A crash after the last durable write. The restarted gate starts from
    # that snapshot and never reissues an id that was handed out.
    restarted = StatusGate(TEST_KEY, "laptop-a")
    restarted.restore_state(recorder.snapshots[-1])
    assert restarted.issue_challenge(0) > max(transmitted)


def test_status_sequence_is_persisted_before_it_can_authorize():
    ref = [None]
    recorder = Recorder(ref)
    receiver, gate, older, delayed, current = _live(persist=recorder)
    ref[0] = gate
    challenge = gate.issue_challenge(20)
    assert receiver.handle(
        packet(receiver, sequence=5, task_state="hold", status_challenge=challenge)
    ) == "accepted_stop"
    fresh = receiver.status()

    recorder.fail = True
    assert gate.observe(fresh, 25) == "persist_failed"
    assert gate.latest is None
    assert _authorized(gate, 25, fresh) == (False, False)
    assert gate.sequences[42] == fresh["status_sequence"]
    recorder.fail = False
    assert gate.observe(fresh, 25) == "stale_or_reordered"

    challenge = gate.issue_challenge(30)
    assert receiver.handle(
        packet(receiver, sequence=6, task_state="hold", status_challenge=challenge)
    ) == "accepted_stop"
    newest = receiver.status()
    recorder.during_write.clear()
    assert gate.observe(newest, 35) == "accepted"
    assert recorder.during_write == [(challenge, None)]
    assert recorder.snapshots[-1]["sequences"] == [[42, newest["status_sequence"]]]
    assert _authorized(gate, 35, newest) == (True, True)


def test_contract_documents_the_split_and_the_ordering():
    text = CONTRACT.read_text()
    assert "#### Persisted status-gate state" in text
    assert "#### Persisted sender state" not in text
    for phrase in (
        "before the challenge is transmitted",
        "before that status can authorize",
        "atomic replacement",
        "independent trusted history",
        "command-session allocator",
        "not implemented or tested",
    ):
        assert phrase in text, phrase
