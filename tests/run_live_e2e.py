"""Live end-to-end run: camera -> task controller -> Publisher.tick() -> MotorReceiver.

Run from the repository root:

    python tests/run_live_e2e.py PROBE_REPORT.json --accepted-by NAME
    python tests/run_live_e2e.py PROBE_REPORT.json --accepted-by NAME --stand-in-gesture 0.95

Real time, real frames, no scripted cues. Once per camera frame it:

1. reads one frame from perception.mediapipe_source.CameraSource;
2. advances the receiver to the laptop clocks and lets the publisher observe
   the receiver's status;
3. produces a gesture message (see "Gesture source" below);
4. gives the frame and the gesture to the publisher and to the task
   controller (control/task_controller.py, with control/cue_map.py CUE_MAP
   and a profile built from the probe report);
5. calls TaskController.tick(), which calls Publisher.tick() once;
6. prints one line.

Each line shows the tick gap and frame age in ms, every bound channel that is
past its threshold on its bound side (with its value), the cues the
controller acted on (and any Go it suppressed during a lockout), the task,
the MotionRequest, the datagram the publisher sent, and the receiver's
decision. When the datagram is a stop, the publisher's reason follows.

Gesture source. There is no trained classifier. The repository's only
gesture producer, ml/classify.py, emits `unknown` for a non-actionable
window and NOTHING for an actionable one. That is the default here, so with
a face in view the controller and the publisher see "gesture missing" and
nothing moves. --stand-in-gesture SCORE replaces it with a stand-in:
`neutral`, model_version `none`, a one-frame window on the current frame,
and the given score, which the confidence cap then uses. The score is not
measured; the user picks it. The stand-in carries no cue: every cue still
comes from the live frame through the calibrated channels.

The profile. profile_from_report (perception/probe_session.py) binds
jawOpen, pitch_deg and yaw_deg with jawOpen as the stop channel, as
control/cue_map.py requires. The probe session does not record whether the
camera was mirrored, so --mirrored must match how that session was run;
the wrong value swaps Left and Right.

The receiver is the contract oracle in tests/motor_receiver.py, in-process,
not firmware, not a hardware test. It shares the laptop's clocks: its
monotonic clock is the laptop's monotonic clock and its Unix clock is
marked synchronized to the laptop's, so the contract's 250 ms watchdog,
challenge freshness and command expiry are all enforced in real time. It is
armed once at start by an operator rearm; nothing rearms it afterwards.

Ctrl+C stops the run and prints a summary.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.cue_map import CUE_MAP  # noqa: E402
from control.publisher import SystemClock  # noqa: E402
from control.task_controller import TaskController  # noqa: E402
from control.tiers import channel_value  # noqa: E402
from tests.control_rig import build_publisher, provision  # noqa: E402
from tests.motor_receiver import MotorReceiver  # noqa: E402

CHANNELS = ("jawOpen", "pitch_deg", "yaw_deg")
STOP_CHANNEL = "jawOpen"


class LiveRig:
    """Publisher and in-process receiver on the laptop's real clocks."""

    def __init__(self, state_dir: Path):
        provision(state_dir)
        self.clock = SystemClock()
        self.receiver = MotorReceiver(random.randrange(1, 2**31))
        self.sent: list[dict] = []
        self.results: list[str] = []
        self.publisher = build_publisher(state_dir, self._deliver, self.clock)
        self.sync()

    def _deliver(self, payload: bytes) -> None:
        self.sent.append(json.loads(payload))
        self.results.append(self.receiver.handle_raw(payload))

    def sync(self) -> None:
        self.receiver.set_monotonic(self.clock.mono_ms())
        self.receiver.note_verified_clock(self.clock.unix_ms())

    def status(self) -> str:
        status = self.receiver.status()
        if status is None:
            return "not-delivered"
        return self.publisher.observe_status(json.dumps(status).encode("utf-8"))

    def tick_raw(self, **kwargs):
        before = len(self.results)
        message = self.publisher.tick(**kwargs)
        return message, (self.results[-1] if len(self.results) > before else None)

    def start(self) -> list[str]:
        """Receiver ready, session handshake, operator rearm. Returns what happened, step by step."""
        log = []
        self.receiver.complete_startup()
        self.receiver.set_physical_estop(False)
        log.append(f"cutoff acknowledged: {self.receiver.acknowledge_cutoff()}")
        self.sync()
        log.append(f"status -> gate {self.status()}")
        message, result = self.tick_raw()
        log.append(f"session start -> {result} ({self.publisher.last_decision})")
        self.sync()
        log.append(f"status -> gate {self.status()}, trusted={self.publisher.session is not None and self.publisher.session.trusted}")
        message, result = self.tick_raw(operator_rearm=True)
        log.append(f"operator rearm -> {result} ({self.publisher.last_decision})")
        self.sync()
        log.append(f"status -> gate {self.status()}, armed={self.receiver.armed}")
        return log


