"""Shared-control publisher: stop by default, motion only when fully authorized.

docs/contracts.md, "Publisher duties before motion exists", section 3 "Task
state", "Sessions on UDP" and "Freshness challenge", and section 4 "Publisher
status gate".

The default output of every tick is a stop. Motion is the single exception,
and each condition below has to hold on that same tick:

* a MotionRequest was handed in (nothing in this package creates one; the
  gesture-to-velocity mapping is a later reviewed change, see "Messages");
* the session is trusted (section 3, step 5);
* the newest camera frame is present, actionable and at most 250 ms old;
* the newest gesture is known (not "unknown", not an unreviewed label), no
  observed frame inside its window was non-actionable, and its
  window_end_timestamp_ms is at most 250 ms old;
* the status gate authorizes motion now (section 4), and at least 1 ms
  remains before the outstanding challenge's issuance plus 250 ms;
* a new challenge id was written durably before the datagram is sealed.

The freshness_id and rearm_generation in a motion or rearm datagram are
copied from the one status that authorized it. The authorization is checked
before the next challenge is issued, because issuing a challenge discards the
accepted status (section 4).

Stops never wait on persistence. If a new challenge cannot be issued, a stop
carries the last challenge id whose mark is already durable. That id was
already transmitted or is safe to transmit, so "never transmitted before its
mark is durable" still holds.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass

from control.auth import CONTROLLER_ID_PATTERN, Credentials, canonical_velocity, sign_command
from control.session import (
    Session,
    SessionAllocator,
    SessionLocked,
    VerifiedStatusTracker,
    session_start_fields,
)
from control.status_gate import ChallengeExhausted, GateLocked, PersistFailed, StatusGate
from tests.contract_schema import SCHEMA, SEQUENCE_MAX, validate_feature, validate_gesture, validate_motor

LOG = logging.getLogger("control.publisher")

# "Publisher duties before motion exists": 250 ms staleness for frames and gestures.
PERCEPTION_MAX_AGE_MS = 250
# Section 3, "Clocks": 1 <= expires_at_ms - issued_at_ms <= 250.
MAX_LIFETIME_MS = 250
# Section 3, "Freshness challenge": motion needs at least 1 ms remaining.
MIN_REMAINING_MS = 1
# Section 3 "Linear and angular motion".
LINEAR_LIMIT = 0.30
ANGULAR_LIMIT = 1.00
# Section 2 "Label": no reviewed label list exists for any model version yet,
# so the only known label is the reserved "neutral". "unknown" and any other
# label are treated as unknown and produce a stop.
REVIEWED_LABELS: frozenset[str] = frozenset()
KNOWN_LABELS = frozenset({"neutral"}) | REVIEWED_LABELS
# Section 3 "Parsing".
MAX_DATAGRAM_BYTES = 8192
MAX_JSON_DEPTH = 32
# How many observed frames are kept to check a gesture window.
FRAME_HISTORY = 512


@dataclass(frozen=True)
class MotionRequest:
    """A velocity request from an upstream task controller (not built in this phase).

    Section 3 "Task state": only "manual" and "shared" may request motion.
    Velocities must be canonical and inside the section 3 ranges. A request
    that is not is refused at construction.
    """

    task_state: str
    linear_mps: float
    angular_rad_s: float

    def __post_init__(self):
        if self.task_state not in ("manual", "shared"):
            raise ValueError("motion task_state must be 'manual' or 'shared'")
        linear = canonical_velocity(self.linear_mps)
        angular = canonical_velocity(self.angular_rad_s)
        if linear is None or angular is None:
            raise ValueError("motion velocities must be canonical six-digit values, not negative zero")
        if not -LINEAR_LIMIT <= linear <= LINEAR_LIMIT or not -ANGULAR_LIMIT <= angular <= ANGULAR_LIMIT:
            raise ValueError("motion velocities are outside the schema 4 ranges")


class SystemClock:
    """Sender monotonic milliseconds and Unix UTC milliseconds (section 3, "Clocks")."""

    def mono_ms(self) -> int:
        return time.monotonic_ns() // 1_000_000

    def unix_ms(self) -> int:
        return time.time_ns() // 1_000_000


def _depth_exceeds(text: str, limit: int) -> bool:
    depth = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > limit:
                return True
        elif ch in "}]":
            depth -= 1
    return False


def _object_without_duplicates(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise ValueError(f"duplicate key {name!r}")
        out[name] = value
    return out


def _refuse_constant(name):
    raise ValueError(f"non-finite number {name}")


def decode_status(raw) -> tuple[dict | None, str | None]:
    """Strict decode of a status datagram. Returns (object, None) or (None, reason).

    Section 3 "Parsing" and "Versioning": over 8192 bytes, nesting deeper
    than 32, duplicate keys and NaN/Infinity are rejected, and a rejection is
    a result rather than an exception. The decoded object is not trusted
    until the status gate checks it.
    """
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, (bytes, bytearray, str)):
        return None, "not bytes"
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(data) > MAX_DATAGRAM_BYTES:
        return None, "oversize"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not utf-8"
    if _depth_exceeds(text, MAX_JSON_DEPTH):
        return None, "too deep"
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        return None, f"malformed: {exc}"
    if not isinstance(value, dict):
        return None, "not an object"
    return value, None


def is_actionable_frame(frame) -> bool:
    """Section "Messages": tracking, all blendshapes, valid head_pose, confidence null or in (0, 1]."""
    return isinstance(frame, dict) and not validate_feature(frame) and frame["tracking_state"] == "tracking"


class Publisher:
    """The only publisher of motor commands. One tick sends at most one datagram."""

    def __init__(self, credentials: Credentials, gate: StatusGate, allocator: SessionAllocator, send, clock=None):
        if CONTROLLER_ID_PATTERN.fullmatch(credentials.controller_id) is None:
            raise ValueError("controller_id is invalid")
        if gate.controller_id != credentials.controller_id or allocator.controller_id != credentials.controller_id:
            raise ValueError("gate, allocator and credentials name different controllers")
        self._credentials = credentials
        self.gate = gate
        self.allocator = allocator
        self._send = send
        self.clock = SystemClock() if clock is None else clock
        self.tracker = VerifiedStatusTracker(credentials.key, credentials.controller_id)
        self.session: Session | None = None
        self._frame: dict | None = None
        self._gesture: dict | None = None
        self._frames: deque = deque(maxlen=FRAME_HISTORY)
        self.last_decision: str = "none"

    # ----- inputs ------------------------------------------------------

    def observe_frame(self, frame) -> None:
        """Record the newest camera frame received. Its actionability is noted for window checks."""
        self._frame = frame
        timestamp = frame.get("timestamp_ms") if isinstance(frame, dict) else None
        if type(timestamp) is int:
            self._frames.append((timestamp, is_actionable_frame(frame)))

    def observe_gesture(self, gesture) -> None:
        """Record the newest gesture classification received."""
        self._gesture = gesture

    def observe_status(self, raw) -> str:
        """Decode a status, run the gate, update session trust. Returns the gate result."""
        now = self.clock.mono_ms()
        status, reason = decode_status(raw)
        if status is None:
            LOG.info("status rejected before the gate: %s", reason)
            return "malformed"
        marks_before = dict(self.gate.sequences)
        result = self.gate.observe(status, now)
        self.tracker.observe(status, now, marks_before)
        session = self.session
        if session is not None and result == "accepted":
            session.note_gate_status(status, result)
        newest = self.tracker.status
        if session is not None and newest is not None:
            if newest["boot_nonce"] != session.boot_nonce:
                LOG.warning("receiver boot changed; dropping session %s", session.session_id)
                self.session = None
            elif session.trusted and newest["session_id"] != session.session_id:
                LOG.warning("receiver reports another session; dropping session %s", session.session_id)
                self.session = None
        return result

    # ----- perception checks ------------------------------------------

    def perception_stop_reason(self, unix_now: int) -> str | None:
        """Return why perception requires a stop, or None when it permits motion.

        "Publisher duties before motion exists": stop when the newest frame is
        missing, not actionable, or older than 250 ms; stop when the newest
        gesture is unknown, any frame in its window was not actionable, or
        window_end_timestamp_ms is older than 250 ms. A timestamp ahead of the
        laptop clock is treated as stale.
        """
        frame = self._frame
        if frame is None:
            return "camera frame missing"
        if not is_actionable_frame(frame):
            return "camera frame not actionable"
        age = unix_now - frame["timestamp_ms"]
        if age > PERCEPTION_MAX_AGE_MS or age < 0:
            return "camera frame stale"
        gesture = self._gesture
        if gesture is None:
            return "gesture missing"
        if validate_gesture(gesture):
            return "gesture invalid"
        if gesture["label"] not in KNOWN_LABELS:
            return "gesture unknown"
        end = gesture["window_end_timestamp_ms"]
        start = gesture["window_start_timestamp_ms"]
        gesture_age = unix_now - end
        if gesture_age > PERCEPTION_MAX_AGE_MS or gesture_age < 0:
            return "gesture stale"
        for timestamp, actionable in self._frames:
            if start <= timestamp <= end and not actionable:
                return "gesture window contains a non-actionable frame"
        return None

    # ----- the tick ----------------------------------------------------

    def tick(self, *, motion: MotionRequest | None = None, operator_estop: bool = False, operator_rearm: bool = False) -> dict | None:
        """Build, seal and send at most one datagram. Returns it, or None if nothing could be addressed.

        `operator_estop` and `operator_rearm` are local laptop booleans
        ("Publisher duties", section 4 rearm rules). They never come from the
        network.
        """
        mono = self.clock.mono_ms()
        unix = self.clock.unix_ms()
        session = self.session

        if session is not None and self._sequence_exhausted(session):
            LOG.warning("session %s used sequence 4294967295; opening a greater session", session.session_id)
            self.session = session = None

        if session is None:
            if operator_estop is True:
                return self._estop_reported_session(mono, unix)
            return self._start_session(mono, unix)

        if operator_estop is True:
            return self._send_stop(session, "estop", mono, unix, "operator estop")

        if not session.trusted:
            if mono - session.started_mono > MAX_LIFETIME_MS:
                LOG.info("session %s was never confirmed; opening a greater session", session.session_id)
                self.session = None
                return self._start_session(mono, unix)
            self.last_decision = "awaiting session confirmation"
            return None

        status = self.gate.current_status(mono)
        sequence = session.next_sequence

        if operator_rearm is True and status is not None and self.gate.rearm_authorized(
            mono,
            session.session_id,
            sequence,
            status["freshness_id"],
            status["rearm_generation"],
            operator_authorized=True,
        ):
            return self._send_authorized(session, status, "rearm", 0.0, 0.0, True, mono, unix)

        reason = self.perception_stop_reason(unix)
        if reason is not None:
            return self._send_stop(session, "idle", mono, unix, reason)

        if motion is None:
            return self._send_stop(session, "idle", mono, unix, "no motion requested")

        if not isinstance(motion, MotionRequest):
            raise TypeError("motion must be a MotionRequest")

        if status is None:
            return self._send_stop(session, "idle", mono, unix, "no current gate-accepted status")
        if not self.gate.motion_authorized(mono, session.session_id, sequence, status["freshness_id"]):
            return self._send_stop(session, "idle", mono, unix, "status gate refuses motion")
        remaining = self.gate.remaining_ms(mono)
        if remaining is None or remaining < MIN_REMAINING_MS:
            return self._send_stop(session, "idle", mono, unix, "under 1 ms of challenge lifetime remains")
        return self._send_authorized(
            session, status, motion.task_state, motion.linear_mps, motion.angular_rad_s, False, mono, unix, remaining
        )

    # ----- datagram construction --------------------------------------

    def _sequence_exhausted(self, session: Session) -> bool:
        return session.next_sequence > SEQUENCE_MAX

    def _start_session(self, mono: int, unix: int) -> dict | None:
        """Section 3, "Sessions on UDP", steps 1-4."""
        status = self.tracker.recent(mono, MAX_LIFETIME_MS)
        if status is None:
            self.last_decision = "no recent verified status to address a session start"
            return None
        try:
            session_id = self.allocator.next_session_id(floor=unix)
            challenge = self.gate.issue_challenge(mono)
        except (SessionLocked, GateLocked, PersistFailed, ChallengeExhausted) as exc:
            self.last_decision = f"cannot start a session: {exc}"
            LOG.error(self.last_decision)
            return None
        fields = session_start_fields(status, session_id, challenge)
        self.session = Session(
            boot_nonce=status["boot_nonce"], session_id=session_id, start_challenge=challenge, started_mono=mono
        )
        return self._seal_and_send(fields, unix, MAX_LIFETIME_MS, "session start")

    def _estop_reported_session(self, mono: int, unix: int) -> dict | None:
        """Operator estop while this process has no session of its own.

        Section 3 "Receiver procedure", steps 6 and 8: an authenticated
        `estop` for the active boot and session is applied even with an older
        sequence or an expired challenge. The receiver's own status names its
        active session and last sequence, so the estop is addressed there.
        This can only stop; it cannot arm or move.
        """
        status = self.tracker.status
        if status is None or status["session_id"] is None:
            self.last_decision = "estop requested but the receiver reports no active session"
            LOG.warning(self.last_decision)
            return None
        challenge = self._next_challenge_for_stop(mono)
        if challenge is None:
            self.last_decision = "estop requested but no durable challenge id exists"
            LOG.error(self.last_decision)
            return None
        fields = {
            "boot_nonce": status["boot_nonce"],
            "session_id": status["session_id"],
            "sequence": min(status["last_sequence"] + 1, SEQUENCE_MAX),
            "freshness_id": status["freshness_id"],
            "rearm_generation": status["rearm_generation"],
            "status_challenge": challenge,
            "task_state": "estop",
            "linear_mps": 0.0,
            "angular_rad_s": 0.0,
            "stop": True,
        }
        return self._seal_and_send(fields, unix, MAX_LIFETIME_MS, "estop: operator, receiver-reported session")

    def _addressing_status(self, mono: int) -> dict | None:
        current = self.gate.current_status(mono)
        if current is not None:
            return current
        return self.tracker.status

    def _next_challenge_for_stop(self, mono: int) -> int | None:
        try:
            return self.gate.issue_challenge(mono)
        except (GateLocked, PersistFailed, ChallengeExhausted) as exc:
            LOG.error("stop sent without a new challenge: %s", exc)
            return self.gate.last_durable_challenge

    def _send_stop(self, session: Session, task_state: str, mono: int, unix: int, reason: str) -> dict | None:
        """Every stop: stop true, both velocities 0.0 (section 3 "Task state")."""
        status = self._addressing_status(mono)
        sequence = session.take_sequence()
        if status is None or sequence is None:
            self.last_decision = f"cannot address a stop ({reason})"
            LOG.error(self.last_decision)
            return None
        challenge = self._next_challenge_for_stop(mono)
        if challenge is None:
            self.last_decision = f"no durable challenge id for a stop ({reason})"
            LOG.error(self.last_decision)
            return None
        fields = {
            "boot_nonce": session.boot_nonce,
            "session_id": session.session_id,
            "sequence": sequence,
            "freshness_id": status["freshness_id"],
            "rearm_generation": status["rearm_generation"],
            "status_challenge": challenge,
            "task_state": task_state,
            "linear_mps": 0.0,
            "angular_rad_s": 0.0,
            "stop": True,
        }
        return self._seal_and_send(fields, unix, MAX_LIFETIME_MS, f"{task_state}: {reason}")

    def _send_authorized(
        self, session, status, task_state, linear, angular, stop, mono, unix, remaining=MAX_LIFETIME_MS
    ) -> dict | None:
        """Rearm or motion, already authorized against `status` on this tick.

        The next challenge must be written durably first. If that fails, a
        stop goes out instead.
        """
        try:
            challenge = self.gate.issue_challenge(mono)
        except (GateLocked, PersistFailed, ChallengeExhausted) as exc:
            return self._send_stop(session, "idle", mono, unix, f"challenge not durable: {exc}")
        sequence = session.take_sequence()
        if sequence is None:
            self.session = None
            self.last_decision = "sequence exhausted"
            return None
        fields = {
            "boot_nonce": session.boot_nonce,
            "session_id": session.session_id,
            "sequence": sequence,
            "freshness_id": status["freshness_id"],
            "rearm_generation": status["rearm_generation"],
            "status_challenge": challenge,
            "task_state": task_state,
            "linear_mps": linear,
            "angular_rad_s": angular,
            "stop": stop,
        }
        lifetime = min(MAX_LIFETIME_MS, remaining)
        return self._seal_and_send(fields, unix, lifetime, task_state)

    def _seal_and_send(self, fields: dict, unix: int, lifetime: int, decision: str) -> dict:
        message = {
            "schema_version": SCHEMA,
            "controller_id": self._credentials.controller_id,
            "boot_nonce": fields["boot_nonce"],
            "session_id": fields["session_id"],
            "sequence": fields["sequence"],
            "freshness_id": fields["freshness_id"],
            "rearm_generation": fields["rearm_generation"],
            "status_challenge": fields["status_challenge"],
            "issued_at_ms": unix,
            "expires_at_ms": unix + lifetime,
            "task_state": fields["task_state"],
            "linear_mps": fields["linear_mps"],
            "angular_rad_s": fields["angular_rad_s"],
            "stop": fields["stop"],
        }
        # Section 3 "Linear and angular motion": stop true means both velocities 0.
        if message["stop"] is True and (message["linear_mps"] != 0.0 or message["angular_rad_s"] != 0.0):
            raise AssertionError("a stop datagram carried non-zero velocity")
        message["mac"] = sign_command(self._credentials.key, message)
        errors = validate_motor(message)
        if errors:
            raise AssertionError(f"publisher built an invalid motor command: {errors}")
        self.last_decision = decision
        LOG.info(
            "send %s seq=%s stop=%s lin=%.6f ang=%.6f (%s)",
            message["task_state"],
            message["sequence"],
            message["stop"],
            message["linear_mps"],
            message["angular_rad_s"],
            decision,
        )
        self._send(encode_command(message))
        return message


def encode_command(message: dict) -> bytes:
    """JSON bytes for the wire. Velocities are already canonical."""
    return json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
