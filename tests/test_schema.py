"""Schema checks for mocked messages. No model output and no motors."""

from tests.contract_schema import (
    BLENDSHAPES,
    sender_may_request_motion,
    validate_feature,
    validate_gesture,
    validate_motor,
    validate_status,
)

T0 = 1_795_276_800_000


def features():
    return {name: 0 for name in BLENDSHAPES}


def pose():
    return {
        "matrix": [
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 1, -50],
            [0, 0, 0, 1],
        ],
        "yaw_deg": 0,
        "pitch_deg": 0,
        "roll_deg": 0,
    }


def feature(**overrides):
    message = {
        "schema_version": "2",
        "timestamp_ms": T0,
        "tracking_state": "tracking",
        "features": features(),
        "head_pose": pose(),
        "confidence": None,
    }
    message.update(overrides)
    return message


def gesture(**overrides):
    message = {
        "schema_version": "2",
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


def motor(**overrides):
    message = {
        "schema_version": "2",
        "boot_nonce": 42,
        "session_id": 7,
        "sequence": 1,
        "issued_at_ms": T0,
        "expires_at_ms": T0 + 250,
        "task_state": "idle",
        "linear_mps": 0,
        "angular_rad_s": 0,
        "stop": True,
    }
    message.update(overrides)
    return message


def test_blendshape_table_matches_face_landmarker_order():
    assert len(BLENDSHAPES) == 52
    assert BLENDSHAPES[0] == "_neutral"
    assert BLENDSHAPES[51] == "noseSneerRight"
    assert len(set(BLENDSHAPES)) == 52


def test_tracking_with_null_confidence_is_valid():
    assert validate_feature(feature()) == []


def test_lost_does_not_use_a_synthetic_zero_confidence():
    lost = feature(
        tracking_state="lost",
        features={},
        head_pose=None,
        confidence=None,
    )
    assert validate_feature(lost) == []
    lost["confidence"] = 0
    assert validate_feature(lost)


def test_partial_face_is_not_declared_lost_for_missing_pose():
    partial = feature(
        tracking_state="partial",
        features={"jawOpen": 0.2},
        head_pose=None,
        confidence=None,
    )
    assert validate_feature(partial) == []


def test_feature_rejects_unknown_fields_and_schema_1():
    message = feature()
    message["min_face_detection_confidence"] = 0.5
    assert validate_feature(message)
    old = feature(schema_version="1", confidence=1)
    assert validate_feature(old)


def test_gesture_window_for_one_frame_and_for_a_span():
    assert validate_gesture(gesture()) == []
    spanned = gesture(
        window_start_timestamp_ms=T0 - 80,
        window_end_timestamp_ms=T0,
        source_timestamp_ms=T0,
        window_frame_count=3,
        label="neutral",
        confidence=0.4,
    )
    assert validate_gesture(spanned) == []


def test_gesture_rejects_a_window_that_does_not_end_at_the_source_frame():
    bad = gesture(window_end_timestamp_ms=T0 - 10)
    assert validate_gesture(bad)
    assert validate_gesture(gesture(window_frame_count=0))
    assert validate_gesture(gesture(label="blink"))


def test_motor_stop_shape_and_limits():
    assert validate_motor(motor()) == []
    moving = motor(
        sequence=4,
        task_state="manual",
        linear_mps=0.3,
        angular_rad_s=-1,
        stop=False,
    )
    assert validate_motor(moving) == []
    too_fast = motor(
        sequence=4,
        task_state="manual",
        linear_mps=0.31,
        stop=False,
    )
    assert validate_motor(too_fast)
    assert validate_motor(motor(schema_version="1"))


def test_status_reports_physical_estop_separately():
    message = {
        "schema_version": "2",
        "timestamp_ms": T0,
        "boot_nonce": 42,
        "session_id": 7,
        "last_sequence": 2,
        "physical_estop": True,
        "software_estop_latch": False,
        "fault_latch": False,
        "armed": False,
        "commanding_stop": True,
    }
    assert validate_status(message) == []
    message["armed"] = True
    assert validate_status(message)
    assert sender_may_request_motion(message, session_id=7, sequence=3) is False
