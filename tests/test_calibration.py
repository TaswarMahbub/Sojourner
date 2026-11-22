"""Checks calibration scoring in perception/calibration.py.

Standard library plus pytest. Every recording here is synthetic and built in
the test with a known answer: a 30 Hz frame stream walked through the real
protocol plan, with chosen channels moving during chosen repetitions. No
camera, no model, no recorded human data.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from perception import calibrate, calibration
from perception.calibration import (
    CONFIRM,
    FRAME,
    HEADER,
    MARKER,
    PROBE_REP,
    PROBE_REST,
    REST_BASELINE,
    Probe,
    ProtocolTiming,
)
from tests.contract_schema import BLENDSHAPES

T0_MS = 1795276800000
STEP_MS = 33
REPO_ROOT = Path(__file__).resolve().parent.parent


class Channel:
    """How one synthetic channel behaves.

    rest: rest level. noise: gaussian std added to every frame.
    amps: per-repetition excursion added during that probe's repetitions,
    from onset_ms after the prompt is issued until the repetition ends.
    rest_spike: (offset_ms into a probe_rest, duration_ms, amplitude,
    probe indices), an excursion added in the probe_rest after each listed
    probe. copy_of: this channel is copy_of * scale + offset, frame by
    frame. constant: the channel never changes. hold_through_release:
    the previous repetition's excursion is held through the release, so the
    next repetition starts already past threshold. rest_baseline_bump:
    (from_ms, to_ms, amplitude) added inside the rest baseline, measured
    from its start.
    """

    def __init__(self, rest=0.02, noise=0.004, probe=None, amps=(0.0, 0.0, 0.0), onset_ms=150,
                 rest_spike=None, copy_of=None, scale=1.0, offset=0.0, constant=None, hold_through_release=False,
                 rest_baseline_bump=None):
        self.rest, self.noise, self.probe, self.amps, self.onset_ms = rest, noise, probe, amps, onset_ms
        self.rest_spike, self.copy_of, self.scale, self.offset, self.constant = rest_spike, copy_of, scale, offset, constant
        self.hold_through_release, self.rest_baseline_bump = hold_through_release, rest_baseline_bump


def build_records(channels: dict[str, Channel], probes, *, prompt_delay_ms: int = 0, seed: int = 3,
                  timing: ProtocolTiming = calibration.DEFAULT_TIMING) -> list[dict]:
    """A complete synthetic recording as a list of JSONL records.

    prompt_delay_ms separates each repetition's prompt_issued_ms from its
    segment start_ms. Channels not listed are fixed blendshape noise at
    0.02 +/- 0.004 so they never move or cross.
    """
    rng = random.Random(seed)
    plan = calibration.plan_protocol(probes, timing)
    end_ms = plan[-1].offset_ms + plan[-1].duration_ms
    records = [{
        "record_type": HEADER, "recording_schema_version": calibration.RECORDING_SCHEMA_VERSION,
        "session_id": "synthetic", "user_id": "test", "source": "synthetic", "measured": False,
        "started_ms": T0_MS, "protocol": calibration.protocol_description(probes, timing),
        "brightness": {"mean": 130.0, "min": 129.0, "max": 131.0}, "frame_rate_hz": 30.3,
    }]
    issued = {}
    for planned in plan:
        start = T0_MS + planned.offset_ms
        prompt_ms = start + (prompt_delay_ms if planned.segment == PROBE_REP else 0)
        issued[planned.offset_ms] = prompt_ms
    markers = [(T0_MS + p.offset_ms, p) for p in plan]
    marker_index = 0
    probe_index = {p.name: i for i, p in enumerate(probes)}
    for t in range(0, end_ms, STEP_MS):
        ts = T0_MS + t
        while marker_index < len(markers) and markers[marker_index][0] <= ts:
            start, planned = markers[marker_index]
            records.append(calibrate.marker(planned, start, issued[planned.offset_ms]))
            marker_index += 1
        current = markers[marker_index - 1][1]
        values = {}
        for name, ch in channels.items():
            if ch.copy_of is not None:
                continue
            if ch.constant is not None:
                values[name] = ch.constant
                continue
            v = ch.rest + rng.gauss(0.0, ch.noise)
            if current.segment == PROBE_REP and current.probe == ch.probe:
                since_prompt = ts - issued[current.offset_ms]
                if since_prompt >= ch.onset_ms:
                    v += ch.amps[current.repetition - 1]
            if ch.hold_through_release and current.segment == "probe_release" and current.probe == ch.probe:
                v += ch.amps[current.repetition - 1]
            if ch.rest_baseline_bump is not None and current.segment == REST_BASELINE:
                into = ts - (T0_MS + current.offset_ms)
                if ch.rest_baseline_bump[0] <= into < ch.rest_baseline_bump[1]:
                    v += ch.rest_baseline_bump[2]
            if ch.rest_spike is not None and current.segment == PROBE_REST and probe_index[current.probe] in ch.rest_spike[3]:
                into = ts - (T0_MS + current.offset_ms)
                if ch.rest_spike[0] <= into < ch.rest_spike[0] + ch.rest_spike[1]:
                    v += ch.rest_spike[2]
            values[name] = v
        for name, ch in channels.items():
            if ch.copy_of is not None:
                values[name] = values[ch.copy_of] * ch.scale + ch.offset
        records.append({"record_type": FRAME, "message": message(ts, values, rng), "brightness": 130.0})
    records.append({"record_type": "session_end", "ended_ms": T0_MS + end_ms, "frames": 0,
                    "brightness": {"mean": 130.0}, "frame_rate_hz": 30.3})
    return records


def message(ts: int, values: dict[str, float], rng: random.Random) -> dict:
    """A tracking message: listed channels from values, other blendshapes noise."""
    features = {name: values.get(name, 0.02 + rng.gauss(0.0, 0.004)) for name in BLENDSHAPES}
    pose = {name: values.get(name, rng.gauss(0.0, 0.3)) for name in ("yaw_deg", "pitch_deg", "roll_deg")}
    matrix = [[1.0, 0.0, 0.0, 1.0], [0.0, 1.0, 0.0, -2.0], [0.0, 0.0, 1.0, -45.0], [0.0, 0.0, 0.0, 1.0]]
    return {"timestamp_ms": ts, "tracking_state": "tracking", "features": features,
            "head_pose": {"matrix": matrix, **pose}, "confidence": None}


def score(channels, probes, **kwargs) -> dict:
    return calibration.score_session(calibration.parse_records(build_records(channels, probes, **kwargs)))


def row(result: dict, name: str) -> dict:
    return next(r for r in result["ranking"] if r["channel"] == name)


# Real blendshape names, so the synthetic message carries them as features.
CH_A, CH_B, CH_C = "jawOpen", "browInnerUp", "mouthSmileLeft"
PA = Probe("a", "Do A", (CH_A,))
PB = Probe("b", "Do B", (CH_B,))
PC = Probe("c", "Do C", (CH_C,))


# --- Protocol ----------------------------------------------------------------


def test_default_protocol_is_sixty_seconds_with_the_report_segments():
    plan = calibration.plan_protocol()
    total = plan[-1].offset_ms + plan[-1].duration_ms
    assert total == 60000
    by_segment = {}
    for s in plan:
        by_segment[s.segment] = by_segment.get(s.segment, 0) + s.duration_ms
    assert by_segment["free_exploration"] == 15000
    assert by_segment[PROBE_REP] + by_segment["probe_release"] + by_segment[PROBE_REST] == 30000
    assert by_segment[REST_BASELINE] == 10000
    assert by_segment[CONFIRM] == 5000
    reps = [s for s in plan if s.segment == PROBE_REP]
    assert len(reps) == 3 * len(calibration.PROBES)
    assert plan[0].prompt == "Move however you can."


def test_constants_refuse_overlapping_held_out_rest():
    with pytest.raises(ValueError):
        calibration.ScoringConstants(held_out_settle_ms=100, rep_grace_ms=500)


# --- Ranking -----------------------------------------------------------------


def test_wide_clean_channel_ranks_above_noisy_narrow_one():
    channels = {
        CH_A: Channel(rest=0.02, noise=0.004, probe="a", amps=(0.6, 0.6, 0.6)),
        CH_B: Channel(rest=0.02, noise=0.01, probe="b", amps=(0.17, 0.17, 0.17)),
    }
    result = score(channels, (PA, PB))
    wide, narrow = row(result, CH_A), row(result, CH_B)
    assert not wide["reasons"] and not narrow["reasons"], (wide, narrow)
    assert wide["rank"] < narrow["rank"]
    assert result["channels"][CH_A]["snr"] > result["channels"][CH_B]["snr"]


def test_pinned_zero_variance_channel_is_disqualified_not_ranked_first():
    channels = {
        CH_A: Channel(probe="a", amps=(0.5, 0.5, 0.5)),
        CH_B: Channel(probe="b", constant=0.0),
    }
    result = score(channels, (PA, PB))
    pinned = row(result, CH_B)
    assert "zero_variance" in pinned["reasons"]
    assert pinned["rank"] is None and pinned["score"] is None
    assert result["ranking"][0]["channel"] == CH_A
    # SNR uses max(std, noise_floor), so it is finite, not infinite.
    assert result["channels"][CH_B]["rest"]["effective_std"] == calibration.NOISE_FLOORS["score"]


def test_held_out_rest_crossing_disqualifies_regardless_of_other_scores():
    # A wide, clean, repeatable channel that twitches for 200 ms, 600 ms
    # into the rest after probe b: past the 500 ms held-out settle, and in
    # a rest that qualifies for it (not its own probe's rest).
    channels = {
        CH_A: Channel(probe="a", amps=(0.7, 0.7, 0.7), rest_spike=(600, 200, 0.3, (1,))),
        CH_B: Channel(probe="b", amps=(0.3, 0.3, 0.3)),
    }
    result = score(channels, (PA, PB))
    twitchy = row(result, CH_A)
    assert twitchy["reasons"] == ["false_positive_in_held_out_rest"]
    assert result["channels"][CH_A]["snr"] > result["channels"][CH_B]["snr"]
    assert result["channels"][CH_A]["false_positives"]["crossings"] == 1
    assert row(result, CH_B)["rank"] == 1


def test_false_positives_are_not_evaluated_on_the_threshold_source():
    # The same twitch inside the rest baseline (the threshold source) only
    # widens the threshold; held-out rest is clean, so no false positive.
    channels = {CH_A: Channel(probe="a", amps=(0.7, 0.7, 0.7)), CH_B: Channel(probe="b", amps=(0.4, 0.4, 0.4))}
    result = score(channels, (PA, PB))
    fp = result["channels"][CH_A]["false_positives"]
    assert fp["threshold_source"] == REST_BASELINE and fp["evaluated_on"] == PROBE_REST
    # Only probe b's rest qualifies: probe a's rest follows chanA's own probe.
    assert fp["held_out_periods"] == 1 and fp["held_out_frames"] > 0
    assert [w["reason"] for w in fp["excluded_windows"]] == ["own_probe"]
    assert fp["crossings"] == 0


def test_no_held_out_rest_is_insufficient_data_not_zero_false_positives():
    channels = {CH_A: Channel(probe="a", amps=(0.6, 0.6, 0.6))}
    records = [r for r in build_records(channels, (PA,)) if not (r["record_type"] == MARKER and r["segment"] == PROBE_REST)]
    result = calibration.score_session(calibration.parse_records(records))
    assert "insufficient_data" in row(result, CH_A)["reasons"]


def test_perfectly_correlated_channels_are_one_cluster():
    probes = (Probe("a", "Do A", (CH_A, CH_C)), PB)
    channels = {
        CH_A: Channel(probe="a", amps=(0.5, 0.5, 0.5)),
        CH_C: Channel(copy_of=CH_A, scale=0.8, offset=0.01),
        CH_B: Channel(probe="b", amps=(0.4, 0.4, 0.4)),
    }
    result = score(channels, probes)
    multi = [g for g in result["clusters"] if len(g["channels"]) > 1]
    assert len(multi) == 1
    assert sorted(multi[0]["channels"]) == sorted([CH_A, CH_C])
    assert multi[0]["max_abs_r"] == pytest.approx(1.0)
    statuses = sorted(row(result, n)["status"] for n in (CH_A, CH_C))
    assert statuses == ["redundant", "usable"]
    assert result["tier"]["usable_channels"].count(CH_A) + result["tier"]["usable_channels"].count(CH_C) == 1


def test_repeatability_is_worse_when_peaks_differ():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6, 0.6, 0.6)),
        CH_B: Channel(probe="b", amps=(0.2, 0.6, 0.9)),
    }
    result = score(channels, (PA, PB))
    steady = result["channels"][CH_A]["repeatability_cv"]
    erratic = result["channels"][CH_B]["repeatability_cv"]
    assert steady < 0.05
    assert erratic > 0.4
    assert row(result, CH_A)["scores"]["repeatability"] > row(result, CH_B)["scores"]["repeatability"]


def test_fatigue_drift_detected_when_last_rep_is_well_below_first():
    channels = {CH_A: Channel(probe="a", amps=(0.8, 0.6, 0.3)), CH_B: Channel(probe="b", amps=(0.5, 0.5, 0.5))}
    result = score(channels, (PA, PB))
    drift = result["channels"][CH_A]["fatigue_drift"]
    assert drift["absolute"] == pytest.approx(-0.5, abs=0.03)
    assert drift["relative"] == pytest.approx(-0.625, abs=0.04)
    steady = result["channels"][CH_B]["fatigue_drift"]["relative"]
    assert abs(steady) < 0.05
    assert row(result, CH_A)["scores"]["fatigue"] < row(result, CH_B)["scores"]["fatigue"]


def test_latency_is_measured_from_the_prompt_not_the_segment_start():
    # Prompts are issued 300 ms after each repetition segment starts and the
    # movement begins 200 ms after the prompt. Latency from the prompt is
    # about 200 ms; from the segment start it would be about 500 ms.
    channels = {CH_A: Channel(probe="a", amps=(0.6, 0.6, 0.6), onset_ms=200), CH_B: Channel(probe="b", amps=(0.4,) * 3)}
    result = score(channels, (PA, PB), prompt_delay_ms=300)
    reps = result["channels"][CH_A]["repetitions"]
    assert all(r["latency_ms"] is not None for r in reps)
    for r in reps:
        assert 200 <= r["latency_ms"] < 200 + STEP_MS, r
    assert result["channels"][CH_A]["latency_ms_median"] < 300


def test_threshold_tracks_a_non_zero_rest_mean():
    channels = {CH_A: Channel(rest=0.4, noise=0.005, probe="a", amps=(0.4, 0.4, 0.4)), CH_B: Channel(probe="b", amps=(0.4,) * 3)}
    result = score(channels, (PA, PB))
    m = result["channels"][CH_A]
    assert m["rest"]["mean"] == pytest.approx(0.4, abs=0.003)
    assert m["direction"] == 1
    assert m["threshold"]["active"] > 0.4
    expected = max(5 * m["rest"]["effective_std"], 0.5 * calibration.MIN_EXCURSION["score"])
    assert m["threshold"]["active"] == pytest.approx(m["rest"]["mean"] + expected)
    assert m["threshold"]["lower"] > 0.3
    assert m["deadband"]["lower"] < 0.4 < m["deadband"]["upper"] < m["threshold"]["upper"]


def test_threshold_follows_a_negative_movement_from_a_resting_pitch():
    # Head pitch resting at +9.8 degrees, as Phase 2 measured, moving down.
    probes = (Probe("nod", "Tilt your head down", ("pitch_deg",)), PB)
    channels = {"pitch_deg": Channel(rest=9.8, noise=0.4, probe="nod", amps=(-15.0, -15.0, -15.0)),
                CH_B: Channel(probe="b", amps=(0.4,) * 3)}
    result = score(channels, probes)
    m = result["channels"]["pitch_deg"]
    assert m["direction"] == -1
    assert 9.8 - 4 < m["threshold"]["active"] < 9.8
    assert m["extreme"] == pytest.approx(9.8 - 15.0, abs=1.0)
    assert m["unit"] == "deg"


# --- Tier ----------------------------------------------------------------------


def test_one_surviving_channel_yields_tier_3():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6, 0.6, 0.6)),
        CH_B: Channel(probe="b", amps=(0.0, 0.0, 0.0)),
        CH_C: Channel(probe="c", amps=(0.6, 0.6, 0.6), rest_spike=(700, 200, 0.3, (1,))),
    }
    result = score(channels, (PA, PB, PC))
    assert result["tier"]["usable_channels"] == [CH_A]
    assert result["tier"]["tier"] == 3
    assert result["tier"]["mode"] == "scanning"
    assert CH_A in result["tier"]["reason"]


def test_several_broad_channels_yield_tier_1():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6, 0.6, 0.6)),
        CH_B: Channel(probe="b", amps=(0.5, 0.5, 0.5)),
        CH_C: Channel(probe="c", amps=(0.7, 0.7, 0.7)),
    }
    result = score(channels, (PA, PB, PC))
    assert result["tier"]["tier"] == 1
    assert sorted(result["tier"]["broad_channels"]) == sorted([CH_A, CH_B, CH_C])
    recording = calibration.parse_records(build_records(channels, (PA, PB, PC)))
    profile = calibration.build_profile(result, recording)
    roles = [b["role"] for b in profile["bindings"]]
    assert roles[:2] == ["proportional_primary", "proportional_secondary"]


def test_several_narrow_channels_yield_tier_2():
    channels = {
        CH_A: Channel(probe="a", amps=(0.12, 0.12, 0.12), noise=0.004),
        CH_B: Channel(probe="b", amps=(0.12, 0.12, 0.12), noise=0.004),
    }
    result = score(channels, (PA, PB))
    assert len(result["tier"]["usable_channels"]) == 2
    assert result["tier"]["broad_channels"] == []
    assert result["tier"]["tier"] == 2


def test_no_surviving_channel_proposes_no_tier():
    channels = {CH_A: Channel(probe="a", amps=(0.0, 0.0, 0.0))}
    result = score(channels, (PA,))
    assert result["tier"]["tier"] is None


# --- Discovery, files, profile -------------------------------------------------


def test_free_exploration_discovers_an_unprobed_channel():
    records = build_records({CH_A: Channel(probe="a", amps=(0.6,) * 3)}, (PA,))
    free_end = calibration.DEFAULT_TIMING.free_exploration_ms
    for r in records:
        if r["record_type"] == FRAME and 3000 <= r["message"]["timestamp_ms"] - T0_MS < 4000:
            r["message"]["features"]["cheekPuff"] = 0.7
        assert r["record_type"] != FRAME or r["message"]["timestamp_ms"] - T0_MS < 60000
    assert free_end > 4000
    result = calibration.score_session(calibration.parse_records(records))
    found = [d["channel"] for d in result["discovered"]]
    assert "cheekPuff" in found
    assert "cheekPuff" not in result["candidates"]


def test_recording_round_trips_through_a_file_and_builds_a_profile(tmp_path):
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in build_records(channels, (PA, PB))), encoding="utf-8")
    result = calibration.score_recording(path)
    recording = calibration.load_recording(path)
    profile = calibration.build_profile(result, recording)
    out = tmp_path / "profile.json"
    calibration.write_json(profile, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["record_type"] == "calibration_profile"
    assert loaded["schema_version"] == calibration.PROFILE_SCHEMA_VERSION
    assert loaded["proposed_tier"]["tier"] == 1
    assert {b["channel"] for b in loaded["bindings"]} == {CH_A, CH_B}
    assert set(loaded["enrolment_metrics"]["channels"]) == {CH_A, CH_B}
    assert loaded["scoring_constants"]["provisional"] is True
    assert loaded["lighting"]["warmup_brightness"]["mean"] == 130.0
    assert loaded["accepted"] is None
    assert "Not part of docs/contracts.md" in loaded["note"]
    assert "proposed tier: 1" in calibration.format_report(result)


def test_recording_must_start_with_a_header():
    with pytest.raises(ValueError):
        calibration.parse_records([{"record_type": FRAME, "message": {}}])


def test_crossing_detector_needs_sustained_frames_and_respects_direction():
    detector = calibration.CrossingDetector(1.0, 2)
    assert [detector.update(v) for v in (2.0, 0.0, 2.0, 2.0, 2.0, None, -2.0, -2.0)] == [
        False, False, False, True, False, False, False, True]
    up_only = calibration.CrossingDetector(1.0, 2, direction=1)
    assert not any(up_only.update(v) for v in (-2.0, -2.0, -2.0))


def test_live_session_on_synthetic_frames_records_markers_and_confirms(tmp_path):
    # The live engine driven by a synthetic stream in which jawOpen follows
    # the prompts. It must emit structured events, score from its own file,
    # and accept when the top channel moves during confirm.
    events = []

    class Capture:
        def tick(self, frame):
            return None

        def event(self, event):
            events.append(event)

    probes = (PA, PB)
    timing = calibration.DEFAULT_TIMING
    plan = calibration.plan_protocol(probes, timing)
    warmup_ms = 1000
    rng = random.Random(5)

    def frames():
        origin = None
        t = 0
        while True:
            ts = T0_MS + t
            if t >= warmup_ms and origin is None:
                origin = ts
            values = {CH_A: 0.02 + rng.gauss(0, 0.004), CH_B: 0.02 + rng.gauss(0, 0.004)}
            if origin is not None:
                rel = ts - origin
                seg = next((s for s in plan if s.offset_ms <= rel < s.offset_ms + s.duration_ms), None)
                if seg is not None and seg.segment == PROBE_REP and rel - seg.offset_ms > 150:
                    values[CH_A if seg.probe == "a" else CH_B] += 0.5
                if seg is not None and seg.segment == CONFIRM and rel - seg.offset_ms > 1000:
                    values[CH_A] += 0.5
                    values[CH_B] += 0.5
            yield calibrate.Frame(message(ts, values, rng), None)
            t += STEP_MS

    path = tmp_path / "live.jsonl"
    recorder = calibrate.Recorder(path, [Capture()])
    try:
        calibrate.run_session(frames(), recorder, user_id="test", source="fake", warmup_seconds=warmup_ms / 1000,
                              probes=probes, timing=timing)
    finally:
        recorder.close()
    kinds = [e["record_type"] for e in events]
    assert kinds[0] == HEADER and kinds[-1] == "session_end"
    markers = [e for e in events if e["record_type"] == MARKER]
    assert [m["segment"] for m in markers][1:] == [s.segment for s in plan]
    for m in markers:
        assert {"segment", "probe", "repetition", "prompt", "start_ms", "prompt_issued_ms"} <= set(m)
    confirm = next(e for e in events if e["record_type"] == "confirm_result")
    assert confirm["accepted"] is True
    result = calibration.score_recording(path)
    profile = calibration.build_profile(result, calibration.load_recording(path))
    assert profile["accepted"] is True
    assert result["tier"]["tier"] == 1


def test_importing_calibration_modules_loads_no_camera_packages():
    code = (
        "import sys\n"
        "import perception.calibration, perception.calibrate\n"
        "loaded = sorted({'cv2', 'mediapipe', 'numpy', 'websockets'} & set(sys.modules))\n"
        "print(loaded)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


# --- Rest-quality gate ---------------------------------------------------------


def test_clean_session_has_ok_rest_quality():
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    result = score(channels, (PA, PB))
    assert result["rest_quality"]["status"] == "ok"
    assert result["rest_quality"]["responsible_channels"] == {}
    assert result["tier"]["tier"] == 1


def test_suspect_rest_disqualifies_only_that_channel_and_the_session_continues():
    # The first camera session: smile around 0.6 for the first four seconds
    # of "Hold still", then back to rest. No stored baseline is consulted.
    # The gate is per channel: the smile is disqualified, the others are not.
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
        CH_C: Channel(probe="c", amps=(0.5,) * 3, rest_baseline_bump=(1000, 5000, 0.55)),
    }
    probes = (PA, PB, PC)
    result = score(channels, probes)
    quality = result["rest_quality"]
    assert quality["status"] == "channels_disqualified"
    assert list(quality["responsible_channels"]) == [CH_C]
    assert "rest_level_shift" in quality["responsible_channels"][CH_C]
    assert "rest_suspect" in row(result, CH_C)["reasons"]
    assert result["channels"][CH_C]["rest_quality"]["reasons"] == quality["responsible_channels"][CH_C]
    assert result["session_quality"] == "ok"
    assert result["tier"]["tier"] == 1
    assert sorted(result["tier"]["usable_channels"]) == sorted([CH_A, CH_B])
    profile = calibration.build_profile(result, calibration.parse_records(build_records(channels, probes)))
    assert {b["channel"] for b in profile["bindings"]} == {CH_A, CH_B}
    assert "some channels disqualified as rest_suspect" in calibration.format_report(result)


def test_session_is_suspect_only_when_no_usable_channel_survives_the_rest_gate():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3, rest_baseline_bump=(1000, 5000, 0.55)),
        CH_B: Channel(probe="b", amps=(0.5,) * 3, rest_baseline_bump=(1000, 5000, 0.45)),
    }
    probes = (PA, PB)
    result = score(channels, probes)
    assert calibration.MIN_CHANNELS_FOR_TIER == 1
    assert result["rest_quality"]["status"] == "suspect"
    assert result["session_quality"] == "suspect"
    assert result["tier"]["tier"] is None and "rest_quality suspect" in result["tier"]["reason"]
    profile = calibration.build_profile(result, calibration.parse_records(build_records(channels, probes)))
    assert profile["rest_quality"] == "suspect" and profile["bindings"] == []
    assert "REST QUALITY SUSPECT" in calibration.format_report(result)


def test_noisy_final_rest_against_quiet_probe_rests_disqualifies_that_channel():
    # Fidgeting through the whole final rest: both halves agree with each
    # other, but not with the probe rests.
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
        CH_C: Channel(probe="c", amps=(0.5,) * 3),
    }
    records = build_records(channels, (PA, PB, PC))
    rng = random.Random(9)
    rest = next(r for r in records if r["record_type"] == MARKER and r["segment"] == REST_BASELINE)
    for r in records:
        if r["record_type"] == FRAME and r["message"]["timestamp_ms"] >= rest["start_ms"]:
            r["message"]["features"][CH_A] += rng.gauss(0.0, 0.06)
    result = calibration.score_session(calibration.parse_records(records))
    assert result["rest_quality"]["responsible_channels"] == {CH_A: ["probe_rest_std_ratio"]}
    assert "rest_suspect" in row(result, CH_A)["reasons"]
    assert result["tier"]["tier"] == 1 and CH_A not in result["tier"]["usable_channels"]


def test_live_session_with_suspect_rest_offers_nothing_to_accept():
    result = score({CH_A: Channel(probe="a", amps=(0.6,) * 3, rest_baseline_bump=(1000, 5000, 0.55))}, (PA,))
    result["probes"] = calibration.protocol_description((PA,))["probes"]
    prompt, top = calibrate.confirm_prompt(result)
    assert top is None
    assert "repeated" in prompt


# --- Minimum excursion -----------------------------------------------------------


def test_tiny_movement_on_a_near_silent_channel_fails_the_excursion_floor_despite_high_snr():
    channels = {
        CH_A: Channel(rest=1e-6, noise=1e-6, probe="a", amps=(5e-4,) * 3),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
    }
    result = score(channels, (PA, PB))
    m = result["channels"][CH_A]
    assert m["snr"] > calibration.DEFAULT_CONSTANTS.snr_floor
    assert row(result, CH_A)["reasons"] == ["excursion_below_floor"]


def test_discovery_ignores_amplified_silence():
    records = build_records({CH_A: Channel(probe="a", amps=(0.6,) * 3)}, (PA,))
    rng = random.Random(4)
    for r in records:
        if r["record_type"] != FRAME:
            continue
        t = r["message"]["timestamp_ms"] - T0_MS
        sneer = 1e-6 + rng.gauss(0.0, 2e-7)
        if 3000 <= t < 4000:
            sneer += 2.5e-5
        r["message"]["features"]["noseSneerLeft"] = sneer
        if 6000 <= t < 7000:
            r["message"]["features"]["cheekPuff"] = 0.7
    result = calibration.score_session(calibration.parse_records(records))
    found = [d["channel"] for d in result["discovered"]]
    assert "noseSneerLeft" not in found
    assert "cheekPuff" in found


# --- Latency validity and missed repetitions ---------------------------------------


def test_repetition_starting_past_threshold_counts_its_peak_but_not_its_latency():
    # The movement is held through each release, so repetitions 2 and 3
    # begin already past threshold, as yaw did in the first camera session.
    # Their peaks are real movements and count; their latency is null.
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3, onset_ms=300, hold_through_release=True),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
    }
    result = score(channels, (PA, PB))
    m = result["channels"][CH_A]
    reps = m["repetitions"]
    assert reps[0]["counted"] and 300 <= reps[0]["latency_ms"] < 300 + STEP_MS
    for r in reps[1:]:
        assert r["latency_excluded"] == "not_below_threshold_at_prompt"
        assert r["latency_ms"] is None
        assert r["counted"] and r["peak"] == pytest.approx(0.6, abs=0.03)
    counts = m["repetition_counts"]
    assert counts["counted"] == 3 and counts["latency"] == 1
    assert counts["latency_excluded"]["not_below_threshold_at_prompt"] == 2
    assert m["latency_ms_median"] == reps[0]["latency_ms"]
    assert m["usable_range"] == pytest.approx(0.6, abs=0.03) and m["repeatability_cv"] < 0.05
    assert not row(result, CH_A)["reasons"]
    assert "3c/1l" in calibration.format_report(result)


def test_anticipatory_latency_is_excluded_not_reported_as_fast():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3, onset_ms=0),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
    }
    result = score(channels, (PA, PB))
    m = result["channels"][CH_A]
    assert all(r["latency_excluded"] == "anticipatory" and r["latency_ms"] is None for r in m["repetitions"])
    assert m["latency_ms_median"] is None
    assert m["repetition_counts"] == {"counted": 3, "latency": 0, "missed": 0,
                                      "latency_excluded": {"not_below_threshold_at_prompt": 0, "anticipatory": 3}}
    assert row(result, CH_A)["scores"]["latency"] == 0.0


def test_missed_repetition_is_flagged_and_not_read_as_fatigue():
    # browInnerUp in the first camera session: 0.044, then 0.57 and 0.62.
    channels = {
        CH_A: Channel(probe="a", amps=(0.044, 0.57, 0.62)),
        CH_B: Channel(probe="b", amps=(0.5,) * 3),
    }
    result = score(channels, (PA, PB))
    m = result["channels"][CH_A]
    assert m["repetitions"][0]["excluded"] == "missed"
    assert m["repetition_counts"]["missed"] == 1 and m["repetition_counts"]["counted"] == 2
    drift = m["fatigue_drift"]
    assert (drift["first_repetition"], drift["last_repetition"]) == (2, 3)
    assert abs(drift["relative"]) < 0.15
    assert m["repeatability_cv"] < 0.1
    assert not row(result, CH_A)["reasons"]


def test_genuinely_uneven_repetitions_are_not_marked_missed():
    channels = {CH_A: Channel(probe="a", amps=(0.2, 0.6, 0.9)), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    result = score(channels, (PA, PB))
    assert result["channels"][CH_A]["repetition_counts"]["missed"] == 0


# --- Threshold floor -------------------------------------------------------------


def test_threshold_is_floored_at_half_the_minimum_excursion_for_a_very_still_rest():
    # The still camera session: jawOpen rest std 0.00055 gave a 5-sigma
    # offset of about 0.003, smaller than ordinary resting variation.
    channels = {CH_A: Channel(rest=0.003, noise=0.0005, probe="a", amps=(0.6,) * 3),
                CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    result = score(channels, (PA, PB))
    t = result["channels"][CH_A]["threshold"]
    assert t["floored"] is True
    assert t["offset"] == pytest.approx(0.025)
    assert result["channels"][CH_A]["deadband"]["offset"] == pytest.approx(0.025 * 3 / 5)
    noisy = result["channels"][CH_B]["threshold"]
    assert noisy["offset"] == pytest.approx(0.025)  # 5 * 0.004 = 0.02 is also below the floor


def test_threshold_follows_measured_noise_above_the_floor():
    channels = {CH_A: Channel(noise=0.01, probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    t = score(channels, (PA, PB))["channels"][CH_A]["threshold"]
    assert t["floored"] is False
    assert t["offset"] > 0.04


# --- Per-channel rest windows ------------------------------------------------------


def test_own_probe_rest_is_not_a_rest_for_that_channel():
    # jawOpen keeps moving through the rest after its own probe; that tail
    # must not count as a false positive or as rest noise.
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3, rest_spike=(600, 400, 0.3, (0,))),
                CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    result = score(channels, (PA, PB))
    fp = result["channels"][CH_A]["false_positives"]
    assert fp["crossings"] == 0
    assert {w["probe"]: w["reason"] for w in fp["excluded_windows"]} == {"a": "own_probe"}
    assert not row(result, CH_A)["reasons"]
    assert result["rest_quality"]["status"] == "ok"


def test_rest_after_a_coupled_probe_is_excluded_when_the_channel_has_not_returned():
    # Like yaw during the tilt probe: chanA is moved by probe b and is
    # still away from rest when probe b's rest window opens.
    probes = (PA, PB)
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    records = build_records(channels, probes)
    rest_b = next(r for r in records if r["record_type"] == MARKER and r["segment"] == PROBE_REST and r["probe"] == "b")
    for r in records:
        if r["record_type"] == FRAME and rest_b["start_ms"] <= r["message"]["timestamp_ms"] < rest_b["start_ms"] + 900:
            r["message"]["features"][CH_A] += 0.4
    result = calibration.score_session(calibration.parse_records(records))
    fp = result["channels"][CH_A]["false_positives"]
    assert {w["probe"]: w["reason"] for w in fp["excluded_windows"]} == {"a": "own_probe", "b": "not_returned_to_rest"}
    assert fp["held_out_frames"] == 0
    assert "insufficient_data" in row(result, CH_A)["reasons"]
    assert fp["crossings"] == 0 and fp["per_minute"] is None


# --- Lighting gate -----------------------------------------------------------------


def lit(records, brightness_at):
    """Set every frame's brightness from brightness_at(ms since start)."""
    for r in records:
        if r["record_type"] == FRAME:
            r["brightness"] = brightness_at(r["message"]["timestamp_ms"] - T0_MS)
    return records


