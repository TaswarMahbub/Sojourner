"""Targeted probe session: timing, lead-in, recording format, Phase 3 metrics, overlap-aware cross-talk.

Synthetic recordings with known movements and known injections, so each
number in the report can be checked against what was put in.
"""

from __future__ import annotations

import json
import random

import pytest

from perception import calibrate, calibration, probe_session as ps
from perception.calibration import PROBE_RELEASE, PROBE_REP, PROBE_REST, REST_BASELINE
from perception.probe_session import PROBE_LEAD_IN, TargetedTiming
from tests.control_rig import tracking_frame

T0 = 1795276800000
STEP_MS = 40  # 25 Hz: the 2 s warm-up ends exactly on a frame
MOVE_AFTER_GO_MS = 200
REST = {"jawOpen": 0.002, "pitch_deg": 12.0, "yaw_deg": -1.0, "roll_deg": 0.0}
NOISE = {"jawOpen": 0.002, "pitch_deg": 0.2, "yaw_deg": 0.2, "roll_deg": 0.2}
GESTURE = {
    "jaw_open": ("jawOpen", 0.5), "head_lift": ("pitch_deg", 25.0), "head_down": ("pitch_deg", -20.0),
    "turn_right": ("yaw_deg", -45.0), "turn_left": ("yaw_deg", 45.0),
    "tilt_right": ("roll_deg", 12.0), "tilt_left": ("roll_deg", -12.0),
}


def coupling(seg, into, values):
    """Real coupling. Angle thresholds floor at 2 deg, so +3 deg is 1.5 thresholds and +1 deg is 0.5."""
    if seg.segment == PROBE_REP and into >= MOVE_AFTER_GO_MS:
        if seg.probe == "jaw_open":
            values["pitch_deg"] += 3.0
        if seg.probe == "tilt_left":
            values["yaw_deg"] += 1.0
    if seg.segment == PROBE_RELEASE and seg.probe == "head_lift" and 120 <= into < 1500:
        values["pitch_deg"] -= 3.0  # head drops through the head-down threshold on release


def tails(seg, into, values):
    """Movements that have not finished when the next GO prompt is shown."""
    coupling(seg, into, values)
    # Pitch still up from head_lift at head_down's first GO: overlap, not coupling.
    if seg.probe == "head_down" and seg.repetition == 1 and (
            seg.segment == PROBE_LEAD_IN or (seg.segment == PROBE_REP and into < 150)):
        values["pitch_deg"] += 5.0
    # The jaw never fully closes before any head_lift GO prompt.
    if seg.probe == "head_lift" and (seg.segment == PROBE_LEAD_IN or (seg.segment == PROBE_REP and into < 150)):
        values["jawOpen"] += 0.3
    # turn_right repetition 2 starts during "Ready...": anticipation.
    if seg.probe == "turn_right" and seg.repetition == 2 and seg.segment == PROBE_LEAD_IN and into >= 500:
        values["yaw_deg"] -= 45.0


def synthetic_frames(inject, timing: TargetedTiming = ps.DEFAULT_TARGETED_TIMING, seed: int = 3):
    rng = random.Random(seed)
    plan = ps.plan_targeted(timing=timing)
    origin = T0 + 2000 + 1
    ts = T0
    while True:
        values = {ch: REST[ch] + rng.gauss(0.0, NOISE[ch]) for ch in REST}
        offset = ts - origin
        seg = next((s for s in plan if s.offset_ms <= offset < s.offset_ms + s.duration_ms), None)
        if seg is not None:
            into = offset - seg.offset_ms
            if seg.segment == PROBE_REP and into >= MOVE_AFTER_GO_MS:
                channel, amount = GESTURE[seg.probe]
                values[channel] += amount
            inject(seg, into, values)
        frame = tracking_frame(ts)
        frame["features"]["jawOpen"] = min(1.0, max(0.0, values["jawOpen"]))
        for ch in ("pitch_deg", "yaw_deg", "roll_deg"):
            frame["head_pose"][ch] = values[ch]
        yield calibrate.Frame(frame, 120.0)
        ts += STEP_MS


def record(path, inject, timing=ps.DEFAULT_TARGETED_TIMING):
    recorder = calibrate.Recorder(path, [])
    try:
        ps.run_targeted_session(synthetic_frames(inject, timing), recorder, user_id="test", source="fake",
                                warmup_seconds=2.0, timing=timing, session_id="synthetic")
    finally:
        recorder.close()
    return path


