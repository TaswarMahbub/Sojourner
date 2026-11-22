"""Targeted probe session: measure a proposed cue set and the cross-talk between its gestures.

Phase 3 discovery (calibrate.py) asks "which movements can this person
make?". This session asks a narrower question about a proposed mapping:
can these specific gestures be bound to cues without one of them firing
another one's cue? It answers with measurements, not with a choice made in
advance.

Protocol (plan_targeted). No free exploration and no confirm step: the
gestures are already chosen. Each gesture is three repetitions, each
preceded by a lead-in, with a release between repetitions and a rest after
the last; then a clean rest baseline at the end, which is the rest
reference every threshold comes from. Order:

    jaw open, head lift, head down, head turn right, head turn left,
    head tilt right, head tilt left

Both directions of all three head axes are prompted, so symmetric options
are measured rather than assumed.

Timing (TargetedTiming, every interval a command-line flag):

    lead-in    1.0 s   "Ready... next: <prompt>"   --lead-in
    perform    2.0 s   the GO prompt                --perform
    release    3.0 s   "Relax."                     --release
    rest       3.5 s   after the third repetition   --rest
    baseline  10.0 s   final rest, the reference    --baseline

so one gesture is 3 x (1.0 + 2.0) + 2 x 3.0 + 3.5 = 18.5 s and the seven
take 129.5 s, plus the 10 s baseline: 139.5 s after the warm-up. The Phase 3
discovery timing (0.9 s perform, 0.5 s release, 1.3 s rest) left no time to
return from a movement before the next prompt; Phase 3 measured about 3 s
of relaxation after voluntary effort on this face. Prompting during that
tail corrupts latency (the channel is not below threshold at the prompt),
rest quality (the "rest" is recovery), and cross-talk (the tail is read as
coupling to the next gesture). The release and rest defaults are sized to
that 3 s.

LATENCY IS MEASURED FROM THE GO PROMPT, NOT FROM THE LEAD-IN. The lead-in is
its own segment (PROBE_LEAD_IN) before each repetition, and the repetition
segment's marker carries prompt_issued_ms, the moment the GO prompt was
shown. calibration.repetition_metrics measures from that timestamp, so the
number is the time from "do it now" to the first sustained threshold
crossing: a reaction time. Measuring from the lead-in would fold in the
one-second foreperiod and make every latency about 1 s longer than the
reaction; measuring from nothing, or from a segment boundary, would measure
nothing. A movement started during the lead-in is still above threshold at
the GO prompt, so Phase 3 excludes its latency (not_below_threshold_at_prompt,
or anticipatory below 100 ms) rather than report a false fast onset.

Caution for the Stop binding. With a fixed 1 s warning, this is a WARNED
reaction time. It is shorter than the unwarned reaction a real stop needs,
and it is not comparable with the Phase 3 discovery latencies, which had no
lead-in. Use these latencies to compare gestures with each other, not as
the stop latency the chair will see.

Recording. The Phase 3 recording format (calibration.py, RECORDING FORMAT),
so calibration.load_recording() reads it and this module rescores it from
file. One segment kind is added, PROBE_LEAD_IN; Phase 3 scoring ignores
segments it does not use, so a lead-in is neither a repetition nor held-out
rest. The header's protocol records the timing actually configured
(timing_ms, including lead_in_ms), the expected protocol duration, and that
latency is measured from the GO prompt. A report always prints the timing
it was produced under.

Metrics per gesture are the Phase 3 metrics from calibration.score_channel,
unchanged: latency per repetition and median, repeatability CV, false
positives in held-out rest, and the peak excursion (sustained peak per
repetition and usable_range, their median), plus the Phase 3 disqualifiers
and rest-quality checks. One difference in how the scorer is called, not in
what it computes: score_session keys channels by name, and here pitch_deg,
yaw_deg and roll_deg each belong to two gestures, so score_channel is
called once per (gesture, channel) pair. The direction of each pair is the
majority sign of that gesture's repetition peaks, as in Phase 3.

Cross-talk (the one measurement Phase 3 does not make). For a gesture A
that was performed and every other gesture B, how far B's channel moved
toward B's own threshold, ON B'S SIDE, while A was performed:

    fraction = sustained level of B's channel beyond its rest mean, on B's side
               / B's threshold offset

A sustained level is the Phase 3 definition (calibration.sustained_peak):
the largest level held for min_sustained_frames consecutive tracking
frames. A fraction >= 1.0 means B's cue would have fired during A; the
number of sustained crossings is reported alongside. Two phases:

    performing  A's repetition segments
    releasing   A's release segments and the rest after A

Overlap is not coupling. A repetition of A is measurable for B only if B's
channel was back inside B's threshold at that repetition's GO prompt
(calibration.inside_threshold_before, the rule Phase 3 uses before a
latency). If it was not, whatever B's channel does during that repetition
and its release may be the tail of an earlier movement (B's own gesture, or
anything else that moved that channel), so that repetition is excluded from
the cell. A cell with no measurable repetition is UNMEASURABLE and has no
fraction. A cell with some repetitions excluded reports the measurable ones
and says how many.

Thresholds. Phase 3 set each threshold from how still the person sits:
max(5 sigma of the final rest, half the unit's minimum excursion). With a
genuinely still rest 5 sigma collapses and the floor decides, and that floor
came from a contaminated session: in 20261122T073621Z yaw and roll sat on it
at 2.19 and 2.0 degrees against voluntary movements of 48 and 15 degrees. A
threshold that small on a head axis fires on breathing. This session scores
with the corrected rule (calibration.threshold_offsets, "Voluntary term"):

    threshold = rest_mean +/- max(5 sigma, absolute floor, VOLUNTARY_K * voluntary_range)

where voluntary_range is THAT GESTURE's usable_range. Each side of a head
axis therefore gets its own threshold (head lift and head down are scored
as separate gestures with separate ranges). The deadband scales with it
(deadband_k / threshold_k of each term). --voluntary-k 0 gives the Phase 3
rule back.

Derivation of VOLUNTARY_K, from 20261122T073621Z (one session, so
PROVISIONAL). For every cross-talk cell, take the coupling level (the
sustained level of the target's channel on the target's side, measurable
repetitions only) divided by the TARGET gesture's voluntary range. A
coupling is removed by the threshold exactly when that ratio is below k.

    lower bound: the couplings sorted by ratio fall into two groups with
      nothing between them. The largest fixable one is jaw_open -> head_lift
      at 0.282 (10.5 deg of head lift against a 37.2 deg range). The
      smallest of the rest is tilt_right -> head_down at 0.556. So k must
      exceed 0.282, and no k up to 0.556 removes anything more.
    upper bound: every gesture's weakest counted repetition must clear its
      own threshold with a factor of 2, the headroom Phase 3 already
      requires (snr_floor / threshold_k = 2; threshold_floor_fraction = 0.5).
      The weakest relative repetition is tilt_left, 12.0 deg against a
      15.8 deg range, ratio 0.763, so k <= 0.763 / 2 = 0.381.
    choice: (0.282, 0.381]. VOLUNTARY_K = 0.33, the geometric mean of the
      two bounds, the same convention Phase 3 uses for MIN_EXCURSION. The
      largest fixable coupling then reaches 0.85 of its threshold, and the
      weakest repetition is 2.3 times its threshold. Above about 0.42, the
      larger thresholds let more probe rests qualify as rest and pitch
      becomes rest_suspect, a second reason the upper bound is real.

The interval is a factor of only 1.35 wide and comes from one session.
Re-derive it from every new session before trusting it.

CROSSTALK_WARN (0.5) is a reporting margin, PROVISIONAL like every scoring
constant: a gesture that drives another cue's channel past half its
threshold has no room for a bad day, even though it did not fire.

    python -m perception.probe_session --source camera --user ID
    python -m perception.probe_session --source fake --user ID       dry run
    python -m perception.probe_session --rescore PATH.jsonl          no camera

--rescore also reads a Phase 3 discovery recording; it then reports the
gestures that recording contains (one direction per head axis) for the
target channels only, under that recording's own (Phase 3) timing.

Recordings are human data and go under captures/, which git ignores.
Standard library only at module level, as in calibrate.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import calibrate, calibration  # noqa: E402
from perception.calibration import (  # noqa: E402
    DEFAULT_CONSTANTS,
    HEADER,
    MARKER,
    PROBE_RELEASE,
    PROBE_REP,
    PROBE_REST,
    PROBE_REST_PROMPT,
    RECORDING_SCHEMA_VERSION,
    RELEASE_PROMPT,
    REST_BASELINE,
    REST_BASELINE_PROMPT,
    SESSION_END,
    WARMUP,
    CrossingDetector,
    PlannedSegment,
    Probe,
    Recording,
    ScoringConstants,
    Series,
    Window,
)

PROTOCOL_KIND = "targeted_probe"
REPORT_RECORD = "probe_report"
REPORT_SCHEMA_VERSION = 2
PROBE_LEAD_IN = "probe_lead_in"
LATENCY_REFERENCE = "GO prompt: prompt_issued_ms of each probe_rep marker, not the lead-in"

# Prompt order is the order asked for. Prompts follow the Phase 3 wording
# ("Tilt your head up", "Turn your head to the left", "Tilt your head
# toward your left shoulder") so the two sessions ask for the same movement.
TARGETED_PROBES = (
    Probe("jaw_open", "Open your mouth", ("jawOpen",)),
    Probe("head_lift", "Tilt your head up", ("pitch_deg",)),
    Probe("head_down", "Tilt your head down", ("pitch_deg",)),
    Probe("turn_right", "Turn your head to the right", ("yaw_deg",)),
    Probe("turn_left", "Turn your head to the left", ("yaw_deg",)),
    Probe("tilt_right", "Tilt your head toward your right shoulder", ("roll_deg",)),
    Probe("tilt_left", "Tilt your head toward your left shoulder", ("roll_deg",)),
)
TARGET_CHANNELS = ("jawOpen", "pitch_deg", "yaw_deg", "roll_deg")


@dataclass(frozen=True)
class TargetedTiming:
    """Segment durations in milliseconds. See "Timing" in the module docstring.

    lead_in_ms 0 runs no lead-in. probe_rest_ms must exceed
    held_out_settle_ms (500 ms), or no held-out rest is evaluated, and
    rest_baseline_ms must exceed rest_baseline_settle_ms (1000 ms), or there
    is no rest reference.
    """

    lead_in_ms: int = 1000
    rep_ms: int = 2000
    release_ms: int = 3000
    probe_rest_ms: int = 3500
    repetitions: int = 3
    rest_baseline_ms: int = 10000

    def __post_init__(self):
        c = DEFAULT_CONSTANTS
        if self.lead_in_ms < 0:
            raise ValueError("lead-in must be 0 or more")
        if self.rep_ms <= 0 or self.release_ms <= 0 or self.repetitions < 1:
            raise ValueError("perform and release must be positive and there must be at least one repetition")
        if self.probe_rest_ms <= c.held_out_settle_ms:
            raise ValueError(f"rest must exceed {c.held_out_settle_ms} ms, the held-out settle time, or no "
                             "held-out rest is evaluated")
        if self.rest_baseline_ms <= c.rest_baseline_settle_ms:
            raise ValueError(f"baseline must exceed {c.rest_baseline_settle_ms} ms, the baseline settle time")


DEFAULT_TARGETED_TIMING = TargetedTiming()

# docs/contracts.md section 1 "Angles", on an unmirrored image: positive yaw
# turns the nose to the subject's left, positive pitch raises the nose,
# positive roll lowers the subject's right ear. A measured direction that
# disagrees means a mirrored source or a gesture made the other way.
EXPECTED_SIGN = {
    "jaw_open": 1, "head_lift": 1, "head_down": -1, "turn_right": -1, "turn_left": 1,
    "tilt_right": 1, "tilt_left": -1,
    # Phase 3 probe names, for rescoring a discovery recording.
    "jaw": 1, "nod": 1, "turn": 1, "tilt": -1,
}

CROSSTALK_WARN = 0.5
CROSSED = 1.0

# See "Derivation of VOLUNTARY_K" in the module docstring. PROVISIONAL.
VOLUNTARY_K = 0.33
PROBE_CONSTANTS = replace(DEFAULT_CONSTANTS, voluntary_k=VOLUNTARY_K)


# --- Protocol ----------------------------------------------------------------


def lead_in_prompt(prompt: str) -> str:
    return f"Ready... next: {prompt}"


def plan_targeted(probes=TARGETED_PROBES, timing: TargetedTiming = DEFAULT_TARGETED_TIMING) -> list[PlannedSegment]:
    """Probe blocks then the rest baseline, contiguous.

    Each repetition is a lead-in (when lead_in_ms > 0) then the GO segment
    (PROBE_REP). A release follows every repetition but the last, and a
    rest follows the last.
    """
    plan: list[PlannedSegment] = []
    offset = 0

    def add(segment, probe, repetition, prompt, duration):
        nonlocal offset
        plan.append(PlannedSegment(segment, probe, repetition, prompt, offset, duration))
        offset += duration

    for probe in probes:
        for repetition in range(1, timing.repetitions + 1):
            prompt = f"{probe.prompt} ({repetition} of {timing.repetitions})"
            if timing.lead_in_ms > 0:
                add(PROBE_LEAD_IN, probe.name, repetition, lead_in_prompt(prompt), timing.lead_in_ms)
            add(PROBE_REP, probe.name, repetition, prompt, timing.rep_ms)
            if repetition < timing.repetitions:
                add(PROBE_RELEASE, probe.name, repetition, RELEASE_PROMPT, timing.release_ms)
        add(PROBE_REST, probe.name, None, PROBE_REST_PROMPT, timing.probe_rest_ms)
    add(REST_BASELINE, None, None, REST_BASELINE_PROMPT, timing.rest_baseline_ms)
    return plan


def expected_duration_ms(probes=TARGETED_PROBES, timing: TargetedTiming = DEFAULT_TARGETED_TIMING) -> int:
    """Protocol length after the warm-up."""
    plan = plan_targeted(probes, timing)
    return plan[-1].offset_ms + plan[-1].duration_ms


def describe_timing(timing: TargetedTiming, probes=TARGETED_PROBES, warmup_seconds: float | None = None) -> str:
    """One line for the operator, printed before the session starts."""
    total = expected_duration_ms(probes, timing) / 1000.0
    warm = 0.0 if warmup_seconds is None else warmup_seconds
    minutes, seconds = divmod(round(total + warm), 60)
    return (f"protocol: {len(probes)} gestures x {timing.repetitions} reps; lead-in {timing.lead_in_ms / 1000:g} s, "
            f"perform {timing.rep_ms / 1000:g} s, release {timing.release_ms / 1000:g} s, "
            f"rest {timing.probe_rest_ms / 1000:g} s, baseline {timing.rest_baseline_ms / 1000:g} s; "
            f"expected {minutes} min {seconds:02d} s ({total:g} s"
            + ("" if warmup_seconds is None else f" + {warm:g} s warm-up") + ")")


def protocol_record(probes, timing: TargetedTiming) -> dict:
    """The header's protocol: the timing actually used, the probes, and the latency reference."""
    return {
        "timing_ms": asdict(timing),
        "probes": [{"name": p.name, "prompt": p.prompt, "channels": list(p.channels)} for p in probes],
        "expected_duration_ms": expected_duration_ms(probes, timing),
        "latency_reference": LATENCY_REFERENCE,
        "segments": [PROBE_LEAD_IN, PROBE_REP, PROBE_RELEASE, PROBE_REST, REST_BASELINE],
    }


