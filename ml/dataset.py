"""Metadata, local JSONL ingestion, and split checks for later training.

This module does not train a model, estimate normalization, or publish a
gesture or a motor command. It reads local files only.

A camera line is one Schema 4 feature message, the same compact JSON object
``perception`` writes with one object per line. Validation and windowing go
through ``ml.schema`` and ``ml.windows``. Vectors come from ``extract_features``.

An authorization field records a human decision. It is not proof that the
decision was correct, and it is not a license grant.

A measured capture timestamp, a reconstructed offline timeline, and a
constructed fixture clock are different. This module will not call a
constructed or reconstructed number a measured camera observation.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ml.features import FEATURE_DIM, extract_features
from ml.records import LabeledExample
from ml.schema import is_timestamp, validate_feature
from ml.windows import WindowAssembler

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DECISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SPLITS = frozenset({"train", "calibration", "eval"})
_FIT_SPLITS = frozenset({"train", "calibration"})
_ROLES = frozenset({"population_train", "held_out"})
_CATEGORIES = frozenset({"consented_local", "authorized_public", "constructed_fixture"})
_AUTH_STATUS = frozenset({"human_authorized", "not_authorized", "constructed"})
_PROVENANCE = frozenset({"measured_capture", "constructed", "reconstructed_offline"})
_GRANULARITY = frozenset({"interval", "clip"})
FEATURE_JSONL_FORMAT = "sojourner-feature-jsonl"
SCHEMA_4_VERSION = "schema-4"

_SOURCE_KEYS = frozenset(
    {
        "dataset_id",
        "source_category",
        "source_format",
        "source_version",
        "authorization",
        "extraction_version",
    }
)
_AUTH_KEYS = frozenset({"status", "decision_id"})
_RECORDING_KEYS = frozenset(
    {
        "dataset_id",
        "participant_id",
        "session_id",
        "recording_id",
        "timestamp_provenance",
    }
)
_ANNOTATION_KEYS = frozenset(
    {
        "participant_id",
        "session_id",
        "recording_id",
        "label",
        "granularity",
        "start_ms",
        "end_ms",
        "timestamp_provenance",
    }
)

ENFORCED_SPLIT_RULES = (
    "one role per participant",
    "population participants stay in train",
    "held-out participants stay out of train",
    "one split per participant session",
    "calibration and eval for one held-out participant use different sessions",
    "one identity and one split per recording id",
    "every example matches its session and its recording",
    "eval examples are refused as normalization or selection inputs",
)
NOT_ENFORCED_SPLIT_RULES = (
    "how many people are held out",
    "whether a held-out participant supplied calibration",
    "whether calibration is short enough",
    "whether eval was recorded after calibration",
    "whether calibration is used for personalization only",
    "which model or hyperparameters will be selected",
    "whether a human authorization was actually correct",
    "cross-validation folds",
)


class DatasetError(ValueError):
    """A metadata or file problem. ``code`` is a stable token, not a record."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SplitPlanError(ValueError):
    """One or more split rules failed. ``codes`` is sorted and contains no rows."""

    def __init__(self, codes: tuple[str, ...]):
        self.codes = codes
        super().__init__(",".join(codes))


