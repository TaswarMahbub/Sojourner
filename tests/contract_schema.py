"""Schema 4 checks for Sojourner messages.

This module is the test oracle for docs/contracts.md. It is not a runtime
service, and it does not talk to motors. Validation returns errors. It does
not raise on hostile numbers or types.
"""

from __future__ import annotations

import math
import re

from tests.control_auth import canonical_velocity

SCHEMA = "4"
CONTROLLER_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAC_HEX = re.compile(r"^[0-9a-f]{64}$")
SAFE_INT_MAX = 9007199254740991
SEQUENCE_MAX = 4294967295
MODEL_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

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

STOP_TASKS = ("idle", "hold", "estop", "fault", "rearm")
MOVE_TASKS = ("manual", "shared")
SAFETY_STOP_TASKS = ("idle", "hold", "estop", "fault")
TASKS = STOP_TASKS + MOVE_TASKS

FEATURE_FIELDS = {
    "schema_version",
    "timestamp_ms",
    "tracking_state",
    "features",
    "head_pose",
    "confidence",
}
GESTURE_FIELDS = {
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
MOTOR_FIELDS = {
    "schema_version",
    "controller_id",
    "boot_nonce",
    "session_id",
    "sequence",
    "freshness_id",
    "rearm_generation",
    "status_challenge",
    "issued_at_ms",
    "expires_at_ms",
    "task_state",
    "linear_mps",
    "angular_rad_s",
    "stop",
    "mac",
}
STATUS_FIELDS = {
    "schema_version",
    "controller_id",
    "timestamp_ms",
    "boot_nonce",
    "session_id",
    "last_sequence",
    "status_sequence",
    "freshness_id",
    "rearm_generation",
    "status_challenge",
    "clock_state",
    "sensor_state",
    "cutoff_open",
    "physical_estop",
    "software_estop_latch",
    "fault_latch",
    "armed",
    "commanding_stop",
    "mac",
}


def is_bool(value) -> bool:
    return isinstance(value, bool)


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_timestamp(value) -> bool:
    return is_int(value) and 0 <= value <= SAFE_INT_MAX


def is_safe_id(value) -> bool:
    return is_int(value) and 1 <= value <= SAFE_INT_MAX


def _exact_keys(message, allowed, errors) -> None:
    if not isinstance(message, dict):
        errors.append("message is not an object")
        return
    extra = set(message) - allowed
    missing = allowed - set(message)
    for name in sorted(extra):
        errors.append(f"unknown field {name}")
    for name in sorted(missing):
        errors.append(f"missing field {name}")


def _unit(value) -> float:
    try:
        return float(value)
    except (OverflowError, ValueError, TypeError):
        return math.nan


def _score(value, errors, name: str, low: float, high: float) -> None:
    if not is_number(value) or not math.isfinite(_unit(value)):
        errors.append(f"{name} is not a finite number")
        return
    if _unit(value) < low or _unit(value) > high:
        errors.append(f"{name} is outside [{low}, {high}]")


def _optional_score(value, errors, name: str, low: float, high: float) -> None:
    if value is None:
        return
    _score(value, errors, name, low, high)


def validate_pose(pose) -> list[str]:
    errors: list[str] = []
    if not isinstance(pose, dict):
        return ["head_pose is not an object"]
    _exact_keys(pose, {"matrix", "yaw_deg", "pitch_deg", "roll_deg"}, errors)
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
        if any(not is_number(value) or not math.isfinite(_unit(value)) for value in row):
            return ["matrix contains a non-finite number"]
        rows.append([_unit(value) for value in row])
    if rows[3] != [0.0, 0.0, 0.0, 1.0]:
        errors.append("matrix last row is not [0, 0, 0, 1]")
    rotation, problem = _scaled_rotation(rows)
    if rotation is None:
        errors.append(problem)
        return errors
    yaw = math.degrees(math.atan2(rotation[0][2], rotation[2][2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, rotation[1][2]))))
    roll = math.degrees(math.atan2(rotation[1][0], rotation[1][1]))
    for name, extracted, low, high in (
        ("yaw_deg", yaw, -90.0, 90.0),
        ("pitch_deg", pitch, -60.0, 60.0),
        ("roll_deg", roll, -45.0, 45.0),
    ):
        published = pose[name]
        if not is_number(published) or not math.isfinite(_unit(published)):
            errors.append(f"{name} is not a finite number")
            continue
        if abs(_unit(published) - extracted) > 0.1:
            errors.append(f"{name} does not match the matrix")
        if extracted < low or extracted > high:
            errors.append(f"{name} is outside [{low}, {high}]")
        if _unit(published) < low or _unit(published) > high:
            errors.append(f"published {name} is outside [{low}, {high}]")
    x_cm, y_cm, z_cm = rows[0][3], rows[1][3], rows[2][3]
    if not (-100.0 <= x_cm <= 100.0 and -100.0 <= y_cm <= 100.0):
        errors.append("translation x or y is outside [-100, 100] cm")
    if not (-200.0 <= z_cm <= -5.0):
        errors.append("translation z is outside [-200, -5] cm")
    return errors