@pytest.fixture(scope="module")
def clean_path(tmp_path_factory):
    return record(tmp_path_factory.mktemp("probe") / "clean_probe.jsonl", coupling)


# The coupling and overlap tests below are built on Phase 3 thresholds (angles floored
# at 2 deg), so they score with voluntary_k 0; the voluntary term has its own tests.
PHASE3 = calibration.DEFAULT_CONSTANTS


@pytest.fixture(scope="module")
def clean(clean_path):
    return ps.score_probe_session(calibration.load_recording(clean_path), PHASE3)


@pytest.fixture(scope="module")
def clean_voluntary(clean_path):
    return ps.score_probe_session(calibration.load_recording(clean_path))


@pytest.fixture(scope="module")
def overlapping(tmp_path_factory):
    path = record(tmp_path_factory.mktemp("probe") / "tails_probe.jsonl", tails)
    return ps.score_probe_session(calibration.load_recording(path), PHASE3)


def gesture(report, name):
    return next(g for g in report["gestures"] if g["gesture"] == name)


def cell(report, source, target, phase):
    return report["crosstalk"][source][target][phase]


def verdict(report, question):
    return next(v for v in ps.verdicts(report) if v["question"] == question)


# ----- timing and protocol ----------------------------------------------------


def test_default_timing_is_the_requested_one():
    t = ps.DEFAULT_TARGETED_TIMING
    assert (t.lead_in_ms, t.rep_ms, t.release_ms, t.probe_rest_ms, t.rest_baseline_ms, t.repetitions) == (
        1000, 2000, 3000, 3500, 10000, 3)
    # Per gesture 3 x (1.0 + 2.0) + 2 x 3.0 + 3.5 = 18.5 s; seven of them plus the 10 s baseline.
    assert ps.expected_duration_ms() == 7 * 18500 + 10000 == 139500


def test_plan_has_a_lead_in_before_every_go_prompt():
    plan = ps.plan_targeted()
    order = [s.probe for s in plan if s.segment == PROBE_REP and s.repetition == 1]
    assert order == ["jaw_open", "head_lift", "head_down", "turn_right", "turn_left", "tilt_right", "tilt_left"]
    assert {s.segment for s in plan} == {PROBE_LEAD_IN, PROBE_REP, PROBE_RELEASE, PROBE_REST, REST_BASELINE}
    for i, seg in enumerate(plan):
        if seg.segment == PROBE_REP:
            lead = plan[i - 1]
            assert lead.segment == PROBE_LEAD_IN and lead.duration_ms == 1000
            assert (lead.probe, lead.repetition) == (seg.probe, seg.repetition)
            assert lead.prompt == f"Ready... next: {seg.prompt}"
    assert plan[-1].segment == REST_BASELINE
    for a, b in zip(plan, plan[1:]):
        assert a.offset_ms + a.duration_ms == b.offset_ms


def test_lead_in_zero_runs_none():
    plan = ps.plan_targeted(timing=TargetedTiming(lead_in_ms=0))
    assert PROBE_LEAD_IN not in {s.segment for s in plan}


def test_timing_that_leaves_no_held_out_rest_or_baseline_is_refused():
    with pytest.raises(ValueError, match="held-out"):
        TargetedTiming(probe_rest_ms=500)
    with pytest.raises(ValueError, match="baseline"):
        TargetedTiming(rest_baseline_ms=1000)
    with pytest.raises(ValueError):
        TargetedTiming(lead_in_ms=-1)


def test_every_interval_is_a_flag_and_the_duration_is_announced():
    args = ps.parse_args(["--lead-in", "1.5", "--perform", "2.5", "--release", "4", "--rest", "5",
                          "--baseline", "12", "--repetitions", "2"])
    assert args.timing == TargetedTiming(1500, 2500, 4000, 5000, 2, 12000)
    assert ps.parse_args([]).timing == ps.DEFAULT_TARGETED_TIMING
    with pytest.raises(SystemExit):
        ps.parse_args(["--rest", "0.3"])
    text = ps.describe_timing(ps.DEFAULT_TARGETED_TIMING, warmup_seconds=2.0)
    assert "lead-in 1 s, perform 2 s, release 3 s, rest 3.5 s, baseline 10 s" in text
    assert "expected 2 min 22 s (139.5 s + 2 s warm-up)" in text


