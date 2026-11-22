"""Offline population baseline for confirmed gesture intervals.

Run ``python -m ml.train``. ``fit`` trains one standardized logistic
regression on population ``train`` sessions. ``evaluate`` scores that model
only on separate held-out ``eval`` sessions. Neither command opens a camera,
publishes a gesture message, or builds a motor command, and the model is not
connected to live control.

Every row that fits the scaler or the model goes through the ML-2A checks:
``validate_split_plan``, then ``fitting_scope(plan, "population")``, then
``assert_examples_may_fit``. The hyperparameters are fixed. Nothing is
selected on any data, so no row is used for selection.

``fit --features baseline-relative`` (ML-4A, experimental) subtracts each
training session's resting baseline, the mean of its confirmed ``neutral``
windows, before scaling. Its ``evaluate`` needs one ``calibration`` session
per held-out participant, and the baseline comes from that session's neutral
rows only, through ``fitting_scope(plan, "personalization", participant)``.
The default ``raw`` mode is the unchanged ML-3 model.

The artifact is JSON, not a pickle, so loading it runs no code.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ml.dataset import (
    _LABEL,
    _PROVENANCE,
    _TOKEN,
    DatasetError,
    ExtractionConfig,
    SplitPlanError,
    assert_examples_may_fit,
    fitting_scope,
    validate_split_plan,
)
from ml.features import FEATURE_DIM, FEATURE_NAMES
from ml.labeling import (
    LabelingError,
    SessionExamples,
    _output_dir,
    _read_regular,
    _strict_json,
    _sync_directory,
    check_collection,
    load_session_examples,
)

MODEL_FORMAT = "sojourner-gesture-logreg-v1"
MODEL_FILE = "model.json"
# Written and fsynced under this name, then renamed to MODEL_FILE only after
# its bytes are verified. Its presence means publication did not finish.
PARTIAL_FILE = ".model.json.partial"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_CLASSES = ("neutral", "mouth_open", "brow_raise")
# ``raw`` is the ML-3 model. ``baseline-relative`` subtracts a per-session
# resting baseline (the mean of confirmed ``neutral`` windows) before scaling.
FEATURE_MODES = ("raw", "baseline-relative")
BASELINE_LABEL = "neutral"
# A sanity floor fixed before any recording was seen, not a tuned value.
MIN_BASELINE_WINDOWS = 10
# Fixed before any data is seen. Nothing here is tuned on a recording.
# The penalty is L2, scikit-learn's default; it is not passed because 1.8
# deprecated the ``penalty`` argument.
HYPERPARAMETERS = {
    "C": 1.0,
    "class_weight": "balanced",
    "max_iter": 1000,
    "solver": "lbfgs",
    "tol": 1e-4,
}
_MODEL_KEYS = frozenset(
    {
        "format",
        "purpose",
        "not_for_live_control",
        "classes",
        "feature_names",
        "scaler_mean",
        "scaler_scale",
        "coef",
        "intercept",
        "hyperparameters",
        "library_versions",
        "extraction",
        "training",
        "training_diagnostics",
    }
)
_TRAINING_KEYS = frozenset(
    {"recordings", "class_counts", "examples", "timestamp_provenance", "source_categories"}
)
_RECORDING_KEYS = frozenset(
    {"dataset_id", "participant_id", "session_id", "recording_id", "sha256", "byte_length"}
)


class TrainingError(ValueError):
    """A training or evaluation refusal. ``code`` is a stable token, not a record."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FitSummary:
    out_dir: Path
    classes: tuple[str, ...]
    class_counts: tuple[tuple[str, int], ...]
    interval_counts: tuple[tuple[str, int], ...]
    examples: int
    recordings: int
    participants: int
    timestamp_provenance: str
    n_iter: int
    converged: bool
    resubstitution_accuracy: float
    review_reasons: tuple[str, ...]
    feature_mode: str = "raw"


