"""Pure assembly of camera feature messages, message 1 of docs/contracts.md.

Every function here takes and returns plain Python data: ints, floats,
strings, lists and dicts. Nothing here touches a camera, a model, MediaPipe,
OpenCV or numpy, so this module imports on the standard library alone and
the contract tests can exercise it where only pytest is installed.

Both the synthetic source (fake_stream.py) and the camera source
(mediapipe_source.py) build their messages through make_feature_message, so
the tracking-state decision and the pose rules live in one place.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The contract oracle lives in the tests package at the repository root.
# Running a perception file as a script puts only perception/ on sys.path,
# so the repository root is added here to make `tests.contract_schema`
# importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.contract_schema import BLENDSHAPES, SCHEMA  # noqa: E402

# Column norms of the upper-left 3x3 must agree within this fraction of
# their mean, per the contract's Angles subsection.
SCALE_TOLERANCE = 0.01

# Published angles are rounded to this many decimal places. The contract
# allows 0.1 degrees of disagreement with the extraction.
DECIMALS = 6

# Valid ranges for a tracking pose, from the contract's Head pose section.
# Angles in degrees, translation in centimeters, both bounds inclusive.
YAW_RANGE_DEG = (-90.0, 90.0)
PITCH_RANGE_DEG = (-60.0, 60.0)
ROLL_RANGE_DEG = (-45.0, 45.0)
XY_RANGE_CM = (-100.0, 100.0)
Z_RANGE_CM = (-200.0, -5.0)

LAST_ROW = [0.0, 0.0, 0.0, 1.0]
BLENDSHAPE_SET = frozenset(BLENDSHAPES)


# --- Rotation and matrix -------------------------------------------------


def rot_y(yaw_rad: float) -> list[list[float]]:
    """Ry(y) exactly as written in the contract. Angle in radians."""
    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    return [
        [c, 0.0, s],
        [0.0, 1.0, 0.0],
        [-s, 0.0, c],
    ]


def rot_x_neg_pitch(pitch_rad: float) -> list[list[float]]:
    """Rx(-p) exactly as written in the contract. Pitch p in radians."""
    c, s = math.cos(pitch_rad), math.sin(pitch_rad)
    return [
        [1.0, 0.0, 0.0],
        [0.0, c, s],
        [0.0, -s, c],
    ]


def rot_z(roll_rad: float) -> list[list[float]]:
    """Rz(r) exactly as written in the contract. Angle in radians."""
    c, s = math.cos(roll_rad), math.sin(roll_rad)
    return [
        [c, -s, 0.0],
        [s, c, 0.0],
        [0.0, 0.0, 1.0],
    ]


def matmul3(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Product a @ b of two 3x3 matrices given as lists of rows."""
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def build_matrix(
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float,
    scale: float,
    tx_cm: float,
    ty_cm: float,
    tz_cm: float,
) -> list[list[float]]:
    """Facial transformation matrix as four rows, matrix[row][col].

    yaw_deg, pitch_deg and roll_deg are degrees. The rotation is
    R = Ry(yaw) @ Rx(-pitch) @ Rz(roll), the contract's composition, which
    its extraction inverts for pitch inside (-90, 90). scale is a positive,
    unitless uniform scale applied to all three columns of R. tx_cm, ty_cm
    and tz_cm are the translation in centimeters, placed in the last column.
    The last row is exactly [0, 0, 0, 1].
    """
    rotation = matmul3(
        matmul3(rot_y(math.radians(yaw_deg)), rot_x_neg_pitch(math.radians(pitch_deg))),
        rot_z(math.radians(roll_deg)),
    )
    translation = (tx_cm, ty_cm, tz_cm)
    rows = [
        [scale * rotation[r][c] for c in range(3)] + [translation[r]]
        for r in range(3)
    ]
    rows.append(list(LAST_ROW))
    return rows


