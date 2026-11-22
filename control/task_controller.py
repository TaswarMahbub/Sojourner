"""Task controller: turns cues into a motion REQUEST. It never decides what is ALLOWED.

This is the gesture-to-velocity mapping that docs/contracts.md "Messages"
says "requires a later reviewed change". Because it can lead to non-zero
motor commands, it needs the Hardware/ML safety review before merge.

Why this layer can be wrong and the system is still safe:

This layer is pure policy. It decides what motion to request and produces
at most one control.publisher.MotionRequest per tick. It hands that request
to Publisher.tick(), and Publisher.tick() alone decides whether anything
moves. The publisher already enforces the session handshake, the status
gate, the rearm generation and both latches, the freshness binding, the
perception staleness rules, the reviewed-label rule, the velocity ranges
and every other stop condition (docs/contracts.md section 3, section 4 and
"Publisher duties before motion exists"). Nothing in this module or in
cues.py, task.py, tiers.py or confidence.py builds, seals or sends a
datagram, touches the status gate, or calls any publisher method except
tick(). A request that is wrong can only be refused; it cannot bypass a
check. MotionRequest itself refuses a velocity outside the section 3
ranges or a non-canonical value at construction.

What this layer adds on top, all of it in the direction of stopping:

* Stop on loss. A missing, invalid, stale or `unknown` gesture, a label
  not in the list for its exact model_version (control/cues.py), or a
  missing, non-actionable or stale camera frame is a Stop cue, so the task
  itself returns to Hold. Without this the task would still be Go when the
  signal came back and the chair would resume without a new cue.
* Stop when confidence falls below the floor (control/confidence.py), and
  cap both velocities by the confidence factor otherwise.
* Hold after any stop the publisher makes. If a requested motion is not
  sent as that motion (the publisher sent a stop, an estop, a rearm, or
  nothing), the task returns to Hold. Motion never resumes on its own after
  a stop; the user has to cue again.

Order inside one tick (project report, arbitration): loss and confidence
stops are added to the same cue list as the user's cues and arbitrated
there, so a Stop from any source beats everything else in that tick.

A gesture message is read for a label cue once, not on every tick it stays
the newest. A camera frame is read by the tier once, when its timestamp is
newer than the last one read.

Go lockout after an uncued stop
-------------------------------
With the context-dependent stop channel (control/cue_map.py: the jaw is Go
in Hold, Stop while moving), the meaning of a press depends on the TASK
state, not on what the user sees. After a stop the user did not cue, the
task is already Hold while the chair may still be visibly slowing, and a
jaw press meant as "stop!" would read as Go. So after every uncued stop:

* signal loss (missing, invalid, stale or unknown gesture or frame),
* confidence below the stop floor,
* the publisher not sending a requested motion as motion (a status-gate
  refusal, a receiver fault or estop latch, an operator estop or rearm,
  under 1 ms of challenge lifetime, nothing sent),

the controller starts a lockout of GO_LOCKOUT_MS. The lockout is refreshed
on every tick the condition holds, so it runs GO_LOCKOUT_MS past the last
lost tick, and it starts even when the task was already Hold: the user
cannot tell from the chair whether the system has them as stopped. During
the lockout:

* the tier reader is told the task is moving, so the stop channel's
  context-dependent binding resolves to Stop, never Go. A "stop!" press is
  honoured as Stop (in Hold it changes nothing but clears any pending
  turn);
* any Go or Confirm cue, from any source, is dropped and recorded in
  last_suppressed, so nothing can take the task out of Hold (Go starts
  motion; Confirm could start a proposed Through);
* the task is Hold with the default speed and no pending turn or proposal,
  both when the lockout starts and when it ends, so a turn or speed change
  queued before or during it (an incidental tilt) does not survive it.

GO_LOCKOUT_MS = 2500, PROVISIONAL. It has to cover the time from the uncued
stop to the end of a reactive "stop!" press:

    250 ms   the longest a command stays applied after the challenge that
             authorised it (docs/contracts.md section 3, "Clocks"), so the
             wheels are commanded safe within 250 ms of the last motion
             datagram
  + 2262 ms  2 x 1131 ms, the slowest valid latency of any bound gesture in
             probe session 20261122T073621Z (turn_left; the jaw's slowest
             was 464 ms). Doubled because those were WARNED reactions, with
             a 1 s lead-in, and a reaction to an unexpected stop is
             unwarned. The factor 2 is a judgement, not a measurement.
  = 2512 ms, rounded to 2500.

Physical deceleration is not in it: no bench measurement exists. The number
should be replaced by a measured unwarned reaction to an uncued stop.
"""