def test_constant_lighting_is_ok_and_reports_min_max_median():
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    records = lit(build_records(channels, (PA, PB)), lambda t: 130.0 + (t % 7) * 0.1)
    result = calibration.score_session(calibration.parse_records(records))
    lq = result["lighting_quality"]
    assert lq["status"] == "ok"
    assert lq["brightness_min"] == pytest.approx(130.0) and lq["brightness_max"] == pytest.approx(130.6)
    assert 130.0 <= lq["brightness_median"] <= 130.6
    assert result["session_quality"] == "ok" and result["tier"]["tier"] == 1


def test_light_that_rises_during_the_session_is_suspect_and_refuses_a_tier():
    # The second camera session: warm-up at 100, then 165 to 217.
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    records = build_records(channels, (PA, PB))
    warm = {"record_type": MARKER, "event": "segment_start", "segment": "warmup", "probe": None, "repetition": None,
            "prompt": "Warming up.", "start_ms": T0_MS - 2000, "prompt_issued_ms": T0_MS - 2000, "planned_duration_ms": 2000}
    frames = [{"record_type": FRAME, "brightness": 100.0, "message": message(T0_MS - 2000 + i * STEP_MS, {}, random.Random(i))}
              for i in range(60)]
    records = [records[0], warm, *frames, *lit(records[1:], lambda t: 165.0)]
    result = calibration.score_session(calibration.parse_records(records))
    lq = result["lighting_quality"]
    assert lq["status"] == "suspect"
    assert lq["still_segment_brightness_ratio"] == pytest.approx(1.65)
    assert result["session_quality"] == "suspect"
    assert result["tier"]["tier"] is None and "lighting_quality suspect" in result["tier"]["reason"]
    profile = calibration.build_profile(result, calibration.parse_records(records))
    assert profile["lighting_quality"] == "suspect" and profile["bindings"] == []
    assert "LIGHTING SUSPECT" in calibration.format_report(result)