class MlGestures:
    """The repository's real gesture producer: ml/classify.py over one-frame windows."""

    label = "ml/classify.py (unknown for a non-actionable window, nothing for an actionable one)"

    def __init__(self):
        from ml.classify import classification_for_window
        from ml.windows import WindowAssembler

        self._classify = classification_for_window
        self._assembler = WindowAssembler(length=1, stride=1)
        self.latest: dict | None = None
        self.last_errors: tuple = ()

    def update(self, frame: dict, now_ms: int) -> dict | None:
        push = self._assembler.push(frame)
        self.last_errors = push.errors
        if push.window is not None:
            produced = self._classify(push.window, produced_at_ms=max(now_ms, push.window.end_timestamp_ms))
            if produced is not None:
                self.latest = produced
        return self.latest


class StandInGestures:
    """`neutral` with a user-chosen score, one-frame window on the current frame. Not a classifier."""

    def __init__(self, score: float):
        self.score = score
        self.label = f"STAND-IN neutral, model_version none, score {score} (chosen, not measured)"
        self.last_errors: tuple = ()

    def update(self, frame: dict, now_ms: int) -> dict:
        ts = frame["timestamp_ms"]
        return {
            "schema_version": frame["schema_version"],
            "timestamp_ms": max(now_ms, ts),
            "source_timestamp_ms": ts,
            "window_start_timestamp_ms": ts,
            "window_end_timestamp_ms": ts,
            "window_frame_count": 1,
            "label": "neutral",
            "confidence": self.score,
            "model_version": "none",
        }


def crossed(controller: TaskController, frame: dict) -> str:
    """Every bound side whose channel is past its threshold in this frame, with the value."""
    out = []
    for cb in CUE_MAP:
        value = channel_value(frame, cb.channel)
        if value is None:
            continue
        binding = controller.profile.binding(cb.channel)
        if binding.past(value, cb.side):
            arrow = "+" if cb.side > 0 else "-"
            out.append(f"{cb.channel}{arrow}{value:.3g}")
    return ",".join(out) or "-"


