"""The motion task state machine: the user decides, the system steers.

Project report, collaborative autonomy model. The model does NOT steer. The
chair is always executing a current motion task. The default task is Hold,
which is stopped. A cue replaces or modifies the active task, and that task
then runs on its own until the next cue. Nobody holds a position to keep
moving: a Go cue keeps the chair moving on every later tick with no further
input, until a Stop, a Cancel, or any stop condition returns it to Hold.

Tasks (project report, cue vocabulary):

* Hold: stopped. The default, and the state after any stop. Both
  velocities are exactly 0 and nothing is requested.
* Go: forward at the current speed setting.
* Through: a committed straight run at the current heading at reduced
  speed, until Cancel or Stop. It is NOT doorway detection. This build has
  no obstacle sensing, so it cannot see a doorway, a wall or a person. It
  only stops listening to steering and speed cues so that a straight line
  is held. It starts only after Confirm.

Modifiers:

* Left / Right add one fixed heading increment (TURN_INCREMENT_RAD) and the
  previous task continues. The increment is executed as a turn at
  TURN_RATE_RAD_S while the task is moving, and then the angular velocity
  returns to 0. It is an increment, not a continuous turn. In Hold the
  increment is kept and executed when Go starts, so Hold never moves. There
  is no heading sensor: the increment is open loop, commanded yaw rate
  times elapsed time, and may overshoot by at most one tick of TURN_RATE.
* Slower / Faster step the speed setting by SPEED_STEP_MPS, clamped to
  [SPEED_MIN_MPS, SPEED_MAX_MPS]. SPEED_MAX_MPS is the contract's linear
  limit, so no number of Faster cues can exceed it.
* Confirm accepts the pending proposal (only Through proposes). Cancel
  drops any proposal and returns to Hold.

docs/contracts.md section 3 "Linear and angular motion": linear in
[-0.30, 0.30] m/s, angular in [-1.00, 1.00] rad/s, canonical six-digit
values, no negative zero. This module only ever produces linear >= 0
(no cue asks for reverse). REP-103: positive angular is a left turn.

This module is pure policy and holds no clocks or sockets. It never builds
a datagram. Its output goes through control/publisher.py, which decides
whether motion is allowed at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from control.cues import CANCEL, CONFIRM, FASTER, GO, LEFT, RIGHT, SLOWER, STOP, THROUGH, Cue, arbitrate
from control.publisher import ANGULAR_LIMIT, LINEAR_LIMIT, MAX_LIFETIME_MS

HOLD = "hold"
GOING = "go"
THROUGH_RUN = "through"
TASKS = (HOLD, GOING, THROUGH_RUN)

# Speed setting (provisional values, inside section 3's linear range).
SPEED_MIN_MPS = 0.05
SPEED_STEP_MPS = 0.05
SPEED_DEFAULT_MPS = 0.10
SPEED_MAX_MPS = LINEAR_LIMIT
# Through runs at the lower of the speed setting and this value.
THROUGH_SPEED_MPS = 0.10

# Heading increment (provisional): 15 degrees per Left or Right, executed at
# 0.5 rad/s, so one increment takes about 0.52 s. At most four increments
# (60 degrees) can be pending in either direction.
TURN_INCREMENT_RAD = 0.261799
TURN_RATE_RAD_S = 0.50
MAX_PENDING_TURN_RAD = 1.047196  # 4 * TURN_INCREMENT_RAD, written canonically

assert 0.0 < SPEED_MIN_MPS <= SPEED_DEFAULT_MPS <= SPEED_MAX_MPS <= LINEAR_LIMIT
assert 0.0 < THROUGH_SPEED_MPS <= LINEAR_LIMIT
assert 0.0 < TURN_RATE_RAD_S <= ANGULAR_LIMIT


def canonical(value: float) -> float:
    """The six-digit value section 3 requires, with negative zero folded to 0.0."""
    rounded = round(value, 6)
    return 0.0 if rounded == 0.0 else rounded


assert MAX_PENDING_TURN_RAD == canonical(4 * TURN_INCREMENT_RAD)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Task:
    """The active motion task.

    `speed_mps` is the user's speed setting. It survives a stop, so Go after
    a stop resumes at the setting the user chose. `turn_rad` is the heading
    increment still to execute (positive is left). `proposal` is a pending
    action waiting for Confirm, or None.
    """

    kind: str = HOLD
    speed_mps: float = SPEED_DEFAULT_MPS
    turn_rad: float = 0.0
    proposal: str | None = None

    def __post_init__(self):
        if self.kind not in TASKS:
            raise ValueError(f"not a task: {self.kind!r}")
        if not SPEED_MIN_MPS <= self.speed_mps <= SPEED_MAX_MPS:
            raise ValueError("speed setting is outside the envelope")
        if not -MAX_PENDING_TURN_RAD <= self.turn_rad <= MAX_PENDING_TURN_RAD:
            raise ValueError("pending turn is outside the allowed increment")
        if self.proposal not in (None, THROUGH):
            raise ValueError("only Through can be proposed")


def stopped(task: Task) -> Task:
    """Hold: the state after any stop. The speed setting is kept; the pending
    turn and any proposal are discarded so nothing resumes on its own."""
    return Task(kind=HOLD, speed_mps=task.speed_mps)


def _speed_from_magnitude(magnitude: float) -> float:
    """Tier 1 proportional: fraction of the envelope, to 0.01 m/s, clamped."""
    return _clamp(round(magnitude * SPEED_MAX_MPS, 2), SPEED_MIN_MPS, SPEED_MAX_MPS)


def apply_cue(task: Task, cue: Cue) -> Task:
    """One cue against the active task. See the module docstring for each rule."""
    kind = cue.kind
    if kind in (STOP, CANCEL):
        return stopped(task)
    if kind == CONFIRM:
        if task.proposal == THROUGH:
            # Commit to the current heading: any pending increment is dropped.
            return replace(task, kind=THROUGH_RUN, turn_rad=0.0, proposal=None)
        return task
    if task.kind == THROUGH_RUN:
        # Committed straight run: only Stop and Cancel end it.
        return task
    if kind == GO:
        speed = task.speed_mps if cue.magnitude is None else _speed_from_magnitude(cue.magnitude)
        return replace(task, kind=GOING, speed_mps=speed, proposal=None)
    if kind in (LEFT, RIGHT):
        sign = 1.0 if kind == LEFT else -1.0
        step = TURN_INCREMENT_RAD * (1.0 if cue.magnitude is None else cue.magnitude)
        turn = _clamp(canonical(task.turn_rad + sign * step), -MAX_PENDING_TURN_RAD, MAX_PENDING_TURN_RAD)
        return replace(task, turn_rad=turn)
    if kind in (SLOWER, FASTER):
        sign = 1.0 if kind == FASTER else -1.0
        speed = _clamp(round(task.speed_mps + sign * SPEED_STEP_MPS, 2), SPEED_MIN_MPS, SPEED_MAX_MPS)
        return replace(task, speed_mps=speed)
    if kind == THROUGH:
        return replace(task, proposal=THROUGH)
    raise AssertionError(f"unhandled cue {kind!r}")


def apply_cues(task: Task, cues: list[Cue]) -> Task:
    """Arbitrate one tick's cues (control/cues.py) and apply the survivors in order."""
    for cue in arbitrate(cues):
        task = apply_cue(task, cue)
    return task


