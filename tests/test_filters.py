"""Checks the downstream per-channel filter stage in perception/filters.py.

Standard library plus pytest. No camera, no model.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

from perception import filters
from perception.filters import (
    INVALID,
    ChannelConfig,
    ChannelFilter,
    FilterBank,
    MovingMedian,
    OnePoleLowPass,
    group_delay_ms,
)
from tests.contract_schema import BLENDSHAPES

REPO_ROOT = Path(__file__).resolve().parent.parent
T0_MS = 1795276800000
FRAME_MS = 50  # 20 frames per second
RATE_HZ = 20.0


def message(t_ms: int, state: str = "tracking", value: float = 0.05, yaw: float = 0.0) -> dict:
    """A minimal camera feature message in the shape filters reads.

    tracking: every blendshape at value plus a head pose. partial: half
    the blendshapes, no pose. lost: nothing.
    """
    if state == "tracking":
        matrix = [[1.0, 0.0, 0.0, 2.0], [0.0, 1.0, 0.0, -1.0], [0.0, 0.0, 1.0, -45.0], [0.0, 0.0, 0.0, 1.0]]
        pose = {"matrix": matrix, "yaw_deg": yaw, "pitch_deg": 0.0, "roll_deg": 0.0}
        return {"timestamp_ms": t_ms, "tracking_state": state, "features": dict.fromkeys(BLENDSHAPES, value), "head_pose": pose, "confidence": None}
    if state == "partial":
        features = dict.fromkeys(BLENDSHAPES[:26], 0.0)
        return {"timestamp_ms": t_ms, "tracking_state": state, "features": features, "head_pose": None, "confidence": None}
    return {"timestamp_ms": t_ms, "tracking_state": "lost", "features": {}, "head_pose": None, "confidence": None}


def run_median(window: int, signal: list[float]) -> list[float | None]:
    stage = MovingMedian(window)
    return [stage.update(x) for x in signal]


def feed(channel: ChannelFilter, samples: list[tuple[int, float]]):
    return [channel.update(t, x) for t, x in samples]


# --- Median ------------------------------------------------------------------


def test_median_of_three_removes_a_single_frame_spike():
    signal = [0.05] * 5 + [1.0] + [0.05] * 5
    out = run_median(3, signal)
    assert all(v == 0.05 for v in out[2:])


def test_median_of_three_passes_a_three_frame_excursion():
    signal = [0.05] * 5 + [1.0] * 3 + [0.05] * 5
    out = run_median(3, signal)
    assert max(v for v in out if v is not None) == 1.0
    assert out[6] == 1.0 and out[7] == 1.0


def test_median_window_one_is_pass_through():
    signal = [0.05, 1.0, 0.3, 0.0]
    assert run_median(1, signal) == signal


@pytest.mark.parametrize("window", [0, 2, 4, -1, 3.0])
def test_median_rejects_even_or_non_positive_windows(window):
    with pytest.raises(ValueError):
        MovingMedian(window)


# --- One-pole ----------------------------------------------------------------


def test_alpha_is_derived_from_cutoff_and_rate():
    assert filters.one_pole_alpha(2.0, 20.0) == pytest.approx(1 - math.exp(-2 * math.pi * 2.0 / 20.0))
    assert filters.one_pole_alpha(2.0, 20.0) != filters.one_pole_alpha(2.0, 30.0)


@pytest.mark.parametrize("cutoff, rate", [(0.0, 20.0), (-1.0, 20.0), (10.0, 20.0), (12.0, 20.0), (1.0, 0.0)])
def test_alpha_rejects_cutoffs_outside_zero_to_nyquist(cutoff, rate):
    with pytest.raises(ValueError):
        filters.one_pole_alpha(cutoff, rate)


def test_one_pole_step_reaches_63_percent_after_one_time_constant():
    # tau = 1/(2*pi*fc) = 0.25 s, which is exactly 5 frames at 20 Hz.
    rate_hz = 20.0
    tau_s = 0.25
    cutoff_hz = 1.0 / (2 * math.pi * tau_s)
    stage = OnePoleLowPass(cutoff_hz, rate_hz)
    stage.update(0.0)  # seed at the pre-step level
    out = [stage.update(1.0) for _ in range(round(tau_s * rate_hz))]
    # Tolerance 0.005 of the step around 1 - 1/e = 0.632.
    assert out[-1] == pytest.approx(1 - math.exp(-1), abs=0.005)
    assert 0.627 <= out[-1] <= 0.637


def test_one_pole_first_sample_seeds_state_instead_of_ramping_from_zero():
    stage = OnePoleLowPass(2.0, 20.0)
    assert stage.update(0.4) == 0.4
    assert stage.update(0.4) == 0.4


# --- Gaps --------------------------------------------------------------------


DIRECTIONAL = ChannelConfig(median_window=3, cutoff_hz=2.0)


def bank_output(bank: FilterBank, frames: list[dict], name: str = "jawOpen"):
    return [bank.process(frame)[name] for frame in frames]


def test_partial_and_lost_frames_leave_state_unchanged():
    values = [0.05, 0.07, 0.04, 0.30, 0.31]
    good = [message(T0_MS + i * FRAME_MS, value=v) for i, v in enumerate(values)]
    last = message(T0_MS + 7 * FRAME_MS, value=0.33)  # 100 ms after good[-1], under reset_after_ms
    bad = [message(T0_MS + 5 * FRAME_MS, "partial"), message(T0_MS + 6 * FRAME_MS, "lost")]

    clean = FilterBank({"jawOpen": DIRECTIONAL, "yaw_deg": DIRECTIONAL}, RATE_HZ)
    dirty = FilterBank({"jawOpen": DIRECTIONAL, "yaw_deg": DIRECTIONAL}, RATE_HZ)
    clean_out = [clean.process(f) for f in good + [last]]
    dirty_out = [dirty.process(f) for f in good + bad + [last]]

    assert dirty_out[5] == {"jawOpen": INVALID, "yaw_deg": INVALID}
    assert dirty_out[6] == {"jawOpen": INVALID, "yaw_deg": INVALID}
    assert dirty_out[-1] == clean_out[-1]
    assert dirty.filters["jawOpen"].resets == 0


def test_partial_frame_features_are_not_used_even_when_present():
    bank = FilterBank({BLENDSHAPES[0]: ChannelConfig()}, RATE_HZ)
    partial = message(T0_MS, "partial")
    assert BLENDSHAPES[0] in partial["features"]
    assert bank.process(partial)[BLENDSHAPES[0]] == INVALID
    # The partial frame did not count toward warm-up or the gap clock.
    assert bank.filters[BLENDSHAPES[0]]._last_ms is None


def test_gap_longer_than_reset_after_ms_resets_the_filter():
    channel = ChannelFilter(ChannelConfig(median_window=1, cutoff_hz=2.0), RATE_HZ, reset_after_ms=200)
    pre = feed(channel, [(T0_MS + i * FRAME_MS, 0.05) for i in range(20)])
    assert pre[-1].value == pytest.approx(0.05)
    # 450 ms dropout, as measured during a fast head turn.
    after_gap = T0_MS + 19 * FRAME_MS + 450
    first = channel.update(after_gap, 0.60)
    assert first.valid
    assert first.value == 0.60
    assert channel.resets == 1


def test_gap_reset_with_median_window_contains_no_pre_gap_values():
    channel = ChannelFilter(DIRECTIONAL, RATE_HZ, reset_after_ms=200)
    feed(channel, [(T0_MS + i * FRAME_MS, 0.05) for i in range(20)])
    start = T0_MS + 19 * FRAME_MS + 450
    post = feed(channel, [(start + i * FRAME_MS, 0.60) for i in range(3)])
    assert [p.valid for p in post] == [False, False, True]
    assert post[2].value == 0.60


def test_gap_shorter_than_reset_after_ms_does_not_reset():
    channel = ChannelFilter(ChannelConfig(median_window=1, cutoff_hz=2.0), RATE_HZ, reset_after_ms=200)
    feed(channel, [(T0_MS + i * FRAME_MS, 0.05) for i in range(20)])
    after_gap = T0_MS + 19 * FRAME_MS + 150
    out = channel.update(after_gap, 0.60)
    alpha = filters.one_pole_alpha(2.0, RATE_HZ)
    assert channel.resets == 0
    assert out.value == pytest.approx(0.05 + alpha * (0.60 - 0.05))


def test_gap_exactly_reset_after_ms_does_not_reset():
    channel = ChannelFilter(ChannelConfig(median_window=1, cutoff_hz=2.0), RATE_HZ, reset_after_ms=200)
    channel.update(T0_MS, 0.05)
    channel.update(T0_MS + 200, 0.60)
    assert channel.resets == 0


def test_bank_resets_across_a_long_dropout_of_lost_frames():
    bank = FilterBank({"jawOpen": ChannelConfig(1, 2.0)}, RATE_HZ)
    frames = [message(T0_MS + i * FRAME_MS, value=0.05) for i in range(10)]
    frames += [message(T0_MS + i * FRAME_MS, "lost") for i in range(10, 19)]
    frames.append(message(T0_MS + 19 * FRAME_MS, value=0.60))  # 500 ms after the last tracking frame
    assert bank_output(bank, frames)[-1].value == 0.60


def test_non_increasing_timestamp_is_rejected():
    channel = ChannelFilter(ChannelConfig(), RATE_HZ)
    channel.update(T0_MS, 0.1)
    with pytest.raises(ValueError):
        channel.update(T0_MS, 0.1)


# --- Warm-up -----------------------------------------------------------------


def test_output_is_invalid_until_the_median_window_is_full():
    bank = FilterBank({"jawOpen": ChannelConfig(median_window=5, cutoff_hz=2.0)}, RATE_HZ)
    out = bank_output(bank, [message(T0_MS + i * FRAME_MS, value=0.2) for i in range(7)])
    assert [o.valid for o in out] == [False] * 4 + [True] * 3
    assert all(o.value is None for o in out[:4])
    assert out[4].value == 0.2


def test_window_one_is_valid_on_the_first_frame():
    bank = FilterBank({"yaw_deg": ChannelConfig(1, 8.0)}, RATE_HZ)
    assert bank.process(message(T0_MS, yaw=12.5))["yaw_deg"] == filters.FilteredValue(12.5, True)


def test_channel_values_reads_pose_and_translation():
    values = filters.channel_values(message(T0_MS, yaw=7.0))
    assert values["yaw_deg"] == 7.0
    assert (values["x_cm"], values["y_cm"], values["z_cm"]) == (2.0, -1.0, -45.0)
    assert len(values) == len(BLENDSHAPES) + 6


def test_channel_values_omits_missing_channels_instead_of_zeroing():
    values = filters.channel_values(message(T0_MS, "partial"))
    assert "yaw_deg" not in values
    assert BLENDSHAPES[-1] not in values


# --- Group delay -------------------------------------------------------------


@pytest.mark.parametrize(
    "config, rate_hz, median_ms, one_pole_ms",
    [
        (ChannelConfig(1, None), 20.0, 0.0, 0.0),
        (ChannelConfig(1, 8.0), 20.0, 0.0, 1000.0 / (2 * math.pi * 8.0)),  # 19.894 ms
        (ChannelConfig(3, 2.0), 20.0, 50.0, 1000.0 / (2 * math.pi * 2.0)),  # 50 + 79.577 ms
        (ChannelConfig(5, None), 30.0, 2000.0 / 30.0, 0.0),  # 2 frames at 30 Hz
    ],
)
def test_group_delay_matches_hand_computed_values(config, rate_hz, median_ms, one_pole_ms):
    delay = group_delay_ms(config, rate_hz)
    assert delay["median_ms"] == pytest.approx(median_ms)
    assert delay["one_pole_ms"] == pytest.approx(one_pole_ms)
    assert delay["total_ms"] == pytest.approx(median_ms + one_pole_ms)


def test_group_delay_literal_values():
    assert group_delay_ms(ChannelConfig(3, 2.0), 20.0)["total_ms"] == pytest.approx(129.577, abs=0.001)
    assert group_delay_ms(ChannelConfig(1, 8.0), 20.0)["total_ms"] == pytest.approx(19.894, abs=0.001)


# --- Isolation ---------------------------------------------------------------


def test_importing_filters_and_baseline_loads_no_camera_or_server_package():
    code = (
        "import sys; import perception.filters, perception.baseline; "
        "print(' '.join(m for m in ('cv2', 'mediapipe', 'numpy', 'websockets') if m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""