def _token(value, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise DatasetError(code)
    return value


def _exact_keys(data, allowed: frozenset[str]) -> None:
    if not isinstance(data, dict):
        raise DatasetError("metadata_not_an_object")
    if allowed - set(data):
        raise DatasetError("missing_metadata_field")
    if set(data) - allowed:
        raise DatasetError("unexpected_metadata_field")


def _clock(value, code: str) -> int:
    if not is_timestamp(value):
        raise DatasetError(code)
    return value


@dataclass(frozen=True)
class AuthorizationRecord:
    """A human decision id. Not a consent form and not proof of permission."""

    status: str
    decision_id: str

    def __post_init__(self) -> None:
        if self.status not in _AUTH_STATUS:
            raise DatasetError("invalid_authorization_status")
        _token(self.decision_id, _DECISION, "invalid_decision_id")


@dataclass(frozen=True)
class DatasetSource:
    """Where a recording came from, and the human decision recorded about it."""

    dataset_id: str
    source_category: str
    source_format: str
    source_version: str
    authorization: AuthorizationRecord
    extraction_version: str

    def __post_init__(self) -> None:
        _token(self.dataset_id, _TOKEN, "invalid_dataset_id")
        if self.source_category not in _CATEGORIES:
            raise DatasetError("invalid_source_category")
        _token(self.source_format, _DECISION, "invalid_source_format")
        _token(self.source_version, _DECISION, "invalid_source_version")
        if not isinstance(self.authorization, AuthorizationRecord):
            raise DatasetError("invalid_authorization")
        _token(self.extraction_version, _DECISION, "invalid_extraction_version")
        if self.source_category == "constructed_fixture":
            if self.authorization.status != "constructed":
                raise DatasetError("constructed_source_needs_constructed_authorization")
        elif self.authorization.status == "constructed":
            raise DatasetError("constructed_authorization_only_for_fixtures")


@dataclass(frozen=True)
class RecordingIdentity:
    """One local clip. Tokens are opaque. They are not names or email addresses."""

    dataset_id: str
    participant_id: str
    session_id: str
    recording_id: str
    timestamp_provenance: str

    def __post_init__(self) -> None:
        _token(self.dataset_id, _TOKEN, "invalid_dataset_id")
        _token(self.participant_id, _TOKEN, "invalid_participant_id")
        _token(self.session_id, _TOKEN, "invalid_session_id")
        _token(self.recording_id, _TOKEN, "invalid_recording_id")
        if self.timestamp_provenance not in _PROVENANCE:
            raise DatasetError("invalid_timestamp_provenance")

    @property
    def timestamps_are_measured_capture(self) -> bool:
        return self.timestamp_provenance == "measured_capture"


@dataclass(frozen=True)
class GestureAnnotation:
    """A human label for one recording.

    ``interval`` uses a half-open millisecond range ``[start_ms, end_ms)``.
    A timestamp equal to ``end_ms`` belongs to a later interval, not this one.
    ``clip`` means the label applies to the whole file and the onset and offset
    are unknown. Clip labels are not copied onto windows. ``unknown`` is not
    an annotation. ``neutral`` is allowed only when a person marked that interval.
    """

    participant_id: str
    session_id: str
    recording_id: str
    label: str
    granularity: str
    start_ms: int | None
    end_ms: int | None
    timestamp_provenance: str

    def __post_init__(self) -> None:
        _token(self.participant_id, _TOKEN, "invalid_participant_id")
        _token(self.session_id, _TOKEN, "invalid_session_id")
        _token(self.recording_id, _TOKEN, "invalid_recording_id")
        _token(self.label, _LABEL, "invalid_annotation_label")
        if self.label == "unknown":
            raise DatasetError("unknown_is_not_an_annotation")
        if self.granularity not in _GRANULARITY:
            raise DatasetError("invalid_annotation_granularity")
        if self.timestamp_provenance not in _PROVENANCE:
            raise DatasetError("invalid_timestamp_provenance")
        if self.granularity == "clip":
            if self.start_ms is not None or self.end_ms is not None:
                raise DatasetError("clip_label_has_no_interval")
            return
        start = _clock(self.start_ms, "invalid_annotation_time")
        end = _clock(self.end_ms, "invalid_annotation_time")
        if start >= end:
            raise DatasetError("annotation_interval_is_empty")


@dataclass(frozen=True)
class ExtractionConfig:
    """The ML-1 window settings used for this pass. This does not fit anything."""

    version: str
    length: int
    stride: int

    def __post_init__(self) -> None:
        _token(self.version, _DECISION, "invalid_extraction_version")
        if isinstance(self.length, bool) or not isinstance(self.length, int) or self.length < 1:
            raise DatasetError("invalid_extraction_length")
        if isinstance(self.stride, bool) or not isinstance(self.stride, int) or self.stride < 1:
            raise DatasetError("invalid_extraction_stride")


@dataclass(frozen=True)
class PreparedExample:
    """A ``LabeledExample`` plus the provenance of the recording it came from."""

    example: LabeledExample
    dataset_id: str
    recording_id: str
    timestamp_provenance: str
    timestamps_are_measured_capture: bool
    extraction_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.example, LabeledExample):
            raise DatasetError("invalid_example")
        _token(self.dataset_id, _TOKEN, "invalid_dataset_id")
        _token(self.recording_id, _TOKEN, "invalid_recording_id")
        if self.timestamp_provenance not in _PROVENANCE:
            raise DatasetError("invalid_timestamp_provenance")
        measured = self.timestamp_provenance == "measured_capture"
        if self.timestamps_are_measured_capture is not measured:
            raise DatasetError("timestamp_provenance_flag_mismatch")
        _token(self.extraction_version, _DECISION, "invalid_extraction_version")