def test_head_movement_brightness_during_probes_does_not_trip_the_lighting_gate():
    # Whole-image brightness dips while the head turns in a repetition;
    # only still segments are compared.
    channels = {CH_A: Channel(probe="a", amps=(0.6,) * 3), CH_B: Channel(probe="b", amps=(0.5,) * 3)}
    records = build_records(channels, (PA, PB))
    reps = [(r["start_ms"], r["start_ms"] + r["planned_duration_ms"]) for r in records
            if r["record_type"] == MARKER and r["segment"] == PROBE_REP]
    lit(records, lambda t: 80.0 if any(a <= T0_MS + t < b for a, b in reps) else 130.0)
    result = calibration.score_session(calibration.parse_records(records))
    assert result["lighting_quality"]["brightness_min"] == 80.0
    assert result["lighting_quality"]["status"] == "ok"


# --- Stop binding ------------------------------------------------------------------


def test_stop_binds_to_the_lowest_latency_channel_with_enough_latency_samples():
    # chanA would be fastest on its one valid latency, but repetitions 2
    # and 3 start past threshold, so it has one latency sample and cannot
    # take Stop. chanB has three at about 400 ms; chanC three at about 250 ms.
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3, onset_ms=120, hold_through_release=True),
        CH_B: Channel(probe="b", amps=(0.5,) * 3, onset_ms=400),
        CH_C: Channel(probe="c", amps=(0.5,) * 3, onset_ms=250),
    }
    result = score(channels, (PA, PB, PC))
    assert result["channels"][CH_A]["repetition_counts"]["latency"] == 1
    assert result["channels"][CH_A]["latency_ms_median"] < result["channels"][CH_C]["latency_ms_median"]
    stop = result["stop_binding"]
    assert stop["channel"] == CH_C
    assert stop["latency_repetitions"] == 3 and stop["min_latency_repetitions"] == 2
    assert "hardware cutoff" in stop["stop_path"]
    profile = calibration.build_profile(result, calibration.parse_records(build_records(channels, (PA, PB, PC))))
    assert profile["stop_binding"]["channel"] == CH_C
    assert "stop binding: " + CH_C in calibration.format_report(result)


def test_no_channel_with_two_latency_samples_leaves_stop_unbound():
    channels = {
        CH_A: Channel(probe="a", amps=(0.6,) * 3, onset_ms=300, hold_through_release=True),
        CH_B: Channel(probe="b", amps=(0.5,) * 3, onset_ms=300, hold_through_release=True),
    }
    result = score(channels, (PA, PB))
    assert result["tier"]["tier"] == 1
    stop = result["stop_binding"]
    assert stop["channel"] is None
    assert "2 or more valid latency repetitions" in stop["reason"]
    assert "signal-loss stop" in stop["stop_path"]
    profile = calibration.build_profile(result, calibration.parse_records(build_records(channels, (PA, PB))))
    assert profile["stop_binding"]["channel"] is None
    assert "stop binding: null" in calibration.format_report(result)


def test_no_tier_means_no_stop_binding():
    result = score({CH_A: Channel(probe="a", amps=(0.0,) * 3)}, (PA,))
    assert result["tier"]["tier"] is None
    assert result["stop_binding"]["channel"] is None