def line(dt, age, frame, gesture, controller, request, message, result, decision) -> str:
    state = frame.get("tracking_state", "?")
    cues = ",".join(f"{c.kind}({c.source})" for c in controller.last_cues) or "-"
    if controller.last_suppressed:
        cues += " [locked out: " + ",".join(c.kind for c in controller.last_suppressed) + "]"
    task = controller.task
    task_text = f"{task.kind} v={task.speed_mps:.2f} turn={task.turn_rad:+.3f}"
    lock = controller.lockout_remaining_ms(controller._last_ms) if controller._last_ms is not None else 0
    if lock:
        task_text += f" lock={lock}"
    req = "None" if request is None else f"{request.linear_mps:.3f} {request.angular_rad_s:+.3f}"
    sent = ("-" if message is None else
            f"{message['task_state']} stop={str(message['stop']).lower()} "
            f"{message['linear_mps']:.3f} {message['angular_rad_s']:+.3f}")
    g = "g=none" if gesture is None else f"g={gesture['label']}"
    why = f"  ({decision})" if message is None or message["stop"] else ""
    return (f"dt={dt:>4} age={age:>4} {state:<8} {g:<12} crossed={crossed(controller, frame):<28} "
            f"cues={cues:<22} task={task_text:<34} request={req:<13} sent={sent:<28} "
            f"receiver={result or 'nothing sent'}{why}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Live camera -> task controller -> publisher -> MotorReceiver.")
    parser.add_argument("report", type=Path, help="probe report JSON from perception/probe_session.py")
    parser.add_argument("--accepted-by", required=True,
                        help="who reviewed the report and accepts the profile (recorded in the profile)")
    parser.add_argument("--camera", type=int, default=0, metavar="INDEX", help="camera device index (default 0)")
    parser.add_argument("--mirrored", action="store_true",
                        help="the camera delivers a mirrored image; must match the probe session")
    parser.add_argument("--stand-in-gesture", type=float, metavar="SCORE",
                        help="replace ml/classify.py with a stand-in neutral gesture carrying this score")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds (default: until Ctrl+C)")
    args = parser.parse_args(argv)
    if args.stand_in_gesture is not None and not 0.0 <= args.stand_in_gesture <= 1.0:
        parser.error("--stand-in-gesture must lie in [0, 1]")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    from perception import probe_session
    from perception.mediapipe_source import CameraSource

    report = json.loads(args.report.read_text(encoding="utf-8"))
    profile = probe_session.profile_from_report(report, CHANNELS, STOP_CHANNEL, accepted_by=args.accepted_by)
    controller = TaskController(profile, CUE_MAP)
    gestures = StandInGestures(args.stand_in_gesture) if args.stand_in_gesture is not None else MlGestures()

    print(f"profile: probe report {report['session_id']} ({report['threshold_rule']}), accepted by {args.accepted_by}")
    for b in controller.profile.bindings:
        print(f"  {b.channel:<10} rest={b.rest_mean:.4g} below<={b.threshold_lower:.4g} above>={b.threshold_upper:.4g} "
              f"deadband=[{b.deadband_lower:.4g}, {b.deadband_upper:.4g}]")
    print("cue map: " + ", ".join(f"{cb.channel} {cb.direction} -> {cb.cue}" + (f"/{cb.moving_cue} while moving" if cb.moving_cue else "")
                                  for cb in CUE_MAP))
    print(f"gesture source: {gestures.label}")
    print(f"camera {args.camera}, mirrored={args.mirrored}")

    ticks = 0
    dts: list[int] = []
    ages: list[int] = []
    results: Counter = Counter()
    decisions: Counter = Counter()
    motion_sent = 0
    with tempfile.TemporaryDirectory() as tmp:
        rig = LiveRig(Path(tmp))
        source = CameraSource(camera_index=args.camera, mirrored=args.mirrored)
        try:
            for step in rig.start():
                print(f"start: {step}")
            started = time.monotonic()
            last_mono = rig.clock.mono_ms()
            for camera_frame in source.frames():
                frame = camera_frame.message
                rig.sync()
                rig.status()
                now = rig.clock.unix_ms()
                gesture = gestures.update(frame, now)
                rig.publisher.observe_frame(frame)
                if gesture is not None:
                    rig.publisher.observe_gesture(gesture)
                before = len(rig.results)
                request, message = controller.tick(rig.publisher, gesture, frame)
                result = rig.results[-1] if len(rig.results) > before else None
                mono = rig.clock.mono_ms()
                dt, last_mono = mono - last_mono, mono
                age = now - frame["timestamp_ms"]
                ticks += 1
                dts.append(dt)
                ages.append(age)
                results[result or "nothing sent"] += 1
                if message is not None and message["stop"] is False:
                    motion_sent += 1
                elif message is None or message["stop"]:
                    decisions[rig.publisher.last_decision] += 1
                print(line(dt, age, frame, gesture, controller, request, message, result,
                           rig.publisher.last_decision), flush=True)
                if gestures.last_errors:
                    print(f"    ml window assembler rejected the frame: {list(gestures.last_errors)}")
                if args.seconds is not None and time.monotonic() - started >= args.seconds:
                    break
        except KeyboardInterrupt:
            pass
        finally:
            source.close()
            print("\n== summary")
            print(f"ticks={ticks} motion datagrams sent={motion_sent}")
            if dts:
                print(f"tick gap ms: median={statistics.median(dts):.0f} max={max(dts)} "
                      f"over 250 (receiver watchdog / challenge freshness)={sum(d > 250 for d in dts)}")
                print(f"frame age at tick ms: median={statistics.median(ages):.0f} max={max(ages)} "
                      f"over 250 (perception staleness)={sum(a > 250 for a in ages)}")
            print("receiver decisions: " + ", ".join(f"{k}={v}" for k, v in results.most_common()))
            print("publisher stop reasons: " + ", ".join(f"{k}={v}" for k, v in decisions.most_common()))
            snap = rig.receiver.snapshot()
            print(f"receiver end state: armed={rig.receiver.armed} fault={snap.get('fault_latch')} "
                  f"estop={snap.get('software_estop_latch')} torque={rig.receiver.motors_have_torque()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
