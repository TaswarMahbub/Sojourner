"""Schema 2 motor-command oracle.

The oracle applies docs/contracts.md to mocked datagrams. It is not firmware,
it does not drive motors, and a passing test is not a hardware verification.
"""

from __future__ import annotations

from tests.contract_schema import SAFETY_STOP_TASKS, validate_motor, validate_status


class MotorReceiver:
    """One receiver boot. A new boot_nonce requires a new instance."""

    watchdog_ms = 250
    future_skew_ms = 2000

    def __init__(self, boot_nonce: int):
        self.boot_nonce = boot_nonce
        self.now_ms = 0
        self.active_session = None
        self.last_sequence = None
        self.last_accept_ms = None
        self.physical_estop = False
        self.software_estop_latch = False
        self.fault_latch = False
        self.armed = False
        self.linear_mps = 0.0
        self.angular_rad_s = 0.0

    def set_time(self, now_ms: int) -> None:
        self.now_ms = now_ms
        self._watchdog()

    def set_physical_estop(self, asserted: bool) -> None:
        """Local hardware input. Packets cannot call this."""
        if asserted:
            self.physical_estop = True
            self.armed = False
            self._stop_output()
            return
        if self.physical_estop:
            self.physical_estop = False
            self.armed = False
            self._stop_output()

    def status(self) -> dict:
        message = {
            "schema_version": "2",
            "timestamp_ms": self.now_ms,
            "boot_nonce": self.boot_nonce,
            "session_id": self.active_session,
            "last_sequence": self.last_sequence,
            "physical_estop": self.physical_estop,
            "software_estop_latch": self.software_estop_latch,
            "fault_latch": self.fault_latch,
            "armed": self.armed,
            "commanding_stop": self.linear_mps == 0.0 and self.angular_rad_s == 0.0,
        }
        errors = validate_status(message)
        if errors:
            raise RuntimeError(errors)
        return message

    def handle(self, message) -> str:
        self._watchdog()
        if validate_motor(message):
            if self._claims_active_boot(message):
                self._stop_output()
                return "invalid_stop"
            return "ignored"
        if message["boot_nonce"] != self.boot_nonce:
            return "ignored"
        session_id = message["session_id"]
        if self.active_session is not None and session_id < self.active_session:
            return "ignored"
        if self.active_session is None or session_id > self.active_session:
            if self._is_session_start(message) and self._acceptable_time(message):
                self.active_session = session_id
                self.last_sequence = 1
                self.armed = False
                self._stop_output()
                self._mark_accepted()
                return "accepted_start"
            return "ignored"
        if self._too_far_ahead(message):
            self._stop_output()
            return "invalid_stop"
        if message["sequence"] <= self.last_sequence:
            if self._is_safety_stop(message):
                self._apply_stop(message)
                return "stale_stop"
            return "ignored"
        if message["stop"] is False:
            if self._expired(message) or self._motion_blocked():
                self._stop_output()
                return "rejected_motion"
            self.linear_mps = float(message["linear_mps"])
            self.angular_rad_s = float(message["angular_rad_s"])
            self._mark_accepted(message["sequence"])
            return "accepted_motion"
        if self._expired(message):
            self._apply_stop(message)
            return "expired_stop"
        if message["task_state"] == "rearm":
            if self.physical_estop:
                self._stop_output()
                return "rejected_rearm"
            self.software_estop_latch = False
            self.fault_latch = False
            self.armed = True
            self._stop_output()
            self._mark_accepted(message["sequence"])
            return "accepted_rearm"
        self._apply_stop(message)
        self._mark_accepted(message["sequence"])
        return "accepted_stop"

    def _claims_active_boot(self, message) -> bool:
        return (
            isinstance(message, dict)
            and message.get("boot_nonce") == self.boot_nonce
            and self.active_session is not None
            and message.get("session_id") == self.active_session
        )

    def _is_session_start(self, message) -> bool:
        return (
            message["sequence"] == 1
            and message["task_state"] == "idle"
            and message["stop"] is True
            and float(message["linear_mps"]) == 0.0
            and float(message["angular_rad_s"]) == 0.0
        )

    def _is_safety_stop(self, message) -> bool:
        return (
            message["stop"] is True
            and message["task_state"] in SAFETY_STOP_TASKS
            and float(message["linear_mps"]) == 0.0
            and float(message["angular_rad_s"]) == 0.0
        )

    def _acceptable_time(self, message) -> bool:
        return not self._expired(message) and not self._too_far_ahead(message)

    def _expired(self, message) -> bool:
        return message["expires_at_ms"] < self.now_ms

    def _too_far_ahead(self, message) -> bool:
        return message["issued_at_ms"] > self.now_ms + self.future_skew_ms

    def _motion_blocked(self) -> bool:
        return (
            self.physical_estop
            or self.software_estop_latch
            or self.fault_latch
            or not self.armed
        )

    def _apply_stop(self, message) -> None:
        self._stop_output()
        task = message["task_state"]
        if task == "estop":
            self.software_estop_latch = True
            self.armed = False
        elif task == "fault":
            self.fault_latch = True
            self.armed = False

    def _stop_output(self) -> None:
        self.linear_mps = 0.0
        self.angular_rad_s = 0.0

    def _mark_accepted(self, sequence: int | None = None) -> None:
        if sequence is not None:
            self.last_sequence = sequence
        self.last_accept_ms = self.now_ms

    def _watchdog(self) -> None:
        if self.last_accept_ms is None:
            return
        if self.now_ms - self.last_accept_ms > self.watchdog_ms:
            self._stop_output()
            self.fault_latch = True
            self.armed = False

    def snapshot(self) -> dict:
        return {
            "session_id": self.active_session,
            "last_sequence": self.last_sequence,
            "last_accept_ms": self.last_accept_ms,
            "physical_estop": self.physical_estop,
            "software_estop_latch": self.software_estop_latch,
            "fault_latch": self.fault_latch,
            "armed": self.armed,
            "linear_mps": self.linear_mps,
            "angular_rad_s": self.angular_rad_s,
        }
