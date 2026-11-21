"""Publisher-side status gate.

A status dict is not authorization. The gate accepts a status only when its
MAC matches the provisioned controller, it echoes the challenge the sender
just issued, its sequence is newer for that boot, and the sender's monotonic
receipt is inside 250 ms of that challenge. Cached, spoofed, reordered,
wrong-boot, and stale status do not authorize motion.

Challenge ids are strictly increasing for the life of the sender, across
restarts. An id at or below the high-water mark is retired and never becomes
fresh again, so A -> B -> A cannot reset A's issuance time.

Persisted state holds only durable high-water marks. A restart never restores
an outstanding challenge, its monotonic issuance time, or an accepted status,
because the sender's monotonic epoch may have reset. A restarted gate needs a
newly issued challenge and a new authenticated status before it authorizes
anything. Missing, corrupted, or inconsistent persisted state locks the gate,
and so does a snapshot older than the marks the gate already holds.

The optional `persist` callback is a specification hook, not durable storage.
The gate calls it with the new state before a new challenge becomes
outstanding and before a newly accepted status can authorize anything. If it
raises, the challenge is not issued and the status does not authorize. No real
file-I/O layer exists in this repository.
"""

from __future__ import annotations

import json

from tests.contract_schema import SAFE_INT_MAX, validate_status
from tests.control_auth import verify_status

FRESH_MS = 250
STATE_VERSION = 1
STATE_KEYS = frozenset(
    {"version", "controller_id", "challenge_high_water", "boot_nonce", "sequences"}
)


class PersistedStateError(ValueError):
    """Saved gate state is missing, malformed, ambiguous, or inconsistent."""


class PersistFailed(RuntimeError):
    """The persist hook did not confirm a durable write."""


class ChallengeExhausted(RuntimeError):
    """No unused challenge id remains at or below the safe-integer maximum."""


def _exact_int(value, low: int, high: int = SAFE_INT_MAX) -> bool:
    return type(value) is int and low <= value <= high


def _reject_duplicate_keys(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise PersistedStateError(f"duplicate key {name!r}")
        out[name] = value
    return out


def dump_state(state: dict) -> str:
    """Serialize exported gate state as canonical JSON."""
    return json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)


def load_state(text) -> dict:
    """Parse saved gate state, rejecting duplicate keys and non-finite numbers."""
    if not isinstance(text, (str, bytes, bytearray)):
        raise PersistedStateError("saved state is missing")

    def _no_constant(name):
        raise PersistedStateError(f"non-finite number {name}")

    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_no_constant
        )
    except PersistedStateError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PersistedStateError("saved state is not valid JSON") from exc