@dataclass(frozen=True)
class RecordRejection:
    """One rejected JSONL record. The reason is a code. The line is not stored."""

    line_number: int
    reason: str


@dataclass(frozen=True)
class ReviewItem:
    """A case a person must resolve. No training row was created for it."""

    recording_id: str
    reason: str


@dataclass(frozen=True)
class IngestCounts:
    records_read: int
    accepted_frames: int
    rejected_records: int
    unlabeled_frames: int
    empty_intervals: int
    windows_emitted: int
    non_actionable_windows: int
    examples_emitted: int
    review_required: int


@dataclass(frozen=True)
class IngestResult:
    counts: IngestCounts
    rejections: tuple[RecordRejection, ...]
    reviews: tuple[ReviewItem, ...]
    examples: tuple[PreparedExample, ...]
    authorization_is_proof: bool

    def __post_init__(self) -> None:
        if self.authorization_is_proof is not False:
            raise DatasetError("authorization_is_not_proof")
        counts = self.counts
        if counts.rejected_records != len(self.rejections):
            raise DatasetError("counts_do_not_match")
        if counts.examples_emitted != len(self.examples):
            raise DatasetError("counts_do_not_match")
        if counts.review_required != len(self.reviews):
            raise DatasetError("counts_do_not_match")
        if counts.records_read != counts.accepted_frames + counts.rejected_records:
            raise DatasetError("counts_do_not_match")
        if counts.windows_emitted != counts.examples_emitted + counts.non_actionable_windows:
            raise DatasetError("counts_do_not_match")


@dataclass(frozen=True)
class ParticipantRole:
    participant_id: str
    role: str

    def __post_init__(self) -> None:
        _token(self.participant_id, _TOKEN, "invalid_participant_id")
        if self.role not in _ROLES:
            raise DatasetError("invalid_participant_role")


@dataclass(frozen=True)
class SessionSplit:
    participant_id: str
    session_id: str
    split: str

    def __post_init__(self) -> None:
        _token(self.participant_id, _TOKEN, "invalid_participant_id")
        _token(self.session_id, _TOKEN, "invalid_session_id")
        if self.split not in _SPLITS:
            raise DatasetError("invalid_split")


@dataclass(frozen=True)
class RecordingAssignment:
    recording_id: str
    participant_id: str
    session_id: str
    split: str

    def __post_init__(self) -> None:
        _token(self.recording_id, _TOKEN, "invalid_recording_id")
        _token(self.participant_id, _TOKEN, "invalid_participant_id")
        _token(self.session_id, _TOKEN, "invalid_session_id")
        if self.split not in _SPLITS:
            raise DatasetError("invalid_split")