from __future__ import annotations

import math

from control.confidence import ConfidenceGovernor, signal_confidence
from control.cues import CONFIRM, GO, NEUTRAL, STOP, UNKNOWN, Cue, classify_label
from control.publisher import (
    ANGULAR_LIMIT,
    LINEAR_LIMIT,
    PERCEPTION_MAX_AGE_MS,
    MotionRequest,
    Publisher,
    is_actionable_frame,
)
from control.task import HOLD, Task, apply_cues, canonical, consume_turn, velocity
from control.tiers import Profile, reader_for
from tests.contract_schema import validate_gesture

# Section 3 "Task state": the message already combines operator and assist
# input. The user picks the task, this layer steers it.
REQUEST_TASK_STATE = "shared"

# See "Go lockout after an uncued stop" above. PROVISIONAL.
GO_LOCKOUT_MS = 2500
# Cues that can take the task out of Hold; dropped during the lockout.
STARTING_CUES = frozenset({GO, CONFIRM})


def _floor6(value: float) -> float:
    """Round toward zero to six digits, so a capped value never exceeds its cap."""
    return canonical(math.copysign(math.floor(abs(value) * 1_000_000) / 1_000_000, value))


def signal_loss(gesture, frame, now_ms: int) -> str | None:
    """Why the input counts as lost this tick, or None.

    Mirrors "Publisher duties before motion exists" and section 2 "Label",
    so the task returns to Hold on the same conditions that make the
    publisher stop.
    """
    if frame is None:
        return "camera frame missing"
    if not is_actionable_frame(frame):
        return "camera frame not actionable"
    age = now_ms - frame["timestamp_ms"]
    if age > PERCEPTION_MAX_AGE_MS or age < 0:
        return "camera frame stale"
    if gesture is None:
        return "gesture missing"
    if validate_gesture(gesture):
        return "gesture invalid"
    if classify_label(gesture["model_version"], gesture["label"]) == UNKNOWN:
        return f"gesture label {gesture['label']!r} unknown for model_version {gesture['model_version']!r}"
    age = now_ms - gesture["window_end_timestamp_ms"]
    if age > PERCEPTION_MAX_AGE_MS or age < 0:
        return "gesture stale"
    return None


def cap_velocity(linear: float, angular: float, factor: float) -> tuple[float, float]:
    """Apply the confidence cap to both velocities (control/confidence.py)."""
    if not 0.0 <= factor <= 1.0:
        raise ValueError("cap factor must lie in [0, 1]")
    linear = _floor6(math.copysign(min(abs(linear), factor * LINEAR_LIMIT), linear))
    angular = _floor6(math.copysign(min(abs(angular), factor * ANGULAR_LIMIT), angular))
    return linear, angular


def plan(task: Task, cues: list[Cue], factor: float) -> tuple[Task, MotionRequest | None]:
    """Apply one tick's cues, then build the request the new task asks for.

    Hold, a zero cap, or a zero velocity give None: the publisher then sends
    its default stop. Anything returned is already inside section 3's ranges
    because MotionRequest refuses anything else.
    """
    task = apply_cues(task, cues)
    if task.kind == HOLD or factor == 0.0:
        return task, None
    linear, angular = cap_velocity(*velocity(task), factor)
    if linear == 0.0 and angular == 0.0:
        return task, None
    return task, MotionRequest(REQUEST_TASK_STATE, linear, angular)


