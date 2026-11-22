"""Calibration scoring: which movements can this person actually produce?

This is system identification, not a trained model. A recorded calibration
session is a short input-output experiment on one person: a prompt is the
input, the channel trajectories are the output, and this module measures the
response of each candidate channel (its range over rest noise, how repeatable
it is, how fast it starts, whether it tires, whether it moves on its own at
rest, and whether it merely echoes another channel). It then ranks the
channels with a fixed, documented heuristic and proposes a control tier.
Nothing is learned or fitted beyond means, standard deviations and
correlations.

Scoring reads only a recording (see RECORDING FORMAT below), never the
camera, so a change to scoring is re-run on old sessions without asking the
person to calibrate again. Standard library only.

Channels
--------
A channel is one scalar per tracking frame, as defined by
filters.channel_values: a blendshape score (unitless, [0, 1]), a head angle
in degrees (yaw_deg, pitch_deg, roll_deg) or a head translation in
centimetres (x_cm, y_cm, z_cm). Only "tracking" frames contribute values; a
"partial" or "lost" frame contributes nothing and breaks any run of
consecutive frames (see MIN_SUSTAINED_FRAMES).

Candidate channels are the ones a guided probe asks for (PROBES, or the
probes recorded in the session header). Every other channel is examined only
for discovery: if it moved strongly during free exploration it is reported
as discovered, so a movement nobody thought to ask for is not lost, but it
is never bound, because it has no repetitions to measure repeatability,
latency or fatigue on.

Rest reference and thresholds
-----------------------------
Every threshold and deadband is relative to the channel's MEASURED rest
mean, never to an absolute value or to zero. A laptop camera sits below eye
level, and Phase 2 measured head pitch resting at +9.8 degrees; a
threshold "above 0" would fire continuously for that user.

The rest reference of each channel is the mean and sample standard
deviation (n - 1) over the rest_baseline segment, after its first
REST_BASELINE_SETTLE_MS. The standard deviation used everywhere is

    effective_std = max(std, noise_floor)

with noise_floor from baseline.NOISE_FLOORS for the channel's unit, so a
pinned channel whose std is near zero cannot produce an infinite SNR or a
threshold of zero width.

    threshold offset = THRESHOLD_K * effective_std   (activation)
    deadband offset  = DEADBAND_K  * effective_std   (no output inside)

both measured from rest_mean, on both sides. The channel's active
direction is the majority sign of its repetition peaks, and the profile
reports the threshold on that side as well as both bounds.

Why THRESHOLD_K = 5. For gaussian noise a two-sided 5 sigma exceedance has
probability about 6e-7 per frame, and MIN_SUSTAINED_FRAMES = 2 squares that
again. Real rest noise is not gaussian (blinks, slow postural drift,
detector jitter), so the margin is deliberately generous: at 3 sigma a
rest-only channel would cross several times a minute. A larger K would push
the activation point toward the user's extreme and shrink the usable range;
SNR_FLOOR = 10 guarantees that a surviving channel's range is at least twice
its threshold offset. DEADBAND_K = 3 sits inside the threshold so that a
proportional output starts before a discrete activation does and noise at
rest stays inside the zero-output band most of the time.

Threshold floor
---------------
A truly still rest gives a tiny std, and 5 std of it can be smaller than
the ordinary variation of a resting face between movements (in the still
camera session, jawOpen's offset was 0.0054). So the activation offset is
also floored at threshold_floor_fraction (0.5) of the unit's minimum
excursion:

    threshold offset = max(THRESHOLD_K * effective_std,
                           threshold_floor_fraction * min_excursion[unit])

and the deadband is floored in the same proportion (deadband_k /
threshold_k of that floor). With the fraction at 0.5, a channel that clears
the minimum excursion always reaches at least twice its threshold offset,
the same headroom SNR_FLOOR = 10 gives over THRESHOLD_K = 5.

A rest window is per channel
----------------------------
A rest window is only a valid rest FOR A GIVEN CHANNEL if that channel was
not recently moved. The rest that follows a probe is recovery, not rest,
for the channels that probe moved: in the still camera session jawOpen's
std was 0.127 in the rest after the jaw probe against 0.00055 in the final
rest, and yaw's was 8.9 deg after the turn probe and 13.4 deg after the
tilt probe (a head tilt also turns the head). Treating those windows as
rest counts the tail of a deliberate movement as a false positive and as
rest noise. So, for each channel, a probe_rest window qualifies only if

  * it does not follow the channel's own probe, and
  * the channel is back inside its threshold when the evaluated part of
    the window opens (the last min_sustained_frames frames of the
    held_out_settle_ms settle), which also excludes the rest after a probe
    that moves the channel through coupling, as the tilt probe moves yaw.

The false-positive rate and the rest-quality comparison both use only a
channel's qualifying windows. A channel with no qualifying window reports
insufficient_data, never zero false positives. The windows that did not
qualify, and why, are recorded with the channel.

False positives are measured on HELD-OUT rest
---------------------------------------------
The threshold is derived from ONE rest period, the rest_baseline segment.
The false-positive rate is evaluated on the OTHER rest periods: every
probe_rest segment, after its first HELD_OUT_SETTLE_MS. Deriving a
threshold from a rest period and then counting crossings in that same
period is circular: a threshold at mean + 5 std of a sample essentially
never crosses that same sample, so almost every channel would report zero
false positives and the disqualifier would do nothing. Do not simplify this
to one rest period. For the same reason the final rest is never one of the
qualifying windows: it is where the threshold came from. A recording with no held-out rest frames disqualifies
every channel as insufficient_data rather than reporting zero.

The seven metrics, per candidate channel
----------------------------------------
usable_range       median over repetitions of the sustained peak
                   deviation from rest_mean, in the channel's unit. The
                   span between rest and voluntary extreme. Comparable
                   only within one unit family (scores, degrees, cm).
snr                usable_range / effective_std. Dimensionless, so it is
                   the ONLY metric comparable across scores, degrees and
                   centimetres, and the primary ranking term.
repeatability_cv   coefficient of variation of the per-repetition peaks,
                   sample std / mean. Unit-free; 0 is perfectly repeatable.
independence       Pearson correlation with every other candidate over the
                   whole session (free exploration through rest baseline).
                   Channels with |r| >= CLUSTER_MIN_ABS_R are clustered:
                   two channels that move together are one channel, and
                   only the best-scoring member of a cluster is bound.
latency_ms         per repetition, from the prompt_issued_ms of that
                   repetition's marker (NOT the segment start) to the first
                   frame of the first sustained threshold crossing.
fatigue_drift      peak of the last repetition minus peak of the first,
                   absolute (channel unit) and relative to the first.
                   Negative means the movement got smaller.
false_positives    sustained threshold crossings in held-out rest, as a
                   count and per minute of held-out rest.

A sustained value is one held for MIN_SUSTAINED_FRAMES consecutive tracking
frames: a sustained peak is the largest level reached by every frame of
such a run, and a crossing is the start of such a run beyond the
threshold. This matches the median-of-3 stage in filters.py, which passes
any excursion of two or more frames and removes single-frame spikes, so
calibration counts what shared control would actually see.

Ranking
-------
Hard disqualifiers are applied first (see disqualifiers()): insufficient
data, zero variance over the whole session, any held-out false positive,
SNR below SNR_FLOOR. The survivors are ranked by a weighted sum of
per-metric scores in [0, 1] (see ScoringConstants). Range is not a term of
its own because raw range is not comparable across units; it enters
through SNR.

Every number in ScoringConstants is PROVISIONAL. They were chosen from the
Phase 2 rest baselines and the protocol timing, not tuned against
calibration data, and will be tuned once real sessions exist.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception.baseline import NOISE_FLOORS, channel_unit  # noqa: E402
from perception.filters import channel_values, is_tracking  # noqa: E402

# Version of the recording (JSONL) and profile (JSON) records written here.
# Both are independent of the contract's schema in docs/contracts.md and are
# not part of that contract yet.
RECORDING_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 1

# Segment names, as written in marker records.
WARMUP = "warmup"
FREE_EXPLORATION = "free_exploration"
PROBE_REP = "probe_rep"
PROBE_RELEASE = "probe_release"
PROBE_REST = "probe_rest"
REST_BASELINE = "rest_baseline"
CONFIRM = "confirm"

# Record types, the "record_type" field of every JSONL line.
HEADER = "session_header"
FRAME = "frame"
MARKER = "marker"
SCORING = "scoring"
CONFIRM_RESULT = "confirm_result"
SESSION_END = "session_end"
PROFILE = "calibration_profile"


# --- Scoring constants -------------------------------------------------------

# Smallest excursion accepted as a real movement, per unit family, applied
# alongside the SNR floor. SNR is a ratio: a channel whose rest noise is
# near zero reaches a large SNR from a movement that is itself near zero
# (noseSneerLeft peaked at 2.5e-5 in the first camera session and still
# scored a free-exploration SNR of 37). Only an absolute floor separates a
# real movement from amplified silence.
#
# Derivation, from the camera session of 2026-11-22T01:58Z, the one whose
# final rest held still (jawOpen rest std 0.00055, yaw 0.24 deg). Each floor
# is the geometric mean of a silence ceiling and a movement floor, so it
# sits the same factor above one as below the other: the data span decades,
# so the midpoint is taken on a log scale.
#
#   score  silence ceiling 0.0132: the largest free-exploration peak below
#          the first wide gap in the sorted peaks (0.0132 -> 0.036). That
#          cluster is the pinned channels (1.5e-6 to 4.1e-5, including
#          noseSneerLeft) and channels not moved during exploration
#          (jawRight 0.0097, mouthFrownLeft 0.0103, jawOpen 0.0132; the jaw
#          was opened only when probed). Movement floor 0.199: the smallest
#          voluntary probe peak (browOuterUpRight; the others reached 0.24
#          to 0.85). sqrt(0.0132 * 0.199) = 0.051 -> 0.05, a factor of
#          about 4 on each side.
#   deg    silence ceiling 2.78: the largest sustained excursion of any
#          angle during the still final rest (pitch; yaw 0.56, roll 0.70).
#          Movement floor 5.89: the smallest voluntary excursion, a head
#          tilt toward the shoulder (roll; pitch reached 25, yaw 47).
#          sqrt(2.78 * 5.89) = 4.05 -> 4.0. This gap is only a factor of 2.1,
#          because head tilt is a small movement and pitch drifts at rest;
#          it is the floor most likely to need revisiting.
#   cm     silence ceiling 0.51: the largest sustained translation excursion
#          in the still final rest (z; x 0.29, y 0.45). No probe asks for a
#          translation, so the movement floor is the smallest
#          free-exploration translation peak across both camera sessions,
#          5.73 (x). sqrt(0.51 * 5.73) = 1.71 -> 1.7. It applies only to
#          discovery, since no translation channel is a candidate.
#
# One person, one lighting condition, two sessions: provisional like every
# other scoring constant.
MIN_EXCURSION = {"score": 0.05, "deg": 4.0, "cm": 1.7}


@dataclass(frozen=True)
class ScoringConstants:
    """Every tunable number in scoring, in one place. ALL PROVISIONAL.

    They are set from the Phase 2 rest baselines and the protocol timing,
    not fitted to calibration data, and will be tuned against real
    sessions. The profile copies them so a later re-score can tell which
    constants produced a ranking.

    threshold_k, deadband_k: multipliers on effective_std, see the module
        docstring for why 5 and 3.
    min_sustained_frames: consecutive tracking frames a level must hold to
        count as a peak or a crossing; 2 matches a median of 3.
    rest_baseline_settle_ms: start of rest_baseline ignored when deriving
        the rest reference, while the person stops moving after the probes.
    held_out_settle_ms: start of each probe_rest ignored when counting false
        positives, while the last repetition decays. Must be at least
        rep_grace_ms so no frame is both in a repetition window and in
        held-out rest.
    rep_grace_ms: a repetition's peak and latency window runs from its
        start to rep_grace_ms after its end (the release that follows), so
        a slow responder is still measured.
    min_rest_frames: fewer tracking frames than this in the rest reference
        and the channel is insufficient_data.
    snr_floor: disqualify below this. 10 keeps range >= 2 * threshold.
    snr_saturation: SNR at which the SNR score reaches 1. Scored on a log
        scale between snr_floor and here, because SNR spans decades.
    cv_worst: repeatability CV at which that score reaches 0.
    latency_worst_ms: latency at which that score reaches 0; the length of
        a repetition window (900 ms cue + 500 ms grace).
    drift_worst: relative decline from rep 1 to the last rep at which the
        fatigue score reaches 0. Growth is not penalised.
    cluster_min_abs_r: |Pearson r| at or above which two candidates are
        one cluster.
    broad_min_snr, broad_max_cv: a channel is "broad and repeatable", the
        Tier 1 criterion, with SNR at least this and CV at most this.
    min_excursion: smallest usable_range (candidates) or free-exploration
        peak (discovery) accepted as a real movement, per unit family. See
        MIN_EXCURSION for the derivation.
    missed_fraction: a counted repetition whose peak is below this fraction
        of the median of the other counted peaks is "missed": an absent
        response to the prompt, not a weaker one. 0.25 because a genuine
        fatigue decline over three repetitions in under 4 s is a change of
        tens of percent, not a factor of four; the missed browInnerUp rep in
        the first camera session peaked at 0.044 against 0.57 and 0.62, a
        factor of 13. A missed rep is excluded from range, repeatability and
        drift, and reported.
    min_plausible_latency_ms: a latency below this is anticipatory, not a
        response, and the repetition is excluded. 100 ms is the standard
        anticipation cut-off in reaction-time research: no voluntary
        response to a visual prompt starts sooner. Not fitted to any
        session. It backs up below_threshold_at_prompt, which a channel
        still swinging back toward rest can pass just before the prompt.
    rest_std_ratio_max: rest-quality gate. Two rest measurements of one
        channel disagree when their std differ by more than this factor.
        3 because each rest half is about 4.5 s and each probe rest under
        1 s, and frames are strongly autocorrelated, so a segment holds
        only about ten independent samples; for ten gaussian samples per
        side the 99th percentile std ratio is about 2.3, and 3 leaves a
        margin above it.
    rest_shift_k: rest-quality gate. The two halves of the final rest
        disagree when their means differ by more than rest_shift_k times the
        quieter half's effective std: the rest level moved by more than an
        activation threshold, so a threshold set on the whole segment is
        set on two different rests. Equal to threshold_k for that reason.
    threshold_floor_fraction: the threshold offset is at least this
        fraction of min_excursion for the unit. 0.5 keeps the guarantee that
        a channel clearing min_excursion reaches twice its threshold, the
        same ratio snr_floor / threshold_k gives. See "Threshold floor".
    min_stop_latency_reps: a channel can be bound to Stop only with at
        least this many valid latency repetitions. Stop goes to the
        lowest-latency channel because a late stop is unsafe, and a median
        over one sample is not a basis for that binding. With no channel
        meeting it, Stop is not bound to any channel (see stop_binding).
    lighting_max_ratio: lighting gate. The session is suspect when the
        brightest still segment is more than this factor brighter than the
        dimmest. Phase 2 measured that a brightness change of 1.29x (105 to
        135) moved the frame rate 1.49x (19.94 to 29.80 Hz) and with it the
        filter delay and the noise floor; 1.2 sits below that, the smallest
        change known to move the operating point. Still segments (warm-up,
        probe rests, final rest) are used, not every frame, because the
        image brightness is of the whole frame and a head turn during a
        probe changes it without any change in the light.
    weights: weight of each score in the ranking sum; they sum to 1. SNR
        dominates because it is the only cross-unit measure of usable
        signal.
    """

    threshold_k: float = 5.0
    deadband_k: float = 3.0
    min_sustained_frames: int = 2
    rest_baseline_settle_ms: int = 1000
    held_out_settle_ms: int = 500
    rep_grace_ms: int = 500
    min_rest_frames: int = 20
    snr_floor: float = 10.0
    snr_saturation: float = 1000.0
    cv_worst: float = 0.5
    latency_worst_ms: float = 1400.0
    drift_worst: float = 0.5
    cluster_min_abs_r: float = 0.8
    broad_min_snr: float = 50.0
    broad_max_cv: float = 0.25
    min_excursion: dict = field(default_factory=lambda: dict(MIN_EXCURSION))
    missed_fraction: float = 0.25
    min_plausible_latency_ms: float = 100.0
    rest_std_ratio_max: float = 3.0
    rest_shift_k: float = 5.0
    threshold_floor_fraction: float = 0.5
    lighting_max_ratio: float = 1.2
    min_stop_latency_reps: int = 2
    weights: dict = field(default_factory=lambda: {
        "snr": 0.40,
        "repeatability": 0.20,
        "independence": 0.15,
        "latency": 0.15,
        "fatigue": 0.10,
    })

    def __post_init__(self) -> None:
        if self.held_out_settle_ms < self.rep_grace_ms:
            raise ValueError("held_out_settle_ms must be >= rep_grace_ms, or held-out rest overlaps a repetition window")
        if self.snr_floor <= self.threshold_k:
            raise ValueError("snr_floor must exceed threshold_k, or a surviving channel may never reach its threshold")
        if not math.isclose(sum(self.weights.values()), 1.0):
            raise ValueError(f"weights must sum to 1, got {sum(self.weights.values())}")


DEFAULT_CONSTANTS = ScoringConstants()


# --- Protocol ----------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One guided probe: a prompt and the candidate channels it exercises."""

    name: str
    prompt: str
    channels: tuple[str, ...]


