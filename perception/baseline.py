"""Measure the rest baseline: the noise floor every threshold is set against.

The subject holds still for --seconds while frames arrive from either the
fake source or the camera. For each channel (see filters.channel_values)
the statistics are computed over tracking frames only, on the raw values
as published, before any filter stage:

    count, mean, std (sample standard deviation, n - 1 denominator),
    min, max, peak_to_peak (max - min)

std is None when a channel has fewer than two samples. Units are the
channel's own: blendshape scores are unitless on [0, 1], angles are
degrees, translation is centimeters.

Each channel also carries:

    distinct_values     number of distinct raw values seen
    zero_variance       True when every value was identical (std exactly 0)
    noise_floor         the floor for the channel's unit, see NOISE_FLOORS
    below_noise_floor   True when std < noise_floor
    range               the channel's full-scale range, see FULL_RANGES
    snr                 range / max(std, noise_floor), always finite

zero_variance and distinct_values are exact and report a truly constant
channel as dead. below_noise_floor catches the commoner case measured on
this detector: a channel pinned near zero whose std is tiny but not zero.
When it is True, snr was computed from the floor rather than the measured
std, and should be read as "no usable rest noise measured", not as a very
clean channel.

For the run as a whole the record also holds total frames, frames per
tracking state, the fraction of frames that were not tracking, the frame
rate measured from timestamp_ms, and frame brightness.

Brightness is the mean over all captured frames of
mediapipe_source.grayscale_brightness, 0 to 255, the repository's single
definition of brightness. It is stored because clinician output must
caveat that a decline may reflect worse lighting rather than worse motor
control; storing the lighting the baseline was measured under makes that
caveat checkable. The fake source has no image, so its brightness is null.

Frame rate and channel noise both depend on scene brightness: in dim light
the camera lengthens its exposure and caps its frame rate (measured 19.94
Hz at brightness 105 and 29.80 Hz at 135 on the development laptop, with
nothing else changed). A baseline is therefore valid only for the lighting
it was measured under, and the record says so in its "lighting" field, so
that a comparison across sessions does not read a lighting change as a
change in the user.

The output is one JSON file. Its schema_version is the version of this
baseline record, independent of the contract's schema; the record is not
part of docs/contracts.md.

    python -m perception.baseline --source fake --out baseline.json
    python -m perception.baseline --source camera --out baseline.json

Standard library only at module level; the camera packages are imported
only when the camera source is opened.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perception import fake_stream  # noqa: E402
from perception.filters import (  # noqa: E402
    POSE_ANGLE_CHANNELS,
    POSE_CHANNELS,
    TRANSLATION_CHANNELS,
    channel_values,
    is_tracking,
)

# 2 added distinct_values, zero_variance, noise_floor, below_noise_floor,
# range and snr per channel, and the run-level "lighting" field.
BASELINE_SCHEMA_VERSION = 2

# Smallest standard deviation accepted as measured noise, per channel unit.
#
# score, 5e-7: chosen from two camera rest baselines (brightness 105 and
# 135). In both, five blendshapes (cheekSquintLeft, cheekSquintRight,
# _neutral, noseSneerLeft, noseSneerRight) sat pinned near zero, every
# value under 3e-6, with std between 6e-9 and 4.0e-7. The quietest other
# channel, cheekPuff, had std 7.7e-7 and 1.9e-6. 5e-7 lies in that gap in
# both runs, so exactly the five pinned channels fall below it. The gap is
# narrow, under a factor of two on each side, so treat the five as
# below_noise_floor because of this measurement, not because of a margin.
#
# deg and cm, 0.01: a divide-by-zero guard only. The smallest pose stds
# measured were 0.30 deg and 0.19 cm, so this floor never bound in either
# run and is not derived from them.
NOISE_FLOORS = {"score": 5e-7, "deg": 0.01, "cm": 0.01}

# Full-scale range per channel, in the channel's unit: the width of the
# valid range in docs/contracts.md (blendshape scores [0, 1]; yaw [-90, 90],
# pitch [-60, 60], roll [-45, 45] degrees; x and y [-100, 100], z
# [-200, -5] cm). These are contract bounds, not the range a given user can
# reach, so snr is an upper bound on usable signal-to-noise for pose.
BLENDSHAPE_RANGE = 1.0
FULL_RANGES = {
    "yaw_deg": 180.0,
    "pitch_deg": 120.0,
    "roll_deg": 90.0,
    "x_cm": 200.0,
    "y_cm": 200.0,
    "z_cm": 195.0,
}
LIGHTING_NOTE = (
    "frame_rate_hz and every channel std, and so the noise floor thresholds are set against, are functions "
    "of scene brightness: in dim light the camera lengthens exposure and caps its frame rate. This baseline "
    "is valid only for lighting near brightness.mean; compare it with a session measured under different "
    "lighting only after accounting for the brightness difference, or a lighting change will read as a "
    "change in the user."
)
RECORD_TYPE = "rest_baseline"
DEFAULT_SECONDS = 10.0
DEFAULT_SETTLE_SECONDS = 1.0
BRIGHTNESS_DEFINITION = "mean grayscale value of the captured image, BT.601 luma, 0 to 255"
TRACKING_STATES = ("tracking", "partial", "lost")


@dataclass(frozen=True)
class SourceFrame:
    """One camera feature message plus the brightness of its image.

    brightness is 0 to 255, or None when the source has no image.
    """

    message: dict
    brightness: float | None


# --- Sources -----------------------------------------------------------------


def fake_source_frames(rate_hz: float, seed: int, start_ms: int):
    """SourceFrames from the synthetic stream. Not measured data; no image."""
    for message in fake_stream.generate_frames(rate_hz, seed, start_ms):
        yield SourceFrame(message, None)


def camera_source_frames(source):
    """SourceFrames from an open mediapipe_source.CameraSource."""
    for frame in source.frames():
        yield SourceFrame(frame.message, frame.brightness)


# --- Collection --------------------------------------------------------------


def elapsed_ms(frame: SourceFrame, first: SourceFrame) -> int:
    """Milliseconds between two frames' timestamp_ms."""
    return frame.message["timestamp_ms"] - first.message["timestamp_ms"]