def run_targeted_session(frames, recorder: calibrate.Recorder, *, user_id: str, source: str, warmup_seconds: float,
                         clock=None, probes=TARGETED_PROBES, timing: TargetedTiming = DEFAULT_TARGETED_TIMING,
                         session_id: str | None = None) -> None:
    """Warm up, run the targeted protocol, close the recording. Same records as calibrate.run_session.

    clock and segment pacing behave exactly as in calibrate.run_session: a
    segment starts on the first frame at or past its planned offset, and its
    marker's prompt_issued_ms is when that prompt was shown.
    """
    frames = iter(frames)
    warmup = calibrate.collect_warmup(frames, warmup_seconds)
    messages = [f.message for f in warmup]
    started_ms = messages[0]["timestamp_ms"]
    recorder.emit({
        "record_type": HEADER,
        "recording_schema_version": RECORDING_SCHEMA_VERSION,
        "protocol_kind": PROTOCOL_KIND,
        "session_id": session_id or calibrate.session_id_now(),
        "user_id": user_id,
        "source": source,
        "measured": source == "camera",
        "started_ms": started_ms,
        "warmup_ms": int(warmup_seconds * 1000),
        "protocol": protocol_record(probes, timing),
        "brightness": calibrate.brightness_summary(warmup),
        "frame_rate_hz": calibration.frame_rate_hz(messages),
        "brightness_and_frame_rate_window": "warm-up before the protocol",
        "lighting_note": ("frame rate and rest noise depend on scene brightness; thresholds derived from this "
                          "session are valid only for lighting near this brightness"),
    })
    recorder.emit({"record_type": MARKER, "event": "segment_start", "segment": WARMUP, "probe": None,
                   "repetition": None, "prompt": "Warming up.", "start_ms": started_ms,
                   "prompt_issued_ms": started_ms, "planned_duration_ms": int(warmup_seconds * 1000)})
    for frame in warmup:
        recorder.frame(frame)

    plan = plan_targeted(probes, timing)
    origin_ms = messages[-1]["timestamp_ms"] + 1
    end_ms = origin_ms + plan[-1].offset_ms + plan[-1].duration_ms
    all_frames = list(warmup)
    next_index = 0
    for frame in frames:
        ts = frame.message["timestamp_ms"]
        if ts >= end_ms:
            break
        while next_index < len(plan) and ts >= origin_ms + plan[next_index].offset_ms:
            planned = plan[next_index]
            issued = ts if clock is None else max(clock(), ts)
            recorder.emit(calibrate.marker(planned, origin_ms + planned.offset_ms, issued))
            next_index += 1
        recorder.frame(frame)
        all_frames.append(frame)
    if next_index < len(plan):
        raise RuntimeError("the source ended before the protocol reached the rest baseline")
    recorder.emit({"record_type": SESSION_END, "ended_ms": end_ms, "frames": len(all_frames),
                   "protocol_duration_ms": end_ms - origin_ms,
                   "brightness": calibrate.brightness_summary(all_frames),
                   "frame_rate_hz": calibration.frame_rate_hz([f.message for f in all_frames])})