def column_norms(matrix: list[list[float]]) -> tuple[float, float, float]:
    """L2 norms of the three columns of the upper-left 3x3. Unitless."""
    n0, n1, n2 = (
        math.sqrt(sum(matrix[r][c] ** 2 for r in range(3))) for c in range(3)
    )
    return n0, n1, n2


def scale_problem(matrix: list[list[float]]) -> str | None:
    """Why the upper-left 3x3 has no usable uniform scale, or None if it has.

    s is the mean of the three column norms. The contract requires s finite
    and positive and every norm within SCALE_TOLERANCE (1 percent) of s.
    """
    norms = column_norms(matrix)
    s = sum(norms) / 3.0
    if not math.isfinite(s) or s <= 0.0:
        return f"matrix scale {s} is not positive and finite"
    spread = max(abs(n - s) for n in norms) / s
    if spread > SCALE_TOLERANCE:
        return f"column norms disagree by {spread:.4f} of their mean"
    return None


def extract_angles(matrix: list[list[float]]) -> tuple[float, float, float]:
    """Return (yaw_deg, pitch_deg, roll_deg) from a 4x4 row-layout matrix.

    Implements the contract's Angles subsection: s is the mean of the three
    column norms of the upper-left 3x3 A, every norm must lie within 1
    percent of s, and R = A / s. Raises ValueError when the scale is not
    positive and finite or the norms disagree, because such a matrix cannot
    be published as a tracking pose. Results are degrees: yaw and roll in
    (-180, 180], pitch in [-90, 90].
    """
    problem = scale_problem(matrix)
    if problem is not None:
        raise ValueError(problem)
    s = sum(column_norms(matrix)) / 3.0
    r = [[matrix[i][j] / s for j in range(3)] for i in range(3)]
    yaw_deg = math.degrees(math.atan2(r[0][2], r[2][2]))
    pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, r[1][2]))))
    roll_deg = math.degrees(math.atan2(r[1][0], r[1][1]))
    return yaw_deg, pitch_deg, roll_deg


# --- Validation of plain inputs ---------------------------------------------