@dataclass(frozen=True)
class SplitReport:
    """What a successful check established, and what it deliberately did not."""

    population_train_participants: tuple[str, ...]
    held_out_participants: tuple[str, ...]
    fit_example_count: int
    eval_example_count: int
    enforced: tuple[str, ...]
    not_enforced: tuple[str, ...]


def parse_authorization(data) -> AuthorizationRecord:
    _exact_keys(data, _AUTH_KEYS)
    return AuthorizationRecord(status=data["status"], decision_id=data["decision_id"])


def parse_dataset_source(data) -> DatasetSource:
    _exact_keys(data, _SOURCE_KEYS)
    return DatasetSource(
        dataset_id=data["dataset_id"],
        source_category=data["source_category"],
        source_format=data["source_format"],
        source_version=data["source_version"],
        authorization=parse_authorization(data["authorization"]),
        extraction_version=data["extraction_version"],
    )


def parse_recording_identity(data) -> RecordingIdentity:
    _exact_keys(data, _RECORDING_KEYS)
    return RecordingIdentity(
        dataset_id=data["dataset_id"],
        participant_id=data["participant_id"],
        session_id=data["session_id"],
        recording_id=data["recording_id"],
        timestamp_provenance=data["timestamp_provenance"],
    )


def parse_gesture_annotation(data) -> GestureAnnotation:
    _exact_keys(data, _ANNOTATION_KEYS)
    return GestureAnnotation(
        participant_id=data["participant_id"],
        session_id=data["session_id"],
        recording_id=data["recording_id"],
        label=data["label"],
        granularity=data["granularity"],
        start_ms=data["start_ms"],
        end_ms=data["end_ms"],
        timestamp_provenance=data["timestamp_provenance"],
    )


def _check_plan(source, recording, annotations, split, extraction):
    if not isinstance(source, DatasetSource) or not isinstance(recording, RecordingIdentity):
        raise DatasetError("invalid_metadata")
    if not isinstance(extraction, ExtractionConfig):
        raise DatasetError("invalid_extraction")
    if split not in _SPLITS:
        raise DatasetError("invalid_split")
    if source.dataset_id != recording.dataset_id:
        raise DatasetError("recording_dataset_mismatch")
    if source.extraction_version != extraction.version:
        raise DatasetError("extraction_version_mismatch")
    if source.source_format != FEATURE_JSONL_FORMAT or source.source_version != SCHEMA_4_VERSION:
        raise DatasetError("unsupported_source_format")
    if source.source_category == "constructed_fixture":
        if recording.timestamp_provenance != "constructed":
            raise DatasetError("constructed_data_is_not_a_measurement")
    elif source.authorization.status != "human_authorized":
        raise DatasetError("source_not_authorized")
    elif recording.timestamp_provenance == "constructed":
        raise DatasetError("real_source_cannot_use_constructed_timestamps")
    copied = tuple(annotations)
    reviews: list[ReviewItem] = []
    intervals: list[GestureAnnotation] = []
    for annotation in copied:
        if not isinstance(annotation, GestureAnnotation):
            raise DatasetError("invalid_annotation")
        same_person = (
            annotation.participant_id == recording.participant_id
            and annotation.session_id == recording.session_id
            and annotation.recording_id == recording.recording_id
        )
        if not same_person:
            raise DatasetError("annotation_does_not_match_recording")
        if annotation.timestamp_provenance != recording.timestamp_provenance:
            raise DatasetError("timestamp_provenance_mismatch")
        if annotation.granularity == "clip":
            reviews.append(ReviewItem(recording.recording_id, "clip_level_label_without_interval"))
        else:
            intervals.append(annotation)
    ordered = tuple(sorted(intervals, key=lambda item: (item.start_ms, item.end_ms, item.label)))
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.start_ms < later.end_ms and later.start_ms < earlier.end_ms:
            raise DatasetError("overlapping_annotations")
    return ordered, tuple(reviews)


