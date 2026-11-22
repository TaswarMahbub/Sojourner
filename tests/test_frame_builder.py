"""Checks perception/frame_builder.py against the contract oracle.

Inputs are plain Python data built here: no camera, no model, no MediaPipe,
OpenCV or numpy. This file runs where only pytest is installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from perception import frame_builder
from perception.frame_builder import make_feature_message, mirror_feature_names
from tests.contract_schema import BLENDSHAPES, SCHEMA, validate_feature

T_MS = 1795276800000
REPO_ROOT = Path(__file__).resolve().parent.parent


def full_scores(value: float = 0.25) -> dict[str, float]:
    return {name: value for name in BLENDSHAPES}


def valid_matrix(**overrides) -> list[list[float]]:
    params = dict(yaw_deg=10.0, pitch_deg=-5.0, roll_deg=3.0, scale=1.0, tx_cm=2.0, ty_cm=-1.0, tz_cm=-45.0)
    params.update(overrides)
    return frame_builder.build_matrix(**params)


def build(face_count=1, blendshapes=None, matrix=None) -> dict:
    """make_feature_message, asserting the contract validator accepts it."""
    message = make_feature_message(T_MS, face_count, blendshapes, matrix)
    assert validate_feature(message) == []
    assert validate_feature(json.loads(json.dumps(message, allow_nan=False))) == []
    return message


def reasons(face_count=1, blendshapes=None, matrix=None) -> list[str]:
    return frame_builder.assess(face_count, blendshapes, matrix).reasons


# --- Face count ---------------------------------------------------------------


@pytest.mark.parametrize("face_count", [0, 2])
def test_no_face_or_two_faces_is_lost(face_count):
    message = build(face_count, full_scores(), valid_matrix())
    assert message["tracking_state"] == "lost"
    assert message["features"] == {}
    assert message["head_pose"] is None
    assert message["confidence"] is None


def test_lost_with_no_detector_output_at_all():
    message = build(0, None, None)
    assert message["tracking_state"] == "lost"
    assert message["features"] == {}


# --- Tracking -------------------------------------------------------------------


def test_one_face_complete_and_valid_is_tracking():
    matrix = valid_matrix()
    message = build(1, full_scores(), matrix)
    assert message["tracking_state"] == "tracking"
    assert message["confidence"] is None
    assert message["schema_version"] == SCHEMA
    assert message["timestamp_ms"] == T_MS
    assert set(message["features"]) == set(BLENDSHAPES)
    assert message["head_pose"]["matrix"] == matrix
    assert abs(message["head_pose"]["yaw_deg"] - 10.0) <= 0.1
    assert abs(message["head_pose"]["pitch_deg"] + 5.0) <= 0.1
    assert abs(message["head_pose"]["roll_deg"] - 3.0) <= 0.1
    assert reasons(1, full_scores(), matrix) == []


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_scores_on_the_range_bounds_are_tracking(value):
    assert build(1, full_scores(value), valid_matrix())["tracking_state"] == "tracking"


def test_bounds_of_translation_are_inclusive():
    for tz_cm in (-5.0, -200.0):
        assert build(1, full_scores(), valid_matrix(tz_cm=tz_cm))["tracking_state"] == "tracking"


def test_scaled_matrix_is_still_tracking():
    assert build(1, full_scores(), valid_matrix(scale=1.4))["tracking_state"] == "tracking"


def test_tracking_message_does_not_alias_the_input_matrix():
    matrix = valid_matrix()
    message = build(1, full_scores(), matrix)
    matrix[0][3] = 999.0
    assert message["head_pose"]["matrix"][0][3] == 2.0


# --- Partial: blendshapes -------------------------------------------------------


def assert_partial(message: dict) -> None:
    assert message["tracking_state"] == "partial"
    assert message["head_pose"] is None
    assert message["confidence"] is None


def test_fifty_one_names_is_partial():
    scores = full_scores()
    del scores["jawOpen"]
    message = build(1, scores, valid_matrix())
    assert_partial(message)
    assert set(message["features"]) == set(scores)
    assert "1 of 52 blendshapes missing" in reasons(1, scores, valid_matrix())


def test_a_score_of_one_point_five_is_partial_and_is_not_published():
    scores = full_scores()
    scores["mouthSmileLeft"] = 1.5
    message = build(1, scores, valid_matrix())
    assert_partial(message)
    assert "mouthSmileLeft" not in message["features"]
    assert len(message["features"]) == 51
    assert any("mouthSmileLeft" in reason for reason in reasons(1, scores, valid_matrix()))


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf"), True, "0.5", None])
def test_other_bad_scores_are_partial(bad):
    scores = full_scores()
    scores["browInnerUp"] = bad
    message = build(1, scores, valid_matrix())
    assert_partial(message)
    assert "browInnerUp" not in message["features"]


def test_unknown_blendshape_name_is_partial_and_dropped():
    scores = full_scores()
    scores["tongueOut"] = 0.5
    message = build(1, scores, valid_matrix())
    assert_partial(message)
    assert "tongueOut" not in message["features"]


def test_one_face_with_no_blendshapes_is_partial_with_empty_features():
    message = build(1, None, valid_matrix())
    assert_partial(message)
    assert message["features"] == {}


# --- Partial: pose --------------------------------------------------------------


def test_column_norms_three_percent_apart_is_partial():
    matrix = valid_matrix()
    for r in range(3):
        matrix[r][0] *= 1.03
    norms = frame_builder.column_norms(matrix)
    assert max(norms) / min(norms) == pytest.approx(1.03)
    message = build(1, full_scores(), matrix)
    assert_partial(message)
    assert len(message["features"]) == 52
    assert any("column norms" in reason for reason in reasons(1, full_scores(), matrix))


@pytest.mark.parametrize("tz_cm", [40.0, -300.0])
def test_translation_z_outside_range_is_partial(tz_cm):
    matrix = valid_matrix(tz_cm=tz_cm)
    assert_partial(build(1, full_scores(), matrix))
    assert any("translation z" in reason for reason in reasons(1, full_scores(), matrix))


@pytest.mark.parametrize("axis", ["tx_cm", "ty_cm"])
def test_translation_x_or_y_outside_range_is_partial(axis):
    assert_partial(build(1, full_scores(), valid_matrix(**{axis: 100.5})))


def test_yaw_beyond_ninety_degrees_is_partial():
    matrix = valid_matrix(yaw_deg=120.0, pitch_deg=0.0, roll_deg=0.0)
    assert frame_builder.extract_angles(matrix)[0] == pytest.approx(120.0)
    assert_partial(build(1, full_scores(), matrix))
    assert any(reason.startswith("yaw") for reason in reasons(1, full_scores(), matrix))


@pytest.mark.parametrize("overrides", [{"pitch_deg": 61.0}, {"pitch_deg": -61.0}, {"roll_deg": 46.0}, {"roll_deg": -46.0}])
def test_pitch_or_roll_outside_range_is_partial(overrides):
    assert_partial(build(1, full_scores(), valid_matrix(**overrides)))


def test_last_row_not_exactly_0001_is_partial():
    matrix = valid_matrix()
    matrix[3] = [0.0, 0.0, 0.0, 0.9]
    assert_partial(build(1, full_scores(), matrix))
    assert any("last row" in reason for reason in reasons(1, full_scores(), matrix))


def test_missing_matrix_is_partial():
    assert_partial(build(1, full_scores(), None))


def test_zero_matrix_is_partial():
    zero = [[0.0] * 4 for _ in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    assert_partial(build(1, full_scores(), zero))


@pytest.mark.parametrize(
    "matrix",
    [
        [[1.0, 0.0, 0.0, 0.0]] * 3,
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]],
        [[float("nan"), 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, -45.0], [0.0, 0.0, 0.0, 1.0]],
        "not a matrix",
    ],
)
def test_malformed_matrix_is_partial(matrix):
    assert_partial(build(1, full_scores(), matrix))


def test_every_failing_check_is_reported():
    scores = full_scores()
    del scores["_neutral"]
    matrix = valid_matrix(yaw_deg=120.0, tz_cm=40.0)
    matrix[3] = [0.0, 0.0, 0.0, 0.9]
    found = reasons(1, scores, matrix)
    assert len(found) == 4


# --- Mirroring ------------------------------------------------------------------


SIDED = [name for name in BLENDSHAPES if name.endswith(("Left", "Right"))]
UNSIDED = [name for name in BLENDSHAPES if name not in SIDED]


def test_contract_names_pair_up_left_with_right():
    left = {name[: -len("Left")] for name in SIDED if name.endswith("Left")}
    right = {name[: -len("Right")] for name in SIDED if name.endswith("Right")}
    assert left == right and len(left) == 20
    assert len(UNSIDED) == 12


def test_mirror_swaps_every_left_right_pair():
    scores = {name: index / 100.0 for index, name in enumerate(BLENDSHAPES)}
    mirrored = mirror_feature_names(scores)
    assert set(mirrored) == set(BLENDSHAPES)
    for name in SIDED:
        if name.endswith("Left"):
            partner = name[: -len("Left")] + "Right"
        else:
            partner = name[: -len("Right")] + "Left"
        assert mirrored[partner] == scores[name], name


def test_mirror_swaps_jaw_and_mouth_direction_names():
    mirrored = mirror_feature_names({"jawLeft": 0.1, "jawRight": 0.2, "mouthLeft": 0.3, "mouthRight": 0.4})
    assert mirrored == {"jawRight": 0.1, "jawLeft": 0.2, "mouthRight": 0.3, "mouthLeft": 0.4}


def test_mirror_is_its_own_inverse():
    scores = {name: index / 100.0 for index, name in enumerate(BLENDSHAPES)}
    assert mirror_feature_names(mirror_feature_names(scores)) == scores


@pytest.mark.parametrize("name", ["jawOpen", "mouthFunnel", "_neutral"])
def test_mirror_leaves_unsided_names_untouched(name):
    scores = full_scores()
    scores[name] = 0.77
    assert mirror_feature_names(scores)[name] == 0.77


def test_mirror_leaves_every_unsided_contract_name_in_place():
    scores = {name: index / 100.0 for index, name in enumerate(BLENDSHAPES)}
    mirrored = mirror_feature_names(scores)
    for name in UNSIDED:
        assert mirrored[name] == scores[name]


def test_mirrored_complete_set_still_tracks():
    message = build(1, mirror_feature_names(full_scores()), valid_matrix())
    assert message["tracking_state"] == "tracking"


# --- Mirroring the pose -----------------------------------------------------------

POSES = [
    dict(yaw_deg=10.0, pitch_deg=-5.0, roll_deg=3.0, scale=1.0, tx_cm=2.0, ty_cm=-1.0, tz_cm=-45.0),
    dict(yaw_deg=-40.0, pitch_deg=25.0, roll_deg=-20.0, scale=1.2, tx_cm=-7.5, ty_cm=4.0, tz_cm=-60.0),
    dict(yaw_deg=85.0, pitch_deg=55.0, roll_deg=40.0, scale=0.9, tx_cm=30.0, ty_cm=-20.0, tz_cm=-120.0),
    dict(yaw_deg=0.0, pitch_deg=0.0, roll_deg=0.0, scale=1.0, tx_cm=0.0, ty_cm=0.0, tz_cm=-45.0),
]


def pose_pair(params: dict) -> tuple[dict, dict]:
    """The unmirrored and mirrored head_pose objects for one tracking frame."""
    matrix = frame_builder.build_matrix(**params)
    plain = build(1, full_scores(), matrix)
    mirrored = build(1, full_scores(), frame_builder.mirror_matrix(matrix))
    assert plain["tracking_state"] == mirrored["tracking_state"] == "tracking"
    return plain["head_pose"], mirrored["head_pose"]


@pytest.mark.parametrize("params", POSES)
def test_mirrored_pose_negates_yaw_roll_and_x(params):
    plain, mirrored = pose_pair(params)
    assert mirrored["yaw_deg"] == pytest.approx(-plain["yaw_deg"], abs=1e-9)
    assert mirrored["roll_deg"] == pytest.approx(-plain["roll_deg"], abs=1e-9)
    assert mirrored["matrix"][0][3] == pytest.approx(-plain["matrix"][0][3], abs=1e-12)


@pytest.mark.parametrize("params", POSES)
def test_mirrored_pose_keeps_pitch_y_and_z(params):
    plain, mirrored = pose_pair(params)
    assert mirrored["pitch_deg"] == plain["pitch_deg"]
    assert mirrored["matrix"][1][3] == plain["matrix"][1][3]
    assert mirrored["matrix"][2][3] == plain["matrix"][2][3]


@pytest.mark.parametrize("params", POSES)
def test_mirrored_angles_match_the_published_matrix(params):
    _, mirrored = pose_pair(params)
    extracted = frame_builder.extract_angles(mirrored["matrix"])
    published = (mirrored["yaw_deg"], mirrored["pitch_deg"], mirrored["roll_deg"])
    for got, want in zip(published, extracted):
        assert abs(got - want) <= 0.1


@pytest.mark.parametrize("params", POSES)
def test_mirrored_angles_are_the_negated_inputs(params):
    _, mirrored = pose_pair(params)
    assert abs(mirrored["yaw_deg"] + params["yaw_deg"]) <= 0.1
    assert abs(mirrored["pitch_deg"] - params["pitch_deg"]) <= 0.1
    assert abs(mirrored["roll_deg"] + params["roll_deg"]) <= 0.1


@pytest.mark.parametrize("params", POSES)
def test_mirror_matrix_twice_returns_the_original(params):
    matrix = frame_builder.build_matrix(**params)
    assert frame_builder.mirror_matrix(frame_builder.mirror_matrix(matrix)) == matrix


@pytest.mark.parametrize("params", POSES)
def test_mirror_matrix_keeps_scale_and_last_row(params):
    matrix = frame_builder.build_matrix(**params)
    mirrored = frame_builder.mirror_matrix(matrix)
    assert mirrored[3] == [0.0, 0.0, 0.0, 1.0]
    assert "-0.0" not in json.dumps(mirrored[3])
    assert frame_builder.column_norms(mirrored) == pytest.approx(frame_builder.column_norms(matrix))


def test_mirror_matrix_does_not_modify_its_input():
    matrix = valid_matrix()
    snapshot = [list(row) for row in matrix]
    frame_builder.mirror_matrix(matrix)
    assert matrix == snapshot


def test_mirror_matrix_passes_a_malformed_matrix_through_to_partial():
    bad = [[1.0, 0.0, 0.0, 0.0]] * 3
    assert frame_builder.mirror_matrix(bad) is bad
    assert_partial(build(1, full_scores(), frame_builder.mirror_matrix(bad)))


def test_unmirror_corrects_names_and_pose_together():
    """The camera source's correction step, exercised without a camera."""
    from perception.mediapipe_source import unmirror

    scores = full_scores()
    scores["eyeBlinkLeft"], scores["eyeBlinkRight"] = 0.9, 0.1
    matrix = valid_matrix()
    names, pose_matrix = unmirror(scores, matrix)
    message = build(1, names, pose_matrix)
    assert message["tracking_state"] == "tracking"
    assert message["features"]["eyeBlinkRight"] == 0.9
    assert message["features"]["eyeBlinkLeft"] == 0.1
    assert abs(message["head_pose"]["yaw_deg"] + 10.0) <= 0.1
    assert abs(message["head_pose"]["roll_deg"] + 3.0) <= 0.1
    assert message["head_pose"]["matrix"][0][3] == -2.0
    assert unmirror(None, None) == (None, None)


# --- Import isolation -----------------------------------------------------------


def test_perception_modules_import_without_camera_packages():
    """Importing the camera source and publisher must not pull in cv2,
    mediapipe or numpy, so the contract tests run where only pytest is
    installed."""
    code = (
        "import sys\n"
        "import perception.frame_builder, perception.fake_stream\n"
        "import perception.mediapipe_source, perception.publish\n"
        "loaded = sorted({'cv2', 'mediapipe', 'numpy', 'websockets'} & set(sys.modules))\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