def test_header_records_the_timing_actually_used(tmp_path):
    custom = TargetedTiming(lead_in_ms=800, rep_ms=1500, release_ms=2500, probe_rest_ms=3000, rest_baseline_ms=8000)
    path = record(tmp_path / "custom_probe.jsonl", coupling, custom)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    header, end = lines[0], lines[-1]
    assert header["protocol_kind"] == ps.PROTOCOL_KIND
    assert header["protocol"]["timing_ms"] == {"lead_in_ms": 800, "rep_ms": 1500, "release_ms": 2500,
                                               "probe_rest_ms": 3000, "repetitions": 3, "rest_baseline_ms": 8000}
    assert header["protocol"]["expected_duration_ms"] == ps.expected_duration_ms(timing=custom)
    assert "GO prompt" in header["protocol"]["latency_reference"]
    assert end["record_type"] == "session_end" and end["protocol_duration_ms"] == ps.expected_duration_ms(timing=custom)
    report = ps.score_probe_session(calibration.load_recording(path))
    assert report["timing_ms"] == header["protocol"]["timing_ms"]
    assert "lead_in_ms=800" in ps.format_probe_report(report)


def test_a_report_without_timing_is_refused(clean_path):
    recording = calibration.load_recording(clean_path)
    del recording.header["protocol"]["timing_ms"]
    with pytest.raises(ValueError, match="timing"):
        ps.score_probe_session(recording)


def test_recording_is_the_phase3_format(clean_path):
    lines = [json.loads(line) for line in clean_path.read_text(encoding="utf-8").splitlines()]
    assert {r["record_type"] for r in lines} == {"session_header", "marker", "frame", "session_end"}
    recording = calibration.load_recording(clean_path)
    assert [p.name for p in calibration.probes_from_header(recording.header)] == [p.name for p in ps.TARGETED_PROBES]


# ----- per-gesture metrics -----------------------------------------------------


def test_each_head_axis_is_scored_once_per_direction(clean):
    names = [(g["gesture"], g["channel"], g["direction"]) for g in clean["gestures"]]
    assert names == [
        ("jaw_open", "jawOpen", 1), ("head_lift", "pitch_deg", 1), ("head_down", "pitch_deg", -1),
        ("turn_right", "yaw_deg", -1), ("turn_left", "yaw_deg", 1),
        ("tilt_right", "roll_deg", 1), ("tilt_left", "roll_deg", -1),
    ]
    assert all(g["usable"] for g in clean["gestures"]), [(g["gesture"], g["disqualifiers"]) for g in clean["gestures"]]


def test_latency_is_measured_from_the_go_prompt_not_the_lead_in(clean):
    """The movement starts 200 ms after GO, 1200 ms after "Ready...". 200 is the reaction time."""
    for g in clean["gestures"]:
        assert all(MOVE_AFTER_GO_MS <= v < MOVE_AFTER_GO_MS + 2 * STEP_MS for v in g["latency_ms"]), g


def test_release_and_lead_in_leave_time_to_return_before_the_next_go(clean):
    """The head_lift release overshoot that cost reps 2 and 3 their latency at the old timing no longer does."""
    lift = gesture(clean, "head_lift")
    assert lift["latency_excluded"] == [None, None, None]


def test_a_movement_started_during_the_lead_in_is_excluded_not_fast(overlapping):
    right = gesture(overlapping, "turn_right")
    assert right["latency_ms"][1] is None
    assert right["latency_excluded"][1] == "not_below_threshold_at_prompt"


def test_per_gesture_metrics_are_phase3(clean):
    down = gesture(clean, "head_down")
    assert down["usable_range"] == pytest.approx(20.0, abs=1.0)
    assert down["repeatability_cv"] < 0.05
    assert down["false_positives"] == 0 and down["held_out_seconds"] > 0
    assert down["threshold_offset"] == pytest.approx(2.0) and down["threshold_floored"] is True


# ----- cross-talk --------------------------------------------------------------


