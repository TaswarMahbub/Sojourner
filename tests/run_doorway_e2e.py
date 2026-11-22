"""Doorway-shaped run with the chosen cue map: jaw to Go, travel, turn, travel, jaw to Stop.

Run from the repository root:

    python tests/run_doorway_e2e.py [PATH_TO_PROBE_REPORT.json]

With a probe report (perception/probe_session.py), the profile is built from
that session's measured rest means and thresholds. Without one, a synthetic
profile of the same shape is used and the output says so. Either way the
cue map is control/cue_map.py.

Every frame is a valid camera message: the head-pose matrix is composed as
docs/contracts.md section 1 "Angles" defines it (R = Ry(yaw) Rx(-pitch)
Rz(roll)) and the published angles are extracted back from it, so the
publisher's actionability check passes on the same frame the controller
reads. Each line shows the channel values, the cues, the task, the
MotionRequest handed to Publisher.tick(), the datagram actually sent and the
MotorReceiver's decision. The receiver is the contract oracle, not firmware.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.cue_map import CUE_MAP  # noqa: E402
from control.publisher import is_actionable_frame  # noqa: E402
from control.task_controller import TaskController  # noqa: E402
from perception import probe_session  # noqa: E402
from control.task_controller import GO_LOCKOUT_MS  # noqa: E402
from tests.control_rig import Rig, gesture as rig_gesture, lost_frame, tracking_frame  # noqa: E402

TICK_MS = 50
CHANNELS = ("jawOpen", "pitch_deg", "yaw_deg")


def synthetic_profile() -> dict:
    """Same shape as a probe-report profile. Synthetic numbers, not measured."""
    def binding(channel, direction, rest, up, down, unit, rng):
        return {"channel": channel, "role": "discrete", "unit": unit, "direction": direction, "rest_mean": rest,
                "threshold": {"lower": rest - down, "upper": rest + up, "active": rest + (up if direction > 0 else -down)},
                "deadband": {"lower": rest - 0.6 * down, "upper": rest + 0.6 * up}, "usable_range": rng}
    return {
        "record_type": "calibration_profile", "accepted": True, "accepted_by": "synthetic", "session_quality": "ok",
        "proposed_tier": {"tier": 2, "mode": "discrete"}, "stop_binding": {"channel": "jawOpen"},
        "bindings": [binding("jawOpen", 1, 0.003, 0.25, 0.25, "score", 0.77),
                     binding("pitch_deg", 1, 10.0, 12.3, 10.7, "deg", 37.0),
                     binding("yaw_deg", -1, 0.4, 16.1, 16.0, "deg", 48.0)],
    }


def rotation(yaw: float, pitch: float, roll: float) -> list[list[float]]:
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
    rx = [[1, 0, 0], [0, math.cos(p), math.sin(p)], [0, -math.sin(p), math.cos(p)]]
    rz = [[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    return mul(mul(ry, rx), rz)


def posed_frame(ts: int, jaw: float, yaw: float, pitch: float, roll: float) -> dict:
    frame = tracking_frame(ts)
    frame["features"]["jawOpen"] = round(min(1.0, max(0.0, jaw)), 6)
    rot = rotation(yaw, pitch, roll)
    translation = [row[3] for row in frame["head_pose"]["matrix"][:3]]
    matrix = [[round(v, 6) for v in rot[i]] + [translation[i]] for i in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    frame["head_pose"] = {
        "matrix": matrix,
        "yaw_deg": round(math.degrees(math.atan2(rot[0][2], rot[2][2])), 6),
        "pitch_deg": round(math.degrees(math.asin(max(-1.0, min(1.0, rot[1][2])))), 6),
        "roll_deg": round(math.degrees(math.atan2(rot[1][0], rot[1][1])), 6),
    }
    if not is_actionable_frame(frame):
        raise AssertionError("built a frame the publisher would not accept")
    return frame


class Doorway:
    def __init__(self, state_dir: Path, profile: dict):
        self.rig = Rig(state_dir)
        self.rig.ready_receiver()
        self.rig.handshake()
        self.rig.arm()
        self.controller = TaskController(profile, CUE_MAP)
        self.rest = {b["channel"]: b["rest_mean"] for b in profile["bindings"]}
        self.rest.setdefault("roll_deg", 0.0)

    def tick(self, note: str, *, jaw=None, yaw=None, pitch=None, roll=None, face=True, quiet=False):
        rig = self.rig
        rig.advance(TICK_MS)
        rig.status()
        now = rig.clock.unix
        values = {"jaw": self.rest["jawOpen"] if jaw is None else jaw,
                  "yaw": self.rest["yaw_deg"] if yaw is None else yaw,
                  "pitch": self.rest["pitch_deg"] if pitch is None else pitch,
                  "roll": self.rest["roll_deg"] if roll is None else roll}
        frame = posed_frame(now, **values) if face else lost_frame(now)
        gesture = rig_gesture(now, "neutral")
        gesture["confidence"] = 0.95
        rig.publisher.observe_frame(frame)
        rig.publisher.observe_gesture(gesture)
        held = self.controller.task.kind
        before = len(rig.results)
        request, message = self.controller.tick(rig.publisher, gesture, frame)
        result = rig.results[-1] if len(rig.results) > before else "nothing sent"
        if quiet:
            return
        cues = ",".join(c.kind for c in self.controller.last_cues) or "-"
        t = self.controller.task
        locked = self.controller.lockout_remaining_ms(now)
        lock = f"lock {locked:>4}" if locked else "         "
        req = "None" if request is None else f"{request.linear_mps:.3f} {request.angular_rad_s:+.3f}"
        sent = f"{message['task_state']} stop={str(message['stop']).lower()} {message['linear_mps']:.3f} {message['angular_rad_s']:+.3f}"
        if face:
            seen = (f"jaw={frame['features']['jawOpen']:.3f} pitch={frame['head_pose']['pitch_deg']:6.1f} "
                    f"yaw={frame['head_pose']['yaw_deg']:6.1f} roll={frame['head_pose']['roll_deg']:5.1f}")
        else:
            seen = f"{'face lost (tracking_state lost)':<46}"
        print(f"  {note:<24} {seen} | was {held:<5} {lock} "
              f"cues={cues:<13} task={t.kind:<4} v={t.speed_mps:.2f} turn={t.turn_rad:+.3f} | request={req:<14} "
              f"sent={sent:<26} receiver={result}")

    def wait(self, note: str, ms: int):
        """Rest ticks, printed as one line."""
        for _ in range(ms // TICK_MS - 1):
            self.tick(note, quiet=True)
        self.tick(f"{note} ({ms} ms)")

    def press(self, note: str, frames: int = 2, **values):
        for i in range(frames):
            self.tick(f"{note} ({i + 1}/{frames})", **values)


def banner(text: str) -> None:
    print(f"\n== {text}")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        profile = probe_session.profile_from_report(report, CHANNELS, "jawOpen", accepted_by="doorway e2e run")
        source = f"probe report {report['session_id']} ({report['threshold_rule']})"
    else:
        profile = synthetic_profile()
        source = "SYNTHETIC profile, not measured"
    with tempfile.TemporaryDirectory() as tmp:
        d = Doorway(Path(tmp), profile)
        jaw_open = d.rest["jawOpen"] + 0.6
        turned_right = d.rest["yaw_deg"] - 30.0
        print(f"profile: {source}")
        print(f"bindings: {d.controller.reader.bindings}")

        banner("ready: receiver armed, task Hold")
        d.tick("rest")
        d.tick("rest")

        banner("jaw -> Go (the task is Hold, so the jaw means Go)")
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")

        banner("travel: no cue, the task keeps running")
        for i in range(8):
            d.tick(f"travel {i + 1}")

        banner("yaw -> turn right through the doorway (one heading increment)")
        d.press("turn head right", yaw=turned_right)
        d.tick("head back to centre")
        for i in range(12):
            d.tick(f"travel {i + 1}")

        banner("jaw -> Stop mid-motion (the task is moving, so the same jaw means Stop)")
        d.press("open jaw", jaw=jaw_open)
        d.tick("jaw still open", jaw=jaw_open)
        d.tick("close jaw")
        d.tick("rest")
        snap = d.rig.receiver.snapshot()
        print(f"    receiver: torque={d.rig.receiver.motors_have_torque()} lin={snap['linear_mps']:.3f}")

        banner("residual risk 1, after a stop the user cued (no lockout): an incidental tilt while held")
        d.press("tilt head right", roll=d.rest["roll_deg"] + 15.0, yaw=d.rest["yaw_deg"] - 30.0,
                pitch=d.rest["pitch_deg"] - 15.0)
        d.tick("head back")
        print("    Hold did not move. The tilt left a pending right increment and a lower speed setting.")
        print("    There is no lockout after a cued stop, so they apply at the next Go:")
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")
        for i in range(3):
            d.tick(f"travel {i + 1}")
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")

        banner(f"closing beat: signal-loss stop, then the Go lockout ({GO_LOCKOUT_MS} ms)")
        d.wait("rest", 500)
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")
        d.tick("travel")
        d.press("tilt head right", roll=d.rest["roll_deg"] + 15.0, yaw=d.rest["yaw_deg"] - 30.0,
                pitch=d.rest["pitch_deg"] - 15.0)
        d.tick("head back")
        d.tick("face lost", face=False)
        d.tick("face lost", face=False)
        d.tick("face back")
        print("    uncued stop: Hold, pending turn and speed reset, Go locked out. A \"stop!\" press now:")
        d.press("open jaw (stop!)", jaw=jaw_open)
        d.tick("close jaw")
        print(f"    the same press after the lockout is Go, straight and at the default speed:")
        d.wait("rest", GO_LOCKOUT_MS)
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")
        d.tick("travel")
        d.press("open jaw", jaw=jaw_open)
        d.tick("close jaw")

        motion = [m for m in d.rig.sent if m["stop"] is False]
        print(f"\n    {len(d.rig.sent)} datagrams, {d.rig.results.count('accepted_motion')} accepted motion, "
              f"max |lin|={max(abs(m['linear_mps']) for m in motion):.2f} "
              f"max |ang|={max(abs(m['angular_rad_s']) for m in motion):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
