#!/usr/bin/env python3
"""Sojourner: write and read sessions in Tiger Cloud (TimescaleDB).

Setup (run from backend/db; requirements.txt is at the repository root):
    pip install -r ../../requirements.txt
    export TIMESCALE_SERVICE_URL="postgres://<role>:<password>@<host>:<port>/tsdb?sslmode=require"
    psql "$TIMESCALE_SERVICE_URL" -f schema.sql      # admin connection, once per service

Commands:
    make-fixture   Write two SYNTHETIC session files for testing the pipeline (not measured data).
    load-session   Write one session (frames + events files) to the database.
    refresh        Refresh the continuous aggregates now (admin connection only; they also
                   refresh themselves every minute).
    report         Query the two most recent sessions back and print a comparison.
    delete-profile Delete a profile and everything that references it (one action).

Frame files (JSONL, one object per line). Only derived numbers are stored, never images.

  --frame-format contract  (perception output; docs/contracts.md, camera feature stream, schema "4")
    {"schema_version": "4", "timestamp_ms": 1795276800000, "tracking_state": "tracking",
     "features": {"_neutral": 0.0, "browDownLeft": 0.02, ..., "noseSneerRight": 0.01},
     "head_pose": {"matrix": [[...], [...], [...], [0, 0, 0, 1]],
                   "yaw_deg": 2.1, "pitch_deg": -0.4, "roll_deg": 0.0},
     "confidence": null}
    Blendshape names are stored exactly as MediaPipe spells them. Head pose angles are stored as
    channels "head_pose.yaw_deg", "head_pose.pitch_deg" and "head_pose.roll_deg".
    tracking_ok is tracking_state == "tracking". confidence null is stored as SQL NULL.
    A frame that fails the contract is skipped and reported; it is never coerced.

  --frame-format synthetic  (default; the make-fixture output, for pipeline testing only)
    {"t": 1763700000.033, "channels": {"brow_raise_left": 0.41, ...},
     "head": {"yaw": 2.1, "pitch": -0.4, "roll": 0.0}, "confidence": 0.93}
    "t" is epoch seconds or an ISO-8601 string.

Event file (JSONL), one object per line. Interim format: the contract's gesture classification
message is not supported yet, and cues such as "go" are not contract labels.
    {"t": 1763700004.5, "cue": "go", "confidence": 0.88, "stop_reason": null}
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

# psycopg is imported where a connection is made, so the frame adapters can be imported and
# tested without a database driver installed.

CONFIDENCE_FLOOR = 0.5  # synthetic frames below this are stored with tracking_ok = false

SIGNAL_SAMPLES_COPY = ("signal_samples (ts, session_id, profile_id, channel_name, value, "
                       "tracking_ok, confidence)")
CONTROL_EVENTS_COPY = ("control_events (ts, session_id, profile_id, event_sequence, cue, confidence, "
                       "stop_reason)")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(autocommit: bool = False) -> "psycopg.Connection":
    import psycopg

    url = os.environ.get("TIMESCALE_SERVICE_URL")
    if not url:
        sys.exit("Set TIMESCALE_SERVICE_URL to your Tiger Cloud connection string first.")
    return psycopg.connect(url, autocommit=autocommit)


def _json(value):
    from psycopg.types.json import Json

    return Json(value)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_ts(value) -> datetime:
    """Synthetic-fixture timestamps: float epoch seconds or an ISO-8601 string."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_jsonl(path: Path):
    with path.open() as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no}: invalid JSON ({e})") from e


def synthetic_frame_rows(frames_path: Path, session_id, profile_id, conf_floor: float):
    """Synthetic fixtures only. Yield one SIGNAL_SAMPLES_COPY row per channel per frame."""
    for frame in read_jsonl(frames_path):
        ts = parse_ts(frame["t"])
        confidence = frame.get("confidence")
        confidence = None if confidence is None else float(confidence)
        ok = confidence is not None and confidence >= conf_floor
        values = dict(frame.get("channels", {}))
        for axis, v in (frame.get("head") or {}).items():
            values[f"head_{axis}"] = v
        for name, v in values.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            yield (ts, session_id, profile_id, name, float(v), ok, confidence)