def test_crosstalk_finds_injected_coupling(clean):
    jaw_to_lift = cell(clean, "jaw_open", "head_lift", "performing")
    assert jaw_to_lift["fraction"] == pytest.approx(1.5, abs=0.25) and jaw_to_lift["crossings"] == 3
    assert jaw_to_lift["measured"] == 3 and jaw_to_lift["overlap"] == 0
    lift_to_down = cell(clean, "head_lift", "head_down", "releasing")
    assert lift_to_down["fraction"] == pytest.approx(1.5, abs=0.25) and lift_to_down["crossings"] == 2
    tilt_to_turn = cell(clean, "tilt_left", "turn_left", "performing")
    assert tilt_to_turn["fraction"] == pytest.approx(0.5, abs=0.25) and tilt_to_turn["crossings"] == 0
    for source in ("head_lift", "head_down", "turn_right", "turn_left", "tilt_right", "tilt_left"):
        for phase in ("performing", "releasing"):
            assert cell(clean, source, "jaw_open", phase)["fraction"] < 0.5


def test_crosstalk_is_side_specific(clean):
    assert cell(clean, "head_down", "head_lift", "performing")["fraction"] < 0.2  # rest noise only
    assert cell(clean, "turn_left", "turn_right", "performing")["fraction"] < 0.2


def test_a_channel_not_back_at_one_go_prompt_excludes_that_repetition(overlapping):
    c = cell(overlapping, "head_down", "head_lift", "performing")
    assert (c["windows"], c["measured"], c["overlap"], c["unmeasurable"]) == (3, 2, 1, False)
    assert c["fraction"] < 0.2, "the head_lift tail is not reported as coupling"
    released = cell(overlapping, "head_down", "head_lift", "releasing")
    assert released["overlap"] == 1, "the release after an overlapped repetition is excluded with it"


def test_a_channel_never_back_makes_the_cell_unmeasurable(overlapping):
    for phase in ("performing", "releasing"):
        c = cell(overlapping, "head_lift", "jaw_open", phase)
        assert c["unmeasurable"] and c["fraction"] is None and c["measured"] == 0
    v = verdict(overlapping, "jawOpen (Stop) during head movements")
    assert "head_lift->jaw_open" in v["unmeasurable"]
    assert all(x["source"] != "head_lift" for x in v["cells"])
    text = ps.format_probe_report(overlapping)
    assert "ovl|ovl" in text and "unmeasurable: head_lift->jaw_open" in text


def test_verdicts(clean):
    assert verdict(clean, "jawOpen (Stop) during head movements")["status"] == "clean"
    assert verdict(clean, "head movements during jaw open")["status"] == "crossed"
    pitch = verdict(clean, "pitch up vs pitch down")
    assert pitch["status"] == "crossed" and pitch["worst"]["source"] == "head_lift"
    assert pitch["worst"]["phase"] == "releasing"
    assert verdict(clean, "yaw during tilt")["status"] == "margin"
    assert verdict(clean, "roll during turn")["status"] == "clean"
    assert all(not v["unmeasurable"] for v in ps.verdicts(clean))


def test_report_text_and_file(clean_path, capsys):
    assert ps.finish(clean_path) == 0
    out = capsys.readouterr().out
    assert "timing_ms: lead_in_ms=1000, rep_ms=2000" in out and "latency measured from: GO prompt" in out
    assert "CROSS-TALK" in out and "pitch up vs pitch down" in out
    saved = json.loads(ps.report_path_for(clean_path).read_text(encoding="utf-8"))
    assert saved["record_type"] == ps.REPORT_RECORD and len(saved["gestures"]) == 7


def test_sustained_level_ignores_single_frame_spikes():
    assert ps.sustained_level([0.0, 5.0, 0.0, 1.0, 1.2, 0.0], 1, 2) == 1.0
    assert ps.sustained_level([0.0, -5.0, -4.0], 1, 2) == 0.0
    assert ps.sustained_level([None, 3.0, None], 1, 2) is None


# ----- the voluntary threshold term ---------------------------------------------


def test_phase3_rule_is_unchanged_when_the_term_is_off():
    c = calibration.DEFAULT_CONSTANTS
    assert c.voluntary_k == 0.0
    t = calibration.threshold_offsets(0.01, "deg", c, voluntary_range=40.0)
    assert t["threshold"] == 2.0 and t["floored"] is True and t["set_by"] == "floor"


