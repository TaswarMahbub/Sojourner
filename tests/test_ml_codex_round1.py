"""Regressions for the Codex round 1 findings on the phase-1 ML pipeline.

Every message here is constructed in the test. None is a recording, a camera
capture, or model output. Schema cases run against both ``ml.schema`` and the
contract oracle in ``tests/contract_schema.py``, because both accepted the
original defects.
"""

from __future__ import annotations

import copy
import math

import pytest

from ml.features import FEATURE_DIM
from ml.pipeline import FeaturePipeline
from ml.records import LabeledExample, NormalizationSpec, apply_normalization
from ml.schema import validate_feature, validate_gesture
from ml.windows import WindowAssembler
from tests.contract_schema import validate_feature as oracle_feature
from tests.contract_schema import validate_gesture as oracle_gesture
from tests.test_ml_pipeline import T0, lost, partial, pose, tracking

FEATURE_VALIDATORS = pytest.mark.parametrize(
    "check", [validate_feature, oracle_feature], ids=["ml", "oracle"]
)
GESTURE_VALIDATORS = pytest.mark.parametrize(
    "check", [validate_gesture, oracle_gesture], ids=["ml", "oracle"]
)


def _with_matrix(rotation3, scale=1.0, angles=(0.0, 0.0, 0.0)):
    head = pose()
    head["matrix"] = [
        [scale * rotation3[r][c] for c in range(3)] + [head["matrix"][r][3]] for r in range(3)
    ] + [[0.0, 0.0, 0.0, 1.0]]
    head["yaw_deg"], head["pitch_deg"], head["roll_deg"] = angles
    return tracking(T0, head=head)


# A. Published angles must respect their own range.

LIMITS = [("yaw", 90.0), ("pitch", 60.0), ("roll", 45.0)]
BOUNDARIES = [(name, limit, sign) for name, limit in LIMITS for sign in (1, -1)]


@FEATURE_VALIDATORS
@pytest.mark.parametrize("name,limit,sign", BOUNDARIES)
def test_published_angle_slightly_outside_range_is_rejected(check, name, limit, sign):
    head = pose(**{f"{name}_deg": sign * limit})
    head[f"{name}_deg"] = sign * (limit + 0.05)
    assert check(tracking(T0, head=head))


@FEATURE_VALIDATORS
@pytest.mark.parametrize("name,limit,sign", BOUNDARIES)
def test_published_angle_exactly_at_or_inside_the_boundary_is_accepted(check, name, limit, sign):
    head = pose(**{f"{name}_deg": sign * limit})
    head[f"{name}_deg"] = sign * limit
    assert check(tracking(T0, head=head)) == []
    inside = pose(**{f"{name}_deg": sign * (limit - 0.05)})
    inside[f"{name}_deg"] = sign * limit
    assert check(tracking(T0, head=inside)) == []


@FEATURE_VALIDATORS
@pytest.mark.parametrize("name,limit,sign", BOUNDARIES)
def test_matrix_angle_outside_range_is_rejected_even_if_published_is_legal(check, name, limit, sign):
    head = pose(**{f"{name}_deg": sign * (limit + 0.05)})
    head[f"{name}_deg"] = sign * limit
    assert check(tracking(T0, head=head))


# B. Rotation geometry, and 8. numerical stability.

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


@FEATURE_VALIDATORS
@pytest.mark.parametrize(
    "angles", [(0, 0, 0), (30, -20, 10), (-89.9, 59.9, -44.9), (89.99, -59.99, 44.99), (-45, 10, 44)]
)
@pytest.mark.parametrize("scale", [1.0, 0.5, 3.7, 1e200, 1e-200, 1e300, 1e-300])
def test_valid_rotations_pass_at_ordinary_and_extreme_scales(check, angles, scale):
    yaw, pitch, roll = angles
    head = pose(yaw_deg=yaw, pitch_deg=pitch, roll_deg=roll, scale=scale)
    assert check(tracking(T0, head=head)) == []


@FEATURE_VALIDATORS
@pytest.mark.parametrize(
    "rotation3,angles",
    [
        ([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], (0.0, 0.0, 0.0)),
        ([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]], (180.0, 0.0, 0.0)),
        ([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]], (0.0, 0.0, 180.0)),
        ([[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], (0.0, 0.0, 90.0)),
    ],
    ids=["mirror-x", "mirror-z", "mirror-y", "swap-xy"],
)
@pytest.mark.parametrize("scale", [1.0, 1e200])
def test_reflections_are_rejected(check, rotation3, angles, scale):
    assert check(_with_matrix(rotation3, scale=scale, angles=angles))