def is_finite_number(value) -> bool:
    """True for an int or float that is finite. Booleans are not numbers."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def in_range(value: float, bounds: tuple[float, float]) -> bool:
    """True when bounds[0] <= value <= bounds[1]."""
    return bounds[0] <= value <= bounds[1]


def matrix_shape_problem(matrix) -> str | None:
    """Why matrix is not four rows of four finite numbers, or None."""
    if not isinstance(matrix, list) or len(matrix) != 4:
        return "matrix is not a list of 4 rows"
    for row in matrix:
        if not isinstance(row, list) or len(row) != 4:
            return "matrix row is not a list of 4 numbers"
        if not all(is_finite_number(value) for value in row):
            return "matrix contains a non-finite or non-numeric value"
    return None


def angle_problems(yaw_deg: float, pitch_deg: float, roll_deg: float) -> list[str]:
    """Range failures of extracted angles, in degrees. Empty when all pass."""
    problems = []
    for name, value, bounds in (
        ("yaw", yaw_deg, YAW_RANGE_DEG),
        ("pitch", pitch_deg, PITCH_RANGE_DEG),
        ("roll", roll_deg, ROLL_RANGE_DEG),
    ):
        if not in_range(value, bounds):
            problems.append(f"{name} {value:.3f} deg is outside [{bounds[0]:g}, {bounds[1]:g}]")
    return problems


def translation_problems(matrix: list[list[float]]) -> list[str]:
    """Range failures of the translation column, in centimeters."""
    problems = []
    for name, value, bounds in (
        ("x", matrix[0][3], XY_RANGE_CM),
        ("y", matrix[1][3], XY_RANGE_CM),
        ("z", matrix[2][3], Z_RANGE_CM),
    ):
        if not in_range(value, bounds):
            problems.append(f"translation {name} {value:.3f} cm is outside [{bounds[0]:g}, {bounds[1]:g}]")
    return problems


def pose_problems(matrix) -> list[str]:
    """Every reason matrix cannot be a tracking pose. Empty when it can.

    Checks, in the contract's terms: shape and finiteness, last row exactly
    [0, 0, 0, 1], uniform scale within 1 percent, extracted yaw in
    [-90, 90], pitch in [-60, 60], roll in [-45, 45] degrees, translation
    x and y in [-100, 100] and z in [-200, -5] centimeters.
    """
    if matrix is None:
        return ["no transformation matrix in the result"]
    shape = matrix_shape_problem(matrix)
    if shape is not None:
        return [shape]
    problems = []
    if [float(value) for value in matrix[3]] != LAST_ROW:
        problems.append(f"matrix last row {matrix[3]} is not [0, 0, 0, 1]")
    scale = scale_problem(matrix)
    if scale is not None:
        problems.append(scale)
    else:
        problems.extend(angle_problems(*extract_angles(matrix)))
    problems.extend(translation_problems(matrix))
    return problems


def feature_problems(blendshapes: dict | None) -> list[str]:
    """Every reason blendshapes is not a complete tracking feature set.

    A complete set has exactly the 52 contract names, each a finite number
    in [0, 1]. Empty when complete.
    """
    if blendshapes is None:
        return ["no blendshapes in the result"]
    problems = []
    missing = BLENDSHAPE_SET - set(blendshapes)
    if missing:
        problems.append(f"{len(missing)} of 52 blendshapes missing")
    unknown = set(blendshapes) - BLENDSHAPE_SET
    if unknown:
        problems.append(f"unknown blendshape names {sorted(unknown)}")
    for name in BLENDSHAPES:
        if name in blendshapes and not valid_score(blendshapes[name]):
            problems.append(f"{name} = {blendshapes[name]!r} is outside [0, 1]")
    return problems


def valid_score(value) -> bool:
    """True for a finite number in the blendshape score range [0, 1]."""
    return is_finite_number(value) and 0.0 <= value <= 1.0


def publishable_features(blendshapes: dict | None) -> dict[str, float]:
    """The subset of blendshapes a partial frame may carry.

    Keeps only contract names whose score is a finite number in [0, 1],
    in contract order. Anything else is dropped rather than repaired, so no
    published value is invented or clamped.
    """
    if blendshapes is None:
        return {}
    return {
        name: blendshapes[name]
        for name in BLENDSHAPES
        if name in blendshapes and valid_score(blendshapes[name])
    }


# --- Message assembly --------------------------------------------------------


@dataclass(frozen=True)
class Assessment:
    """The tracking decision for one detector result, before serialization.

    state is "tracking", "partial" or "lost". reasons lists every check that
    kept the frame from tracking, in plain words, and is empty for tracking.
    """

    state: str
    reasons: list[str] = field(default_factory=list)


def assess(face_count: int, blendshapes: dict | None, matrix) -> Assessment:
    """Apply the contract's result-contents table to plain detector output.

    face_count is the number of faces the detector returned. Zero, or more
    than one, is "lost". Exactly one face is "tracking" only when the
    blendshapes are complete and in range and the pose passes every rule in
    pose_problems; otherwise it is "partial".
    """
    if face_count == 0:
        return Assessment("lost", ["no face in the result"])
    if face_count > 1:
        return Assessment("lost", [f"{face_count} faces in the result, expected 1"])
    reasons = feature_problems(blendshapes) + pose_problems(matrix)
    if reasons:
        return Assessment("partial", reasons)
    return Assessment("tracking")


def head_pose_object(matrix: list[list[float]]) -> dict:
    """The contract's head_pose object for a matrix that passed pose_problems.

    Angles are degrees extracted from matrix itself and rounded to DECIMALS
    places, so they agree with the contract's extraction within 0.1 degrees.
    """
    yaw_deg, pitch_deg, roll_deg = extract_angles(matrix)
    return {
        "matrix": [list(row) for row in matrix],
        "yaw_deg": round(yaw_deg, DECIMALS),
        "pitch_deg": round(pitch_deg, DECIMALS),
        "roll_deg": round(roll_deg, DECIMALS),
    }


def assemble_message(timestamp_ms: int, state: str, features: dict, head_pose: dict | None) -> dict:
    """Lay out the six schema 4 fields. confidence is always None.

    The stock Face Landmarker result carries no per-frame score, and the
    contract forbids substituting thresholds, 0 or 1 for one.
    """
    return {
        "schema_version": SCHEMA,
        "timestamp_ms": int(timestamp_ms),
        "tracking_state": state,
        "features": features,
        "head_pose": head_pose,
        "confidence": None,
    }


def make_feature_message(
    timestamp_ms: int,
    face_count: int,
    blendshapes: dict | None,
    matrix: list[list[float]] | None,
) -> dict:
    """A complete schema 4 camera feature message from plain detector data.

    timestamp_ms is the frame's capture time, integer Unix epoch
    milliseconds, UTC. face_count is the number of faces detected.
    blendshapes maps blendshape name to score, nominally in [0, 1], or is
    None when the result had none. matrix is the 4x4 facial transformation
    matrix as a list of rows, translation in centimeters, or None.

    tracking: all 52 scores and the full head_pose object.
    partial: only the scores that are contract names in [0, 1], head_pose None.
    lost: features {} and head_pose None.
    """
    assessment = assess(face_count, blendshapes, matrix)
    if assessment.state == "tracking":
        features = {name: blendshapes[name] for name in BLENDSHAPES}
        pose = head_pose_object(matrix)
    elif assessment.state == "partial":
        features, pose = publishable_features(blendshapes), None
    else:
        features, pose = {}, None
    return assemble_message(timestamp_ms, assessment.state, features, pose)


# --- Mirroring ----------------------------------------------------------------


def mirror_name(name: str) -> str:
    """Swap a trailing "Left" for "Right" or the reverse. Others unchanged."""
    if name.endswith("Left"):
        return name[: -len("Left")] + "Right"
    if name.endswith("Right"):
        return name[: -len("Right")] + "Left"
    return name


def mirror_feature_names(blendshapes: dict) -> dict:
    """Swap every *Left name with its *Right counterpart. Scores unchanged.

    For a detector that was given a horizontally mirrored buffer, this
    restores the contract's subject-relative left and right. Names without
    a side, such as jawOpen, mouthFunnel and _neutral, keep their key. The
    function is its own inverse.
    """
    return {mirror_name(name): score for name, score in blendshapes.items()}


def negate(value: float) -> float:
    """-value, except that zero stays +0.0 so no -0.0 reaches the JSON."""
    return 0.0 - value


def mirror_matrix(matrix: list[list[float]]) -> list[list[float]]:
    """Undo a horizontal mirror of the image on a facial transformation matrix.

    Mirroring about the image vertical is the reflection S = diag(-1, 1, 1)
    in camera X. The face fitted in a mirrored image is S @ M @ S, and S is
    its own inverse, so the same conjugation restores M. Element-wise it
    negates the entries whose row or column (but not both) is X: [0][1],
    [0][2], [0][3], [1][0] and [2][0]. The effect is that translation x
    (cm) changes sign while y and z do not, and the extracted yaw and roll
    (degrees) change sign while pitch does not. The last row is copied
    unchanged. The function is its own inverse.

    A matrix that is not four rows of four finite numbers is returned
    unchanged; make_feature_message classifies it as partial regardless.
    Published angles are always extracted from the matrix that is
    published, so after mirroring they still agree with it.
    """
    if matrix_shape_problem(matrix) is not None:
        return matrix
    mirrored = [list(row) for row in matrix]
    for r, c in ((0, 1), (0, 2), (0, 3), (1, 0), (2, 0)):
        mirrored[r][c] = negate(matrix[r][c])
    return mirrored
