"""Directional cue bindings: (channel, above|below rest, cue), both sides of one channel."""

from __future__ import annotations

import pytest

from control.cues import FASTER, GO, LEFT, RIGHT, STOP, Cue
from control.tiers import ABOVE, BELOW, MIN_SUSTAINED_FRAMES, CueBinding, DiscreteReader, Profile, cue_bindings
from perception.calibration import DEFAULT_CONSTANTS
from tests.control_rig import tracking_frame

T0 = 1795276800000
PITCH_REST = 12.0


def angle_binding(channel: str, rest: float, direction: int = 1, *, offset: float = 2.0, deadband: float = 1.2,
                  lower: float | None = None, upper: float | None = None) -> dict:
    """A Phase 3 shaped binding: symmetric thresholds unless lower/upper are given."""
    lo = rest - offset if lower is None else lower
    hi = rest + offset if upper is None else upper
    return {
        "channel": channel, "role": "discrete", "unit": "deg", "direction": direction, "rest_mean": rest,
        "threshold": {"lower": lo, "upper": hi, "active": hi if direction > 0 else lo},
        "deadband": {"lower": rest - deadband, "upper": rest + deadband},
        "usable_range": 30.0,
    }


def jaw_binding() -> dict:
    return {
        "channel": "jawOpen", "role": "discrete", "unit": "score", "direction": 1, "rest_mean": 0.002,
        "threshold": {"lower": -0.023, "upper": 0.027, "active": 0.027},
        "deadband": {"lower": -0.013, "upper": 0.017}, "usable_range": 0.5,
    }


def profile(*bindings: dict, stop: str | None = "jawOpen") -> Profile:
    return Profile.from_dict({
        "record_type": "calibration_profile", "accepted": True, "session_quality": "ok",
        "proposed_tier": {"tier": 2, "mode": "discrete"}, "stop_binding": {"channel": stop},
        "bindings": [jaw_binding(), *bindings],
    })


PITCH_BOTH = [CueBinding("pitch_deg", ABOVE, FASTER), CueBinding("pitch_deg", BELOW, LEFT)]


class Feed:
    def __init__(self, reader):
        self.reader = reader
        self.now = T0

    def frame(self, pitch: float = PITCH_REST, jaw: float = 0.002) -> list[str]:
        self.now += 50
        f = tracking_frame(self.now)
        f["head_pose"]["pitch_deg"] = pitch
        f["features"]["jawOpen"] = jaw
        return [c.kind for c in self.reader.read(f, self.now, moving=False)]

    def hold(self, frames: int, **values) -> list[str]:
        fired = []
        for _ in range(frames):
            fired += self.frame(**values)
        return fired


def test_sustained_frames_match_calibration():
    assert MIN_SUSTAINED_FRAMES == DEFAULT_CONSTANTS.min_sustained_frames


def test_opposite_sides_of_one_channel_are_distinct_cues():
    feed = Feed(DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)), PITCH_BOTH))
    assert feed.hold(2) == []
    assert feed.hold(3, pitch=PITCH_REST + 5) == [FASTER]
    assert feed.hold(2) == []
    assert feed.hold(3, pitch=PITCH_REST - 5) == [LEFT]


def test_one_frame_past_the_threshold_is_not_a_crossing():
    feed = Feed(DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)), PITCH_BOTH))
    feed.hold(2)
    assert feed.frame(pitch=PITCH_REST + 5) == []
    assert feed.frame() == []
    assert feed.frame(pitch=PITCH_REST + 5) == []
    assert feed.frame(pitch=PITCH_REST + 5) == [FASTER]


def test_each_side_rearms_only_inside_the_deadband():
    feed = Feed(DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)), PITCH_BOTH))
    feed.hold(2)
    assert feed.hold(2, pitch=PITCH_REST + 5) == [FASTER]
    # Between the deadband (1.2) and the threshold (2.0): not rest, so no re-arm.
    assert feed.hold(2, pitch=PITCH_REST + 1.6) == []
    assert feed.hold(2, pitch=PITCH_REST + 5) == []
    feed.hold(1)
    assert feed.hold(2, pitch=PITCH_REST + 5) == [FASTER]