def _local_file(path) -> Path:
    if not isinstance(path, Path):
        raise DatasetError("path_not_local")
    raw = str(path)
    if "://" in raw or raw.startswith("\\\\") or any(part.endswith(":") for part in path.parts):
        raise DatasetError("path_not_local")
    if path.is_symlink():
        raise DatasetError("symlink_not_allowed")
    if not path.is_file():
        raise DatasetError("path_not_a_file")
    return path.resolve()


def _loads_feature(line: str):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise DatasetError("duplicate_key")
            obj[key] = value
        return obj

    def reject_constant(_name):
        raise DatasetError("non_finite_number")

    try:
        value = json.loads(line, object_pairs_hook=pairs, parse_constant=reject_constant)
    except DatasetError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError):
        raise DatasetError("invalid_json") from None
    _walk_finite(value)
    if not isinstance(value, dict):
        raise DatasetError("not_an_object")
    return value


def _walk_finite(value) -> None:
    stack = [value]
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > 10000:
            raise DatasetError("invalid_json")
        if isinstance(node, float):
            if not math.isfinite(node):
                raise DatasetError("non_finite_number")
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _read_frames(path: Path):
    accepted: list[dict] = []
    rejections: list[RecordRejection] = []
    records_read = 0
    last_timestamp = None
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            records_read += 1
            try:
                message = _loads_feature(line)
            except DatasetError as exc:
                rejections.append(RecordRejection(line_number, exc.code))
                continue
            if validate_feature(message):
                rejections.append(RecordRejection(line_number, "schema_rejected"))
                continue
            timestamp = message["timestamp_ms"]
            if last_timestamp is not None and timestamp <= last_timestamp:
                rejections.append(RecordRejection(line_number, "out_of_order"))
                continue
            last_timestamp = timestamp
            accepted.append(copy.deepcopy(message))
    return records_read, accepted, tuple(rejections)


def _window_examples(frames, annotation, *, split, source, recording, length, stride):
    assembler = WindowAssembler(length=length, stride=stride)
    examples: list[PreparedExample] = []
    windows = 0
    non_actionable = 0
    for frame in frames:
        pushed = assembler.push(frame)
        if not pushed.accepted:
            raise DatasetError("accepted_frame_rejected_by_assembler")
        if pushed.window is None:
            continue
        windows += 1
        vector = extract_features(pushed.window)
        if vector is None:
            non_actionable += 1
            continue
        if len(vector) != FEATURE_DIM:
            raise DatasetError("feature_dimension_mismatch")
        try:
            labeled = LabeledExample(
                participant_id=recording.participant_id,
                session_id=recording.session_id,
                split=split,
                label=annotation.label,
                features=vector,
                window_start_timestamp_ms=pushed.window.start_timestamp_ms,
                window_end_timestamp_ms=pushed.window.end_timestamp_ms,
                frame_count=pushed.window.frame_count,
            )
        except ValueError:
            raise DatasetError("example_rejected") from None
        examples.append(
            PreparedExample(
                example=labeled,
                dataset_id=source.dataset_id,
                recording_id=recording.recording_id,
                timestamp_provenance=recording.timestamp_provenance,
                timestamps_are_measured_capture=recording.timestamps_are_measured_capture,
                extraction_version=source.extraction_version,
            )
        )
    return tuple(examples), windows, non_actionable