# The six guided probes. Prompts are directional so that the three
# repetitions of a head movement go the same way. Eye closure is not probed:
# the person cannot read the release prompt with their eyes shut. Blinks and
# gaze are still examined through free-exploration discovery.
PROBES = (
    Probe("brows", "Raise your eyebrows", ("browInnerUp", "browOuterUpLeft", "browOuterUpRight")),
    Probe("jaw", "Open your mouth", ("jawOpen",)),
    Probe("smile", "Smile", ("mouthSmileLeft", "mouthSmileRight")),
    Probe("turn", "Turn your head to the left", ("yaw_deg",)),
    Probe("nod", "Tilt your head up", ("pitch_deg",)),
    Probe("tilt", "Tilt your head toward your left shoulder", ("roll_deg",)),
)


@dataclass(frozen=True)
class ProtocolTiming:
    """Segment durations in milliseconds. The defaults total 60 s.

    free exploration 15 s; guided probes 30 s, which is six probes of
    (3 x 900 ms cue + 2 x 500 ms release + 1300 ms rest) = 5 s each;
    rest baseline 10 s; confirm and bind 5 s.
    """

    free_exploration_ms: int = 15000
    rep_ms: int = 900
    release_ms: int = 500
    probe_rest_ms: int = 1300
    repetitions: int = 3
    rest_baseline_ms: int = 10000
    confirm_ms: int = 5000


DEFAULT_TIMING = ProtocolTiming()

FREE_EXPLORATION_PROMPT = "Move however you can."
RELEASE_PROMPT = "Relax."
PROBE_REST_PROMPT = "Rest. Hold still."
REST_BASELINE_PROMPT = "Hold still."
CONFIRM_PROMPT = "Confirm your channels."


@dataclass(frozen=True)
class PlannedSegment:
    """One segment of the protocol, offset_ms from the protocol start."""

    segment: str
    probe: str | None
    repetition: int | None
    prompt: str
    offset_ms: int
    duration_ms: int


