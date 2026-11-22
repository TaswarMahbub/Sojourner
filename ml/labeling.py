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
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

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
    _DECISION,
    _TOKEN,
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
_CATEGORIES = frozenset({"consented_local", "authorized_public", "constructed_fixture"})
_PROVENANCE = frozenset({"measured_capture", "constructed", "reconstructed_offline"})
_AUTH_STATUS = frozenset({"human_authorized", "not_authorized", "constructed"})
_SPLITS = frozenset({"train", "calibration", "eval"})
_ROLES = frozenset({"population_train", "held_out"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_DIRS = frozenset({"captures", "datasets"})
# Files this tool owns in a session directory. Temporary files are created
# with unique names by ``_atomic_write`` and are never listed here.
_GENERATED = frozenset({MARKER_NAME, "session.json", "annotations.json", "quality-report.json"})
_MARKER_BYTES = (SESSION_FORMAT + "\n").encode("ascii")
# Prefixes of private directories a replacement creates inside the session
# directory, so every rename stays on one file system.
_STAGE_PREFIX = ".label-stage-"
_BACKUP_PREFIX = ".label-backup-"
# Private directory ``discard --include-recording`` moves the recording into
# before verifying and deleting it. It lives in the session directory, so the
# move is a rename on one file system. The claimed file has a fixed name.
_CLAIM_PREFIX = ".label-claim-"
_CLAIMED_NAME = "recording"
_METADATA_LIMIT = 4 * 1024 * 1024
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
# Written by every new session. A session written before this field existed
# loads without it, and then its recording can never be deleted by discard.
_IDENTITY_KEY = "recording_identity"
_SESSION_OPTIONAL = frozenset({_IDENTITY_KEY})
_IDENTITY_KEYS = frozenset({"name", "device", "inode"})
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


def _exact(data, allowed: frozenset[str], optional: frozenset[str] = frozenset()) -> None:
    if not isinstance(data, dict):
        raise LabelingError("metadata_not_an_object")
    if allowed - set(data):
        raise LabelingError("missing_metadata_field")
    if set(data) - allowed - optional:
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


def _existing_kind(path: Path) -> str | None:
    """``None`` when absent, else ``"file"``, ``"symlink"``, or ``"other"``. Never follows a link."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise LabelingError("output_write_failed") from None
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _check_destination(path: Path, allow_replace: bool) -> None:
    kind = _existing_kind(path)
    if kind == "symlink":
        raise LabelingError("symlink_not_allowed")
    if kind == "other":
        raise LabelingError("unsafe_output_type")
    if kind == "file" and not allow_replace:
        raise LabelingError("output_exists")


def _unlink_owned(path: Path) -> bool:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _sync_directory(path: Path) -> None:
    # Best effort. Some platforms cannot open a directory for fsync.
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _atomic_write(path: Path, text: str, *, allow_replace: bool) -> None:
    """Replace one file through a uniquely named temporary file in the same directory.

    ``mkstemp`` creates the temporary file exclusively with mode 0600, so an
    existing file or symlink at a predictable name is never opened. The data is
    written, flushed, and closed before ``os.replace``. A failure removes only
    that temporary file and leaves the previous destination in place.
    """
    data = text.encode("utf-8")
    _check_destination(path, allow_replace)
    try:
        handle, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    except OSError:
        raise LabelingError("output_write_failed") from None
    temporary = Path(name)
    committed = False
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _check_destination(path, allow_replace)
        os.replace(temporary, path)
        committed = True
    except LabelingError:
        raise
    except Exception:
        raise LabelingError("output_write_failed") from None
    finally:
        if not committed:
            _unlink_owned(temporary)
    _sync_directory(path.parent)


# ``O_NONBLOCK`` keeps ``open`` from waiting on a FIFO with no writer. It has
# no effect on reads of a regular file. ``O_NOFOLLOW`` refuses a final symlink.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _read_regular(path: Path, *, missing: str, limit: int = _METADATA_LIMIT) -> bytes:
    """Read a regular file without following a final symlink or blocking on a special file.

    ``lstat`` refuses an obvious FIFO or device early. The descriptor is still
    checked with ``fstat`` before any read, in case the path was swapped.
    Errors are codes only.
    """
    refused = missing if missing == "not_a_label_session" else "metadata_unreadable"
    kind = _existing_kind(path)
    if kind is None:
        raise LabelingError(missing)
    if kind != "file":
        raise LabelingError(refused)
    try:
        handle = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        raise LabelingError(missing) from None
    except OSError:
        raise LabelingError(missing if missing == "not_a_label_session" else "metadata_unreadable") from None
    try:
        with os.fdopen(handle, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LabelingError(missing if missing == "not_a_label_session" else "metadata_unreadable")
            data = stream.read(limit + 1)
    except LabelingError:
        raise
    except OSError:
        raise LabelingError("metadata_unreadable") from None
    if len(data) > limit:
        raise LabelingError("metadata_too_large")
    return data


def _strict_json(raw: bytes):
    """Strict UTF-8 JSON. Duplicate keys at any depth, NaN, and Infinity are refused."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LabelingError("metadata_invalid_utf8") from None

    def pairs(items):
        document = {}
        for key, value in items:
            if key in document:
                raise LabelingError("duplicate_metadata_key")
            document[key] = value
        return document

    def constant(_name):
        raise LabelingError("metadata_non_finite_number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except LabelingError:
        raise
    except RecursionError:
        raise LabelingError("metadata_too_deep") from None
    except ValueError:
        raise LabelingError("metadata_unreadable") from None


def _read_json(path: Path, allowed: frozenset[str], optional: frozenset[str] = frozenset()):
    data = _strict_json(_read_regular(path, missing="metadata_unreadable"))
    _exact(data, allowed, optional)
    return data


@dataclass(frozen=True)
class _Snapshot:
    path: Path
    source: Path
    sha256: str
    byte_length: int
    # Device and inode of the descriptor the bytes were copied from.
    device: int
    inode: int


def _hash_file(path: Path, expect: tuple[int, int] | None = None) -> tuple[str, int]:
    """SHA-256 and length of a regular file. With ``expect``, the opened
    descriptor must have that device and inode, or ``LabelingError`` is raised."""
    digest = hashlib.sha256()
    size = 0
    handle = os.open(path, _READ_FLAGS)
    with os.fdopen(handle, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("not a regular file")
        if expect is not None and (opened.st_dev, opened.st_ino) != expect:
            raise LabelingError("recording_identity_mismatch")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@contextmanager
def _snapshot(path: Path) -> Iterator[_Snapshot]:
    """Copy the recording once into a private temporary file and hash that copy.

    Everything that reads frames afterwards reads the copy, so the counts,
    the fingerprint, and the ingested values all come from the same bytes.
    The copy is a new file (not a hard link) in a 0700 directory under the
    system temporary directory, outside the repository. It is removed on
    every exit path.
    """
    local = _local_file(path)
    try:
        workdir = Path(tempfile.mkdtemp(prefix="sojourner-label-snapshot-"))
    except OSError:
        # Nothing was created. The recording and the session are untouched.
        raise LabelingError("snapshot_unavailable") from None
    target = workdir / "recording.jsonl"
    try:
        digest = hashlib.sha256()
        size = 0
        try:
            source_handle = os.open(local, _READ_FLAGS)
            with os.fdopen(source_handle, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise LabelingError("recording_unreadable")
                out_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(target, out_flags, 0o600), "wb") as out:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                        out.write(chunk)
            os.chmod(target, 0o400)
        except LabelingError:
            raise
        except OSError:
            raise LabelingError("recording_unreadable") from None
        snap = _Snapshot(target, local, digest.hexdigest(), size, info.st_dev, info.st_ino)
        yield snap
    finally:
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        _unlink_owned(target)
        try:
            os.rmdir(workdir)
        except OSError:
            pass


def _snapshot_unchanged(snap: _Snapshot) -> bool:
    try:
        return _hash_file(snap.path) == (snap.sha256, snap.byte_length)
    except OSError:
        return False


def _live_matches(snap: _Snapshot) -> bool:
    """Whether the recording path still holds exactly the snapshot bytes."""
    try:
        if Path(snap.source).is_symlink():
            return False
        return _hash_file(snap.source) == (snap.sha256, snap.byte_length)
    except OSError:
        return False


def _summarize(snap: _Snapshot):
    _reject_calibration(snap.path)
    _read_count, accepted, rejections = _read_frames(snap.path)
    timestamps = tuple(frame["timestamp_ms"] for frame in accepted)
    states = tuple(frame["tracking_state"] for frame in accepted)
    return _overview(timestamps, states, rejections), timestamps, states


def _fingerprint(path: Path):
    with _snapshot(path) as snap:
        overview, timestamps, states = _summarize(snap)
        return snap.source, snap.sha256, snap.byte_length, overview, timestamps, states


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


def _choice(value, allowed: frozenset[str], code: str) -> str:
    # Type first, so an unhashable value cannot raise TypeError on membership.
    if not isinstance(value, str) or value not in allowed:
        raise LabelingError(code)
    return value


def _text_token(value, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise LabelingError(code)
    return value


def _count(value, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LabelingError(code)
    return value


def _claim_pair(claim, category, provenance, authorization) -> None:
    """The capture claim, category, provenance, and authorization must agree.

    ``constructed_fixture`` goes only with category, provenance, and
    authorization ``constructed``. ``operator_claims_camera`` goes only with
    ``consented_local``, ``human_authorized``, and a measured or reconstructed
    timeline. This is a consistency rule on recorded claims, not proof.
    """
    _choice(claim, _CLAIMS, "invalid_capture_claim")
    _choice(category, _CATEGORIES, "invalid_source_category")
    _choice(provenance, _PROVENANCE, "invalid_timestamp_provenance")
    _choice(authorization, _AUTH_STATUS, "invalid_authorization_status")
    if claim == "constructed_fixture":
        if category != "constructed_fixture" or provenance != "constructed" or authorization != "constructed":
            raise LabelingError("constructed_data_is_not_a_measurement")
        return
    if category != "consented_local" or authorization != "human_authorized":
        raise LabelingError("capture_claim_mismatch")
    if provenance not in {"measured_capture", "reconstructed_offline"}:
        raise LabelingError("capture_claim_mismatch")


def _check_labels(value) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LabelingError("invalid_reviewed_labels")
    seen: set[str] = set()
    for name in value:
        checked = _label_name(name, allow_none=False)
        if checked in seen:
            raise LabelingError("duplicate_label")
        seen.add(checked)
    return tuple(value)


def _check_session(data) -> dict:
    """Validate every field of ``session.json``. Raise ``LabelingError`` with a code only."""
    _exact(data, _SESSION_KEYS, _SESSION_OPTIONAL)
    if data["session_format"] != SESSION_FORMAT:
        raise LabelingError("metadata_unreadable")
    _text_token(data["dataset_id"], _TOKEN, "invalid_dataset_id")
    _text_token(data["participant_id"], _TOKEN, "invalid_participant_id")
    _text_token(data["session_id"], _TOKEN, "invalid_session_id")
    _text_token(data["recording_id"], _TOKEN, "invalid_recording_id")
    path = data["recording_path"]
    if not isinstance(path, str) or not path or "\x00" in path or not os.path.isabs(path):
        raise LabelingError("invalid_recording_path")
    # Stored for the record. Ingestion always uses the Schema 4 feature format.
    _text_token(data["source_format"], _DECISION, "invalid_source_format")
    _text_token(data["source_version"], _DECISION, "invalid_source_version")
    _text_token(data["extraction_version"], _DECISION, "invalid_extraction_version")
    _choice(data["split"], _SPLITS, "invalid_split")
    _choice(data["participant_role"], _ROLES, "invalid_participant_role")
    _text_token(data["authorization_decision_id"], _DECISION, "invalid_decision_id")
    _claim_pair(
        data["capture_claim"],
        data["source_category"],
        data["timestamp_provenance"],
        data["authorization_status"],
    )
    _check_labels(data["reviewed_labels"])
    for key in ("window_length", "window_stride"):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LabelingError("invalid_extraction_parameters")
    _conditions(data["conditions"])
    frames = _count(data["accepted_frames"], "invalid_recording_summary")
    first, last = data["first_timestamp_ms"], data["last_timestamp_ms"]
    if frames == 0:
        if first is not None or last is not None:
            raise LabelingError("invalid_recording_summary")
    else:
        if not is_timestamp(first) or not is_timestamp(last):
            raise LabelingError("invalid_recording_summary")
        if (frames == 1 and first != last) or (frames > 1 and first >= last):
            raise LabelingError("invalid_recording_summary")
    _count(data["byte_length"], "invalid_recording_summary")
    if not isinstance(data["sha256"], str) or _SHA256.fullmatch(data["sha256"]) is None:
        raise LabelingError("invalid_recording_summary")
    if _IDENTITY_KEY in data:
        identity = data[_IDENTITY_KEY]
        _exact(identity, _IDENTITY_KEYS)
        name = identity["name"]
        if (
            not isinstance(name, str)
            or name in {"", ".", ".."}
            or "/" in name
            or os.sep in name
            or "\x00" in name
        ):
            raise LabelingError("invalid_recording_identity")
        _count(identity["device"], "invalid_recording_identity")
        _count(identity["inode"], "invalid_recording_identity")
    history = data["split_history"]
    if not isinstance(history, list):
        raise LabelingError("invalid_split_history")
    for entry in history:
        _exact(entry, frozenset({"from_split", "to_split", "reviewed"}))
        before = _choice(entry["from_split"], _SPLITS, "invalid_split_history")
        after = _choice(entry["to_split"], _SPLITS, "invalid_split_history")
        if before == after or entry["reviewed"] is not True:
            raise LabelingError("invalid_split_history")
    return data


def _check_annotations(data, session) -> dict:
    """Validate ``annotations.json`` against its session. A bad row is refused, not skipped."""
    _exact(data, _ANNOTATION_KEYS)
    if data["annotation_format"] != ANNOTATION_FORMAT:
        raise LabelingError("metadata_unreadable")
    for key in ("participant_id", "session_id", "recording_id"):
        _text_token(data[key], _TOKEN, f"invalid_{key}")
        if data[key] != session[key]:
            raise LabelingError("annotation_session_mismatch")
    # A provenance that differs from the session is a prepare block, kept below.
    _choice(data["timestamp_provenance"], _PROVENANCE, "invalid_timestamp_provenance")
    _intervals(data)
    if data["clip_label"] is not None:
        _label_name(data["clip_label"], allow_none=False)
    return data


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


def _check_create_inputs(**values) -> None:
    # Wrong types become stable codes before any ML-2A constructor sees them.
    for name, value in values.items():
        if not isinstance(value, str):
            raise LabelingError(f"invalid_{name}")


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
    """Create a session directory of metadata. The recording file is not copied or modified.

    With ``replace=True`` an existing session is replaced as one unit. The new
    files are written and checked in a private staging directory first. The
    previous files are moved aside, not deleted, until the new ones are in
    place. If any step fails in this process, the previous files are moved
    back. See ``ml/LABELING.md`` for what this does not cover.
    """
    if not isinstance(replace, bool):
        raise LabelingError("operator_input_invalid")
    _check_create_inputs(
        dataset_id=dataset_id,
        participant_id=participant_id,
        session_id=session_id,
        recording_id=recording_id,
        split=split,
        participant_role=participant_role,
        source_category=source_category,
        timestamp_provenance=timestamp_provenance,
        capture_claim=capture_claim,
        decision_id=decision_id,
        extraction_version=extraction_version,
    )
    if isinstance(reviewed_labels, (str, bytes)) or isinstance(extra_labels, (str, bytes)):
        raise LabelingError("invalid_reviewed_labels")
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
    local = _local_file(recording)
    destination = _output_dir(output)
    _reject_collision(local, destination)
    with _snapshot(local) as snap:
        overview, _timestamps, _states = _summarize(snap)
        local, digest, size = snap.source, snap.sha256, snap.byte_length
        # Taken from the same descriptor the fingerprint was read from.
        bound = {"name": local.name, "device": snap.device, "inode": snap.inode}
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
        _IDENTITY_KEY: bound,
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
    documents = {
        "session.json": json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        "annotations.json": json.dumps(annotations, sort_keys=True, separators=(",", ":")) + "\n",
        MARKER_NAME: SESSION_FORMAT + "\n",
    }
    created, existing = _preflight_directory(destination, local, replace)
    _commit_generation(destination, documents, existing, created)
    return destination


def _reserved_name(name: str) -> bool:
    folded = name.casefold()
    if folded in {item.casefold() for item in _GENERATED}:
        return True
    if folded.startswith((_STAGE_PREFIX, _BACKUP_PREFIX, _CLAIM_PREFIX)):
        return True
    return folded.startswith(".") and folded.endswith(".tmp")


def _same_directory(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _reject_collision(source: Path, destination: Path) -> None:
    """Refuse a recording that is, or could become, one of this tool's own files.

    Names are compared case-insensitively, because macOS and Windows file
    systems often are. Existing generated files are also compared by device
    and inode, which catches a hard link or another alias of the recording.
    """
    if _same_directory(source.parent, destination) and _reserved_name(source.name):
        raise LabelingError("recording_collides_with_session_file")
    if not destination.is_dir():
        return
    try:
        identity = os.stat(source)
    except OSError:
        raise LabelingError("recording_unreadable") from None
    for name in _GENERATED:
        try:
            other = os.lstat(destination / name)
        except FileNotFoundError:
            continue
        except OSError:
            raise LabelingError("output_write_failed") from None
        if (other.st_dev, other.st_ino) == (identity.st_dev, identity.st_ino):
            raise LabelingError("recording_collides_with_session_file")


def _preflight_directory(destination: Path, recording: Path, replace: bool) -> tuple[bool, tuple[str, ...]]:
    """Check the output directory without deleting anything. Return (created, existing generated names)."""
    if destination.is_symlink():
        raise LabelingError("symlink_not_allowed")
    created = False
    if destination.exists():
        if not destination.is_dir():
            raise LabelingError("output_not_a_directory")
    else:
        try:
            destination.mkdir(parents=True)
        except OSError:
            raise LabelingError("output_write_failed") from None
        created = True
    try:
        present = {item.name for item in destination.iterdir()}
    except OSError:
        raise LabelingError("output_write_failed") from None
    allowed = set(_GENERATED)
    if _same_directory(recording.parent, destination):
        allowed.add(recording.name)
    if present - allowed:
        raise LabelingError("directory_not_empty")
    existing = tuple(sorted(present & _GENERATED))
    if existing and not replace:
        raise LabelingError("output_exists")
    for name in existing:
        kind = _existing_kind(destination / name)
        if kind == "symlink":
            raise LabelingError("symlink_not_allowed")
        if kind != "file":
            raise LabelingError("unsafe_output_type")
    return created, existing


# Commit order. The marker goes in last, so an interrupted commit never leaves
# a directory that loads as a session with only some of its new files.
_COMMIT_ORDER = ("session.json", "annotations.json", MARKER_NAME)
# Backup order. The marker leaves first, for the same reason.
_BACKUP_ORDER = (MARKER_NAME, "quality-report.json", "annotations.json", "session.json")


def _remove_owned_dir(path: Path | None) -> bool:
    """Remove a staging or backup directory this call created. Only regular files are expected in it."""
    if path is None:
        return True
    try:
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                return False
            os.unlink(entry.path)
        os.rmdir(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _verify_staged(stage: Path, documents: dict[str, str]) -> None:
    for name, text in documents.items():
        if _read_regular(stage / name, missing="output_write_failed") != text.encode("utf-8"):
            raise LabelingError("output_write_failed")
    if _read_regular(stage / MARKER_NAME, missing="output_write_failed") != _MARKER_BYTES:
        raise LabelingError("output_write_failed")
    session = _check_session(_read_json(stage / "session.json", _SESSION_KEYS, _SESSION_OPTIONAL))
    if _IDENTITY_KEY not in session:
        raise LabelingError("output_write_failed")
    _check_annotations(_read_json(stage / "annotations.json", _ANNOTATION_KEYS), session)


def _commit_generation(
    destination: Path, documents: dict[str, str], existing: Sequence[str], created: bool
) -> None:
    """Stage, verify, then swap in a complete new set of session files.

    On any failure the files placed by this call are moved back out and the
    previous files are moved back in, then the staging and backup directories
    are removed.

    If any rollback rename fails, ``replacement_rollback_incomplete`` is
    raised and the backup directory keeps every previous file that could not
    be moved back. The previous marker is moved back only when no file of the
    new generation is left in the session directory, so old annotations are
    never marked loadable beside new metadata. Independently, ``_load``
    refuses a directory whose backup directory still holds files.
    """
    try:
        stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=destination))
    except OSError:
        raise LabelingError("output_write_failed") from None
    backup: Path | None = None
    moved_out: list[str] = []
    placed: list[str] = []
    try:
        for name in _COMMIT_ORDER:
            _atomic_write(stage / name, documents[name], allow_replace=False)
        _verify_staged(stage, documents)
        if existing:
            backup = Path(tempfile.mkdtemp(prefix=_BACKUP_PREFIX, dir=destination))
            for name in _BACKUP_ORDER:
                if name in existing:
                    os.replace(destination / name, backup / name)
                    moved_out.append(name)
        for name in _COMMIT_ORDER:
            os.replace(stage / name, destination / name)
            placed.append(name)
    except BaseException as failure:
        # 1. Move the new files back out of the session directory.
        stuck = set()
        for name in reversed(placed):
            try:
                os.replace(destination / name, stage / name)
            except OSError:
                stuck.add(name)
        # 2. Move the previous files back, marker excluded. A successful
        #    restore over a stuck new file replaces it with the old one.
        unrestored = set()
        for name in reversed(moved_out):
            if name == MARKER_NAME:
                continue
            try:
                os.replace(backup / name, destination / name)
            except OSError:
                unrestored.add(name)
            else:
                stuck.discard(name)
        # 3. The marker last, and only if no new file is left behind.
        if MARKER_NAME in moved_out:
            if stuck:
                unrestored.add(MARKER_NAME)
            else:
                try:
                    os.replace(backup / MARKER_NAME, destination / MARKER_NAME)
                except OSError:
                    unrestored.add(MARKER_NAME)
        restored = not stuck and not unrestored
        clean = _remove_owned_dir(stage)
        if restored:
            clean = _remove_owned_dir(backup) and clean
        if created and clean and restored:
            try:
                destination.rmdir()
            except OSError:
                pass
        if not restored:
            raise LabelingError("replacement_rollback_incomplete") from None
        if not clean:
            raise LabelingError("replacement_cleanup_incomplete") from None
        if isinstance(failure, LabelingError) or not isinstance(failure, Exception):
            raise
        raise LabelingError("output_write_failed") from None
    _sync_directory(destination)
    if not (_remove_owned_dir(stage) and _remove_owned_dir(backup)):
        # The new session is complete and in place. Only an old copy is left over.
        raise LabelingError("replacement_cleanup_incomplete")


def _recovery_pending(destination: Path) -> str | None:
    """The code to refuse with when an earlier operation left files to recover, else ``None``.

    A ``.label-backup-*`` directory that holds anything means a replacement
    did not put the previous generation back (``replacement_recovery_pending``).
    A ``.label-claim-*`` directory that holds anything means a discard moved a
    recording aside and did not finish (``discard_recovery_pending``). An empty
    one of either is only a cleanup leftover. One that cannot be read, or is
    not a directory, blocks too. The directory is not used until a person
    resolves it.
    """
    try:
        entries = list(os.scandir(destination))
    except (FileNotFoundError, NotADirectoryError):
        return None  # The marker check reports this as not_a_label_session.
    except OSError:
        return "replacement_recovery_pending"
    found = None
    for entry in entries:
        if entry.name.startswith(_BACKUP_PREFIX):
            code = "replacement_recovery_pending"
        elif entry.name.startswith(_CLAIM_PREFIX):
            code = "discard_recovery_pending"
        else:
            continue
        if not entry.is_dir(follow_symlinks=False):
            pending = True
        else:
            try:
                with os.scandir(entry.path) as inner:
                    pending = any(True for _item in inner)
            except OSError:
                pending = True
        if pending:
            if code == "replacement_recovery_pending":
                return code
            found = code
    return found


def _load(directory: Path):
    """Load and fully validate a session. Any problem raises ``LabelingError``."""
    destination = _output_dir(directory)
    pending = _recovery_pending(destination)
    if pending:
        raise LabelingError(pending)
    if _read_regular(destination / MARKER_NAME, missing="not_a_label_session") != _MARKER_BYTES:
        raise LabelingError("not_a_label_session")
    session = _check_session(_read_json(destination / "session.json", _SESSION_KEYS, _SESSION_OPTIONAL))
    annotations = _check_annotations(_read_json(destination / "annotations.json", _ANNOTATION_KEYS), session)
    return destination, session, annotations


def _intervals(data) -> tuple[MarkedInterval, ...]:
    rows = data["intervals"]
    if not isinstance(rows, list):
        raise LabelingError("invalid_annotation")
    parsed = []
    for row in rows:
        _exact(row, _INTERVAL_KEYS)
        status = _choice(row["status"], _STATUSES, "invalid_annotation_status")
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
    with _snapshot(Path(session["recording_path"])) as snap:
        report = _prepare_snapshot(session, annotations, snap)
    _atomic_write(destination / "quality-report.json", report.to_json(), allow_replace=True)
    return report


def _prepare_snapshot(session, annotations, snap: _Snapshot) -> QualityReport:
    """Build the report from one private copy of the recording.

    The fingerprint, the counts, and ``ingest_recording`` all read ``snap.path``.
    After ingestion the copy is hashed again, and so is the original path. If
    either differs, the examples are withheld and the report is blocked.
    """
    overview, timestamps, states = _summarize(snap)
    digest, size = snap.sha256, snap.byte_length
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
        if timestamps and any(
            item.start_ms < timestamps[0] or item.end_ms > timestamps[-1] + 1 for item in confirmed
        ):
            # The interval reaches past the first or last accepted frame. Its
            # onset or offset there was not observed. Windows still use real frames only.
            reviews.append("interval_boundary_not_observed")
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
            ingested = ingest_recording(snap.path, source, identity, gesture, session["split"], extraction)
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
            integrity = set()
            if not _snapshot_unchanged(snap):
                integrity.add("snapshot_changed")
            if not _live_matches(snap):
                integrity.add("recording_changed_during_prepare")
            if integrity:
                blocks.update(integrity)
                examples_emitted = 0
                windows = ()
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


def _source_identity(session) -> tuple[str, tuple[int, int] | None, tuple[str, int]]:
    path = session["recording_path"]
    try:
        info = os.stat(path)
        inode = (info.st_dev, info.st_ino)
    except OSError:
        inode = None
    return os.path.realpath(path), inode, (session["sha256"], session["byte_length"])


def check_collection(directories: Sequence[Path]):
    """Check session assignments together, including reuse of one source recording.

    Declared ids are checked by ``validate_split_plan``. Sources are compared
    by canonical path, by device and inode when the file exists, and by the
    SHA-256 and length stored when each session was created. Any reuse is
    refused, and reuse across different splits adds a second code. This does
    not read feature frames, and it does not re-hash the recordings.
    """
    if isinstance(directories, (str, bytes, Path)) or not isinstance(directories, Sequence):
        raise LabelingError("operator_input_invalid")
    roles = []
    sessions = []
    recordings = []
    seen_roles = {}
    sources = []
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
        sources.append((session["split"], _source_identity(session)))
    reuse: set[str] = set()
    for index, (split, (path, inode, content)) in enumerate(sources):
        for other_split, (other_path, other_inode, other_content) in sources[index + 1 :]:
            same_file = path == other_path or (inode is not None and inode == other_inode)
            if not (same_file or content == other_content):
                continue
            reuse.add("recording_source_reused" if same_file else "recording_content_duplicated")
            if split != other_split:
                reuse.add("recording_source_reused_across_splits")
                if {split, other_split} == {"calibration", "eval"}:
                    reuse.add("calibration_eval_source_leakage")
    try:
        plan = validate_split_plan(roles, sessions, recordings, ())
    except SplitPlanError as exc:
        raise SplitPlanError(tuple(sorted(set(exc.codes) | reuse))) from None
    if reuse:
        raise SplitPlanError(tuple(sorted(reuse)))
    return plan


def _discard_state(destination: Path):
    """Load for discard. Accept only the partial states an interrupted discard leaves.

    Discard deletes ``quality-report.json``, then ``annotations.json``, then
    ``session.json``, then the marker. A retry may therefore find the marker
    with ``session.json`` and no ``annotations.json``, or the marker alone.
    Every file that is present is still validated in full.
    """
    pending = _recovery_pending(destination)
    if pending:
        raise LabelingError(pending)
    if _read_regular(destination / MARKER_NAME, missing="not_a_label_session") != _MARKER_BYTES:
        raise LabelingError("not_a_label_session")
    has_session = _existing_kind(destination / "session.json") is not None
    has_annotations = _existing_kind(destination / "annotations.json") is not None
    if not has_session:
        if has_annotations:
            raise LabelingError("metadata_unreadable")
        return None
    session = _check_session(_read_json(destination / "session.json", _SESSION_KEYS, _SESSION_OPTIONAL))
    if has_annotations:
        _check_annotations(_read_json(destination / "annotations.json", _ANNOTATION_KEYS), session)
    return session


def _verified_recording(destination: Path, session) -> Path | None:
    """Preflight: return the recording path only if it is the file bound at ``init``.

    This only decides whether a deletion is attempted. Another process can
    replace the path right after it returns, so ``_delete_recording`` moves
    the file away and checks the moved file again before deleting it.

    Policy, all checked before anything is moved or deleted:

    - The path is a direct child of the session directory. Otherwise it is
      left in place and ``None`` is returned.
    - The session has a ``recording_identity``. A session written before that
      field existed cannot have its recording deleted.
    - The path's file name equals the bound name.
    - It is a regular file, not a symlink, not one of this tool's file names,
      with one hard link.
    - Its device and inode equal the bound ones, so a copy or a replacement
      at the same name is refused even when its bytes are identical.
    - Its SHA-256 and length equal the stored fingerprint.

    A missing file at the bound name returns ``None``. Anything else raises.
    """
    recording = Path(session["recording_path"])
    if not _same_directory(recording.parent, destination):
        return None
    bound = session.get(_IDENTITY_KEY)
    if bound is None:
        raise LabelingError("recording_identity_unavailable")
    if recording.name != bound["name"]:
        raise LabelingError("recording_identity_mismatch")
    try:
        info = os.lstat(recording)
    except FileNotFoundError:
        return None
    except OSError:
        raise LabelingError("recording_unreadable") from None
    if stat.S_ISLNK(info.st_mode):
        raise LabelingError("symlink_not_allowed")
    if not stat.S_ISREG(info.st_mode):
        raise LabelingError("unsafe_output_type")
    if _reserved_name(recording.name):
        raise LabelingError("recording_collides_with_session_file")
    if info.st_nlink != 1:
        raise LabelingError("recording_has_other_links")
    if (info.st_dev, info.st_ino) != (bound["device"], bound["inode"]):
        raise LabelingError("recording_identity_mismatch")
    try:
        # The descriptor is checked against the bound inode too, so a swap
        # between ``lstat`` and ``open`` is refused.
        fingerprint = _hash_file(recording, expect=(bound["device"], bound["inode"]))
    except LabelingError:
        raise
    except OSError:
        raise LabelingError("recording_unreadable") from None
    if fingerprint != (session["sha256"], session["byte_length"]):
        raise LabelingError("recording_identity_mismatch")
    return recording


def _rmdir_quietly(path: Path) -> None:
    """Remove a directory only if it is empty. Never deletes what is inside."""
    try:
        os.rmdir(path)
    except OSError:
        pass


def _check_claimed(claimed: Path, session) -> None:
    """Check the file moved into the claim directory against the ``init`` identity."""
    bound = session[_IDENTITY_KEY]
    expect = (bound["device"], bound["inode"])
    try:
        info = os.lstat(claimed)
    except OSError:
        raise LabelingError("recording_unreadable") from None
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != expect:
        raise LabelingError("recording_identity_mismatch")
    if info.st_nlink != 1:
        raise LabelingError("recording_has_other_links")
    try:
        fingerprint = _hash_file(claimed, expect=expect)
    except LabelingError:
        raise
    except OSError:
        raise LabelingError("recording_unreadable") from None
    if fingerprint != (session["sha256"], session["byte_length"]):
        raise LabelingError("recording_identity_mismatch")


def _release_claim(claim: Path, claimed: Path, recording: Path, code: str) -> None:
    """Put a claimed file back at its old name without replacing anything there, then raise.

    The file is put back with a hard link, which fails if the name is taken,
    and the claimed name is then removed. If that cannot be done the file stays
    in the claim directory and ``discard_recovery_pending`` is raised; the
    claim directory then blocks the session until a person resolves it.
    """
    try:
        info = os.lstat(claimed)
    except FileNotFoundError:
        _rmdir_quietly(claim)
        raise LabelingError(code) from None
    except OSError:
        raise LabelingError("discard_recovery_pending") from None
    if not stat.S_ISREG(info.st_mode):
        raise LabelingError("discard_recovery_pending")
    try:
        os.link(claimed, recording)
    except OSError:
        raise LabelingError("discard_recovery_pending") from None
    try:
        os.unlink(claimed)
    except OSError:
        raise LabelingError("discard_recovery_pending") from None
    _rmdir_quietly(claim)  # An empty claim directory does not block anything.
    raise LabelingError(code)


def _delete_recording(destination: Path, recording: Path, session) -> None:
    """Delete the recording only as the exact file that was verified.

    1. Create a new private directory ``.label-claim-*`` in the session directory.
    2. Rename the recording into it. The rename is atomic, so from then on the
       public name no longer reaches the claimed file, and a file another
       process puts at the public name afterwards is never touched.
    3. Check the claimed file: regular, one link, bound device and inode,
       SHA-256 and length.
    4. Unlink only the claimed name, then remove the empty claim directory.

    If the rename fails, nothing has moved (``recording_claim_failed``). If a
    check or the unlink fails, the claimed file is put back without replacing
    anything (``_release_claim``) and the session is left as it was.
    """
    try:
        claim = Path(tempfile.mkdtemp(prefix=_CLAIM_PREFIX, dir=destination))
    except OSError:
        raise LabelingError("recording_claim_failed") from None
    claimed = claim / _CLAIMED_NAME
    try:
        os.rename(recording, claimed)
    except OSError:
        _rmdir_quietly(claim)
        raise LabelingError("recording_claim_failed") from None
    try:
        _check_claimed(claimed, session)
    except LabelingError as exc:
        _release_claim(claim, claimed, recording, exc.code)
    try:
        os.unlink(claimed)
    except OSError:
        _release_claim(claim, claimed, recording, "recording_delete_failed")
    _rmdir_quietly(claim)  # If it stays, it is empty and counted as an unexpected entry.


def discard_session(directory: Path, *, include_recording: bool = False) -> DiscardResult:
    """Delete files this tool wrote, and the recording only when asked and verified.

    Every deletion target is checked before anything is deleted. The recording
    is deleted first, so a failure there leaves the session intact for a retry.
    A failure while deleting generated files raises ``discard_incomplete`` and
    a retry finishes the job.
    """
    if not isinstance(include_recording, bool):
        raise LabelingError("operator_input_invalid")
    destination = _output_dir(directory)
    session = _discard_state(destination)
    present = []
    for name in _BACKUP_ORDER:
        kind = _existing_kind(destination / name)
        if kind is None:
            continue
        if kind == "symlink":
            raise LabelingError("symlink_not_allowed")
        if kind != "file":
            raise LabelingError("unsafe_output_type")
        present.append(name)
    outside = False
    target = None
    if session is not None:
        outside = not _same_directory(Path(session["recording_path"]).parent, destination)
        if include_recording:
            target = _verified_recording(destination, session)
    elif include_recording:
        raise LabelingError("recording_identity_unavailable")
    deleted_recording = False
    if target is not None:
        _delete_recording(destination, target, session)
        deleted_recording = True
    deleted = 0
    # Marker last, so an interrupted discard is still a session a retry can load.
    for name in ("quality-report.json", "annotations.json", "session.json", MARKER_NAME):
        if name not in present:
            continue
        try:
            (destination / name).unlink()
        except OSError:
            raise LabelingError("discard_incomplete") from None
        deleted += 1
    unexpected = 0
    removed = False
    if destination.exists():
        unexpected = sum(1 for _item in destination.iterdir())
        if unexpected == 0:
            destination.rmdir()
            removed = True
    else:
        removed = True
    return DiscardResult(deleted, deleted_recording, outside and not deleted_recording, unexpected, removed)