def test_release_overshoot_through_the_other_threshold_fires_the_other_cue():
    """Hysteresis cannot tell an overshoot from a gesture; the cross-talk measurement has to."""
    feed = Feed(DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)), PITCH_BOTH))
    feed.hold(2)
    assert feed.hold(3, pitch=PITCH_REST + 8) + feed.hold(2, pitch=PITCH_REST - 3) == [FASTER, LEFT]


def test_both_sides_refused_when_a_threshold_lies_inside_the_deadband():
    tight = angle_binding("pitch_deg", PITCH_REST, lower=PITCH_REST - 1.0)  # deadband is 1.2
    with pytest.raises(ValueError, match="inside the deadband"):
        DiscreteReader(profile(tight), PITCH_BOTH)
    # The measured side alone is still bindable.
    reader = DiscreteReader(profile(tight), [CueBinding("pitch_deg", ABOVE, FASTER)])
    assert reader.bindings == [("jawOpen", ABOVE, STOP), ("pitch_deg", ABOVE, FASTER)]


def test_crossed_thresholds_are_refused():
    crossed = angle_binding("pitch_deg", PITCH_REST, lower=PITCH_REST + 0.5, upper=PITCH_REST + 2.0)
    with pytest.raises(ValueError, match="overlap"):
        DiscreteReader(profile(crossed), PITCH_BOTH)


def test_a_side_bound_twice_is_refused():
    with pytest.raises(ValueError, match="bound twice"):
        DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)),
                       [CueBinding("pitch_deg", ABOVE, FASTER), CueBinding("pitch_deg", ABOVE, GO)])


def test_nothing_but_stop_on_either_side_of_the_stop_channel():
    p = profile(angle_binding("pitch_deg", PITCH_REST))
    for side in (ABOVE, BELOW):
        with pytest.raises(ValueError, match="stop channel"):
            DiscreteReader(p, [CueBinding("jawOpen", side, RIGHT)])
    with pytest.raises(ValueError, match="stop channel"):
        DiscreteReader(p, [CueBinding("jawOpen", BELOW, STOP)])
    assert [b.cue for b in cue_bindings(p, [CueBinding("jawOpen", ABOVE, STOP)])] == [STOP]


def test_dict_cue_map_binds_the_measured_side():
    down = angle_binding("pitch_deg", PITCH_REST, direction=-1)
    reader = DiscreteReader(profile(down), {"pitch_deg": LEFT})
    assert reader.bindings == [("jawOpen", ABOVE, STOP), ("pitch_deg", BELOW, LEFT)]
    feed = Feed(reader)
    feed.hold(2)
    assert feed.hold(2, pitch=PITCH_REST + 5) == []
    assert feed.hold(2, pitch=PITCH_REST - 5) == [LEFT]


def test_stop_and_a_head_cue_in_one_frame_both_fire():
    """Detection reports both; arbitration (control/cues.py) then lets only Stop through."""
    feed = Feed(DiscreteReader(profile(angle_binding("pitch_deg", PITCH_REST)), PITCH_BOTH))
    feed.hold(2)
    assert sorted(feed.hold(2, pitch=PITCH_REST + 5, jaw=0.3)) == sorted([STOP, FASTER])
    assert Cue(STOP, "t").kind == STOP


def test_bad_cue_binding_values():
    with pytest.raises(ValueError):
        CueBinding("pitch_deg", "up", FASTER)
    with pytest.raises(ValueError):
        CueBinding("pitch_deg", ABOVE, "jump")


# ----- context-dependent binding: Go in Hold, Stop while moving ------------------

from control.cue_map import CUE_MAP, UNBOUND_CHANNELS  # noqa: E402
from control.task import HOLD, Task, apply_cues  # noqa: E402
from control.task_controller import TaskController  # noqa: E402

GO_OR_STOP = CueBinding("jawOpen", ABOVE, GO, moving_cue=STOP)


def moving_feed(reader):
    feed = Feed(reader)
    feed.hold(2)
    return feed


def test_the_jaw_means_go_in_hold_and_stop_while_moving():
    p = profile(angle_binding("pitch_deg", PITCH_REST))
    reader = DiscreteReader(p, [GO_OR_STOP])
    assert reader.bindings == [("jawOpen", ABOVE, GO, STOP)]
    f = tracking_frame(T0)
    f["features"]["jawOpen"] = 0.002
    reader.read(f, T0, moving=False)
    f["features"]["jawOpen"] = 0.3
    reader.read(f, T0 + 50, moving=False)
    assert [c.kind for c in reader.read(f, T0 + 100, moving=False)] == [GO]
    f["features"]["jawOpen"] = 0.002
    reader.read(f, T0 + 150, moving=True)
    f["features"]["jawOpen"] = 0.3
    reader.read(f, T0 + 200, moving=True)
    assert [c.kind for c in reader.read(f, T0 + 250, moving=True)] == [STOP]