def plan_protocol(probes=PROBES, timing: ProtocolTiming = DEFAULT_TIMING) -> list[PlannedSegment]:
    """The ordered segments of the protocol, ending with confirm.

    Free exploration, then for each probe its repetitions with a release
    between them and a rest after the last, then the rest baseline, then
    confirm. Offsets are contiguous.
    """
    plan: list[PlannedSegment] = []
    offset = 0

    def add(segment, probe, repetition, prompt, duration):
        nonlocal offset
        plan.append(PlannedSegment(segment, probe, repetition, prompt, offset, duration))
        offset += duration

    add(FREE_EXPLORATION, None, None, FREE_EXPLORATION_PROMPT, timing.free_exploration_ms)
    for probe in probes:
        for repetition in range(1, timing.repetitions + 1):
            add(PROBE_REP, probe.name, repetition, f"{probe.prompt} ({repetition} of {timing.repetitions})", timing.rep_ms)
            if repetition < timing.repetitions:
                add(PROBE_RELEASE, probe.name, repetition, RELEASE_PROMPT, timing.release_ms)
        add(PROBE_REST, probe.name, None, PROBE_REST_PROMPT, timing.probe_rest_ms)
    add(REST_BASELINE, None, None, REST_BASELINE_PROMPT, timing.rest_baseline_ms)
    add(CONFIRM, None, None, CONFIRM_PROMPT, timing.confirm_ms)
    return plan


def protocol_description(probes=PROBES, timing: ProtocolTiming = DEFAULT_TIMING) -> dict:
    """JSON-ready description of the protocol, stored in the session header."""
    return {
        "timing_ms": asdict(timing),
        "probes": [{"name": p.name, "prompt": p.prompt, "channels": list(p.channels)} for p in probes],
    }


def probes_from_header(header: dict) -> tuple[Probe, ...]:
    """The probes a recording was made with, from its session header."""
    return tuple(Probe(p["name"], p["prompt"], tuple(p["channels"])) for p in header["protocol"]["probes"])


# --- Recording ---------------------------------------------------------------
#
# RECORDING FORMAT: one JSON object per line.
#   session_header  first line. recording_schema_version, session_id,
#                   user_id, source, measured, started_ms, protocol
#                   (protocol_description), brightness and frame_rate_hz
#                   measured over the warm-up before the protocol.
#   marker          event "segment_start"; segment, probe, repetition,
#                   prompt, start_ms (segment boundary, epoch ms),
#                   prompt_issued_ms (when the prompt was shown, epoch ms),
#                   planned_duration_ms.
#   frame           message (the camera feature message exactly as
#                   published) and brightness (0 to 255, or null).
#   scoring         summary of the score computed before confirm.
#   confirm_result  accepted, channel, detected_ms.
#   session_end     ended_ms, whole-session brightness and frame_rate_hz.
# A segment runs from its marker's start_ms to the next marker's start_ms
# (the last one to session_end.ended_ms).


@dataclass
class Recording:
    """A parsed calibration recording."""

    header: dict
    messages: list[dict]
    brightness: list[float | None]
    markers: list[dict]
    confirm: dict | None = None
    end: dict | None = None


def parse_records(records) -> Recording:
    """Build a Recording from decoded JSONL records, in file order.

    Raises ValueError if the first record is not a session header, if a
    record type is unknown, or if frame timestamps decrease.
    """
    records = iter(records)
    header = next(records, None)
    if header is None or header.get("record_type") != HEADER:
        raise ValueError("a calibration recording must start with a session_header record")
    if header.get("recording_schema_version") != RECORDING_SCHEMA_VERSION:
        raise ValueError(f"unsupported recording_schema_version {header.get('recording_schema_version')!r}")
    recording = Recording(header, [], [], [])
    for record in records:
        kind = record.get("record_type")
        if kind == FRAME:
            message = record["message"]
            if recording.messages and message["timestamp_ms"] < recording.messages[-1]["timestamp_ms"]:
                raise ValueError(f"frame timestamp_ms decreased at {message['timestamp_ms']}")
            recording.messages.append(message)
            recording.brightness.append(record.get("brightness"))
        elif kind == MARKER:
            recording.markers.append(record)
        elif kind == CONFIRM_RESULT:
            recording.confirm = record
        elif kind == SESSION_END:
            recording.end = record
        elif kind == SCORING:
            continue
        else:
            raise ValueError(f"unknown record_type {kind!r}")
    return recording


def load_recording(path: Path) -> Recording:
    """Read and parse a JSONL calibration recording."""
    with Path(path).open(encoding="utf-8") as handle:
        return parse_records(json.loads(line) for line in handle if line.strip())


# --- Windows and series ------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One recorded segment: [start_ms, end_ms) plus its marker fields."""

    segment: str
    probe: str | None
    repetition: int | None
    prompt: str
    start_ms: int
    end_ms: int
    prompt_issued_ms: int


def windows(recording: Recording) -> list[Window]:
    """Every segment of the recording, in time order.

    Each segment ends where the next marker starts; the last ends at
    session_end.ended_ms, or 1 ms after the last frame without one.
    """
    markers = sorted(recording.markers, key=lambda m: m["start_ms"])
    if recording.end is not None:
        final_ms = recording.end["ended_ms"]
    elif recording.messages:
        final_ms = recording.messages[-1]["timestamp_ms"] + 1
    else:
        final_ms = markers[-1]["start_ms"] if markers else 0
    result = []
    for index, marker in enumerate(markers):
        end_ms = markers[index + 1]["start_ms"] if index + 1 < len(markers) else final_ms
        result.append(Window(
            marker["segment"], marker.get("probe"), marker.get("repetition"), marker.get("prompt", ""),
            marker["start_ms"], end_ms, marker.get("prompt_issued_ms", marker["start_ms"]),
        ))
    return result


class Series:
    """Per-channel values aligned with the recording's frames.

    values[name][i] is the channel's value in frame i, or None when frame i
    is not tracking or does not carry the channel. None breaks a run of
    consecutive frames; it is never read as 0.0.
    """

    def __init__(self, messages: list[dict]) -> None:
        self.times = [m["timestamp_ms"] for m in messages]
        count = len(messages)
        self.values: dict[str, list[float | None]] = {}
        for index, message in enumerate(messages):
            if not is_tracking(message):
                continue
            for name, value in channel_values(message).items():
                self.values.setdefault(name, [None] * count)[index] = value

    def span(self, start_ms: int, end_ms: int) -> tuple[int, int]:
        """Frame index range [lo, hi) with start_ms <= timestamp_ms < end_ms."""
        return bisect.bisect_left(self.times, start_ms), bisect.bisect_left(self.times, end_ms)

    def window(self, name: str, start_ms: int, end_ms: int) -> tuple[list[int], list[float | None]]:
        """(timestamps, values) of one channel in [start_ms, end_ms)."""
        lo, hi = self.span(start_ms, end_ms)
        return self.times[lo:hi], self.values.get(name, [None] * len(self.times))[lo:hi]


def present(values) -> list[float]:
    """The non-None values."""
    return [v for v in values if v is not None]


# --- Sustained levels and crossings ------------------------------------------


def sustained_peak(deviations: list[float | None], min_frames: int) -> tuple[float, int] | None:
    """Largest deviation held for min_frames consecutive frames, and its sign.

    Over every run of min_frames consecutive non-None deviations, the
    level reached on the positive side is the run's minimum and on the
    negative side the minimum of the negated values. Returns (magnitude,
    sign) of the largest such level, sign +1 or -1, or None when no run of
    min_frames exists. A single-frame spike therefore cannot set the peak.
    """
    best: tuple[float, int] | None = None
    for start in range(len(deviations) - min_frames + 1):
        run = deviations[start:start + min_frames]
        if any(v is None for v in run):
            continue
        for sign in (1, -1):
            level = min(sign * v for v in run)
            if best is None or level > best[0]:
                best = (level, sign)
    if best is None:
        return None
    return max(best[0], 0.0), best[1]


class CrossingDetector:
    """Streaming detector of sustained threshold crossings.

    A crossing is min_frames consecutive deviations beyond offset on the
    same side (or, with direction given, on that side only). update()
    returns True exactly once per crossing, on the frame that completes it.
    None (a non-tracking frame) ends any run. Shared by scoring and by the
    live confirm step, so both detect the same thing.
    """

    def __init__(self, offset: float, min_frames: int, direction: int | None = None) -> None:
        if offset <= 0:
            raise ValueError(f"offset must be positive, got {offset!r}")
        self.offset = offset
        self.min_frames = min_frames
        self.direction = direction
        self._side = 0
        self._run = 0

    def side(self, deviation: float) -> int:
        """+1, -1 or 0 for a deviation beyond, beyond negatively, or inside."""
        if deviation > self.offset and self.direction in (None, 1):
            return 1
        if deviation < -self.offset and self.direction in (None, -1):
            return -1
        return 0

    def update(self, deviation: float | None) -> bool:
        side = 0 if deviation is None else self.side(deviation)
        if side == 0:
            self._side, self._run = 0, 0
            return False
        if side != self._side:
            self._side, self._run = side, 0
        self._run += 1
        return self._run == self.min_frames


def crossing_starts(deviations: list[float | None], offset: float, min_frames: int) -> list[int]:
    """Indices where each sustained crossing begins (first frame of its run)."""
    detector = CrossingDetector(offset, min_frames)
    return [index - min_frames + 1 for index, v in enumerate(deviations) if detector.update(v)]


# --- Statistics helpers ------------------------------------------------------


def coefficient_of_variation(values: list[float]) -> float | None:
    """Sample std / mean, unit-free. None for fewer than two values or mean 0."""
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return None
    return statistics.stdev(values) / mean


