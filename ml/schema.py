"""Schema 4 checks for camera features and gesture classifications.

The rules are the camera-feature and gesture sections of ``docs/contracts.md``.
This module does not accept motor commands. A message is not trusted because
it reached this function; callers still have to decide whether a valid frame
is actionable.

Acceptance is the contract's acceptance. ``tests/test_ml_pipeline.py`` compares
these results with the contract oracle in ``tests/contract_schema.py``.
"""

from __future__ import annotations

import math
import re
from typing import Mapping

SCHEMA_VERSION = "4"
SAFE_INT_MAX = 9007199254740991
MODEL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

# Face Landmarker order. Index 0, ``_neutral``, is not a model input.
BLENDSHAPES = (
    "_neutral",
    "browDownLeft",
    "browDownRight",
    "browInnerUp",
    "browOuterUpLeft",
    "browOuterUpRight",
    "cheekPuff",
    "cheekSquintLeft",
    "cheekSquintRight",
    "eyeBlinkLeft",
    "eyeBlinkRight",
    "eyeLookDownLeft",
    "eyeLookDownRight",
    "eyeLookInLeft",
    "eyeLookInRight",
    "eyeLookOutLeft",
    "eyeLookOutRight",
    "eyeLookUpLeft",
    "eyeLookUpRight",
    "eyeSquintLeft",
    "eyeSquintRight",
    "eyeWideLeft",
    "eyeWideRight",
    "jawForward",
    "jawLeft",
    "jawOpen",
    "jawRight",
    "mouthClose",
    "mouthDimpleLeft",
    "mouthDimpleRight",
    "mouthFrownLeft",
    "mouthFrownRight",
    "mouthFunnel",
    "mouthLeft",
    "mouthLowerDownLeft",
    "mouthLowerDownRight",
    "mouthPressLeft",
    "mouthPressRight",
    "mouthPucker",
    "mouthRight",
    "mouthRollLower",
    "mouthRollUpper",
    "mouthShrugLower",
    "mouthShrugUpper",
    "mouthSmileLeft",
    "mouthSmileRight",
    "mouthStretchLeft",
    "mouthStretchRight",
    "mouthUpperUpLeft",
    "mouthUpperUpRight",
    "noseSneerLeft",
    "noseSneerRight",
)
MODEL_BLENDSHAPES = BLENDSHAPES[1:]

FEATURE_FIELDS = frozenset(
    {
        "schema_version",
        "timestamp_ms",
        "tracking_state",
        "features",
        "head_pose",
        "confidence",
    }
)
GESTURE_FIELDS = frozenset(
    {
        "schema_version",
        "timestamp_ms",
        "source_timestamp_ms",
        "window_start_timestamp_ms",
        "window_end_timestamp_ms",
        "window_frame_count",
        "label",
        "confidence",
        "model_version",
    }
)
POSE_FIELDS = frozenset({"matrix", "yaw_deg", "pitch_deg", "roll_deg"})

# Schema 4: each column norm within 1 percent of their mean ``s``.
SCALE_TOLERANCE = 0.01
# Implementation tolerances for "a uniform scale and a rotation". Schema 4 does
# not give numbers for these. After removing scale, each pair of unit column
# directions must have |cos| <= 1e-3 (within about 0.057 degrees of a right
# angle), and the determinant of the unit directions must be >= 1 - 1e-3.
# That rejects reflections (det -1), singular matrices (det 0), and skewed
# columns whose norms happen to agree. MediaPipe's transform is a scaled
# orthogonal Procrustes solution, so a real pose sits far inside both bounds.
ORTHOGONALITY_TOLERANCE = 1e-3
DETERMINANT_TOLERANCE = 1e-3
ANGLE_TOLERANCE_DEG = 0.1
ANGLE_RANGES = (
    ("yaw_deg", -90.0, 90.0),
    ("pitch_deg", -60.0, 60.0),
    ("roll_deg", -45.0, 45.0),
)
_BLENDSHAPE_SET = frozenset(BLENDSHAPES)


