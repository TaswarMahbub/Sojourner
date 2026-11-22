"""Publisher status gate and its persisted high-water marks.

docs/contracts.md section 4, "Publisher status gate" and "Persisted
status-gate state". This module is written from that text. The test oracle in
tests/status_gate.py is used only by tests/test_control.py to show that this
implementation gives the same answers.

The rules this module enforces, each traced to the contract:

* A stored status object is not authorization ("Publisher status gate").
  Authorization is re-evaluated against the sender's monotonic clock on every
  call, from the one status accepted under the outstanding challenge.
* Challenge ids are exact integers, strictly increasing for the life of the
  sender, with a persisted high-water mark. A retired id is never fresh again.
  Changing the outstanding challenge discards the accepted status.
* Both 250 ms ages are required: since the challenge was issued and since the
  status was received.
* status_sequence is strictly increasing per boot_nonce, persisted.
* After the first accepted boot, an armed status from a different boot is
  rejected.
* Persisted state is one JSON object with exactly five keys. Restoration is
  strict, rejects rollback, fails closed, and restores no challenge and no
  status. A locked gate unlocks only through a valid snapshot.
* Write ordering ("Persisted status-gate state"): a new challenge mark is
  durable before the challenge is returned for transmission, and a new
  status_sequence mark is durable before that status can authorize. A failed
  write still raises the in-memory mark.

Recovering from a lost or corrupt state is outside the contract. This package
provides it only through control/reset.py, a local operator tool.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from control import durable
from control.auth import verify_status
from tests.contract_schema import SAFE_INT_MAX, validate_status

LOG = logging.getLogger("control.status_gate")

# Section 4, "Publisher status gate": both ages at most 250 ms.
FRESH_MS = 250
STATE_VERSION = 1
STATE_KEYS = frozenset({"version", "controller_id", "challenge_high_water", "boot_nonce", "sequences"})


class PersistedStateError(ValueError):
    """Persisted gate state is missing, malformed, inconsistent, or rolls back."""


class PersistFailed(RuntimeError):
    """A durable write did not complete. The dependent action must not happen."""


class ChallengeExhausted(RuntimeError):
    """The challenge high-water mark is at the safe-integer maximum."""


class GateLocked(RuntimeError):
    """The gate is locked by invalid or missing persisted state."""


def _exact_int(value, low: int, high: int = SAFE_INT_MAX) -> bool:
    """Exact integer in range. Booleans and floats such as 44.0 are refused."""
    return type(value) is int and low <= value <= high


def _object_without_duplicates(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise PersistedStateError(f"duplicate key {name!r}")
        out[name] = value
    return out


def _refuse_constant(name):
    raise PersistedStateError(f"non-finite number {name}")


def parse_state_text(text) -> dict:
    """Parse persisted gate JSON. Duplicate keys and NaN/Infinity are rejected.

    Section 4, "Persisted status-gate state": restoration rejects input that
    is missing or not valid JSON, or that has a duplicate key or a non-finite
    number.
    """
    if not isinstance(text, (str, bytes, bytearray)):
        raise PersistedStateError("persisted state is missing")
    try:
        return json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_refuse_constant)
    except PersistedStateError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PersistedStateError("persisted state is not valid JSON") from exc


def fresh_state(controller_id: str) -> dict:
    """The empty state for a new controller identity. Used only by control/reset.py."""
    return {
        "version": STATE_VERSION,
        "controller_id": controller_id,
        "challenge_high_water": 0,
        "boot_nonce": None,
        "sequences": [],
    }


class StatusGate:
    """Section 4 publisher status gate, backed by one persisted JSON file.

    A new gate is locked. It authorizes nothing and refuses every challenge
    until restore_from_file() or restore_from_text() succeeds.
    """

    def __init__(self, key: bytes, controller_id: str, state_path):
        self._key = key
        self.controller_id = controller_id
        self.state_path = Path(state_path)
        self.locked = True
        self.challenge_high_water = 0
        self.boot_nonce: int | None = None
        self.sequences: dict[int, int] = {}
        self.last_durable_challenge: int | None = None
        self._challenge: int | None = None
        self._challenge_mono: int | None = None
        self._status: dict | None = None
        self._received_mono: int | None = None

    # ----- restoration -------------------------------------------------

    def restore_from_file(self) -> None:
        """Restore from state_path. A missing or invalid file locks the gate and raises."""
        text = durable.read_text_or_none(self.state_path)
        self.restore_from_text(text)

    def restore_from_text(self, text) -> None:
        """Restore high-water marks from persisted JSON text.

        Section 4, "Persisted status-gate state": the whole object is
        validated before any part of it is applied, a snapshot that would
        lower a mark is rejected, and a rejection locks the gate and keeps
        the in-memory marks. Success restores no challenge and no status.
        """
        self._drop_challenge()
        try:
            state = parse_state_text(text)
            sequences, boot_nonce, high_water = self._validate(state)
            self._reject_rollback(sequences, boot_nonce, high_water)
        except PersistedStateError as exc:
            self.locked = True
            LOG.error("status gate locked: %s", exc)
            raise
        self.sequences = sequences
        self.boot_nonce = boot_nonce
        self.challenge_high_water = high_water
        self.locked = False

    def _validate(self, state):
        if not isinstance(state, dict):
            raise PersistedStateError("persisted state is not an object")
        if set(state) != STATE_KEYS:
            raise PersistedStateError("persisted state has missing or extra keys")
        if type(state["version"]) is not int or state["version"] != STATE_VERSION:
            raise PersistedStateError("persisted state version is not 1")
        if not isinstance(state["controller_id"], str) or state["controller_id"] != self.controller_id:
            raise PersistedStateError("persisted state belongs to another controller")
        high_water = state["challenge_high_water"]
        if not _exact_int(high_water, 0):
            raise PersistedStateError("challenge_high_water is not an exact integer in range")
        pairs = state["sequences"]
        if not isinstance(pairs, list):
            raise PersistedStateError("sequences is not a list")
        sequences: dict[int, int] = {}
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2:
                raise PersistedStateError("a sequences entry is not a two-item pair")
            boot, seq = pair
            if not _exact_int(boot, 1) or not _exact_int(seq, 1):
                raise PersistedStateError("a sequences pair is not two exact integers in range")
            if boot in sequences:
                raise PersistedStateError("two pairs for the same boot")
            sequences[boot] = seq
        boot_nonce = state["boot_nonce"]
        if boot_nonce is None:
            if sequences:
                raise PersistedStateError("pairs present while boot_nonce is null")
        elif not _exact_int(boot_nonce, 1) or boot_nonce not in sequences:
            raise PersistedStateError("boot_nonce is invalid or has no pair")
        return sequences, boot_nonce, high_water

    def _reject_rollback(self, sequences, boot_nonce, high_water) -> None:
        if high_water < self.challenge_high_water:
            raise PersistedStateError("snapshot lowers challenge_high_water")
        for boot, known in self.sequences.items():
            if boot not in sequences:
                raise PersistedStateError("snapshot omits a known boot")
            if sequences[boot] < known:
                raise PersistedStateError("snapshot lowers a status_sequence mark")
        if self.boot_nonce is not None and boot_nonce != self.boot_nonce and boot_nonce in self.sequences:
            raise PersistedStateError("snapshot returns to an earlier known boot")

    def export_state(self) -> dict:
        """Durable marks only. A locked gate does not export a state (section 4)."""
        if self.locked:
            raise GateLocked("a locked gate does not export state")
        return self._snapshot()

    def _snapshot(self) -> dict:
        return {
            "version": STATE_VERSION,
            "controller_id": self.controller_id,
            "challenge_high_water": self.challenge_high_water,
            "boot_nonce": self.boot_nonce,
            "sequences": [[boot, seq] for boot, seq in sorted(self.sequences.items())],
        }

    def _write_durably(self) -> bool:
        """Atomic replacement of the whole object. False when it did not complete."""
        try:
            durable.atomic_write_json(self.state_path, self._snapshot())
        except (OSError, ValueError) as exc:
            LOG.error("status gate durable write failed: %r", exc)
            return False
        return True

    # ----- challenges --------------------------------------------------

    def issue_challenge(self, now_mono: int) -> int:
        """Allocate the next challenge id, persist its mark, then make it outstanding.

        Section 4, "Persisted status-gate state": the new challenge_high_water
        is durable before the id is returned, so the caller can only transmit
        an id whose mark is on disk. If the write fails, PersistFailed is
        raised, nothing is outstanding, and the in-memory mark stays raised so
        the id is never offered again in this process.
        """
        if self.locked:
            self._drop_challenge()
            raise GateLocked("gate is locked by invalid persisted state")
        if not _exact_int(now_mono, 0):
            self._drop_challenge()
            raise ValueError("now_mono is not an exact non-negative integer")
        if self.challenge_high_water >= SAFE_INT_MAX:
            self._drop_challenge()
            raise ChallengeExhausted("challenge ids are exhausted under this persisted state")
        challenge = self.challenge_high_water + 1
        if not self.expect(challenge, now_mono):
            raise PersistFailed("challenge high-water mark was not written durably")
        return challenge

    def expect(self, challenge, sent_mono) -> bool:
        """Make `challenge` the outstanding challenge, issued at `sent_mono`.

        Section 4, "Publisher status gate": ids are exact integers 1..2^53-1
        and issuance times exact integers 0..2^53-1. Repeating the outstanding
        id keeps its original issuance time. A different id must be above the
        high-water mark; changing the outstanding challenge discards the
        accepted status. A retired or invalid id leaves nothing outstanding.
        """
        if self.locked or not _exact_int(challenge, 1) or not _exact_int(sent_mono, 0):
            self._drop_challenge()
            return False
        if challenge == self._challenge:
            return True
        self._drop_challenge()
        if challenge <= self.challenge_high_water:
            return False
        self.challenge_high_water = challenge
        if not self._write_durably():
            return False
        self.last_durable_challenge = challenge
        self._challenge = challenge
        self._challenge_mono = sent_mono
        return True

    def _drop_challenge(self) -> None:
        self._challenge = None
        self._challenge_mono = None
        self._status = None
        self._received_mono = None

    @property
    def outstanding_challenge(self) -> int | None:
        return self._challenge

    def remaining_ms(self, now_mono: int) -> int | None:
        """Milliseconds left before the outstanding challenge's issuance plus 250."""
        if self._challenge_mono is None or not _exact_int(now_mono, 0):
            return None
        return self._challenge_mono + FRESH_MS - now_mono

    # ----- status ------------------------------------------------------

    def observe(self, status, now_mono) -> str:
        """Run the section 4 status checks. Returns "accepted" or a rejection reason.

        Order: lock, MAC, full schema validation, controller, outstanding
        challenge, both ages, per-boot sequence, armed-from-a-different-boot.
        A newer status first discards the previously accepted one, then its
        sequence mark is written durably, and only then is it stored.
        """
        if self.locked:
            return "locked"
        if not isinstance(status, dict) or not verify_status(self._key, status):
            return "spoofed"
        if validate_status(status):
            return "invalid_status"
        if status["controller_id"] != self.controller_id:
            return "wrong_controller"
        echoed = status["status_challenge"]
        if self._challenge is None or not _exact_int(echoed, 1) or echoed != self._challenge:
            return "stale_challenge"
        if (
            not _exact_int(now_mono, 0)
            or self._challenge_mono is None
            or now_mono < self._challenge_mono
            or now_mono - self._challenge_mono > FRESH_MS
        ):
            return "stale"
        boot = status["boot_nonce"]
        seq = status["status_sequence"]
        if seq <= self.sequences.get(boot, 0):
            return "stale_or_reordered"
        if self.boot_nonce is not None and boot != self.boot_nonce and status["armed"] is True:
            return "wrong_boot"
        self._status = None
        self._received_mono = None
        self.sequences[boot] = seq
        self.boot_nonce = boot
        if not self._write_durably():
            return "persist_failed"
        self._status = dict(status)
        self._received_mono = now_mono
        return "accepted"

    def current_status(self, now_mono) -> dict | None:
        """A copy of the accepted status while it is still current, else None.

        Current means: gate unlocked, both 250 ms ages satisfied, and the
        status still echoes the outstanding challenge (section 4).
        """
        if self.locked or self._status is None or self._received_mono is None:
            return None
        if self._challenge_mono is None or not _exact_int(now_mono, 0):
            return None
        if now_mono < self._received_mono or now_mono < self._challenge_mono:
            return None
        if now_mono - self._received_mono > FRESH_MS:
            return None
        if now_mono - self._challenge_mono > FRESH_MS:
            return None
        if self._status.get("status_challenge") != self._challenge:
            return None
        return dict(self._status)

    def _sequence_above(self, status: dict, sequence) -> bool:
        last = status.get("last_sequence")
        return _exact_int(sequence, 1) and _exact_int(last, 1) and sequence > last

    def motion_authorized(self, now_mono, session_id, sequence, freshness_id) -> bool:
        """Section 4 motion checks on the current status. Stored status alone is not enough."""
        status = self.current_status(now_mono)
        if status is None or not self._sequence_above(status, sequence):
            return False
        return (
            status["armed"] is True
            and status["cutoff_open"] is False
            and status["sensor_state"] == "released"
            and status["physical_estop"] is False
            and status["software_estop_latch"] is False
            and status["fault_latch"] is False
            and status["session_id"] == session_id
            and status["freshness_id"] == freshness_id
        )

    def rearm_authorized(self, now_mono, session_id, sequence, freshness_id, rearm_generation, *, operator_authorized) -> bool:
        """Section 4 rearm checks. Latches may be set and armed may be false.

        operator_authorized must be the literal True: it is a local laptop
        boolean, never a value read from the network.
        """
        if operator_authorized is not True:
            return False
        status = self.current_status(now_mono)
        if status is None or not self._sequence_above(status, sequence):
            return False
        if type(rearm_generation) is not int:
            return False
        return (
            status["cutoff_open"] is False
            and status["sensor_state"] == "released"
            and status["physical_estop"] is False
            and status["session_id"] == session_id
            and status["freshness_id"] == freshness_id
            and status["rearm_generation"] == rearm_generation
        )
