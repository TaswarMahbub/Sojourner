"""Publisher-side status gate.

A status dict is not authorization. The gate accepts a status only when its
MAC matches the provisioned controller, it echoes the challenge the sender
just issued, its sequence is newer for that boot, and the sender's monotonic
receipt is inside 250 ms of that challenge. Cached, spoofed, reordered,
wrong-boot, and stale status do not authorize motion.
"""

from __future__ import annotations

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

    def expect(self, challenge: int, sent_mono: int) -> None:
        self.challenge = challenge
        self.challenge_mono = sent_mono

    def export_state(self) -> dict:
        return {
            "sequences": dict(self.sequences),
            "boot_nonce": self.boot_nonce,
            "challenge": self.challenge,
            "challenge_mono": self.challenge_mono,
        }

    def restore_state(self, state: dict) -> None:
        self.sequences = dict(state["sequences"])
        self.boot_nonce = state["boot_nonce"]
        self.challenge = state["challenge"]
        self.challenge_mono = state["challenge_mono"]
        self.latest = None
        self.received_mono = None

    def observe(self, status, sender_mono: int) -> str:
        if not isinstance(status, dict) or not verify_status(self.key, status):
            return "spoofed"
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

    def motion_authorized(self, sender_mono, session_id, sequence, freshness_id) -> bool:
        if self.latest is None or self.received_mono is None:
            return False
        if (
            isinstance(sender_mono, bool)
            or not isinstance(sender_mono, int)
            or sender_mono < self.received_mono
            or sender_mono - self.received_mono > FRESH_MS
        ):
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
