"""Go lockout after an uncued stop (control/task_controller.py, GO_LOCKOUT_MS) with the chosen cue map."""

from __future__ import annotations

import pytest

from control.cue_map import CUE_MAP
from control.cues import GO, SLOWER, STOP, THROUGH, CONFIRM, Cue
from control.task import HOLD, SPEED_DEFAULT_MPS
from control.task_controller import GO_LOCKOUT_MS, TaskController
from tests.control_rig import gesture as rig_gesture
from tests.run_doorway_e2e import posed_frame, synthetic_profile

T0 = 1795276800000
TICK = 50
REST = {b["channel"]: b["rest_mean"] for b in synthetic_profile()["bindings"]}
JAW = REST["jawOpen"] + 0.6


class Chair:
    def __init__(self):
        self.c = TaskController(synthetic_profile(), CUE_MAP)
        self.now = T0

    def tick(self, *, jaw=None, yaw=None, pitch=None, roll=0.0, face=True, score=0.95):
        self.now += TICK
        frame = None
        if face:
            frame = posed_frame(self.now, REST["jawOpen"] if jaw is None else jaw,
                                REST["yaw_deg"] if yaw is None else yaw,
                                REST["pitch_deg"] if pitch is None else pitch, roll)
        g = rig_gesture(self.now, "neutral")
        g["confidence"] = score
        return self.c.step(g, frame, self.now)

    def press_jaw(self):
        self.tick(jaw=JAW)
        request = self.tick(jaw=JAW)
        self.tick()
        return request

    def tilt(self):
        """An incidental tilt: roll unbound, yaw and pitch dragged past their thresholds."""
        self.tick(roll=15.0, yaw=REST["yaw_deg"] - 30.0, pitch=REST["pitch_deg"] - 15.0)
        self.tick(roll=15.0, yaw=REST["yaw_deg"] - 30.0, pitch=REST["pitch_deg"] - 15.0)
        self.tick()

    def wait(self, ms):
        for _ in range(ms // TICK):
            assert self.tick() is None or self.c.task.kind != HOLD

    def moving(self):
        self.tick()
        assert self.press_jaw() is not None and self.c.task.kind == "go"
        assert self.tick() is not None


def lose_face(chair):
    assert chair.tick(face=False) is None
    assert chair.c.task.kind == HOLD and chair.c.locked(chair.now)


def test_lockout_constant():
    assert GO_LOCKOUT_MS == 2500


def test_a_jaw_press_inside_the_lockout_produces_no_motion():
    chair = Chair()
    chair.moving()
    lose_face(chair)
    chair.tick()  # face back, still locked
    assert chair.press_jaw() is None and chair.c.task.kind == HOLD
    assert chair.c.last_suppressed == [] and GO not in [c.kind for c in chair.c.last_cues]


def test_the_same_press_after_the_lockout_is_go():
    chair = Chair()
    chair.moving()
    lose_face(chair)
    stopped_at = chair.now
    while chair.now < stopped_at + GO_LOCKOUT_MS:
        assert chair.tick() is None
    assert not chair.c.locked(chair.now)
    assert chair.press_jaw() is not None and chair.c.task.kind == "go"


def test_the_lockout_runs_from_the_last_lost_tick():
    chair = Chair()
    chair.moving()
    for _ in range(40):  # 2 s of lost face
        chair.tick(face=False)
    last_lost = chair.now
    chair.wait(GO_LOCKOUT_MS - 4 * TICK)
    assert chair.c.locked(chair.now)
    assert chair.press_jaw() is None, "the press completes 2400 ms after the LAST lost tick: still locked"
    assert chair.press_jaw() is not None, "the next press, after 2500 ms, is Go"


def test_a_stop_during_the_lockout_is_honoured():
    chair = Chair()
    chair.moving()
    lose_face(chair)
    chair.tick()
    chair.tick(jaw=JAW)
    assert chair.tick(jaw=JAW) is None
    assert chair.c.last_cues == [Cue(STOP, "tier2")], "the jaw means Stop during the lockout, not Go"


def test_a_pending_tilt_turn_does_not_survive_the_lockout():
    chair = Chair()
    chair.tick()
    chair.tilt()
    assert chair.c.task.turn_rad != 0.0 and chair.c.task.speed_mps < SPEED_DEFAULT_MPS
    lose_face(chair)
    assert chair.c.task.turn_rad == 0.0 and chair.c.task.speed_mps == SPEED_DEFAULT_MPS
    chair.tick()
    chair.tilt()  # queued during the lockout
    assert chair.c.task.turn_rad != 0.0
    chair.wait(GO_LOCKOUT_MS)
    assert not chair.c.locked(chair.now)
    request = chair.press_jaw()
    assert request is not None and request.angular_rad_s == 0.0, "the Go after the lockout runs straight"
    assert request.linear_mps == SPEED_DEFAULT_MPS


def test_low_confidence_starts_the_lockout():
    chair = Chair()
    chair.moving()
    assert chair.tick(score=0.2) is None and chair.c.locked(chair.now)
    chair.tick()
    assert chair.press_jaw() is None


def test_a_publisher_refusal_starts_the_lockout():
    chair = Chair()
    chair.moving()
    request = chair.tick()
    chair.c.note_published(request, {"stop": True, "task_state": "idle", "linear_mps": 0.0, "angular_rad_s": 0.0},
                           "idle: status gate refuses motion")
    assert chair.c.task.kind == HOLD and chair.c.locked(chair.now)
    assert chair.press_jaw() is None


def test_a_user_cued_stop_starts_no_lockout():
    chair = Chair()
    chair.moving()
    assert chair.press_jaw() is None and chair.c.task.kind == HOLD
    assert not chair.c.locked(chair.now)
    assert chair.press_jaw() is not None


class Scripted:
    """A reader that returns given cues, to reach cues the jaw map cannot produce."""

    def __init__(self):
        self.queue = []

    def reset(self):
        pass

    def read(self, frame, now_ms, moving):
        return self.queue.pop(0) if self.queue else []


@pytest.mark.parametrize("cues", [[Cue(GO, "x")], [Cue(THROUGH, "x"), Cue(CONFIRM, "x")], [Cue(GO, "x", 0.5)]])
def test_nothing_takes_the_task_out_of_hold_during_the_lockout(cues):
    chair = Chair()
    chair.c.reader = Scripted()
    chair.tick()
    lose_face(chair)
    chair.c.reader.queue.append(list(cues))
    assert chair.tick() is None and chair.c.task.kind == HOLD
    assert all(c.kind in (GO, CONFIRM) for c in chair.c.last_suppressed) and chair.c.last_suppressed


def test_slower_during_the_lockout_still_applies_until_it_ends():
    chair = Chair()
    chair.c.reader = Scripted()
    chair.tick()
    lose_face(chair)
    chair.c.reader.queue.append([Cue(SLOWER, "x")])
    chair.tick()
    assert chair.c.task.speed_mps < SPEED_DEFAULT_MPS
    chair.wait(GO_LOCKOUT_MS)
    chair.tick()
    assert chair.c.task.speed_mps == SPEED_DEFAULT_MPS
