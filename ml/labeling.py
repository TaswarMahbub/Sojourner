"""Local review of gesture intervals on an existing Schema 4 JSONL file.

This module does not open a camera, train a model, or publish a gesture or a
motor command. Frames stay in the recording the operator already has.
Annotations are stored beside it, and only confirmed intervals are passed to
``ingest_recording``.

A prompted mark is a candidate. Confirming it records an operator review of
the onset, the offset, and the label. It does not prove the movement happened.
``authorization_is_proof`` stays false. A constructed fixture cannot be given
a measured-capture claim.

Timestamps come from accepted frames. Nothing is placed on a 30 FPS grid.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ml.dataset import (
    FEATURE_JSONL_FORMAT,
    SCHEMA_4_VERSION,
    AuthorizationRecord,
    DatasetError,
    DatasetSource,
    ExtractionConfig,
    GestureAnnotation,
    ParticipantRole,
    RecordingAssignment,
    RecordingIdentity,
    SessionSplit,
    SplitPlanError,
    _local_file,
    _loads_feature,
    _physical_lines,
    _read_frames,
    ingest_recording,
    validate_split_plan,
)

# ``_read_frames`` and the helpers above are the ML-2A reader. Labeling calls
# them so it does not keep a second JSONL parser or a second Schema 4 check.
from ml.features import FEATURE_DIM
from ml.schema import is_timestamp

SESSION_FORMAT = "sojourner-label-session-v1"
ANNOTATION_FORMAT = "sojourner-label-annotations-v1"
MARKER_NAME = "SOJOURNER_LABEL_SESSION"
CANDIDATE_LABELS = (
    "neutral",
    "brow_raise",
    "deliberate_blink",
    "mouth_open",
    "head_left",
    "head_right",
    "select",
)
_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CONDITIONS = re.compile(r"[A-Za-z0-9 ,./-]{0,120}")
_CLAIMS = frozenset({"constructed_fixture", "operator_claims_camera"})
_STATUSES = frozenset({"candidate", "confirmed", "uncertain"})
_PRIVATE_DIRS = frozenset({"captures", "datasets"})
_GENERATED = frozenset(
    {MARKER_NAME, "session.json", "annotations.json", "quality-report.json", ".quality-report.json.tmp", ".session.json.tmp", ".annotations.json.tmp"}
)
_REPO_ROOT = Path(__file__).resolve().parent.parent

_SESSION_KEYS = frozenset(
    {
        "session_format",
        "dataset_id",
        "participant_id",
        "session_id",
        "recording_id",
        "recording_path",
        "source_category",
        "source_format",
        "source_version",
        "extraction_version",
        "timestamp_provenance",
        "split",
        "participant_role",
        "authorization_status",
        "authorization_decision_id",
        "reviewed_labels",
        "window_length",
        "window_stride",
        "capture_claim",
        "conditions",
        "accepted_frames",
        "first_timestamp_ms",
        "last_timestamp_ms",
        "byte_length",
        "sha256",
        "split_history",
    }
)
_ANNOTATION_KEYS = frozenset(
    {
        "annotation_format",
        "participant_id",
        "session_id",
        "recording_id",
        "timestamp_provenance",
        "intervals",
        "clip_label",
    }
)
_INTERVAL_KEYS = frozenset({"start_ms", "end_ms", "label", "status", "prompted"})


class LabelingError(ValueError):
    """An operator or file problem. ``code`` is a stable token, not a record."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RecordingOverview:
    """Counts and timestamp bounds. Frame payloads are not kept."""

    accepted_frames: int
    rejected_records: int
    rejection_reasons: tuple[tuple[str, int], ...]
    timeline_start_ms: int | None
    timeline_end_ms: int | None
    observed_span_ms: int | None
    smallest_step_ms: int | None
    largest_step_ms: int | None
    tracking_frames: int
    partial_frames: int
    lost_frames: int

    def to_text(self) -> str:
        return _lines(self._pairs())

    def _pairs(self) -> list[tuple[str, object]]:
        rows: list[tuple[str, object]] = [
            ("accepted_frames", self.accepted_frames),
            ("rejected_records", self.rejected_records),
            ("timeline_start_ms", self.timeline_start_ms),
            ("timeline_end_ms", self.timeline_end_ms),
            ("observed_span_ms", self.observed_span_ms),
            ("smallest_step_ms", self.smallest_step_ms),
            ("largest_step_ms", self.largest_step_ms),
            ("tracking_frames", self.tracking_frames),
            ("partial_frames", self.partial_frames),
            ("lost_frames", self.lost_frames),
        ]
        for reason, count in self.rejection_reasons:
            rows.append((f"rejected_{reason}", count))
        return rows


@dataclass(frozen=True)
class MarkedInterval:
    start_ms: int
    end_ms: int
    label: str | None
    status: str
    prompted: bool


