"""Failure drills: one command each, a transcript, a one-line verdict, and asserted outcomes.

Run from the repository root:

    python tests/drills.py a        camera disconnected mid-run
    python tests/drills.py b        face leaves the frame
    python tests/drills.py c        light drops below the level where tracking survives
    python tests/drills.py d        a deliberate wrong cue (tilt on the unbound roll axis)
    python tests/drills.py e        operator estop, then rearm and recovery
    python tests/drills.py f        the laptop process restarts mid-session
    python tests/drills.py all

The exit status is 0 only when every check of every drill run passed.
tests/test_drills.py runs the same functions under pytest.

Every drill drives the unmodified control/ code (TaskController, Publisher,
StatusGate with its real persisted file) through tests/session_harness.py
against the MotorReceiver contract oracle, on a simulated clock so the
transcript is identical on every run. What each drill simulates is printed in
its header. None of this is a hardware test: there is no ESP32, no radio and
no motor driver here.

The profile is the synthetic doorway profile (tests/run_doorway_e2e.py), the
same shape as a probe-report profile, so the drills run anywhere, including
CI. The cue map is control/cue_map.py.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from control.auth import load_credentials  # noqa: E402
from control.publisher import MotionRequest, Publisher, is_actionable_frame  # noqa: E402
from control.reset import GATE_FILE, SESSION_FILE  # noqa: E402
from control.session import SessionAllocator  # noqa: E402
from control.status_gate import PersistedStateError, StatusGate  # noqa: E402
from control.task import HOLD, SPEED_DEFAULT_MPS, SPEED_MIN_MPS, TURN_RATE_RAD_S  # noqa: E402
from control.task_controller import GO_LOCKOUT_MS  # noqa: E402
from perception.frame_builder import make_feature_message  # noqa: E402
from tests.control_rig import TEST_ENV, tracking_frame  # noqa: E402
from tests.run_doorway_e2e import posed_frame, synthetic_profile  # noqa: E402
from tests.session_harness import TICK_MS, HarnessRig, Pipeline, SimClock, start  # noqa: E402


@dataclass
class DrillResult:
    ident: str
    title: str
    lines: list[str] = field(default_factory=list)
    checks: list[tuple[str, bool]] = field(default_factory=list)
    summary: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok in self.checks)

    @property
    def verdict(self) -> str:
        failed = sum(not ok for _, ok in self.checks)
        status = "PASS" if self.passed else f"FAIL ({failed} of {len(self.checks)} checks failed)"
        return f"VERDICT drill {self.ident} ({self.title}): {status}. {self.summary}"


class Drill:
    """One drill's rig, pipeline, transcript and checks."""

    def __init__(self, result: DrillResult, out, state_dir: Path, *, clock=None, receiver=None,
                 provisioned=True, ready=True):
        self.r = result
        self._out = out
        self.state_dir = state_dir
        self.clock = clock or SimClock()
        self.rig = HarnessRig(state_dir, self.clock, provisioned=provisioned, receiver=receiver)
        if ready:
            start(self.rig)
        self.p = Pipeline(self.rig, synthetic_profile(), label=f"DRILL {result.ident}", out=self.say, every_tick=False)
        b = {x["channel"]: x["rest_mean"] for x in synthetic_profile()["bindings"]}
        self.rest = {"jaw": b["jawOpen"], "yaw": b["yaw_deg"], "pitch": b["pitch_deg"], "roll": 0.0}
        self.jaw_open = self.rest["jaw"] + 0.6

    def say(self, line: str) -> None:
        self.r.lines.append(line)
        self._out(line)

    def note(self, text: str) -> None:
        self.p.note(text)

    def check(self, description: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.r.checks.append((description, ok))
        self.say(f"DRILL {self.r.ident} |   [{'ok' if ok else 'FAIL'}] {description}" + (f"  ({detail})" if detail else ""))
        return ok

    def frame(self, kind="posed", **values) -> dict | None:
        now = self.clock.unix
        if kind is None:
            return None
        if callable(kind):
            return kind(now)
        v = {k: values.get(k, self.rest[k]) for k in ("jaw", "yaw", "pitch", "roll")}
        if v["jaw"] is None:
            v["jaw"] = self.rest["jaw"]
        return posed_frame(now, v["jaw"], v["yaw"], v["pitch"], v["roll"])

    def step(self, note="", kind="posed", *, estop=False, rearm=False, **values):
        self.rig.advance(TICK_MS)
        return self.p.tick(self.frame(kind, **values), note=note, estop=estop, rearm=rearm)

    def steps(self, n: int, note="", kind="posed", **values) -> list:
        return [self.step(note, kind, **values) for _ in range(n)]

    def go(self, note="jaw open -> Go") -> list:
        out = self.steps(2, note, jaw=self.jaw_open)
        out.append(self.step("jaw closed"))
        return out

    def stop_press(self, note="jaw open -> Stop") -> list:
        out = self.steps(2, note, jaw=self.jaw_open)
        out.append(self.step("jaw closed"))
        return out

    def torque(self) -> bool:
        return self.rig.receiver.motors_have_torque()

    def moving_ok(self, n: int = 6) -> bool:
        ds = self.steps(n, "travel")
        return all(d is not None and d.result == "accepted_motion" for d in ds)


def results_of(deliveries) -> list[str]:
    return [d.result for d in deliveries if d is not None]


def header(d: Drill, simulated: str) -> None:
    d.say(f"DRILL {d.r.ident} | ===== drill {d.r.ident}: {d.r.title}")
    d.say(f"DRILL {d.r.ident} | simulated: {simulated}")
    d.say(f"DRILL {d.r.ident} | receiver: MotorReceiver contract oracle in this process; simulated clock, {TICK_MS} ms ticks")


# ----- a. camera disconnected ----------------------------------------------------------


def drill_a(out=print) -> DrillResult:
    r = DrillResult("a", "camera disconnected mid-run")
    with tempfile.TemporaryDirectory() as tmp:
        d = Drill(r, out, Path(tmp))
        header(d, "the camera thread raises CameraError mid-run, as perception/mediapipe_source.py does when "
                  "the device stops delivering; after that no frame arrives and the loop keeps ticking")
        d.steps(2, "rest")
        d.go()
        d.check("the chair is moving before the unplug", d.moving_ok() and d.torque())
        last_capture = d.p.frame["timestamp_ms"]
        d.p.camera_error("simulated: The camera stopped delivering frames.")
        after = [d.step("camera gone", None) for _ in range(10)]
        motion_ages = [d_.message["issued_at_ms"] - last_capture for d_ in after if d_ and d_.result == "accepted_motion"]
        first_stop = next(d_ for d_ in after if d_ and d_.message["stop"])
        stop_age = first_stop.message["issued_at_ms"] - last_capture
        d.check("motion continued only while the newest frame was at most 250 ms old",
                all(a <= 250 for a in motion_ages), f"motion at ages {motion_ages} ms")
        d.check("a stop datagram went out on the first tick the frame was older than 250 ms",
                250 < stop_age <= 250 + TICK_MS, f"stop at age {stop_age} ms")
        d.check("the receiver turned torque off on that stop", first_stop.torque_before and not first_stop.torque_after)
        d.check("the task is Hold and Go is locked out",
                d.p.controller.task.kind == HOLD and d.p.controller.locked(d.clock.unix))
        d.check("no motion datagram after the stop", "accepted_motion" not in results_of(after[after.index(first_stop):]))
        d.note("camera reconnected: frames resume at rest; a jaw press inside the lockout")
        back = d.steps(4, "camera back, rest") + d.steps(2, "jaw open (inside lockout)", jaw=d.jaw_open) + [d.step("jaw closed")]
        d.check("after reconnecting, nothing moves without a new Go (and Go is dropped during the lockout)",
                "accepted_motion" not in results_of(back) and d.p.controller.task.kind == HOLD)
        d.steps(GO_LOCKOUT_MS // TICK_MS, "wait out the lockout")
        d.go("jaw open -> Go (after lockout)")
        d.check("a fresh Go after the lockout moves again", d.moving_ok(3))
        d.check("zero watchdog trips: the laptop kept sending stops", not d.rig.watchdog_trips)
        d.check("the signal-loss stop was measured", len(d.p.log.of("signal-loss stop")) == 1)
        for line in d.p.summary():
            d.say(f"DRILL a | {line}")
        r.summary = (f"Stop sent {stop_age} ms after the last frame (staleness limit 250 ms), torque off, "
                     f"Hold with Go locked out; recovery needed a new Go.")
    return r


# ----- b. face leaves the frame --------------------------------------------------------


def no_face(ts: int) -> dict:
    """What the detector reports with no face in view (perception/frame_builder.py, face_count 0)."""
    return make_feature_message(ts, 0, None, None)


def drill_b(out=print) -> DrillResult:
    r = DrillResult("b", "face leaves the frame")
    with tempfile.TemporaryDirectory() as tmp:
        d = Drill(r, out, Path(tmp))
        header(d, "the detector returns no face; frames are built by perception/frame_builder.make_feature_message "
                  "with face_count 0, the exact message the live camera publishes")
        d.steps(2, "rest")
        d.go()
        d.check("the chair is moving before the face leaves", d.moving_ok() and d.torque())
        first = d.step("face leaves", no_face)
        d.check("the first no-face frame is not actionable", not is_actionable_frame(d.p.frame))
        d.check("a stop went out on that same tick and torque went off",
                first is not None and first.message["stop"] and first.torque_before and not first.torque_after,
                first.result if first else "nothing sent")
        gone = d.steps(6, "face away", no_face)
        d.check("stops continue while the face is away", all(x.message["stop"] for x in gone))
        back = d.steps(6, "face back, rest")
        d.check("the face returning does not move the chair", "accepted_motion" not in results_of(back))
        d.check("Go is locked out after the uncued stop", d.p.controller.locked(d.clock.unix))
        d.steps(GO_LOCKOUT_MS // TICK_MS, "wait out the lockout")
        d.go("jaw open -> Go (after lockout)")
        d.check("a fresh Go after the lockout moves again", d.moving_ok(3))
        d.check("zero watchdog trips", not d.rig.watchdog_trips)
        loss = d.p.log.of("signal-loss stop")
        d.check("the signal-loss stop was measured", len(loss) == 1)
        for line in d.p.summary():
            d.say(f"DRILL b | {line}")
        ms = f"{loss[0].total:.0f} ms" if loss else "unmeasured"
        r.summary = f"Stop on the first no-face frame, last actionable frame to torque off {ms} (simulated clock)."
    return r


# ----- c. light drops ------------------------------------------------------------------


def dim_partial(ts: int) -> dict:
    """One face found but the blendshapes incomplete: the 'partial' state, not actionable."""
    base = tracking_frame(ts)
    features = dict(list(base["features"].items())[: len(base["features"]) // 2])
    return make_feature_message(ts, 1, features, base["head_pose"]["matrix"])


def drill_c(out=print) -> DrillResult:
    r = DrillResult("c", "light drops below the level where tracking survives")
    with tempfile.TemporaryDirectory() as tmp:
        d = Drill(r, out, Path(tmp))
        header(d, "the light falls; the detector first loses part of the face (a 'partial' frame: one face, "
                  "incomplete blendshapes) and then the face (face_count 0). The brightness values below are "
                  "illustrative: this drill does NOT measure the level at which the detector fails. A live run "
                  "(tests/run_live_e2e.py) prints the measured brightness at the moment tracking drops")
        d.steps(2, "rest")
        d.go()
        d.check("the chair is moving in good light", d.moving_ok(4) and d.torque())
        for b in (110, 80, 55):
            d.step(f"light falling, brightness ~{b}")
        partial = d.step("brightness ~35: partial", dim_partial)
        d.check("the first partial frame is not actionable", d.p.frame["tracking_state"] == "partial"
                and not is_actionable_frame(d.p.frame))
        d.check("the partial frame stopped the chair on that tick",
                partial is not None and partial.message["stop"] and not partial.torque_after)
        dark = d.steps(4, "brightness ~15: no face", no_face)
        d.check("stops continue in the dark", all(x.message["stop"] for x in dark))
        lit = d.steps(4, "light restored, rest")
        d.check("restoring the light does not move the chair", "accepted_motion" not in results_of(lit)
                and d.p.controller.task.kind == HOLD)
        d.check("zero watchdog trips", not d.rig.watchdog_trips)
        for line in d.p.summary():
            d.say(f"DRILL c | {line}")
        r.summary = "The first non-tracking frame stopped the chair; light returning did not restart it."
    return r


# ----- d. deliberate wrong cue ---------------------------------------------------------


def drill_d(out=print) -> DrillResult:
    r = DrillResult("d", "a deliberate wrong cue: head tilt, roll unbound")
    with tempfile.TemporaryDirectory() as tmp:
        d = Drill(r, out, Path(tmp))
        header(d, "while moving, (1) a pure roll of 15 degrees, the unbound axis; (2) a realistic tilt, where the "
                  "head also turns and drops, as measured in probe 20261122T073621Z (control/cue_map.py, "
                  "residual risk 1); then the user's Stop")
        d.steps(2, "rest")
        d.go()
        d.check("moving straight at the default speed", d.moving_ok(4)
                and d.p.controller.task.speed_mps == SPEED_DEFAULT_MPS)
        pure = d.steps(3, "pure roll +15 (unbound)", roll=15.0)
        d.step("head back")
        d.check("a pure roll fires no cue and changes nothing",
                all(x.message["angular_rad_s"] == 0.0 and x.result == "accepted_motion" for x in pure)
                and d.p.controller.task.speed_mps == SPEED_DEFAULT_MPS and d.p.controller.task.turn_rad == 0.0)
        tilt = d.steps(2, "realistic tilt right", roll=15.0, yaw=d.rest["yaw"] - 30.0, pitch=d.rest["pitch"] - 15.0)
        fired = sorted({c.kind for c in d.p.controller.last_cues})
        d.step("head back")
        d.check("the realistic tilt couples into yaw and pitch: it fires Right and Slower",
                fired == ["right", "slower"], f"cues {fired}")
        run = d.steps(14, "travel after the wrong cue")
        angular = [x.message["angular_rad_s"] for x in tilt + run if x.result == "accepted_motion"]
        d.check("its effect is bounded: one turn increment at no more than 0.5 rad/s, then straight again",
                max(abs(a) for a in angular) <= TURN_RATE_RAD_S and angular[-1] == 0.0
                and d.p.controller.task.turn_rad == 0.0, f"max |ang| {max(abs(a) for a in angular):.2f}")
        d.check("the speed setting stepped down once, to the minimum",
                d.p.controller.task.speed_mps == SPEED_MIN_MPS, f"v={d.p.controller.task.speed_mps:.2f}")
        stop = d.stop_press()
        d.check("the user's Stop beat it: stop datagram and torque off on the cue tick",
                stop[1].message["stop"] and stop[1].torque_before and not stop[1].torque_after)
        d.check("zero watchdog trips", not d.rig.watchdog_trips)
        for line in d.p.summary():
            d.say(f"DRILL d | {line}")
        r.summary = ("Unbound roll alone fired nothing; a real tilt fired a bounded Right + Slower that the "
                     "system executed (no unbounded motion), and Stop still won on its tick.")
    return r


# ----- e. operator estop ---------------------------------------------------------------


def drill_e(out=print) -> DrillResult:
    r = DrillResult("e", "operator estop, then rearm and recovery")
    with tempfile.TemporaryDirectory() as tmp:
        d = Drill(r, out, Path(tmp))
        header(d, "the operator presses the laptop estop (a local boolean, never from the network) while moving, "
                  "then rearms")
        d.steps(2, "rest")
        d.go()
        d.check("moving before the estop", d.moving_ok(4) and d.torque())
        es = d.step("OPERATOR ESTOP", estop=True)
        d.check("an estop datagram was accepted and torque went off on that tick",
                es.message["task_state"] == "estop" and es.result == "accepted_stop" and not es.torque_after,
                es.result)
        d.check("the receiver latched the software estop", d.rig.receiver.software_estop_latch is True)
        d.check("the controller went to Hold with Go locked out",
                d.p.controller.task.kind == HOLD and d.p.controller.locked(d.clock.unix))
        d.steps(GO_LOCKOUT_MS // TICK_MS, "wait out the lockout")
        latched = d.go("jaw open -> Go while latched") + d.steps(3, "latched")
        d.check("while latched, a Go is refused by the status gate: no motion reaches the receiver",
                "accepted_motion" not in results_of(latched))
        d.check("the refusal itself is an uncued stop: Hold and a new lockout",
                d.p.controller.task.kind == HOLD and d.p.controller.locked(d.clock.unix))
        rearm = d.step("OPERATOR REARM", rearm=True)
        d.check("the operator rearm was accepted and cleared the latch",
                rearm.result == "accepted_rearm" and d.rig.receiver.software_estop_latch is False, rearm.result)
        d.check("rearm alone does not move the chair", not d.torque() and d.p.controller.task.kind == HOLD)
        d.steps(GO_LOCKOUT_MS // TICK_MS, "wait out the lockout")
        d.go("jaw open -> Go after rearm")
        d.check("after rearm and a fresh Go, motion resumes", d.moving_ok(3))
        d.check("zero watchdog trips", not d.rig.watchdog_trips)
        for line in d.p.summary():
            d.say(f"DRILL e | {line}")
        r.summary = "Estop stopped the chair on its tick; nothing moved while latched; rearm plus a new Go recovered."
    return r


# ----- f. laptop process restart -------------------------------------------------------


def drill_f(out=print) -> DrillResult:
    r = DrillResult("f", "the laptop process restarts mid-session")
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        clock = SimClock()
        gate_path = state / GATE_FILE
        d1 = Drill(r, out, state, clock=clock)
        say, check = d1.say, d1.check
        header(d1, "process 1 is killed while the chair moves (no shutdown code runs); process 2 starts from the "
                   "same state directory and the real persisted status_gate.json; then the file is corrupted and "
                   "process 3 starts. The receiver boot continues throughout")
        d1.steps(2, "rest")
        d1.go()
        check("process 1: moving", d1.moving_ok(4) and d1.torque())
        marks1 = d1.rig.publisher.gate.export_state()
        challenges1 = [m["status_challenge"] for m in d1.rig.sent]
        # A genuine, signed status the receiver emitted while process 1 was alive, kept by anyone on the link.
        old_status = json.dumps(d1.rig.receiver.status()).encode("utf-8")
        say(f"DRILL f | on disk at the kill: {gate_path.read_text(encoding='utf-8')}")
        d1.note("PROCESS 1 KILLED (no shutdown, no final stop). The receiver keeps running.")
        receiver = d1.rig.receiver
        d1.rig.advance(300)
        check("with the laptop silent, the receiver's watchdog cut torque within 300 ms",
              not receiver.motors_have_torque() and receiver.fault_latch is True)

        say("DRILL f | ----- process 2 starts from the persisted state (its transcript times restart at 0)")
        d2 = Drill(r, out, state, clock=clock, receiver=receiver, provisioned=False, ready=False)
        gate = d2.rig.publisher.gate
        pub = d2.rig.publisher
        say(f"DRILL f | restored marks: challenge_high_water={gate.challenge_high_water} "
            f"sequences={gate.sequences} boot_nonce={gate.boot_nonce}")
        check("high-water marks survived the kill exactly",
              gate.export_state() == marks1, f"high water {gate.challenge_high_water}")
        check("no challenge and no status were restored: nothing is authorized",
              gate.outstanding_challenge is None and gate.current_status(clock.mono) is None)
        replay = pub.observe_status(old_status)
        check("replaying process 1's last signed status is rejected", replay == "stale_challenge", replay)
        fresh = d2.rig.status()
        check("even the receiver's genuine current status is rejected until a new challenge is echoed",
              fresh == "stale_challenge", fresh)
        clock.advance(10)
        d2.rig.sync()
        msg = pub.tick(motion=MotionRequest("shared", 0.10, 0.0))
        check("a motion request on the first tick does not move: it opens a session instead",
              msg is not None and msg["stop"] is True and d2.rig.results[-1] == "accepted_start",
              f"{pub.last_decision}; receiver {d2.rig.results[-1] if d2.rig.results else '-'}")
        new_challenge = msg["status_challenge"]
        check("the new challenge is above the persisted high-water mark (no id reuse across the restart)",
              new_challenge > marks1["challenge_high_water"] and new_challenge > max(challenges1),
              f"{new_challenge} > {marks1['challenge_high_water']}")
        clock.advance(10)
        echoed = d2.rig.status()
        check("the receiver echoes the new challenge and the gate accepts it; the session is trusted",
              echoed == "accepted" and pub.session.trusted, echoed)
        clock.advance(10)
        d2.rig.sync()
        # Fresh, actionable perception, so the only thing left to refuse motion is the status gate.
        d2.rig.fresh_perception()
        refused = pub.tick(motion=MotionRequest("shared", 0.10, 0.0))
        check("motion is still refused: the receiver is disarmed with its watchdog fault latched",
              refused["stop"] is True and pub.last_decision == "idle: status gate refuses motion", pub.last_decision)
        clock.advance(10)
        d2.rig.status()
        clock.advance(10)
        d2.rig.sync()
        rearm = pub.tick(operator_rearm=True)
        check("only an operator rearm clears it", rearm is not None and d2.rig.results[-1] == "accepted_rearm",
              d2.rig.results[-1])
        d2.steps(2, "rest")
        d2.go("jaw open -> Go (process 2)")
        check("process 2: a fresh Go moves the chair", d2.moving_ok(3))
        check("every challenge process 2 sent is above process 1's high-water mark",
              all(m["status_challenge"] > marks1["challenge_high_water"] for m in d2.rig.sent))
        check("process 2 saw no further watchdog trip", not d2.rig.watchdog_trips)
        marks2 = gate.export_state()

        say("DRILL f | ----- in-process rollback: process 2 is handed process 1's older snapshot")
        try:
            gate.restore_from_text(json.dumps(marks1))
            rolled = "accepted"
        except PersistedStateError as exc:
            rolled = str(exc)
        check("a snapshot that lowers a mark is refused and locks the gate",
              rolled == "snapshot lowers challenge_high_water" and gate.locked, rolled)

        say("DRILL f | ----- process 2 killed; status_gate.json is corrupted before process 3 starts")
        clock.advance(300)
        receiver.set_monotonic(clock.mono)
        check("the receiver's watchdog cut torque again", not receiver.motors_have_torque())
        creds = load_credentials(TEST_ENV)
        good = json.dumps(marks2)
        corruptions = {
            "truncated write": good[: len(good) // 2],
            "not JSON": "\x00\x00garbage",
            "empty file": "",
            "extra key": json.dumps({**marks2, "note": 1}),
            "duplicate key": good[:-1] + ',"version":1}',
            "high water as a float": json.dumps({**marks2, "challenge_high_water": float(marks2["challenge_high_water"])}),
            "another controller's state": json.dumps({**marks2, "controller_id": "other-laptop"}),
            "file missing": None,
        }
        for name, text in corruptions.items():
            if text is None:
                gate_path.unlink(missing_ok=True)
            else:
                gate_path.write_text(text, encoding="utf-8")
            g3 = StatusGate(creds.key, creds.controller_id, gate_path)
            try:
                g3.restore_from_file()
                outcome = "restored"
            except PersistedStateError as exc:
                outcome = f"refused: {exc}"
            check(f"corrupt state ({name}) fails closed: gate locked", g3.locked and outcome != "restored", outcome)

        gate_path.write_text(corruptions["truncated write"], encoding="utf-8")
        g3 = StatusGate(creds.key, creds.controller_id, gate_path)
        try:
            g3.restore_from_file()
        except PersistedStateError:
            pass
        allocator = SessionAllocator(creds.controller_id, state / SESSION_FILE)
        allocator.restore_from_file()
        sent3: list[bytes] = []
        p3 = Publisher(creds, g3, allocator, sent3.append, clock)
        status3 = p3.observe_status(json.dumps(receiver.status()).encode("utf-8"))
        motion3 = p3.tick(motion=MotionRequest("shared", 0.10, 0.0))
        decision_motion = p3.last_decision
        estop3 = p3.tick(operator_estop=True)
        decision_estop = p3.last_decision
        say(f"DRILL f | process 3 with a truncated state file: status -> {status3}; motion -> {decision_motion}; "
            f"estop -> {decision_estop}")
        check("process 3 accepts no status, starts no session and sends no motion",
              status3 == "locked" and motion3 is None and not sent3)
        check("process 3 cannot send an estop either: it holds no durable challenge id",
              estop3 is None and not sent3, decision_estop)
        say("DRILL f |   consequence: with a corrupt gate file the laptop goes silent. What stops the chair is the "
            "receiver's 250 ms watchdog and the physical estop, not the laptop.")
        say("DRILL f |   recovery is a deliberate operator action: python -m control.reset with a NEW controller id, "
            "then re-provision the receiver with that id. Not run here.")

        say("DRILL f | ----- LIMIT (reported, not a pass condition): an older but valid file in a FRESH process")
        g4 = StatusGate(creds.key, creds.controller_id, gate_path)
        try:
            g4.restore_from_text(json.dumps(marks1))
            limit = f"accepted, challenge_high_water back to {g4.challenge_high_water} from {marks2['challenge_high_water']}"
        except PersistedStateError as exc:
            limit = f"refused: {exc}"
        say(f"DRILL f |   restoring process 1's snapshot into a new process: {limit}. The rollback check compares "
            "against marks held in memory, and a new process holds none, so a file restored from an old backup "
            "would let challenge ids be issued again. Atomic writes make this unlikely by accident; it is not "
            "detected if it happens.")

        r.summary = (f"Marks survived the kill (high water {marks1['challenge_high_water']}); replayed and "
                     f"current statuses were refused until challenge {new_challenge} was issued and echoed; "
                     f"motion also waited for an operator rearm; {len(corruptions)} kinds of corrupt state all "
                     "locked the gate. Limit noted: an old valid file in a fresh process is not detected.")
    return r


DRILLS = {"a": drill_a, "b": drill_b, "c": drill_c, "d": drill_d, "e": drill_e, "f": drill_f}


def main(argv: list[str]) -> int:
    # control/ logs its refusals; print them in order with the transcript.
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="  log | %(name)s: %(message)s")
    wanted = argv[1:] or ["all"]
    if wanted == ["all"]:
        wanted = list(DRILLS)
    unknown = [w for w in wanted if w not in DRILLS]
    if unknown:
        print(f"unknown drill(s): {', '.join(unknown)}. Choose from {', '.join(DRILLS)} or all.")
        return 2
    results = []
    for ident in wanted:
        result = DRILLS[ident]()
        print(result.verdict)
        print()
        results.append(result)
    if len(results) > 1:
        for result in results:
            print(result.verdict)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
