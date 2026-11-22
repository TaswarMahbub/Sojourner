"""Live end-to-end run: laptop camera -> calibrated channels -> task controller -> publisher -> receiver oracle.

Run from the repository root with the camera dependencies installed
(perception/requirements.txt):

    python tests/run_live_e2e.py --probe-report PATH_TO_PROBE_REPORT.json [--seconds 60]

Every session is recorded by default to captures/control/live_<UTC>.jsonl
(gitignored) so it can be played back with tests/run_replay.py. Pass
--no-record to skip that.

The transcript prints a line whenever something changes (cue, task, speed
setting, datagram, receiver result, torque) and once a second otherwise;
--every-tick prints every tick. Each line shows the current speed setting v.
The summary reports accepted motion datagrams, watchdog trips, every speed
change (flagging the ones made in Hold, which nothing on the chair shows),
and the stop-latency tables (control/latency.py) against the 150 ms budget.

Things to do in front of the camera for a complete latency table:

    jaw open            Go (from Hold)
    jaw open again      Stop (while moving): a cued-stop latency sample
    look away / cover   signal-loss stop: a signal-loss latency sample
    the lens

Stop with Ctrl-C or --seconds. Unplugging the camera mid-run is handled: the
loop keeps ticking, the frame goes stale and the chair stops (drill a, live).

See tests/session_harness.py for what is real here and what is not. In
short: control/ is unmodified; the receiver is the in-process contract
oracle, not the rover; there is no gesture classifier, so each actionable
frame gets a placeholder "neutral" gesture.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from control.cue_map import CUE_MAP  # noqa: E402
from perception import probe_session  # noqa: E402
from tests.session_harness import (  # noqa: E402
    PLACEHOLDER_CONFIDENCE,
    TICK_MS,
    CameraThread,
    HarnessRig,
    Pipeline,
    Recorder,
    WallClock,
    run_loop,
    start,
)

CHANNELS = ("jawOpen", "pitch_deg", "yaw_deg")
LABEL = "LIVE"


def profile_from(path: Path, accepted_by: str) -> tuple[dict, str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    profile = probe_session.profile_from_report(report, CHANNELS, "jawOpen", accepted_by=accepted_by)
    return profile, f"probe report {report['session_id']} ({report['threshold_rule']})"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe-report", type=Path, required=True, help="probe report JSON for the profile")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--mirrored", action="store_true")
    parser.add_argument("--model", type=Path, default=None, help="face_landmarker.task (default perception/models/)")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--record", type=Path, default=None, help="recording path (default captures/control/live_<UTC>.jsonl)")
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--every-tick", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format=f"{LABEL} | log %(name)s: %(message)s")
    profile, source = profile_from(args.probe_report, accepted_by="live e2e run")
    recorder = None
    if not args.no_record:
        path = args.record or REPO_ROOT / "captures" / "control" / f"live_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        recorder = Recorder(path)

    clock = WallClock()
    with tempfile.TemporaryDirectory() as tmp:
        print(f"{LABEL} | live session, camera {args.camera}{' (mirrored)' if args.mirrored else ''}, up to {args.seconds:.0f} s")
        print(f"{LABEL} | profile: {source}; cue map control/cue_map.py")
        print(f"{LABEL} | gesture: no classifier; each actionable frame gets 'neutral' over that frame at the "
              f"placeholder score {PLACEHOLDER_CONFIDENCE}")
        print(f"{LABEL} | receiver: in-process contract oracle (no radio, no ESP32); ticks on each frame or every {TICK_MS} ms")
        if recorder is not None:
            print(f"{LABEL} | recording to {recorder.path}")
        # Open the camera and the model BEFORE arming: arming starts the receiver's 250 ms watchdog,
        # and loading the model takes longer than that.
        try:
            camera = CameraThread(args.camera, args.mirrored, clock, args.model)
        except Exception as exc:
            print(f"{LABEL} | camera could not start: {exc}")
            return 1
        rig = HarnessRig(Path(tmp), clock)
        start(rig)
        pipeline = Pipeline(rig, profile, CUE_MAP, label=LABEL, every_tick=args.every_tick, recorder=recorder)
        try:
            run_loop(pipeline, camera, duration_s=args.seconds)
        except KeyboardInterrupt:
            pipeline.note("stopped by the operator (Ctrl-C)")
        finally:
            camera.close()
        pipeline.print_summary()
        if recorder is not None:
            recorder.close()
            print(f"{LABEL} | replay with: python tests/run_replay.py {recorder.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
