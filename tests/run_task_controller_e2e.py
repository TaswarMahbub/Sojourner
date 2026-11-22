"""Scripted end-to-end run: task controller -> Publisher.tick() -> MotorReceiver.

Run from the repository root:

    python tests/run_task_controller_e2e.py

Each line shows the tick's cues, the resulting task, the MotionRequest the
controller handed to the publisher, the datagram the publisher actually sent
and the receiver's decision. Segments: Tier 2 Go that keeps running with no
further cues, Faster, Left, Stop mid-motion, a cue label refused by both
layers, signal loss, and a Tier 3 scan cycle with a select during the blank
slot, a select of Go, and a select while moving (Stop).

The receiver is the contract oracle in tests/motor_receiver.py, not
firmware. This is not a hardware test.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.cues import PROPOSED_CUE_MODEL_VERSION  # noqa: E402
from control.task_controller import GO_LOCKOUT_MS, TaskController  # noqa: E402
from tests.control_rig import Rig, gesture as rig_gesture, tracking_frame  # noqa: E402

REST = 0.30
PRESS = 0.60
TICK_MS = 50


def binding(channel: str, role: str) -> dict:
    return {
        "channel": channel,
        "role": role,
        "direction": 1,
        "rest_mean": REST,
        "threshold": {"active": REST + 0.15},
        "deadband": {"lower": REST - 0.05, "upper": REST + 0.05},
        "usable_range": 0.50,
    }


def profile(tier: int, bindings: list[dict], stop: str) -> dict:
    return {
        "record_type": "calibration_profile",
        "accepted": True,
        "session_quality": "ok",
        "proposed_tier": {"tier": tier, "mode": {2: "discrete", 3: "scanning"}[tier]},
        "stop_binding": {"channel": stop},
        "bindings": bindings,
    }


TIER2_MAP = {"browInnerUp": "go", "mouthSmileRight": "faster", "browOuterUpLeft": "left", "mouthSmileLeft": "cancel"}
TIER2 = profile(2, [binding("jawOpen", "discrete")] + [binding(c, "discrete") for c in TIER2_MAP], "jawOpen")
TIER3 = profile(3, [binding("jawOpen", "scan_switch")], "jawOpen")
CHANNELS = ("jawOpen", *TIER2_MAP)


class Session:
    def __init__(self, state_dir: Path, controller: TaskController, label: str, tick_ms: int = TICK_MS):
        self.tick_ms = tick_ms
        self.rig = Rig(state_dir, boot_nonce=11 if label == "tier3" else 7)
        self.rig.ready_receiver()
        self.rig.handshake()
        self.rig.arm()
        self.controller = controller
        self.label = label

    def tick(self, note: str = "", *, label="neutral", model_version="none", score=0.95, frame_ok=True, **values):
        rig = self.rig
        rig.advance(self.tick_ms)
        rig.status()
        now = rig.clock.unix
        frame = tracking_frame(now)
        for channel in CHANNELS:
            frame["features"][channel] = REST
        frame["features"].update(values)
        if not frame_ok:
            frame = {"schema_version": "4", "timestamp_ms": now, "tracking_state": "lost", "features": {},
                     "head_pose": None, "confidence": None}
        gesture = rig_gesture(now, label)
        gesture["model_version"] = model_version
        gesture["confidence"] = score
        rig.publisher.observe_frame(frame)
        rig.publisher.observe_gesture(gesture)
        scan = None
        if self.label == "tier3":
            # While moving, any select is Stop, so the display says so.
            scan = "any=stop" if self.controller.task.kind != "hold" else (self.controller.reader.highlighted(now) or "blank")
        before = len(rig.results)
        request, message = self.controller.tick(rig.publisher, gesture, frame)
        result = rig.results[-1] if len(rig.results) > before else "nothing sent"
        print(line(note, self.controller, request, message, result, scan, rig.publisher.last_decision))
        return request, message, result

    def wait(self, note: str, ms: int):
        """Rest ticks printed as one line: lets a Go lockout run out."""
        rig = self.rig
        for _ in range(ms // self.tick_ms - 1):
            rig.advance(self.tick_ms)
            rig.status()
            now = rig.clock.unix
            frame = tracking_frame(now)
            for channel in CHANNELS:
                frame["features"][channel] = REST
            gesture = rig_gesture(now, "neutral")
            gesture["confidence"] = 0.95
            rig.publisher.observe_frame(frame)
            rig.publisher.observe_gesture(gesture)
            self.controller.tick(rig.publisher, gesture, frame)
        return self.tick(f"{note} ({ms} ms)")

    def press(self, note: str, **values):
        """A press is held for two frames: control/tiers.py fires on a sustained crossing."""
        self.tick(f"{note} (frame 1)", **values)
        return self.tick(f"{note} (frame 2)", **values)


def line(note, controller, request, message, result, scan, decision) -> str:
    cue_text = ",".join(f"{c.kind}({c.source})" for c in controller.last_cues) or "-"
    if controller.last_suppressed:
        cue_text += " [locked out: " + ",".join(c.kind for c in controller.last_suppressed) + "]"
    task = controller.task
    task_text = f"{task.kind} v={task.speed_mps:.2f} turn={task.turn_rad:+.3f}" + (
        f" proposal={task.proposal}" if task.proposal else ""
    )
    req = "None" if request is None else f"{request.task_state} {request.linear_mps:.6f} {request.angular_rad_s:+.6f}"
    sent = (
        "-" if message is None
        else f"{message['task_state']} stop={str(message['stop']).lower()} "
             f"{message['linear_mps']:.6f} {message['angular_rad_s']:+.6f}"
    )
    scan = "" if scan is None else f"[{scan}]"
    cap = "-" if controller.governor.factor is None else f"{controller.governor.factor:.2f}"
    why = f"  ({decision})" if message is not None and message["stop"] else ""
    return (f"  {note:<20}{scan:<11} cues={cue_text:<24} task={task_text:<28} cap={cap:<5} "
            f"request={req:<27} sent={sent:<33} receiver={result}{why}")


def banner(text: str) -> None:
    print(f"\n== {text}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        banner("Tier 2 (discrete): receiver ready, session trusted, armed")
        s = Session(Path(tmp) / "t2", TaskController(TIER2, TIER2_MAP), "tier2")
        s.tick("rest")
        s.press("press Go", browInnerUp=PRESS)
        for i in range(6):
            s.tick(f"no cue {i + 1}")
        s.press("press Faster", mouthSmileRight=PRESS)
        s.tick("no cue")
        s.press("press Left", browOuterUpLeft=PRESS)
        for i in range(12):
            s.tick(f"no cue {i + 1}")

        banner("Stop mid-motion, with Cancel and Go in the same tick")
        s.press("Stop+Cancel+Go", jawOpen=PRESS, mouthSmileLeft=PRESS, browInnerUp=PRESS)
        for i in range(3):
            s.tick(f"no cue {i + 1}")
        snap = s.rig.receiver.snapshot()
        print(f"    receiver: torque={s.rig.receiver.motors_have_torque()} lin={snap['linear_mps']:.6f}")

        banner("A classifier cue label: refused by the controller and by the publisher")
        s.tick("rest")
        s.press("press Go", browInnerUp=PRESS)
        s.tick("no cue")
        s.tick("label go (proposed)", label="go", model_version=PROPOSED_CUE_MODEL_VERSION)
        s.tick("no cue")
        print("    the cap restarts from 0 after any loss and rises at 1.0 per second (control/confidence.py)")

        print(f"    the refusal was an uncued stop: Go is locked out for {GO_LOCKOUT_MS} ms (control/task_controller.py)")
        s.press("press Go (locked out)", browInnerUp=PRESS)
        s.wait("rest", GO_LOCKOUT_MS)

        banner("Signal loss mid-motion: face lost, then back")
        s.press("press Go", browInnerUp=PRESS)
        s.tick("no cue")
        s.tick("face lost", frame_ok=False)
        s.tick("face back")
        s.tick("face back")

        banner("Tier 3 (scanning, one switch, dwell 300 ms, 100 ms ticks): fresh receiver boot")
        c3 = TaskController(TIER3, dwell_ms=300)
        s3 = Session(Path(tmp) / "t3", c3, "tier3", tick_ms=100)
        s3.tick("rest")
        # A press takes two frames; the option chosen is the one shown when the
        # second frame completes the crossing, so both frames must fall in the slot.
        s3.press("select (blank slot)", jawOpen=PRESS)
        print("    one full cycle: blank, then each option for one dwell")
        for i in range(27):
            s3.tick("scan")

        def shown(ahead: int):
            return c3.reader.highlighted(s3.rig.clock.unix + ahead * s3.tick_ms)

        for choice in ("left", "go"):
            while not (shown(1) == choice and shown(2) == choice):
                s3.tick("scan")
            s3.press(f"select {choice}", jawOpen=PRESS)
        for i in range(8):
            s3.tick(f"no cue {i + 1}")
        s3.press("select while moving", jawOpen=PRESS)
        s3.tick("no cue")
        snap = s3.rig.receiver.snapshot()
        print(f"    receiver: torque={s3.rig.receiver.motors_have_torque()} lin={snap['linear_mps']:.6f}")

        banner("Receiver decisions")
        for name, session in (("tier 2", s), ("tier 3", s3)):
            results = session.rig.results
            motion = [m for m in session.rig.sent if m["stop"] is False]
            print(f"    {name}: {len(results)} datagrams, {results.count('accepted_motion')} accepted motion, "
                  f"{len(motion)} motion sent, max |lin|={max((abs(m['linear_mps']) for m in motion), default=0):.2f} "
                  f"max |ang|={max((abs(m['angular_rad_s']) for m in motion), default=0):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