def pearson(a: list[float | None], b: list[float | None]) -> float | None:
    """Pearson r over frames where both are present. None if either is constant."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    if statistics.pvariance(xs) == 0 or statistics.pvariance(ys) == 0:
        return None
    return statistics.correlation(xs, ys)


def clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


# --- Per-channel metrics -----------------------------------------------------


@dataclass
class SessionWindows:
    """The windows scoring needs, grouped by role."""

    rest: Window | None
    held_out: list[Window]
    free: Window | None
    reps: dict[str, list[Window]]
    session_start_ms: int
    session_end_ms: int


def group_windows(all_windows: list[Window]) -> SessionWindows:
    """Pick the rest reference, held-out rests, free exploration and reps.

    The threshold source is the first rest_baseline segment. Held-out rest
    is every probe_rest segment plus any further rest_baseline segment.
    The whole session for correlation runs from free exploration (or the
    first non-warm-up segment) to the end of the rest baseline; warm-up
    and confirm are excluded.
    """
    rests = [w for w in all_windows if w.segment == REST_BASELINE]
    reps: dict[str, list[Window]] = {}
    for w in all_windows:
        if w.segment == PROBE_REP:
            reps.setdefault(w.probe, []).append(w)
    for probe_reps in reps.values():
        probe_reps.sort(key=lambda w: w.repetition)
    core = [w for w in all_windows if w.segment not in (WARMUP, CONFIRM)]
    return SessionWindows(
        rest=rests[0] if rests else None,
        held_out=[w for w in all_windows if w.segment == PROBE_REST] + rests[1:],
        free=next((w for w in all_windows if w.segment == FREE_EXPLORATION), None),
        reps=reps,
        session_start_ms=core[0].start_ms if core else 0,
        session_end_ms=core[-1].end_ms if core else 0,
    )


def rest_reference(series: Series, name: str, rest: Window | None, c: ScoringConstants) -> dict:
    """Mean, sample std, noise floor and effective std of the rest reference."""
    floor = NOISE_FLOORS[channel_unit(name)]
    result = {"mean": None, "std": None, "noise_floor": floor, "effective_std": None, "frames": 0,
              "window_ms": None, "segment": REST_BASELINE}
    if rest is None:
        return result
    start = rest.start_ms + c.rest_baseline_settle_ms
    _, values = series.window(name, start, rest.end_ms)
    values = present(values)
    result.update(frames=len(values), window_ms=[start, rest.end_ms])
    if len(values) < 2:
        return result
    std = statistics.stdev(values)
    result.update(mean=statistics.fmean(values), std=std, effective_std=max(std, floor))
    return result


def threshold_offsets(effective_std: float, unit: str, c: ScoringConstants) -> dict:
    """Activation and deadband offsets from rest_mean, with the floor applied.

    threshold = max(threshold_k * effective_std, threshold_floor_fraction *
    min_excursion[unit]); deadband = max(deadband_k * effective_std, the
    same floor scaled by deadband_k / threshold_k). floored says whether the
    floor, not the measured noise, set the threshold.
    """
    floor = c.threshold_floor_fraction * c.min_excursion[unit]
    measured = c.threshold_k * effective_std
    return {
        "threshold": max(measured, floor),
        "deadband": max(c.deadband_k * effective_std, floor * c.deadband_k / c.threshold_k),
        "floor_offset": floor,
        "floored": floor > measured,
    }


OWN_PROBE = "own_probe"
NOT_RETURNED = "not_returned_to_rest"


def rest_windows_for(series: Series, name: str, probe: str | None, mean: float, offset: float,
                     held_out: list[Window], c: ScoringConstants) -> tuple[list[Window], list[dict]]:
    """The held-out rest windows that are a valid rest for this channel.

    A rest window is only a valid rest for a given channel if that channel
    was not recently moved (see "A rest window is per channel" in the
    module docstring). A probe_rest that follows the channel's own probe is
    excluded as OWN_PROBE; any other window is excluded as NOT_RETURNED
    when the channel is not inside rest_mean +/- offset as its evaluated
    part opens. Returns (qualifying windows, excluded windows with reasons).
    """
    eligible: list[Window] = []
    excluded: list[dict] = []
    for w in held_out:
        if probe is not None and w.segment == PROBE_REST and w.probe == probe:
            excluded.append({"probe": w.probe, "start_ms": w.start_ms, "reason": OWN_PROBE})
        elif not inside_threshold_before(series, name, mean, offset, w.start_ms + c.held_out_settle_ms, c):
            excluded.append({"probe": w.probe, "start_ms": w.start_ms, "reason": NOT_RETURNED})
        else:
            eligible.append(w)
    return eligible, excluded


def false_positives(series: Series, name: str, mean: float, offset: float, held_out: list[Window], c: ScoringConstants) -> dict:
    """Sustained crossings of mean +/- offset in the channel's qualifying rest windows.

    held_out is the output of rest_windows_for. Each window is evaluated
    after its first held_out_settle_ms, separately, so a run never spans two
    windows. No qualifying window gives held_out_frames 0, which
    disqualifiers() reports as insufficient_data, never as zero.
    """
    crossings = 0
    frames = 0
    seconds = 0.0
    for w in held_out:
        start = w.start_ms + c.held_out_settle_ms
        if start >= w.end_ms:
            continue
        _, values = series.window(name, start, w.end_ms)
        deviations = [None if v is None else v - mean for v in values]
        frames += len(present(deviations))
        seconds += (w.end_ms - start) / 1000.0
        crossings += len(crossing_starts(deviations, offset, c.min_sustained_frames))
    return {
        "crossings": crossings,
        "held_out_periods": len(held_out),
        "held_out_frames": frames,
        "held_out_seconds": seconds,
        "per_minute": crossings / seconds * 60.0 if seconds > 0 else None,
        "threshold_source": REST_BASELINE,
        "evaluated_on": PROBE_REST,
    }


NOT_BELOW_THRESHOLD = "not_below_threshold_at_prompt"
ANTICIPATORY = "anticipatory"
MISSED = "missed"


def inside_threshold_before(series: Series, name: str, mean: float, offset: float, at_ms: int, c: ScoringConstants) -> bool:
    """True when the last min_sustained_frames frames before at_ms, within
    rep_grace_ms of it, are all tracking and all inside rest_mean +/- offset."""
    hi = bisect.bisect_left(series.times, at_ms)
    lo = bisect.bisect_left(series.times, at_ms - c.rep_grace_ms)
    values = series.values.get(name, [None] * len(series.times))[lo:hi][-c.min_sustained_frames:]
    if len(values) < c.min_sustained_frames or any(v is None for v in values):
        return False
    return all(abs(v - mean) <= offset for v in values)


def below_threshold_at_prompt(series: Series, name: str, mean: float, offset: float, prompt_ms: int, c: ScoringConstants) -> bool:
    """True when the channel was inside rest_mean +/- offset when the prompt was issued.

    The last min_sustained_frames frames before prompt_ms, within
    rep_grace_ms of it, must all be tracking and all inside the threshold.
    A repetition that starts with the channel still past its threshold
    (it never returned after the previous repetition) has no measurable
    onset: its first "crossing" is the carry-over, and its latency would
    be near zero. In the first camera session that produced 28 ms for yaw
    and 44 ms for pitch. A falsely fast latency is unsafe, because Stop is
    bound to the lowest-latency channel, so such a repetition is excluded
    rather than reported as fast.
    """
    return inside_threshold_before(series, name, mean, offset, prompt_ms, c)


def repetition_metrics(series: Series, name: str, mean: float, offset: float, reps: list[Window], c: ScoringConstants) -> list[dict]:
    """Sustained peak, sign, latency and validity of each repetition.

    The window is [start_ms, end_ms + rep_grace_ms). Latency is from the
    marker's prompt_issued_ms to the first frame of the first sustained
    crossing at or after it; None if there is none.

    Peak and latency are judged separately. The peak always counts toward
    range, repeatability, drift and SNR (unless mark_missed later marks the
    repetition missed): a movement that started before the channel had
    fully returned is still a real movement of that size. Only the latency
    needs a measurable onset, so it is null, and latency_excluded says why,
    when the channel was not below threshold at the prompt
    (NOT_BELOW_THRESHOLD) or crossed sooner than min_plausible_latency_ms
    (ANTICIPATORY). Latency stays defined as the threshold crossing, "time
    until the system can act", because Stop is bound to the lowest-latency
    channel; a null latency is honest, a redefined one is not.
    """
    result = []
    for w in reps:
        times, values = series.window(name, w.start_ms, w.end_ms + c.rep_grace_ms)
        deviations = [None if v is None else v - mean for v in values]
        peak = sustained_peak(deviations, c.min_sustained_frames)
        armed = below_threshold_at_prompt(series, name, mean, offset, w.prompt_issued_ms, c)
        after = [i for i, t in enumerate(times) if t >= w.prompt_issued_ms]
        latency = None
        if armed and after:
            first = after[0]
            starts = crossing_starts(deviations[first:], offset, c.min_sustained_frames)
            if starts:
                latency = times[first + starts[0]] - w.prompt_issued_ms
        latency_excluded = None if armed else NOT_BELOW_THRESHOLD
        if latency is not None and latency < c.min_plausible_latency_ms:
            latency_excluded, latency = ANTICIPATORY, None
        result.append({
            "repetition": w.repetition,
            "prompt_issued_ms": w.prompt_issued_ms,
            "below_threshold_at_prompt": armed,
            "counted": True,
            "excluded": None,
            "peak": None if peak is None else peak[0],
            "sign": None if peak is None else peak[1],
            "observed_peak": None if peak is None else peak[0],
            "latency_ms": latency,
            "latency_excluded": latency_excluded,
        })
    return result


def mark_missed(reps: list[dict], c: ScoringConstants) -> None:
    """Mark counted repetitions whose peak is far below the others as missed.

    A counted repetition is missed when its peak is below missed_fraction
    of the median of the other counted peaks. It is an absent response to
    the prompt (the person did not see it, or did not start), not a weaker
    movement, so reporting it as fatigue drift or poor repeatability would
    be wrong. Its latency is cleared and it stops counting. Every
    comparison uses the peaks as they were before any marking.
    """
    counted = [r for r in reps if r["counted"] and r["peak"] is not None]
    missed = []
    for r in counted:
        others = [o["peak"] for o in counted if o is not r]
        if others and r["peak"] < c.missed_fraction * statistics.median(others):
            missed.append(r)
    for r in missed:
        r.update(counted=False, excluded=MISSED, latency_ms=None, latency_excluded=r["latency_excluded"] or MISSED)


def score_channel(series: Series, name: str, probe: str | None, sw: SessionWindows, c: ScoringConstants) -> dict:
    """Every metric of one candidate channel except independence.

    Fields that cannot be computed from the data are None; disqualifiers()
    turns missing data into insufficient_data rather than a score.
    """
    rest = rest_reference(series, name, sw.rest, c)
    lo, hi = series.span(sw.session_start_ms, sw.session_end_ms)
    session_values = present(series.values.get(name, [])[lo:hi])
    metrics = {
        "channel": name,
        "unit": channel_unit(name),
        "probe": probe,
        "rest": rest,
        "session_frames": len(session_values),
        "session_zero_variance": len(set(session_values)) <= 1 if session_values else None,
        "threshold": None,
        "deadband": None,
        "direction": None,
        "usable_range": None,
        "extreme": None,
        "snr": None,
        "repetitions": [],
        "repetition_counts": None,
        "repeatability_cv": None,
        "latency_ms_median": None,
        "fatigue_drift": None,
        "false_positives": None,
        "free_exploration_peak": None,
    }
    mean, eff = rest["mean"], rest["effective_std"]
    if mean is None or rest["frames"] < c.min_rest_frames:
        return metrics
    offsets = threshold_offsets(eff, metrics["unit"], c)
    offset, deadband = offsets["threshold"], offsets["deadband"]
    metrics["deadband"] = {"k": c.deadband_k, "offset": deadband, "lower": mean - deadband, "upper": mean + deadband}
    eligible, excluded = rest_windows_for(series, name, probe, mean, offset, sw.held_out, c)
    metrics["false_positives"] = {**false_positives(series, name, mean, offset, eligible, c),
                                  "qualifying_windows": [[w.start_ms, w.end_ms, w.probe] for w in eligible],
                                  "excluded_windows": excluded}
    if sw.free is not None:
        _, free_values = series.window(name, sw.free.start_ms, sw.free.end_ms)
        free_peak = sustained_peak([None if v is None else v - mean for v in free_values], c.min_sustained_frames)
        metrics["free_exploration_peak"] = None if free_peak is None else {
            "peak": free_peak[0], "sign": free_peak[1], "snr": free_peak[0] / eff}

    reps = repetition_metrics(series, name, mean, offset, sw.reps.get(probe, []), c)
    mark_missed(reps, c)
    metrics["repetitions"] = reps
    # Two counts, because they differ: repetitions whose peak counts, and
    # repetitions that also gave a valid latency.
    metrics["repetition_counts"] = {
        "counted": sum(1 for r in reps if r["counted"] and r["peak"] is not None),
        "latency": sum(1 for r in reps if r["counted"] and r["latency_ms"] is not None),
        "missed": sum(1 for r in reps if r["excluded"] == MISSED),
        "latency_excluded": {
            reason: sum(1 for r in reps if r["counted"] and r["latency_excluded"] == reason)
            for reason in (NOT_BELOW_THRESHOLD, ANTICIPATORY)
        },
    }
    counted = [r for r in reps if r["counted"] and r["peak"] is not None]
    peaks = [r["peak"] for r in counted]
    signs = [r["sign"] for r in counted]
    direction = 1 if sum(signs) >= 0 else -1
    metrics["direction"] = direction
    metrics["threshold"] = {"k": c.threshold_k, "offset": offset, "lower": mean - offset, "upper": mean + offset,
                            "active": mean + direction * offset, "floor_offset": offsets["floor_offset"],
                            "floored": offsets["floored"]}
    if len(peaks) < 2:
        return metrics
    usable = statistics.median(peaks)
    latencies = [r["latency_ms"] for r in counted if r["latency_ms"] is not None]
    metrics.update(
        usable_range=usable,
        extreme=mean + direction * usable,
        snr=usable / eff,
        repeatability_cv=coefficient_of_variation(peaks),
        latency_ms_median=statistics.median(latencies) if latencies else None,
    )
    # Drift between the first and last COUNTED repetitions: a missed or
    # not-armed repetition is never read as fatigue.
    first, last = counted[0]["peak"], counted[-1]["peak"]
    metrics["fatigue_drift"] = {
        "first_repetition": counted[0]["repetition"],
        "last_repetition": counted[-1]["repetition"],
        "first_repetition_peak": first,
        "last_repetition_peak": last,
        "absolute": last - first,
        "relative": (last - first) / first if first > 0 else None,
    }
    return metrics


# --- Independence and clusters -----------------------------------------------


def correlations(series: Series, names: list[str], sw: SessionWindows) -> dict[str, dict[str, float | None]]:
    """Pearson r between every pair of names over the whole session."""
    lo, hi = series.span(sw.session_start_ms, sw.session_end_ms)
    cols = {n: series.values.get(n, [])[lo:hi] for n in names}
    table: dict[str, dict[str, float | None]] = {n: {} for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            r = pearson(cols[a], cols[b])
            table[a][b] = r
            table[b][a] = r
    return table


def clusters(names: list[str], table: dict, min_abs_r: float) -> list[list[str]]:
    """Connected groups of channels linked by |r| >= min_abs_r, input order.

    Singletons are included, so every candidate is in exactly one cluster.
    """
    parent = {n: n for n in names}

    def root(n):
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            r = table[a].get(b)
            if r is not None and abs(r) >= min_abs_r:
                parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(root(n), []).append(n)
    return list(groups.values())


# --- Disqualification, scores, ranking ---------------------------------------


def disqualifiers(metrics: dict, c: ScoringConstants) -> list[str]:
    """Every hard disqualifier that applies, applied before any ranking.

    insufficient_data: no rest reference, too few rest frames, no held-out
        rest frames, or fewer than two measured repetitions. Reported
        instead of a score that the data cannot support.
    zero_variance: every tracking value in the session identical.
    false_positive_in_held_out_rest: any sustained crossing in held-out
        rest, whatever the other scores are.
    snr_below_floor: snr < snr_floor.
    excursion_below_floor: usable_range below min_excursion for the unit;
        a high SNR from a near-silent channel is not a movement.
    rest_suspect: this channel's own rest measurements disagree (see
        channel_rest_quality); its threshold cannot be trusted. Added by
        score_session, which records the specific checks that failed.

    insufficient_data includes a channel left with fewer than two counted
    repetitions after excluding those not below threshold at the prompt
    and those missed.
    """
    reasons = []
    fp = metrics["false_positives"]
    if (metrics["rest"]["mean"] is None or metrics["rest"]["frames"] < c.min_rest_frames
            or fp is None or fp["held_out_frames"] == 0 or metrics["snr"] is None):
        reasons.append("insufficient_data")
    if metrics["session_zero_variance"]:
        reasons.append("zero_variance")
    if fp is not None and fp["crossings"] > 0:
        reasons.append("false_positive_in_held_out_rest")
    if metrics["snr"] is not None and metrics["snr"] < c.snr_floor:
        reasons.append("snr_below_floor")
    if metrics["usable_range"] is not None and metrics["usable_range"] < c.min_excursion[metrics["unit"]]:
        reasons.append("excursion_below_floor")
    return reasons


# --- Rest quality ------------------------------------------------------------

REST_OK = "ok"
REST_SUSPECT = "suspect"
REST_CHANNELS_DISQUALIFIED = "channels_disqualified"

# The session's rest is suspect only when fewer than this many usable
# channels survive, after rest-suspect channels are disqualified. 1, because
# one reliable channel is the least any tier needs (Tier 3, scanning). With
# fewer there is no tier to propose, and a suspect rest is the reason.
MIN_CHANNELS_FOR_TIER = 1


def segment_stats(values: list[float | None]) -> tuple[float, float, int] | None:
    """(mean, sample std, count) of the present values; None for fewer than 3."""
    xs = present(values)
    if len(xs) < 3:
        return None
    return statistics.fmean(xs), statistics.stdev(xs), len(xs)


def std_ratio(a: float, b: float, floor: float) -> float:
    """Larger std over the smaller, each floored at the noise floor; >= 1."""
    a, b = max(a, floor), max(b, floor)
    return max(a, b) / min(a, b)


def channel_rest_quality(series: Series, name: str, sw: SessionWindows, c: ScoringConstants,
                         probe_rests: list[Window] | None = None) -> dict:
    """The internal-consistency checks of one channel's rest measurements.

    Uses only this session, never a stored baseline: a first-time user has
    none. Three rest measurements are compared.

      halves: the final rest (after rest_baseline_settle_ms) split at its
        midpoint. Fails as rest_level_shift when the half means differ by
        more than rest_shift_k * the quieter half's effective std, and as
        rest_halves_std_ratio when the half stds differ by more than
        rest_std_ratio_max.
      probe rests: the lower median, over the channel's qualifying probe_rest
        windows (rest_windows_for; all probe rests when probe_rests is None)
        after held_out_settle_ms, of each window's std, against the median std
        of the final rest cut into consecutive windows of the same length.
        Fails as probe_rest_std_ratio beyond rest_std_ratio_max. Windows
        of equal length matter: for a slowly drifting signal std grows with
        window length, so a 0.8 s probe rest against the whole 9 s final
        rest would disagree in a perfectly still session. The medians keep
        one fidgety window from deciding alone. Skipped with fewer than two
        qualifying windows.

    A failure counts only when it is large enough to matter at threshold
    scale: a level shift must also be at least min_excursion for the unit,
    and for a std ratio the larger std times threshold_k must be at least
    min_excursion. Otherwise a pinned channel whose std moves from 1e-7 to
    1e-6 would void a session over a difference no threshold can see.
    """
    unit = channel_unit(name)
    floor = NOISE_FLOORS[unit]
    minimum = c.min_excursion[unit]
    reasons: list[str] = []
    detail: dict = {"halves": None, "probe_rests": None}
    if sw.rest is None:
        return {"reasons": reasons, **detail}
    start = sw.rest.start_ms + c.rest_baseline_settle_ms
    mid = (start + sw.rest.end_ms) // 2
    first = segment_stats(series.window(name, start, mid)[1])
    second = segment_stats(series.window(name, mid, sw.rest.end_ms)[1])
    if first is not None and second is not None:
        shift = abs(first[0] - second[0])
        quiet = max(min(first[1], second[1]), floor)
        ratio = std_ratio(first[1], second[1], floor)
        detail["halves"] = {"first_mean": first[0], "second_mean": second[0], "first_std": first[1],
                            "second_std": second[1], "shift": shift, "shift_over_quiet_std": shift / quiet,
                            "std_ratio": ratio}
        if shift > c.rest_shift_k * quiet and shift >= minimum:
            reasons.append("rest_level_shift")
        if ratio > c.rest_std_ratio_max and max(first[1], second[1]) * c.threshold_k >= minimum:
            reasons.append("rest_halves_std_ratio")
    probe_stds = []
    lengths = []
    for w in (sw.held_out if probe_rests is None else probe_rests):
        if w.segment != PROBE_REST:
            continue
        begin = w.start_ms + c.held_out_settle_ms
        stats = segment_stats(series.window(name, begin, w.end_ms)[1])
        if stats is not None:
            probe_stds.append(stats[1])
            lengths.append(w.end_ms - begin)
    final_stds = []
    length = int(statistics.median(lengths)) if lengths else 0
    if length > 0:
        for chunk_start in range(start, sw.rest.end_ms - length + 1, length):
            stats = segment_stats(series.window(name, chunk_start, chunk_start + length)[1])
            if stats is not None:
                final_stds.append(stats[1])
    # Two qualifying windows at least: with one, a single twitch would be the
    # whole comparison, and that twitch is already counted as a false positive.
    if len(probe_stds) >= 2 and final_stds:
        # Lower median: with a channel's own probe rest excluded it may have
        # only two qualifying windows, and the mean of two lets a single
        # twitch decide. The twitch already costs that channel a false
        # positive; it should not also void the session.
        typical = statistics.median_low(probe_stds)
        final_typical = statistics.median(final_stds)
        ratio = std_ratio(typical, final_typical, floor)
        detail["probe_rests"] = {"median_std": typical, "final_rest_median_window_std": final_typical,
                                 "window_ms": length, "probe_segments": len(probe_stds),
                                 "final_rest_windows": len(final_stds), "std_ratio": ratio}
        if ratio > c.rest_std_ratio_max and max(typical, final_typical) * c.threshold_k >= minimum:
            reasons.append("probe_rest_std_ratio")
    return {"reasons": reasons, **detail}


def rest_quality(series: Series, names: list[str], sw: SessionWindows, c: ScoringConstants,
                 probe_rests: dict[str, list[Window]] | None = None) -> dict:
    """Per-channel rest quality, and the channels whose rest is suspect.

    The gate is per channel. A channel whose own rest measurements
    disagree has an untrustworthy threshold, so score_session disqualifies
    that channel as rest_suspect, and only that channel: a brow still
    relaxing in the final rest says nothing about jawOpen's rest. The
    session's status is set by score_session: "ok" when no channel is
    suspect, "channels_disqualified" when some are but at least
    MIN_CHANNELS_FOR_TIER usable channels survive, and "suspect" when too
    few survive to propose any tier.
    """
    probe_rests = probe_rests or {}
    per_channel = {name: channel_rest_quality(series, name, sw, c, probe_rests.get(name)) for name in names}
    responsible = {name: q["reasons"] for name, q in per_channel.items() if q["reasons"]}
    return {
        "status": REST_CHANNELS_DISQUALIFIED if responsible else REST_OK,
        "responsible_channels": responsible,
        "channels": per_channel,
        "basis": "internal consistency of this session's rest periods; no stored baseline is used",
    }


def component_scores(metrics: dict, c: ScoringConstants) -> dict[str, float]:
    """Per-metric scores in [0, 1]; missing data scores 0 for that term."""
    snr = metrics["snr"]
    cv = metrics["repeatability_cv"]
    latency = metrics["latency_ms_median"]
    drift = metrics["fatigue_drift"]
    independence = metrics["independence"]["outside_cluster_max_abs_r"]
    relative = None if drift is None else drift["relative"]
    return {
        "snr": 0.0 if snr is None or snr <= 0 else clamp01(math.log(snr / c.snr_floor) / math.log(c.snr_saturation / c.snr_floor)),
        "repeatability": 0.0 if cv is None else clamp01(1.0 - cv / c.cv_worst),
        "independence": 1.0 - (independence or 0.0),
        "latency": 0.0 if latency is None else clamp01(1.0 - latency / c.latency_worst_ms),
        "fatigue": 0.0 if relative is None else clamp01(1.0 - max(0.0, -relative) / c.drift_worst),
    }


def is_broad(metrics: dict, c: ScoringConstants) -> bool:
    """Broad and repeatable range: the Tier 1 criterion."""
    return (metrics["snr"] is not None and metrics["snr"] >= c.broad_min_snr
            and metrics["repeatability_cv"] is not None and metrics["repeatability_cv"] <= c.broad_max_cv)


def assign_tier(usable: list[dict], c: ScoringConstants) -> dict:
    """Proposed control tier from the usable channels, with the reason.

    usable are the surviving channels that represent their cluster, best
    first. Exactly one usable channel is Tier 3 (scanning), even if it is
    broad: one channel cannot drive more than a selection switch. Two or
    more with at least one broad and repeatable is Tier 1 (proportional).
    Two or more with none broad is Tier 2 (discrete). None usable proposes
    no tier.
    """
    names = [m["channel"] for m in usable]
    broad = [m["channel"] for m in usable if is_broad(m, c)]
    base = {"usable_channels": names, "broad_channels": broad}
    if not usable:
        return {**base, "tier": None, "mode": None,
                "reason": "no channel survived the disqualifiers; repeat calibration or use assisted operation"}
    if len(usable) == 1:
        return {**base, "tier": 3, "mode": "scanning",
                "reason": f"exactly one reliable channel ({names[0]}); it can drive a scanning selector only"}
    if broad:
        return {**base, "tier": 1, "mode": "proportional",
                "reason": (f"{len(usable)} independent usable channels, {len(broad)} with broad repeatable range "
                           f"(snr >= {c.broad_min_snr:g}, cv <= {c.broad_max_cv:g}): {', '.join(broad)}")}
    return {**base, "tier": 2, "mode": "discrete",
            "reason": (f"{len(usable)} independent usable channels but none with broad repeatable range "
                       f"(snr >= {c.broad_min_snr:g}, cv <= {c.broad_max_cv:g}); use them as discrete switches")}


def score_session(recording: Recording, constants: ScoringConstants = DEFAULT_CONSTANTS) -> dict:
    """Metrics, ranking, clusters, discoveries and proposed tier for a recording.

    Candidates are the channels named by the probes in the session header.
    The ranking lists survivors best first, then disqualified channels.
    A survivor whose cluster already has a better member is "redundant",
    not bound and not counted toward the tier.

    When rest_quality is "suspect" the metrics and ranking are still
    reported, because they show what went wrong, but no tier is proposed:
    every threshold derives from a rest that disagrees with itself.
    """
    c = constants
    probes = probes_from_header(recording.header)
    series = Series(recording.messages)
    all_windows = windows(recording)
    sw = group_windows(all_windows)
    candidates = [(name, p.name) for p in probes for name in p.channels]
    names = [name for name, _ in candidates]

    metrics = {name: score_channel(series, name, probe, sw, c) for name, probe in candidates}
    table = correlations(series, names, sw)
    groups = clusters(names, table, c.cluster_min_abs_r)
    group_of = {n: g for g in groups for n in g}
    for name in names:
        others = {o: r for o, r in table[name].items()}
        outside = {o: r for o, r in others.items() if o not in group_of[name] and r is not None}
        strongest = max(others.items(), key=lambda kv: -1 if kv[1] is None else abs(kv[1]), default=(None, None))
        strongest_outside = max(outside.items(), key=lambda kv: abs(kv[1]), default=(None, None))
        metrics[name]["independence"] = {
            "correlations": others,
            "max_abs_r": None if strongest[1] is None else abs(strongest[1]),
            "max_with": strongest[0] if strongest[1] is not None else None,
            "outside_cluster_max_abs_r": None if strongest_outside[1] is None else abs(strongest_outside[1]),
            "cluster": group_of[name],
        }

    probe_rests = {
        name: [Window(PROBE_REST, probe, None, "", start, end, start)
               for start, end, probe in (metrics[name]["false_positives"] or {}).get("qualifying_windows", [])]
        for name in names
    }
    quality = rest_quality(series, names, sw, c, probe_rests)
    for name in names:
        metrics[name]["rest_quality"] = quality["channels"][name]

    ranking = []
    for name in names:
        m = metrics[name]
        reasons = disqualifiers(m, c)
        if quality["channels"][name]["reasons"]:
            reasons.append("rest_suspect")
        scores = component_scores(m, c)
        total = sum(c.weights[k] * scores[k] for k in c.weights)
        ranking.append({"channel": name, "reasons": reasons, "scores": scores,
                        "score": None if reasons else total, "status": "disqualified" if reasons else None})
    survivors = sorted((r for r in ranking if not r["reasons"]),
                       key=lambda r: (-r["score"], -metrics[r["channel"]]["snr"], r["channel"]))
    disqualified = [r for r in ranking if r["reasons"]]
    bound_clusters: dict[int, str] = {}
    usable = []
    for rank, row in enumerate(survivors, start=1):
        row["rank"] = rank
        key = id(group_of[row["channel"]])
        if key in bound_clusters:
            row["status"] = "redundant"
            row["redundant_with"] = bound_clusters[key]
        else:
            bound_clusters[key] = row["channel"]
            row["status"] = "usable"
            usable.append(metrics[row["channel"]])
    for row in disqualified:
        row["rank"] = None

    light = lighting_quality(recording, all_windows, c)
    tier = assign_tier(usable, c)
    problems = []
    if quality["responsible_channels"] and len(usable) < MIN_CHANNELS_FOR_TIER:
        quality["status"] = REST_SUSPECT
        named = "; ".join(f"{n} ({', '.join(r)})" for n, r in quality["responsible_channels"].items())
        problems.append(f"rest_quality suspect: fewer than {MIN_CHANNELS_FOR_TIER} usable channel survived after "
                        f"disqualifying the channels whose rest disagreed: {named}")
    if light["status"] == REST_SUSPECT:
        problems.append(
            f"lighting_quality suspect: still-segment brightness ranged {light['still_segment_brightness_min']:.1f} to "
            f"{light['still_segment_brightness_max']:.1f} ({light['still_segment_brightness_ratio']:.2f}x, limit "
            f"{c.lighting_max_ratio:g}x), so the frame rate and noise floor under every threshold were not constant")
    if problems:
        tier = {**tier, "tier": None, "mode": None,
                "reason": "no tier proposed. " + ". ".join(problems) + ". Repeat calibration."}

    return {
        "session_id": recording.header.get("session_id"),
        "candidates": names,
        "channels": metrics,
        "ranking": survivors + disqualified,
        "clusters": [{"channels": g, "max_abs_r": cluster_max_r(g, table)} for g in groups],
        "discovered": discover(series, names, sw, c),
        "tier": tier,
        "rest_quality": quality,
        "lighting_quality": light,
        "stop_binding": stop_binding(usable, tier, c),
        "session_quality": REST_SUSPECT if problems else REST_OK,
        "lighting": lighting_summary(recording),
        "constants": constants_record(c),
    }


def cluster_max_r(group: list[str], table: dict) -> float | None:
    """Largest |r| between members of a cluster, None for a singleton."""
    values = [abs(table[a][b]) for i, a in enumerate(group) for b in group[i + 1:] if table[a].get(b) is not None]
    return max(values) if values else None


def discover(series: Series, candidates: list[str], sw: SessionWindows, c: ScoringConstants) -> list[dict]:
    """Non-candidate channels that moved strongly in free exploration.

    A channel is reported when its sustained free-exploration peak over
    effective_std is at least snr_floor, the peak itself is at least
    min_excursion for its unit, it has no held-out false positive,
    and it is not clustered with a candidate (|r| < cluster_min_abs_r with
    every candidate). It was never probed, so it is not ranked or bound;
    it needs a guided probe first. Best first by free-exploration SNR.
    """
    found = []
    for name in sorted(series.values):
        if name in candidates:
            continue
        m = score_channel(series, name, None, sw, c)
        free = m["free_exploration_peak"]
        fp = m["false_positives"]
        if free is None or fp is None or fp["held_out_frames"] == 0:
            continue
        if free["snr"] < c.snr_floor or free["peak"] < c.min_excursion[m["unit"]]:
            continue
        if fp["crossings"] > 0 or m["session_zero_variance"]:
            continue
        lo, hi = series.span(sw.session_start_ms, sw.session_end_ms)
        rs = {cand: pearson(series.values[name][lo:hi], series.values.get(cand, [])[lo:hi]) for cand in candidates}
        rs = {k: v for k, v in rs.items() if v is not None}
        top = max(rs.items(), key=lambda kv: abs(kv[1]), default=(None, None))
        if top[1] is not None and abs(top[1]) >= c.cluster_min_abs_r:
            continue
        found.append({"channel": name, "unit": m["unit"], "free_exploration_peak": free["peak"],
                      "free_exploration_snr": free["snr"], "rest_mean": m["rest"]["mean"],
                      "max_abs_r_with_candidate": None if top[1] is None else abs(top[1]),
                      "max_with": top[0], "note": "not probed; needs a guided probe before it can be bound"})
    return sorted(found, key=lambda d: -d["free_exploration_snr"])


def constants_record(c: ScoringConstants) -> dict:
    return {**asdict(c), "provisional": True}


def frame_rate_hz(messages: list[dict]) -> float | None:
    """(frames - 1) / first-to-last timestamp span, in hertz."""
    if len(messages) < 2:
        return None
    span = messages[-1]["timestamp_ms"] - messages[0]["timestamp_ms"]
    return (len(messages) - 1) * 1000.0 / span if span > 0 else None


def lighting_summary(recording: Recording) -> dict:
    """Brightness and frame rate: the header's warm-up values and the whole session's.

    Frame rate and rest noise both move with scene brightness (Phase 2:
    19.94 Hz at 105, 29.80 Hz at 135), so thresholds in a profile are valid
    only for lighting near the brightness they were measured under.
    """
    values = [b for b in recording.brightness if b is not None]
    return {
        "warmup_brightness": recording.header.get("brightness"),
        "warmup_frame_rate_hz": recording.header.get("frame_rate_hz"),
        "session_brightness_mean": statistics.fmean(values) if values else None,
        "session_brightness_min": min(values) if values else None,
        "session_brightness_max": max(values) if values else None,
        "session_brightness_median": statistics.median(values) if values else None,
        "session_frame_rate_hz": frame_rate_hz(recording.messages),
        "valid_only_for_measured_lighting": True,
    }


STILL_SEGMENTS = (WARMUP, PROBE_REST, REST_BASELINE)


def lighting_quality(recording: Recording, all_windows: list[Window], c: ScoringConstants) -> dict:
    """Lighting stability over the session: "ok", or "suspect" beyond lighting_max_ratio.

    Records min, max, median and mean brightness over every frame, and the
    median brightness and frame rate of each still segment (warm-up, probe
    rests, final rest). The gate compares the brightest and dimmest still
    segments, because whole-image brightness also moves when the head turns
    during a probe, which is not a change in the light. Phase 2 showed that
    frame rate and noise floor both move with brightness, so a session whose
    light changed has a moving noise floor under every threshold it derived.
    Brightness 0 to 255; frame rate in hertz. Without brightness (synthetic
    source) the status is "unmeasured" and does not gate.
    """
    times = [m["timestamp_ms"] for m in recording.messages]
    values = [b for b in recording.brightness if b is not None]
    segments = []
    for w in all_windows:
        if w.segment not in STILL_SEGMENTS:
            continue
        lo, hi = bisect.bisect_left(times, w.start_ms), bisect.bisect_left(times, w.end_ms)
        seg = [b for b in recording.brightness[lo:hi] if b is not None]
        if seg:
            segments.append({"segment": w.segment, "probe": w.probe, "start_ms": w.start_ms,
                             "brightness_median": statistics.median(seg),
                             "frame_rate_hz": frame_rate_hz(recording.messages[lo:hi])})
    medians = [g["brightness_median"] for g in segments]
    rates = [g["frame_rate_hz"] for g in segments if g["frame_rate_hz"] is not None]
    ratio = max(medians) / min(medians) if medians and min(medians) > 0 else None
    if ratio is None:
        status = "unmeasured"
    else:
        status = REST_SUSPECT if ratio > c.lighting_max_ratio else REST_OK
    return {
        "status": status,
        "brightness_min": min(values) if values else None,
        "brightness_max": max(values) if values else None,
        "brightness_median": statistics.median(values) if values else None,
        "brightness_mean": statistics.fmean(values) if values else None,
        "still_segment_brightness_min": min(medians) if medians else None,
        "still_segment_brightness_max": max(medians) if medians else None,
        "still_segment_brightness_ratio": ratio,
        "max_ratio": c.lighting_max_ratio,
        "still_segment_frame_rate_min_hz": min(rates) if rates else None,
        "still_segment_frame_rate_max_hz": max(rates) if rates else None,
        "segments": segments,
        "basis": "median brightness of still segments (warm-up, probe rests, final rest), whole image, BT.601 luma 0-255",
    }


def score_recording(path: Path, constants: ScoringConstants = DEFAULT_CONSTANTS) -> dict:
    """score_session on a recording read from disk."""
    return score_session(load_recording(path), constants)


# --- Profile -----------------------------------------------------------------


STOP_PATH_NOTE = (
    "The hardware cutoff and the signal-loss stop remain the stop path. Neither depends on a calibrated channel, "
    "so they stop the rover whether or not a channel is bound to Stop."
)


def stop_binding(usable: list[dict], tier: dict, c: ScoringConstants) -> dict:
    """The channel bound to Stop, or channel None with the reason.

    Stop goes to the usable channel with the lowest median latency, among
    those with at least min_stop_latency_reps valid latency repetitions.
    Latency is the threshold crossing, the time until the system can act. A
    channel with fewer latency samples is never chosen, however fast its
    one sample was. With no tier, or no channel meeting the minimum, no
    channel is bound and the stop path is the hardware cutoff and the
    signal-loss stop alone. Those are the stop path in every case; a bound
    channel adds to them and never replaces them.
    """
    base = {"min_latency_repetitions": c.min_stop_latency_reps, "stop_path": STOP_PATH_NOTE}
    if tier["tier"] is None:
        return {**base, "channel": None, "reason": "no tier proposed, so no channel is bound to Stop"}
    eligible = [m for m in usable
                if m["repetition_counts"]["latency"] >= c.min_stop_latency_reps and m["latency_ms_median"] is not None]
    if not eligible:
        counts = ", ".join(f"{m['channel']} {m['repetition_counts']['latency']}" for m in usable)
        return {**base, "channel": None,
                "reason": f"no usable channel has {c.min_stop_latency_reps} or more valid latency repetitions "
                          f"(latency repetitions: {counts})"}
    best = min(eligible, key=lambda m: (m["latency_ms_median"], m["channel"]))
    return {**base, "channel": best["channel"], "latency_ms_median": best["latency_ms_median"],
            "latency_repetitions": best["repetition_counts"]["latency"],
            "reason": f"lowest median latency among usable channels with {c.min_stop_latency_reps} or more latency repetitions"}


def roles_for(tier: dict, usable: list[dict], c: ScoringConstants) -> list[tuple[str, str]]:
    """(channel, role) for each usable channel under the proposed tier.

    Tier 1: the first two broad channels are proportional_primary and
    proportional_secondary; the rest are discrete. Tier 2: all discrete.
    Tier 3: the single channel is scan_switch. These are proposals; the
    confirm step and the operator decide.
    """
    if tier["tier"] == 3:
        return [(usable[0]["channel"], "scan_switch")]
    if tier["tier"] == 2:
        return [(m["channel"], "discrete") for m in usable]
    if tier["tier"] == 1:
        proportional = iter(("proportional_primary", "proportional_secondary"))
        roles = []
        for m in usable:
            role = next(proportional, "discrete") if is_broad(m, c) else "discrete"
            roles.append((m["channel"], role))
        return roles
    return []


def build_profile(result: dict, recording: Recording, constants: ScoringConstants = DEFAULT_CONSTANTS) -> dict:
    """The per-user profile: bindings, thresholds, deadbands, tier and metrics.

    The enrolment metrics are kept in full because they are the first
    point of the user's longitudinal record. accepted comes from the
    recording's confirm_result, or is None when no confirm step ran.

    rest_quality and lighting_quality are each "ok" or "suspect", and
    session_quality is "suspect" when either is. A suspect profile has no
    tier and no bindings, and names the channels whose rest periods
    disagreed or the brightness range; it is written so the failed
    enrolment is on record, never as a usable profile.
    """
    channels = result["channels"]
    usable = [channels[r["channel"]] for r in result["ranking"] if r["status"] == "usable"]
    bindings = []
    for name, role in roles_for(result["tier"], usable, constants):
        m = channels[name]
        bindings.append({
            "channel": name, "role": role, "unit": m["unit"], "probe": m["probe"], "direction": m["direction"],
            "rest_mean": m["rest"]["mean"], "rest_std": m["rest"]["std"], "effective_std": m["rest"]["effective_std"],
            "threshold": m["threshold"], "deadband": m["deadband"],
            "usable_range": m["usable_range"], "extreme": m["extreme"],
        })
    header = recording.header
    confirm = recording.confirm
    return {
        "record_type": PROFILE,
        "schema_version": PROFILE_SCHEMA_VERSION,
        "user_id": header.get("user_id"),
        "session_id": header.get("session_id"),
        "source": header.get("source"),
        "measured": header.get("measured"),
        "created_from_recording_started_ms": header.get("started_ms"),
        "session_quality": result["session_quality"],
        "rest_quality": result["rest_quality"]["status"],
        "rest_quality_responsible_channels": result["rest_quality"]["responsible_channels"],
        "lighting_quality": result["lighting_quality"]["status"],
        "lighting_quality_detail": {k: v for k, v in result["lighting_quality"].items() if k != "segments"},
        "proposed_tier": {k: result["tier"][k] for k in ("tier", "mode", "reason")},
        "stop_binding": result["stop_binding"],
        "accepted": None if confirm is None else confirm["accepted"],
        "confirm": confirm,
        "bindings": bindings,
        "thresholds": {name: {"threshold": m["threshold"], "deadband": m["deadband"], "rest_mean": m["rest"]["mean"]}
                       for name, m in channels.items()},
        "lighting": result["lighting"],
        "enrolment_metrics": {
            "channels": channels,
            "ranking": result["ranking"],
            "clusters": result["clusters"],
            "discovered": result["discovered"],
            "tier": result["tier"],
            "rest_quality": result["rest_quality"],
            "lighting_quality": result["lighting_quality"],
        },
        "scoring_constants": result["constants"],
        "note": ("Not part of docs/contracts.md yet. schema_version is this profile's own version, "
                 "independent of the contract's schema."),
    }


def write_json(record: dict, path: Path) -> None:
    """Pretty-printed JSON, NaN and infinity refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