def skip_seconds(frames, seconds: float):
    """Drop frames captured in the first `seconds`, measured by timestamp_ms.

    Used to let camera exposure settle before measuring. Returns an
    iterator positioned at the first kept frame.
    """
    frames = iter(frames)
    first = next(frames)
    limit_ms = seconds * 1000.0
    return itertools.dropwhile(lambda frame: elapsed_ms(frame, first) < limit_ms, itertools.chain([first], frames))


def collect(frames, seconds: float) -> list[SourceFrame]:
    """Frames captured in the first `seconds` of `frames`, by timestamp_ms.

    Duration is measured on the frames' own timestamps, not the wall
    clock, so a synthetic stream is collected instantly and
    deterministically. The first frame at or beyond the limit is read and
    discarded.
    """
    collected: list[SourceFrame] = []
    limit_ms = seconds * 1000.0
    for frame in frames:
        if collected and elapsed_ms(frame, collected[0]) >= limit_ms:
            break
        collected.append(frame)
    return collected


# --- Statistics --------------------------------------------------------------


def channel_unit(name: str) -> str:
    """"deg" for pose angles, "cm" for translation, "score" for blendshapes."""
    if name in POSE_ANGLE_CHANNELS:
        return "deg"
    if name in TRANSLATION_CHANNELS:
        return "cm"
    return "score"


def full_range(name: str) -> float:
    """The channel's full-scale range in its own unit, from FULL_RANGES."""
    return FULL_RANGES.get(name, BLENDSHAPE_RANGE)


def snr(range_: float, std: float, noise_floor: float) -> float:
    """range_ / max(std, noise_floor). Finite for any std >= 0 because
    noise_floor is positive."""
    if noise_floor <= 0:
        raise ValueError(f"noise_floor must be positive, got {noise_floor!r}")
    return range_ / max(std, noise_floor)


def channel_stats(values: list[float], noise_floor: float, range_: float) -> dict:
    """Rest statistics of one channel's values, in the channel's unit.

    count, mean, std (sample, n - 1), min, max, peak_to_peak, and
    distinct_values. For two or more values also zero_variance (every value
    identical), below_noise_floor (std < noise_floor) and
    snr = range_ / max(std, noise_floor); for fewer those three are None.
    noise_floor and range_ are echoed into the result so the snr can be
    recomputed from the record.
    """
    result = {
        "count": len(values),
        "mean": None,
        "std": None,
        "min": None,
        "max": None,
        "peak_to_peak": None,
        "distinct_values": len(set(values)),
        "zero_variance": None,
        "noise_floor": noise_floor,
        "below_noise_floor": None,
        "range": range_,
        "snr": None,
    }
    if not values:
        return result
    low, high = min(values), max(values)
    result.update(mean=statistics.fmean(values), min=low, max=high, peak_to_peak=high - low)
    if len(values) >= 2:
        std = statistics.stdev(values)
        result.update(
            std=std,
            zero_variance=result["distinct_values"] == 1,
            below_noise_floor=std < noise_floor,
            snr=snr(range_, std, noise_floor),
        )
    return result


def stats_for(name: str, values: list[float]) -> dict:
    """channel_stats with the floor and range for the channel's unit."""
    return channel_stats(values, NOISE_FLOORS[channel_unit(name)], full_range(name))


