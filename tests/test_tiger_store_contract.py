"""Camera feature contract adapter in backend/db/tiger_store.py.

Constructed inputs only: no database, no network, no recorded data. These tests check the
conversion from contract messages to database rows. They make no claim about accuracy,
latency, or any clinical property.
"""
from __future__ import annotations

import copy
import importlib.util
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "backend" / "db" / "tiger_store.py"
_spec = importlib.util.spec_from_file_location("tiger_store", _PATH)
tiger_store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tiger_store)

SESSION = uuid.UUID(int=1)
PROFILE = uuid.UUID(int=2)
TS_MS = 1795276800123

# Written out independently of tiger_store.BLENDSHAPE_NAMES so a rename there is caught here.
MEDIAPIPE_BLENDSHAPES = [
    "_neutral", "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft",
    "browOuterUpRight", "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "eyeBlinkLeft",
    "eyeBlinkRight", "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight", "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft",
    "eyeSquintRight", "eyeWideLeft", "eyeWideRight", "jawForward", "jawLeft", "jawOpen",
    "jawRight", "mouthClose", "mouthDimpleLeft", "mouthDimpleRight", "mouthFrownLeft",
    "mouthFrownRight", "mouthFunnel", "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight", "mouthPucker", "mouthRight", "mouthRollLower",
    "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper", "mouthSmileLeft", "mouthSmileRight",
    "mouthStretchLeft", "mouthStretchRight", "mouthUpperUpLeft", "mouthUpperUpRight",
    "noseSneerLeft", "noseSneerRight",
]