def velocity(task: Task) -> tuple[float, float]:
    """The velocity the task asks for before any confidence cap.

    Hold is exactly (0.0, 0.0). Go is the speed setting, turning at
    TURN_RATE_RAD_S only while an increment is pending. Through is straight
    at the reduced speed.
    """
    if task.kind == HOLD:
        return 0.0, 0.0
    if task.kind == THROUGH_RUN:
        return canonical(min(task.speed_mps, THROUGH_SPEED_MPS)), 0.0
    angular = 0.0
    if task.turn_rad != 0.0:
        angular = math.copysign(TURN_RATE_RAD_S, task.turn_rad)
    return canonical(task.speed_mps), canonical(angular)


def consume_turn(task: Task, applied_angular: float, elapsed_ms: int) -> Task:
    """Subtract the heading the previous request turned through.

    `applied_angular` is the angular velocity that was requested on the
    previous tick and `elapsed_ms` the time since. The elapsed time is capped
    at the command lifetime (section 3 "Clocks", 250 ms), because the
    receiver stops applying a command after that. Open loop: there is no
    heading measurement in this build.
    """
    if task.turn_rad == 0.0 or applied_angular == 0.0 or elapsed_ms <= 0:
        return task
    if math.copysign(1.0, applied_angular) != math.copysign(1.0, task.turn_rad):
        return task
    turned = abs(applied_angular) * min(elapsed_ms, MAX_LIFETIME_MS) / 1000.0
    remaining = abs(task.turn_rad) - turned
    if remaining <= 0.0:
        return replace(task, turn_rad=0.0)
    return replace(task, turn_rad=canonical(math.copysign(remaining, task.turn_rad)))
