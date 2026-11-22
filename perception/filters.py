"""Per-channel filtering of the camera feature stream, downstream of publishing.

The published camera feature stream (message 1 of docs/contracts.md) is the
raw Face Landmarker output and stays raw: the frontend, the backend and the
classifier consume it as published. This module is a separate stage that
calibration and shared control apply to that stream after receiving it.

A channel is one scalar per tracking frame: a blendshape score (unitless,
[0, 1]), a head pose angle in degrees (yaw_deg, pitch_deg, roll_deg), or a
head translation in centimeters (x_cm, y_cm, z_cm, the last column of the
facial transformation matrix). See channel_values.

Each channel runs a causal moving median followed by a one-pole low-pass.
The median removes single-frame spikes without smearing them; the one-pole
removes the remaining frame-to-frame jitter.

Gap handling
------------
Only frames whose tracking_state is "tracking" update filter state. A
"partial" or "lost" frame is skipped entirely: it is not fed as 0.0, not
fed as the last good value, and a channel missing from a frame is never
read as 0.0. Feeding any substitute value would put a number into the
filter that the camera never measured, and the filter would then present
it as signal.

When the interval between two consecutive tracking frames exceeds
reset_after_ms (default 200 ms), the channel filter resets and starts again
from the post-gap sample instead of resuming. A one-pole that resumes
across a 450 ms dropout blends the pre-gap and post-gap values into a slow
ramp, and that ramp looks exactly like a deliberate gesture to a threshold
downstream. A 450 ms dropout has been measured during a fast head turn, so
this is a real case, not a hypothetical.

Until a channel's median window is full, its output is marked not valid
and its value is None. Nothing is emitted half-warmed. When the window
fills, the one-pole is seeded with the first median output rather than
with 0.0, so a fresh filter does not ramp up from zero either.

Standard library only.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median

DEFAULT_RESET_AFTER_MS = 200.0

POSE_ANGLE_CHANNELS = ("yaw_deg", "pitch_deg", "roll_deg")
TRANSLATION_CHANNELS = ("x_cm", "y_cm", "z_cm")
POSE_CHANNELS = POSE_ANGLE_CHANNELS + TRANSLATION_CHANNELS


# --- Channels ----------------------------------------------------------------


def channel_values(message: dict) -> dict[str, float]:
    """Every channel value carried by a camera feature message.

    Blendshape scores are taken from features as published. When head_pose
    is present, yaw_deg, pitch_deg and roll_deg come from it directly, and
    x_cm, y_cm, z_cm are matrix[0][3], matrix[1][3], matrix[2][3]. Channels
    the message does not carry are absent from the result, never 0.0.
    """
    values = {name: float(score) for name, score in message["features"].items()}
    pose = message["head_pose"]
    if pose is not None:
        for name in POSE_ANGLE_CHANNELS:
            values[name] = float(pose[name])
        matrix = pose["matrix"]
        for row, name in enumerate(TRANSLATION_CHANNELS):
            values[name] = float(matrix[row][3])
    return values


def is_tracking(message: dict) -> bool:
    """True only for a frame whose tracking_state is "tracking"."""
    return message["tracking_state"] == "tracking"


# --- Stages ------------------------------------------------------------------


class MovingMedian:
    """Causal moving median over the last `window` samples.

    window must be a positive odd integer, so the median is always one of
    the samples. window 1 is a pass-through. A single-sample spike is
    removed by any window of 3 or more; an excursion lasting (window+1)/2
    samples or longer passes through.
    """

    def __init__(self, window: int) -> None:
        if not isinstance(window, int) or window < 1 or window % 2 == 0:
            raise ValueError(f"median window must be a positive odd integer, got {window!r}")
        self.window = window
        self._samples: deque[float] = deque(maxlen=window)

    @property
    def full(self) -> bool:
        """True once `window` samples have been seen since the last reset."""
        return len(self._samples) == self.window

    def update(self, value: float) -> float | None:
        """Add one sample. Returns the median, or None until the window is full."""
        self._samples.append(value)
        if not self.full:
            return None
        return median(self._samples)

    def reset(self) -> None:
        """Forget every sample."""
        self._samples.clear()


def one_pole_alpha(cutoff_hz: float, rate_hz: float) -> float:
    """Smoothing factor of a one-pole low-pass, in (0, 1).

    alpha = 1 - exp(-2*pi*cutoff_hz/rate_hz). This is the exact
    discretization of a continuous RC low-pass with time constant
    tau = 1/(2*pi*cutoff_hz) seconds sampled every 1/rate_hz seconds: after
    k samples the step response is 1 - exp(-k/(tau*rate_hz)), so it
    reaches 1 - 1/e, about 63.2 percent, after one time constant.

    cutoff_hz must be positive and below the Nyquist frequency rate_hz/2.
    """
    if rate_hz <= 0:
        raise ValueError(f"rate_hz must be positive, got {rate_hz!r}")
    if not 0 < cutoff_hz < rate_hz / 2:
        raise ValueError(f"cutoff_hz must be in (0, {rate_hz / 2}) for rate {rate_hz} Hz, got {cutoff_hz!r}")
    return 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / rate_hz)


class OnePoleLowPass:
    """One-pole low-pass: y[n] = y[n-1] + alpha * (x[n] - y[n-1]).

    alpha comes from one_pole_alpha(cutoff_hz, rate_hz). Samples are
    treated as uniformly spaced at rate_hz. The first sample after
    construction or reset seeds the state and is returned unchanged, so the
    output never ramps up from an assumed 0.0.
    """

    def __init__(self, cutoff_hz: float, rate_hz: float) -> None:
        self.cutoff_hz = cutoff_hz
        self.rate_hz = rate_hz
        self.alpha = one_pole_alpha(cutoff_hz, rate_hz)
        self._state: float | None = None

    def update(self, value: float) -> float:
        """Add one sample and return the filtered value, in the input's unit."""
        if self._state is None:
            self._state = value
        else:
            self._state += self.alpha * (value - self._state)
        return self._state

    def reset(self) -> None:
        """Forget the state; the next sample seeds it again."""
        self._state = None