# --- Scoring -----------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One gesture as a would-be cue: its channel, its side and its threshold."""

    gesture: str
    channel: str
    side: int
    rest_mean: float
    offset: float


def sustained_level(deviations: list[float | None], side: int, min_frames: int) -> float | None:
    """Largest level held for min_frames consecutive frames on one side of rest.

    calibration.sustained_peak restricted to `side`: over every run of
    min_frames consecutive non-None deviations, the run's minimum of
    side * deviation; the largest of those, floored at 0. None when no run
    exists.
    """
    best = None
    for start in range(len(deviations) - min_frames + 1):
        run = deviations[start:start + min_frames]
        if any(v is None for v in run):
            continue
        level = min(side * v for v in run)
        best = level if best is None else max(best, level)
    return None if best is None else max(best, 0.0)


def phase_windows(all_windows: list[Window], probe: str) -> dict[str, list[tuple[Window, int]]]:
    """Each phase's windows, each paired with the GO prompt time of the repetition it belongs to.

    performing: each repetition, with its own GO prompt. releasing: the
    release after repetition k with k's GO prompt, and the rest after the
    last repetition with the last GO prompt. The lead-in belongs to neither.
    """
    reps = sorted((w for w in all_windows if w.probe == probe and w.segment == PROBE_REP), key=lambda w: w.repetition)
    go = {w.repetition: w.prompt_issued_ms for w in reps}
    last = reps[-1].repetition if reps else None
    releasing = []
    for w in all_windows:
        if w.probe != probe:
            continue
        if w.segment == PROBE_RELEASE and w.repetition in go:
            releasing.append((w, go[w.repetition]))
        elif w.segment == PROBE_REST and last is not None:
            releasing.append((w, go[last]))
    return {"performing": [(w, go[w.repetition]) for w in reps], "releasing": releasing}