@FEATURE_VALIDATORS
def test_reflection_of_a_small_legal_rotation_is_rejected(check):
    head = pose(yaw_deg=10.0, pitch_deg=5.0, roll_deg=3.0)
    for r in range(3):
        head["matrix"][r][0] = -head["matrix"][r][0]
    assert check(tracking(T0, head=head))


@FEATURE_VALIDATORS
@pytest.mark.parametrize(
    "rotation3",
    [
        [[1.0, 1.0, 1.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        [[1.0, 1.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
        [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]],
    ],
    ids=["identical-columns", "two-identical", "zero", "zero-column"],
)
def test_singular_matrices_are_rejected(check, rotation3):
    assert check(_with_matrix(rotation3))


@FEATURE_VALIDATORS
@pytest.mark.parametrize("skew_deg", [1.0, 10.0, 45.0])
def test_nonorthogonal_columns_with_equal_norms_are_rejected(check, skew_deg):
    c, s = math.cos(math.radians(skew_deg)), math.sin(math.radians(skew_deg))
    sheared = [[1.0, s, 0.0], [0.0, c, 0.0], [0.0, 0.0, 1.0]]
    assert check(_with_matrix(sheared))


@FEATURE_VALIDATORS
def test_float_noise_far_inside_the_tolerance_is_accepted(check):
    noisy = [[1.0, 1e-6, -2e-6], [-1e-6, 1.0, 1e-6], [2e-6, -1e-6, 1.0]]
    assert check(_with_matrix(noisy)) == []


@FEATURE_VALIDATORS
def test_the_one_percent_scale_rule_is_unchanged(check):
    inside = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.015]]
    outside = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.02]]
    assert check(_with_matrix(inside)) == []
    assert check(_with_matrix(outside))


@FEATURE_VALIDATORS
@pytest.mark.parametrize("scale", [1e308, 5e-324])
def test_extreme_representations_never_raise(check, scale):
    message = _with_matrix(IDENTITY, scale=scale)
    assert isinstance(check(message), list)


# C. One-frame provenance.


def _gesture(**overrides):
    message = {
        "schema_version": "4",
        "timestamp_ms": T0 + 40,
        "source_timestamp_ms": T0,
        "window_start_timestamp_ms": T0,
        "window_end_timestamp_ms": T0,
        "window_frame_count": 1,
        "label": "unknown",
        "confidence": 0,
        "model_version": "none",
    }
    message.update(overrides)
    return message


@GESTURE_VALIDATORS
def test_one_frame_window_with_equal_times_is_valid(check):
    assert check(_gesture()) == []


@GESTURE_VALIDATORS
@pytest.mark.parametrize(
    "overrides",
    [
        {"window_start_timestamp_ms": T0 - 1},
        {"window_start_timestamp_ms": T0 - 1000},
        {"window_end_timestamp_ms": T0 + 1, "source_timestamp_ms": T0 + 1, "window_start_timestamp_ms": T0},
        {"source_timestamp_ms": T0 + 1},
    ],
)
def test_one_frame_window_with_differing_times_is_rejected(check, overrides):
    assert check(_gesture(**overrides))


@GESTURE_VALIDATORS
def test_multi_frame_window_rules_are_unchanged(check):
    assert check(_gesture(window_frame_count=3, window_start_timestamp_ms=T0 - 66)) == []
    assert check(_gesture(window_frame_count=2, window_start_timestamp_ms=T0 + 1))
    assert check(_gesture(window_frame_count=2, source_timestamp_ms=T0 - 1))


# D. Stride less than, equal to, and greater than length.


def _windows(length, stride, timestamps, make=None):
    make = make or tracking
    pipeline = FeaturePipeline(length, stride)
    got = []
    for t in timestamps:
        result = pipeline.push(make(t), produced_at_ms=t)
        assert result.accepted, result.errors
        if result.product is not None:
            window = result.product.window
            times = tuple(frame["timestamp_ms"] for frame in window.frames)
            assert window.frame_count == len(times) == length
            assert (window.start_timestamp_ms, window.end_timestamp_ms) == (times[0], times[-1])
            got.append(times)
    return got


