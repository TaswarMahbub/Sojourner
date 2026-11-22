"""Constructed checks for the phase-1 feature pipeline.

Every message in this file is built in the test. None of them is a participant
recording, a camera capture, or a model output.
"""

from __future__ import annotations

import copy
import math

from ml.features import FEATURE_DIM, FEATURE_NAMES, named_features
from ml.pipeline import FeaturePipeline
from ml.records import (
    LabeledExample,
    NormalizationSpec,
    PersonalizedModelRef,
    apply_normalization,
    group_by_participant,
    group_by_session,
    partition_splits,
)
from ml.schema import BLENDSHAPES, MODEL_BLENDSHAPES, validate_feature, validate_gesture
from tests.contract_schema import validate_feature as oracle_feature
from tests.contract_schema import validate_gesture as oracle_gesture

# The contract's own illustration instant. Not a captured frame.
T0 = 1_795_276_800_000


def _matmul(left, right):
    return tuple(
        tuple(sum(left[row][k] * right[k][col] for k in range(3)) for col in range(3))
        for row in range(3)
    )


def pose(yaw_deg=0.0, pitch_deg=0.0, roll_deg=0.0, translation=(0.0, 0.0, -50.0), scale=1.0):
    """Build a pose from the contract's rotation composition. Not a measured head."""
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    roll = math.radians(roll_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    rx = ((1.0, 0.0, 0.0), (0.0, cp, sp), (0.0, -sp, cp))
    rz = ((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0))
    rotation = _matmul(_matmul(ry, rx), rz)
    extracted_yaw = math.degrees(math.atan2(rotation[0][2], rotation[2][2]))
    extracted_pitch = math.degrees(math.asin(max(-1.0, min(1.0, rotation[1][2]))))
    extracted_roll = math.degrees(math.atan2(rotation[1][0], rotation[1][1]))
    x_cm, y_cm, z_cm = translation
    matrix = [
        [scale * rotation[0][0], scale * rotation[0][1], scale * rotation[0][2], x_cm],
        [scale * rotation[1][0], scale * rotation[1][1], scale * rotation[1][2], y_cm],
        [scale * rotation[2][0], scale * rotation[2][1], scale * rotation[2][2], z_cm],
        [0.0, 0.0, 0.0, 1.0],
    ]
    return {
        "matrix": matrix,
        "yaw_deg": extracted_yaw,
        "pitch_deg": extracted_pitch,
        "roll_deg": extracted_roll,
    }


def _scores(**overrides):
    values = {name: 0.0 for name in BLENDSHAPES}
    values.update(overrides)
    return values


def tracking(timestamp, confidence=None, scores=None, head=None, **overrides):
    message = {
        "schema_version": "4",
        "timestamp_ms": timestamp,
        "tracking_state": "tracking",
        "features": _scores() if scores is None else scores,
        "head_pose": pose() if head is None else head,
        "confidence": confidence,
    }
    message.update(overrides)
    return message


def lost(timestamp, **overrides):
    message = {
        "schema_version": "4",
        "timestamp_ms": timestamp,
        "tracking_state": "lost",
        "features": {},
        "head_pose": None,
        "confidence": None,
    }
    message.update(overrides)
    return message


def partial(timestamp, features=None, confidence=None):
    return {
        "schema_version": "4",
        "timestamp_ms": timestamp,
        "tracking_state": "partial",
        "features": {} if features is None else features,
        "head_pose": None,
        "confidence": confidence,
    }


def _accepted(message) -> bool:
    return validate_feature(message) == [] and oracle_feature(message) == []


def test_tracking_null_confidence_matches_the_contract_and_is_actionable():
    message = tracking(T0, confidence=None)
    assert _accepted(message)
    pipeline = FeaturePipeline(length=1, stride=1)
    result = pipeline.push(message, produced_at_ms=T0)
    assert result.accepted
    assert result.product is not None
    assert result.product.window.actionable is True
    assert result.product.features is not None
    assert result.product.classification is None


def test_real_confidence_partial_and_lost_follow_the_contract():
    assert _accepted(tracking(T0, confidence=1.0))
    assert _accepted(tracking(T0, confidence=0.25))
    assert validate_feature(tracking(T0, confidence=0))
    assert oracle_feature(tracking(T0, confidence=0))
    assert _accepted(partial(T0, features={"jawOpen": 0.0}, confidence=0))
    assert _accepted(partial(T0, confidence=None))
    assert _accepted(lost(T0))
    assert validate_feature(lost(T0, confidence=0))
    assert validate_feature(partial(T0, features={"jawOpen": 0.2}, confidence=None) | {"head_pose": pose()})


def test_blendshape_names_and_values_are_strict():
    missing = tracking(T0)
    del missing["features"]["noseSneerRight"]
    extra = tracking(T0)
    extra["features"]["notABlendshape"] = 0.0
    high = tracking(T0, scores=_scores(jawOpen=1.5))
    negative = tracking(T0, scores=_scores(jawOpen=-0.01))
    boolean = tracking(T0, scores=_scores(jawOpen=True))
    nan = tracking(T0, scores=_scores(jawOpen=float("nan")))
    infinite = tracking(T0, scores=_scores(jawOpen=float("inf")))
    text = tracking(T0, scores=_scores(jawOpen="0.2"))
    for message in (missing, extra, high, negative, boolean, nan, infinite, text):
        assert validate_feature(message)
        assert oracle_feature(message)
        assert not FeaturePipeline().push(message, produced_at_ms=T0).accepted


def test_head_pose_constraints_reject_malformed_geometry():
    zero = [[0.0, 0.0, 0.0, 0.0] for _ in range(4)]
    bad_row = pose()
    bad_row["matrix"][3] = [0.0, 0.0, 0.0, 2.0]
    mismatch = pose()
    mismatch["yaw_deg"] = 10.0
    behind = pose(translation=(0.0, 0.0, 5.0))
    too_far = pose(translation=(0.0, 0.0, -201.0))
    edge_near = pose(translation=(100.0, -100.0, -5.0))
    edge_far = pose(translation=(-100.0, 100.0, -200.0))
    outside_pitch = pose(pitch_deg=70.0)
    stretched = pose()
    stretched["matrix"][0][0] *= 1.05
    non_finite = pose()
    non_finite["matrix"][2][3] = float("nan")
    extra = pose()
    extra["units"] = "cm"
    for head in (zero, bad_row, mismatch, behind, too_far, outside_pitch, stretched, non_finite, extra):
        message = tracking(T0, head=head)
        assert validate_feature(message)
        assert oracle_feature(message)
    assert _accepted(tracking(T0, head=edge_near))
    assert _accepted(tracking(T0, head=edge_far))
    assert _accepted(tracking(T0, head=pose(yaw_deg=20.0, pitch_deg=-10.0, roll_deg=5.0)))


def test_non_finite_timestamps_and_unexpected_fields_are_rejected():
    cases = [
        tracking(T0, schema_version="3"),
        tracking(True),
        tracking(1.0),
        tracking(-1),
        tracking(T0 + 0.5),
        {**tracking(T0), "camera": "built-in"},
        {**lost(T0), "features": {"jawOpen": 0.0}},
        partial(T0, features={"jawOpen": float("-inf")}),
        [],
        None,
        "tracking",
    ]
    for message in cases:
        assert validate_feature(message)
        assert oracle_feature(message)


def test_validator_does_not_modify_the_message_and_agrees_with_the_oracle():
    message = tracking(T0, confidence=0.5, scores=_scores(jawOpen=0.25))
    before = copy.deepcopy(message)
    assert validate_feature(message) == []
    assert message == before
    corpus = [
        message,
        lost(T0),
        partial(T0, features={"eyeBlinkLeft": 1}, confidence=1),
        tracking(T0, confidence=0),
        tracking(T0, scores=_scores(jawOpen=float("nan"))),
        {**tracking(T0), "extra": 1},
    ]
    for item in corpus:
        assert bool(validate_feature(item)) == bool(oracle_feature(item))


def test_windows_keep_timestamps_counts_and_gaps_without_inventing_frames():
    pipeline = FeaturePipeline(length=3, stride=2)
    first = pipeline.push(tracking(T0, scores=_scores(jawOpen=0.0)), produced_at_ms=T0)
    second = pipeline.push(tracking(T0 + 100, scores=_scores(jawOpen=1.0)), produced_at_ms=T0 + 100)
    assert first.product is None and second.product is None
    assert pipeline.pending_count == 2
    third = pipeline.push(tracking(T0 + 500, scores=_scores(jawOpen=0.5)), produced_at_ms=T0 + 500)
    window = third.product.window
    assert window.frame_count == 3
    assert [frame["timestamp_ms"] for frame in window.frames] == [T0, T0 + 100, T0 + 500]
    assert window.start_timestamp_ms == T0
    assert window.end_timestamp_ms == T0 + 500
    assert pipeline.pending_count == 1
    assert pipeline.last_timestamp_ms == T0 + 500
    fourth = pipeline.push(tracking(T0 + 800), produced_at_ms=T0 + 800)
    assert fourth.product is None
    fifth = pipeline.push(tracking(T0 + 900, scores=_scores(jawOpen=0.5)), produced_at_ms=T0 + 900)
    assert [frame["timestamp_ms"] for frame in fifth.product.window.frames] == [T0 + 500, T0 + 800, T0 + 900]


def test_one_frame_windows_and_stride_do_not_overlap_when_stride_covers_the_window():
    pipeline = FeaturePipeline(length=1, stride=1)
    result = pipeline.push(tracking(T0), produced_at_ms=T0)
    assert result.product.window.frame_count == 1
    assert result.product.window.start_timestamp_ms == result.product.window.end_timestamp_ms == T0
    assert pipeline.pending_count == 0
    wide = FeaturePipeline(length=2, stride=2)
    assert wide.push(tracking(T0), produced_at_ms=T0).product is None
    emitted = wide.push(tracking(T0 + 40), produced_at_ms=T0 + 40)
    assert emitted.product.window.frame_count == 2
    assert wide.pending_count == 0
    assert wide.push(tracking(T0 + 30), produced_at_ms=T0 + 30).accepted is False


def test_out_of_order_equal_and_invalid_frames_are_rejected_without_being_stored():
    pipeline = FeaturePipeline(length=2, stride=1)
    assert pipeline.push(tracking(T0 + 20), produced_at_ms=T0 + 20).accepted
    replay = pipeline.push(tracking(T0 + 20), produced_at_ms=T0 + 20)
    older = pipeline.push(tracking(T0 + 10), produced_at_ms=T0 + 10)
    invalid = pipeline.push(tracking(T0 + 30, scores=_scores(jawOpen=float("nan"))), produced_at_ms=T0 + 30)
    assert replay.accepted is False
    assert older.accepted is False
    assert invalid.accepted is False
    assert pipeline.pending_count == 1
    assert pipeline.last_timestamp_ms == T0 + 20
    accepted = pipeline.push(tracking(T0 + 30), produced_at_ms=T0 + 30)
    assert accepted.accepted
    assert [frame["timestamp_ms"] for frame in accepted.product.window.frames] == [T0 + 20, T0 + 30]


def test_feature_order_dimension_and_neutral_exclusion_are_stable():
    assert FEATURE_DIM == 162
    assert len(FEATURE_NAMES) == 162
    assert FEATURE_NAMES[:3] == ("browDownLeft.mean", "browDownLeft.std", "browDownLeft.delta")
    assert FEATURE_NAMES[51 * 3 : 51 * 3 + 3] == ("yaw_deg.mean", "yaw_deg.std", "yaw_deg.delta")
    assert FEATURE_NAMES[-3:] == ("roll_deg.mean", "roll_deg.std", "roll_deg.delta")
    assert all("_neutral" not in name for name in FEATURE_NAMES)
    assert MODEL_BLENDSHAPES[0] == "browDownLeft"
    assert MODEL_BLENDSHAPES[-1] == "noseSneerRight"
    assert "_neutral" not in MODEL_BLENDSHAPES
    low = FeaturePipeline().push(
        tracking(T0, scores=_scores(_neutral=0.0, jawOpen=0.4)), produced_at_ms=T0
    )
    high = FeaturePipeline().push(
        tracking(T0, scores=_scores(_neutral=1.0, jawOpen=0.4)), produced_at_ms=T0
    )
    assert low.product.features == high.product.features
    assert named_features(low.product.features)[0][0] == "browDownLeft.mean"


def test_mean_population_deviation_and_first_to_last_change():
    pipeline = FeaturePipeline(length=2, stride=2)
    pipeline.push(
        tracking(T0, scores=_scores(jawOpen=0.0), head=pose(yaw_deg=0.0)),
        produced_at_ms=T0,
    )
    result = pipeline.push(
        tracking(T0 + 40, scores=_scores(jawOpen=1.0), head=pose(yaw_deg=10.0)),
        produced_at_ms=T0 + 40,
    )
    named = dict(named_features(result.product.features))
    assert named["jawOpen.mean"] == 0.5
    assert named["jawOpen.std"] == 0.5
    assert named["jawOpen.delta"] == 1.0
    assert named["browDownLeft.mean"] == 0.0
    assert named["browDownLeft.std"] == 0.0
    assert named["browDownLeft.delta"] == 0.0
    assert named["yaw_deg.delta"] == pytest_yaw_delta(result)
    assert named["yaw_deg.mean"] == sum_of_yaws(result) / 2
    assert named["pitch_deg.std"] == 0.0
    single = FeaturePipeline().push(tracking(T0, scores=_scores(jawOpen=0.3)), produced_at_ms=T0)
    single_named = dict(named_features(single.product.features))
    assert single_named["jawOpen.mean"] == 0.3
    assert single_named["jawOpen.std"] == 0.0
    assert single_named["jawOpen.delta"] == 0.0


def pytest_yaw_delta(result):
    frames = result.product.window.frames
    return frames[1]["head_pose"]["yaw_deg"] - frames[0]["head_pose"]["yaw_deg"]


def sum_of_yaws(result):
    return sum(frame["head_pose"]["yaw_deg"] for frame in result.product.window.frames)


def test_non_actionable_window_is_unknown_and_has_no_vector():
    pipeline = FeaturePipeline(length=2, stride=2)
    pipeline.push(tracking(T0, scores=_scores(jawOpen=0.8)), produced_at_ms=T0)
    early = pipeline.push(lost(T0 + 25), produced_at_ms=T0 + 24)
    assert early.accepted is False
    assert pipeline.pending_count == 1
    result = pipeline.push(lost(T0 + 25), produced_at_ms=T0 + 25)
    product = result.product
    assert product.features is None
    assert product.window.actionable is False
    assert product.window.frame_count == 2
    message = product.classification
    assert message["label"] == "unknown"
    assert message["confidence"] == 0
    assert message["model_version"] == "none"
    assert message["schema_version"] == "4"
    assert message["source_timestamp_ms"] == T0 + 25
    assert message["window_end_timestamp_ms"] == T0 + 25
    assert message["window_start_timestamp_ms"] == T0
    assert message["window_frame_count"] == 2
    assert message["timestamp_ms"] == T0 + 25
    assert validate_gesture(message) == []
    assert oracle_gesture(message) == []
    assert "linear_mps" not in message
    assert "angular_rad_s" not in message
    assert "stop" not in message
    assert message["label"] != "neutral"


def test_actionable_data_does_not_emit_an_intentional_label():
    result = FeaturePipeline().push(tracking(T0, confidence=None), produced_at_ms=T0 + 5)
    assert result.product.classification is None
    assert result.product.features is not None
    lost_result = FeaturePipeline().push(lost(T0), produced_at_ms=T0)
    assert lost_result.product.classification["label"] == "unknown"
    partial_result = FeaturePipeline().push(partial(T0, features={"jawOpen": 0.1}), produced_at_ms=T0)
    assert partial_result.product.features is None
    assert partial_result.product.classification["label"] == "unknown"


def test_push_does_not_modify_the_callers_message():
    message = tracking(T0, scores=_scores(jawOpen=0.2))
    before = copy.deepcopy(message)
    result = FeaturePipeline().push(message, produced_at_ms=T0)
    message["features"]["jawOpen"] = 1.0
    message["head_pose"]["matrix"][0][3] = 80.0
    message["timestamp_ms"] = T0 + 999
    stored = result.product.window.frames[0]
    assert message != before
    assert stored["features"]["jawOpen"] == 0.2
    assert stored["head_pose"]["matrix"][0][3] == 0.0
    assert stored["timestamp_ms"] == T0
    try:
        stored["features"]["jawOpen"] = 1.0
        mutated = False
    except TypeError:
        mutated = True
    assert mutated is True


def test_future_records_do_not_fit_weights_or_eval_normalization():
    vector = FeaturePipeline().push(tracking(T0), produced_at_ms=T0).product.features
    example = LabeledExample(
        participant_id="p01",
        session_id="s01",
        split="train",
        label="jawOpen",
        features=vector,
        window_start_timestamp_ms=T0,
        window_end_timestamp_ms=T0,
        frame_count=1,
    )
    held_out = LabeledExample(
        participant_id="p01",
        session_id="s02",
        split="eval",
        label="jawOpen",
        features=vector,
        window_start_timestamp_ms=T0,
        window_end_timestamp_ms=T0 + 10,
        frame_count=1,
    )
    other = LabeledExample(
        participant_id="p02",
        session_id="s01",
        split="calibration",
        label="browUp",
        features=vector,
        window_start_timestamp_ms=T0,
        window_end_timestamp_ms=T0,
        frame_count=1,
    )
    assert group_by_participant([other, example, held_out])["p01"] == (example, held_out)
    assert group_by_session([example, held_out])[("p01", "s02")] == (held_out,)
    assert partition_splits([held_out, example, other])["calibration"] == (other,)
    spec = NormalizationSpec(
        participant_id="p01",
        mean=tuple(0.0 for _ in range(FEATURE_DIM)),
        scale=tuple(1.0 for _ in range(FEATURE_DIM)),
        source_split="calibration",
    )
    scaled = apply_normalization(vector, spec)
    assert scaled == tuple(float(value) for value in vector)
    assert spec.mean[0] == 0.0
    try:
        NormalizationSpec(
            participant_id="p01",
            mean=spec.mean,
            scale=spec.scale,
            source_split="eval",
        )
        eval_rejected = False
    except ValueError:
        eval_rejected = True
    assert eval_rejected is True
    model = PersonalizedModelRef(
        participant_id="p01",
        model_version="none",
        disabled_labels=("jawOpen",),
        rejection_threshold=0.5,
    )
    assert model.weights_present is False
    for factory in (
        lambda: PersonalizedModelRef(
            participant_id="p01",
            model_version="none",
            disabled_labels=(),
            rejection_threshold=None,
            weights_present=True,
        ),
        lambda: PersonalizedModelRef(
            participant_id="p01",
            model_version="none",
            disabled_labels=("unknown",),
            rejection_threshold=None,
        ),
        lambda: LabeledExample(
            participant_id="person@example.com",
            session_id="s01",
            split="train",
            label="jawOpen",
            features=vector,
            window_start_timestamp_ms=T0,
            window_end_timestamp_ms=T0,
            frame_count=1,
        ),
    ):
        try:
            factory()
            refused = False
        except ValueError:
            refused = True
        assert refused is True