def crosstalk_cell(series: Series, target: Target, windows_: list[tuple[Window, int]], c: ScoringConstants) -> dict:
    """Peak fraction of target's threshold on target's side, over the measurable windows.

    A window is measurable only when target's channel was inside its own
    threshold at the GO prompt of the repetition the window belongs to
    (see "Overlap is not coupling"). With none measurable the cell is
    unmeasurable and its fraction is None.
    """
    level = None
    crossings = 0
    measured = 0
    overlap = 0
    for w, go_ms in windows_:
        if not calibration.inside_threshold_before(series, target.channel, target.rest_mean, target.offset, go_ms, c):
            overlap += 1
            continue
        measured += 1
        _, values = series.window(target.channel, w.start_ms, w.end_ms)
        deviations = [None if v is None else v - target.rest_mean for v in values]
        lv = sustained_level(deviations, target.side, c.min_sustained_frames)
        if lv is not None:
            level = lv if level is None else max(level, lv)
        detector = CrossingDetector(target.offset, c.min_sustained_frames, target.side)
        crossings += sum(1 for v in deviations if detector.update(v))
    return {
        "fraction": None if level is None else level / target.offset,
        "level": level,
        "crossings": crossings,
        "windows": len(windows_),
        "measured": measured,
        "overlap": overlap,
        "unmeasurable": measured == 0 and len(windows_) > 0,
    }


def threshold_rule(c: ScoringConstants) -> str:
    voluntary = f", {c.voluntary_k:g} * voluntary_range" if c.voluntary_k > 0 else ""
    floors = ", ".join(f"{u} {c.threshold_floor_fraction * v:g}" for u, v in c.min_excursion.items())
    return f"rest_mean +/- max({c.threshold_k:g} sigma, floor [{floors}]{voluntary})"


