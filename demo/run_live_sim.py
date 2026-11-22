"""Software-only demo: camera -> calibrated cues -> shared control -> receiver oracle, mirrored to the 3D simulator.

Live camera (needs a probe report and perception/requirements.txt):

    python -m demo.run_live_sim --probe-report PROBE_REPORT.json [--seconds 300]

Fallback, no camera (scripted, synthetic frames through the same pipeline, in real time):

    python -m demo.run_live_sim --synthetic

Replay of MEASURED frames (a probe recording or a --record file), in real time, with
the user's profile. Labelled REPLAY on every line; nothing is re-detected:

    python -m demo.run_live_sim --replay RECORDING.jsonl --probe-report PROBE_REPORT.json

Both modes run the unmodified control/ code through tests/session_harness.py,
exactly as tests/run_live_e2e.py does. The receiver is the in-process contract
oracle, wrapped by demo/motor_bridge.TapReceiver, and every receiver call is
streamed to ws://127.0.0.1:${SOJOURNER_MOTOR_BRIDGE_PORT:-default} for the
browser mirror (frontend/live.html). In live mode the camera is opened once,
here, and every frame is also relayed on ws://127.0.0.1:8765 in the
perception.publish wire format, so the dashboard runs without a second camera
process. Do not run perception.publish at the same time.

Nothing here can reach a rover. No UDP socket is opened, and the only key is the
labelled test key from tests/control_auth.py.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from control.cue_map import CUE_MAP  # noqa: E402
from demo.face_preview import PreviewChannel, preview_port  # noqa: E402
from demo.motor_bridge import Fanout, OpLog, SharedCamera, TapReceiver, bridge_port  # noqa: E402
from perception.publish import DEFAULT_PORT as CAMERA_PORT  # noqa: E402
from tests.control_rig import lost_frame  # noqa: E402
from tests.motor_receiver import MotorReceiver  # noqa: E402
from tests.run_doorway_e2e import posed_frame, synthetic_profile  # noqa: E402
from tests.session_harness import (  # noqa: E402
    END,
    CameraThread,
    HarnessRig,
    Pipeline,
    Recorder,
    ReplaySource,
    WallClock,
    load_recording,
    run_loop,
    start,
)

LABEL = "DEMO"
FRAME_MS = 50
BOOT_NONCE = 7

# (seconds, action). Actions: rest, jaw, yaw_left, yaw_right, lost, drop_link, restore_link, forge.
# Synthetic: these frames are generated here, not measured.
SCRIPT = (
    (3.0, "rest"),
    (0.1, "jaw"),          # Go: a sustained crossing takes two frames
    (5.0, "rest"),         # travel
    (0.1, "yaw_left"),     # one 15 degree heading increment
    (3.0, "rest"),
    (0.1, "jaw"),          # Stop while moving
    (2.0, "rest"),
    (0.1, "jaw"),          # Go again
    (2.0, "rest"),
    (0.05, "forge"),       # while moving, a forged-MAC copy reaches the receiver: refused, never animated
    (3.0, "rest"),
    (1.0, "lost"),         # face leaves the frame: signal-loss stop, then the Go lockout
    (3.5, "rest"),
    (0.1, "jaw"),          # Go
    (2.0, "rest"),
    (0.05, "drop_link"),   # while moving, datagrams stop arriving: the receiver's watchdog halts and latches
    (1.0, "rest"),
    (0.05, "restore_link"),
    (2.0, "rest"),
)


class ScriptedFrames:
    """A frame source with CameraThread's get() interface, playing SCRIPT on the given clock."""

    def __init__(self, clock, profile: dict, rig: HarnessRig, out=print):
        self.clock = clock
        self.rig = rig
        self.out = out
        rest = {b["channel"]: b["rest_mean"] for b in profile["bindings"]}
        self.rest = {"jaw": rest["jawOpen"], "yaw": rest["yaw_deg"], "pitch": rest["pitch_deg"], "roll": 0.0}
        self.steps = []
        for seconds, action in SCRIPT:
            self.steps += [action] * max(1, round(seconds * 1000 / FRAME_MS))

    def get(self, timeout_ms):
        if not self.steps:
            return END
        self.clock.wait(FRAME_MS)
        action = self.steps.pop(0)
        now = self.clock.unix
        values = dict(self.rest)
        if action == "jaw":
            values["jaw"] = 0.45
        elif action == "yaw_left":
            values["yaw"] = self.rest["yaw"] + 25.0
        elif action == "yaw_right":
            values["yaw"] = self.rest["yaw"] - 25.0
        elif action == "drop_link":
            self.rig.link_up = False
            self.out(f"{LABEL} | script: link dropped (datagrams lost in transit)")
        elif action == "restore_link":
            self.rig.link_up = True
            self.out(f"{LABEL} | script: link restored")
        elif action == "forge":
            self.forge()
        frame = lost_frame(now) if action == "lost" else posed_frame(now, **values)
        return ("frame", frame, self.clock.stamp(), None)

    def forge(self) -> None:
        """Deliver the last real datagram again with its MAC altered, straight to the receiver input."""
        if not self.rig.sent:
            return
        message = dict(self.rig.sent[-1])
        message["mac"] = ("0" if message["mac"][0] != "0" else "1") + message["mac"][1:]
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        result = self.rig.receiver.handle_raw(payload)
        self.out(f"{LABEL} | script: forged-MAC datagram injected; oracle result {result}")

    def close(self) -> None:
        pass


