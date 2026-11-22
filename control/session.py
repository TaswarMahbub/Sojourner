"""Sender session sequence on UDP.

docs/contracts.md section 3, "Sessions on UDP". The sender:

1. chooses a new status_challenge and sends a session start only after a
   status whose MAC verifies for the provisioned controller;
2. copies boot_nonce, freshness_id and rearm_generation from that status;
3. chooses a session_id strictly greater than every id it has used;
4. sends sequence 1, "idle", stop true, both velocities 0;
5. trusts the session only after a later status echoes that challenge, names
   the same boot and session, and passes the status gate;
6. sends rearm under the conditions in control/publisher.py;
7. sends non-zero motion only when the status gate authorizes it.

Steps 1-5 and the sequence rules are here. Steps 6 and 7 are decided in
control/publisher.py.

The session_id high-water mark is its own persisted record, separate from the
status-gate object ("Persisted status-gate state"). It is written durably
before an id is returned, so an id is never transmitted before its mark is
on disk.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from control import durable
from control.auth import verify_status
from tests.contract_schema import SAFE_INT_MAX, SEQUENCE_MAX, validate_status

LOG = logging.getLogger("control.session")

SESSION_STATE_VERSION = 1
SESSION_STATE_KEYS = frozenset({"version", "controller_id", "session_high_water"})


class SessionStateError(ValueError):
    """The persisted session record is missing, malformed, or rolls back."""


class SessionLocked(RuntimeError):
    """The allocator is locked by an invalid session record."""


def _exact_int(value, low: int, high: int = SAFE_INT_MAX) -> bool:
    return type(value) is int and low <= value <= high


def _object_without_duplicates(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise SessionStateError(f"duplicate key {name!r}")
        out[name] = value
    return out


def _refuse_constant(name):
    raise SessionStateError(f"non-finite number {name}")


def fresh_session_state(controller_id: str) -> dict:
    """The empty session record for a new controller identity. Used by control/reset.py."""
    return {"version": SESSION_STATE_VERSION, "controller_id": controller_id, "session_high_water": 0}


class SessionAllocator:
    """Persisted, strictly increasing session_id source (section 3, step 3).

    A new allocator is locked until restore_from_file() succeeds. A missing,
    corrupt or rolled-back record keeps it locked. It does not fall back to
    an empty mark, because reusing a session id is what step 3 forbids.
    """

    def __init__(self, controller_id: str, state_path):
        self.controller_id = controller_id
        self.state_path = Path(state_path)
        self.locked = True
        self.session_high_water = 0

    def restore_from_file(self) -> None:
        text = durable.read_text_or_none(self.state_path)
        try:
            if text is None:
                raise SessionStateError("session record is missing")
            try:
                state = json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_refuse_constant)
            except SessionStateError:
                raise
            except (ValueError, RecursionError) as exc:
                raise SessionStateError("session record is not valid JSON") from exc
            if not isinstance(state, dict) or set(state) != SESSION_STATE_KEYS:
                raise SessionStateError("session record has missing or extra keys")
            if type(state["version"]) is not int or state["version"] != SESSION_STATE_VERSION:
                raise SessionStateError("session record version is not 1")
            if state["controller_id"] != self.controller_id:
                raise SessionStateError("session record belongs to another controller")
            mark = state["session_high_water"]
            if not _exact_int(mark, 0):
                raise SessionStateError("session_high_water is not an exact integer in range")
            if mark < self.session_high_water:
                raise SessionStateError("session record lowers session_high_water")
        except SessionStateError as exc:
            self.locked = True
            LOG.error("session allocator locked: %s", exc)
            raise
        self.session_high_water = mark
        self.locked = False

    def next_session_id(self, floor: int = 0) -> int:
        """Return a session_id strictly greater than every id used before.

        `floor` (for example the current Unix milliseconds, as in the
        section 3 illustration) only ever raises the id. The new mark is
        written durably before the id is returned. On a failed write the
        in-memory mark stays raised and SessionLocked is raised.
        """
        if self.locked:
            raise SessionLocked("session allocator is locked")
        if not _exact_int(floor, 0):
            raise ValueError("floor is not an exact non-negative integer")
        if self.session_high_water >= SAFE_INT_MAX:
            raise SessionLocked("session ids are exhausted")
        candidate = max(self.session_high_water + 1, min(floor, SAFE_INT_MAX))
        self.session_high_water = candidate
        try:
            durable.atomic_write_json(
                self.state_path,
                {"version": SESSION_STATE_VERSION, "controller_id": self.controller_id, "session_high_water": candidate},
            )
        except (OSError, ValueError) as exc:
            LOG.error("session record durable write failed: %r", exc)
            raise SessionLocked("session_id mark was not written durably") from exc
        return candidate


class VerifiedStatusTracker:
    """Newest status whose MAC verifies, for addressing only.

    Section 3, "Sessions on UDP", step 1: a session start needs only a status
    whose MAC verifies for the provisioned controller. Stop datagrams copy
    freshness_id and rearm_generation "from the newest motor status"
    (section 3, Fields). This tracker never authorizes motion or rearm; only
    control/status_gate.py does that.

    It rejects a status that fails the MAC, the schema, or the controller
    check, one whose status_sequence is not above the highest seen for that
    boot (in memory, and never below the status gate's persisted marks), and
    an armed status from a boot other than the newest one, since a new boot
    starts disarmed.
    """

    def __init__(self, key: bytes, controller_id: str):
        self._key = key
        self.controller_id = controller_id
        self._seen: dict[int, int] = {}
        self.status: dict | None = None
        self.received_mono: int | None = None

    def observe(self, status, now_mono: int, gate_marks_before: dict[int, int]) -> bool:
        """Record `status` if it is authentic and newer. Returns True when recorded.

        `gate_marks_before` are the status gate's per-boot marks as they were
        before the gate saw this same status, so the persisted marks act as a
        floor without rejecting the status the gate has just accepted.
        """
        if not isinstance(status, dict) or not verify_status(self._key, status):
            return False
        if validate_status(status) or status["controller_id"] != self.controller_id:
            return False
        boot = status["boot_nonce"]
        seq = status["status_sequence"]
        floor = max(self._seen.get(boot, 0), gate_marks_before.get(boot, 0))
        if seq <= floor:
            return False
        if self.status is not None and boot != self.status["boot_nonce"] and status["armed"] is True:
            return False
        self._seen[boot] = max(seq, self._seen.get(boot, 0))
        self.status = dict(status)
        self.received_mono = now_mono
        return True

    def recent(self, now_mono: int, max_age_ms: int) -> dict | None:
        if self.status is None or self.received_mono is None:
            return None
        if now_mono < self.received_mono or now_mono - self.received_mono > max_age_ms:
            return None
        return dict(self.status)


@dataclass
class Session:
    """One sender session (section 3, "Sessions on UDP")."""

    boot_nonce: int
    session_id: int
    start_challenge: int
    started_mono: int
    next_sequence: int = 2
    trusted: bool = False

    def take_sequence(self) -> int | None:
        """Next sequence for this session, or None once 4294967295 is used.

        Section 3: wraparound is not allowed. After 4294967295 the sender
        starts a greater session_id.
        """
        if self.next_sequence > SEQUENCE_MAX:
            return None
        value = self.next_sequence
        self.next_sequence += 1
        return value

    def note_gate_status(self, status: dict, gate_result: str) -> bool:
        """Step 5: trust only after a gate-accepted status echoes the start challenge."""
        if self.trusted:
            return True
        if (
            gate_result == "accepted"
            and status.get("status_challenge") == self.start_challenge
            and status.get("boot_nonce") == self.boot_nonce
            and status.get("session_id") == self.session_id
        ):
            self.trusted = True
        return self.trusted


def session_start_fields(status: dict, session_id: int, challenge: int) -> dict:
    """Step 4 field values, copying boot, freshness and generation (step 2)."""
    return {
        "boot_nonce": status["boot_nonce"],
        "session_id": session_id,
        "sequence": 1,
        "freshness_id": status["freshness_id"],
        "rearm_generation": status["rearm_generation"],
        "status_challenge": challenge,
        "task_state": "idle",
        "linear_mps": 0.0,
        "angular_rad_s": 0.0,
        "stop": True,
    }