def test_the_stop_channel_must_always_stop_a_moving_chair():
    p = profile(angle_binding("pitch_deg", PITCH_REST))
    for bad in (CueBinding("jawOpen", ABOVE, GO), CueBinding("jawOpen", ABOVE, GO, moving_cue=FASTER),
                CueBinding("jawOpen", ABOVE, FASTER, moving_cue=STOP), CueBinding("jawOpen", BELOW, GO, moving_cue=STOP)):
        with pytest.raises(ValueError, match="stop channel"):
            DiscreteReader(p, [bad])
    with pytest.raises(ValueError, match="different cue"):
        CueBinding("jawOpen", ABOVE, STOP, moving_cue=STOP)


def jaw_pitch_yaw_profile() -> dict:
    yaw = angle_binding("yaw_deg", 0.4, direction=-1, offset=16.0, deadband=9.6)
    pitch = angle_binding("pitch_deg", PITCH_REST, offset=11.0, deadband=6.6)
    return {"record_type": "calibration_profile", "accepted": True, "session_quality": "ok",
            "proposed_tier": {"tier": 2, "mode": "discrete"}, "stop_binding": {"channel": "jawOpen"},
            "bindings": [jaw_binding(), pitch, yaw]}


def test_the_cue_map_binds_and_leaves_roll_unbound():
    reader = TaskController(jaw_pitch_yaw_profile(), CUE_MAP).reader
    assert reader.bindings == [
        ("jawOpen", ABOVE, GO, STOP), ("pitch_deg", ABOVE, FASTER), ("pitch_deg", BELOW, "slower"),
        ("yaw_deg", ABOVE, LEFT), ("yaw_deg", BELOW, RIGHT),
    ]
    assert UNBOUND_CHANNELS == ("roll_deg",)
    assert all(b.channel != "roll_deg" for b in CUE_MAP)


class Session:
    def __init__(self):
        self.c = TaskController(jaw_pitch_yaw_profile(), CUE_MAP)
        self.now = T0

    def tick(self, jaw=0.002, yaw=0.4, pitch=PITCH_REST):
        from tests.control_rig import gesture
        self.now += 50
        from tests.run_doorway_e2e import posed_frame  # a pose matrix that agrees with the angles
        f = posed_frame(self.now, jaw, yaw, pitch, 0.0)
        g = gesture(self.now)
        g["confidence"] = 0.95
        return self.c.step(g, f, self.now)


def test_one_jaw_press_is_one_cue_go_then_stop_then_go():
    s = Session()
    s.tick()
    s.tick(jaw=0.5)
    assert s.tick(jaw=0.5) is not None and s.c.task.kind == "go"
    for _ in range(10):
        assert s.tick() is not None, "travel needs no cue"
    s.tick(jaw=0.5)
    assert s.tick(jaw=0.5) is None and s.c.task.kind == HOLD
    for _ in range(5):
        assert s.tick(jaw=0.5) is None, "holding the jaw open after a Stop does not restart the chair"
    s.tick()
    s.tick(jaw=0.5)
    assert s.tick(jaw=0.5) is not None, "a new press after returning to rest is Go again"


def test_through_counts_as_moving_for_the_jaw():
    s = Session()
    s.tick()
    s.c.task = apply_cues(Task(), [Cue("through", "t"), Cue("confirm", "t")])
    s.tick(jaw=0.5)
    assert s.tick(jaw=0.5) is None and s.c.task.kind == HOLD


def test_incidental_head_movement_in_hold_never_moves():
    """Turn and Slower cues in Hold change only the pending heading and the speed setting."""
    s = Session()
    s.tick()
    for values in ({"yaw": -30.0}, {"yaw": 30.0}, {"pitch": PITCH_REST - 15}, {"pitch": PITCH_REST + 15},
                   {"yaw": -30.0, "pitch": PITCH_REST - 15}):
        assert s.tick(**values) is None
        assert s.tick(**values) is None
        assert s.tick() is None
    assert s.c.task.kind == HOLD