# ---------------------------------------------------------------------------
# Contract adapter: docs/contracts.md, section 1, camera feature stream
# ---------------------------------------------------------------------------

CONTRACT_SCHEMA_VERSION = "4"

# The 52 Face Landmarker blendshape names, in MediaPipe index order. Stored verbatim.
BLENDSHAPE_NAMES = (
    "_neutral",
    "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight", "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose", "mouthDimpleLeft", "mouthDimpleRight", "mouthFrownLeft", "mouthFrownRight",
    "mouthFunnel", "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight", "mouthPucker", "mouthRight",
    "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "noseSneerLeft", "noseSneerRight",
)
_BLENDSHAPE_SET = frozenset(BLENDSHAPE_NAMES)

_CAMERA_FIELDS = frozenset(
    ("schema_version", "timestamp_ms", "tracking_state", "features", "head_pose", "confidence"))
_TRACKING_STATES = frozenset(("tracking", "partial", "lost"))
_HEAD_POSE_FIELDS = frozenset(("matrix", "yaw_deg", "pitch_deg", "roll_deg"))
_HEAD_POSE_RANGES = {"yaw_deg": 90.0, "pitch_deg": 60.0, "roll_deg": 45.0}  # symmetric bounds
_MAX_SAFE_INTEGER = 9007199254740991
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ContractFrame(NamedTuple):
    """Result of converting one camera feature message.

    rows      SIGNAL_SAMPLES_COPY tuples; empty when rejected (and for a valid "lost" frame,
              which carries no feature values).
    rejection None when the frame was accepted, otherwise the reason it was rejected.
    """
    rows: list
    rejection: str | None