def gesture_record(probe: Probe, channel: str, m: dict, reasons: list[str], rest_q: dict) -> dict:
    """The per-gesture report: Phase 3 metrics only."""
    reps = m["repetitions"]
    fp = m["false_positives"] or {}
    threshold = m["threshold"] or {}
    return {
        "gesture": probe.name,
        "prompt": probe.prompt,
        "channel": channel,
        "unit": m["unit"],
        "direction": m["direction"],
        "expected_sign": EXPECTED_SIGN.get(probe.name),
        "rep_signs": [r["sign"] for r in reps],
        "rest_mean": m["rest"]["mean"],
        "rest_std": m["rest"]["std"],
        "threshold_offset": threshold.get("offset"),
        "threshold_floored": threshold.get("floored"),
        "threshold_set_by": threshold.get("set_by", "floor" if threshold.get("floored") else "noise"),
        "deadband_offset": (m["deadband"] or {}).get("offset"),
        "latency_ms": [r["latency_ms"] for r in reps],
        "latency_excluded": [r["latency_excluded"] for r in reps],
        "latency_ms_median": m["latency_ms_median"],
        "repeatability_cv": m["repeatability_cv"],
        "peaks": [r["peak"] for r in reps],
        "peak_counted": [r["counted"] for r in reps],
        "usable_range": m["usable_range"],
        "snr": m["snr"],
        "false_positives": fp.get("crossings"),
        "false_positives_per_minute": fp.get("per_minute"),
        "held_out_seconds": fp.get("held_out_seconds"),
        "held_out_excluded": fp.get("excluded_windows", []),
        "rest_quality_reasons": rest_q["reasons"],
        "disqualifiers": reasons,
        "usable": not reasons,
    }


def score_probe_session(recording: Recording, constants: ScoringConstants = PROBE_CONSTANTS,
                        channels: tuple[str, ...] | None = None) -> dict:
    """Per-gesture Phase 3 metrics and the overlap-aware cross-talk matrix.

    channels limits the gestures to those whose probe exercises one of
    these channels (used when rescoring a Phase 3 discovery recording); None
    takes every probe in the header.
    """
    c = constants
    protocol = recording.header.get("protocol") or {}
    if "timing_ms" not in protocol:
        raise ValueError("the recording header does not say what timing produced it; refusing to report")
    probes = [p for p in calibration.probes_from_header(recording.header)
              if channels is None or set(p.channels) & set(channels)]
    series = Series(recording.messages)
    all_windows = calibration.windows(recording)
    sw = calibration.group_windows(all_windows)

    gestures = []
    targets = []
    for probe in probes:
        for channel in probe.channels:
            if channels is not None and channel not in channels:
                continue
            m = calibration.score_channel(series, channel, probe.name, sw, c)
            qualifying = [Window(PROBE_REST, p, None, "", s, e, s)
                          for s, e, p in (m["false_positives"] or {}).get("qualifying_windows", [])]
            rest_q = calibration.channel_rest_quality(series, channel, sw, c, qualifying)
            reasons = calibration.disqualifiers(m, c) + (["rest_suspect"] if rest_q["reasons"] else [])
            gestures.append(gesture_record(probe, channel, m, reasons, rest_q))
            if m["threshold"] is not None and m["direction"] is not None:
                targets.append(Target(probe.name, channel, m["direction"], m["rest"]["mean"], m["threshold"]["offset"]))

    crosstalk = {}
    for probe in probes:
        phases = phase_windows(all_windows, probe.name)
        row = {}
        for target in targets:
            if target.gesture == probe.name:
                continue
            row[target.gesture] = {name: crosstalk_cell(series, target, ws, c) for name, ws in phases.items()}
        crosstalk[probe.name] = row

    names = [n for n in TARGET_CHANNELS if n in series.values]
    table = calibration.correlations(series, names, sw)
    return {
        "record_type": REPORT_RECORD,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "session_id": recording.header.get("session_id"),
        "protocol_kind": recording.header.get("protocol_kind", "discovery"),
        "source": recording.header.get("source"),
        "timing_ms": protocol["timing_ms"],
        "threshold_rule": threshold_rule(c),
        "latency_reference": protocol.get("latency_reference", "prompt_issued_ms of each probe_rep marker"),
        "gestures": gestures,
        "crosstalk": crosstalk,
        "correlations": {a: {b: r for b, r in row.items()} for a, row in table.items()},
        "lighting_quality": calibration.lighting_quality(recording, all_windows, c),
        "crosstalk_warn": CROSSTALK_WARN,
        "constants": calibration.constants_record(c),
        "note": "Measurement report only. Not part of docs/contracts.md. No cue mapping is written from it.",
    }


def worst(report: dict, source: str, target: str) -> dict | None:
    """The larger measurable phase for source -> target; None when absent or wholly unmeasurable."""
    cells = report["crosstalk"].get(source, {}).get(target)
    if not cells:
        return None
    best = None
    for phase, cell in cells.items():
        if cell["fraction"] is not None and (best is None or cell["fraction"] > best["fraction"]):
            best = {**cell, "phase": phase}
    return best


def unmeasurable(report: dict, source: str, target: str) -> bool:
    cells = report["crosstalk"].get(source, {}).get(target)
    return bool(cells) and all(cell["unmeasurable"] for cell in cells.values())