def is_timestamp(value) -> bool:
    """True for an exact integer from 0 through the JSON safe-integer maximum."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= SAFE_INT_MAX


def validate_feature(message) -> list[str]:
    """Return Schema 4 errors for one camera feature message. Does not raise."""
    try:
        return _validate_feature(message)
    except Exception:
        return ["rejected"]


def validate_gesture(message) -> list[str]:
    """Return Schema 4 errors for one gesture classification. Does not raise."""
    try:
        return _validate_gesture(message)
    except Exception:
        return ["rejected"]


def is_actionable_feature(message) -> bool:
    """A frame is actionable only when Schema 4 says ``tracking`` is complete.

    ``confidence: null`` stays actionable. ``partial`` and ``lost`` do not.
    """
    if validate_feature(message):
        return False
    return message["tracking_state"] == "tracking"


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unit(value) -> float:
    try:
        return float(value)
    except (OverflowError, ValueError, TypeError):
        return math.nan


def _exact_keys(message, allowed: frozenset[str], errors: list[str]) -> None:
    if not isinstance(message, dict):
        errors.append("message is not an object")
        return
    for name in sorted(set(message) - allowed):
        errors.append(f"unknown field {name}")
    for name in sorted(allowed - set(message)):
        errors.append(f"missing field {name}")


def _score(value, errors: list[str], name: str, low: float, high: float) -> None:
    if not _is_number(value) or not math.isfinite(_unit(value)):
        errors.append(f"{name} is not a finite number")
        return
    if _unit(value) < low or _unit(value) > high:
        errors.append(f"{name} is outside [{low}, {high}]")


def _optional_score(value, errors: list[str], name: str, low: float, high: float) -> None:
    if value is None:
        return
    _score(value, errors, name, low, high)


def _validate_pose(pose) -> list[str]:
    errors: list[str] = []
    if not isinstance(pose, dict):
        return ["head_pose is not an object"]
    _exact_keys(pose, POSE_FIELDS, errors)
    if errors:
        return errors
    matrix = pose["matrix"]
    if (
        not isinstance(matrix, list)
        or len(matrix) != 4
        or any(not isinstance(row, list) or len(row) != 4 for row in matrix)
    ):
        return ["matrix is not 4x4"]
    rows: list[list[float]] = []
    for row in matrix:
        if any(not _is_number(value) or not math.isfinite(_unit(value)) for value in row):
            return ["matrix contains a non-finite number"]
        rows.append([_unit(value) for value in row])
    if rows[3] != [0.0, 0.0, 0.0, 1.0]:
        errors.append("matrix last row is not [0, 0, 0, 1]")
    rotation, geometry_error = _rotation_part(rows)
    if rotation is None:
        errors.append(geometry_error)
        return errors
    extracted_angles = {
        "yaw_deg": math.degrees(math.atan2(rotation[0][2], rotation[2][2])),
        "pitch_deg": math.degrees(math.asin(max(-1.0, min(1.0, rotation[1][2])))),
        "roll_deg": math.degrees(math.atan2(rotation[1][0], rotation[1][1])),
    }
    for name, low, high in ANGLE_RANGES:
        extracted = extracted_angles[name]
        published = pose[name]
        if not _is_number(published) or not math.isfinite(_unit(published)):
            errors.append(f"{name} is not a finite number")
            continue
        value = _unit(published)
        # The 0.1 degree match is a consistency check. It never widens the range.
        if abs(value - extracted) > ANGLE_TOLERANCE_DEG:
            errors.append(f"{name} does not match the matrix")
        if extracted < low or extracted > high:
            errors.append(f"{name} from the matrix is outside [{low}, {high}]")
        if value < low or value > high:
            errors.append(f"published {name} is outside [{low}, {high}]")
    x_cm, y_cm, z_cm = rows[0][3], rows[1][3], rows[2][3]
    if not (-100.0 <= x_cm <= 100.0 and -100.0 <= y_cm <= 100.0):
        errors.append("translation x or y is outside [-100, 100] cm")
    if not (-200.0 <= z_cm <= -5.0):
        errors.append("translation z is outside [-200, -5] cm")
    return errors


def _rotation_part(rows):
    """Return ``(R, None)`` for a uniform scale times a proper rotation.

    Returns ``(None, reason)`` otherwise. ``s`` and ``R = A / s`` follow
    Schema 4, evaluated on ``A / max|A|`` so that squares of large or small
    finite entries cannot overflow or underflow. The ratios the contract
    checks do not change under that positive rescaling. When entries are so
    small that they are subnormal, precision is lost and the checks below may
    reject the pose; that fails closed.
    """
    a = [[rows[r][c] for c in range(3)] for r in range(3)]
    peak = max(abs(value) for row in a for value in row)
    if not math.isfinite(peak) or peak <= 0:
        return None, "pose scale is not positive"
    b = [[value / peak for value in row] for row in a]
    norms = [math.hypot(b[0][c], b[1][c], b[2][c]) for c in range(3)]
    scale = (norms[0] + norms[1] + norms[2]) / 3.0
    if not math.isfinite(scale) or scale <= 0:
        return None, "pose scale is not positive"
    if any(abs(norm - scale) / scale > SCALE_TOLERANCE for norm in norms):
        return None, "pose column norms disagree by more than 1 percent"
    units = [[b[r][c] / norms[c] for r in range(3)] for c in range(3)]
    for i, j in ((0, 1), (0, 2), (1, 2)):
        cosine = math.fsum(units[i][k] * units[j][k] for k in range(3))
        if not abs(cosine) <= ORTHOGONALITY_TOLERANCE:
            return None, "pose matrix columns are not orthogonal"
    u0, u1, u2 = units
    determinant = math.fsum(
        (
            u0[0] * (u1[1] * u2[2] - u1[2] * u2[1]),
            -u0[1] * (u1[0] * u2[2] - u1[2] * u2[0]),
            u0[2] * (u1[0] * u2[1] - u1[1] * u2[0]),
        )
    )
    if not determinant >= 1.0 - DETERMINANT_TOLERANCE:
        return None, "pose matrix is not a proper rotation"
    return [[b[r][c] / scale for c in range(3)] for r in range(3)], None


def _features(value, errors: list[str], state: str) -> None:
    if not isinstance(value, dict):
        errors.append("features is not an object")
        return
    for name in sorted(set(value) - _BLENDSHAPE_SET):
        errors.append(f"unknown feature {name}")
    if state == "tracking":
        for name in sorted(_BLENDSHAPE_SET - set(value)):
            errors.append(f"missing feature {name}")
    elif state == "lost" and value:
        errors.append("lost features must be empty")
    for name, score in value.items():
        if name not in _BLENDSHAPE_SET:
            continue
        _score(score, errors, name, 0.0, 1.0)


def _validate_feature(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, FEATURE_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA_VERSION:
        errors.append("schema_version is not 4")
    if not is_timestamp(message["timestamp_ms"]):
        errors.append("timestamp_ms is not a timestamp")
    state = message["tracking_state"]
    if state not in ("tracking", "partial", "lost"):
        errors.append("tracking_state is invalid")
        return errors
    _features(message["features"], errors, state)
    pose = message["head_pose"]
    confidence = message["confidence"]
    if state == "tracking":
        errors.extend(_validate_pose(pose))
        _optional_score(confidence, errors, "confidence", 0.0, 1.0)
        if confidence is not None and _is_number(confidence) and _unit(confidence) <= 0:
            errors.append("tracking confidence must be null or in (0, 1]")
    elif state == "partial":
        if pose is not None:
            errors.append("partial head_pose must be null")
        _optional_score(confidence, errors, "confidence", 0.0, 1.0)
    else:
        if pose is not None:
            errors.append("lost head_pose must be null")
        if confidence is not None:
            errors.append("lost confidence must be null")
    return errors


def _validate_gesture(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, GESTURE_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA_VERSION:
        errors.append("schema_version is not 4")
    names = (
        "timestamp_ms",
        "source_timestamp_ms",
        "window_start_timestamp_ms",
        "window_end_timestamp_ms",
    )
    for name in names:
        if not is_timestamp(message[name]):
            errors.append(f"{name} is not a timestamp")
    count = message["window_frame_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        errors.append("window_frame_count must be an integer >= 1")
    if all(is_timestamp(message[name]) for name in names):
        if message["window_start_timestamp_ms"] > message["window_end_timestamp_ms"]:
            errors.append("window starts after it ends")
        if message["source_timestamp_ms"] != message["window_end_timestamp_ms"]:
            errors.append("source_timestamp_ms is not the window end")
        if message["timestamp_ms"] < message["window_end_timestamp_ms"]:
            errors.append("classification time is before the window end")
        if count == 1 and (
            message["window_start_timestamp_ms"] != message["window_end_timestamp_ms"]
        ):
            errors.append("a one-frame window must start and end at the source frame")
    label = message["label"]
    if not isinstance(label, str) or label == "":
        errors.append("label is empty")
    elif label not in ("neutral", "unknown"):
        errors.append("label has no reviewed list entry")
    version = message["model_version"]
    if not isinstance(version, str) or MODEL_VERSION.fullmatch(version) is None:
        errors.append("model_version is invalid")
    _score(message["confidence"], errors, "confidence", 0.0, 1.0)
    return errors


def published_angles(message: Mapping) -> tuple[float, float, float]:
    """Return yaw, pitch, and roll from a feature message that already validated."""
    pose = message["head_pose"]
    return (_unit(pose["yaw_deg"]), _unit(pose["pitch_deg"]), _unit(pose["roll_deg"]))