def channel_series(messages: list[dict]) -> dict[str, list[float]]:
    """Every channel's values across the tracking messages, in order.

    Non-tracking messages contribute nothing; their partial features are
    not mixed into the statistics.
    """
    series: dict[str, list[float]] = {}
    for message in messages:
        if not is_tracking(message):
            continue
        for name, value in channel_values(message).items():
            series.setdefault(name, []).append(value)
    return series


def frame_rate_hz(messages: list[dict]) -> float | None:
    """(frames - 1) intervals divided by first-to-last timestamp_ms span.

    Frames per second over every frame, tracking or not. None for fewer
    than two frames or a zero span.
    """
    if len(messages) < 2:
        return None
    span_ms = messages[-1]["timestamp_ms"] - messages[0]["timestamp_ms"]
    if span_ms <= 0:
        return None
    return (len(messages) - 1) * 1000.0 / span_ms


def brightness_values(frames: list[SourceFrame]) -> list[float]:
    """Brightness of every frame that has one, 0 to 255."""
    return [frame.brightness for frame in frames if frame.brightness is not None]


def mean_brightness(frames: list[SourceFrame]) -> float | None:
    """Mean brightness over every frame that has one, 0 to 255, else None."""
    values = brightness_values(frames)
    return statistics.fmean(values) if values else None