def test_threshold_is_the_largest_of_noise_floor_and_voluntary_fraction():
    c = ps.PROBE_CONSTANTS
    assert c.voluntary_k == ps.VOLUNTARY_K == 0.33
    voluntary = calibration.threshold_offsets(0.01, "deg", c, voluntary_range=40.0)
    assert voluntary["threshold"] == pytest.approx(13.2) and voluntary["set_by"] == "voluntary"
    assert voluntary["floored"] is False
    assert voluntary["deadband"] == pytest.approx(13.2 * 3 / 5), "the deadband scales with the threshold"
    noisy = calibration.threshold_offsets(5.0, "deg", c, voluntary_range=40.0)
    assert noisy["threshold"] == 25.0 and noisy["set_by"] == "noise"
    small = calibration.threshold_offsets(0.01, "deg", c, voluntary_range=3.0)
    assert small["threshold"] == 2.0 and small["set_by"] == "floor"


def test_each_side_of_a_head_axis_gets_its_own_voluntary_threshold(clean_voluntary):
    lift, down = gesture(clean_voluntary, "head_lift"), gesture(clean_voluntary, "head_down")
    assert lift["threshold_offset"] == pytest.approx(0.33 * lift["usable_range"])
    assert down["threshold_offset"] == pytest.approx(0.33 * down["usable_range"])
    assert lift["threshold_offset"] > down["threshold_offset"]
    assert lift["threshold_set_by"] == down["threshold_set_by"] == "voluntary"
    assert all(g["usable"] for g in clean_voluntary["gestures"])


def test_couplings_below_the_voluntary_fraction_no_longer_fire(clean_voluntary):
    """The injected 3 deg of head lift during jaw open crosses a 2 deg floor but not 0.33 * 25 deg."""
    c = cell(clean_voluntary, "jaw_open", "head_lift", "performing")
    assert c["crossings"] == 0 and c["fraction"] == pytest.approx(3.0 / (0.33 * 25.0), abs=0.1)
    assert verdict(clean_voluntary, "head movements during jaw open")["status"] == "clean"


def test_voluntary_k_is_a_flag_and_printed():
    assert ps.parse_args([]).voluntary_k == ps.VOLUNTARY_K
    assert ps.parse_args(["--voluntary-k", "0"]).voluntary_k == 0.0
    with pytest.raises(SystemExit):
        ps.parse_args(["--voluntary-k", "1.2"])
    assert "0.33 * voluntary_range" in ps.threshold_rule(ps.PROBE_CONSTANTS)
    assert "voluntary" not in ps.threshold_rule(calibration.DEFAULT_CONSTANTS)


# ----- control profile from a probe report --------------------------------------


def test_profile_from_report_keeps_each_sides_own_threshold(clean_voluntary):
    from control.cue_map import CUE_MAP
    from control.task_controller import TaskController

    profile = ps.profile_from_report(clean_voluntary, ("jawOpen", "pitch_deg", "yaw_deg"), "jawOpen", "test")
    pitch = next(b for b in profile["bindings"] if b["channel"] == "pitch_deg")
    lift, down = gesture(clean_voluntary, "head_lift"), gesture(clean_voluntary, "head_down")
    assert pitch["threshold"]["upper"] == pytest.approx(pitch["rest_mean"] + lift["threshold_offset"])
    assert pitch["threshold"]["lower"] == pytest.approx(pitch["rest_mean"] - down["threshold_offset"])
    assert pitch["measured_sides"] == {"above": "head_lift", "below": "head_down"}
    assert profile["accepted_by"] == "test" and profile["source_session_id"] == "synthetic"
    assert len(TaskController(profile, CUE_MAP).reader.bindings) == 5


def test_profile_from_report_refuses_without_acceptance_or_with_an_unusable_gesture(clean_voluntary):
    with pytest.raises(ValueError, match="accepted_by"):
        ps.profile_from_report(clean_voluntary, ("jawOpen",), "jawOpen", "")
    broken = json.loads(json.dumps(clean_voluntary))
    gesture(broken, "turn_left")["usable"] = False
    with pytest.raises(ValueError, match="not usable"):
        ps.profile_from_report(broken, ("jawOpen", "yaw_deg"), "jawOpen", "test")


def test_doorway_run_with_the_synthetic_profile(capsys):
    from tests import run_doorway_e2e

    assert run_doorway_e2e.main(["run_doorway_e2e.py"]) == 0
    out = capsys.readouterr().out
    import re

    assert re.search(r"was hold\s+cues=go ", out) and re.search(r"was go\s+cues=stop ", out) and "cues=right" in out
    assert re.search(r"open jaw \(stop!\) \(2/2\).*lock\s+\d+ cues=stop .*request=None", out), "lockout reads as Stop"