def _scaled_rotation(rows):
    """Oracle check that the upper-left 3x3 is a uniform scale times a rotation.

    Written independently of ``ml/schema.py``: it works on the Gram matrix
    ``B^T B`` of ``B = A / max|A|``. Tolerances: Schema 4's 1 percent on column
    norms, then normalized Gram off-diagonals at most 1e-3 and the normalized
    scalar triple product at least 1 - 1e-3.
    """
    peak = max(abs(rows[r][c]) for r in range(3) for c in range(3))
    if not math.isfinite(peak) or peak <= 0:
        return None, "pose scale is not positive"
    b = [[rows[r][c] / peak for c in range(3)] for r in range(3)]
    gram = [
        [math.fsum(b[k][i] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]
    norms = [math.sqrt(gram[i][i]) for i in range(3)]
    scale = math.fsum(norms) / 3.0
    if not math.isfinite(scale) or scale <= 0:
        return None, "pose scale is not positive"
    if max(abs(norm - scale) for norm in norms) / scale > 0.01:
        return None, "pose column norms disagree by more than 1 percent"
    for i, j in ((0, 1), (0, 2), (1, 2)):
        if not abs(gram[i][j]) / (norms[i] * norms[j]) <= 1e-3:
            return None, "pose columns are not orthogonal"
    triple = math.fsum(
        (
            b[0][0] * (b[1][1] * b[2][2] - b[2][1] * b[1][2]),
            -b[1][0] * (b[0][1] * b[2][2] - b[2][1] * b[0][2]),
            b[2][0] * (b[0][1] * b[1][2] - b[1][1] * b[0][2]),
        )
    )
    if not triple / (norms[0] * norms[1] * norms[2]) >= 1.0 - 1e-3:
        return None, "pose is not a proper rotation"
    return [[b[r][c] / scale for c in range(3)] for r in range(3)], None


def _features(value, errors, state: str) -> None:
    if not isinstance(value, dict):
        errors.append("features is not an object")
        return
    unknown = set(value) - set(BLENDSHAPES)
    for name in sorted(unknown):
        errors.append(f"unknown feature {name}")
    if state == "tracking":
        missing = set(BLENDSHAPES) - set(value)
        for name in sorted(missing):
            errors.append(f"missing feature {name}")
    elif state == "lost" and value:
        errors.append("lost features must be empty")
    for name, score in value.items():
        if name not in set(BLENDSHAPES):
            continue
        _score(score, errors, name, 0.0, 1.0)


def validate_feature(message) -> list[str]:
    try:
        return _validate_feature(message)
    except Exception:
        return ["rejected"]


def _validate_feature(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, FEATURE_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA:
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
        errors.extend(validate_pose(pose))
        _optional_score(confidence, errors, "confidence", 0.0, 1.0)
        if confidence is not None and is_number(confidence) and _unit(confidence) <= 0:
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


def validate_gesture(message) -> list[str]:
    try:
        return _validate_gesture(message)
    except Exception:
        return ["rejected"]


def _validate_gesture(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, GESTURE_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA:
        errors.append("schema_version is not 4")
    for name in (
        "timestamp_ms",
        "source_timestamp_ms",
        "window_start_timestamp_ms",
        "window_end_timestamp_ms",
    ):
        if not is_timestamp(message[name]):
            errors.append(f"{name} is not a timestamp")
    count = message["window_frame_count"]
    if not is_int(count) or count < 1:
        errors.append("window_frame_count must be an integer >= 1")
    if all(is_timestamp(message[name]) for name in (
        "window_start_timestamp_ms",
        "window_end_timestamp_ms",
        "source_timestamp_ms",
        "timestamp_ms",
    )):
        if message["window_start_timestamp_ms"] > message["window_end_timestamp_ms"]:
            errors.append("window starts after it ends")
        if message["source_timestamp_ms"] != message["window_end_timestamp_ms"]:
            errors.append("source_timestamp_ms is not the window end")
        if message["timestamp_ms"] < message["window_end_timestamp_ms"]:
            errors.append("classification time is before the window end")
        if (
            is_int(count)
            and count == 1
            and message["window_start_timestamp_ms"] != message["window_end_timestamp_ms"]
        ):
            errors.append("one-frame window start, end, and source differ")
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


def _velocity(value, errors, name: str, low: float, high: float) -> None:
    applied = canonical_velocity(value)
    if applied is None:
        errors.append(f"{name} is not a canonical velocity")
        return
    if applied < low or applied > high:
        errors.append(f"{name} is outside [{low}, {high}]")


def _zero(value) -> bool:
    return canonical_velocity(value) == 0.0


def validate_motor(message) -> list[str]:
    try:
        return _validate_motor(message)
    except Exception:
        return ["rejected"]


def _validate_motor(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, MOTOR_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA:
        errors.append("schema_version is not 4")
    if not isinstance(message["controller_id"], str) or CONTROLLER_ID.fullmatch(message["controller_id"]) is None:
        errors.append("controller_id is invalid")
    if not is_safe_id(message["boot_nonce"]):
        errors.append("boot_nonce is invalid")
    if not is_safe_id(message["session_id"]):
        errors.append("session_id is invalid")
    if not is_safe_id(message["freshness_id"]):
        errors.append("freshness_id is invalid")
    generation = message["rearm_generation"]
    if not is_int(generation) or not 0 <= generation <= SAFE_INT_MAX:
        errors.append("rearm_generation is invalid")
    if not is_safe_id(message["status_challenge"]):
        errors.append("status_challenge is invalid")
    if not isinstance(message["mac"], str) or MAC_HEX.fullmatch(message["mac"]) is None:
        errors.append("mac is invalid")
    sequence = message["sequence"]
    if not is_int(sequence) or not 1 <= sequence <= SEQUENCE_MAX:
        errors.append("sequence is invalid")
    issued = message["issued_at_ms"]
    expires = message["expires_at_ms"]
    if not is_timestamp(issued) or not is_timestamp(expires):
        errors.append("command timestamps are invalid")
    elif not 1 <= expires - issued <= 250:
        errors.append("command lifetime is outside 1..250 ms")
    task = message["task_state"]
    if task not in TASKS:
        errors.append("task_state is invalid")
    stop = message["stop"]
    if not is_bool(stop):
        errors.append("stop is not a boolean")
    linear = message["linear_mps"]
    angular = message["angular_rad_s"]
    _velocity(linear, errors, "linear_mps", -0.30, 0.30)
    _velocity(angular, errors, "angular_rad_s", -1.00, 1.00)
    if task in STOP_TASKS:
        if stop is not True or not _zero(linear) or not _zero(angular):
            errors.append("stop task must have stop true and zero motion")
    elif task in MOVE_TASKS and stop is True and not (_zero(linear) and _zero(angular)):
        errors.append("stop true requires zero motion")
    elif task in MOVE_TASKS and stop is False and (
        not is_number(linear) or not is_number(angular)
    ):
        errors.append("motion values are invalid")
    return errors


def validate_status(message) -> list[str]:
    try:
        return _validate_status(message)
    except Exception:
        return ["rejected"]


def _validate_status(message) -> list[str]:
    errors: list[str] = []
    _exact_keys(message, STATUS_FIELDS, errors)
    if not isinstance(message, dict) or errors:
        return errors
    if message["schema_version"] != SCHEMA:
        errors.append("schema_version is not 4")
    if not isinstance(message["controller_id"], str) or CONTROLLER_ID.fullmatch(message["controller_id"]) is None:
        errors.append("controller_id is invalid")
    if not is_timestamp(message["timestamp_ms"]):
        errors.append("timestamp_ms is not a timestamp")
    if not is_safe_id(message["boot_nonce"]):
        errors.append("boot_nonce is invalid")
    if not is_safe_id(message["freshness_id"]):
        errors.append("freshness_id is invalid")
    generation = message["rearm_generation"]
    if not is_int(generation) or not 0 <= generation <= SAFE_INT_MAX:
        errors.append("rearm_generation is invalid")
    challenge = message["status_challenge"]
    if not is_int(challenge) or not 0 <= challenge <= SAFE_INT_MAX:
        errors.append("status_challenge is invalid")
    status_sequence = message["status_sequence"]
    if not is_int(status_sequence) or not 1 <= status_sequence <= SAFE_INT_MAX:
        errors.append("status_sequence is invalid")
    if message["clock_state"] not in ("unsynchronized", "synchronized"):
        errors.append("clock_state is invalid")
    if message["sensor_state"] not in ("asserted", "released", "unknown"):
        errors.append("sensor_state is invalid")
    if not is_bool(message["cutoff_open"]):
        errors.append("cutoff_open is not a boolean")
    if not isinstance(message["mac"], str) or MAC_HEX.fullmatch(message["mac"]) is None:
        errors.append("mac is invalid")
    session = message["session_id"]
    sequence = message["last_sequence"]
    if session is not None and not is_safe_id(session):
        errors.append("session_id is invalid")
    if sequence is not None and (
        not is_int(sequence) or not 1 <= sequence <= SEQUENCE_MAX
    ):
        errors.append("last_sequence is invalid")
    if session is None and sequence is not None:
        errors.append("last_sequence requires a session")
    if session is not None and sequence is None:
        errors.append("session requires last_sequence")
    for name in (
        "physical_estop",
        "software_estop_latch",
        "fault_latch",
        "armed",
        "commanding_stop",
    ):
        if not is_bool(message[name]):
            errors.append(f"{name} is not a boolean")
    if message.get("sensor_state") == "released" and message.get("physical_estop") is not False:
        errors.append("released sensor contradicts physical_estop")
    if message.get("sensor_state") != "released" and message.get("physical_estop") is not True:
        errors.append("unknown or asserted sensor requires physical_estop")
    if message.get("armed") is True and (
        message.get("physical_estop") is not False
        or message.get("software_estop_latch") is not False
        or message.get("fault_latch") is not False
        or message.get("cutoff_open") is not False
        or message.get("sensor_state") != "released"
    ):
        errors.append("armed status contradicts a stop condition")
    if session is None and message.get("armed") is True:
        errors.append("armed requires an active session")
    return errors