def _is_number(v) -> bool:
    """A finite JSON number that fits a double. Booleans are not numbers. Never raises."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(float(v))
    except OverflowError:          # an integer too large for a double
        return False


def _check_head_pose(pose) -> str | None:
    """docs/contracts.md section 1, Head pose: matrix layout, scale, angles, translation."""
    if not isinstance(pose, dict):
        return "tracking frame has no head_pose object"
    if pose.keys() != _HEAD_POSE_FIELDS:
        return "head_pose fields are not matrix, yaw_deg, pitch_deg, roll_deg"
    matrix = pose["matrix"]
    if not (isinstance(matrix, list) and len(matrix) == 4
            and all(isinstance(r, list) and len(r) == 4 and all(_is_number(x) for x in r)
                    for r in matrix)):
        return "head_pose.matrix is not 4 rows of 4 finite numbers"
    m = [[float(x) for x in row] for row in matrix]
    if m[3] != [0.0, 0.0, 0.0, 1.0]:
        return "head_pose.matrix last row is not [0, 0, 0, 1]"

    norms = [math.sqrt(sum(m[r][c] ** 2 for r in range(3))) for c in range(3)]
    scale = sum(norms) / 3.0
    if not math.isfinite(scale) or scale <= 0:
        return "head_pose.matrix scale is not positive (a zero matrix is invalid)"
    if any(abs(n - scale) / scale > 0.01 for n in norms):
        return "head_pose.matrix column norms disagree by more than 1 percent"
    rot = [[m[r][c] / scale for c in range(3)] for r in range(3)]
    extracted = {
        "yaw_deg": math.degrees(math.atan2(rot[0][2], rot[2][2])),
        "pitch_deg": math.degrees(math.asin(max(-1.0, min(1.0, rot[1][2])))),
        "roll_deg": math.degrees(math.atan2(rot[1][0], rot[1][1])),
    }
    for axis, bound in _HEAD_POSE_RANGES.items():
        v = pose[axis]
        if not _is_number(v):
            return f"head_pose.{axis} is not a finite number"
        if abs(float(v) - extracted[axis]) > 0.1:
            return f"head_pose.{axis} does not match the matrix within 0.1 degrees"
        if not -bound <= extracted[axis] <= bound:
            return f"head_pose.{axis} is outside [-{bound:g}, {bound:g}]"
    x_cm, y_cm, z_cm = m[0][3], m[1][3], m[2][3]
    if not (-100.0 <= x_cm <= 100.0 and -100.0 <= y_cm <= 100.0):
        return "head_pose translation x or y is outside [-100, 100] cm"
    if not -200.0 <= z_cm <= -5.0:
        return "head_pose translation z is outside [-200, -5] cm"
    return None


def _check_contract_frame(frame) -> str | None:
    """Return why `frame` violates the camera feature contract, or None if it conforms."""
    if not isinstance(frame, dict):
        return "frame is not a JSON object"
    version = frame.get("schema_version")
    if version != CONTRACT_SCHEMA_VERSION:
        return f"schema_version {version!r} is not {CONTRACT_SCHEMA_VERSION!r}"
    missing = _CAMERA_FIELDS - frame.keys()
    if missing:
        return f"missing field(s): {', '.join(sorted(missing))}"
    extra = frame.keys() - _CAMERA_FIELDS
    if extra:
        return f"undefined field(s): {', '.join(sorted(extra))}"

    ts_ms = frame["timestamp_ms"]
    if not isinstance(ts_ms, int) or isinstance(ts_ms, bool):
        return "timestamp_ms is not an integer"
    if not 0 <= ts_ms <= _MAX_SAFE_INTEGER:
        return "timestamp_ms is outside the safe integer range"

    state = frame["tracking_state"]
    if not isinstance(state, str) or state not in _TRACKING_STATES:
        return f"tracking_state {state!r} is not tracking, partial or lost"

    features = frame["features"]
    if not isinstance(features, dict):
        return "features is not an object"
    unknown = features.keys() - _BLENDSHAPE_SET
    if unknown:
        return f"unknown feature name(s): {', '.join(sorted(unknown))}"
    for name, v in features.items():
        if not _is_number(v) or not 0.0 <= v <= 1.0:
            return f"feature {name} is not a number in [0, 1]"
    if state == "tracking" and len(features) != len(BLENDSHAPE_NAMES):
        return "tracking frame does not carry all 52 blendshapes"
    if state == "lost" and features:
        return "lost frame carries features"

    pose = frame["head_pose"]
    if state == "tracking":
        pose_error = _check_head_pose(pose)
        if pose_error is not None:
            return pose_error
    elif pose is not None:
        return f"{state} frame has a non-null head_pose"

    conf = frame["confidence"]
    if conf is not None:
        if not _is_number(conf):
            return "confidence is neither null nor a number"
        if state == "lost":
            return "lost frame has a non-null confidence"
        if state == "tracking" and not 0.0 < conf <= 1.0:
            return "tracking confidence is not in (0, 1]"
        if state == "partial" and not 0.0 <= conf <= 1.0:
            return "partial confidence is not in [0, 1]"
    return None


def contract_frame_rows(frame, session_id, profile_id) -> ContractFrame:
    """Convert one camera feature message into SIGNAL_SAMPLES_COPY rows.

    Never raises on bad input: a non-conforming frame comes back with no rows and a rejection
    reason. confidence null stays None (SQL NULL). tracking_ok comes from tracking_state only.
    """
    reason = _check_contract_frame(frame)
    if reason is None:
        try:
            ts = _EPOCH + timedelta(milliseconds=frame["timestamp_ms"])
        except OverflowError:
            reason = "timestamp_ms is beyond the representable date range"
    if reason is not None:
        return ContractFrame([], reason)

    ok = frame["tracking_state"] == "tracking"
    conf = frame["confidence"]
    conf = None if conf is None else float(conf)
    rows = [(ts, session_id, profile_id, name, float(v), ok, conf)
            for name, v in frame["features"].items()]
    if frame["head_pose"] is not None:
        rows += [(ts, session_id, profile_id, f"head_pose.{axis}", float(frame["head_pose"][axis]),
                  ok, conf)
                 for axis in ("yaw_deg", "pitch_deg", "roll_deg")]
    return ContractFrame(rows, None)


def contract_timestamp(frame) -> datetime | None:
    """Capture time of a conforming camera feature message, or None if it is rejected."""
    if _check_contract_frame(frame) is not None:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=frame["timestamp_ms"])
    except OverflowError:
        return None


def contract_file_rows(frames_path: Path, session_id, profile_id, rejections: list):
    """Yield rows for every conforming frame; append (line number, reason) for each rejection."""
    for line_no, frame in enumerate(read_jsonl(frames_path), 1):
        result = contract_frame_rows(frame, session_id, profile_id)
        if result.rejection is not None:
            rejections.append((line_no, result.rejection))
        yield from result.rows


def event_rows(events_path: Path, session_id, profile_id):
    """Yield CONTROL_EVENTS_COPY rows. event_sequence is the order of the cue in its session."""
    for event_sequence, ev in enumerate(read_jsonl(events_path), 1):
        yield (parse_ts(ev["t"]), session_id, profile_id, event_sequence, ev["cue"],
               ev.get("confidence"), ev.get("stop_reason"))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_profile(conn, display_name, consent_state="pending", demo_visible=False):
    return conn.execute(
        "INSERT INTO profiles (display_name, consent_state, demo_visible) "
        "VALUES (%s, %s, %s) RETURNING profile_id",
        (display_name, consent_state, demo_visible)).fetchone()[0]


def add_calibration(conn, profile_id, tier, thresholds=None, metric_summary=None):
    return conn.execute(
        "INSERT INTO calibrations (profile_id, tier, thresholds, metric_summary) "
        "VALUES (%s, %s, %s, %s) RETURNING calibration_id",
        (profile_id, tier, _json(thresholds or {}), _json(metric_summary or {}))).fetchone()[0]


def start_session(conn, profile_id, calibration_id, tier, started_at, data_origin):
    return conn.execute(
        "INSERT INTO sessions (profile_id, calibration_id, tier, started_at, data_origin) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING session_id",
        (profile_id, calibration_id, tier, started_at, data_origin)).fetchone()[0]


def copy_rows(conn, table_and_cols: str, rows) -> int:
    n = 0
    with conn.cursor() as cur, cur.copy(f"COPY {table_and_cols} FROM STDIN") as cp:
        for row in rows:
            cp.write_row(row)
            n += 1
    return n


def load_session(args) -> None:
    frames_path, events_path = Path(args.frames), Path(args.events)
    contract = args.frame_format == "contract"
    if contract:
        stamps = [t for t in map(contract_timestamp, read_jsonl(frames_path)) if t is not None]
    else:
        stamps = [parse_ts(f["t"]) for f in read_jsonl(frames_path)]
    if not stamps:
        sys.exit(f"{frames_path} has no usable frames.")
    started_at, ended_at = min(stamps), max(stamps)
    rejections: list = []

    with connect() as conn:  # one transaction: all or nothing
        profile_id = args.profile_id or create_profile(
            conn, args.new_profile, args.consent, args.demo_visible)
        calibration = json.loads(Path(args.calibration).read_text()) if args.calibration else {}
        calibration_id = add_calibration(
            conn, profile_id, args.tier,
            calibration.get("thresholds"), calibration.get("metric_summary"))
        session_id = start_session(conn, profile_id, calibration_id, args.tier,
                                   started_at, args.origin)
        if contract:
            rows = contract_file_rows(frames_path, session_id, profile_id, rejections)
        else:
            rows = synthetic_frame_rows(frames_path, session_id, profile_id, args.conf_floor)
        n_samples = copy_rows(conn, SIGNAL_SAMPLES_COPY, rows)
        n_events = copy_rows(conn, CONTROL_EVENTS_COPY,
                             event_rows(events_path, session_id, profile_id))
        sim = json.loads(args.sim_metrics) if args.sim_metrics else {}
        conn.execute("UPDATE sessions SET ended_at = %s, simulator_metrics = %s WHERE session_id = %s",
                     (ended_at, _json(sim), session_id))
    print(f"profile  {profile_id}\nsession  {session_id}  ({args.origin})\n"
          f"wrote    {n_samples} samples, {n_events} events")
    if rejections:
        print(f"rejected {len(rejections)} frame(s) that do not match the camera feature contract:",
              file=sys.stderr)
        for line_no, reason in rejections[:20]:
            print(f"  {frames_path}:{line_no}: {reason}", file=sys.stderr)
        if len(rejections) > 20:
            print(f"  ... and {len(rejections) - 20} more", file=sys.stderr)


def refresh(_args=None) -> None:
    # Continuous aggregate refresh cannot run inside a transaction block.
    with connect(autocommit=True) as conn:
        for view in ("signal_summary_1min", "control_summary_1min"):
            conn.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
            print(f"refreshed {view}")


def delete_profile(args) -> None:
    with connect() as conn:
        n = conn.execute("DELETE FROM profiles WHERE profile_id = %s", (args.profile_id,)).rowcount
    print(f"deleted {n} profile (sessions, calibrations, samples and events cascade)")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

OVERVIEW_SQL = """
SELECT left(s.session_id::text, 8)                                        AS session,
       s.data_origin                                                      AS origin,
       s.tier,
       round(extract(epoch FROM (s.ended_at - s.started_at))::numeric, 1) AS duration_s,
       (SELECT count(*) FROM signal_samples x WHERE x.session_id = s.session_id) AS samples,
       (SELECT round(100 * avg(x.tracking_ok::int), 1)
          FROM signal_samples x WHERE x.session_id = s.session_id)        AS tracking_ok_pct,
       (SELECT count(*) FROM control_events e WHERE e.session_id = s.session_id) AS events,
       (SELECT count(*) FROM control_events e
         WHERE e.session_id = s.session_id AND e.stop_reason IS NOT NULL) AS stops