class PreviewCameraThread(CameraThread):
    """tests/session_harness.CameraThread with the local face preview tapped off the same camera.

    Identical to CameraThread._run except that the CameraSource gets the preview
    callback. One camera, one detector; the preview only receives references.
    """

    def __init__(self, camera_index: int, mirrored: bool, clock, model_path=None, *, preview=None):
        self._preview = preview
        super().__init__(camera_index, mirrored, clock, model_path)

    def _run(self, camera_index, mirrored) -> None:
        from perception.mediapipe_source import MODEL_PATH, CameraError, CameraSource

        try:
            source = CameraSource(camera_index, mirrored, self._model_path or MODEL_PATH, preview=self._preview)
        except Exception as exc:  # reported to the caller, never swallowed
            self._open_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    cf = source.read_frame()
                except CameraError as exc:
                    self._q.put(("error", str(exc)))
                    return
                self._q.put(("frame", cf.message, self._clock.stamp(), cf.brightness))
        finally:
            source.close()


def build(clock, state_dir: Path, sink) -> HarnessRig:
    """A harness rig whose receiver is the tapped oracle. Every receiver call reaches the sink."""
    tap = TapReceiver(MotorReceiver(BOOT_NONCE), sink)
    rig = HarnessRig(state_dir, clock, boot_nonce=BOOT_NONCE, receiver=tap)
    start(rig)
    return rig


def run_scripted(clock, state_dir: Path, sink, *, out=print, every_tick=False) -> Pipeline:
    """The synthetic script through the unmodified pipeline. Used by --synthetic and by the fixture."""
    profile = synthetic_profile()
    rig = build(clock, state_dir, sink)
    pipeline = Pipeline(rig, profile, CUE_MAP, label=LABEL, out=out, every_tick=every_tick)
    run_loop(pipeline, ScriptedFrames(clock, profile, rig, out=out))
    return pipeline