def verdicts(report: dict) -> list[dict]:
    """The questions the mapping turns on, answered from the measurable cells.

    "crossed": a sustained crossing (fraction >= 1.0) in a measurable
    repetition. "margin": 0.5 <= fraction < 1.0. "unmeasurable": every cell
    the question needs was overlap. "not measured": a gesture it needs is
    absent. Unmeasurable cells are listed beside any status, because a
    "clean" built on some cells says nothing about the others.
    """
    present = {g["gesture"] for g in report["gestures"]}
    jaw = next((g for g in ("jaw_open", "jaw") if g in present), None)
    pairs = []

    def check(question: str, sources: list[str], targets: list[str]):
        cells, blind = [], []
        for s in sources:
            for t in targets:
                if s in present and t in present and s != t:
                    if unmeasurable(report, s, t):
                        blind.append(f"{s}->{t}")
                        continue
                    cell = worst(report, s, t)
                    if cell is not None:
                        cells.append({"source": s, "target": t, **cell})
        if not cells:
            status = "unmeasurable" if blind else "not measured"
            pairs.append({"question": question, "status": status, "cells": [], "unmeasurable": blind})
            return
        top = max(cells, key=lambda x: x["fraction"])
        status = "crossed" if top["fraction"] >= CROSSED else "margin" if top["fraction"] >= CROSSTALK_WARN else "clean"
        pairs.append({"question": question, "status": status, "worst": top, "cells": cells, "unmeasurable": blind})

    heads = sorted(g for g in present if g != jaw)
    if jaw:
        check("jawOpen (Stop) during head movements", heads, [jaw])
        check("head movements during jaw open", [jaw], heads)
    check("pitch up vs pitch down", ["head_lift", "head_down"], ["head_lift", "head_down"])
    check("yaw during tilt", ["tilt_right", "tilt_left", "tilt"], ["turn_right", "turn_left", "turn"])
    check("roll during turn", ["turn_right", "turn_left", "turn"], ["tilt_right", "tilt_left", "tilt"])
    check("yaw right vs left", ["turn_right", "turn_left"], ["turn_right", "turn_left"])
    check("roll right vs left", ["tilt_right", "tilt_left"], ["tilt_right", "tilt_left"])
    return pairs


# --- Control profile -----------------------------------------------------------


def profile_from_report(report: dict, channels: tuple[str, ...], stop_channel: str, accepted_by: str) -> dict:
    """A Tier 2 calibration_profile for control/tiers.py, built from a probe report.

    One discrete binding per channel. Each side of a channel takes the
    threshold and deadband of the gesture measured on that side, so the two
    sides of a head axis keep their own voluntary thresholds. A side with no
    gesture mirrors the measured side (it is then bindable but unmeasured;
    control/cue_map.py binds only measured sides). The binding's direction,
    active threshold and usable range are those of the stop gesture on the
    stop channel, else of the first gesture on the channel.

    The probe session has no confirm step, so acceptance is stated, not
    inferred: accepted_by names who reviewed the report and chose the
    channels, and is recorded. Refused when a gesture on a requested channel
    is not usable, or the lighting was not ok.
    """
    if not accepted_by:
        raise ValueError("accepted_by is required: the probe session has no confirm step")
    if report["lighting_quality"]["status"] != "ok":
        raise ValueError("lighting was not ok in this session")
    bindings = []
    for channel in channels:
        gestures = [g for g in report["gestures"] if g["channel"] == channel]
        if not gestures:
            raise ValueError(f"{channel} was not measured in this session")
        bad = [g["gesture"] for g in gestures if not g["usable"]]
        if bad:
            raise ValueError(f"{channel}: gestures not usable: {', '.join(bad)}")
        rest = gestures[0]["rest_mean"]
        sides = {g["direction"]: g for g in gestures}
        if len(sides) != len(gestures):
            raise ValueError(f"{channel}: two gestures measured on the same side")
        lead = gestures[0]
        offsets = {side: (g["threshold_offset"], g["deadband_offset"]) for side, g in sides.items()}
        for side in (1, -1):
            if side not in offsets:
                offsets[side] = offsets[-side]
        bindings.append({
            "channel": channel, "role": "discrete", "unit": lead["unit"], "probe": lead["gesture"],
            "direction": lead["direction"], "rest_mean": rest, "rest_std": lead["rest_std"],
            "threshold": {"lower": rest - offsets[-1][0], "upper": rest + offsets[1][0],
                          "active": rest + lead["direction"] * offsets[lead["direction"]][0]},
            "deadband": {"lower": rest - offsets[-1][1], "upper": rest + offsets[1][1]},
            "usable_range": lead["usable_range"],
            "measured_sides": {("above" if s > 0 else "below"): g["gesture"] for s, g in sides.items()},
        })
    if stop_channel not in channels:
        raise ValueError("the stop channel must be one of the bound channels")
    return {
        "record_type": "calibration_profile",
        "schema_version": calibration.PROFILE_SCHEMA_VERSION,
        "source": "probe_report",
        "source_session_id": report["session_id"],
        "threshold_rule": report["threshold_rule"],
        "timing_ms": report["timing_ms"],
        "accepted": True,
        "accepted_by": accepted_by,
        "session_quality": "ok",
        "proposed_tier": {"tier": 2, "mode": "discrete",
                          "reason": "cue map chosen from the probe report's per-gesture metrics and cross-talk"},
        "stop_binding": {"channel": stop_channel, "stop_path": calibration.STOP_PATH_NOTE},
        "bindings": bindings,
        "note": "Built from a probe report, not from discovery. Not part of docs/contracts.md.",
    }


# --- Report ------------------------------------------------------------------


def _f(value, spec=".3g", none="-"):
    return none if value is None else format(value, spec)