FROM sessions s
ORDER BY s.started_at DESC
LIMIT %s
"""

CHANNEL_SQL = """
SELECT left(v.session_id::text, 8) AS session,
       v.channel_name,
       round(v.observed_range::numeric, 3)   AS observed_range,
       round(v.mean_value::numeric, 3)       AS mean_value,
       round(100 * v.tracking_ok_frac::numeric, 1) AS tracking_ok_pct
FROM session_channel_stats v
WHERE v.session_id IN (SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT %s)
ORDER BY v.channel_name, v.started_at
"""


def print_table(title: str, cur) -> None:
    cols = [d.name for d in cur.description]
    rows = [[("" if v is None else str(v)) for v in r] for r in cur.fetchall()]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    print(f"\n{title}")
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def report(args) -> None:
    with connect() as conn:
        print_table(f"Latest {args.sessions} sessions", conn.execute(OVERVIEW_SQL, (args.sessions,)))
        print_table("Per-channel summary (from the 1-minute continuous aggregate)",
                    conn.execute(CHANNEL_SQL, (args.sessions,)))
    print("\nIf the channel table is empty, run:  python tiger_store.py refresh")


# ---------------------------------------------------------------------------
# Synthetic fixtures (pipeline testing only; always load with --origin synthetic_seed)
# ---------------------------------------------------------------------------

def make_fixture(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)
    specs = [("a", 11, base), ("b", 22, base + timedelta(hours=1))]
    cues = ["go", "left", "right", "stop"]
    for tag, seed, start in specs:
        rng = random.Random(seed)
        hz, seconds = 30, 90
        pulses = []
        t = rng.uniform(5, 9)
        while t < seconds - 5:
            pulses.append(t)
            t += rng.uniform(8, 14)
        dropout = (rng.uniform(40, 60), 1.0)  # one second of lost tracking
        frames, events = [], []
        for pulse_i, p in enumerate(pulses):
            stop = cues[pulse_i % len(cues)] == "stop"
            events.append({"t": (start + timedelta(seconds=p)).timestamp(),
                           "cue": cues[pulse_i % len(cues)],
                           "confidence": round(rng.uniform(0.7, 0.97), 3),
                           "stop_reason": "user_cue" if stop else None})
        events.append({"t": (start + timedelta(seconds=dropout[0])).timestamp(),
                       "cue": "hold", "confidence": 0.2, "stop_reason": "signal_loss"})
        events.sort(key=lambda e: e["t"])
        for i in range(hz * seconds):
            sec = i / hz
            lost = dropout[0] <= sec < dropout[0] + dropout[1]
            pulse = any(p <= sec < p + 0.6 for p in pulses)
            frames.append({
                "t": (start + timedelta(seconds=sec)).timestamp(),
                "channels": {
                    "brow_raise_left": round(0.05 + rng.gauss(0, 0.01) + (0.6 if pulse else 0), 4),
                    "jaw_open": round(0.04 + rng.gauss(0, 0.01), 4),
                    "eye_blink_left": round(0.03 + abs(rng.gauss(0, 0.02)), 4),
                    "mouth_smile_left": round(0.06 + rng.gauss(0, 0.012), 4),
                },
                "head": {"yaw": round(rng.gauss(0, 0.8), 3), "pitch": round(rng.gauss(0, 0.6), 3),
                         "roll": round(rng.gauss(0, 0.4), 3)},
                "confidence": 0.2 if lost else round(rng.uniform(0.85, 0.99), 3),
            })
        (out / f"session_{tag}_frames.jsonl").write_text("\n".join(json.dumps(f) for f in frames) + "\n")
        (out / f"session_{tag}_events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    print(f"wrote synthetic fixtures to {out}/ (SYNTHETIC: load with --origin synthetic_seed only)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make-fixture")
    p.add_argument("--out", default="fixtures")
    p.set_defaults(fn=make_fixture)

    p = sub.add_parser("load-session")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--profile-id", help="existing pseudonymous profile UUID")
    who.add_argument("--new-profile", help="create a profile with this display name")
    p.add_argument("--consent", default="pending", choices=["pending", "granted", "withdrawn"],
                   help="consent state for --new-profile; granting consent must be explicit")
    p.add_argument("--demo-visible", action="store_true",
                   help="allow aggregated data from this profile in the public demo views")
    p.add_argument("--frames", required=True)
    p.add_argument("--frame-format", default="synthetic", choices=["synthetic", "contract"],
                   help="contract = camera feature messages from docs/contracts.md; "
                        "synthetic = make-fixture output")
    p.add_argument("--events", required=True)
    p.add_argument("--tier", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--origin", required=True, choices=["measured", "synthetic_seed"])
    p.add_argument("--calibration", help='JSON file: {"thresholds": {...}, "metric_summary": {...}}')
    p.add_argument("--sim-metrics", help="JSON string of simulator metrics")
    p.add_argument("--conf-floor", type=float, default=CONFIDENCE_FLOOR,
                   help="synthetic frames only; contract frames use tracking_state")
    p.set_defaults(fn=load_session)

    p = sub.add_parser("refresh")
    p.set_defaults(fn=refresh)

    p = sub.add_parser("report")
    p.add_argument("--sessions", type=int, default=2)
    p.set_defaults(fn=report)

    p = sub.add_parser("delete-profile")
    p.add_argument("--profile-id", required=True)
    p.set_defaults(fn=delete_profile)

    return ap


def main() -> None:
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
