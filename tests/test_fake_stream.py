"""Checks the synthetic camera feature stream against the contract oracle.

perception/fake_stream.py imports only the standard library at module level,
so this file runs where only pytest is installed.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path

import pytest

from perception import fake_stream
from tests.contract_schema import BLENDSHAPES, SCHEMA, validate_feature

RUN_LENGTH = 600
RATE_HZ = 30.0
SEED = 11
START_MS = fake_stream.FIXTURE_START_MS


def seeded_run(length: int = RUN_LENGTH, rate_hz: float = RATE_HZ) -> list[dict]:
    return list(itertools.islice(fake_stream.generate_frames(rate_hz, SEED, START_MS), length))


def long_run() -> list[dict]:
    """Twenty seconds at 30 Hz, long enough to cover every state."""
    return seeded_run(length=600)


def frames_in(state: str, frames: list[dict]) -> list[dict]:
    return [frame for frame in frames if frame["tracking_state"] == state]


def test_every_frame_of_a_seeded_run_passes_the_validator():
    for frame in seeded_run():
        assert validate_feature(frame) == [], frame["timestamp_ms"]


def test_every_frame_survives_the_json_wire_encoding():
    for frame in seeded_run():
        assert validate_feature(json.loads(fake_stream.encode(frame))) == []


def test_run_is_deterministic_for_a_seed():
    assert seeded_run(length=200) == seeded_run(length=200)


def test_run_covers_all_three_states():
    states = {frame["tracking_state"] for frame in long_run()}
    assert states == {"tracking", "partial", "lost"}


def test_schedule_is_ninety_percent_tracking_per_cycle():
    cycle = seeded_run(length=int(fake_stream.CYCLE_S * RATE_HZ))
    counts = {state: len(frames_in(state, cycle)) for state in ("tracking", "partial", "lost")}
    assert counts == {"tracking": 405, "partial": 15, "lost": 30}


def test_tracking_features_are_exactly_the_52_blendshapes():
    for frame in frames_in("tracking", long_run()):
        assert set(frame["features"]) == set(BLENDSHAPES)
        assert len(frame["features"]) == 52
        assert all(0.0 <= value <= 1.0 for value in frame["features"].values())


def test_partial_frames_have_no_pose_and_a_subset_of_names():
    partial = frames_in("partial", long_run())
    assert partial
    for frame in partial:
        assert frame["head_pose"] is None
        assert set(frame["features"]) < set(BLENDSHAPES)
        assert all(0.0 <= value <= 1.0 for value in frame["features"].values())


def test_lost_frames_have_empty_features_and_no_pose():
    lost = frames_in("lost", long_run())
    assert lost
    for frame in lost:
        assert frame["features"] == {}
        assert frame["head_pose"] is None


@pytest.mark.parametrize("state", ["tracking", "partial", "lost"])
def test_confidence_is_always_null(state):
    frame = fake_stream.make_frame(START_MS, state, random.Random(SEED))
    assert frame["confidence"] is None
    assert frame["schema_version"] == SCHEMA


def test_confidence_is_null_on_every_frame_of_a_run():
    assert all(frame["confidence"] is None for frame in long_run())


def test_unknown_state_is_refused():
    with pytest.raises(ValueError):
        fake_stream.make_frame(START_MS, "maybe", random.Random(SEED))


ANGLE_TRIPLES = [
    (0.0, 0.0, 0.0),
    (30.0, -20.0, 10.0),
    (-30.0, 20.0, -10.0),
    (89.0, 0.0, 0.0),
    (-89.0, 0.0, 0.0),
    (0.0, 59.0, 0.0),
    (0.0, -59.0, 0.0),
    (0.0, 0.0, 44.0),
    (0.0, 0.0, -44.0),
    (85.0, 55.0, 40.0),
    (-85.0, -55.0, -40.0),
    (60.0, -45.0, 30.0),
    (-12.5, 7.25, -3.75),
]


@pytest.mark.parametrize("yaw, pitch, roll", ANGLE_TRIPLES)
@pytest.mark.parametrize("scale", [0.8, 1.0, 1.3])
def test_extract_angles_inverts_build_matrix(yaw, pitch, roll, scale):
    matrix = fake_stream.build_matrix(yaw, pitch, roll, scale, 4.0, -3.0, -45.0)
    got = fake_stream.extract_angles(matrix)
    for expected, actual in zip((yaw, pitch, roll), got):
        assert abs(expected - actual) <= 0.1


@pytest.mark.parametrize("yaw, pitch, roll", ANGLE_TRIPLES)
def test_build_matrix_shape_last_row_and_uniform_scale(yaw, pitch, roll):
    matrix = fake_stream.build_matrix(yaw, pitch, roll, 1.07, 1.0, 2.0, -60.0)
    assert len(matrix) == 4 and all(len(row) == 4 for row in matrix)
    assert matrix[3] == [0, 0, 0, 1]
    norms = fake_stream.column_norms(matrix)
    mean = sum(norms) / 3.0
    assert max(abs(norm - mean) for norm in norms) / mean <= 0.01
    assert [matrix[r][3] for r in range(3)] == [1.0, 2.0, -60.0]


def test_build_matrix_matches_the_contract_composition_order():
    """Positive yaw alone puts sin(yaw) in R[0][2], per Ry in the contract."""
    matrix = fake_stream.build_matrix(30.0, 0.0, 0.0, 1.0, 0.0, 0.0, -45.0)
    assert math.isclose(matrix[0][2], math.sin(math.radians(30.0)))
    assert math.isclose(matrix[2][0], -math.sin(math.radians(30.0)))


def test_extract_angles_refuses_non_uniform_scale():
    matrix = fake_stream.build_matrix(10.0, 5.0, 2.0, 1.0, 0.0, 0.0, -45.0)
    for r in range(3):
        matrix[r][0] *= 1.05
    with pytest.raises(ValueError):
        fake_stream.extract_angles(matrix)


def test_extract_angles_refuses_a_zero_matrix():
    zero = [[0.0] * 4 for _ in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    with pytest.raises(ValueError):
        fake_stream.extract_angles(zero)


def test_timestamps_are_integers_and_never_decrease():
    frames = seeded_run()
    stamps = [frame["timestamp_ms"] for frame in frames]
    assert all(type(stamp) is int for stamp in stamps)
    assert all(later >= earlier for earlier, later in zip(stamps, stamps[1:]))
    assert stamps[0] == START_MS


def test_tracking_translation_is_inside_contract_bounds():
    for frame in frames_in("tracking", long_run()):
        matrix = frame["head_pose"]["matrix"]
        x_cm, y_cm, z_cm = matrix[0][3], matrix[1][3], matrix[2][3]
        assert -200.0 <= z_cm <= -5.0
        assert z_cm < 0
        assert -100.0 <= x_cm <= 100.0
        assert -100.0 <= y_cm <= 100.0


def test_tracking_angles_stay_well_inside_valid_ranges():
    for frame in frames_in("tracking", long_run()):
        pose = frame["head_pose"]
        assert abs(pose["yaw_deg"]) <= 30.0
        assert abs(pose["pitch_deg"]) <= 20.0
        assert abs(pose["roll_deg"]) <= 15.0


def test_pulse_channels_reach_a_visible_peak():
    tracking = frames_in("tracking", long_run())
    for name in fake_stream.PULSE_CHANNELS:
        peak = max(frame["features"][name] for frame in tracking)
        assert 0.6 <= peak <= 0.8, name


FIXTURE_DIR = Path(__file__).resolve().parent.parent / "perception" / "fixtures"


@pytest.mark.parametrize("state", ["tracking", "partial", "lost"])
def test_fixture_files_parse_and_pass_the_validator(state):
    frame = json.loads((FIXTURE_DIR / f"frame_{state}.json").read_text(encoding="utf-8"))
    assert frame["tracking_state"] == state
    assert validate_feature(frame) == []


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("state", ["tracking", "partial", "lost"])
def test_fixture_files_match_what_the_generator_writes(tmp_path, state):
    """Compares parsed JSON, so line endings and whitespace cannot matter."""
    name = f"frame_{state}.json"
    fake_stream.write_fixtures(tmp_path)
    assert load_json(FIXTURE_DIR / name) == load_json(tmp_path / name)


@pytest.mark.parametrize("state", ["tracking", "partial", "lost"])
def test_fixture_files_match_the_generated_frame(state):
    """The committed fixture equals the in-memory frame the generator builds."""
    frame = fake_stream.first_frame_in_state(state, SEED, RATE_HZ, START_MS)
    assert load_json(FIXTURE_DIR / f"frame_{state}.json") == frame


def test_replay_reads_back_a_recording(tmp_path):
    frames = seeded_run(length=20)
    path = tmp_path / "run.jsonl"
    path.write_text("".join(fake_stream.encode(frame) + "\n" for frame in frames), encoding="utf-8")
    assert list(fake_stream.replay_frames(path)) == frames
