"""Speed cap from signal confidence. PROVISIONAL: these numbers are not tuned.

Project report: scale the speed cap with signal confidence, and stop on
loss. Low confidence must never raise the cap.

Which number is "signal confidence". docs/contracts.md section 2
"Confidence": the gesture message's `confidence` is the classifier's score
for its label, in [0, 1], and it is not a calibrated probability; `0` means
the producer assigned no score. Section 1 "Confidence": a camera frame's
`confidence` is null unless the detector really produced a score. The cap
uses the gesture score, and when the frame carries a number too, the lower
of the two. A null frame score adds nothing; it is not read as 0 or 1. An
`unknown` label is a loss whatever its score (section 2: its score must not
override the label), and control/task_controller.py stops on it before this
module is consulted.

The curve, factor f of the contract limits (section 3: 0.30 m/s, 1.00 rad/s):

    confidence c          factor f
    c <  0.40             0      loss: the controller stops, task -> Hold
    0.40 <= c < 0.90      0.25 + 0.75 * (c - 0.40) / 0.50   (linear)
    c >= 0.90             1.0

Why this shape:

* A floor below which the controller stops rather than crawls. A score that
  low means the input is not trustworthy, and slow motion on untrustworthy
  input is still motion the user may not have asked for. Section 2's "0 is
  no score" lands here as well, so an unscored producer cannot move.
* A linear ramp above the floor, starting at a quarter of the envelope, so
  the cap is never so small that the chair moves imperceptibly while the
  user believes it is stopped.
* Monotone non-decreasing in c, so a lower score can never give a higher
  cap.
* Asymmetric in time: a drop takes effect on the same tick, a rise is
  limited to RECOVERY_PER_S of the envelope per second. Flickering scores
  therefore cannot pump the cap upward, and a loss followed by recovery
  ramps back instead of jumping.

All four numbers are provisional, chosen for review, and must be revisited
against real classifier scores once a trained model and its score
distribution exist.
"""

from __future__ import annotations

import math

STOP_BELOW = 0.40
FULL_AT = 0.90
FLOOR_FACTOR = 0.25
RECOVERY_PER_S = 1.0


def factor_for(confidence: float) -> float:
    """The static curve above. Raises on a value outside [0, 1]."""
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        raise ValueError("confidence must be a finite number")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must lie in [0, 1]")
    if confidence < STOP_BELOW:
        return 0.0
    if confidence >= FULL_AT:
        return 1.0
    return FLOOR_FACTOR + (1.0 - FLOOR_FACTOR) * (confidence - STOP_BELOW) / (FULL_AT - STOP_BELOW)


def signal_confidence(gesture: dict, frame: dict | None) -> float:
    """The score the cap uses: the gesture score, or the lower of it and a numeric frame score."""
    score = float(gesture["confidence"])
    frame_score = frame.get("confidence") if isinstance(frame, dict) else None
    if isinstance(frame_score, (int, float)) and not isinstance(frame_score, bool):
        score = min(score, float(frame_score))
    return score


class ConfidenceGovernor:
    """Holds the current cap factor and applies the drop-now, rise-slowly rule."""

    def __init__(self):
        self.factor: float | None = None
        self._last_ms: int | None = None

    def update(self, confidence: float, now_ms: int) -> float:
        """Return the cap factor for this tick, in [0, 1].

        A target below the current factor is taken at once. A target above
        it is approached at RECOVERY_PER_S per second of elapsed time.
        """
        target = factor_for(confidence)
        if self.factor is None or self._last_ms is None or target <= self.factor:
            self.factor = target
        else:
            elapsed_s = max(0, now_ms - self._last_ms) / 1000.0
            self.factor = min(target, self.factor + RECOVERY_PER_S * elapsed_s)
        self._last_ms = now_ms
        return self.factor

    def lose(self, now_ms: int) -> float:
        """Signal loss: the cap falls to 0 and recovers only through update()."""
        self.factor = 0.0
        self._last_ms = now_ms
        return 0.0