# --- Channel filter ----------------------------------------------------------


@dataclass(frozen=True)
class ChannelConfig:
    """Filter settings for one channel.

    median_window: odd number of frames, 1 for no median.
    cutoff_hz: one-pole cutoff in hertz, or None for no one-pole.
    """

    median_window: int = 1
    cutoff_hz: float | None = None


@dataclass(frozen=True)
class FilteredValue:
    """One channel's output for one frame.

    value is in the channel's own unit, or None when valid is False. valid
    is False for a non-tracking frame, a channel the frame did not carry,
    and every sample before the median window has filled.
    """

    value: float | None
    valid: bool


INVALID = FilteredValue(None, False)


class ChannelFilter:
    """A moving median followed by a one-pole low-pass, with gap reset.

    Call update() for tracking samples only. If timestamp_ms is more than
    reset_after_ms after the previous accepted sample, both stages reset
    before the sample is used, so no pre-gap value reaches the output. See
    the module docstring for why.
    """

    def __init__(self, config: ChannelConfig, rate_hz: float, reset_after_ms: float = DEFAULT_RESET_AFTER_MS) -> None:
        if reset_after_ms <= 0:
            raise ValueError(f"reset_after_ms must be positive, got {reset_after_ms!r}")
        self.config = config
        self.reset_after_ms = reset_after_ms
        self._median = MovingMedian(config.median_window)
        self._low_pass = None if config.cutoff_hz is None else OnePoleLowPass(config.cutoff_hz, rate_hz)
        self._last_ms: int | None = None
        self.resets = 0

    def reset(self) -> None:
        """Drop all state, as if newly constructed."""
        self._median.reset()
        if self._low_pass is not None:
            self._low_pass.reset()
        self._last_ms = None

    def gap_exceeded(self, timestamp_ms: int) -> bool:
        """True if timestamp_ms is more than reset_after_ms past the last sample.

        Raises ValueError if timestamp_ms does not increase.
        """
        if self._last_ms is None:
            return False
        if timestamp_ms <= self._last_ms:
            raise ValueError(f"timestamp_ms must increase: {timestamp_ms} after {self._last_ms}")
        return timestamp_ms - self._last_ms > self.reset_after_ms

    def update(self, timestamp_ms: int, value: float) -> FilteredValue:
        """Filter one tracking sample captured at timestamp_ms (Unix epoch ms)."""
        if self.gap_exceeded(timestamp_ms):
            self.reset()
            self.resets += 1
        self._last_ms = timestamp_ms
        median_value = self._median.update(value)
        if median_value is None:
            return INVALID
        if self._low_pass is None:
            return FilteredValue(median_value, True)
        return FilteredValue(self._low_pass.update(median_value), True)


# --- Bank --------------------------------------------------------------------


class FilterBank:
    """Applies a per-channel ChannelConfig to whole camera feature messages.

    configs maps channel name (see channel_values) to its ChannelConfig.
    process() returns one FilteredValue per configured channel. A frame
    that is not "tracking" leaves every channel's state untouched and
    returns INVALID for all of them. A configured channel absent from a
    tracking frame is left untouched and returned INVALID.
    """

    def __init__(self, configs: dict[str, ChannelConfig], rate_hz: float, reset_after_ms: float = DEFAULT_RESET_AFTER_MS) -> None:
        self.rate_hz = rate_hz
        self.filters = {name: ChannelFilter(config, rate_hz, reset_after_ms) for name, config in configs.items()}

    def process(self, message: dict) -> dict[str, FilteredValue]:
        if not is_tracking(message):
            return {name: INVALID for name in self.filters}
        values = channel_values(message)
        timestamp_ms = message["timestamp_ms"]
        output = {}
        for name, channel in self.filters.items():
            if name in values:
                output[name] = channel.update(timestamp_ms, values[name])
            else:
                output[name] = INVALID
        return output


# --- Latency -----------------------------------------------------------------


def median_delay_ms(window: int, rate_hz: float) -> float:
    """Group delay of a causal moving median: (window - 1)/2 frames, in ms."""
    return (window - 1) / 2 * 1000.0 / rate_hz


def one_pole_delay_ms(cutoff_hz: float | None) -> float:
    """Approximate group delay of a one-pole low-pass, in ms.

    1/(2*pi*cutoff_hz) seconds, the filter's time constant, which is its
    group delay at frequencies well below the cutoff. 0 when cutoff_hz is
    None. It does not depend on the frame rate.
    """
    if cutoff_hz is None:
        return 0.0
    return 1000.0 / (2.0 * math.pi * cutoff_hz)


def group_delay_ms(config: ChannelConfig, rate_hz: float) -> dict[str, float]:
    """Latency added by a ChannelConfig at rate_hz frames per second.

    Returns milliseconds for each stage and their sum:
    {"median_ms": ..., "one_pole_ms": ..., "total_ms": ...}.
    """
    median_ms = median_delay_ms(config.median_window, rate_hz)
    one_pole_ms = one_pole_delay_ms(config.cutoff_hz)
    return {"median_ms": median_ms, "one_pole_ms": one_pole_ms, "total_ms": median_ms + one_pole_ms}