def brightness_summary(frames: list[SourceFrame]) -> dict:
    """Definition, mean, min and max brightness; numbers None without an image.

    min and max show whether the lighting itself drifted during the run.
    """
    values = brightness_values(frames)
    return {
        "definition": BRIGHTNESS_DEFINITION,
        "mean": statistics.fmean(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def lighting_record(brightness: dict) -> dict:
    """The record's statement that it holds only for the lighting measured.

    valid_for_brightness repeats the mean brightness so a consumer comparing
    two baselines finds the condition next to the caveat.
    """
    return {
        "dependent_on_brightness": ["frame_rate_hz", "channels.*.std", "channels.*.snr", "channels.*.below_noise_floor"],
        "valid_for_brightness": brightness["mean"],
        "valid_only_for_measured_lighting": True,
        "note": LIGHTING_NOTE,
    }


def state_counts(messages: list[dict]) -> dict[str, int]:
    """Number of messages in each tracking state."""
    return {state: sum(1 for m in messages if m["tracking_state"] == state) for state in TRACKING_STATES}


def summarize(frames: list[SourceFrame], source: str, seconds: float, settle_seconds: float) -> dict:
    """The baseline record for collected frames, as a JSON-ready dict."""
    if not frames:
        raise ValueError("no frames were collected")
    messages = [frame.message for frame in frames]
    counts = state_counts(messages)
    total = len(messages)
    series = channel_series(messages)
    brightness = brightness_summary(frames)
    return {
        "record_type": RECORD_TYPE,
        "schema_version": BASELINE_SCHEMA_VERSION,
        "source": source,
        "measured": source == "camera",
        "requested_seconds": seconds,
        "settle_seconds": settle_seconds,
        "first_timestamp_ms": messages[0]["timestamp_ms"],
        "last_timestamp_ms": messages[-1]["timestamp_ms"],
        "total_frames": total,
        "tracking_frames": counts["tracking"],
        "partial_frames": counts["partial"],
        "lost_frames": counts["lost"],
        "lost_fraction": (total - counts["tracking"]) / total,
        "frame_rate_hz": frame_rate_hz(messages),
        "brightness": brightness,
        "lighting": lighting_record(brightness),
        "statistics": (
            "over tracking frames only, raw published values; std is the sample standard deviation (n - 1); "
            "snr = range / max(std, noise_floor)"
        ),
        "channels": {name: stats_for(name, values) for name, values in series.items()},
    }


# --- Report ------------------------------------------------------------------


def ranked_by_std(record: dict, names) -> list[tuple[str, float]]:
    """(name, std) for the given channels that have a std, noisiest first."""
    channels = record["channels"]
    pairs = [(name, channels[name]["std"]) for name in names if name in channels and channels[name]["std"] is not None]
    return sorted(pairs, key=lambda pair: pair[1], reverse=True)


def blendshape_names(record: dict) -> list[str]:
    """Channel names in the record that are blendshapes, not head pose."""
    return [name for name in record["channels"] if name not in POSE_CHANNELS]


def channel_flags(stats: dict) -> str:
    """Short tags for a channel's dead or floored state, empty if neither."""
    tags = []
    if stats["zero_variance"]:
        tags.append("ZERO_VARIANCE")
    if stats["below_noise_floor"]:
        tags.append("BELOW_NOISE_FLOOR")
    return " ".join(tags)


def blendshape_line(name: str, stats: dict) -> str:
    """One report line. Numbers use significant figures, never fixed
    decimals, so a std of 1e-8 is printed as 1e-08, not as 0.00000."""
    return (
        f"  {name:<22} std={stats['std']:.3g}  mean={stats['mean']:.3g}  "
        f"distinct={stats['distinct_values']}  snr={stats['snr']:.3g}  {channel_flags(stats)}"
    ).rstrip()


def flagged_names(record: dict, field: str) -> list[str]:
    """Channels whose stats[field] is True, in record order."""
    return [name for name, stats in record["channels"].items() if stats[field]]


def format_report(record: dict, top: int = 5) -> str:
    """Human-readable summary of a baseline record.

    Blendshapes are ranked by std among themselves only: their unit is a
    score, and ranking them against degrees or centimeters would be
    meaningless. Head pose channels are listed separately.
    """
    ranked = ranked_by_std(record, blendshape_names(record))
    channels = record["channels"]
    brightness = record["brightness"]["mean"]
    rate = record["frame_rate_hz"]
    lines = [
        f"source={record['source']}  measured={record['measured']}",
        f"frames total={record['total_frames']}  tracking={record['tracking_frames']}  "
        f"partial={record['partial_frames']}  lost={record['lost_frames']}",
        f"lost_fraction={record['lost_fraction']:.4f}",
        f"frame_rate_hz={'n/a' if rate is None else f'{rate:.2f}'}",
        f"mean_brightness={'n/a (no image)' if brightness is None else f'{brightness:.1f} / 255'}",
        "valid only for this lighting: frame rate and noise floor depend on brightness",
        f"noisiest {top} blendshapes by sample std:",
    ]
    lines += [blendshape_line(name, channels[name]) for name, _ in ranked[:top]]
    lines.append(f"quietest {top} blendshapes by sample std:")
    lines += [blendshape_line(name, channels[name]) for name, _ in ranked[-top:][::-1]]
    lines.append(f"zero_variance: {flagged_names(record, 'zero_variance') or 'none'}")
    lines.append(f"below_noise_floor ({NOISE_FLOORS['score']:g} score): {flagged_names(record, 'below_noise_floor') or 'none'}")
    lines.append("head pose:")
    for name, std in ranked_by_std(record, POSE_CHANNELS):
        stats = channels[name]
        lines.append(
            f"  {name:<22} std={std:.4f}  mean={stats['mean']:+.3f}  peak_to_peak={stats['peak_to_peak']:.4f}  snr={stats['snr']:.0f}"
        )
    return "\n".join(lines)


def write_record(record: dict, path: Path) -> None:
    """Write the record as pretty-printed JSON, NaN and infinity refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


# --- Command line ------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure the Sojourner rest baseline while the subject holds still.")
    parser.add_argument("--source", choices=("fake", "camera"), default="fake",
                        help="fake: synthetic frames, not measured. camera: live Face Landmarker frames (default fake)")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help=f"seconds of frames to measure (default {DEFAULT_SECONDS})")
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS,
                        help=f"seconds of frames to discard first, while camera exposure settles (default {DEFAULT_SETTLE_SECONDS})")
    parser.add_argument("--out", type=Path, required=True, help="path of the JSON baseline record to write")
    fake = parser.add_argument_group("fake source")
    fake.add_argument("--rate", type=float, default=30.0, help="frames per second (default 30.0)")
    fake.add_argument("--seed", type=int, default=11, help="random seed (default 11)")
    camera = parser.add_argument_group("camera source")
    camera.add_argument("--camera", type=int, default=0, metavar="INDEX", help="camera device index (default 0)")
    camera.add_argument("--mirrored", action="store_true", help="the camera delivers a mirrored image (default off)")
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.settle < 0:
        parser.error("--settle must not be negative")
    if args.rate <= 0:
        parser.error("--rate must be positive")
    return args


def measure(frames, args: argparse.Namespace) -> dict:
    """Settle, collect and summarize frames according to args."""
    kept = skip_seconds(frames, args.settle) if args.settle > 0 else frames
    return summarize(collect(kept, args.seconds), args.source, args.seconds, args.settle)


def run(args: argparse.Namespace) -> dict:
    """Measure from the selected source and return the record."""
    if args.source == "fake":
        return measure(fake_source_frames(args.rate, args.seed, time.time_ns() // 1_000_000), args)
    from perception.mediapipe_source import CameraSource

    source = CameraSource(camera_index=args.camera, mirrored=args.mirrored)
    try:
        print(f"hold still: measuring {args.seconds:g} s after {args.settle:g} s settle", file=sys.stderr, flush=True)
        return measure(camera_source_frames(source), args)
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from perception.mediapipe_source import CameraError

    try:
        record = run(args)
    except CameraError as error:
        print(f"camera source failed: {error}", file=sys.stderr)
        return 1
    write_record(record, args.out)
    print(format_report(record))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
