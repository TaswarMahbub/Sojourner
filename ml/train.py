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

The artifact is JSON, not a pickle, so loading it runs no code.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ml.dataset import (
    DatasetError,
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
    check_collection,
    load_session_examples,
)

MODEL_FORMAT = "sojourner-gesture-logreg-v1"
MODEL_FILE = "model.json"
DEFAULT_CLASSES = ("neutral", "mouth_open", "brow_raise")
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


@dataclass(frozen=True)
class EvalSummary:
    examples: int
    recordings: int
    participants: int
    accuracy: float
    per_class: tuple[tuple[str, int, int], ...]  # label, support, correct
    confusion: tuple[tuple[str, str, int], ...]  # true, predicted, count


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
    return sorted(loaded, key=lambda item: item.assignment.recording_id)


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
) -> FitSummary:
    """Fit the scaler and the model on authorized population train rows, then write the artifact."""
    classes = _parse_classes(classes)
    numpy, sklearn, LogisticRegression, StandardScaler = _libraries()
    destination = _new_output(out_dir)
    loaded = _load_sessions(directories, allow_constructed)
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
    )


def _new_output(out_dir: Path) -> Path:
    """A new local directory: absent now, outside git or under captures/ or datasets/."""
    destination = _output_dir(Path(out_dir))
    if os.path.lexists(destination):
        raise TrainingError("output_exists")
    if not destination.parent.is_dir():
        raise TrainingError("output_parent_missing")
    return destination


def _create_file(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _write_artifact(destination: Path, artifact: dict) -> None:
    """Create the directory (0700), a ``.gitignore`` that ignores everything, then ``model.json`` (0600)."""
    text = json.dumps(artifact, sort_keys=True, indent=1, allow_nan=False) + "\n"
    try:
        os.mkdir(destination, 0o700)
    except FileExistsError:
        raise TrainingError("output_exists") from None
    except OSError:
        raise TrainingError("output_write_failed") from None
    written = []
    try:
        for name, content in ((".gitignore", "*\n"), (MODEL_FILE, text)):
            _create_file(destination / name, content)
            written.append(destination / name)
    except OSError:
        for path in written:
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
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item):
            raise TrainingError(code)
    return [float(item) for item in value]


def load_model(model_dir: Path) -> dict:
    """Read ``model.json`` as strict JSON and check every field before use."""
    path = Path(model_dir) / MODEL_FILE
    try:
        data = _strict_json(_read_regular(path, missing="model_missing"))
    except LabelingError as exc:
        code = "model_missing" if exc.code == "model_missing" else "invalid_model"
        raise TrainingError(code) from None
    if not isinstance(data, dict) or set(data) != _MODEL_KEYS:
        raise TrainingError("invalid_model")
    if data["format"] != MODEL_FORMAT or data["purpose"] != "population":
        raise TrainingError("invalid_model")
    if data["not_for_live_control"] is not True:
        raise TrainingError("invalid_model")
    if data["feature_names"] != list(FEATURE_NAMES):
        raise TrainingError("feature_names_mismatch")
    classes = data["classes"]
    if (
        not isinstance(classes, list)
        or len(classes) < 2
        or len(set(classes)) != len(classes)
        or not all(isinstance(name, str) and name for name in classes)
    ):
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
    extraction = data["extraction"]
    if not isinstance(extraction, dict) or set(extraction) != {"version", "length", "stride"}:
        raise TrainingError("invalid_model")
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


def evaluate_held_out(
    model_dir: Path, directories: Sequence[Path], *, allow_constructed: bool = False
) -> EvalSummary:
    """Score the model on separate held-out eval sessions, or refuse.

    Every session must be ``split=eval`` for a ``held_out`` participant who
    did not contribute to training, and no recording may match a training
    recording by id or by SHA-256 and length. Windows from one recording are
    never divided between training and evaluation.
    """
    numpy, _sklearn, _lr, _scaler = _libraries()
    model = load_model(model_dir)
    loaded = _load_sessions(directories, allow_constructed)
    for item in loaded:
        if item.role.role != "held_out" or item.session.split != "eval":
            raise TrainingError("evaluation_requires_held_out_eval_sessions")
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
    examples = _ordered(row for item in loaded for row in item.examples)
    if not examples:
        raise TrainingError("no_evaluation_examples")
    _plan(loaded, examples)
    classes = tuple(model["classes"])
    features, labels = _matrix(numpy, examples, classes)
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
        recordings=len(loaded),
        participants=len({item.assignment.participant_id for item in loaded}),
        accuracy=correct / len(examples),
        per_class=per_class,
        confusion=confusion,
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
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--model", type=Path, required=True)
    evaluate.add_argument("--session", type=Path, action="append", required=True)
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
            )
            print(_fit_text(summary), end="")
        else:
            print(_eval_text(evaluate_held_out(args.model, args.session, allow_constructed=args.allow_constructed)), end="")
        return 0
    except (TrainingError, LabelingError, DatasetError) as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except SplitPlanError as exc:
        print(",".join(exc.codes), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