class StatusGate:
    def __init__(self, key: bytes, controller_id: str, *, persist=None):
        self.key = key
        self.persist = persist
        self.controller_id = controller_id
        self.sequences: dict[int, int] = {}
        self.boot_nonce = None
        self.latest = None
        self.received_mono = None
        self.challenge = None
        self.challenge_mono = None
        self.challenge_high_water = 0
        self.locked = False

    def issue_challenge(self, sent_mono: int) -> int:
        """Allocate the next unused challenge id and make it outstanding.

        Raises ChallengeExhausted, with no challenge outstanding, when the next
        id would pass the safe-integer maximum. Ids never wrap.
        """
        if self.locked:
            self._drop_challenge()
            raise PersistedStateError("gate is locked by invalid persisted state")
        if self.challenge_high_water >= SAFE_INT_MAX:
            self._drop_challenge()
            raise ChallengeExhausted("challenge ids are exhausted")
        challenge = self.challenge_high_water + 1
        if not self.expect(challenge, sent_mono):
            if self.challenge_high_water == challenge:
                raise PersistFailed("challenge high-water mark was not persisted")
            raise ValueError("sent_mono is invalid")
        return challenge

    def expect(self, challenge: int, sent_mono: int) -> bool:
        """Record the challenge the sender has outstanding.

        The id must be an exact integer from 1 to 2^53 - 1, and sent_mono an
        exact integer from 0 to 2^53 - 1. Bool and float aliases are refused.
        Repeating the outstanding challenge does not move its issuance time
        forward. A different challenge must be greater than every id used
        before. It invalidates authorization from the previous status. A
        retired or invalid id is refused, and the gate then has no outstanding
        challenge, so nothing is authorized until a new id is issued.
        """
        if self.locked or not _exact_int(challenge, 1) or not _exact_int(sent_mono, 0):
            self._drop_challenge()
            return False
        if challenge == self.challenge:
            return True
        self._drop_challenge()
        if challenge <= self.challenge_high_water:
            return False
        # The mark rises in memory even if the write fails, so the id is
        # never offered again. It is outstanding only after a durable write.
        self.challenge_high_water = challenge
        if not self._persist():
            return False
        self.challenge = challenge
        self.challenge_mono = sent_mono
        return True

    def export_state(self) -> dict:
        """Durable high-water marks only. JSON-safe: no integer dict keys."""
        if self.locked:
            raise PersistedStateError("gate is locked by invalid persisted state")
        return self._snapshot()

    def _snapshot(self) -> dict:
        return {
            "version": STATE_VERSION,
            "controller_id": self.controller_id,
            "challenge_high_water": self.challenge_high_water,
            "boot_nonce": self.boot_nonce,
            "sequences": [[boot, seq] for boot, seq in sorted(self.sequences.items())],
        }

    def restore_state(self, state) -> None:
        """Restore high-water marks after a sender restart.

        Never restores an outstanding challenge or an accepted status. The
        whole snapshot is validated before anything is applied. A snapshot
        that would lower any mark the gate already holds, including one that
        omits a known boot, is rejected. On any rejection the gate is locked,
        keeps its existing marks, and raises PersistedStateError. A locked
        gate refuses challenges and authorizes nothing. The same check applies
        when a locked gate is recovered.
        """
        self.latest = None
        self.received_mono = None
        self._drop_challenge()
        try:
            sequences, boot_nonce, high_water = self._validate_state(state)
            self._reject_rollback(sequences, boot_nonce, high_water)
        except PersistedStateError:
            self.locked = True
            raise
        self.sequences = sequences
        self.boot_nonce = boot_nonce
        self.challenge_high_water = high_water
        self.locked = False

    def restore_json(self, text) -> None:
        try:
            state = load_state(text)
        except PersistedStateError:
            self.locked = True
            self.latest = None
            self.received_mono = None
            self._drop_challenge()
            raise
        self.restore_state(state)

    def _validate_state(self, state):
        if not isinstance(state, dict):
            raise PersistedStateError("saved state is missing or not an object")
        if set(state) != STATE_KEYS:
            raise PersistedStateError("saved state has missing or unknown keys")
        if type(state["version"]) is not int or state["version"] != STATE_VERSION:
            raise PersistedStateError("unsupported saved state version")
        if state["controller_id"] != self.controller_id:
            raise PersistedStateError("saved state belongs to another controller")
        high_water = state["challenge_high_water"]
        if not _exact_int(high_water, 0):
            raise PersistedStateError("challenge_high_water is invalid")
        entries = state["sequences"]
        if not isinstance(entries, list):
            raise PersistedStateError("sequences is not a list")
        sequences: dict[int, int] = {}
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise PersistedStateError("sequence entry is not a [boot, sequence] pair")
            boot, seq = entry
            if not _exact_int(boot, 1):
                raise PersistedStateError("boot nonce is invalid")
            if not _exact_int(seq, 1):
                raise PersistedStateError("status sequence is invalid")
            if boot in sequences:
                raise PersistedStateError("duplicate boot nonce")
            sequences[boot] = seq
        boot_nonce = state["boot_nonce"]
        if boot_nonce is None:
            if sequences:
                raise PersistedStateError("sequences recorded without a current boot")
        elif not _exact_int(boot_nonce, 1) or boot_nonce not in sequences:
            raise PersistedStateError("boot_nonce is invalid or has no sequence")
        return sequences, boot_nonce, high_water

    def _reject_rollback(self, sequences, boot_nonce, high_water) -> None:
        if high_water < self.challenge_high_water:
            raise PersistedStateError("snapshot lowers challenge_high_water")
        for boot, known in self.sequences.items():
            if boot not in sequences:
                raise PersistedStateError("snapshot omits a known boot")
            if sequences[boot] < known:
                raise PersistedStateError("snapshot lowers a status sequence")
        if (
            self.boot_nonce is not None
            and boot_nonce != self.boot_nonce
            and boot_nonce in self.sequences
        ):
            raise PersistedStateError("snapshot returns to an earlier boot")

    def _persist(self) -> bool:
        if self.persist is None:
            return True
        try:
            self.persist(self._snapshot())
        except Exception:
            return False
        return True

    def _drop_challenge(self) -> None:
        self.challenge = None
        self.challenge_mono = None
        self.latest = None
        self.received_mono = None

    def observe(self, status, sender_mono: int) -> str:
        if self.locked:
            return "locked"
        if not isinstance(status, dict) or not verify_status(self.key, status):
            return "spoofed"
        if validate_status(status):
            return "invalid_status"
        if status.get("controller_id") != self.controller_id:
            return "wrong_controller"
        echoed = status.get("status_challenge")
        if (
            not _exact_int(self.challenge, 1)
            or not _exact_int(echoed, 1)
            or echoed != self.challenge
        ):
            return "stale_challenge"
        if (
            not _exact_int(sender_mono, 0)
            or not _exact_int(self.challenge_mono, 0)
            or sender_mono < self.challenge_mono
            or sender_mono - self.challenge_mono > FRESH_MS
        ):
            return "stale"
        boot = status.get("boot_nonce")
        seq = status.get("status_sequence")
        if not _exact_int(boot, 1):
            return "spoofed"
        if not _exact_int(seq, 1):
            return "stale_or_reordered"
        if seq <= self.sequences.get(boot, 0):
            return "stale_or_reordered"
        if self.boot_nonce is not None and boot != self.boot_nonce:
            if status.get("armed") is True:
                return "wrong_boot"
        self.sequences[boot] = seq
        self.boot_nonce = boot
        if not self._persist():
            return "persist_failed"
        self.latest = status
        self.received_mono = sender_mono
        return "accepted"

    def _current(self, sender_mono) -> bool:
        if self.locked or self.latest is None or self.received_mono is None:
            return False
        if not _exact_int(self.challenge_mono, 0) or not _exact_int(sender_mono, 0):
            return False
        if sender_mono < self.received_mono or sender_mono < self.challenge_mono:
            return False
        if sender_mono - self.received_mono > FRESH_MS:
            return False
        if sender_mono - self.challenge_mono > FRESH_MS:
            return False
        echoed = self.latest.get("status_challenge")
        return _exact_int(echoed, 1) and _exact_int(self.challenge, 1) and echoed == self.challenge

    def motion_authorized(self, sender_mono, session_id, sequence, freshness_id) -> bool:
        if not self._current(sender_mono):
            return False
        status = self.latest
        last = status.get("last_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            return False
        if isinstance(last, bool) or not isinstance(last, int) or sequence <= last:
            return False
        return (
            status.get("armed") is True
            and status.get("cutoff_open") is False
            and status.get("sensor_state") == "released"
            and status.get("physical_estop") is False
            and status.get("software_estop_latch") is False
            and status.get("fault_latch") is False
            and status.get("session_id") == session_id
            and status.get("freshness_id") == freshness_id
        )

    def rearm_authorized(
        self,
        sender_mono,
        session_id,
        sequence,
        freshness_id,
        rearm_generation,
        *,
        operator_authorized: bool,
    ) -> bool:
        """Allow the command that clears software latches.

        The software estop latch and the watchdog fault latch may be set.
        The physical stop must be released, and the operator must have asked.
        """
        if operator_authorized is not True or not self._current(sender_mono):
            return False
        status = self.latest
        last = status.get("last_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            return False
        if isinstance(last, bool) or not isinstance(last, int) or sequence <= last:
            return False
        if isinstance(rearm_generation, bool) or not isinstance(rearm_generation, int):
            return False
        return (
            status.get("cutoff_open") is False
            and status.get("sensor_state") == "released"
            and status.get("physical_estop") is False
            and status.get("session_id") == session_id
            and status.get("freshness_id") == freshness_id
            and status.get("rearm_generation") == rearm_generation
        )