@dataclass(frozen=True)
class EvalSummary:
    examples: int
    recordings: int
    participants: int
    accuracy: float
    per_class: tuple[tuple[str, int, int], ...]  # label, support, correct
    confusion: tuple[tuple[str, str, int], ...]  # true, predicted, count
    feature_mode: str = "raw"
    calibration_recordings: int = 0


def _libraries():
    try:
        import numpy
        import sklearn
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        raise TrainingError("scikit_learn_not_installed") from None
    return numpy, sklearn, LogisticRegression, StandardScaler


def _parse_classes(classes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(classes, str) or not isinstance(classes, Sequence):
        raise TrainingError("invalid_classes")
    names = tuple(classes)
    if len(names) < 2 or len(set(names)) != len(names):
        raise TrainingError("invalid_classes")
    for name in names:
        if not isinstance(name, str) or not name or name == "unknown":
            raise TrainingError("invalid_classes")
    return names


def _load_sessions(directories: Sequence[Path], allow_constructed: bool) -> list[SessionExamples]:
    """Validate the collection, then load each session through the prepare checks."""
    if isinstance(directories, (str, bytes, Path)) or not isinstance(directories, Sequence):
        raise TrainingError("invalid_session_list")
    if not directories:
        raise TrainingError("no_sessions")
    # Refuses reused or duplicated recordings and inconsistent declared ids.
    check_collection(list(directories))
    loaded = [load_session_examples(Path(d)) for d in directories]
    measured = [item.report.timestamps_are_measured_capture for item in loaded]
    if all(measured):
        pass
    elif any(measured):
        raise TrainingError("mixed_provenance")
    elif not allow_constructed:
        raise TrainingError("timestamps_not_measured")
    elif any(item.report.source_category != "constructed_fixture" for item in loaded):
        raise TrainingError("timestamps_not_measured")
    extraction = {(e.version, e.length, e.stride) for e in (item.extraction for item in loaded)}
    if len(extraction) != 1:
        raise TrainingError("extraction_mismatch")
    return loaded


def _plan(loaded: Sequence[SessionExamples], examples):
    roles = []
    for item in loaded:
        if item.role not in roles:
            roles.append(item.role)
    return validate_split_plan(
        roles,
        [item.session for item in loaded],
        [item.assignment for item in loaded],
        examples,
    )


def _matrix(numpy, rows, classes: tuple[str, ...]):
    """Feature matrix and labels. Every vector must be 162 finite numbers."""
    vectors = []
    labels = []
    for item in rows:
        vector = item.example.features
        if not isinstance(vector, (tuple, list)) or len(vector) != FEATURE_DIM:
            raise TrainingError("invalid_feature_vector")
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise TrainingError("non_finite_feature")
        if item.example.label not in classes:
            raise TrainingError("label_not_in_classes")
        vectors.append(vector)
        labels.append(item.example.label)
    return numpy.asarray(vectors, dtype=numpy.float64), labels


def _session_key(item) -> tuple[str, str, str]:
    return (item.example.participant_id, item.example.session_id, item.recording_id)


def _mean_baseline(numpy, rows, classes: tuple[str, ...], code: str):
    """Mean vector of authorized ``neutral`` rows. Too few rows raises ``code``."""
    own = [item for item in rows if item.example.label == BASELINE_LABEL]
    if len(own) < MIN_BASELINE_WINDOWS:
        raise TrainingError(code)
    return _matrix(numpy, own, classes)[0].mean(axis=0)


def _ordered(examples):
    return tuple(
        sorted(
            examples,
            key=lambda item: (
                item.recording_id,
                item.example.window_start_timestamp_ms,
                item.example.window_end_timestamp_ms,
            ),
        )
    )


def fit_population(
    directories: Sequence[Path],
    out_dir: Path,
    *,
    classes: Sequence[str] = DEFAULT_CLASSES,
    allow_constructed: bool = False,
    feature_mode: str = "raw",
) -> FitSummary:
    """Fit the scaler and the model on authorized population train rows, then write the artifact.

    In ``baseline-relative`` mode each training session's baseline is the mean
    of its own authorized ``neutral`` rows, and it is subtracted from every row
    of that session before the scaler and the model are fit.
    """
    classes = _parse_classes(classes)
    if not isinstance(feature_mode, str) or feature_mode not in FEATURE_MODES:
        raise TrainingError("invalid_feature_mode")
    if feature_mode == "baseline-relative" and BASELINE_LABEL not in classes:
        raise TrainingError("baseline_requires_neutral_class")
    numpy, sklearn, LogisticRegression, StandardScaler = _libraries()
    destination = _new_output(out_dir)
    loaded = sorted(_load_sessions(directories, allow_constructed), key=lambda item: item.assignment.recording_id)
    for item in loaded:
        if item.role.role != "population_train" or item.session.split != "train":
            raise TrainingError("training_session_not_population_train")
    examples = _ordered(row for item in loaded for row in item.examples)
    if not examples:
        raise TrainingError("no_training_examples")
    plan = _plan(loaded, examples)
    scope = fitting_scope(plan, "population")
    # The authorized tuple is the exact set of rows fit below: scaler and model.
    authorized = assert_examples_may_fit(examples, scope).examples
    features, labels = _matrix(numpy, authorized, classes)
    present = set(labels)
    if any(name not in present for name in classes):
        raise TrainingError("missing_class")
    if feature_mode == "baseline-relative":
        baselines = {
            key: _mean_baseline(
                numpy,
                [item for item in authorized if _session_key(item) == key],
                classes,
                "training_baseline_insufficient",
            )
            for key in sorted({_session_key(item) for item in authorized})
        }
        features = features - numpy.asarray([baselines[_session_key(item)] for item in authorized])
    scaler = StandardScaler().fit(features)
    model = LogisticRegression(**HYPERPARAMETERS).fit(scaler.transform(features), labels)
    if tuple(model.classes_) != tuple(sorted(classes)):
        raise TrainingError("missing_class")
    n_iter = int(numpy.max(model.n_iter_))
    converged = n_iter < HYPERPARAMETERS["max_iter"]
    predicted = model.predict(scaler.transform(features))
    accuracy = float(numpy.mean(predicted == numpy.asarray(labels)))
    counts = tuple((name, labels.count(name)) for name in sorted(classes))
    intervals: dict[str, int] = {}
    for item in loaded:
        for name, count in item.report.per_label_counts:
            intervals[name] = intervals.get(name, 0) + count
    provenance = loaded[0].report.timestamp_provenance
    artifact = {
        "format": MODEL_FORMAT,
        "purpose": "population",
        "not_for_live_control": True,
        "classes": [str(name) for name in model.classes_],
        "feature_names": list(FEATURE_NAMES),
        "scaler_mean": [float(v) for v in scaler.mean_],
        "scaler_scale": [float(v) for v in scaler.scale_],
        "coef": [[float(v) for v in row] for row in model.coef_],
        "intercept": [float(v) for v in model.intercept_],
        "hyperparameters": dict(HYPERPARAMETERS),
        "library_versions": {"numpy": numpy.__version__, "scikit-learn": sklearn.__version__},
        "extraction": {
            "version": loaded[0].extraction.version,
            "length": loaded[0].extraction.length,
            "stride": loaded[0].extraction.stride,
        },
        "training": {
            "recordings": [
                {
                    "dataset_id": item.dataset_id,
                    "participant_id": item.assignment.participant_id,
                    "session_id": item.assignment.session_id,
                    "recording_id": item.assignment.recording_id,
                    "sha256": item.recording_sha256,
                    "byte_length": item.recording_byte_length,
                }
                for item in loaded
            ],
            "class_counts": dict(counts),
            "examples": len(authorized),
            "timestamp_provenance": provenance,
            "source_categories": sorted({item.report.source_category for item in loaded}),
        },
        "training_diagnostics": {
            "n_iter": n_iter,
            "converged": converged,
            "resubstitution_accuracy": accuracy,
            "held_out": False,
        },
    }
    if feature_mode != "raw":
        # Raw artifacts stay byte-identical to ML-3; the key marks the new mode only.
        artifact["feature_mode"] = feature_mode
    _write_artifact(destination, artifact)
    reviews = sorted({reason for item in loaded for reason in item.report.review_reasons})
    return FitSummary(
        out_dir=destination,
        classes=tuple(artifact["classes"]),
        class_counts=counts,
        interval_counts=tuple(sorted(intervals.items())),
        examples=len(authorized),
        recordings=len(loaded),
        participants=len({item.assignment.participant_id for item in loaded}),
        timestamp_provenance=provenance,
        n_iter=n_iter,
        converged=converged,
        resubstitution_accuracy=accuracy,
        review_reasons=tuple(reviews),
        feature_mode=feature_mode,
    )


def _new_output(out_dir: Path) -> Path:
    """A new local directory: absent now, outside git or under captures/ or datasets/."""
    destination = _output_dir(Path(out_dir))
    if os.path.lexists(destination):
        raise TrainingError("output_exists")
    if not destination.parent.is_dir():
        raise TrainingError("output_parent_missing")
    return destination


def _create_file(path: Path, text: str, created: list[Path]) -> None:
    """Create ``path`` exclusively (0600), write, flush, and fsync it.

    The path is recorded in ``created`` as soon as it exists, so a failed
    write, flush, fsync, or close still leaves it for the caller to remove.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags, 0o600)
    created.append(path)
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _write_artifact(destination: Path, artifact: dict) -> None:
    """Create the directory (0700), a ``.gitignore`` that ignores everything, then ``model.json`` (0600).

    The model is written to ``PARTIAL_FILE`` and fsynced, read back and
    compared byte for byte, and only then renamed to ``model.json`` in this
    new private directory. The published file is loaded once with
    ``load_model``. On any failure every file this call created is removed,
    then the directory, and the fit reports ``output_write_failed``.
    """
    text = json.dumps(artifact, sort_keys=True, indent=1, allow_nan=False) + "\n"
    try:
        os.mkdir(destination, 0o700)
    except FileExistsError:
        raise TrainingError("output_exists") from None
    except OSError:
        raise TrainingError("output_write_failed") from None
    created: list[Path] = []
    try:
        _create_file(destination / ".gitignore", "*\n", created)
        partial = destination / PARTIAL_FILE
        _create_file(partial, text, created)
        if _read_regular(partial, missing="output_write_failed") != text.encode("utf-8"):
            raise TrainingError("output_write_failed")
        final = destination / MODEL_FILE
        os.rename(partial, final)
        created[created.index(partial)] = final
        _sync_directory(destination)
        load_model(destination)
    except (OSError, LabelingError, TrainingError):
        for path in reversed(created):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(destination)
        except OSError:
            pass
        raise TrainingError("output_write_failed") from None


def _vector(value, length: int, code: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise TrainingError(code)
    numbers = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TrainingError(code)
        try:
            number = float(item)
        except OverflowError:
            # An integer too large for a float.
            raise TrainingError(code) from None
        if not math.isfinite(number):
            raise TrainingError(code)
        numbers.append(number)
    return numbers


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrainingError("invalid_model")
    return value


def _check_token(value, pattern) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise TrainingError("invalid_model")


def load_model(model_dir: Path) -> dict:
    """Read ``model.json`` as strict JSON and check every field before use."""
    if os.path.lexists(Path(model_dir) / PARTIAL_FILE):
        raise TrainingError("model_publication_incomplete")
    path = Path(model_dir) / MODEL_FILE
    try:
        data = _strict_json(_read_regular(path, missing="model_missing"))
    except LabelingError as exc:
        code = "model_missing" if exc.code == "model_missing" else "invalid_model"
        raise TrainingError(code) from None
    if not isinstance(data, dict) or set(data) - {"feature_mode"} != _MODEL_KEYS:
        raise TrainingError("invalid_model")
    # A model without ``feature_mode`` is an ML-3 raw model.
    if "feature_mode" in data and data["feature_mode"] != "baseline-relative":
        raise TrainingError("invalid_model")
    if data["format"] != MODEL_FORMAT or data["purpose"] != "population":
        raise TrainingError("invalid_model")
    if data["not_for_live_control"] is not True:
        raise TrainingError("invalid_model")
    if data["feature_names"] != list(FEATURE_NAMES):
        raise TrainingError("feature_names_mismatch")
    classes = data["classes"]
    if not isinstance(classes, list) or len(classes) < 2:
        raise TrainingError("invalid_model")
    for name in classes:
        _check_token(name, _LABEL)
        if name == "unknown":
            raise TrainingError("invalid_model")
    if len(set(classes)) != len(classes):
        raise TrainingError("invalid_model")
    if "feature_mode" in data and BASELINE_LABEL not in classes:
        raise TrainingError("invalid_model")
    rows = 1 if len(classes) == 2 else len(classes)
    _vector(data["scaler_mean"], FEATURE_DIM, "invalid_model")
    scale = _vector(data["scaler_scale"], FEATURE_DIM, "invalid_model")
    if any(value <= 0 for value in scale):
        raise TrainingError("invalid_model")
    if not isinstance(data["coef"], list) or len(data["coef"]) != rows:
        raise TrainingError("invalid_model")
    for row in data["coef"]:
        _vector(row, FEATURE_DIM, "invalid_model")
    _vector(data["intercept"], rows, "invalid_model")
    training = data["training"]
    if not isinstance(training, dict) or set(training) != _TRAINING_KEYS:
        raise TrainingError("invalid_model")
    if not isinstance(training["recordings"], list) or not training["recordings"]:
        raise TrainingError("invalid_model")
    for recording in training["recordings"]:
        if not isinstance(recording, dict) or set(recording) != _RECORDING_KEYS:
            raise TrainingError("invalid_model")
        for key in ("dataset_id", "participant_id", "session_id", "recording_id"):
            _check_token(recording[key], _TOKEN)
        _check_token(recording["sha256"], _SHA256)
        _count(recording["byte_length"])
    provenance = training["timestamp_provenance"]
    if not isinstance(provenance, str) or provenance not in _PROVENANCE:
        raise TrainingError("invalid_model")
    counts = training["class_counts"]
    if not isinstance(counts, dict) or set(counts) != set(classes):
        raise TrainingError("invalid_model")
    for value in counts.values():
        _count(value)
    _count(training["examples"])
    extraction = data["extraction"]
    if not isinstance(extraction, dict) or set(extraction) != {"version", "length", "stride"}:
        raise TrainingError("invalid_model")
    try:
        ExtractionConfig(extraction["version"], extraction["length"], extraction["stride"])
    except DatasetError:
        raise TrainingError("invalid_model") from None
    return data


def _predict(numpy, model: dict, features):
    mean = numpy.asarray(model["scaler_mean"])
    scale = numpy.asarray(model["scaler_scale"])
    coef = numpy.asarray(model["coef"])
    intercept = numpy.asarray(model["intercept"])
    scores = ((features - mean) / scale) @ coef.T + intercept
    classes = model["classes"]
    if len(classes) == 2:
        return [classes[1] if score > 0 else classes[0] for score in scores[:, 0]]
    return [classes[index] for index in numpy.argmax(scores, axis=1)]


def _calibration_baselines(numpy, plan, evaluated, calibrating, classes):
    """One baseline per held-out participant, from their calibration session only.

    Each baseline is the mean of that participant's confirmed ``neutral``
    calibration rows, after ``assert_examples_may_fit`` with a personalization
    scope for that participant. No eval row is read here.
    """
    by_participant: dict[str, SessionExamples] = {}
    for item in calibrating:
        participant = item.assignment.participant_id
        if participant in by_participant:
            raise TrainingError("conflicting_calibration")
        by_participant[participant] = item
    targets = {item.assignment.participant_id for item in evaluated}
    if set(by_participant) - targets:
        raise TrainingError("calibration_participant_not_evaluated")
    if targets - set(by_participant):
        raise TrainingError("calibration_missing")
    eval_sessions = {(item.assignment.participant_id, item.assignment.session_id) for item in evaluated}
    eval_recordings = {item.assignment.recording_id for item in evaluated}
    baselines = {}
    for participant, item in sorted(by_participant.items()):
        if (participant, item.assignment.session_id) in eval_sessions or item.assignment.recording_id in eval_recordings:
            raise TrainingError("calibration_evaluation_overlap")
        rows = tuple(row for row in _ordered(item.examples) if row.example.label == BASELINE_LABEL)
        if len(rows) < MIN_BASELINE_WINDOWS:
            raise TrainingError("calibration_baseline_insufficient")
        scope = fitting_scope(plan, "personalization", participant)
        authorized = assert_examples_may_fit(rows, scope).examples
        baselines[participant] = _mean_baseline(numpy, authorized, classes, "calibration_baseline_insufficient")
    return baselines


def evaluate_held_out(
    model_dir: Path,
    directories: Sequence[Path],
    *,
    calibration: Sequence[Path] = (),
    allow_constructed: bool = False,
) -> EvalSummary:
    """Score the model on separate held-out eval sessions, or refuse.

    Every session must be ``split=eval`` for a ``held_out`` participant who
    did not contribute to training, and no recording may match a training
    recording by id or by SHA-256 and length. Windows from one recording are
    never divided between training and evaluation.

    A ``baseline-relative`` model also needs exactly one ``calibration``
    session per evaluated participant. Its baseline comes only from that
    session's neutral rows. The scaler and the classifier are never refit.
    """
    numpy, _sklearn, _lr, _scaler = _libraries()
    model = load_model(model_dir)
    mode = model.get("feature_mode", "raw")
    for value in (directories, calibration):
        if isinstance(value, (str, bytes, Path)) or not isinstance(value, Sequence):
            raise TrainingError("invalid_session_list")
    directories, calibration = list(directories), list(calibration)
    if not directories:
        raise TrainingError("no_sessions")
    if mode == "raw" and calibration:
        raise TrainingError("calibration_not_used_by_raw_model")
    if mode != "raw" and not calibration:
        raise TrainingError("calibration_required")
    loaded = _load_sessions(directories + calibration, allow_constructed)
    evaluated, calibrating = loaded[: len(directories)], loaded[len(directories) :]
    for item in evaluated:
        if item.role.role != "held_out" or item.session.split != "eval":
            raise TrainingError("evaluation_requires_held_out_eval_sessions")
    for item in calibrating:
        if item.role.role != "held_out" or item.session.split != "calibration":
            raise TrainingError("calibration_session_invalid")
    training = model["training"]
    trained_participants = {r["participant_id"] for r in training["recordings"]}
    trained_recordings = {r["recording_id"] for r in training["recordings"]}
    trained_content = {(r["sha256"], r["byte_length"]) for r in training["recordings"]}
    for item in loaded:
        if item.assignment.participant_id in trained_participants:
            raise TrainingError("participant_overlap_with_training")
        if item.assignment.recording_id in trained_recordings:
            raise TrainingError("recording_overlap_with_training")
        if (item.recording_sha256, item.recording_byte_length) in trained_content:
            raise TrainingError("recording_overlap_with_training")
    provenance = loaded[0].report.timestamp_provenance
    if provenance != training["timestamp_provenance"]:
        raise TrainingError("provenance_mismatch_with_training")
    extraction = loaded[0].extraction
    if (extraction.version, extraction.length, extraction.stride) != (
        model["extraction"]["version"],
        model["extraction"]["length"],
        model["extraction"]["stride"],
    ):
        raise TrainingError("extraction_mismatch")
    examples = _ordered(row for item in evaluated for row in item.examples)
    if not examples:
        raise TrainingError("no_evaluation_examples")
    calibration_rows = _ordered(row for item in calibrating for row in item.examples)
    plan = _plan(loaded, examples + calibration_rows)
    classes = tuple(model["classes"])
    features, labels = _matrix(numpy, examples, classes)
    if mode == "baseline-relative":
        baselines = _calibration_baselines(numpy, plan, evaluated, calibrating, classes)
        features = features - numpy.asarray([baselines[item.example.participant_id] for item in examples])
    predicted = _predict(numpy, model, features)
    correct = sum(p == t for p, t in zip(predicted, labels))
    per_class = tuple(
        (
            name,
            labels.count(name),
            sum(p == t == name for p, t in zip(predicted, labels)),
        )
        for name in classes
    )
    confusion = tuple(
        (true, guess, sum(t == true and p == guess for p, t in zip(predicted, labels)))
        for true in classes
        for guess in classes
    )
    return EvalSummary(
        examples=len(examples),
        recordings=len(evaluated),
        participants=len({item.assignment.participant_id for item in evaluated}),
        accuracy=correct / len(examples),
        per_class=per_class,
        confusion=confusion,
        feature_mode=mode,
        calibration_recordings=len(calibrating),
    )


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise TrainingError("operator_input_invalid")


def _build() -> _Parser:
    parser = _Parser(prog="python -m ml.train")
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser("fit")
    fit.add_argument("--session", type=Path, action="append", required=True)
    fit.add_argument("--out", type=Path, required=True)
    fit.add_argument("--classes", default=",".join(DEFAULT_CLASSES))
    fit.add_argument("--allow-constructed", action="store_true")
    fit.add_argument("--features", choices=FEATURE_MODES, default="raw")
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--session", type=Path, action="append", required=True)
    evaluate.add_argument("--calibration", type=Path, action="append", default=[])
    evaluate.add_argument("--allow-constructed", action="store_true")
    return parser


def _fit_text(summary: FitSummary) -> str:
    lines = [
        f"classes={','.join(summary.classes)}",
        *(f"examples_per_class {name},{count}" for name, count in summary.class_counts),
        *(f"intervals_per_class {name},{count}" for name, count in summary.interval_counts),
        f"examples={summary.examples}",
        f"recordings={summary.recordings}",
        f"participants={summary.participants}",
        f"timestamp_provenance={summary.timestamp_provenance}",
        *([f"feature_mode={summary.feature_mode}"] if summary.feature_mode != "raw" else []),
        "algorithm=standard_scaler+logistic_regression",
        *(f"hyperparameter {key}={HYPERPARAMETERS[key]}" for key in sorted(HYPERPARAMETERS)),
        f"n_iter={summary.n_iter}",
        f"converged={str(summary.converged).lower()}",
        f"training_resubstitution_accuracy={summary.resubstitution_accuracy:.4f}",
        "training_resubstitution_is_not_held_out_accuracy=true",
        "held_out_evaluation=none",
        *(f"review_reasons {reason}" for reason in summary.review_reasons),
        f"model={summary.out_dir / MODEL_FILE}",
    ]
    return "\n".join(lines) + "\n"


def _eval_text(summary: EvalSummary) -> str:
    lines = [
        "held_out_evaluation=true",
        f"held_out_participants={summary.participants}",
        f"held_out_recordings={summary.recordings}",
        *(
            [f"feature_mode={summary.feature_mode}", f"calibration_recordings={summary.calibration_recordings}"]
            if summary.feature_mode != "raw"
            else []
        ),
        f"examples={summary.examples}",
        f"window_accuracy={summary.accuracy:.4f}",
        *(f"per_class {name},support={support},correct={right}" for name, support, right in summary.per_class),
        *(f"confusion {true},{guess},{count}" for true, guess, count in summary.confusion),
        "windows_within_a_recording_are_correlated=true",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _build().parse_args(argv)
        if args.command == "fit":
            summary = fit_population(
                args.session,
                args.out,
                classes=tuple(args.classes.split(",")),
                allow_constructed=args.allow_constructed,
                feature_mode=args.features,
            )
            print(_fit_text(summary), end="")
        else:
            summary = evaluate_held_out(
                args.model,
                args.session,
                calibration=args.calibration,
                allow_constructed=args.allow_constructed,
            )
            print(_eval_text(summary), end="")
        return 0
    except (TrainingError, LabelingError, DatasetError) as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except SplitPlanError as exc:
        print(",".join(exc.codes), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
