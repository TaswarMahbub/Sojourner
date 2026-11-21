"""Publisher-side status gate.

A status dict is not authorization. The gate accepts a status only when its
MAC matches the provisioned controller, it echoes the challenge the sender
just issued, its sequence is newer for that boot, and the sender's monotonic
receipt is inside 250 ms of that challenge. Cached, spoofed, reordered,
wrong-boot, and stale status do not authorize motion.

Challenge ids are strictly increasing for the life of the sender, across
restarts. An id at or below the high-water mark is retired and never becomes
fresh again, so A -> B -> A cannot reset A's issuance time.
"""

from __future__ import annotations

from tests.contract_schema import validate_status
from tests.control_auth import verify_status

FRESH_MS = 250


class StatusGate:
    def __init__(self, key: bytes, controller_id: str):
        self.key = key
        self.controller_id = controller_id
        self.sequences: dict[int, int] = {}
        self.boot_nonce = None
        self.latest = None
        self.received_mono = None
        self.challenge = None
        self.challenge_mono = None
        self.challenge_high_water = 0

    def issue_challenge(self, sent_mono: int) -> int:
        """Allocate the next unused challenge id and make it outstanding."""
        challenge = self.challenge_high_water + 1
        self.expect(challenge, sent_mono)
        return challenge

    def expect(self, challenge: int, sent_mono: int) -> bool:
        """Record the challenge the sender has outstanding.

        Repeating the outstanding challenge does not move its issuance time
        forward. A different challenge must be greater than every id used
        before. It invalidates authorization from the previous status. A
        retired id is refused, and the gate then has no outstanding challenge,
        so nothing is authorized until a new id is issued.
        """
        if challenge == self.challenge and challenge is not None:
            return True
        self.latest = None
        self.received_mono = None
        if (
            isinstance(challenge, bool)
            or not isinstance(challenge, int)
            or challenge <= self.challenge_high_water
        ):
            self.challenge = None
            self.challenge_mono = None
            return False
        self.challenge = challenge
        self.challenge_mono = sent_mono
        self.challenge_high_water = challenge
        return True

    def export_state(self) -> dict:
        return {
            "sequences": dict(self.sequences),
            "boot_nonce": self.boot_nonce,
            "challenge": self.challenge,
            "challenge_mono": self.challenge_mono,
            "challenge_high_water": self.challenge_high_water,
        }

    def restore_state(self, state: dict) -> None:
        self.sequences = dict(state["sequences"])
        self.boot_nonce = state["boot_nonce"]
        self.challenge = state["challenge"]
        self.challenge_mono = state["challenge_mono"]
        self.challenge_high_water = state["challenge_high_water"]
        self.latest = None
        self.received_mono = None

    def observe(self, status, sender_mono: int) -> str:
        if not isinstance(status, dict) or not verify_status(self.key, status):
            return "spoofed"
        if validate_status(status):
            return "invalid_status"
        if status.get("controller_id") != self.controller_id:
            return "wrong_controller"
        if (
            self.challenge in (None, 0)
            or status.get("status_challenge") != self.challenge
        ):
            return "stale_challenge"
        if (
            isinstance(sender_mono, bool)
            or not isinstance(sender_mono, int)
            or isinstance(self.challenge_mono, bool)
            or not isinstance(self.challenge_mono, int)
            or sender_mono < self.challenge_mono
            or sender_mono - self.challenge_mono > FRESH_MS
        ):
            return "stale"
        boot = status.get("boot_nonce")
        seq = status.get("status_sequence")
        if isinstance(boot, bool) or not isinstance(boot, int):
            return "spoofed"
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            return "stale_or_reordered"
        if seq <= self.sequences.get(boot, 0):
            return "stale_or_reordered"
        if self.boot_nonce is not None and boot != self.boot_nonce:
            if status.get("armed") is True:
                return "wrong_boot"
        self.sequences[boot] = seq
        self.boot_nonce = boot
        self.latest = status
        self.received_mono = sender_mono
        return "accepted"

    def _current(self, sender_mono) -> bool:
        if self.latest is None or self.received_mono is None or self.challenge_mono is None:
            return False
        if isinstance(sender_mono, bool) or not isinstance(sender_mono, int):
            return False
        if sender_mono < self.received_mono or sender_mono < self.challenge_mono:
            return False
        if sender_mono - self.received_mono > FRESH_MS:
            return False
        if sender_mono - self.challenge_mono > FRESH_MS:
            return False
        return self.latest.get("status_challenge") == self.challenge

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