def ingest_recording(
    path: Path,
    source: DatasetSource,
    recording: RecordingIdentity,
    annotations: Sequence[GestureAnnotation],
    split: str,
    extraction: ExtractionConfig,
) -> IngestResult:
    """Read one local JSONL recording and return examples plus rejection counts.

    Each annotation interval gets its own window assembler. A window cannot
    combine two intervals, and one call cannot combine two recordings. Frames
    outside every interval stay unlabeled. A clip-level label is reported for
    review and is not applied to windows. Rejected lines are counted and are
    not inserted. Missing timestamps are not filled in.
    """
    intervals, reviews = _check_plan(source, recording, annotations, split, extraction)
    records_read, accepted, rejections = _read_frames(_local_file(path))
    buckets: dict[int, list[dict]] = {index: [] for index in range(len(intervals))}
    unlabeled = 0
    for frame in accepted:
        timestamp = frame["timestamp_ms"]
        owner = None
        for index, annotation in enumerate(intervals):
            if annotation.start_ms <= timestamp < annotation.end_ms:
                owner = index
                break
        if owner is None:
            unlabeled += 1
        else:
            buckets[owner].append(frame)
    examples: list[PreparedExample] = []
    windows = 0
    non_actionable = 0
    empty_intervals = 0
    for index, annotation in enumerate(intervals):
        frames = buckets[index]
        if not frames:
            empty_intervals += 1
            continue
        produced, produced_windows, produced_non_actionable = _window_examples(
            frames,
            annotation,
            split=split,
            source=source,
            recording=recording,
            length=extraction.length,
            stride=extraction.stride,
        )
        examples.extend(produced)
        windows += produced_windows
        non_actionable += produced_non_actionable
    counts = IngestCounts(
        records_read=records_read,
        accepted_frames=len(accepted),
        rejected_records=len(rejections),
        unlabeled_frames=unlabeled,
        empty_intervals=empty_intervals,
        windows_emitted=windows,
        non_actionable_windows=non_actionable,
        examples_emitted=len(examples),
        review_required=len(reviews),
    )
    return IngestResult(
        counts=counts,
        rejections=rejections,
        reviews=reviews,
        examples=tuple(examples),
        authorization_is_proof=False,
    )


def _role_split(role: str, split: str, codes: set[str]) -> None:
    if role == "population_train" and split != "train":
        codes.add("population_participant_outside_train")
        if split == "eval":
            codes.add("participant_leakage")
    if role == "held_out" and split == "train":
        codes.add("held_out_participant_in_train")
        codes.add("participant_leakage")


