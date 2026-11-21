"""Schema 3 motor-command oracle.

The oracle applies docs/contracts.md to mocked datagrams. It is not firmware,
it does not drive motors, and a passing test is not a hardware verification.
Hardware cutoff behavior here is a model. It is unverified on a bench.
"""

from __future__ import annotations

from tests.contract_schema import SAFETY_STOP_TASKS, validate_motor, validate_status
from tests.control_auth import (
    CONTROLLER,
    TEST_KEY,
    decode_strict,
    peek_object,
    sign_status,
    verify_command,
)

WATCHDOG_MS = 250
FRESH_MS = 250
FUTURE_SKEW_MS = 2000


class MotorReceiver:
    """One receiver boot. A new boot_nonce requires a new boot."""

    def __init__(self, boot_nonce: int, *, key: bytes = TEST_KEY, controller_id: str = CONTROLLER):
        self.boot_nonce = boot_nonce
        self.key = key
        self.controller_id = controller_id
        self.monotonic_ms = 0
        self.unix_ms = None
        self.clock_synchronized = False
        self.freshness_id = 1
        self.challenge_issued_mono = 0
        self.rearm_generation = 0
        self.status_challenge = 0
        self.status_sequence = 0
        self.active_session = None
        self.last_sequence = None
        self.last_accept_mono = None
        self.motion_deadline_mono = None
        self.motion_expires_unix = None
        self.last_pet_mono = 0
        self.sensor_state = "unknown"
        self.physical_estop = True
        self.software_estop_latch = False
        self.fault_latch = False
        self.armed = False
        self.startup_latch = True
        self.bus_powered = True
        self.cutoff_open = True
        self.firmware_frozen = False
        self.linear_mps = 0.0
        self.angular_rad_s = 0.0

    def set_monotonic(self, ms: int) -> None:
        """Advance the receiver's monotonic clock. It never moves backward."""
        if isinstance(ms, bool) or not isinstance(ms, int) or ms < self.monotonic_ms:
            return
        self.monotonic_ms = ms
        if self.firmware_frozen:
            self.cutoff_open = self._hardware_cutoff_required()
            return
        self._enforce_timeouts()

    def set_unix(self, unix_ms: int) -> None:
        """Record a wall-clock reading without trusting it."""
        if isinstance(unix_ms, bool) or not isinstance(unix_ms, int) or unix_ms < 0:
            return
        self.unix_ms = unix_ms

    def note_verified_clock(self, unix_ms: int) -> None:
        """Test hook for a completed bounded-error sync. A packet cannot call this."""
        if isinstance(unix_ms, bool) or not isinstance(unix_ms, int) or unix_ms < 0:
            return
        self.unix_ms = unix_ms
        self.clock_synchronized = True
        if not self.firmware_frozen:
            self._enforce_timeouts()

    def set_sensor(self, state: str) -> None:
        if state not in ("asserted", "released", "unknown"):
            raise ValueError(state)
        if state == self.sensor_state:
            return
        self.sensor_state = state
        self.physical_estop = state != "released"
        self.armed = False
        self._stop_output()
        self._bump_generation()
        self._rotate_challenge()
        if not self.firmware_frozen:
            self._refresh_cutoff()
        else:
            self.cutoff_open = True

    def set_physical_estop(self, asserted: bool) -> None:
        """Local hardware input. Packets cannot call this."""
        self.set_sensor("asserted" if asserted else "released")

    def complete_startup(self) -> None:
        """Firmware finished booting. This reads nothing and does not arm."""
        self.startup_latch = False
        self.armed = False
        self._stop_output()
        if not self.firmware_frozen:
            self._refresh_cutoff()

    def lose_motor_power(self) -> None:
        self.bus_powered = False
        self.cutoff_open = True
        self.armed = False
        self._stop_output()

    def restore_motor_power(self) -> None:
        """Motor supply returns without an MCU reset. The same boot stays disarmed."""
        self.bus_powered = True
        self.armed = False
        self._stop_output()
        self._bump_generation()
        self._rotate_challenge()
        if not self.firmware_frozen:
            self._refresh_cutoff()
        else:
            self.cutoff_open = True

    def brownout_reset(self, new_nonce: int) -> None:
        """Power loss of the MCU. This is a new boot, stopped and disarmed."""
        fresh = MotorReceiver(new_nonce, key=self.key, controller_id=self.controller_id)
        self.__dict__.clear()
        self.__dict__.update(fresh.__dict__)

    def freeze_firmware(self) -> None:
        self.firmware_frozen = True

    def poll(self) -> None:
        """One firmware loop. A frozen MCU does not call this."""
        if self.firmware_frozen:
            return
        self._enforce_timeouts()

    def status(self) -> dict:
        self.status_sequence += 1
        message = {
            "schema_version": "3",
            "controller_id": self.controller_id,
            "timestamp_ms": self.unix_ms if self.clock_synchronized and self.unix_ms is not None else 0,
            "boot_nonce": self.boot_nonce,
            "session_id": self.active_session,
            "last_sequence": self.last_sequence,
            "status_sequence": self.status_sequence,
            "freshness_id": self.freshness_id,
            "rearm_generation": self.rearm_generation,
            "status_challenge": self.status_challenge,
            "clock_state": "synchronized" if self.clock_synchronized else "unsynchronized",
            "sensor_state": self.sensor_state,
            "cutoff_open": self.cutoff_open,
            "physical_estop": self.physical_estop,
            "software_estop_latch": self.software_estop_latch,
            "fault_latch": self.fault_latch,
            "armed": self.armed,
            "commanding_stop": self.linear_mps == 0.0 and self.angular_rad_s == 0.0,
        }
        message["mac"] = sign_status(self.key, message)
        errors = validate_status(message)
        if errors:
            raise RuntimeError(errors)
        return message

    def handle(self, message) -> str:
        return self.handle_raw(message)

    def handle_raw(self, raw) -> str:
        try:
            if self.firmware_frozen:
                self.cutoff_open = self._hardware_cutoff_required()
                return "frozen"
            self._enforce_timeouts()
            message, identity, kind = self._decode(raw)
        except Exception:
            return "malformed"
        try:
            return self._dispatch(message, identity, kind)
        except Exception:
            try:
                if self._claims_active(identity) or self._claims_active(message):
                    self._stop_output()
                    return "invalid_stop"
            except Exception:
                return "malformed"
            return "malformed"

    def motors_have_torque(self) -> bool:
        snap = self.snapshot()
        if snap["cutoff_open"] or not snap["bus_powered"]:
            return False
        if snap["sensor_state"] != "released" or snap["physical_estop"]:
            return False
        if snap["firmware_frozen"] and snap["monotonic_ms"] - snap["last_pet_mono"] > WATCHDOG_MS:
            return False
        if not snap["armed"]:
            return False
        if snap["linear_mps"] == 0.0 and snap["angular_rad_s"] == 0.0:
            return False
        deadline = snap["motion_deadline_mono"]
        if deadline is not None and snap["monotonic_ms"] >= deadline:
            return False
        return True

    def snapshot(self) -> dict:
        return {
            "session_id": self.active_session,
            "last_sequence": self.last_sequence,
            "last_accept_ms": self.last_accept_mono,
            "physical_estop": self.physical_estop,
            "software_estop_latch": self.software_estop_latch,
            "fault_latch": self.fault_latch,
            "armed": self.armed,
            "linear_mps": self.linear_mps,
            "angular_rad_s": self.angular_rad_s,
            "monotonic_ms": self.monotonic_ms,
            "unix_ms": self.unix_ms,
            "clock_synchronized": self.clock_synchronized,
            "freshness_id": self.freshness_id,
            "challenge_issued_mono": self.challenge_issued_mono,
            "rearm_generation": self.rearm_generation,
            "status_challenge": self.status_challenge,
            "motion_deadline_mono": self.motion_deadline_mono,
            "last_pet_mono": self.last_pet_mono,
            "sensor_state": self.sensor_state,
            "cutoff_open": self.cutoff_open,
            "bus_powered": self.bus_powered,
            "startup_latch": self.startup_latch,
            "firmware_frozen": self.firmware_frozen,
            "boot_nonce": self.boot_nonce,
            "controller_id": self.controller_id,
        }

    def _decode(self, raw):
        if isinstance(raw, dict):
            return raw, raw, "object"
        if isinstance(raw, (bytes, bytearray)):
            text = bytes(raw)
        elif isinstance(raw, str):
            text = raw
        else:
            return None, None, "malformed"
        value, err = decode_strict(text)
        identity = peek_object(text)
        if err:
            return None, identity, "malformed"
        return value, identity, "object"

    def _dispatch(self, message, identity, kind: str) -> str:
        if kind != "object" or not isinstance(message, dict):
            if self._claims_active(identity):
                self._stop_output()
                return "invalid_stop"
            return "malformed"
        if not verify_command(self.key, message) or message.get("controller_id") != self.controller_id:
            if self._claims_active(message):
                self._stop_output()
                return "invalid_stop"
            return "ignored"
        if validate_motor(message):
            if self._claims_active(message):
                self._stop_output()
                return "invalid_stop"
            return "ignored"
        if message["boot_nonce"] != self.boot_nonce:
            return "ignored"
        session_id = message["session_id"]
        if self.active_session is not None and session_id < self.active_session:
            return "ignored"
        if self.active_session is None or session_id > self.active_session:
            if (
                self._is_session_start(message)
                and self._fresh(message)
                and self._generation_ok(message)
                and not self._unix_too_far(message)
                and not self._unix_expired(message)
            ):
                self.active_session = session_id
                self.armed = False
                self._stop_output()
                self._mark_accepted(1, message)
                return "accepted_start"
            return "ignored"
        if self._unix_too_far(message):
            self._stop_output()
            return "invalid_stop"
        fresh = self._fresh(message)
        generation_ok = self._generation_ok(message)
        if message["sequence"] <= self.last_sequence:
            if self._is_safety_stop(message):
                self._apply_stop(message)
                return "stale_stop"
            return "ignored"
        if message["stop"] is False:
            if not fresh or not generation_ok or self._unix_expired(message):
                return "rejected_motion"
            if self._motion_blocked():
                self._stop_output()
                return "rejected_motion"
            self.linear_mps = float(message["linear_mps"])
            self.angular_rad_s = float(message["angular_rad_s"])
            lifetime = message["expires_at_ms"] - message["issued_at_ms"]
            self.motion_deadline_mono = self.monotonic_ms + lifetime
            self.motion_expires_unix = message["expires_at_ms"]
            self._mark_accepted(message["sequence"], message)
            return "accepted_motion"
        if not fresh or self._unix_expired(message):
            if message["task_state"] == "rearm" or not self._is_safety_stop(message):
                return "rejected_rearm" if message["task_state"] == "rearm" else "invalid_stop"
            self._apply_stop(message)
            return "expired_stop"
        if message["task_state"] == "rearm":
            if not generation_ok or self._rearm_blocked():
                self._stop_output()
                return "rejected_rearm"
            self.software_estop_latch = False
            self.fault_latch = False
            self.armed = True
            self._stop_output()
            self._mark_accepted(message["sequence"], message)
            return "accepted_rearm"
        self._apply_stop(message)
        self._mark_accepted(message["sequence"], message)
        return "accepted_stop"

    def _claims_active(self, message) -> bool:
        if not isinstance(message, dict) or self.active_session is None:
            return False
        return (
            message.get("boot_nonce") == self.boot_nonce
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

    def _fresh(self, message) -> bool:
        return (
            message.get("freshness_id") == self.freshness_id
            and self.monotonic_ms - self.challenge_issued_mono <= FRESH_MS
        )

    def _generation_ok(self, message) -> bool:
        return message.get("rearm_generation") == self.rearm_generation

    def _unix_expired(self, message) -> bool:
        if not self.clock_synchronized or self.unix_ms is None:
            return False
        return message["expires_at_ms"] < self.unix_ms

    def _unix_too_far(self, message) -> bool:
        if not self.clock_synchronized or self.unix_ms is None:
            return False
        return message["issued_at_ms"] > self.unix_ms + FUTURE_SKEW_MS

    def _motion_blocked(self) -> bool:
        return (
            self.physical_estop
            or self.sensor_state != "released"
            or self.software_estop_latch
            or self.fault_latch
            or not self.armed
            or self.cutoff_open
            or not self.bus_powered
            or self.startup_latch
        )

    def _rearm_blocked(self) -> bool:
        return (
            self.physical_estop
            or self.sensor_state != "released"
            or self.cutoff_open
            or not self.bus_powered
            or self.startup_latch
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
        self.motion_deadline_mono = None
        self.motion_expires_unix = None

    def _mark_accepted(self, sequence: int, message) -> None:
        self.last_sequence = sequence
        self.last_accept_mono = self.monotonic_ms
        challenge = message.get("status_challenge")
        if isinstance(challenge, int) and not isinstance(challenge, bool):
            self.status_challenge = challenge
        self._rotate_challenge()

    def _bump_generation(self) -> None:
        self.rearm_generation += 1

    def _rotate_challenge(self) -> None:
        self.freshness_id += 1
        self.challenge_issued_mono = self.monotonic_ms

    def _pet(self) -> None:
        self.last_pet_mono = self.monotonic_ms

    def _hardware_cutoff_required(self) -> bool:
        pet_late = self.monotonic_ms - self.last_pet_mono > WATCHDOG_MS
        return (
            self.startup_latch
            or not self.bus_powered
            or self.sensor_state != "released"
            or self.physical_estop
            or pet_late
        )

    def _refresh_cutoff(self) -> None:
        self.cutoff_open = self._hardware_cutoff_required()
        if self.cutoff_open:
            self.armed = False
            self._stop_output()

    def _enforce_timeouts(self) -> None:
        self._pet()
        self._refresh_cutoff()
        if (
            self.motion_deadline_mono is not None
            and self.monotonic_ms >= self.motion_deadline_mono
        ):
            self._stop_output()
        if (
            self.clock_synchronized
            and self.unix_ms is not None
            and self.motion_expires_unix is not None
            and self.unix_ms >= self.motion_expires_unix
        ):
            self._stop_output()
        if (
            self.last_accept_mono is not None
            and self.monotonic_ms - self.last_accept_mono > WATCHDOG_MS
        ):
            self._stop_output()
            self.fault_latch = True
            self.armed = False
        if self.monotonic_ms - self.challenge_issued_mono > FRESH_MS:
            self._rotate_challenge()
