"""Task controller: cue vocabulary, task machine, tiers, confidence cap, wiring.

The controller only REQUESTS motion. The end-to-end tests at the bottom run
it through control/publisher.py against the MotorReceiver oracle to show
that the publisher still decides what is allowed.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from control import confidence, cues, task as taskmod, task_controller
from control.confidence import ConfidenceGovernor, factor_for
from control.cues import (
    CANCEL,
    CONFIRM,
    CUES,
    FASTER,
    GO,
    LEFT,
    PROPOSED_CUE_MODEL_VERSION,
    RIGHT,
    SLOWER,
    STOP,
    THROUGH,
    UNKNOWN,
    Cue,
    arbitrate,
    classify_label,
)
from control.publisher import ANGULAR_LIMIT, LINEAR_LIMIT, MotionRequest
from control.task import (
    GOING,
    HOLD,
    SPEED_MAX_MPS,
    THROUGH_RUN,
    TURN_INCREMENT_RAD,
    TURN_RATE_RAD_S,
    Task,
    apply_cues,
    consume_turn,
    velocity,
)
from control.task_controller import TaskController, plan
from control.tiers import Profile, ScanningReader
from tests.contract_schema import validate_gesture
from tests.control_rig import Rig, gesture as rig_gesture, tracking_frame

CONTROL = Path(__file__).resolve().parent.parent / "control"
NEW_MODULES = ("cues.py", "task.py", "tiers.py", "confidence.py", "task_controller.py")

# ----- fixtures --------------------------------------------------------------

REST = 0.30


def binding(channel: str, role: str, direction: int = 1) -> dict:
    """A blendshape binding at rest 0.30: deadband [0.25, 0.35], fires at 0.45, usable to 0.80."""
    return {
        "channel": channel,
        "role": role,
        "unit": "score",
        "direction": direction,
        "rest_mean": REST,
        "rest_std": 0.01,
        "threshold": {"active": REST + direction * 0.15},
        "deadband": {"lower": REST - 0.05, "upper": REST + 0.05},
        "usable_range": 0.50,
    }


def profile_dict(tier: int, bindings: list[dict], stop: str | None) -> dict:
    return {
        "record_type": "calibration_profile",
        "schema_version": 1,
        "accepted": True,
        "session_quality": "ok",
        "proposed_tier": {"tier": tier, "mode": {1: "proportional", 2: "discrete", 3: "scanning"}[tier], "reason": "test"},
        "stop_binding": {"channel": stop},
        "bindings": bindings,
    }


TIER2_MAP = {
    "browInnerUp": GO,
    "mouthSmileLeft": CANCEL,
    "mouthSmileRight": FASTER,
    "mouthPucker": SLOWER,
    "browOuterUpLeft": LEFT,
    "browOuterUpRight": RIGHT,
    "cheekPuff": THROUGH,
    "mouthRollLower": CONFIRM,
}
TIER2 = profile_dict(2, [binding("jawOpen", "discrete")] + [binding(c, "discrete") for c in TIER2_MAP], stop="jawOpen")
TIER1 = profile_dict(
    1,
    [
        binding("jawOpen", "proportional_primary"),
        binding("mouthLeft", "proportional_secondary"),
        binding("browInnerUp", "discrete"),
    ],
    stop="browInnerUp",
)
TIER3 = profile_dict(3, [binding("jawOpen", "scan_switch")], stop="jawOpen")
ALL_CHANNELS = ("jawOpen", "mouthLeft", *TIER2_MAP)

T0 = 1795276800000


def frame(now: int, **values: float) -> dict:
    """An actionable frame with every test channel at rest unless overridden."""
    f = tracking_frame(now)
    for channel in ALL_CHANNELS:
        f["features"][channel] = REST
    for channel, value in values.items():
        f["features"][channel] = value
    return f


def gesture(now: int, label: str = "neutral", model_version: str = "none", score: float = 0.95) -> dict:
    g = rig_gesture(now, label)
    g["model_version"] = model_version
    g["confidence"] = score
    return g


class Driver:
    """Feeds a controller one tick at a time on a 50 ms clock."""

    def __init__(self, controller: TaskController, now: int = T0):
        self.c = controller
        self.now = now

    def tick(self, label: str = "neutral", model_version: str = "none", score: float = 0.95, **values):
        self.now += 50
        return self.c.step(gesture(self.now, label, model_version, score), frame(self.now, **values), self.now)

    def press(self, channel: str):
        """One clean crossing: active for two frames (a sustained crossing), then back at rest."""
        self.tick(**{channel: 0.60})
        request = self.tick(**{channel: 0.60})
        return request, self.tick()


def started(controller: TaskController) -> Driver:
    """One rest frame so detectors are armed."""
    d = Driver(controller)
    assert d.tick() is None
    return d


# ----- vocabulary and arbitration --------------------------------------------


def test_vocabulary_is_the_report_cue_list():
    assert set(CUES) == {"stop", "go", "left", "right", "through", "slower", "faster", "confirm", "cancel"}
    assert cues.LABEL_LISTS["none"] == frozenset()


def test_label_outside_vocabulary_is_unknown_and_produces_no_motion():
    assert classify_label(PROPOSED_CUE_MODEL_VERSION, "doorway") == UNKNOWN
    assert classify_label(PROPOSED_CUE_MODEL_VERSION, "Go") == UNKNOWN
    assert classify_label(PROPOSED_CUE_MODEL_VERSION, 3) == UNKNOWN
    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    assert d.c.task.kind == GOING
    assert d.tick(label="doorway", model_version=PROPOSED_CUE_MODEL_VERSION) is None
    assert d.c.task.kind == HOLD
    assert d.c.last_cues == [Cue(STOP, "signal")]


def test_label_from_a_different_model_version_is_unknown():
    assert classify_label(PROPOSED_CUE_MODEL_VERSION, GO) == GO
    assert classify_label("none", GO) == UNKNOWN
    assert classify_label("another-model-7", GO) == UNKNOWN
    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    assert d.c.task.kind == GOING
    assert d.tick(label=GO, model_version="none") is None
    assert d.c.task.kind == HOLD


def _validator_with_reviewed_list(message):
    """Stands in for the validator after the reviewed change: the proposed list is accepted."""
    errors = validate_gesture({**message, "label": "neutral"}) if message.get("label") in CUES else validate_gesture(message)
    return errors


def test_cue_labels_are_invalid_under_the_current_contract():
    """tests/contract_schema.py rejects every non-reserved label today, so the controller stops."""
    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    assert d.tick(label=FASTER, model_version=PROPOSED_CUE_MODEL_VERSION) is None
    assert d.c.task.kind == HOLD and d.c.last_reason.startswith("gesture invalid")


def test_a_known_label_is_read_once_per_message(monkeypatch):
    monkeypatch.setattr(task_controller, "validate_gesture", _validator_with_reviewed_list)
    d = started(TaskController(TIER2, TIER2_MAP))
    d.now += 50
    g = gesture(d.now, FASTER, PROPOSED_CUE_MODEL_VERSION)
    d.c.step(g, frame(d.now), d.now)
    for _ in range(5):
        d.now += 10
        d.c.step(g, frame(d.now), d.now)
    assert d.c.task.speed_mps == pytest.approx(0.15)


def test_arbitration_stop_beats_cancel_beats_everything():
    go, cancel, stop = Cue(GO, "t"), Cue(CANCEL, "t"), Cue(STOP, "t")
    assert arbitrate([go, cancel, stop, Cue(FASTER, "t")]) == [stop]
    assert arbitrate([go, cancel, Cue(FASTER, "t")]) == [cancel]
    assert arbitrate([go, Cue(FASTER, "t")]) == [go, Cue(FASTER, "t")]
    assert arbitrate([]) == []


# ----- task machine ----------------------------------------------------------


def test_hold_is_the_default_and_the_state_after_any_stop():
    assert Task().kind == HOLD and velocity(Task()) == (0.0, 0.0)
    moving = apply_cues(Task(), [Cue(GO, "t"), Cue(LEFT, "t")])
    assert moving.kind == GOING
    for ender in (STOP, CANCEL):
        after = apply_cues(moving, [Cue(ender, "t")])
        assert after.kind == HOLD and after.turn_rad == 0.0 and velocity(after) == (0.0, 0.0)
    through = apply_cues(Task(), [Cue(THROUGH, "t"), Cue(CONFIRM, "t")])
    assert through.kind == THROUGH_RUN
    assert apply_cues(through, [Cue(STOP, "t")]).kind == HOLD

    # Signal loss inside the controller.
    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    d.now += 50
    assert d.c.step(gesture(d.now), None, d.now) is None
    assert d.c.task.kind == HOLD

    # A stop made by the publisher. After a loss the Go lockout must run out first
    # (control/task_controller.py GO_LOCKOUT_MS), and the detectors wait for rest.
    d.now += task_controller.GO_LOCKOUT_MS
    assert d.tick() is None
    d.press("browInnerUp")
    request = d.tick()
    assert request is not None
    d.c.note_published(request, {"stop": True, "task_state": "idle", "linear_mps": 0.0, "angular_rad_s": 0.0}, "x")
    assert d.c.task.kind == HOLD
    assert d.tick() is None


def test_go_starts_motion_and_no_further_cues_keep_it_moving():
    d = started(TaskController(TIER2, TIER2_MAP))
    first, _ = d.press("browInnerUp")
    assert first == MotionRequest("shared", 0.10, 0.0)
    for _ in range(100):  # five seconds of a face at rest and a neutral classifier
        request = d.tick()
        assert d.c.last_cues == [], "no cue was given"
        assert request == MotionRequest("shared", 0.10, 0.0), "the task keeps moving on its own"
    assert d.c.task.kind == GOING


def test_left_then_go_biases_the_heading_once_not_a_continuous_turn():
    t = apply_cues(Task(), [Cue(LEFT, "t")])
    assert t.kind == HOLD and velocity(t) == (0.0, 0.0), "Hold keeps the increment and does not move"
    t = apply_cues(t, [Cue(GO, "t")])
    turned = 0.0
    angulars = []
    for _ in range(60):  # 3 s at 50 ms
        linear, angular = velocity(t)
        assert linear == 0.10
        angulars.append(angular)
        turned += angular * 0.05
        t = consume_turn(t, angular, 50)
    assert all(a >= 0.0 for a in angulars)
    turning = [a for a in angulars if a != 0.0]
    assert turning and all(a == TURN_RATE_RAD_S for a in turning)
    assert angulars[len(turning):] == [0.0] * (len(angulars) - len(turning)), "turning stops and stays stopped"
    assert TURN_INCREMENT_RAD <= turned <= TURN_INCREMENT_RAD + TURN_RATE_RAD_S * 0.05
    assert t.kind == GOING


def test_right_mirrors_left_and_pending_turn_is_bounded():
    t = apply_cues(Task(), [Cue(RIGHT, "t")] * 20)
    assert t.turn_rad == -taskmod.MAX_PENDING_TURN_RAD
    t = apply_cues(t, [Cue(GO, "t")])
    assert velocity(t) == (0.10, -TURN_RATE_RAD_S)


def test_stop_wins_while_go_is_active_even_with_cancel_pending_in_the_same_tick():
    going = apply_cues(Task(), [Cue(GO, "t")])
    t = apply_cues(going, [Cue(CANCEL, "t"), Cue(GO, "t"), Cue(STOP, "t"), Cue(FASTER, "t")])
    assert t.kind == HOLD and t.speed_mps == going.speed_mps, "Faster in the same tick was dropped"

    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    assert d.c.task.kind == GOING
    d.tick(jawOpen=0.60, mouthSmileLeft=0.60, browInnerUp=0.60)
    request = d.tick(jawOpen=0.60, mouthSmileLeft=0.60, browInnerUp=0.60)
    assert request is None and d.c.task.kind == HOLD
    assert {c.kind for c in d.c.last_cues} == {STOP, CANCEL, GO}, "all three arrived; Stop won"


def test_faster_repeated_past_the_limit_clamps_at_030():
    t = apply_cues(Task(), [Cue(GO, "t")])
    speeds = []
    for _ in range(20):
        t = apply_cues(t, [Cue(FASTER, "t")])
        t, request = plan(t, [], 1.0)
        speeds.append(request.linear_mps)
    assert max(speeds) == SPEED_MAX_MPS == LINEAR_LIMIT == 0.30
    assert speeds[-1] == 0.30
    t = apply_cues(t, [Cue(SLOWER, "t")] * 20)
    assert t.speed_mps == taskmod.SPEED_MIN_MPS > 0.0, "Slower never stops; Stop does"
    assert apply_cues(Task(), [Cue(GO, "t", 1.0)]).speed_mps == 0.30


def test_through_needs_confirm_holds_a_straight_line_and_ignores_steering():
    t = apply_cues(Task(), [Cue(GO, "t"), Cue(FASTER, "t"), Cue(FASTER, "t"), Cue(THROUGH, "t")])
    assert t.kind == GOING and t.proposal == THROUGH
    t = apply_cues(t, [Cue(LEFT, "t"), Cue(CONFIRM, "t")])
    assert t.kind == THROUGH_RUN and t.turn_rad == 0.0
    assert velocity(t) == (taskmod.THROUGH_SPEED_MPS, 0.0), "reduced speed, current heading"
    for kind in (LEFT, RIGHT, FASTER, SLOWER, GO, THROUGH, CONFIRM):
        assert apply_cues(t, [Cue(kind, "t")]) == t
    assert apply_cues(t, [Cue(CANCEL, "t")]).kind == HOLD
    assert apply_cues(Task(), [Cue(CONFIRM, "t")]) == Task(), "Confirm with nothing proposed does nothing"


# ----- confidence -------------------------------------------------------------


def test_confidence_curve_is_monotone_and_stops_below_the_floor():
    grid = [i / 1000 for i in range(1001)]
    factors = [factor_for(c) for c in grid]
    assert all(b >= a for a, b in zip(factors, factors[1:]))
    assert factor_for(0.0) == 0.0 and factor_for(confidence.STOP_BELOW - 0.001) == 0.0
    assert factor_for(1.0) == 1.0
    with pytest.raises(ValueError):
        factor_for(1.5)


def test_falling_confidence_lowers_the_cap_and_never_raises_it():
    governor = ConfidenceGovernor()
    caps = [governor.update(c, T0 + i * 50) for i, c in enumerate([0.99, 0.9, 0.8, 0.7, 0.6, 0.5, 0.45, 0.41])]
    assert all(b <= a for a, b in zip(caps, caps[1:])) and caps[-1] < caps[0]
    # A rise is rate limited: one 50 ms tick back to 0.99 cannot restore the full cap.
    assert governor.update(0.99, T0 + 450) < 1.0

    d = started(TaskController(TIER2, TIER2_MAP))
    d.press("browInnerUp")
    d.c.task = apply_cues(d.c.task, [Cue(FASTER, "t")] * 4)
    linears = [d.tick(score=s).linear_mps for s in (0.95, 0.85, 0.75, 0.65, 0.55, 0.45)]
    assert all(b <= a for a, b in zip(linears, linears[1:])) and linears[-1] < linears[0]
    assert d.tick(score=0.2) is None and d.c.task.kind == HOLD
    assert d.c.last_cues == [Cue(STOP, "confidence")]


# ----- tiers -----------------------------------------------------------------


def test_profile_refusals():
    with pytest.raises(ValueError, match="not accepted"):
        Profile.from_dict({**TIER2, "accepted": False})
    with pytest.raises(ValueError, match="no control tier"):
        Profile.from_dict({**TIER2, "proposed_tier": {"tier": None, "mode": None}})
    clash = profile_dict(1, [binding("jawOpen", "proportional_primary")], stop="jawOpen")
    with pytest.raises(ValueError, match="both the stop channel"):
        TaskController(clash)
    with pytest.raises(ValueError, match="cannot be remapped"):
        TaskController(TIER2, {"jawOpen": GO})
    with pytest.raises(ValueError, match="needs a cue_map"):
        TaskController(TIER2)


def test_tier2_crossing_needs_a_return_to_rest_before_it_fires_again():
    d = started(TaskController(TIER2, TIER2_MAP))
    d.tick(mouthSmileRight=0.60)
    assert d.c.task.speed_mps == pytest.approx(0.10), "one frame is not a sustained crossing"
    d.tick(mouthSmileRight=0.60)
    for _ in range(5):
        d.tick(mouthSmileRight=0.60)  # held: still one cue
    assert d.c.task.speed_mps == pytest.approx(0.15)
    d.tick()
    d.tick(mouthSmileRight=0.60)
    d.tick(mouthSmileRight=0.60)
    assert d.c.task.speed_mps == pytest.approx(0.20)


def test_tier1_peak_of_an_excursion_sets_speed_and_relaxing_keeps_it():
    d = started(TaskController(TIER1))
    for value in (0.40, 0.55, 0.70, 0.55, 0.40):  # peak 0.70: (0.40 - 0.05) / 0.45 of the range
        assert d.tick(jawOpen=value) is None
    request = d.tick()  # back at rest: the excursion ends and is reported
    assert d.c.last_cues == [Cue(GO, "tier1", 0.78)]
    assert request == MotionRequest("shared", 0.23, 0.0)
    for _ in range(20):
        assert d.tick() == request, "relaxing does not drag the speed down"
    d.tick(mouthLeft=0.10)  # opposite side of the secondary channel
    d.tick()
    assert d.c.last_cues[0].kind == RIGHT
    assert d.tick().angular_rad_s < 0.0
    d.press("browInnerUp")  # the stop channel
    assert d.c.task.kind == HOLD


def test_tier3_options_cycle_at_the_dwell_interval():
    reader = ScanningReader(Profile.from_dict(TIER3), dwell_ms=1000)
    rest = frame(T0)
    reader.read(rest, T0, moving=False)
    shown = [reader.highlighted(T0 + i * 1000 + 500) for i in range(len(reader.options) + 2)]
    assert shown == [None, *reader.options, None]
    assert reader.highlighted(T0 + 999) is None and reader.highlighted(T0 + 1000) == reader.options[0]


def test_tier3_select_picks_the_highlighted_option():
    d = started(TaskController(TIER3, dwell_ms=1000))  # epoch at T0 + 50
    epoch = d.now
    while d.now < epoch + 1000 + 200:  # into the slot showing Go
        d.tick()
    assert d.c.reader.highlighted(d.now + 50) == GO
    d.tick(jawOpen=0.60)
    request = d.tick(jawOpen=0.60)
    assert d.c.last_cues == [Cue(GO, "tier3")]
    assert request is not None and d.c.task.kind == GOING
    for _ in range(40):
        assert d.tick() is not None, "Go keeps running with the switch at rest"


def test_tier3_select_with_nothing_highlighted_does_nothing():
    d = started(TaskController(TIER3, dwell_ms=1000))
    assert d.c.reader.highlighted(d.now + 50) is None
    assert d.tick(jawOpen=0.60) is None
    assert d.tick(jawOpen=0.60) is None
    assert d.c.last_cues == [] and d.c.task == Task()


def test_tier3_any_select_while_moving_is_stop():
    d = started(TaskController(TIER3, dwell_ms=1000))
    d.c.task = apply_cues(d.c.task, [Cue(GO, "t")])
    for _ in range(30):  # 1.5 s: Left is highlighted now, not Stop
        d.tick()
    assert d.c.reader.highlighted(d.now + 50) is not None
    d.tick(jawOpen=0.60)
    assert d.tick(jawOpen=0.60) is None
    assert d.c.last_cues == [Cue(STOP, "tier3")] and d.c.task.kind == HOLD


# ----- properties ------------------------------------------------------------


def _assert_in_contract(t: Task, request: MotionRequest | None):
    if t.kind == HOLD:
        assert request is None, "motion while the task is Hold"
    if request is not None:
        assert request.task_state == "shared"
        assert 0.0 <= request.linear_mps <= LINEAR_LIMIT
        assert -ANGULAR_LIMIT <= request.angular_rad_s <= ANGULAR_LIMIT
        assert MotionRequest(request.task_state, request.linear_mps, request.angular_rad_s) == request


def test_property_random_cue_sequences_stay_inside_the_contract():
    rng = random.Random(20261122)
    for _ in range(400):
        t = Task()
        last_angular = 0.0
        for _ in range(rng.randint(1, 80)):
            batch = []
            for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
                kind = rng.choice(CUES)
                magnitude = rng.choice((None, None, round(rng.uniform(0.01, 1.0), 2)))
                batch.append(Cue(kind, "prop", magnitude))
            factor = rng.choice((1.0, rng.random(), 0.0))
            t = consume_turn(t, last_angular, rng.randint(0, 400))
            t, request = plan(t, batch, factor)
            _assert_in_contract(t, request)
            if any(c.kind in (STOP, CANCEL) for c in batch):
                assert t.kind == HOLD and request is None
            last_angular = 0.0 if request is None else request.angular_rad_s


def test_property_random_inputs_through_the_controller():
    """Mostly clean input with presses, noise and loss events mixed in.

    Weighted so that the controller really moves and turns; the counts at
    the end make sure the property is not passing on stopped ticks alone.
    """
    rng = random.Random(7)
    bad_labels = ["unknown", "doorway", *CUES]
    for profile, cue_map in ((TIER1, None), (TIER2, TIER2_MAP), (TIER3, None)):
        controller = TaskController(profile, cue_map, dwell_ms=300)
        now = T0
        moving = turning = 0
        held = None
        for _ in range(4000):
            now += rng.choice((10, 50, 50, 50, 120))
            values, label, version, score, f_age = {}, "neutral", "none", rng.uniform(0.6, 1.0), 0
            event = rng.random()
            if held is not None and rng.random() < 0.6:
                values[held] = rng.uniform(0.36, 1.0)  # an excursion or a held press continues
            else:
                held = None
            if event < 0.12:
                held = rng.choice(ALL_CHANNELS)
                values[held] = rng.uniform(0.36, 1.0)
            elif event < 0.17:
                values[rng.choice(ALL_CHANNELS)] = rng.uniform(0.0, 0.25)
            elif event < 0.172:
                label, version = rng.choice(bad_labels), rng.choice(("none", PROPOSED_CUE_MODEL_VERSION, "other"))
            elif event < 0.174:
                score = rng.uniform(0.0, 0.4)
            elif event < 0.176:
                f_age = 400
            f = None if rng.random() < 0.001 else frame(now - f_age, **values)
            g = gesture(now, label, version, score)
            request = controller.step(g, f, now)
            _assert_in_contract(controller.task, request)
            moving += request is not None
            turning += request is not None and request.angular_rad_s != 0.0
        # Seeded, so these counts are fixed (moving/turning: tier 1 515/102, tier 2 250/62,
        # tier 3 151/0). Tier 3 turning needs Left then Go picked from a scan, which this
        # random walk does not do; the scripted run covers it.
        # Every loss event now starts a 2.5 s Go lockout, so Tier 3 (which needs a scan to
        # reach Go) moves on far fewer ticks; 20 still proves the property is not vacuous.
        assert moving > 20, (profile["proposed_tier"], moving)
        if profile is not TIER3:
            assert turning > 0, (profile["proposed_tier"], turning)


# ----- architecture ------------------------------------------------------------


def test_new_modules_never_build_a_datagram_or_import_test_authorities():
    forbidden_modules = {"tests.control_auth", "tests.status_gate", "tests.motor_receiver", "socket"}
    forbidden_names = {"sign_command", "encode_command", "_seal_and_send", "_send_authorized", "_send_stop", "_send"}
    for name in NEW_MODULES:
        tree = ast.parse((CONTROL / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_modules, (name, node.module)
                assert not {a.name for a in node.names} & forbidden_names, name
                if node.module and node.module.startswith("tests."):
                    assert node.module == "tests.contract_schema", (name, node.module)
            elif isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & forbidden_modules, name
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_names | {"gate", "allocator"}:
                raise AssertionError(f"{name} reaches into the publisher: .{node.attr}")


# ----- end to end through the publisher ----------------------------------------


class Loop:
    """Controller -> Publisher.tick() -> MotorReceiver, with a status before each tick."""

    def __init__(self, tmp_path, profile=TIER2, cue_map=TIER2_MAP):
        self.rig = Rig(tmp_path)
        self.rig.ready_receiver()
        self.rig.handshake()
        self.rig.arm()
        self.controller = TaskController(profile, cue_map)

    def tick(self, label="neutral", model_version="none", **values):
        rig = self.rig
        rig.advance(20)
        rig.status()
        now = rig.clock.unix
        f, g = frame(now, **values), gesture(now, label, model_version)
        rig.publisher.observe_frame(f)
        rig.publisher.observe_gesture(g)
        before = len(rig.results)
        request, message = self.controller.tick(rig.publisher, g, f)
        result = rig.results[-1] if len(rig.results) > before else None
        return request, message, result


def test_end_to_end_go_runs_until_stop(tmp_path):
    loop = Loop(tmp_path)
    loop.tick()
    loop.tick(browInnerUp=0.60)
    loop.tick(browInnerUp=0.60)
    results = [loop.tick()[2] for _ in range(20)]
    assert results == ["accepted_motion"] * 20
    assert loop.rig.receiver.motors_have_torque()
    loop.tick(jawOpen=0.60)
    request, message, result = loop.tick(jawOpen=0.60)
    assert request is None and message["stop"] is True
    assert loop.controller.task.kind == HOLD
    assert loop.rig.receiver.snapshot()["linear_mps"] == 0.0


def test_end_to_end_cue_label_is_refused_by_both_layers(tmp_path):
    """The proposed vocabulary does not make anything accept a label (section 2)."""
    loop = Loop(tmp_path)
    loop.tick()
    loop.tick(browInnerUp=0.60)
    loop.tick(browInnerUp=0.60)
    assert loop.tick()[2] == "accepted_motion"
    request, message, _ = loop.tick(label=GO, model_version=PROPOSED_CUE_MODEL_VERSION)
    assert request is None and loop.controller.task.kind == HOLD
    assert message["stop"] is True and loop.rig.publisher.last_decision == "idle: gesture invalid"
    assert loop.tick()[0] is None, "nothing resumes on its own"


def test_end_to_end_publisher_refusal_returns_the_task_to_hold(monkeypatch, tmp_path):
    """Even if the controller accepted a cue label, the publisher refuses it and the task holds."""
    monkeypatch.setattr(task_controller, "validate_gesture", _validator_with_reviewed_list)
    loop = Loop(tmp_path)
    loop.tick()
    request, message, _ = loop.tick(label=GO, model_version=PROPOSED_CUE_MODEL_VERSION)
    assert request is not None, "the controller asked"
    assert message["stop"] is True and loop.rig.publisher.last_decision == "idle: gesture invalid"
    assert loop.controller.task.kind == HOLD, "a refused request returns the task to Hold"
    assert loop.tick()[0] is None, "nothing resumes on its own"


def test_end_to_end_operator_estop_returns_the_task_to_hold(tmp_path):
    loop = Loop(tmp_path)
    loop.tick()
    loop.tick(browInnerUp=0.60)
    loop.tick(browInnerUp=0.60)
    assert loop.tick()[2] == "accepted_motion"
    rig = loop.rig
    rig.advance(20)
    rig.status()
    now = rig.clock.unix
    f, g = frame(now), gesture(now)
    loop.controller.tick(rig.publisher, g, f, operator_estop=True)
    assert loop.controller.task.kind == HOLD
    assert rig.receiver.snapshot()["software_estop_latch"] is True