@pytest.mark.parametrize(
    "length,stride,expected",
    [
        (3, 1, [(0, 1, 2), (1, 2, 3), (2, 3, 4), (3, 4, 5), (4, 5, 6), (5, 6, 7)]),
        (3, 2, [(0, 1, 2), (2, 3, 4), (4, 5, 6)]),
        (2, 2, [(0, 1), (2, 3), (4, 5), (6, 7)]),
        (1, 1, [(t,) for t in range(8)]),
        (1, 3, [(0,), (3,), (6,)]),
        (2, 3, [(0, 1), (3, 4), (6, 7)]),
        (3, 5, [(0, 1, 2), (5, 6, 7)]),
    ],
)
def test_window_starts_are_exactly_stride_accepted_frames_apart(length, stride, expected):
    assert _windows(length, stride, range(8)) == expected


def test_length_2_stride_3_emits_6_7_only_once_frame_7_arrives():
    pipeline = FeaturePipeline(2, 3)
    products = [pipeline.push(tracking(t), produced_at_ms=t).product for t in range(7)]
    assert [p is not None for p in products] == [False, True, False, False, True, False, False]
    last = pipeline.push(tracking(7), produced_at_ms=7).product
    assert tuple(f["timestamp_ms"] for f in last.window.frames) == (6, 7)


def test_skipped_frames_are_validated_and_keep_timestamp_order():
    assembler = WindowAssembler(1, 3)
    assert assembler.push(tracking(10)).window is not None
    assert assembler.skip_remaining == 2
    bad = tracking(11)
    bad["features"]["jawOpen"] = float("nan")
    rejected = assembler.push(bad)
    assert not rejected.accepted and rejected.errors
    assert assembler.skip_remaining == 2 and assembler.last_timestamp_ms == 10
    assert assembler.push(tracking(20)).accepted
    assert assembler.skip_remaining == 1
    assert not assembler.push(tracking(20)).accepted
    assert not assembler.push(tracking(15)).accepted
    assert assembler.push(tracking(21)).window is None
    window = assembler.push(tracking(22)).window
    assert window.frames[0]["timestamp_ms"] == 22 and window.frame_count == 1


def test_non_actionable_skipped_frame_does_not_taint_the_next_window():
    pipeline = FeaturePipeline(1, 2)
    assert pipeline.push(tracking(0), produced_at_ms=0).product.window.actionable
    skipped = pipeline.push(lost(1), produced_at_ms=1)
    assert skipped.accepted and skipped.product is None
    nxt = pipeline.push(tracking(2), produced_at_ms=2).product
    assert nxt.window.actionable and nxt.features is not None and nxt.classification is None


def test_non_actionable_window_after_a_skip_is_unknown_with_true_provenance():
    pipeline = FeaturePipeline(2, 3)
    for t in (0, 1, 2):
        pipeline.push(tracking(t), produced_at_ms=t)
    pipeline.push(partial(3), produced_at_ms=3)
    product = pipeline.push(tracking(4), produced_at_ms=40).product
    message = product.classification
    assert message["label"] == "unknown" and product.features is None
    assert (message["window_start_timestamp_ms"], message["window_end_timestamp_ms"]) == (3, 4)
    assert message["window_frame_count"] == 2 and message["source_timestamp_ms"] == 4
    assert validate_gesture(message) == [] and oracle_gesture(message) == []


def test_rejected_production_time_is_atomic_with_a_pending_skip():
    pipeline = FeaturePipeline(2, 3)
    for t in (0, 1, 2):
        pipeline.push(tracking(t), produced_at_ms=t)
    pipeline.push(lost(3), produced_at_ms=3)
    state = (pipeline.pending_count, pipeline.last_timestamp_ms)
    early = pipeline.push(lost(4), produced_at_ms=3)
    assert not early.accepted and early.product is None
    assert (pipeline.pending_count, pipeline.last_timestamp_ms) == state
    retry = pipeline.push(lost(4), produced_at_ms=4)
    assert retry.accepted and retry.product.window.frame_count == 2
    assert pipeline._assembler.skip_remaining == 1