def run_replayed(clock, state_dir: Path, sink, recording: Path, profile: dict, *, out=print,
                 every_tick=False) -> Pipeline:
    """Recorded frames through the unmodified pipeline, at their recorded offsets (tests/session_harness.ReplaySource)."""
    _header, records = load_recording(recording)
    rig = build(clock, state_dir, sink)
    pipeline = Pipeline(rig, profile, CUE_MAP, label="REPLAY", out=out, every_tick=every_tick)
    run_loop(pipeline, ReplaySource(records, clock))
    return pipeline


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe-report", type=Path, help="probe report JSON: the user's calibration profile")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--synthetic", action="store_true", help="no camera: scripted synthetic frames")
    mode.add_argument("--replay", type=Path, help="replay a recording of measured frames (needs --probe-report)")
    parser.add_argument("--record", type=Path, default=None,
                        help="live mode: also record frames to this JSONL (local, gitignored under captures/)")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--model", type=Path, default=None)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--no-face-preview", action="store_true",
                        help="live mode: do not serve the local face-mesh preview")
    parser.add_argument("--no-frame-relay", action="store_true", help=f"do not serve frames on {CAMERA_PORT}")
    parser.add_argument("--every-tick", action="store_true")
    args = parser.parse_args(argv)
    if not args.synthetic and args.probe_report is None:
        parser.error("--probe-report is required for the live camera and for --replay")
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format=f"{LABEL} | log %(name)s: %(message)s")

    port = bridge_port()
    bridge = Fanout(port, replay=True)
    log = OpLog()
    log.listeners.append(bridge.publish)
    print(f"{LABEL} | SOFTWARE ONLY. Receiver: in-process contract oracle, mirrored to ws://127.0.0.1:{bridge.port}")
    print(f"{LABEL} | open the 3D mirror at http://localhost:5173/live.html (cd frontend && npm run dev)")
    clock = WallClock()
    relay = None
    preview_server = None
    preview = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if args.synthetic:
                print(f"{LABEL} | synthetic scripted frames, not a camera and not measured")
                pipeline = run_scripted(clock, Path(tmp), log, every_tick=args.every_tick)
                pipeline.print_summary()
                return _serve_until_interrupted(pipeline.rig)
            from tests.run_live_e2e import profile_from

            profile, source = profile_from(args.probe_report, accepted_by="live sim demo")
            if args.replay is not None:
                print(f"REPLAY | {args.replay}: recorded MEASURED frames, NOT live; profile {source}")
                pipeline = run_replayed(clock, Path(tmp), log, args.replay, profile, every_tick=args.every_tick)
                pipeline.print_summary()
                return _serve_until_interrupted(pipeline.rig)
            print(f"{LABEL} | live camera {args.camera}; profile {source}; cue map control/cue_map.py")
            if not args.no_face_preview:
                preview_server = Fanout(preview_port(), latest_only=True)
                preview = PreviewChannel(preview_server.publish, has_clients=lambda: preview_server.has_clients)
                print(f"{LABEL} | face preview (local only, not recorded) on ws://127.0.0.1:{preview_server.port}")
            # Open the camera before arming: arming starts the 250 ms watchdog (as run_live_e2e.py does).
            try:
                camera = PreviewCameraThread(args.camera, False, clock, args.model,
                                             preview=preview.offer if preview is not None else None)
            except Exception as exc:
                print(f"{LABEL} | camera could not start: {exc}")
                return 1
            if not args.no_frame_relay:
                relay = Fanout(CAMERA_PORT)
                print(f"{LABEL} | camera frames relayed on ws://127.0.0.1:{relay.port} for the dashboard")
            frames = SharedCamera(camera, relay)
            rig = build(clock, Path(tmp), log)
            recorder = Recorder(args.record) if args.record is not None else None
            if recorder is not None:
                print(f"{LABEL} | recording frames to {args.record} (replay with --replay)")
            pipeline = Pipeline(rig, profile, CUE_MAP, label=LABEL, every_tick=args.every_tick, recorder=recorder)
            try:
                run_loop(pipeline, frames, duration_s=args.seconds)
            except KeyboardInterrupt:
                pipeline.note("stopped by the operator (Ctrl-C)")
            finally:
                frames.close()
            pipeline.print_summary()
            if preview is not None:
                print(f"{LABEL} | {preview.summary()}")
            if recorder is not None:
                recorder.close()
    finally:
        bridge.close()
        if relay is not None:
            relay.close()
        if preview is not None:
            preview.close()
        if preview_server is not None:
            preview_server.close()
    return 0


def advance_after_run(rig: HarnessRig) -> None:
    """The run has ended: the laptop sends nothing more, but a receiver's clock keeps running.

    Advancing the oracle's clock lets its own watchdog halt a rover that was still moving, exactly
    as a receiver whose laptop went silent would, and the browser mirror shows that halt.
    """
    rig.sync()


def _serve_until_interrupted(rig: HarnessRig) -> int:
    print(f"{LABEL} | finished; no more datagrams. The receiver clock keeps running (its watchdog halts a "
          f"moving rover) and the bridge keeps serving late browsers. Ctrl-C to stop.")
    try:
        for _ in range(600):  # 30 s of receiver clock, then serve the frozen session
            advance_after_run(rig)
            time.sleep(0.05)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


def decode_datagram(record: dict) -> bytes:
    """The exact datagram bytes a mirrored 'datagram' op carries."""
    return base64.b64decode(record["op"]["raw_b64"])


if __name__ == "__main__":
    raise SystemExit(main())
