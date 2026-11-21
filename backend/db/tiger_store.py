#!/usr/bin/env python3
"""Sojourner: write and read sessions in Tiger Cloud (TimescaleDB).

Setup:
    pip install -r requirements.txt
    export $TIMESCALE_SERVICE_URL="postgres://tsdbadmin:<password>@<host>:<port>/tsdb?sslmode=require"
    psql "$TIMESCALE_SERVICE_URL" -f schema.sql

Commands:
    make-fixture   Write two SYNTHETIC session files for testing the pipeline (not measured data).
    load-session   Write one session (frames + events files) to the database.
    refresh        Refresh the continuous aggregates so summaries include the new data.
    report         Query the two most recent sessions back and print a comparison.
    delete-profile Delete a profile and everything that references it (one action).

Frame file (JSONL), one object per line, matching the 30 Hz channel stream:
    {"t": 1763700000.033, "channels": {"brow_raise_left": 0.41, ...},
     "head": {"yaw": 2.1, "pitch": -0.4, "roll": 0.0}, "confidence": 0.93}
    "t" is epoch seconds or an ISO-8601 string. Only derived numbers are stored, never images.

Event file (JSONL), one object per line:
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

import psycopg
from psycopg.types.json import Json

CONFIDENCE_FLOOR = 0.5  # frames below this are stored with tracking_ok = false


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(autocommit: bool = False) -> psycopg.Connection:
    url = os.environ.get("TIMESCALE_SERVICE_URL")
    if not url:
        sys.exit("Set TIMESCALE_SERVICE_URL to your Tiger Cloud connection string first.")
    return psycopg.connect(url, autocommit=autocommit)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_ts(value) -> datetime:
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


def frame_rows(frames_path: Path, session_id, profile_id, conf_floor: float):
    """Yield (ts, session_id, profile_id, channel_name, value, tracking_ok) per channel per frame."""
    for frame in read_jsonl(frames_path):
        ts = parse_ts(frame["t"])
        ok = float(frame.get("confidence", 1.0)) >= conf_floor
        values = dict(frame.get("channels", {}))
        for axis, v in (frame.get("head") or {}).items():
            values[f"head_{axis}"] = v
        for name, v in values.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            yield (ts, session_id, profile_id, name, float(v), ok)


def event_rows(events_path: Path, session_id, profile_id):
    for seq, ev in enumerate(read_jsonl(events_path), 1):
        yield (parse_ts(ev["t"]), session_id, profile_id, seq, ev["cue"],
               ev.get("confidence"), ev.get("stop_reason"))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def create_profile(conn, display_name, consent_state="granted", demo_visible=False):
    return conn.execute(
        "INSERT INTO profiles (display_name, consent_state, demo_visible) "
        "VALUES (%s, %s, %s) RETURNING profile_id",
        (display_name, consent_state, demo_visible)).fetchone()[0]


def add_calibration(conn, profile_id, tier, thresholds=None, metric_summary=None):
    return conn.execute(
        "INSERT INTO calibrations (profile_id, tier, thresholds, metric_summary) "
        "VALUES (%s, %s, %s, %s) RETURNING calibration_id",
        (profile_id, tier, Json(thresholds or {}), Json(metric_summary or {}))).fetchone()[0]


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
    first = next(iter(read_jsonl(frames_path)), None)
    if first is None:
        sys.exit(f"{frames_path} has no frames.")
    last = None
    for last in read_jsonl(frames_path):
        pass
    started_at, ended_at = parse_ts(first["t"]), parse_ts(last["t"])

    with connect() as conn:  # one transaction: all or nothing
        profile_id = args.profile_id or create_profile(
            conn, args.new_profile, args.consent, args.demo_visible)
        calibration = json.loads(Path(args.calibration).read_text()) if args.calibration else {}
        calibration_id = add_calibration(
            conn, profile_id, args.tier,
            calibration.get("thresholds"), calibration.get("metric_summary"))
        session_id = start_session(conn, profile_id, calibration_id, args.tier,
                                   started_at, args.origin)
        n_samples = copy_rows(
            conn, "signal_samples (ts, session_id, profile_id, channel_name, value, tracking_ok)",
            frame_rows(frames_path, session_id, profile_id, args.conf_floor))
        n_events = copy_rows(
            conn, "control_events (ts, session_id, profile_id, sequence, cue, confidence, stop_reason)",
            event_rows(events_path, session_id, profile_id))
        sim = json.loads(args.sim_metrics) if args.sim_metrics else {}
        conn.execute("UPDATE sessions SET ended_at = %s, simulator_metrics = %s WHERE session_id = %s",
                     (ended_at, Json(sim), session_id))
    print(f"profile  {profile_id}\nsession  {session_id}  ({args.origin})\n"
          f"wrote    {n_samples} samples, {n_events} events")


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

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make-fixture")
    p.add_argument("--out", default="fixtures")
    p.set_defaults(fn=make_fixture)

    p = sub.add_parser("load-session")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--profile-id", help="existing pseudonymous profile UUID")
    who.add_argument("--new-profile", help="create a profile with this display name")
    p.add_argument("--consent", default="granted", choices=["pending", "granted", "withdrawn"])
    p.add_argument("--demo-visible", action="store_true",
                   help="allow aggregated data from this profile in the public demo views")
    p.add_argument("--frames", required=True)
    p.add_argument("--events", required=True)
    p.add_argument("--tier", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--origin", required=True, choices=["measured", "synthetic_seed"])
    p.add_argument("--calibration", help='JSON file: {"thresholds": {...}, "metric_summary": {...}}')
    p.add_argument("--sim-metrics", help="JSON string of simulator metrics")
    p.add_argument("--conf-floor", type=float, default=CONFIDENCE_FLOOR)
    p.set_defaults(fn=load_session)

    p = sub.add_parser("refresh")
    p.set_defaults(fn=refresh)

    p = sub.add_parser("report")
    p.add_argument("--sessions", type=int, default=2)
    p.set_defaults(fn=report)

    p = sub.add_parser("delete-profile")
    p.add_argument("--profile-id", required=True)
    p.set_defaults(fn=delete_profile)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