class TaskController:
    """One user's task, tier reader and confidence governor.

    The profile is bound at construction because the tier reader keeps
    state between frames (hysteresis, Tier 1 excursions, the Tier 3 scan
    clock). The current task is `self.task`; step() takes the newest
    gesture and frame and returns the optional MotionRequest for
    Publisher.tick().
    """

    def __init__(self, profile: Profile | dict, cue_map: dict[str, str] | None = None, *, task: Task | None = None, **scan_options):
        self.profile = profile if isinstance(profile, Profile) else Profile.from_dict(profile)
        self.reader = reader_for(self.profile, cue_map, **scan_options)
        self.governor = ConfidenceGovernor()
        self.task = Task() if task is None else task
        self.last_cues: list[Cue] = []
        self.last_reason: str = "start"
        self._last_ms: int | None = None
        self._last_angular = 0.0
        self._last_gesture_key = None
        self._last_frame_ms: int | None = None
        self._lockout_until: int | None = None
        self.last_suppressed: list[Cue] = []

    def locked(self, now_ms: int) -> bool:
        """True while a Go lockout is running at now_ms."""
        return self._lockout_until is not None and now_ms < self._lockout_until

    def lockout_remaining_ms(self, now_ms: int) -> int:
        return max(0, self._lockout_until - now_ms) if self.locked(now_ms) else 0

    def _uncued_stop(self, now_ms: int, reason: str) -> None:
        """Hold with defaults, and start or refresh the Go lockout."""
        self.task = Task(kind=HOLD)
        self._last_angular = 0.0
        self._lockout_until = now_ms + GO_LOCKOUT_MS
        self.last_reason = f"{reason}; Go locked out for {GO_LOCKOUT_MS} ms"

    def step(self, gesture, frame, now_ms: int) -> MotionRequest | None:
        """The optional MotionRequest for this tick. `now_ms` is the publisher's Unix clock."""
        if self._last_ms is not None:
            self.task = consume_turn(self.task, self._last_angular, now_ms - self._last_ms)
        if self._lockout_until is not None and not self.locked(now_ms):
            # The lockout has ended: nothing queued during it survives.
            self._lockout_until = None
            self.task = Task(kind=HOLD)
        cues: list[Cue] = []
        factor = 0.0
        loss = signal_loss(gesture, frame, now_ms)
        if loss is not None:
            cues.append(Cue(STOP, "signal"))
            self.reader.reset()
            self.governor.lose(now_ms)
            self._uncued_stop(now_ms, loss)
        else:
            key = (gesture["timestamp_ms"], gesture["window_end_timestamp_ms"], gesture["label"], gesture["model_version"])
            if key != self._last_gesture_key:
                self._last_gesture_key = key
                label = classify_label(gesture["model_version"], gesture["label"])
                if label != NEUTRAL:
                    cues.append(Cue(label, "label"))
            if self._last_frame_ms is None or frame["timestamp_ms"] > self._last_frame_ms:
                self._last_frame_ms = frame["timestamp_ms"]
                moving = self.task.kind != HOLD or self.locked(now_ms)
                cues.extend(self.reader.read(frame, now_ms, moving=moving))
            factor = self.governor.update(signal_confidence(gesture, frame), now_ms)
            if factor == 0.0:
                cues.append(Cue(STOP, "confidence"))
                self.reader.reset()
                self._uncued_stop(now_ms, "confidence below the stop floor")
            elif self.locked(now_ms):
                self.last_reason = f"Go locked out for {self.lockout_remaining_ms(now_ms)} ms more"
            else:
                self.last_reason = "ok"
        self.last_suppressed = []
        if self.locked(now_ms):
            self.last_suppressed = [c for c in cues if c.kind in STARTING_CUES]
            cues = [c for c in cues if c.kind not in STARTING_CUES]
        self.task, request = plan(self.task, cues, factor)
        if self.locked(now_ms) and (self.task.kind != HOLD or request is not None):
            raise AssertionError("the task left Hold during a Go lockout")
        self.last_cues = cues
        self._last_ms = now_ms
        self._last_angular = 0.0 if request is None else request.angular_rad_s
        return request

    def note_published(self, request: MotionRequest | None, message: dict | None, decision: str = "") -> None:
        """After Publisher.tick(): if the requested motion was not sent as motion, return to Hold."""
        if request is None:
            return
        sent = (
            message is not None
            and message["stop"] is False
            and message["task_state"] == request.task_state
            and message["linear_mps"] == request.linear_mps
            and message["angular_rad_s"] == request.angular_rad_s
        )
        if not sent:
            self._uncued_stop(self._last_ms, f"publisher stopped: {decision or 'no motion datagram'}")

    def tick(self, publisher: Publisher, gesture, frame, *, operator_estop: bool = False, operator_rearm: bool = False):
        """step(), then Publisher.tick() with the request, then note_published(). Returns (request, message).

        The caller still feeds the same gesture and frame to
        publisher.observe_gesture() and observe_frame(); the publisher keeps
        its own copy and makes its own decision.
        """
        request = self.step(gesture, frame, publisher.clock.unix_ms())
        message = publisher.tick(motion=request, operator_estop=operator_estop, operator_rearm=operator_rearm)
        self.note_published(request, message, publisher.last_decision)
        return request, message