@dataclass(frozen=True)
class QualityReport:
    """Factual counts for one reviewed recording. No feature values."""

    participant_id: str
    session_id: str
    recording_id: str
    source_category: str
    capture_claim: str
    timestamp_provenance: str
    timestamps_are_measured_capture: bool
    authorization_is_proof: bool
    split: str
    participant_role: str
    records_read: int
    accepted_frames: int
    rejected_records: int
    rejection_reasons: tuple[tuple[str, int], ...]
    timeline_start_ms: int | None
    timeline_end_ms: int | None
    observed_span_ms: int | None
    smallest_step_ms: int | None
    largest_step_ms: int | None
    tracking_frames: int
    partial_frames: int
    lost_frames: int
    tracking_loss_frames: int
    confirmed_intervals: int
    candidate_intervals: int
    uncertain_intervals: int
    prompted_confirmed_intervals: int
    per_label_counts: tuple[tuple[str, int], ...]
    unlabeled_frames: int
    intervals_without_frames: int
    non_actionable_frames_in_intervals: int
    non_actionable_windows: int
    review_required: int
    review_reasons: tuple[str, ...]
    block_reasons: tuple[str, ...]
    examples_emitted: int
    examples_blocked_by_split: int
    example_windows: tuple[tuple[int, int, str, int], ...]
    feature_dimension: int
    split_accepted: bool
    split_errors: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "participant_id": self.participant_id,
            "session_id": self.session_id,
            "recording_id": self.recording_id,
            "source_category": self.source_category,
            "capture_claim": self.capture_claim,
            "timestamp_provenance": self.timestamp_provenance,
            "timestamps_are_measured_capture": self.timestamps_are_measured_capture,
            "authorization_is_proof": False,
            "split": self.split,
            "participant_role": self.participant_role,
            "records_read": self.records_read,
            "accepted_frames": self.accepted_frames,
            "rejected_records": self.rejected_records,
            "rejection_reasons": [list(item) for item in self.rejection_reasons],
            "timeline_start_ms": self.timeline_start_ms,
            "timeline_end_ms": self.timeline_end_ms,
            "observed_span_ms": self.observed_span_ms,
            "smallest_step_ms": self.smallest_step_ms,
            "largest_step_ms": self.largest_step_ms,
            "tracking_frames": self.tracking_frames,
            "partial_frames": self.partial_frames,
            "lost_frames": self.lost_frames,
            "tracking_loss_frames": self.tracking_loss_frames,
            "confirmed_intervals": self.confirmed_intervals,
            "candidate_intervals": self.candidate_intervals,
            "uncertain_intervals": self.uncertain_intervals,
            "prompted_confirmed_intervals": self.prompted_confirmed_intervals,
            "per_label_counts": [list(item) for item in self.per_label_counts],
            "unlabeled_frames": self.unlabeled_frames,
            "intervals_without_frames": self.intervals_without_frames,
            "non_actionable_frames_in_intervals": self.non_actionable_frames_in_intervals,
            "non_actionable_windows": self.non_actionable_windows,
            "review_required": self.review_required,
            "review_reasons": list(self.review_reasons),
            "block_reasons": list(self.block_reasons),
            "examples_emitted": self.examples_emitted,
            "examples_blocked_by_split": self.examples_blocked_by_split,
            "example_windows": [list(item) for item in self.example_windows],
            "feature_dimension": self.feature_dimension,
            "split_accepted": self.split_accepted,
            "split_errors": list(self.split_errors),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"

    def to_text(self) -> str:
        payload = self.to_dict()
        rows = []
        for key in sorted(payload):
            value = payload[key]
            if isinstance(value, list):
                if not value:
                    rows.append(f"{key}=")
                for item in value:
                    rendered = ",".join(str(part) for part in item) if isinstance(item, list) else str(item)
                    rows.append(f"{key} {rendered}")
            else:
                rows.append(f"{key}={_plain(value)}")
        return "\n".join(rows) + "\n"


@dataclass(frozen=True)
class DiscardResult:
    deleted_generated: int
    deleted_recording: bool
    recording_outside_session: bool
    unexpected_files_left: int
    directory_removed: bool


def _lines(pairs: Sequence[tuple[str, object]]) -> str:
    return "".join(f"{name}={_plain(value)}\n" for name, value in pairs)


def _plain(value: object) -> str:
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _exact(data, allowed: frozenset[str]) -> None:
    if not isinstance(data, dict):
        raise LabelingError("metadata_not_an_object")
    if allowed - set(data):
        raise LabelingError("missing_metadata_field")
    if set(data) - allowed:
        raise LabelingError("unexpected_metadata_field")