def _phase_text(cell: dict) -> str:
    if cell["unmeasurable"]:
        return "ovl"
    text = _f(cell["fraction"], ".2f")
    return text + ("~" if cell["overlap"] else "")


def format_probe_report(report: dict) -> str:
    timing = ", ".join(f"{k}={v}" for k, v in report["timing_ms"].items())
    lines = [f"probe report  session {report['session_id']}  ({report['protocol_kind']}, source={report['source']})",
             f"timing_ms: {timing}",
             f"latency measured from: {report['latency_reference']}",
             f"threshold: {report['threshold_rule']}",
             f"lighting_quality={report['lighting_quality']['status']}", "", "PER GESTURE (Phase 3 metrics)"]
    head = (f"  {'gesture':<11}{'channel':<10}{'dir':>4}{'exp':>4} {'latency ms per rep':<22}{'med':>6}"
            f"{'CV':>7} {'peak per rep':<24}{'range':>8}{'thr':>7}{'SNR':>7}{'FP':>4}{'held s':>7}  verdict")
    lines.append(head)
    for g in report["gestures"]:
        lat = "/".join(_f(v, ".0f", "x") for v in g["latency_ms"])
        peaks = "/".join(_f(v, ".3g") + ("" if ok else "*") for v, ok in zip(g["peaks"], g["peak_counted"]))
        sign_ok = "" if g["expected_sign"] in (None, g["direction"]) else " SIGN-FLIPPED"
        verdict = "usable" if g["usable"] else "DISQUALIFIED: " + ", ".join(g["disqualifiers"])
        set_by = {"voluntary": "v", "floor": "f", "noise": "n"}.get(g["threshold_set_by"], "?")
        lines.append(
            f"  {g['gesture']:<11}{g['channel']:<10}{_f(g['direction'], '+d'):>4}{_f(g['expected_sign'], '+d'):>4} "
            f"{lat:<22}{_f(g['latency_ms_median'], '.0f'):>6}{_f(g['repeatability_cv'], '.3f'):>7} {peaks:<24}"
            f"{_f(g['usable_range'], '.3g'):>8}{_f(g['threshold_offset'], '.3g'):>6}{set_by}{_f(g['snr'], '.0f'):>7}"
            f"{_f(g['false_positives'], 'd'):>4}{_f(g['held_out_seconds'], '.1f'):>7}  {verdict}{sign_ok}")
    lines.append("  latency x = excluded (not below threshold at prompt, anticipatory or missed); peak * = missed rep")
    lines.append("  thr suffix: v = set by the voluntary term, f = by the absolute floor, n = by 5 sigma of rest")
    lines.append("")
    lines.append("CROSS-TALK: row gesture performed, column gesture's channel on its own side, as a fraction of its")
    lines.append(f"threshold; 'perform|release'; >= {CROSSED:g} fired (sustained) !!, >= {CROSSTALK_WARN:g} flagged !")
    lines.append("  ovl = unmeasurable: the column's channel was not back inside its threshold at any GO prompt of the")
    lines.append("        row gesture (overlap, not coupling); ~ = some repetitions excluded for overlap")
    order = [g["gesture"] for g in report["gestures"]]
    lines.append("  " + f"{'performed':<11}" + "".join(f"{t:>17}" for t in order))
    for source in order:
        cells = []
        for target in order:
            if target == source:
                cells.append(f"{'(self)':>17}")
                continue
            cell = report["crosstalk"].get(source, {}).get(target)
            if not cell:
                cells.append(f"{'-':>17}")
                continue
            p, r = cell["performing"], cell["releasing"]
            top = max((x["fraction"] for x in (p, r) if x["fraction"] is not None), default=0.0)
            mark = "!!" if top >= CROSSED else "! " if top >= CROSSTALK_WARN else "  "
            cells.append(f"{_phase_text(p)}|{_phase_text(r)}{mark}".rjust(17))
        lines.append("  " + f"{source:<11}" + "".join(cells))
    lines.append("")
    lines.append("QUESTIONS")
    for v in verdicts(report):
        blind = f"; unmeasurable: {', '.join(v['unmeasurable'])}" if v["unmeasurable"] else ""
        if v["status"] in ("not measured", "unmeasurable"):
            lines.append(f"  {v['question']:<40} {v['status']}{blind}")
            continue
        w = v["worst"]
        partial = f", {w['overlap']} of {w['windows']} windows excluded for overlap" if w["overlap"] else ""
        lines.append(f"  {v['question']:<40} {v['status']:<8} worst {w['source']} -> {w['target']} "
                     f"{w['fraction']:.2f} of threshold ({w['phase']}, {w['crossings']} sustained crossing(s){partial})"
                     f"{blind}")
    corr = report["correlations"]
    if "yaw_deg" in corr and "roll_deg" in corr["yaw_deg"]:
        lines.append(f"  Pearson r yaw_deg vs roll_deg over the session: {_f(corr['yaw_deg']['roll_deg'], '.2f')}")
    return "\n".join(lines)


# --- Command line ------------------------------------------------------------


def report_path_for(recording_path: Path) -> Path:
    """<stem>_probe_report.json; a targeted recording is already named <user>_<session>_probe.jsonl."""
    stem = recording_path.stem
    return recording_path.with_name((stem if stem.endswith("_probe") else stem + "_probe") + "_report.json")


