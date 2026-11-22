"""Checks the rest baseline measurement in perception/baseline.py.

Standard library plus pytest. Uses constructed frames and the synthetic
stream; no camera, no model.
"""

from __future__ import annotations

import json
import math

import pytest

from perception import baseline, fake_stream
from perception.baseline import SourceFrame
from tests.contract_schema import BLENDSHAPES
from tests.test_filters import message

T0_MS = 1795276800000


def frames_from(values_and_states: list[tuple[float, str]], step_ms: int = 50, brightness: float | None = None):
    return [
        SourceFrame(message(T0_MS + i * step_ms, state, value=v, yaw=v * 10), brightness)
        for i, (v, state) in enumerate(values_and_states)
    ]


def test_statistics_match_hand_computed_mean_and_sample_std():
    # Tracking values 0.02, 0.04, 0.06, 0.08: mean 0.05.
    # Squared deviations 0.0009 + 0.0001 + 0.0001 + 0.0009 = 0.002.
    # Sample variance 0.002 / 3; sample std sqrt(0.000666...) = 0.0258199.
    # A population std would be sqrt(0.002 / 4) = 0.0223607.
    series = [(0.02, "tracking"), (0.9, "lost"), (0.04, "tracking"), (0.9, "partial"), (0.06, "tracking"), (0.08, "tracking")]
    record = baseline.summarize(frames_from(series), "fake", 10.0, 0.0)
    stats = record["channels"]["jawOpen"]
    assert stats["count"] == 4
    assert stats["mean"] == pytest.approx(0.05)
    assert stats["std"] == pytest.approx(0.0258199, abs=1e-7)
    assert stats["min"] == 0.02 and stats["max"] == 0.08
    assert stats["peak_to_peak"] == pytest.approx(0.06)
    yaw = record["channels"]["yaw_deg"]
    assert yaw["mean"] == pytest.approx(0.5) and yaw["std"] == pytest.approx(0.258199, abs=1e-6)


def test_lost_frames_are_excluded_from_statistics_but_counted_in_lost_fraction():
    series = [(0.02, "tracking"), (0.9, "lost"), (0.04, "tracking"), (0.0, "partial"), (0.06, "tracking"), (0.08, "tracking")]
    record = baseline.summarize(frames_from(series), "fake", 10.0, 0.0)
    assert record["total_frames"] == 6
    assert record["tracking_frames"] == 4
    assert record["partial_frames"] == 1 and record["lost_frames"] == 1
    assert record["lost_fraction"] == pytest.approx(2 / 6)
    # The partial frame carries BLENDSHAPES[0] at 0.0; it must not lower the minimum.
    assert record["channels"][BLENDSHAPES[0]]["min"] == 0.02
    assert record["channels"][BLENDSHAPES[0]]["count"] == 4


def test_single_sample_has_no_std_and_no_flags():
    stats = baseline.channel_stats([0.3], 1e-6, 1.0)
    assert stats["count"] == 1 and stats["mean"] == 0.3 and stats["peak_to_peak"] == 0.0
    assert stats["std"] is None and stats["zero_variance"] is None and stats["snr"] is None
    assert stats["distinct_values"] == 1


# --- Dead and pinned channels ------------------------------------------------


def test_pinned_channel_yields_a_finite_flagged_snr():
    # A channel the detector holds at exactly 0.0 on every frame.
    stats = baseline.channel_stats([0.0] * 200, 5e-7, 1.0)
    assert stats["std"] == 0.0
    assert stats["distinct_values"] == 1
    assert stats["zero_variance"] is True
    assert stats["below_noise_floor"] is True
    assert math.isfinite(stats["snr"])
    assert stats["snr"] == pytest.approx(1.0 / 5e-7)


def test_near_zero_channel_is_floored_but_not_zero_variance():
    # As measured on cheekSquintLeft: values around 1e-7, std around 1e-8.
    values = [1e-7 + (i % 3) * 1e-8 for i in range(200)]
    stats = baseline.channel_stats(values, 5e-7, 1.0)
    assert 0.0 < stats["std"] < 5e-7
    assert stats["distinct_values"] == 3
    assert stats["zero_variance"] is False
    assert stats["below_noise_floor"] is True
    assert stats["snr"] == pytest.approx(1.0 / 5e-7)


def test_live_channel_snr_uses_measured_std():
    stats = baseline.channel_stats([0.10, 0.12, 0.14, 0.16], 5e-7, 1.0)
    assert stats["below_noise_floor"] is False and stats["zero_variance"] is False
    assert stats["snr"] == pytest.approx(1.0 / stats["std"])


def test_snr_rejects_a_non_positive_floor():
    with pytest.raises(ValueError):
        baseline.snr(1.0, 0.0, 0.0)