def pose_matrix(yaw, pitch, roll, scale=1.0, t=(0.0, 0.0, -50.0)):
    """Build a row-major pose matrix from the contract's R = Ry(y) . Rx(-p) . Rz(r)."""
    y, p, r = map(math.radians, (yaw, pitch, roll))
    ry = [[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]]
    rx = [[1, 0, 0], [0, math.cos(p), math.sin(p)], [0, -math.sin(p), math.cos(p)]]
    rz = [[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    rot = mul(mul(ry, rx), rz)
    return [[scale * rot[i][0], scale * rot[i][1], scale * rot[i][2], t[i]] for i in range(3)] + [
        [0, 0, 0, 1]]


def tracking_frame(confidence=None):
    """A constructed schema 4 tracking message. Values are arbitrary, not measured."""
    return {
        "schema_version": "4",
        "timestamp_ms": TS_MS,
        "tracking_state": "tracking",
        "features": {name: round(i / 100, 2) for i, name in enumerate(MEDIAPIPE_BLENDSHAPES)},
        "head_pose": {
            "matrix": pose_matrix(2.5, -1.25, 0.5, scale=1.1),
            "yaw_deg": 2.5,
            "pitch_deg": -1.25,
            "roll_deg": 0.5,
        },
        "confidence": confidence,
    }


def by_channel(rows):
    return {r[3]: r for r in rows}


def convert(frame):
    return tiger_store.contract_frame_rows(frame, SESSION, PROFILE)


def test_valid_tracking_frame_converts():
    result = convert(tracking_frame(confidence=0.75))
    assert result.rejection is None
    assert len(result.rows) == 52 + 3

    expected_ts = datetime(2026, 11, 21, 16, 0, 0, 123000, tzinfo=timezone.utc)
    for ts, session_id, profile_id, _name, value, tracking_ok, confidence in result.rows:
        assert ts == expected_ts
        assert (session_id, profile_id) == (SESSION, PROFILE)
        assert isinstance(value, float)
        assert tracking_ok is True
        assert confidence == 0.75

    rows = by_channel(result.rows)
    assert rows["jawOpen"][4] == 0.25
    assert rows["head_pose.yaw_deg"][4] == 2.5
    assert rows["head_pose.pitch_deg"][4] == -1.25
    assert rows["head_pose.roll_deg"][4] == 0.5


def test_row_shape_matches_copy_columns():
    columns = tiger_store.SIGNAL_SAMPLES_COPY.split("(", 1)[1].rstrip(")").split(",")
    for row in convert(tracking_frame()).rows:
        assert len(row) == len(columns)


def test_timestamp_ms_is_integer_epoch_milliseconds():
    frame = tracking_frame()
    frame["timestamp_ms"] = 0
    assert convert(frame).rows[0][0] == datetime(1970, 1, 1, tzinfo=timezone.utc)

    frame["timestamp_ms"] = 1795276800.5  # float seconds-style value is not a contract timestamp
    assert convert(frame).rejection is not None


def test_null_confidence_converts_and_stays_null():
    result = convert(tracking_frame(confidence=None))
    assert result.rejection is None
    assert result.rows
    assert all(row[6] is None for row in result.rows)
    # null confidence does not make a tracking frame untracked
    assert all(row[5] is True for row in result.rows)


@pytest.mark.parametrize("state", ["partial", "lost"])
def test_non_tracking_states_are_not_tracking_ok(state):
    frame = tracking_frame()
    frame["tracking_state"] = state
    frame["head_pose"] = None
    frame["features"] = {"jawOpen": 0.3, "eyeBlinkLeft": 0.1} if state == "partial" else {}
    result = convert(frame)
    assert result.rejection is None
    assert all(row[5] is False for row in result.rows)
    if state == "partial":
        assert set(by_channel(result.rows)) == {"jawOpen", "eyeBlinkLeft"}
    else:
        assert result.rows == []


def test_partial_with_high_confidence_is_still_not_tracking_ok():
    frame = tracking_frame()
    frame.update(tracking_state="partial", head_pose=None, features={"jawOpen": 0.3},
                 confidence=1.0)
    result = convert(frame)
    assert result.rejection is None
    assert [row[5] for row in result.rows] == [False]


@pytest.mark.parametrize("version", ["3", "2", "5", 4, None, ""])
def test_wrong_schema_version_is_rejected_without_raising(version):
    frame = tracking_frame()
    frame["schema_version"] = version
    result = convert(frame)
    assert result.rows == []
    assert "schema_version" in result.rejection


def test_missing_schema_version_is_rejected():
    frame = tracking_frame()
    del frame["schema_version"]
    result = convert(frame)
    assert result.rows == []
    assert result.rejection


@pytest.mark.parametrize("frame", [
    None, [], "not a frame",
    {**tracking_frame(), "channels": {}},                       # undefined field
    {k: v for k, v in tracking_frame().items() if k != "features"},
    {**tracking_frame(), "timestamp_ms": True},
    {**tracking_frame(), "timestamp_ms": -1},
    {**tracking_frame(), "tracking_state": "found"},
    {**tracking_frame(), "tracking_state": ["tracking"]},      # unhashable: must not raise
    {**tracking_frame(), "tracking_state": {}},
    {**tracking_frame(), "confidence": 0},                     # 0 is not a tracking confidence
    {**tracking_frame(), "confidence": "0.9"},
    {**tracking_frame(), "head_pose": None},
    {**tracking_frame(), "features": {"jaw_open": 0.1}},       # snake_case is not a contract name
])
def test_malformed_frames_are_rejected_without_raising(frame):
    result = convert(frame)
    assert result.rows == []
    assert result.rejection


def with_pose(**pose_overrides):
    frame = tracking_frame()
    frame["head_pose"].update(pose_overrides)
    return frame


@pytest.mark.parametrize("frame", [
    with_pose(matrix=[[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 1]]),  # zero matrix
    with_pose(yaw_deg=2.7),                                    # disagrees with matrix by 0.2 deg
    with_pose(matrix=pose_matrix(2.5, -1.25, 0.5, t=(0, 0, 10))),     # behind the camera
    with_pose(matrix=pose_matrix(2.5, -1.25, 0.5, t=(150, 0, -50))),  # x outside [-100, 100]
    with_pose(matrix=[[1, 0, 0, 0], [0, 2, 0, 0], [0, 0, 1, -50], [0, 0, 0, 1]]),  # non-uniform
    with_pose(matrix=pose_matrix(2.5, -1.25, 0.5)[:3] + [[0, 0, 1, 1]]),  # bad last row
])
def test_invalid_head_pose_is_rejected(frame):
    result = convert(frame)
    assert result.rows == []
    assert "head_pose" in result.rejection


def test_pose_out_of_range_is_rejected():
    frame = tracking_frame()
    frame["head_pose"] = {"matrix": pose_matrix(0, 0, 50), "yaw_deg": 0, "pitch_deg": 0,
                          "roll_deg": 50}
    assert "roll_deg" in convert(frame).rejection


def test_published_angles_within_tolerance_are_accepted():
    assert convert(with_pose(yaw_deg=2.55)).rejection is None


@pytest.mark.parametrize("field", ["confidence", "feature", "yaw_deg", "matrix"])
def test_integer_too_large_for_a_double_is_rejected_without_raising(field):
    huge = 10 ** 400
    frame = tracking_frame()
    if field == "confidence":
        frame["confidence"] = huge
    elif field == "feature":
        frame["features"]["jawOpen"] = huge
    elif field == "yaw_deg":
        frame["head_pose"]["yaw_deg"] = huge
    else:
        frame["head_pose"]["matrix"][0][0] = huge
    result = convert(frame)
    assert result.rows == []
    assert result.rejection


def test_tracking_frame_missing_a_blendshape_is_rejected():
    frame = tracking_frame()
    del frame["features"]["jawOpen"]
    assert convert(frame).rejection


def test_all_52_blendshape_names_survive_unrenamed():
    assert len(MEDIAPIPE_BLENDSHAPES) == 52
    assert list(tiger_store.BLENDSHAPE_NAMES) == MEDIAPIPE_BLENDSHAPES
    channels = [row[3] for row in convert(tracking_frame()).rows]
    feature_channels = [c for c in channels if not c.startswith("head_pose.")]
    assert feature_channels == MEDIAPIPE_BLENDSHAPES


def test_input_frame_is_not_mutated():
    frame = tracking_frame()
    before = copy.deepcopy(frame)
    convert(frame)
    assert frame == before


def test_contract_file_rows_reports_rejections(tmp_path):
    import json

    good = tracking_frame()
    bad = {**tracking_frame(), "schema_version": "3"}
    path = tmp_path / "frames.jsonl"
    path.write_text("\n".join(json.dumps(f) for f in (good, bad, good)) + "\n")

    rejections = []
    rows = list(tiger_store.contract_file_rows(path, SESSION, PROFILE, rejections))
    assert len(rows) == 2 * (52 + 3)
    assert [line for line, _ in rejections] == [2]
    assert "schema_version" in rejections[0][1]


def test_consent_defaults_to_pending():
    import inspect

    default = inspect.signature(tiger_store.create_profile).parameters["consent_state"].default
    assert default == "pending"


def test_consent_flag_defaults_to_pending():
    args = tiger_store.build_parser().parse_args([
        "load-session", "--new-profile", "TEST", "--frames", "f.jsonl", "--events", "e.jsonl",
        "--tier", "1", "--origin", "synthetic_seed"])
    assert args.consent == "pending"


@pytest.mark.parametrize("copy_spec", ["SIGNAL_SAMPLES_COPY", "CONTROL_EVENTS_COPY"])
def test_copy_columns_exist_in_schema(copy_spec):
    import re

    table, cols = getattr(tiger_store, copy_spec).split(" (", 1)
    cols = [c.strip() for c in cols.rstrip(")").split(",")]
    schema = (_PATH.parent / "schema.sql").read_text()
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", schema, re.S).group(1)
    defined = {line.split()[0] for line in body.splitlines()
               if line.strip() and not line.strip().startswith(("--", "PRIMARY", "CHECK"))}
    assert set(cols) <= defined, set(cols) - defined