def finish(recording_path: Path, voluntary_k: float = VOLUNTARY_K) -> int:
    recording = calibration.load_recording(recording_path)
    kind = recording.header.get("protocol_kind")
    constants = replace(DEFAULT_CONSTANTS, voluntary_k=voluntary_k)
    report = score_probe_session(recording, constants, channels=None if kind == PROTOCOL_KIND else TARGET_CHANNELS)
    path = report_path_for(recording_path)
    calibration.write_json(report, path)
    print(format_probe_report(report))
    print(f"recording {recording_path}")
    print(f"report    {path}")
    return 0


def _seconds(text: str) -> float:
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError("must be a finite number of seconds")
    return value


def timing_from_args(args: argparse.Namespace) -> TargetedTiming:
    return TargetedTiming(
        lead_in_ms=round(args.lead_in * 1000),
        rep_ms=round(args.perform * 1000),
        release_ms=round(args.release * 1000),
        probe_rest_ms=round(args.rest * 1000),
        repetitions=args.repetitions,
        rest_baseline_ms=round(args.baseline * 1000),
    )


def parse_args(argv=None) -> argparse.Namespace:
    d = DEFAULT_TARGETED_TIMING
    parser = argparse.ArgumentParser(description="Targeted probe session: measure a proposed cue set and its cross-talk.")
    parser.add_argument("--source", choices=("fake", "camera"), default="fake")
    parser.add_argument("--user", default="anonymous")
    parser.add_argument("--out-dir", type=Path, default=calibrate.DEFAULT_OUT_DIR)
    parser.add_argument("--warmup", type=_seconds, default=calibrate.DEFAULT_WARMUP_SECONDS,
                        help=f"seconds of camera warm-up before the protocol (default {calibrate.DEFAULT_WARMUP_SECONDS:g})")
    timing = parser.add_argument_group("protocol timing, in seconds")
    timing.add_argument("--lead-in", type=_seconds, default=d.lead_in_ms / 1000,
                        help=f"'Ready...' beat before each GO prompt; 0 disables it (default {d.lead_in_ms / 1000:g})")
    timing.add_argument("--perform", type=_seconds, default=d.rep_ms / 1000,
                        help=f"time to perform each repetition after GO (default {d.rep_ms / 1000:g})")
    timing.add_argument("--release", type=_seconds, default=d.release_ms / 1000,
                        help=f"relax between repetitions (default {d.release_ms / 1000:g})")
    timing.add_argument("--rest", type=_seconds, default=d.probe_rest_ms / 1000,
                        help=f"rest after each gesture's last repetition; also held-out rest (default {d.probe_rest_ms / 1000:g})")
    timing.add_argument("--baseline", type=_seconds, default=d.rest_baseline_ms / 1000,
                        help=f"final rest baseline, the threshold reference (default {d.rest_baseline_ms / 1000:g})")
    timing.add_argument("--repetitions", type=int, default=d.repetitions,
                        help=f"repetitions per gesture (default {d.repetitions})")
    parser.add_argument("--events", choices=("text", "json"), default="text")
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--rescore", type=Path, metavar="PATH",
                        help="score an existing recording (targeted or Phase 3) and write its probe report")
    parser.add_argument("--voluntary-k", type=float, default=VOLUNTARY_K,
                        help=f"threshold term as a fraction of each gesture's voluntary range; 0 is the Phase 3 "
                             f"rule (default {VOLUNTARY_K:g}, provisional, see the module docstring)")
    parser.add_argument("--rate", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--camera", type=int, default=0, metavar="INDEX")
    parser.add_argument("--mirrored", action="store_true")
    args = parser.parse_args(argv)
    if args.warmup <= 0 or args.rate <= 0:
        parser.error("--warmup and --rate must be positive")
    if not 0.0 <= args.voluntary_k < 1.0:
        parser.error("--voluntary-k must be in [0, 1)")
    try:
        args.timing = timing_from_args(args)
    except ValueError as error:
        parser.error(str(error))
    return args


def record(args: argparse.Namespace) -> Path:
    print(describe_timing(args.timing, warmup_seconds=args.warmup), file=sys.stderr, flush=True)
    listeners = [calibrate.JsonListener() if args.events == "json" else calibrate.TextListener()]
    session_id = calibrate.session_id_now()
    path = args.out_dir / f"{args.user}_{session_id}_probe.jsonl"
    if args.source == "fake":
        recorder = calibrate.Recorder(path, listeners)
        try:
            run_targeted_session(calibrate.fake_frames(args.rate, args.seed), recorder, user_id=args.user,
                                 source="fake", warmup_seconds=args.warmup, timing=args.timing, session_id=session_id)
        finally:
            recorder.close()
        return path

    from perception.mediapipe_source import CameraSource

    source = CameraSource(camera_index=args.camera, mirrored=args.mirrored)
    window = None
    try:
        frames = calibrate.camera_frames(source)
        if not args.no_window:
            window = calibrate.PromptWindow()
            window.wait_for_start(frames)
            listeners.append(window)
        recorder = calibrate.Recorder(path, listeners)
        try:
            run_targeted_session(frames, recorder, user_id=args.user, source="camera", warmup_seconds=args.warmup,
                                 clock=lambda: time.time_ns() // 1_000_000, timing=args.timing, session_id=session_id)
        finally:
            recorder.close()
    finally:
        if window is not None:
            window.close()
        source.close()
    return path


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.rescore is not None:
        return finish(args.rescore, args.voluntary_k)
    from perception.mediapipe_source import CameraError

    try:
        path = record(args)
    except CameraError as error:
        print(f"camera source failed: {error}", file=sys.stderr)
        return 1
    except calibrate.CalibrationAborted as error:
        print(f"probe session {error}; the partial recording is not scored", file=sys.stderr)
        return 130
    return finish(path, args.voluntary_k)


if __name__ == "__main__":
    sys.exit(main())
