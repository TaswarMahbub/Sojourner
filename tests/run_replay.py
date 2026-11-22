"""Replay a recorded session through the same task controller and publisher. It is NOT live.

Run from the repository root:

    python tests/run_replay.py RECORDING.jsonl                 real time
    python tests/run_replay.py RECORDING.jsonl --fast          simulated clock, no waiting
    python tests/run_replay.py PROBE.jsonl --probe-report PROBE_REPORT.json

A recording from tests/run_live_e2e.py carries its own profile, frame arrival
times and camera errors. A perception probe recording
(perception/probe_session.py) carries frames only, so it needs
--probe-report, and its arrival time is taken as the capture time.

Every transcript line and every summary line is prefixed "REPLAY". What is
replayed: the camera frames, at their recorded arrival offsets. What runs
fresh in this process: the task controller, the publisher, the status gate
and the receiver oracle, so the datagrams, the receiver's decisions and the
latency numbers are produced again now. Each frame is re-stamped so it is as
old, relative to now, as it was when it arrived live. Detector inference is
not re-run: its time survives only as the recorded gap between capture and
arrival. This is the fallback when the camera or the hardware fails, and it
says so on every line.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.cue_map import CUE_MAP  # noqa: E402
from tests.run_live_e2e import profile_from  # noqa: E402
from tests.session_harness import (  # noqa: E402
    HarnessRig,
    Pipeline,
    ReplaySource,
    SimClock,
    WallClock,
    load_recording,
    run_loop,
    start,
)

LABEL = "REPLAY"


def replay(recording: Path, *, probe_report: Path | None = None, fast: bool = False, every_tick: bool = False,
           out=print) -> Pipeline:
    header, records = load_recording(recording)
    if header.get("profile") is not None:
        profile, source = header["profile"], f"the profile recorded with the session ({header.get('label', '?')})"
    elif probe_report is not None:
        profile, source = profile_from(probe_report, accepted_by="replay")
    else:
        raise SystemExit(f"{recording} carries no profile; pass --probe-report")
    clock = SimClock() if fast else WallClock()
    with tempfile.TemporaryDirectory() as tmp:
        rig = HarnessRig(Path(tmp), clock)
        start(rig)
        src = ReplaySource(records, clock)
        started = header.get("started_ms")
        when = dt.datetime.fromtimestamp(started / 1000, dt.timezone.utc).isoformat() if started else "unknown"
        for line in (
            f"REPLAY of {recording} -- recorded data, NOT a live session",
            f"recorded: {header.get('protocol_kind')} '{header.get('label', header.get('session_id', '?'))}' started {when}",
            f"frames: {sum(1 for e in src.events if e[1] == 'frame')}, replayed at their recorded arrival offsets "
            f"on a {'SIMULATED' if fast else 'real-time'} clock, re-stamped to replay time",
            "arrival times: " + ("recorded, so detector inference time is reproduced (not re-measured)" if src.arrivals_recorded
                                 else "NOT recorded; arrival = capture, so detector inference is NOT included"),
            f"profile: {source}; cue map control/cue_map.py",
            "fresh in this process: task controller, publisher, status gate, receiver oracle (no radio, no ESP32)",
        ):
            out(f"{LABEL} | {line}")
        pipeline = Pipeline(rig, profile, CUE_MAP, label=LABEL, out=out, every_tick=every_tick)
        run_loop(pipeline, src)
        for line in pipeline.summary():
            pipeline.emit(line)
        pipeline.emit("this summary is from a REPLAY of recorded frames, not a live run")
    return pipeline


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recording", type=Path)
    parser.add_argument("--probe-report", type=Path, default=None)
    parser.add_argument("--fast", action="store_true", help="simulated clock: no waiting, stage times read 0")
    parser.add_argument("--every-tick", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format=f"{LABEL} | log %(name)s: %(message)s")
    replay(args.recording, probe_report=args.probe_report, fast=args.fast, every_tick=args.every_tick)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