def test_summary_applies_unit_floors_and_contract_ranges():
    series = [(0.05, "tracking"), (0.05, "tracking"), (0.05, "tracking")]
    channels = baseline.summarize(frames_from(series), "fake", 1.0, 0.0)["channels"]
    assert channels["jawOpen"]["noise_floor"] == baseline.NOISE_FLOORS["score"]
    assert channels["jawOpen"]["range"] == 1.0
    assert channels["jawOpen"]["zero_variance"] is True
    assert channels["yaw_deg"]["noise_floor"] == baseline.NOISE_FLOORS["deg"]
    assert channels["yaw_deg"]["range"] == 180.0
    assert channels["z_cm"]["noise_floor"] == baseline.NOISE_FLOORS["cm"]
    assert channels["z_cm"]["range"] == 195.0
    assert all(math.isfinite(stats["snr"]) for stats in channels.values())


def test_report_never_prints_a_tiny_std_as_zero():
    stats = baseline.channel_stats([1e-7 + (i % 3) * 1e-8 for i in range(50)], 5e-7, 1.0)
    line = baseline.blendshape_line("cheekSquintLeft", stats)
    assert "0.00000" not in line
    assert "e-0" in line and "BELOW_NOISE_FLOOR" in line


# --- Lighting ----------------------------------------------------------------


def test_record_states_that_it_is_valid_only_for_its_lighting():
    frames = frames_from([(0.05, "tracking")] * 3, brightness=135.0)
    record = baseline.summarize(frames, "camera", 1.0, 0.0)
    lighting = record["lighting"]
    assert lighting["valid_only_for_measured_lighting"] is True
    assert lighting["valid_for_brightness"] == 135.0
    assert "frame_rate_hz" in lighting["dependent_on_brightness"]
    assert "channels.*.std" in lighting["dependent_on_brightness"]
    assert "brightness" in lighting["note"]
    assert record["brightness"]["min"] == 135.0 and record["brightness"]["max"] == 135.0


def test_frame_rate_is_measured_from_timestamps():
    record = baseline.summarize(frames_from([(0.05, "tracking")] * 11, step_ms=50), "fake", 1.0, 0.0)
    assert record["frame_rate_hz"] == pytest.approx(20.0)


def test_brightness_is_averaged_and_null_without_an_image():
    frames = frames_from([(0.05, "tracking")] * 2, brightness=100.0) + [
        SourceFrame(message(T0_MS + 500), 120.0),
        SourceFrame(message(T0_MS + 550, "lost"), 140.0),
    ]
    assert baseline.summarize(frames, "camera", 1.0, 0.0)["brightness"]["mean"] == pytest.approx(115.0)
    assert baseline.summarize(frames_from([(0.05, "tracking")] * 2), "fake", 1.0, 0.0)["brightness"]["mean"] is None


def test_collect_stops_at_the_requested_duration_by_timestamp():
    frames = baseline.fake_source_frames(30.0, 11, fake_stream.FIXTURE_START_MS)
    collected = baseline.collect(frames, 2.0)
    assert len(collected) == 60
    assert collected[-1].message["timestamp_ms"] - collected[0].message["timestamp_ms"] < 2000


def test_settle_drops_the_leading_seconds():
    frames = baseline.fake_source_frames(30.0, 11, fake_stream.FIXTURE_START_MS)
    first = next(baseline.skip_seconds(frames, 1.0))
    assert first.message["timestamp_ms"] - fake_stream.FIXTURE_START_MS == 1000


def test_fake_run_is_deterministic_and_records_its_own_schema(tmp_path):
    args = baseline.parse_args(["--source", "fake", "--seconds", "15", "--settle", "0", "--out", str(tmp_path / "b.json")])
    one = baseline.measure(baseline.fake_source_frames(30.0, 11, fake_stream.FIXTURE_START_MS), args)
    two = baseline.measure(baseline.fake_source_frames(30.0, 11, fake_stream.FIXTURE_START_MS), args)
    assert one == two
    assert one["record_type"] == "rest_baseline"
    assert one["schema_version"] == baseline.BASELINE_SCHEMA_VERSION
    assert one["measured"] is False
    assert one["total_frames"] == 450
    # One 15 s cycle of the synthetic stream is 1.0 s lost and 0.5 s partial.
    assert one["lost_fraction"] == pytest.approx(45 / 450)
    assert one["frame_rate_hz"] == pytest.approx(30.0, abs=0.01)
    assert set(one["channels"]) == set(BLENDSHAPES) | set(baseline.POSE_CHANNELS)
    assert one["channels"]["cheekPuff"]["std"] == pytest.approx(fake_stream.NOISE_SIGMA, rel=0.25)


def test_main_writes_valid_json_and_a_report(tmp_path, capsys):
    out = tmp_path / "baseline.json"
    assert baseline.main(["--source", "fake", "--seconds", "3", "--out", str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["total_frames"] == 90
    report = capsys.readouterr().out
    assert "noisiest 5 blendshapes" in report and "quietest 5 blendshapes" in report
