"""Fixed numeric features for a later classifier.

The vector uses the 51 Face Landmarker blendshapes other than ``_neutral``,
then yaw, pitch, and roll. For each of those 54 channels the order is:

1. arithmetic mean
2. population standard deviation (divisor ``N``, so one frame has deviation 0)
3. newest value minus oldest value

Nothing here is fit on a dataset. The dimension does not depend on who is in
the window or on which session produced it. ``_neutral`` is not an input.
"""

from __future__ import annotations

import math
from typing import Sequence

from ml.schema import MODEL_BLENDSHAPES, published_angles
from ml.windows import AssembledWindow

_CHANNELS = MODEL_BLENDSHAPES + ("yaw_deg", "pitch_deg", "roll_deg")
_STATS = ("mean", "std", "delta")
FEATURE_NAMES = tuple(f"{channel}.{stat}" for channel in _CHANNELS for stat in _STATS)
FEATURE_DIM = len(FEATURE_NAMES)


def named_features(vector: Sequence[float]) -> tuple[tuple[str, float], ...]:
    """Pair a vector with ``FEATURE_NAMES``. Rejects a different length."""
    if len(vector) != FEATURE_DIM:
        raise ValueError(f"feature vector length is {len(vector)}, not {FEATURE_DIM}")
    return tuple((name, float(value)) for name, value in zip(FEATURE_NAMES, vector))


def extract_features(window: AssembledWindow) -> tuple[float, ...] | None:
    """Return the 162-length vector, or None when the window is not actionable.

    A non-actionable window does not produce a partial vector.
    """
    if not isinstance(window, AssembledWindow) or not window.actionable:
        return None
    if window.frame_count < 1 or len(window.frames) != window.frame_count:
        return None
    blend_series = []
    for name in MODEL_BLENDSHAPES:
        blend_series.append(tuple(float(frame["features"][name]) for frame in window.frames))
    angle_series = list(zip(*(published_angles(frame) for frame in window.frames)))
    values: list[float] = []
    for samples in (*blend_series, *angle_series):
        values.extend(_summarize(samples))
    if len(values) != FEATURE_DIM or any(not math.isfinite(value) for value in values):
        return None
    return tuple(values)


def _summarize(samples: tuple[float, ...]) -> tuple[float, float, float]:
    count = len(samples)
    mean = sum(samples) / count
    variance = sum((sample - mean) ** 2 for sample in samples) / count
    return (mean, math.sqrt(variance), samples[-1] - samples[0])
