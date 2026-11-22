"""Constructed checks for dataset metadata, JSONL ingestion, and split plans.

Every message in this file is built in the test. None of them is a participant
recording, a camera capture, or a measured timestamp. A provenance flag used
here records only the claim the test passed in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from perception.fake_stream import encode
from tests.test_ml_pipeline import T0, _scores, lost, partial, tracking

from ml.dataset import (
    FEATURE_JSONL_FORMAT,
    SCHEMA_4_VERSION,
    AuthorizationRecord,
    DatasetError,
    DatasetSource,
    ExtractionConfig,
    GestureAnnotation,
    IngestCounts,
    IngestResult,
    ParticipantRole,
    PreparedExample,
    RecordingAssignment,
    RecordingIdentity,
    SessionSplit,
    SplitPlanError,
    assert_examples_may_fit,
    fitting_scope,
    ingest_recording,
    normalization_candidates,
    parse_dataset_source,
    parse_gesture_annotation,
    parse_recording_identity,
    validate_split_plan,
    validate_splits,
)
from ml.features import FEATURE_DIM, FEATURE_NAMES, extract_features, named_features
from ml.records import LabeledExample
from ml.windows import WindowAssembler

VERSION = "ml1-window-v1"
PLANTED = "PLANTED99173"
PLANTED_NUMBER = "0.777"


def _source(category="constructed_fixture", source_format=FEATURE_JSONL_FORMAT, status=None):
    if status is None:
        status = "constructed" if category == "constructed_fixture" else "human_authorized"
    return DatasetSource(
        dataset_id="ds01",
        source_category=category,
        source_format=source_format,
        source_version=SCHEMA_4_VERSION,
        authorization=AuthorizationRecord(status, "decision-1"),
        extraction_version=VERSION,
    )


def _recording(participant="p01", session="s01", recording="r01", provenance="constructed"):
    return RecordingIdentity(
        dataset_id="ds01",
        participant_id=participant,
        session_id=session,
        recording_id=recording,
        timestamp_provenance=provenance,
    )


def _interval(label, start, end, recording=None, provenance="constructed"):
    identity = recording or _recording(provenance=provenance)
    return GestureAnnotation(
        participant_id=identity.participant_id,
        session_id=identity.session_id,
        recording_id=identity.recording_id,
        label=label,
        granularity="interval",
        start_ms=start,
        end_ms=end,
        timestamp_provenance=provenance,
    )


def _clip(label="jawOpen", recording=None):
    identity = recording or _recording()
    return GestureAnnotation(
        participant_id=identity.participant_id,
        session_id=identity.session_id,
        recording_id=identity.recording_id,
        label=label,
        granularity="clip",
        start_ms=None,
        end_ms=None,
        timestamp_provenance=identity.timestamp_provenance,
    )


def _extraction(length=1, stride=1):
    return ExtractionConfig(VERSION, length, stride)


def _frame(timestamp, jaw=0.0):
    return tracking(timestamp, scores=_scores(jawOpen=jaw))


def _write(path: Path, frames) -> None:
    path.write_text("".join(encode(frame) + "\n" for frame in frames), encoding="utf-8")


def _write_lines(path: Path, lines) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _ingest(path, frames, annotations, **kwargs):
    _write(path, frames)
    return _ingest_path(path, annotations, **kwargs)


def _ingest_path(path, annotations, **kwargs):
    recording = kwargs.pop("recording", None) or _recording(
        provenance=kwargs.pop("provenance", "constructed")
    )
    return ingest_recording(
        path,
        kwargs.pop("source", None) or _source(),
        recording,
        annotations,
        kwargs.pop("split", "train"),
        kwargs.pop("extraction", None) or _extraction(),
    )


def _prepared(participant, session, recording, split, features=None, provenance="constructed"):
    labeled = LabeledExample(
        participant_id=participant,
        session_id=session,
        split=split,
        label="jawOpen",
        features=(0.0,) * FEATURE_DIM if features is None else features,
        window_start_timestamp_ms=T0,
        window_end_timestamp_ms=T0,
        frame_count=1,
    )
    return PreparedExample(
        example=labeled,
        dataset_id="ds01",
        recording_id=recording,
        timestamp_provenance=provenance,
        timestamps_are_measured_capture=provenance == "measured_capture",
        extraction_version=VERSION,
    )


def _plan(examples):
    roles = []
    sessions = []
    recordings = []
    seen_roles = set()
    seen_sessions = set()
    seen_recordings = set()
    for item in examples:
        labeled = item.example
        if labeled.participant_id not in seen_roles:
            role = "population_train" if labeled.split == "train" else "held_out"
            roles.append(ParticipantRole(labeled.participant_id, role))
            seen_roles.add(labeled.participant_id)
        session_key = (labeled.participant_id, labeled.session_id)
        if session_key not in seen_sessions:
            sessions.append(SessionSplit(*session_key, labeled.split))
            seen_sessions.add(session_key)
        if item.recording_id not in seen_recordings:
            recordings.append(
                RecordingAssignment(
                    item.recording_id, labeled.participant_id, labeled.session_id, labeled.split
                )
            )
            seen_recordings.add(item.recording_id)
    return roles, sessions, recordings


def _source_mapping():
    return {
        "dataset_id": "ds01",
        "source_category": "constructed_fixture",
        "source_format": FEATURE_JSONL_FORMAT,
        "source_version": SCHEMA_4_VERSION,
        "authorization": {"status": "constructed", "decision_id": "decision-1"},
        "extraction_version": VERSION,
    }


def test_valid_metadata_round_trip_uses_opaque_tokens():
    source = parse_dataset_source(_source_mapping())
    recording = parse_recording_identity(
        {
            "dataset_id": "ds01",
            "participant_id": "p01",
            "session_id": "s01",
            "recording_id": "r01",
            "timestamp_provenance": "constructed",
        }
    )
    annotation = parse_gesture_annotation(
        {
            "participant_id": "p01",
            "session_id": "s01",
            "recording_id": "r01",
            "label": "neutral",
            "granularity": "interval",
            "start_ms": T0,
            "end_ms": T0 + 10,
            "timestamp_provenance": "constructed",
        }
    )
    assert source.authorization.status == "constructed"
    assert recording.timestamps_are_measured_capture is False
    assert annotation.label == "neutral"


def test_missing_and_extra_metadata_are_rejected_without_echoing_values():
    missing = _source_mapping()
    del missing["dataset_id"]
    with pytest.raises(DatasetError) as missing_error:
        parse_dataset_source(missing)
    assert missing_error.value.code == "missing_metadata_field"

    extra = _source_mapping()
    extra["email"] = "ada@example.com"
    with pytest.raises(DatasetError) as extra_error:
        parse_dataset_source(extra)
    assert extra_error.value.code == "unexpected_metadata_field"
    assert "ada@example.com" not in str(extra_error.value)

    with pytest.raises(DatasetError) as not_object:
        parse_recording_identity(["p01"])
    assert not_object.value.code == "metadata_not_an_object"


def test_personal_identifiers_and_consent_text_are_rejected():
    with pytest.raises(DatasetError) as email:
        RecordingIdentity("ds01", "ada@example.com", "s01", "r01", "constructed")
    assert email.value.code == "invalid_participant_id"
    assert "ada@example.com" not in str(email.value)

    consent = "Ada consented by email ada@example.com on a form"
    with pytest.raises(DatasetError) as decision:
        AuthorizationRecord("human_authorized", consent)
    assert decision.value.code == "invalid_decision_id"
    assert "ada@example.com" not in str(decision.value)
    assert "consented" not in str(decision.value)


@pytest.mark.parametrize(
    ("category", "status", "provenance", "code"),
    [
        ("constructed_fixture", "human_authorized", "constructed", "constructed_source_needs_constructed_authorization"),
        ("consented_local", "constructed", "measured_capture", "constructed_authorization_only_for_fixtures"),
        ("webcam", "constructed", "constructed", "invalid_source_category"),
    ],
)
def test_invalid_source_combinations_fail(category, status, provenance, code):
    with pytest.raises(DatasetError) as exc:
        DatasetSource(
            "ds01",
            category,
            FEATURE_JSONL_FORMAT,
            SCHEMA_4_VERSION,
            AuthorizationRecord(status, "decision-1") if status in {"human_authorized", "not_authorized", "constructed"} else AuthorizationRecord("constructed", "decision-1"),
            VERSION,
        )
    if category == "webcam":
        assert exc.value.code == "invalid_source_category"
    else:
        assert exc.value.code == code


def test_unknown_is_not_an_annotation_and_clip_has_no_bounds():
    with pytest.raises(DatasetError) as unknown:
        _interval("unknown", T0, T0 + 1)
    assert unknown.value.code == "unknown_is_not_an_annotation"
    with pytest.raises(DatasetError) as bounds:
        GestureAnnotation("p01", "s01", "r01", "jawOpen", "clip", T0, T0 + 1, "constructed")
    assert bounds.value.code == "clip_label_has_no_interval"
    with pytest.raises(DatasetError) as empty:
        _interval("jawOpen", T0, T0)
    assert empty.value.code == "annotation_interval_is_empty"


def test_ingest_matches_ml1_windows_and_keeps_a_gap(tmp_path):
    frames = [_frame(T0, 0.2), _frame(T0 + 40, 0.8), _frame(T0 + 80, 0.0), _frame(T0 + 5000, 0.4)]
    result = _ingest(
        tmp_path / "gap.jsonl",
        frames,
        [_interval("jawOpen", T0, T0 + 6000)],
        extraction=_extraction(2, 3),
    )
    direct = WindowAssembler(2, 3)
    expected = []
    for frame in frames:
        pushed = direct.push(frame)
        if pushed.window is not None and (vector := extract_features(pushed.window)) is not None:
            expected.append((vector, pushed.window.start_timestamp_ms, pushed.window.end_timestamp_ms))
    assert len(expected) == 1
    assert [(item.example.features, item.example.window_start_timestamp_ms, item.example.window_end_timestamp_ms) for item in result.examples] == expected
    example = result.examples[0].example
    assert len(example.features) == FEATURE_DIM == 162
    assert example.frame_count == 2
    assert example.window_end_timestamp_ms - example.window_start_timestamp_ms == 40
    assert named_features(example.features)[FEATURE_NAMES.index("jawOpen.mean")][1] == pytest.approx(0.5)
    assert result.examples[0].timestamps_are_measured_capture is False


def test_strict_jsonl_counts_malformed_rows_and_continues(tmp_path):
    good_early = encode(_frame(T0, 0.1))
    good_late = encode(_frame(T0 + 10, 0.3))
    older = encode(_frame(T0 - 5, 0.9))
    lines = [
        "",
        "{",
        good_early,
        older,
        "   ",
        good_late,
    ]
    path = tmp_path / "mixed.jsonl"
    _write_lines(path, lines)
    result = _ingest_path(path, [_interval("jawOpen", T0 - 10, T0 + 20)])
    assert [(item.line_number, item.reason) for item in result.rejections] == [
        (2, "invalid_json"),
        (4, "out_of_order"),
    ]
    assert result.counts.records_read == 4
    assert result.counts.accepted_frames == 2
    assert result.counts.examples_emitted == 2
    assert [item.example.window_start_timestamp_ms for item in result.examples] == [T0, T0 + 10]
    assert PLANTED not in "".join(item.reason for item in result.rejections)


def test_duplicate_keys_non_finite_numbers_and_bad_types_are_not_examples(tmp_path):
    valid = encode(_frame(T0))
    duplicated = valid.replace('"schema_version":"4"', '"schema_version":"4","schema_version":"4"', 1)
    nested = valid.replace('"jawOpen":0.0', f'"jawOpen":0.0,"jawOpen":{PLANTED_NUMBER}', 1)
    nan = valid.replace('"confidence":null', '"confidence":NaN', 1)
    huge = valid.replace(str(T0), "1e309", 1)
    infinity = valid.replace('"confidence":null', '"confidence":Infinity', 1)
    boolean_time = valid.replace(f'"timestamp_ms":{T0}', '"timestamp_ms":true', 1)
    array = "[1, 2, 3]"
    planted = valid.replace('"schema_version":"4"', f'"schema_version":"4","note":"{PLANTED}"', 1)
    bad_score = encode(_frame(T0 + 1, jaw=7.5))
    path = tmp_path / "bad.jsonl"
    _write_lines(path, [duplicated, nested, nan, huge, infinity, boolean_time, array, planted, bad_score])
    result = _ingest_path(path, [_interval("jawOpen", 0, T0 + 10)])
    assert [item.reason for item in result.rejections] == [
        "duplicate_key",
        "duplicate_key",
        "non_finite_number",
        "non_finite_number",
        "non_finite_number",
        "schema_rejected",
        "not_an_object",
        "schema_rejected",
        "schema_rejected",
    ]
    assert result.examples == ()
    rendered = " ".join(item.reason for item in result.rejections)
    assert PLANTED not in rendered
    assert PLANTED_NUMBER not in rendered
    assert "7.5" not in rendered
    assert "NaN" not in rendered


def test_out_of_order_equal_timestamps_and_invalid_tracking_drop_only_that_row(tmp_path):
    first = _frame(T0, 0.2)
    repeated = _frame(T0, 0.9)
    invalid = _frame(T0 + 20, jaw=1.5)
    second = _frame(T0 + 10, 0.4)
    third = _frame(T0 + 30, 0.6)
    lost_bad = lost(T0 + 40, confidence=0.2)
    path = tmp_path / "order.jsonl"
    _write_lines(
        path,
        [encode(first), encode(repeated), encode(invalid), encode(second), encode(lost_bad), encode(third)],
    )
    result = _ingest_path(path, [_interval("jawOpen", T0, T0 + 50)], extraction=_extraction(2, 2))
    assert [item.reason for item in result.rejections] == [
        "out_of_order",
        "schema_rejected",
        "schema_rejected",
    ]
    assert result.counts.examples_emitted == 1
    example = result.examples[0].example
    assert example.window_start_timestamp_ms == T0
    assert example.window_end_timestamp_ms == T0 + 10
    assert example.frame_count == 2


def test_partial_and_lost_frames_do_not_become_labels(tmp_path):
    frames = [
        _frame(T0, 0.2),
        lost(T0 + 10),
        partial(T0 + 20, confidence=0.0),
        _frame(T0 + 30, 0.4),
    ]
    result = _ingest(tmp_path / "states.jsonl", frames, [_interval("jawOpen", T0, T0 + 40)])
    assert result.counts.accepted_frames == 4
    assert result.counts.rejected_records == 0
    assert result.counts.windows_emitted == 4
    assert result.counts.non_actionable_windows == 2
    assert [item.example.window_start_timestamp_ms for item in result.examples] == [T0, T0 + 30]
    assert {item.example.label for item in result.examples} == {"jawOpen"}


def test_annotation_bounds_are_half_open_and_do_not_cross(tmp_path):
    frames = [_frame(T0 + 10, 0.2), _frame(T0 + 15, 0.8)]
    annotations = [
        _interval("jawOpen", T0, T0 + 15),
        _interval("browInnerUp", T0 + 15, T0 + 40),
    ]
    result = _ingest(tmp_path / "bounds.jsonl", frames, annotations)
    assert [item.example.label for item in result.examples] == ["jawOpen", "browInnerUp"]
    assert [item.example.window_start_timestamp_ms for item in result.examples] == [T0 + 10, T0 + 15]


def test_one_frame_per_interval_does_not_form_a_cross_interval_window(tmp_path):
    frames = [_frame(T0, 0.2), _frame(T0 + 20, 0.8)]
    annotations = [
        _interval("jawOpen", T0, T0 + 10),
        _interval("browInnerUp", T0 + 20, T0 + 30),
    ]
    result = _ingest(
        tmp_path / "separate.jsonl",
        frames,
        annotations,
        extraction=_extraction(2, 2),
    )
    assert result.examples == ()
    assert result.counts.windows_emitted == 0
    assert result.counts.accepted_frames == 2


def test_overlapping_annotations_fail_before_the_file_is_opened(tmp_path):
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(DatasetError) as exc:
        ingest_recording(
            missing,
            _source(),
            _recording(),
            [_interval("jawOpen", T0, T0 + 20), _interval("browInnerUp", T0 + 10, T0 + 30)],
            "train",
            _extraction(),
        )
    assert exc.value.code == "overlapping_annotations"
    assert not missing.exists()


def test_clip_label_is_review_required_and_does_not_label_windows(tmp_path):
    frames = [_frame(T0, 0.2), _frame(T0 + 10, 0.4)]
    clip_only = _ingest(tmp_path / "clip.jsonl", frames, [_clip()])
    assert clip_only.examples == ()
    assert clip_only.counts.unlabeled_frames == 2
    assert clip_only.reviews[0].recording_id == "r01"
    assert clip_only.reviews[0].reason == "clip_level_label_without_interval"
    assert clip_only.counts.review_required == 1

    both = _ingest(
        tmp_path / "both.jsonl",
        frames,
        [_clip(), _interval("jawOpen", T0, T0 + 20)],
    )
    assert [item.example.label for item in both.examples] == ["jawOpen", "jawOpen"]
    assert both.counts.review_required == 1
    assert both.counts.unlabeled_frames == 0


def test_unlabeled_frames_are_not_called_neutral_or_unknown(tmp_path):
    result = _ingest(tmp_path / "bare.jsonl", [_frame(T0), _frame(T0 + 10)], [])
    assert result.examples == ()
    assert result.counts.unlabeled_frames == 2
    assert result.counts.review_required == 0

    neutral = _ingest(
        tmp_path / "neutral.jsonl",
        [_frame(T0), _frame(T0 + 10)],
        [_interval("neutral", T0, T0 + 5)],
    )
    assert [item.example.label for item in neutral.examples] == ["neutral"]
    assert neutral.counts.unlabeled_frames == 1


def test_annotations_for_another_participant_or_session_are_rejected(tmp_path):
    other = _interval("jawOpen", T0, T0 + 10, recording=_recording(participant="p02"))
    with pytest.raises(DatasetError) as exc:
        ingest_recording(tmp_path / "nope.jsonl", _source(), _recording(), [other], "train", _extraction())
    assert exc.value.code == "annotation_does_not_match_recording"


def test_two_recordings_do_not_share_a_window(tmp_path):
    first = _ingest(
        tmp_path / "a.jsonl",
        [_frame(T0, 0.2)],
        [_interval("jawOpen", T0, T0 + 10)],
        extraction=_extraction(2, 1),
    )
    second = _ingest(
        tmp_path / "b.jsonl",
        [_frame(T0 + 10, 0.8)],
        [_interval("jawOpen", T0, T0 + 20, recording=_recording(participant="p02", session="s02", recording="r02"))],
        recording=_recording(participant="p02", session="s02", recording="r02"),
        extraction=_extraction(2, 1),
    )
    assert first.examples == ()
    assert second.examples == ()


def test_overlapping_windows_inside_one_interval_inherit_its_split(tmp_path):
    frames = [_frame(T0, 0.1), _frame(T0 + 10, 0.2), _frame(T0 + 20, 0.3)]
    result = _ingest(
        tmp_path / "overlap.jsonl",
        frames,
        [_interval("jawOpen", T0, T0 + 30)],
        split="calibration",
        extraction=_extraction(2, 1),
    )
    assert len(result.examples) == 2
    assert {item.example.split for item in result.examples} == {"calibration"}
    assert result.examples[0].example.window_end_timestamp_ms == result.examples[1].example.window_start_timestamp_ms


def test_caller_inputs_can_change_after_ingestion_without_changing_the_result(tmp_path):
    frames = [_frame(T0, 0.25)]
    path = tmp_path / "mutate.jsonl"
    _write(path, frames)
    frames[0]["features"]["jawOpen"] = 0.99
    annotations = [_interval("jawOpen", T0, T0 + 10)]
    raw = _source_mapping()
    source = parse_dataset_source(raw)
    raw["dataset_id"] = "changed"
    raw["email"] = "ada@example.com"
    result = ingest_recording(path, source, _recording(), annotations, "train", _extraction())
    annotations.append(_interval("browInnerUp", T0, T0 + 10))
    annotations[0] = _interval("browInnerUp", T0, T0 + 10)
    assert result.examples[0].example.label == "jawOpen"
    assert result.examples[0].dataset_id == "ds01"
    assert named_features(result.examples[0].example.features)[FEATURE_NAMES.index("jawOpen.mean")][1] == pytest.approx(0.25)
    assert "ada@example.com" not in str(result)


def test_rejected_paths_are_local_files_only(tmp_path):
    target = tmp_path / "real.jsonl"
    _write(target, [_frame(T0)])
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(DatasetError) as symlink:
        ingest_recording(link, _source(), _recording(), [], "train", _extraction())
    assert symlink.value.code == "symlink_not_allowed"
    with pytest.raises(DatasetError) as remote:
        ingest_recording(Path("https://example.com/features.jsonl"), _source(), _recording(), [], "train", _extraction())
    assert remote.value.code == "path_not_local"
    with pytest.raises(DatasetError) as folder:
        ingest_recording(tmp_path, _source(), _recording(), [], "train", _extraction())
    assert folder.value.code == "path_not_a_file"


def test_unauthorized_constructed_and_foreign_formats_are_refused(tmp_path):
    missing = tmp_path / "absent.jsonl"
    with pytest.raises(DatasetError) as measured:
        ingest_recording(
            missing,
            _source(),
            _recording(provenance="measured_capture"),
            [],
            "train",
            _extraction(),
        )
    assert measured.value.code == "constructed_data_is_not_a_measurement"
    with pytest.raises(DatasetError) as unauthorized:
        ingest_recording(
            missing,
            _source("consented_local", status="not_authorized"),
            _recording(provenance="measured_capture"),
            [],
            "train",
            _extraction(),
        )
    assert unauthorized.value.code == "source_not_authorized"
    with pytest.raises(DatasetError) as foreign:
        ingest_recording(
            missing,
            _source("authorized_public", source_format="facegest-mediapipe"),
            _recording(provenance="reconstructed_offline"),
            [],
            "train",
            _extraction(),
        )
    assert foreign.value.code == "unsupported_source_format"
    assert not missing.exists()


def test_reconstructed_timeline_is_not_marked_as_a_camera_capture(tmp_path):
    source = _source("consented_local")
    recording = _recording(provenance="reconstructed_offline")
    result = _ingest(
        tmp_path / "offline.jsonl",
        [_frame(T0, 0.2)],
        [_interval("jawOpen", T0, T0 + 10, recording=recording, provenance="reconstructed_offline")],
        source=source,
        recording=recording,
    )
    assert result.authorization_is_proof is False
    assert result.examples[0].timestamp_provenance == "reconstructed_offline"
    assert result.examples[0].timestamps_are_measured_capture is False


def test_measured_capture_flag_copies_the_caller_claim(tmp_path):
    # The frame below is constructed. The flag is true only because the
    # metadata says measured_capture. The numbers are not a camera observation.
    source = _source("consented_local")
    recording = _recording(provenance="measured_capture")
    result = _ingest(
        tmp_path / "claim.jsonl",
        [_frame(T0)],
        [_interval("jawOpen", T0, T0 + 1, recording=recording, provenance="measured_capture")],
        source=source,
        recording=recording,
    )
    assert result.examples[0].timestamps_are_measured_capture is True


def test_authorization_cannot_be_marked_as_proof():
    counts = IngestCounts(0, 0, 0, 0, 0, 0, 0, 0, 0)
    with pytest.raises(DatasetError) as exc:
        IngestResult(counts, (), (), (), True)
    assert exc.value.code == "authorization_is_not_proof"


def test_empty_interval_is_counted_and_not_filled(tmp_path):
    result = _ingest(
        tmp_path / "empty.jsonl",
        [_frame(T0)],
        [_interval("jawOpen", T0 + 100, T0 + 200)],
    )
    assert result.counts.empty_intervals == 1
    assert result.counts.unlabeled_frames == 1
    assert result.examples == ()


def test_valid_split_keeps_held_out_calibration_and_eval_in_different_sessions():
    train = _prepared("p01", "s01", "r01", "train")
    calibration = _prepared("p02", "s02", "r02", "calibration")
    evaluation = _prepared("p02", "s03", "r03", "eval")
    report = validate_splits(*_plan([train, calibration, evaluation]), [train, calibration, evaluation])
    assert report.population_train_participants == ("p01",)
    assert report.held_out_participants == ("p02",)
    assert report.fit_example_count == 2
    assert report.eval_example_count == 1
    assert "eval examples are refused as normalization or selection inputs" in report.enforced
    assert "cross-validation folds" in report.not_enforced
    # Fitting is scoped by purpose. See tests/test_ml_fitting_scope.py.
    plan = validate_split_plan(*_plan([train, calibration, evaluation]), [train, calibration, evaluation])
    population = fitting_scope(plan, "population")
    personal = fitting_scope(plan, "personalization", "p02")
    assert normalization_candidates([train, evaluation, calibration], population) == (train,)
    assert normalization_candidates([train, evaluation, calibration], personal) == (calibration,)
    assert assert_examples_may_fit([train], population).examples == (train,)
    assert assert_examples_may_fit([calibration], personal).examples == (calibration,)
    with pytest.raises(SplitPlanError) as exc:
        assert_examples_may_fit([train, evaluation], population)
    assert "eval_excluded_from_fitting" in exc.value.codes


def test_split_codes_are_sorted_and_deterministic():
    roles = [ParticipantRole("p01", "population_train")]
    sessions = [SessionSplit("p01", "s01", "eval"), SessionSplit("p01", "s01", "calibration")]
    with pytest.raises(SplitPlanError) as first:
        validate_splits(roles, sessions, [], [])
    with pytest.raises(SplitPlanError) as second:
        validate_splits(roles, sessions, [], [])
    assert first.value.codes == second.value.codes
    assert first.value.codes == tuple(sorted(first.value.codes))
    assert "participant_leakage" in first.value.codes
    assert "calibration_eval_session_leakage" in first.value.codes
    assert "session_split_conflict" in first.value.codes


def test_population_participant_in_eval_is_leakage():
    example = _prepared("p01", "s01", "r01", "eval")
    roles = [ParticipantRole("p01", "population_train")]
    sessions = [SessionSplit("p01", "s01", "eval")]
    recordings = [RecordingAssignment("r01", "p01", "s01", "eval")]
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, recordings, [example])
    assert exc.value.codes == ("participant_leakage", "population_participant_outside_train")


def test_population_calibration_is_outside_train_without_being_called_eval_leakage():
    example = _prepared("p01", "s01", "r01", "calibration")
    roles = [ParticipantRole("p01", "population_train")]
    sessions = [SessionSplit("p01", "s01", "calibration")]
    recordings = [RecordingAssignment("r01", "p01", "s01", "calibration")]
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, recordings, [example])
    assert exc.value.codes == ("population_participant_outside_train",)


def test_held_out_participant_in_train_is_leakage():
    example = _prepared("p02", "s01", "r01", "train")
    roles = [ParticipantRole("p02", "held_out")]
    sessions = [SessionSplit("p02", "s01", "train")]
    recordings = [RecordingAssignment("r01", "p02", "s01", "train")]
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, recordings, [example])
    assert exc.value.codes == ("held_out_participant_in_train", "participant_leakage")


def test_calibration_and_eval_from_one_session_fail():
    calibration = _prepared("p02", "s01", "r01", "calibration")
    evaluation = _prepared("p02", "s01", "r02", "eval")
    roles = [ParticipantRole("p02", "held_out")]
    sessions = [SessionSplit("p02", "s01", "calibration")]
    recordings = [
        RecordingAssignment("r01", "p02", "s01", "calibration"),
        RecordingAssignment("r02", "p02", "s01", "eval"),
    ]
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, recordings, [calibration, evaluation])
    assert "calibration_eval_session_leakage" in exc.value.codes
    assert "recording_split_conflict" in exc.value.codes
    assert "example_split_mismatch" in exc.value.codes


def test_duplicate_recording_identifiers_fail_even_when_identical():
    example = _prepared("p01", "s01", "r01", "train")
    roles, sessions, recordings = _plan([example])
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, [*recordings, recordings[0]], [example])
    assert exc.value.codes == ("duplicate_recording_id",)


def test_examples_from_one_recording_cannot_disagree():
    planted_features = [0.0] * FEATURE_DIM
    planted_features[3] = 987654.321
    train = _prepared("p01", "s01", "r01", "train", features=planted_features)
    evaluation = _prepared("p01", "s01", "r01", "eval", features=planted_features)
    roles = [ParticipantRole("p01", "population_train"), ParticipantRole("p02", "held_out")]
    sessions = [SessionSplit("p01", "s01", "train")]
    recordings = [RecordingAssignment("r01", "p01", "s01", "train")]
    with pytest.raises(SplitPlanError) as exc:
        validate_splits(roles, sessions, recordings, [train, evaluation])
    assert "recording_split_conflict" in exc.value.codes
    planted = "987654.321"
    assert planted not in str(exc.value)


def test_validate_splits_does_not_mutate_or_read_feature_values():
    features = [0.0] * FEATURE_DIM
    features[0] = 987654.321
    example = _prepared("p01", "s01", "r01", "train", features=features)
    roles, sessions, recordings = _plan([example])
    report = validate_splits(roles, sessions, recordings, [example])
    roles.append(ParticipantRole("p99", "held_out"))
    sessions.append(SessionSplit("p99", "s99", "eval"))
    features[0] = 1.0
    assert report.population_train_participants == ("p01",)
    assert report.held_out_participants == ()
    assert "987654.321" not in str(report)
    assert example.example.features[0] == 987654.321


def test_missing_role_and_missing_recording_fail_clearly():
    example = _prepared("p01", "s01", "r01", "train")
    with pytest.raises(SplitPlanError) as exc:
        validate_splits([], [], [], [example])
    assert "missing_participant_role" in exc.value.codes
    assert "example_recording_missing" in exc.value.codes
    assert "example_session_missing" in exc.value.codes


def test_public_package_surface_exports_the_dataset_api():
    import ml

    assert ml.ingest_recording is ingest_recording
    assert ml.FEATURE_JSONL_FORMAT == "sojourner-feature-jsonl"
    assert ml.SCHEMA_4_VERSION == "schema-4"