def test_assembled_windows_stay_copies_with_a_large_stride():
    pipeline = FeaturePipeline(1, 4)
    message = tracking(0)
    original = copy.deepcopy(message)
    product = pipeline.push(message, produced_at_ms=0).product
    message["features"]["jawOpen"] = 1.0
    assert product.window.frames[0]["features"]["jawOpen"] == original["features"]["jawOpen"]


# E. LabeledExample keeps an immutable validated copy.


def _example(features):
    return LabeledExample("p1", "s1", "train", "neutral", features, 0, 0, 1)


def test_mutating_the_source_list_does_not_change_the_example():
    source = [0.0] * FEATURE_DIM
    example = _example(source)
    source[0] = float("nan")
    source.append(7.0)
    del source[1:50]
    assert isinstance(example.features, tuple)
    assert len(example.features) == FEATURE_DIM
    assert all(math.isfinite(v) and v == 0.0 for v in example.features)


def test_integer_features_are_canonicalized_to_floats():
    example = _example([1] * FEATURE_DIM)
    assert example.features == (1.0,) * FEATURE_DIM
    assert all(type(v) is float for v in example.features)


@pytest.mark.parametrize(
    "features",
    [
        [0.0] * (FEATURE_DIM - 1),
        [0.0] * (FEATURE_DIM + 1),
        [True] + [0.0] * (FEATURE_DIM - 1),
        [float("nan")] + [0.0] * (FEATURE_DIM - 1),
        [float("inf")] + [0.0] * (FEATURE_DIM - 1),
        ["0"] + [0.0] * (FEATURE_DIM - 1),
        "0" * FEATURE_DIM,
        (x for x in [0.0] * FEATURE_DIM),
        {i: 0.0 for i in range(FEATURE_DIM)},
        None,
    ],
    ids=["short", "long", "bool", "nan", "inf", "str-item", "str", "generator", "dict", "none"],
)
def test_invalid_feature_vectors_are_refused(features):
    with pytest.raises(ValueError):
        _example(features)


def test_features_cannot_be_reassigned():
    example = _example([0.0] * FEATURE_DIM)
    with pytest.raises(AttributeError):
        example.features = [float("nan")]


# F. Normalization never returns a non-finite value.


@pytest.mark.parametrize(
    "value,mean,scale",
    [(1.0, 0.0, 5e-324), (1e308, -1e308, 1.0), (-1e308, 1e308, 1.0), (1e300, 0.0, 1e-300)],
)
def test_normalization_overflow_raises(value, mean, scale):
    spec = NormalizationSpec("p1", (mean,) * FEATURE_DIM, (scale,) * FEATURE_DIM, "train")
    with pytest.raises(ValueError):
        apply_normalization((value,) * FEATURE_DIM, spec)


def test_one_overflowing_channel_rejects_the_whole_vector():
    spec = NormalizationSpec("p1", (0.0,) * FEATURE_DIM, (1.0,) * (FEATURE_DIM - 1) + (5e-324,), "train")
    with pytest.raises(ValueError):
        apply_normalization((1.0,) * FEATURE_DIM, spec)


def test_ordinary_normalization_is_unchanged():
    mean = tuple(float(i) for i in range(FEATURE_DIM))
    scale = tuple(2.0 for _ in range(FEATURE_DIM))
    spec = NormalizationSpec("p1", mean, scale, "calibration")
    vector = tuple(float(i) + 4.0 for i in range(FEATURE_DIM))
    assert apply_normalization(vector, spec) == (2.0,) * FEATURE_DIM
    assert spec.mean == mean and spec.scale == scale
    tiny = NormalizationSpec("p1", (0.0,) * FEATURE_DIM, (1e-300,) * FEATURE_DIM, "train")
    assert apply_normalization((1e-10,) * FEATURE_DIM, tiny) == pytest.approx((1e290,) * FEATURE_DIM)


@pytest.mark.parametrize(
    "vector",
    [
        (0.0,) * (FEATURE_DIM - 1),
        (float("nan"),) + (0.0,) * (FEATURE_DIM - 1),
        (True,) + (0.0,) * (FEATURE_DIM - 1),
    ],
)
def test_normalization_refuses_invalid_vectors(vector):
    spec = NormalizationSpec("p1", (0.0,) * FEATURE_DIM, (1.0,) * FEATURE_DIM, "train")
    with pytest.raises(ValueError):
        apply_normalization(vector, spec)