def validate_splits(
    roles: Sequence[ParticipantRole],
    sessions: Sequence[SessionSplit],
    recordings: Sequence[RecordingAssignment],
    examples: Sequence[PreparedExample],
) -> SplitReport:
    """Check a split plan. Raise ``SplitPlanError`` instead of returning a partial plan.

    The sequences are copied before they are read. This function does not fit
    normalization, select parameters, or look at feature values.
    """
    roles = tuple(roles)
    sessions = tuple(sessions)
    recordings = tuple(recordings)
    examples = tuple(examples)
    for role in roles:
        if not isinstance(role, ParticipantRole):
            raise DatasetError("invalid_participant_role")
    for session in sessions:
        if not isinstance(session, SessionSplit):
            raise DatasetError("invalid_split")
    for recording in recordings:
        if not isinstance(recording, RecordingAssignment):
            raise DatasetError("invalid_recording_id")
    codes: set[str] = set()
    role_by_participant: dict[str, str] = {}
    for role in roles:
        previous = role_by_participant.get(role.participant_id)
        if previous is None:
            role_by_participant[role.participant_id] = role.role
        elif previous != role.role:
            codes.add("participant_role_conflict")
        else:
            codes.add("duplicate_participant_role")
    session_by_key: dict[tuple[str, str], str] = {}
    for session in sessions:
        key = (session.participant_id, session.session_id)
        previous = session_by_key.get(key)
        if previous is None:
            session_by_key[key] = session.split
        elif previous != session.split:
            codes.add("session_split_conflict")
            if {previous, session.split} <= {"calibration", "eval"}:
                codes.add("calibration_eval_session_leakage")
        else:
            codes.add("duplicate_session_assignment")
        role = role_by_participant.get(session.participant_id)
        if role is None:
            codes.add("missing_participant_role")
        else:
            _role_split(role, session.split, codes)
    recording_by_id: dict[str, tuple[str, str, str]] = {}
    for recording in recordings:
        identity = (recording.participant_id, recording.session_id, recording.split)
        previous = recording_by_id.get(recording.recording_id)
        if previous is None:
            recording_by_id[recording.recording_id] = identity
        else:
            codes.add("duplicate_recording_id")
            if previous != identity:
                codes.add("recording_split_conflict")
        role = role_by_participant.get(recording.participant_id)
        if role is None:
            codes.add("missing_participant_role")
        else:
            _role_split(role, recording.split, codes)
        session_split = session_by_key.get((recording.participant_id, recording.session_id))
        if session_split is None:
            codes.add("recording_session_missing")
        elif session_split != recording.split:
            codes.add("recording_split_conflict")
    splits_by_session: dict[tuple[str, str], set[str]] = {}
    identities_by_recording: dict[str, set[tuple[str, str, str]]] = {}
    fit_count = 0
    eval_count = 0
    for prepared in examples:
        if not isinstance(prepared, PreparedExample):
            raise DatasetError("invalid_example")
        labeled = prepared.example
        identity = (labeled.participant_id, labeled.session_id, labeled.split)
        session_key = (labeled.participant_id, labeled.session_id)
        splits_by_session.setdefault(session_key, set()).add(labeled.split)
        identities_by_recording.setdefault(prepared.recording_id, set()).add(identity)
        if labeled.split == "eval":
            eval_count += 1
        elif labeled.split in _FIT_SPLITS:
            fit_count += 1
        role = role_by_participant.get(labeled.participant_id)
        if role is None:
            codes.add("missing_participant_role")
        else:
            _role_split(role, labeled.split, codes)
        expected = session_by_key.get(session_key)
        if expected is None:
            codes.add("example_session_missing")
        elif expected != labeled.split:
            codes.add("example_split_mismatch")
        assigned = recording_by_id.get(prepared.recording_id)
        if assigned is None:
            codes.add("example_recording_missing")
        elif assigned != identity:
            codes.add("example_recording_mismatch")
    for splits in splits_by_session.values():
        if len(splits) > 1:
            codes.add("session_split_conflict")
            if "calibration" in splits and "eval" in splits:
                codes.add("calibration_eval_session_leakage")
    for identities in identities_by_recording.values():
        if len(identities) > 1:
            codes.add("recording_split_conflict")
    if codes:
        raise SplitPlanError(tuple(sorted(codes)))
    return SplitReport(
        population_train_participants=tuple(
            sorted(pid for pid, role in role_by_participant.items() if role == "population_train")
        ),
        held_out_participants=tuple(
            sorted(pid for pid, role in role_by_participant.items() if role == "held_out")
        ),
        fit_example_count=fit_count,
        eval_example_count=eval_count,
        enforced=ENFORCED_SPLIT_RULES,
        not_enforced=NOT_ENFORCED_SPLIT_RULES,
    )


def normalization_candidates(examples: Sequence[PreparedExample]) -> tuple[PreparedExample, ...]:
    """Return train and calibration examples, in input order, with eval left out.

    This does not estimate a mean or a scale.
    """
    kept: list[PreparedExample] = []
    for item in examples:
        if not isinstance(item, PreparedExample):
            raise DatasetError("invalid_example")
        if item.example.split != "eval":
            kept.append(item)
    return tuple(kept)


def assert_examples_may_fit(examples: Sequence[PreparedExample]) -> None:
    """Refuse a sequence that contains an eval example.

    A later trainer must call this on the rows it would use to fit
    normalization or choose parameters. This function does not fit them.
    Calibration rows are allowed through because ML-1 already treats
    calibration as a fit source for personalization. Eval rows are not.
    """
    for item in examples:
        if not isinstance(item, PreparedExample):
            raise DatasetError("invalid_example")
        if item.example.split == "eval":
            raise SplitPlanError(("eval_excluded_from_fitting",))