def _label_name(value, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or _LABEL.fullmatch(value) is None:
        raise LabelingError("invalid_annotation_label")
    if value == "unknown":
        raise LabelingError("unknown_is_not_an_annotation")
    return value


def _clock(value) -> int:
    if not is_timestamp(value):
        raise LabelingError("invalid_annotation_time")
    return value


def _conditions(value: str) -> str:
    if not isinstance(value, str) or _CONDITIONS.fullmatch(value) is None:
        raise LabelingError("invalid_conditions")
    return value


def _output_dir(path: Path) -> Path:
    if not isinstance(path, Path):
        raise LabelingError("path_not_local")
    raw = str(path)
    if "://" in raw or raw.startswith("\\\\") or any(part.endswith(":") for part in path.parts):
        raise LabelingError("path_not_local")
    if path.is_symlink():
        raise LabelingError("symlink_not_allowed")
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(_REPO_ROOT)
    except ValueError:
        return resolved
    if not relative.parts or relative.parts[0] not in _PRIVATE_DIRS:
        raise LabelingError("output_not_in_private_directory")
    return resolved


def _atomic_write(path: Path, text: str, *, allow_replace: bool) -> None:
    if path.is_symlink():
        raise LabelingError("symlink_not_allowed")
    if path.exists() and not allow_replace:
        raise LabelingError("output_exists")
    temporary = path.with_name("." + path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    except LabelingError:
        raise
    except Exception:
        temporary.unlink(missing_ok=True)
        raise LabelingError("output_write_failed") from None


def _read_json(path: Path, allowed: frozenset[str]):
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LabelingError("metadata_unreadable") from None
    _exact(data, allowed)
    return data


def _fingerprint(path: Path):
    local = _local_file(path)
    digest = hashlib.sha256()
    size = 0
    with local.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    _reject_calibration(local)
    _read_count, accepted, rejections = _read_frames(local)
    timestamps = tuple(frame["timestamp_ms"] for frame in accepted)
    states = tuple(frame["tracking_state"] for frame in accepted)
    overview = _overview(timestamps, states, rejections)
    return local, digest.hexdigest(), size, overview, timestamps, states


def _reject_calibration(path: Path) -> None:
    with path.open("rb") as handle:
        for raw in _physical_lines(handle):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if not line.strip():
                continue
            try:
                value = _loads_feature(line)
            except DatasetError:
                continue
            if isinstance(value, dict) and "record_type" in value:
                raise LabelingError("calibration_recording_not_feature_jsonl")


def _overview(timestamps, states, rejections) -> RecordingOverview:
    reasons: dict[str, int] = {}
    for rejection in rejections:
        reasons[rejection.reason] = reasons.get(rejection.reason, 0) + 1
    start = timestamps[0] if timestamps else None
    end = timestamps[-1] if timestamps else None
    steps = tuple(later - earlier for earlier, later in zip(timestamps, timestamps[1:]))
    counts = {name: 0 for name in ("tracking", "partial", "lost")}
    for state in states:
        if state in counts:
            counts[state] += 1
    return RecordingOverview(
        accepted_frames=len(timestamps),
        rejected_records=len(rejections),
        rejection_reasons=tuple(sorted(reasons.items())),
        timeline_start_ms=start,
        timeline_end_ms=end,
        observed_span_ms=None if start is None else end - start,
        smallest_step_ms=min(steps) if steps else None,
        largest_step_ms=max(steps) if steps else None,
        tracking_frames=counts["tracking"],
        partial_frames=counts["partial"],
        lost_frames=counts["lost"],
    )


def inspect_recording(path: Path) -> RecordingOverview:
    """Validate one local feature file with the ML-2A reader and summarize it."""
    _local, _digest, _size, overview, _timestamps, _states = _fingerprint(path)
    return overview


def _reviewed(labels: Sequence[str] | None, extra: Sequence[str]) -> tuple[str, ...]:
    chosen = tuple(CANDIDATE_LABELS if labels is None else labels)
    additions = tuple(extra)
    for name in additions:
        _label_name(name, allow_none=False)
    allowed = set(CANDIDATE_LABELS).union(additions)
    seen = set()
    ordered = []
    for name in (*chosen, *additions):
        checked = _label_name(name, allow_none=False)
        if checked not in allowed:
            raise LabelingError("unsupported_label")
        if checked not in CANDIDATE_LABELS and checked not in additions:
            raise LabelingError("unsupported_label")
        if checked in seen:
            raise LabelingError("duplicate_label")
        seen.add(checked)
        ordered.append(checked)
    return tuple(ordered)


def _claim_pair(claim: str, category: str, provenance: str, authorization: str) -> None:
    if claim not in _CLAIMS:
        raise LabelingError("invalid_capture_claim")
    if claim == "constructed_fixture":
        if category != "constructed_fixture" or provenance != "constructed" or authorization != "constructed":
            raise LabelingError("constructed_data_is_not_a_measurement")
        return
    if category != "consented_local" or authorization != "human_authorized":
        raise LabelingError("capture_claim_mismatch")
    if provenance not in {"measured_capture", "reconstructed_offline"}:
        raise LabelingError("capture_claim_mismatch")


def _idents(session_fields):
    source = DatasetSource(
        dataset_id=session_fields["dataset_id"],
        source_category=session_fields["source_category"],
        source_format=FEATURE_JSONL_FORMAT,
        source_version=SCHEMA_4_VERSION,
        authorization=AuthorizationRecord(
            session_fields["authorization_status"], session_fields["authorization_decision_id"]
        ),
        extraction_version=session_fields["extraction_version"],
    )
    recording = RecordingIdentity(
        dataset_id=session_fields["dataset_id"],
        participant_id=session_fields["participant_id"],
        session_id=session_fields["session_id"],
        recording_id=session_fields["recording_id"],
        timestamp_provenance=session_fields["timestamp_provenance"],
    )
    extraction = ExtractionConfig(
        session_fields["extraction_version"],
        session_fields["window_length"],
        session_fields["window_stride"],
    )
    role = ParticipantRole(session_fields["participant_id"], session_fields["participant_role"])
    session = SessionSplit(
        session_fields["participant_id"], session_fields["session_id"], session_fields["split"]
    )
    assignment = RecordingAssignment(
        session_fields["recording_id"],
        session_fields["participant_id"],
        session_fields["session_id"],
        session_fields["split"],
    )
    return source, recording, extraction, role, session, assignment


def create_session(
    recording: Path,
    output: Path,
    *,
    dataset_id: str,
    participant_id: str,
    session_id: str,
    recording_id: str,
    split: str,
    participant_role: str,
    source_category: str,
    timestamp_provenance: str,
    capture_claim: str,
    decision_id: str,
    reviewed_labels: Sequence[str] | None = None,
    extra_labels: Sequence[str] = (),
    extraction_version: str = "ml1-window-v1",
    window_length: int = 1,
    window_stride: int = 1,
    conditions: str = "",
    replace: bool = False,
) -> Path:
    """Create a session directory of metadata. The recording file is not copied or modified."""
    authorization = "constructed" if capture_claim == "constructed_fixture" else "human_authorized"
    _claim_pair(capture_claim, source_category, timestamp_provenance, authorization)
    labels = _reviewed(reviewed_labels, extra_labels)
    note = _conditions(conditions)
    fields = {
        "dataset_id": dataset_id,
        "participant_id": participant_id,
        "session_id": session_id,
        "recording_id": recording_id,
        "source_category": source_category,
        "authorization_status": authorization,
        "authorization_decision_id": decision_id,
        "extraction_version": extraction_version,
        "timestamp_provenance": timestamp_provenance,
        "window_length": window_length,
        "window_stride": window_stride,
        "split": split,
        "participant_role": participant_role,
    }
    source, identity, extraction, role, bound, assignment = _idents(fields)
    validate_split_plan((role,), (bound,), (assignment,), ())
    local, digest, size, overview, _timestamps, _states = _fingerprint(recording)
    destination = _output_dir(output)
    _prepare_directory(destination, local, replace)
    payload = {
        "session_format": SESSION_FORMAT,
        "dataset_id": source.dataset_id,
        "participant_id": identity.participant_id,
        "session_id": identity.session_id,
        "recording_id": identity.recording_id,
        "recording_path": str(local),
        "source_category": source.source_category,
        "source_format": source.source_format,
        "source_version": source.source_version,
        "extraction_version": extraction.version,
        "timestamp_provenance": identity.timestamp_provenance,
        "split": split,
        "participant_role": participant_role,
        "authorization_status": authorization,
        "authorization_decision_id": decision_id,
        "reviewed_labels": list(labels),
        "window_length": extraction.length,
        "window_stride": extraction.stride,
        "capture_claim": capture_claim,
        "conditions": note,
        "accepted_frames": overview.accepted_frames,
        "first_timestamp_ms": overview.timeline_start_ms,
        "last_timestamp_ms": overview.timeline_end_ms,
        "byte_length": size,
        "sha256": digest,
        "split_history": [],
    }
    annotations = {
        "annotation_format": ANNOTATION_FORMAT,
        "participant_id": identity.participant_id,
        "session_id": identity.session_id,
        "recording_id": identity.recording_id,
        "timestamp_provenance": identity.timestamp_provenance,
        "intervals": [],
        "clip_label": None,
    }
    try:
        _atomic_write(destination / MARKER_NAME, SESSION_FORMAT + "\n", allow_replace=replace)
        _atomic_write(
            destination / "session.json",
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            allow_replace=replace,
        )
        _atomic_write(
            destination / "annotations.json",
            json.dumps(annotations, sort_keys=True, separators=(",", ":")) + "\n",
            allow_replace=replace,
        )
    except Exception:
        _cleanup_partial(destination, local)
        raise
    return destination


def _prepare_directory(destination: Path, recording: Path, replace: bool) -> None:
    if destination.exists() and not destination.is_dir():
        raise LabelingError("output_not_a_directory")
    destination.mkdir(parents=True, exist_ok=True)
    present = {item.name for item in destination.iterdir()}
    allowed = set(_GENERATED)
    if recording.parent == destination:
        allowed.add(recording.name)
    unexpected = present - allowed
    if unexpected:
        raise LabelingError("directory_not_empty")
    if present.intersection(_GENERATED) and not replace:
        raise LabelingError("output_exists")
    if replace:
        for name in present.intersection(_GENERATED):
            target = destination / name
            if target.is_symlink():
                raise LabelingError("symlink_not_allowed")
            target.unlink()


def _cleanup_partial(destination: Path, recording: Path) -> None:
    if not destination.is_dir():
        return
    for name in _GENERATED:
        candidate = destination / name
        if candidate.is_symlink():
            continue
        if candidate.exists():
            candidate.unlink()
    remaining = list(destination.iterdir())
    if not remaining or (len(remaining) == 1 and remaining[0].resolve() == recording.resolve()):
        if not remaining:
            destination.rmdir()


def _load(directory: Path):
    destination = _output_dir(directory)
    marker = destination / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        raise LabelingError("not_a_label_session")
    if marker.read_text(encoding="utf-8") != SESSION_FORMAT + "\n":
        raise LabelingError("not_a_label_session")
    session = _read_json(destination / "session.json", _SESSION_KEYS)
    annotations = _read_json(destination / "annotations.json", _ANNOTATION_KEYS)
    if session["session_format"] != SESSION_FORMAT or annotations["annotation_format"] != ANNOTATION_FORMAT:
        raise LabelingError("metadata_unreadable")
    for key in ("participant_id", "session_id", "recording_id"):
        if annotations[key] != session[key]:
            raise LabelingError("annotation_session_mismatch")
    return destination, session, annotations


def _intervals(data) -> tuple[MarkedInterval, ...]:
    rows = data["intervals"]
    if not isinstance(rows, list):
        raise LabelingError("invalid_annotation")
    parsed = []
    for row in rows:
        _exact(row, _INTERVAL_KEYS)
        status = row["status"]
        if status not in _STATUSES:
            raise LabelingError("invalid_annotation_status")
        label = _label_name(row["label"], allow_none=True)
        if status != "uncertain" and label is None:
            raise LabelingError("label_required")
        if not isinstance(row["prompted"], bool):
            raise LabelingError("invalid_annotation")
        start = _clock(row["start_ms"])
        end = _clock(row["end_ms"])
        if start >= end:
            raise LabelingError("annotation_interval_is_empty")
        parsed.append(MarkedInterval(start, end, label, status, row["prompted"]))
    return tuple(parsed)


def _save_annotations(directory: Path, session, intervals: Sequence[MarkedInterval], clip_label) -> None:
    payload = {
        "annotation_format": ANNOTATION_FORMAT,
        "participant_id": session["participant_id"],
        "session_id": session["session_id"],
        "recording_id": session["recording_id"],
        "timestamp_provenance": session["timestamp_provenance"],
        "intervals": [
            {
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "label": item.label,
                "status": item.status,
                "prompted": item.prompted,
            }
            for item in intervals
        ],
        "clip_label": clip_label,
    }
    _atomic_write(
        directory / "annotations.json",
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        allow_replace=True,
    )


def _problems(intervals: Sequence[MarkedInterval], labels: set[str], timestamps: Sequence[int]) -> set[str]:
    codes: set[str] = set()
    items = tuple(intervals)
    for index, left in enumerate(items):
        if left.label == "unknown":
            codes.add("unknown_is_not_an_annotation")
        elif left.label is not None and left.label not in labels:
            codes.add("unsupported_label")
        if left.status != "uncertain" and left.label is None:
            codes.add("label_required")
        covered = any(left.start_ms <= stamp < left.end_ms for stamp in timestamps)
        if not covered:
            codes.add("annotation_outside_recording")
        for right in items[index + 1 :]:
            same = left.start_ms == right.start_ms and left.end_ms == right.end_ms
            overlaps = left.start_ms < right.end_ms and right.start_ms < left.end_ms
            if same and left.label == right.label:
                codes.add("duplicate_annotation")
            elif same:
                codes.add("contradictory_labels")
            elif overlaps:
                codes.add("overlapping_annotations")
    return codes


def _mutate(directory: Path, update):
    destination, session, annotations = _load(directory)
    intervals = _intervals(annotations)
    clip = annotations["clip_label"]
    if clip is not None:
        clip = _label_name(clip, allow_none=False)
    updated, clip = update(tuple(intervals), clip, session)
    _save_annotations(destination, session, updated, clip)
    return destination


def mark_interval(
    directory: Path,
    start_ms: int,
    end_ms: int,
    label: str | None,
    *,
    prompted: bool = False,
    uncertain: bool = False,
) -> MarkedInterval:
    """Append one candidate or one uncertain interval. This does not confirm it."""
    start = _clock(start_ms)
    end = _clock(end_ms)
    if start >= end:
        raise LabelingError("annotation_interval_is_empty")
    status = "uncertain" if uncertain else "candidate"
    checked = _label_name(label, allow_none=status == "uncertain")
    if not isinstance(prompted, bool):
        raise LabelingError("invalid_annotation")

    def update(intervals, clip, session):
        if checked is not None and checked not in set(session["reviewed_labels"]):
            raise LabelingError("unsupported_label")
        item = MarkedInterval(start, end, checked, status, prompted)
        updated = (*intervals, item)
        _assert_savable(session, updated)
        return updated, clip

    _mutate(directory, update)
    return MarkedInterval(start, end, checked, status, prompted)


def correct_interval(
    directory: Path,
    index: int,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    label: str | None = None,
) -> MarkedInterval:
    """Replace one interval and return it to candidate until it is confirmed again."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise LabelingError("label_index_out_of_range")

    def update(intervals, clip, session):
        if index < 0 or index >= len(intervals):
            raise LabelingError("label_index_out_of_range")
        current = intervals[index]
        start = current.start_ms if start_ms is None else _clock(start_ms)
        end = current.end_ms if end_ms is None else _clock(end_ms)
        if start >= end:
            raise LabelingError("annotation_interval_is_empty")
        renamed = current.label if label is None else _label_name(label, allow_none=False)
        if renamed is not None and renamed not in set(session["reviewed_labels"]):
            raise LabelingError("unsupported_label")
        replacement = MarkedInterval(start, end, renamed, "candidate", current.prompted)
        updated = intervals[:index] + (replacement,) + intervals[index + 1 :]
        _assert_savable(session, updated)
        return updated, clip

    holder = []

    def wrapped(intervals, clip, session):
        updated, clip = update(intervals, clip, session)
        holder.append(updated[index])
        return updated, clip

    _mutate(directory, wrapped)
    return holder[0]


def remove_interval(directory: Path, index: int) -> None:
    if isinstance(index, bool) or not isinstance(index, int):
        raise LabelingError("label_index_out_of_range")

    def update(intervals, clip, _session):
        if index < 0 or index >= len(intervals):
            raise LabelingError("label_index_out_of_range")
        return intervals[:index] + intervals[index + 1 :], clip

    _mutate(directory, update)


def confirm_interval(directory: Path, index: int) -> MarkedInterval:
    """Record that the operator reviewed this interval. A prompt stays marked as a prompt."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise LabelingError("label_index_out_of_range")

    def update(intervals, clip, session):
        if index < 0 or index >= len(intervals):
            raise LabelingError("label_index_out_of_range")
        current = intervals[index]
        if current.label is None:
            raise LabelingError("label_required")
        if current.label not in set(session["reviewed_labels"]):
            raise LabelingError("unsupported_label")
        replacement = MarkedInterval(
            current.start_ms, current.end_ms, current.label, "confirmed", current.prompted
        )
        updated = intervals[:index] + (replacement,) + intervals[index + 1 :]
        _assert_savable(session, updated)
        return updated, clip

    holder = []

    def wrapped(intervals, clip, session):
        updated, clip = update(intervals, clip, session)
        holder.append(updated[index])
        return updated, clip

    _mutate(directory, wrapped)
    return holder[0]


def mark_uncertain(directory: Path, index: int) -> MarkedInterval:
    """Keep the range, and do not let it become a training example."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise LabelingError("label_index_out_of_range")

    def update(intervals, clip, _session):
        if index < 0 or index >= len(intervals):
            raise LabelingError("label_index_out_of_range")
        current = intervals[index]
        replacement = MarkedInterval(
            current.start_ms, current.end_ms, current.label, "uncertain", current.prompted
        )
        return intervals[:index] + (replacement,) + intervals[index + 1 :], clip

    holder = []

    def wrapped(intervals, clip, session):
        updated, clip = update(intervals, clip, session)
        holder.append(updated[index])
        return updated, clip

    _mutate(directory, wrapped)
    return holder[0]


def set_clip_label(directory: Path, label: str) -> None:
    """Record a clip-level label. It has no onset or offset and is not a window label."""
    checked = _label_name(label, allow_none=False)

    def update(intervals, _clip, session):
        if checked not in set(session["reviewed_labels"]):
            raise LabelingError("unsupported_label")
        return intervals, checked

    _mutate(directory, update)


def clear_clip_label(directory: Path) -> None:
    _mutate(directory, lambda intervals, _clip, _session: (intervals, None))


def reclassify_split(directory: Path, split: str, *, reviewed: bool) -> None:
    """Change train, calibration, or eval only after an explicit review, and only if the plan allows it."""
    if not isinstance(reviewed, bool) or reviewed is not True:
        raise LabelingError("split_change_needs_review")
    destination, session, _annotations = _load(directory)
    if session["split"] == split:
        return
    proposed = dict(session)
    proposed["split"] = split
    _source, _identity, _extraction, role, bound, assignment = _idents(proposed)
    validate_split_plan((role,), (bound,), (assignment,), ())
    history = list(session["split_history"])
    history.append({"from_split": session["split"], "to_split": split, "reviewed": True})
    session["split"] = split
    session["split_history"] = history
    _atomic_write(
        destination / "session.json",
        json.dumps(session, sort_keys=True, separators=(",", ":")) + "\n",
        allow_replace=True,
    )


def session_text(directory: Path) -> str:
    destination, session, annotations = _load(directory)
    overview = inspect_recording(Path(session["recording_path"]))
    rows = overview._pairs()
    rows.extend(
        [
            ("participant_id", session["participant_id"]),
            ("session_id", session["session_id"]),
            ("recording_id", session["recording_id"]),
            ("split", session["split"]),
            ("participant_role", session["participant_role"]),
            ("capture_claim", session["capture_claim"]),
            ("timestamp_provenance", session["timestamp_provenance"]),
            ("timestamps_are_measured_capture", session["timestamp_provenance"] == "measured_capture"),
            ("authorization_is_proof", False),
        ]
    )
    for index, item in enumerate(_intervals(annotations)):
        label = "" if item.label is None else item.label
        rows.append(
            (
                f"interval_{index}",
                f"{item.start_ms},{item.end_ms},{label},{item.status},prompted={str(item.prompted).lower()}",
            )
        )
    if annotations["clip_label"] is not None:
        rows.append(("clip_label", annotations["clip_label"]))
    text = _lines(rows)
    return text


def _assert_savable(session, intervals: Sequence[MarkedInterval]) -> None:
    _path, _digest, _size, _overview, timestamps, _states = _fingerprint(Path(session["recording_path"]))
    codes = _problems(intervals, set(session["reviewed_labels"]), timestamps)
    if codes:
        raise LabelingError(tuple(sorted(codes))[0])


def prepare_session(directory: Path) -> QualityReport:
    """Pass confirmed intervals to ``ingest_recording`` and write a count report.

    The report is JSON in the session directory. It does not contain feature
    values, and the examples themselves are not written to disk.
    """
    destination, session, annotations = _load(directory)
    recording_path = Path(session["recording_path"])
    local, digest, size, overview, timestamps, states = _fingerprint(recording_path)
    stored = (
        session["byte_length"],
        session["sha256"],
        session["accepted_frames"],
        session["first_timestamp_ms"],
        session["last_timestamp_ms"],
    )
    current = (size, digest, overview.accepted_frames, overview.timeline_start_ms, overview.timeline_end_ms)
    changed = stored != current
    intervals = _intervals(annotations)
    labels = set(session["reviewed_labels"])
    blocks = set()
    if changed:
        blocks.add("recording_changed")
    if annotations["timestamp_provenance"] != session["timestamp_provenance"]:
        blocks.add("timestamp_provenance_mismatch")
    clip = annotations["clip_label"]
    if clip is not None:
        try:
            clip = _label_name(clip, allow_none=False)
        except LabelingError as exc:
            blocks.add(exc.code)
            clip = None
        else:
            if clip not in labels:
                blocks.add("unsupported_label")
    if clip is not None and intervals:
        blocks.add("conflicting_clip_and_interval")
    if not changed:
        blocks.update(_problems(intervals, labels, timestamps))
    confirmed = tuple(item for item in intervals if item.status == "confirmed")
    candidates = sum(item.status == "candidate" for item in intervals)
    uncertain = sum(item.status == "uncertain" for item in intervals)
    prompted_confirmed = sum(item.status == "confirmed" and item.prompted for item in intervals)
    counts: dict[str, int] = {}
    for item in confirmed:
        if item.label is not None:
            counts[item.label] = counts.get(item.label, 0) + 1
    reviews = []
    if candidates:
        reviews.append("unconfirmed_candidate")
    if uncertain:
        reviews.append("uncertain_boundary")
    if any(item.prompted for item in intervals):
        reviews.append("prompted_marker_is_not_verification")
    if overview.rejected_records:
        reviews.append("rejected_records_present")
    non_actionable_in = 0
    empty = 0
    unlabeled = 0
    if not changed:
        for index, stamp in enumerate(timestamps):
            owners = [item for item in confirmed if item.start_ms <= stamp < item.end_ms]
            if not owners:
                unlabeled += 1
            elif states[index] != "tracking":
                non_actionable_in += 1
        for item in confirmed:
            if not any(item.start_ms <= stamp < item.end_ms for stamp in timestamps):
                empty += 1
        if empty:
            reviews.append("intervals_without_frames")
        if non_actionable_in:
            reviews.append("tracking_loss_in_intervals")
    split_errors: tuple[str, ...] = ()
    split_accepted = False
    examples_emitted = 0
    blocked_count = 0
    windows: tuple[tuple[int, int, str, int], ...] = ()
    ingest_non_actionable = 0
    ingest_review = 0
    if not blocks:
        source, identity, extraction, role, bound, assignment = _idents(session)
        gesture = _gesture_annotations(session, confirmed, clip)
        try:
            ingested = ingest_recording(local, source, identity, gesture, session["split"], extraction)
        except (DatasetError, SplitPlanError) as exc:
            code = getattr(exc, "code", None)
            blocks.add(code or "ingest_rejected")
        else:
            if ingested.counts.unlabeled_frames != unlabeled or ingested.counts.empty_intervals != empty:
                raise LabelingError("counts_do_not_match")
            ingest_non_actionable = ingested.counts.non_actionable_windows
            for item in ingested.reviews:
                reviews.append(item.reason)
            try:
                validate_split_plan((role,), (bound,), (assignment,), ingested.examples)
            except SplitPlanError as exc:
                split_errors = exc.codes
                blocked_count = len(ingested.examples)
            else:
                split_accepted = True
                examples_emitted = len(ingested.examples)
                windows = tuple(
                    (
                        item.example.window_start_timestamp_ms,
                        item.example.window_end_timestamp_ms,
                        item.example.label,
                        item.example.frame_count,
                    )
                    for item in ingested.examples
                )
    else:
        try:
            _source, _identity, _extraction, role, bound, assignment = _idents(session)
            validate_split_plan((role,), (bound,), (assignment,), ())
            split_accepted = True
        except SplitPlanError as exc:
            split_errors = exc.codes
            split_accepted = False
    if split_errors:
        split_accepted = False
    report = QualityReport(
        participant_id=session["participant_id"],
        session_id=session["session_id"],
        recording_id=session["recording_id"],
        source_category=session["source_category"],
        capture_claim=session["capture_claim"],
        timestamp_provenance=session["timestamp_provenance"],
        timestamps_are_measured_capture=session["timestamp_provenance"] == "measured_capture",
        authorization_is_proof=False,
        split=session["split"],
        participant_role=session["participant_role"],
        records_read=overview.accepted_frames + overview.rejected_records,
        accepted_frames=overview.accepted_frames,
        rejected_records=overview.rejected_records,
        rejection_reasons=overview.rejection_reasons,
        timeline_start_ms=overview.timeline_start_ms,
        timeline_end_ms=overview.timeline_end_ms,
        observed_span_ms=overview.observed_span_ms,
        smallest_step_ms=overview.smallest_step_ms,
        largest_step_ms=overview.largest_step_ms,
        tracking_frames=overview.tracking_frames,
        partial_frames=overview.partial_frames,
        lost_frames=overview.lost_frames,
        tracking_loss_frames=overview.partial_frames + overview.lost_frames,
        confirmed_intervals=len(confirmed),
        candidate_intervals=candidates,
        uncertain_intervals=uncertain,
        prompted_confirmed_intervals=prompted_confirmed,
        per_label_counts=tuple(sorted(counts.items())),
        unlabeled_frames=unlabeled,
        intervals_without_frames=empty,
        non_actionable_frames_in_intervals=non_actionable_in,
        non_actionable_windows=ingest_non_actionable,
        review_required=len(set(reviews)),
        review_reasons=tuple(sorted(set(reviews))),
        block_reasons=tuple(sorted(blocks)),
        examples_emitted=examples_emitted,
        examples_blocked_by_split=blocked_count,
        example_windows=windows,
        feature_dimension=FEATURE_DIM,
        split_accepted=split_accepted,
        split_errors=split_errors,
    )
    if report.authorization_is_proof is not False:
        raise LabelingError("authorization_is_not_proof")
    _atomic_write(destination / "quality-report.json", report.to_json(), allow_replace=True)
    return report


def _gesture_annotations(session, confirmed: Sequence[MarkedInterval], clip: str | None):
    provenance = session["timestamp_provenance"]
    if clip is not None and not confirmed:
        return (
            GestureAnnotation(
                participant_id=session["participant_id"],
                session_id=session["session_id"],
                recording_id=session["recording_id"],
                label=clip,
                granularity="clip",
                start_ms=None,
                end_ms=None,
                timestamp_provenance=provenance,
            ),
        )
    return tuple(
        GestureAnnotation(
            participant_id=session["participant_id"],
            session_id=session["session_id"],
            recording_id=session["recording_id"],
            label=item.label,
            granularity="interval",
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            timestamp_provenance=provenance,
        )
        for item in confirmed
    )


def check_collection(directories: Sequence[Path]):
    """Check session assignments together. This does not read feature frames."""
    roles = []
    sessions = []
    recordings = []
    seen_roles = {}
    for directory in directories:
        _destination, session, _annotations = _load(directory)
        _source, _identity, _extraction, role, bound, assignment = _idents(session)
        previous = seen_roles.get(role.participant_id)
        if previous is None:
            seen_roles[role.participant_id] = role
            roles.append(role)
        elif previous.role != role.role:
            roles.append(role)
        sessions.append(bound)
        recordings.append(assignment)
    return validate_split_plan(roles, sessions, recordings, ())


def discard_session(directory: Path, *, include_recording: bool = False) -> DiscardResult:
    """Delete files this tool wrote. A recording is deleted only when asked, and only from inside the session directory."""
    if not isinstance(include_recording, bool):
        raise LabelingError("operator_input_invalid")
    destination, session, _annotations = _load(directory)
    recording = Path(session["recording_path"])
    deleted = 0
    for name in sorted(_GENERATED):
        candidate = destination / name
        if not candidate.exists():
            continue
        if candidate.is_symlink():
            raise LabelingError("symlink_not_allowed")
        candidate.unlink()
        deleted += 1
    deleted_recording = False
    try:
        recording.resolve().relative_to(destination.resolve())
        inside = True
    except ValueError:
        inside = False
    outside = not inside
    if include_recording:
        if recording.is_symlink():
            raise LabelingError("symlink_not_allowed")
        if inside and recording.is_file():
            recording.unlink()
            deleted_recording = True
    unexpected = 0
    if destination.exists():
        unexpected = sum(1 for _item in destination.iterdir())
        removed = False
        if unexpected == 0:
            destination.rmdir()
            removed = True
    else:
        removed = True
    return DiscardResult(deleted, deleted_recording, outside and not deleted_recording, unexpected, removed)