# --- Report ------------------------------------------------------------------


def fmt(value, spec: str = ".3g", none: str = "-") -> str:
    """Format a number with significant figures, or a dash for None."""
    return none if value is None else format(value, spec)


def rep_cell(rep: dict) -> str:
    """One repetition's latency for the table: ms, or u, a, m, - when null."""
    if rep["excluded"] == MISSED:
        return "m"
    if rep["latency_excluded"] == NOT_BELOW_THRESHOLD:
        return "u"
    if rep["latency_excluded"] == ANTICIPATORY:
        return "a"
    return fmt(rep["latency_ms"], ".0f")


def rest_quality_numbers(q: dict) -> str:
    """The numbers behind one channel's rest-quality verdict."""
    parts = []
    if q["halves"]:
        h = q["halves"]
        parts.append(f"halves mean {h['first_mean']:.3g} -> {h['second_mean']:.3g} "
                     f"(shift {h['shift_over_quiet_std']:.3g} x quiet std), std ratio {h['std_ratio']:.3g}")
    if q["probe_rests"]:
        parts.append(f"probe-rest/final-rest std ratio {q['probe_rests']['std_ratio']:.3g}")
    return "; ".join(parts)


def format_report(result: dict) -> str:
    """The ranked table, clusters, discoveries and tier as plain text."""
    channels = result["channels"]
    head = (f"{'rank':>4}  {'channel':<17} {'unit':<5} {'status':<12} {'score':>5}  {'rest_mean':>9} "
            f"{'thresh':>9} {'range':>8} {'snr':>7} {'cv':>6} {'lat_ms':>14} {'reps':>6} {'drift':>7} {'fp':>9} "
            f"{'max|r|':>6} with")
    lines = [head, "-" * len(head)]
    for row in result["ranking"]:
        m = channels[row["channel"]]
        fp = m["false_positives"]
        drift = m["fatigue_drift"]
        lat = "/".join(rep_cell(r) for r in m["repetitions"]) or "-"
        counts = m["repetition_counts"]
        excl = "-" if not counts else f"{counts['counted']}c/{counts['latency']}l"
        threshold = m["threshold"]["active"] if m["threshold"] else None
        lines.append(
            f"{fmt(row['rank'], 'd'):>4}  {row['channel']:<17} {m['unit']:<5} {row['status']:<12} "
            f"{fmt(row['score'], '.3f'):>5}  {fmt(m['rest']['mean']):>9} {fmt(threshold):>9} "
            f"{fmt(m['usable_range']):>8} {fmt(m['snr']):>7} {fmt(m['repeatability_cv'], '.2f'):>6} "
            f"{lat:>14} {excl:>6} {fmt(None if drift is None else drift['relative'], '+.2f'):>7} "
            f"{('-' if fp is None else str(fp['crossings']) + '/' + format(fp['held_out_seconds'], '.1f') + 's'):>9} "
            f"{fmt(m['independence']['max_abs_r'], '.2f'):>6} {m['independence']['max_with'] or '-'}"
        )
    lines.append("lat_ms per rep: latency null because u = not below threshold at the prompt, a = anticipatory "
                 "(< %g ms), m = missed, - = no crossing. u and a reps still count their peak." % result["constants"]["min_plausible_latency_ms"])
    lines.append("reps: Nc = repetitions whose peak counts toward range, cv, drift and snr; Nl = of those, with a valid latency.")
    lines.append("")
    lines.append("disqualified:")
    dq = [r for r in result["ranking"] if r["reasons"]]
    lines += [f"  {r['channel']:<17} {', '.join(r['reasons'])}" for r in dq] or ["  none"]
    redundant = [r for r in result["ranking"] if r["status"] == "redundant"]
    if redundant:
        lines.append("redundant (correlated with a better channel, not bound):")
        lines += [f"  {r['channel']:<17} with {r['redundant_with']}" for r in redundant]
    lines.append("correlated clusters (|r| >= %g):" % result["constants"]["cluster_min_abs_r"])
    multi = [g for g in result["clusters"] if len(g["channels"]) > 1]
    lines += [f"  {' + '.join(g['channels'])}  max|r|={g['max_abs_r']:.3f}" for g in multi] or ["  none"]
    lines.append("discovered in free exploration (not probed, not bound):")
    lines += [f"  {d['channel']:<17} free_snr={d['free_exploration_snr']:.3g} peak={d['free_exploration_peak']:.3g} {d['unit']}"
              for d in result["discovered"]] or ["  none"]
    tier = result["tier"]
    light = result["lighting"]
    lq = result["lighting_quality"]
    lines.append(f"lighting: brightness min {fmt(lq['brightness_min'], '.1f')}  median {fmt(lq['brightness_median'], '.1f')}  "
                 f"max {fmt(lq['brightness_max'], '.1f')} / 255; still segments {fmt(lq['still_segment_brightness_min'], '.1f')} to "
                 f"{fmt(lq['still_segment_brightness_max'], '.1f')} ({fmt(lq['still_segment_brightness_ratio'], '.2f')}x, limit "
                 f"{lq['max_ratio']:g}x); frame rate {fmt(lq['still_segment_frame_rate_min_hz'], '.1f')} to "
                 f"{fmt(lq['still_segment_frame_rate_max_hz'], '.1f')} Hz across still segments, "
                 f"{fmt(light['session_frame_rate_hz'], '.2f')} Hz overall")
    if lq["status"] == REST_SUSPECT:
        lines.append("LIGHTING SUSPECT: the light changed during the session; no tier is proposed")
    else:
        lines.append(f"lighting quality: {lq['status']}")
    quality = result["rest_quality"]
    if quality["status"] == REST_SUSPECT:
        lines.append(f"REST QUALITY SUSPECT: fewer than {MIN_CHANNELS_FOR_TIER} usable channel survived; no tier is proposed")
    elif quality["status"] == REST_CHANNELS_DISQUALIFIED:
        lines.append("rest quality: some channels disqualified as rest_suspect; the session continues without them")
    else:
        lines.append("rest quality: ok (halves of the final rest agree, and agree with the probe rests, on every candidate)")
    for name, reasons in quality["responsible_channels"].items():
        lines.append(f"  {name:<17} {', '.join(reasons)}  {rest_quality_numbers(quality['channels'][name])}")
    lines.append(f"proposed tier: {tier['tier']} ({tier['mode']}): {tier['reason']}")
    stop = result["stop_binding"]
    if stop["channel"] is None:
        lines.append(f"stop binding: null: {stop['reason']}. {stop['stop_path']}")
    else:
        lines.append(f"stop binding: {stop['channel']} (median latency {stop['latency_ms_median']:.0f} ms over "
                     f"{stop['latency_repetitions']} repetitions). {stop['stop_path']}")
    return "\n".join(lines)
