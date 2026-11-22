"""Constructed checks for the ML-3 offline population baseline.

Every frame here is built by the test. None is a participant recording or a
measured camera timestamp. Sessions are ``constructed_fixture``, so fitting
needs ``allow_constructed=True`` exactly as the CLI needs ``--allow-constructed``.
"""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("sklearn")

from perception.fake_stream import encode  # noqa: E402
from tests.test_ml_pipeline import T0, _scores, lost, tracking  # noqa: E402

from ml import train  # noqa: E402
from ml.dataset import SplitPlanError  # noqa: E402
from ml.labeling import (  # noqa: E402
    LabelingError,
    confirm_interval,
    create_session,
    load_session_examples,
    mark_interval,
)

ROOT = Path(__file__).resolve().parent.parent
STEP = 10
SEGMENT = 12
PATTERN = {
    "neutral": {},
    "mouth_open": {"jawOpen": 0.7},
    "brow_raise": {"browInnerUp": 0.6, "browOuterUpLeft": 0.6, "browOuterUpRight": 0.6},
}


def _frames(order, jitter):
    frames = []
    spans = []
    stamp = T0
    for label in order:
        start = stamp
        for index in range(SEGMENT):
            # Deterministic small variation so the scaler sees spread.
            wobble = ((index * 7 + jitter) % 5) * 0.01
            scores = {name: value + wobble for name, value in PATTERN[label].items()}
            scores["eyeBlinkLeft"] = wobble
            frames.append(tracking(stamp, scores=_scores(**scores)))
            stamp += STEP
        spans.append((start, stamp, label))
        # Unlabeled rest between segments.
        frames.append(tracking(stamp, scores=_scores()))
        stamp += STEP
    return frames, spans


def _session(
    base: Path,
    name: str,
    *,
    participant="p01",
    session=None,
    recording=None,
    split="train",
    role="population_train",
    order=("neutral", "mouth_open", "brow_raise"),
    jitter=0,
    frames=None,
):
    folder = base / name
    folder.mkdir(parents=True)
    path = folder / "features.jsonl"
    built, spans = _frames(order, jitter)
    if frames is not None:
        built = frames
    path.write_text("".join(encode(frame) + "\n" for frame in built), encoding="utf-8")
    create_session(
        path,
        folder,
        dataset_id="ds01",
        participant_id=participant,
        session_id=session or f"s-{name}",
        recording_id=recording or f"r-{name}",
        split=split,
        participant_role=role,
        source_category="constructed_fixture",
        timestamp_provenance="constructed",
        capture_claim="constructed_fixture",
        decision_id="decision-1",
    )
    for index, (start, end, label) in enumerate(spans):
        mark_interval(folder, start, end, label)
        confirm_interval(folder, index)
    return folder


def _fit(sessions, out, **kwargs):
    kwargs.setdefault("allow_constructed", True)
    return train.fit_population(sessions, out, **kwargs)


def test_fit_writes_a_private_json_artifact_without_rows(tmp_path):
    a = _session(tmp_path, "a")
    b = _session(tmp_path, "b", participant="p02", jitter=3)
    out = tmp_path / "model"
    summary = _fit([a, b], out)
    assert summary.classes == ("brow_raise", "mouth_open", "neutral")
    assert dict(summary.class_counts) == {"brow_raise": 24, "mouth_open": 24, "neutral": 24}
    assert summary.examples == 72 and summary.recordings == 2 and summary.participants == 2
    assert summary.converged
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o700
    for name in (".gitignore", "model.json"):
        assert stat.S_IMODE(os.stat(out / name).st_mode) == 0o600
    assert (out / ".gitignore").read_text() == "*\n"
    assert sorted(os.listdir(out)) == [".gitignore", "model.json"]
    text = (out / "model.json").read_text()
    data = json.loads(text)
    assert data["not_for_live_control"] is True
    assert data["training_diagnostics"]["held_out"] is False
    assert len(data["coef"]) == 3 and all(len(row) == 162 for row in data["coef"])
    # No window timestamps or per-row vectors are stored.
    assert str(T0) not in text
    assert "features" not in data and "examples" not in data
    assert train.load_model(out)["classes"] == list(summary.classes)


def test_constructed_data_needs_an_explicit_flag(tmp_path):
    a = _session(tmp_path, "a")
    with pytest.raises(train.TrainingError) as exc:
        train.fit_population([a], tmp_path / "model")
    assert exc.value.code == "timestamps_not_measured"
    assert not (tmp_path / "model").exists()


def test_fit_is_reproducible_and_order_independent(tmp_path):
    a = _session(tmp_path, "a")
    b = _session(tmp_path, "b", participant="p02", jitter=3)
    _fit([a, b], tmp_path / "m1")
    _fit([b, a], tmp_path / "m2")
    assert (tmp_path / "m1" / "model.json").read_bytes() == (tmp_path / "m2" / "model.json").read_bytes()


def test_every_fitted_row_passes_population_authorization(tmp_path, monkeypatch):
    a = _session(tmp_path, "a")
    calls = []
    real = train.assert_examples_may_fit

    def spy(rows, scope):
        calls.append((len(rows), scope.purpose))
        return real(rows, scope)

    fitted = []
    numpy, sklearn, lr, scaler = train._libraries()

    class Scaler(scaler):
        def fit(self, X, y=None):
            fitted.append(len(X))
            return super().fit(X, y)

    monkeypatch.setattr(train, "assert_examples_may_fit", spy)
    monkeypatch.setattr(train, "_libraries", lambda: (numpy, sklearn, lr, Scaler))
    summary = _fit([a], tmp_path / "model")
    assert calls == [(summary.examples, "population")]
    assert fitted == [summary.examples]


def test_refused_authorization_fits_and_writes_nothing(tmp_path, monkeypatch):
    a = _session(tmp_path, "a")

    def refuse(rows, scope):
        raise SplitPlanError(("eval_excluded_from_fitting",))

    monkeypatch.setattr(train, "assert_examples_may_fit", refuse)
    with pytest.raises(SplitPlanError):
        _fit([a], tmp_path / "model")
    assert not (tmp_path / "model").exists()


@pytest.mark.parametrize(
    "split,role",
    [("eval", "held_out"), ("calibration", "held_out")],
)
def test_eval_and_calibration_sessions_never_join_population_fit(tmp_path, split, role):
    a = _session(tmp_path, "a")
    held = _session(tmp_path, "held", participant="p09", split=split, role=role, jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        _fit([a, held], tmp_path / "model")
    assert exc.value.code == "training_session_not_population_train"
    assert not (tmp_path / "model").exists()


def test_copied_recording_is_refused_as_duplicate_collection(tmp_path):
    a = _session(tmp_path, "a")
    b = _session(tmp_path, "b", participant="p02")  # same constructed bytes as a
    with pytest.raises(SplitPlanError) as exc:
        _fit([a, b], tmp_path / "model")
    assert "recording_content_duplicated" in exc.value.codes


def test_same_session_twice_is_refused(tmp_path):
    a = _session(tmp_path, "a")
    with pytest.raises(SplitPlanError):
        _fit([a, a], tmp_path / "model")


def test_missing_class_and_unexpected_label(tmp_path):
    two = _session(tmp_path, "two", order=("neutral", "mouth_open"))
    with pytest.raises(train.TrainingError) as exc:
        _fit([two], tmp_path / "m1")
    assert exc.value.code == "missing_class"
    three = _session(tmp_path, "three", participant="p02", jitter=2)
    with pytest.raises(train.TrainingError) as exc:
        _fit([three], tmp_path / "m2", classes=("neutral", "mouth_open"))
    assert exc.value.code == "label_not_in_classes"
    for bad in ((), ("neutral",), ("neutral", "neutral"), ("neutral", "unknown"), "neutral"):
        with pytest.raises(train.TrainingError) as exc:
            _fit([three], tmp_path / "m3", classes=bad)
        assert exc.value.code == "invalid_classes"


def test_two_class_model_trains_and_evaluates(tmp_path):
    a = _session(tmp_path, "a", order=("neutral", "mouth_open"))
    _fit([a], tmp_path / "model", classes=("neutral", "mouth_open"))
    held = _session(
        tmp_path, "held", participant="p09", split="eval", role="held_out",
        order=("neutral", "mouth_open"), jitter=4,
    )
    result = train.evaluate_held_out(tmp_path / "model", [held], allow_constructed=True)
    assert result.examples == 24 and result.accuracy == 1.0


def test_changed_recording_blocks_training(tmp_path):
    a = _session(tmp_path, "a")
    with (a / "features.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(encode(tracking(T0 + 10_000, scores=_scores())) + "\n")
    with pytest.raises(LabelingError) as exc:
        _fit([a], tmp_path / "model")
    assert exc.value.code == "recording_changed"


def test_malformed_and_nan_lines_never_become_rows(tmp_path):
    frames, spans = _frames(("neutral", "mouth_open", "brow_raise"), 0)
    lines = [encode(frame) for frame in frames]
    nan_line = lines[3].replace('"jawOpen":0.0', '"jawOpen":NaN')
    assert nan_line != lines[3]
    lines[3] = nan_line
    lines.insert(5, "{not json")
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "features.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    create_session(
        folder / "features.jsonl", folder, dataset_id="ds01", participant_id="p01",
        session_id="s1", recording_id="r1", split="train", participant_role="population_train",
        source_category="constructed_fixture", timestamp_provenance="constructed",
        capture_claim="constructed_fixture", decision_id="decision-1",
    )
    for index, (start, end, label) in enumerate(spans):
        mark_interval(folder, start, end, label)
        confirm_interval(folder, index)
    loaded = load_session_examples(folder)
    assert loaded.report.rejected_records == 2
    # One labeled frame was the NaN line, so 3 * 12 - 1 rows remain.
    assert len(loaded.examples) == 3 * SEGMENT - 1
    assert all(math.isfinite(v) for item in loaded.examples for v in item.example.features)
    summary = _fit([folder], tmp_path / "model")
    assert summary.examples == 3 * SEGMENT - 1


def test_non_finite_vector_is_refused_by_the_trainer(tmp_path, monkeypatch):
    a = _session(tmp_path, "a")
    real = train.load_session_examples

    def corrupt(directory):
        loaded = real(directory)
        row = loaded.examples[0].example
        object.__setattr__(row, "features", (math.nan,) + row.features[1:])
        return loaded

    monkeypatch.setattr(train, "load_session_examples", corrupt)
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], tmp_path / "model")
    assert exc.value.code == "non_finite_feature"
    assert not (tmp_path / "model").exists()


def test_wrong_length_vector_is_refused_by_the_trainer(tmp_path, monkeypatch):
    a = _session(tmp_path, "a")
    real = train.load_session_examples

    def corrupt(directory):
        loaded = real(directory)
        row = loaded.examples[0].example
        object.__setattr__(row, "features", row.features[:-1])
        return loaded

    monkeypatch.setattr(train, "load_session_examples", corrupt)
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], tmp_path / "model")
    assert exc.value.code == "invalid_feature_vector"


def test_output_location_rules(tmp_path):
    a = _session(tmp_path, "a")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], existing)
    assert exc.value.code == "output_exists"
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], tmp_path / "missing-parent" / "model")
    assert exc.value.code == "output_parent_missing"
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(LabelingError) as exc:
        _fit([a], link)
    assert exc.value.code == "symlink_not_allowed"
    with pytest.raises(LabelingError) as exc:
        _fit([a], ROOT / "ml" / "model-out")
    assert exc.value.code == "output_not_in_private_directory"
    assert not (ROOT / "ml" / "model-out").exists()


def _trained(tmp_path):
    a = _session(tmp_path, "a")
    b = _session(tmp_path, "b", participant="p02", jitter=3)
    _fit([a, b], tmp_path / "model")
    return a, b, tmp_path / "model"


def test_evaluate_scores_only_separate_held_out_sessions(tmp_path):
    a, _b, model = _trained(tmp_path)
    held = _session(tmp_path, "held", participant="p09", split="eval", role="held_out", jitter=1)
    result = train.evaluate_held_out(model, [held], allow_constructed=True)
    assert result.examples == 36 and result.participants == 1
    assert sum(support for _name, support, _ok in result.per_class) == 36
    assert sum(count for _t, _p, count in result.confusion) == 36
    # The JSON-only predictor agrees with scikit-learn on the same parameters.
    numpy, _sk, LR, Scaler = train._libraries()
    rows = [r for d in (a, _b) for r in load_session_examples(d).examples]
    X = numpy.asarray([r.example.features for r in train._ordered(rows)])
    y = [r.example.label for r in train._ordered(rows)]
    scaler = Scaler().fit(X)
    reference = LR(**train.HYPERPARAMETERS).fit(scaler.transform(X), y)
    H = numpy.asarray([r.example.features for r in load_session_examples(held).examples])
    assert train._predict(numpy, train.load_model(model), H) == list(reference.predict(scaler.transform(H)))
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [a], allow_constructed=True)
    assert exc.value.code == "evaluation_requires_held_out_eval_sessions"


def test_evaluate_refuses_overlap_with_training(tmp_path):
    _a, _b, model = _trained(tmp_path)
    same_person = _session(tmp_path, "same", participant="p01", split="eval", role="held_out", jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [same_person], allow_constructed=True)
    assert exc.value.code == "participant_overlap_with_training"
    same_id = _session(tmp_path, "sid", participant="p08", recording="r-a", split="eval", role="held_out", jitter=2)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [same_id], allow_constructed=True)
    assert exc.value.code == "recording_overlap_with_training"
    # Byte-identical to training recording "a" (jitter 0), new ids.
    copy = _session(tmp_path, "copy", participant="p07", split="eval", role="held_out")
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [copy], allow_constructed=True)
    assert exc.value.code == "recording_overlap_with_training"


def test_evaluate_refuses_eval_windows_from_tracking_loss_only(tmp_path):
    _a, _b, model = _trained(tmp_path)
    frames = [lost(T0 + i * STEP) for i in range(10)]
    folder = tmp_path / "lossy"
    folder.mkdir()
    (folder / "features.jsonl").write_text("".join(encode(f) + "\n" for f in frames))
    create_session(
        folder / "features.jsonl", folder, dataset_id="ds01", participant_id="p09",
        session_id="s9", recording_id="r9", split="eval", participant_role="held_out",
        source_category="constructed_fixture", timestamp_provenance="constructed",
        capture_claim="constructed_fixture", decision_id="decision-1",
    )
    mark_interval(folder, T0, T0 + 100, "neutral")
    confirm_interval(folder, 0)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [folder], allow_constructed=True)
    assert exc.value.code == "no_evaluation_examples"


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d.update(format="other"),
        lambda d: d.update(not_for_live_control=False),
        lambda d: d["coef"][0].pop(),
        lambda d: d["scaler_scale"].__setitem__(0, 0.0),
        lambda d: d["feature_names"].reverse(),
        lambda d: d.update(extra=1),
        lambda d: d.pop("training"),
    ],
)
def test_tampered_model_is_refused(tmp_path, edit):
    _a, _b, model = _trained(tmp_path)
    data = json.loads((model / "model.json").read_text())
    edit(data)
    (model / "model.json").write_text(json.dumps(data))
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code in {"invalid_model", "feature_names_mismatch"}


@pytest.mark.parametrize("text", ['{"a": NaN}', '{"a": 1, "a": 2}', "[", ""])
def test_non_json_model_is_refused(tmp_path, text):
    folder = tmp_path / "model"
    folder.mkdir()
    (folder / "model.json").write_text(text)
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(folder)
    assert exc.value.code == "invalid_model"
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(tmp_path / "absent")
    assert exc.value.code == "model_missing"


def test_cli_fit_and_refusals(tmp_path, capsys):
    a = _session(tmp_path, "a")
    assert train.main(["fit", "--session", str(a), "--out", str(tmp_path / "m"), "--allow-constructed"]) == 0
    out = capsys.readouterr().out
    assert "held_out_evaluation=none" in out
    assert "training_resubstitution_is_not_held_out_accuracy=true" in out
    assert "examples_per_class neutral,12" in out
    assert train.main(["evaluate", "--model", str(tmp_path / "m"), "--session", str(a), "--allow-constructed"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "evaluation_requires_held_out_eval_sessions\n"
    assert train.main(["fit", "--session", str(a), "--out", str(tmp_path / "m2")]) == 2
    assert capsys.readouterr().err == "timestamps_not_measured\n"
    assert train.main(["fit"]) == 2
    assert capsys.readouterr().err == "operator_input_invalid\n"


def test_importing_ml_does_not_load_scikit_learn():
    code = "import sys, ml; print('sklearn' in sys.modules, 'ml.train' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False False"


def test_load_session_examples_matches_prepare_and_writes_nothing(tmp_path):
    from ml.labeling import prepare_session

    a = _session(tmp_path, "a")
    before = sorted(os.listdir(a))
    loaded = load_session_examples(a)
    assert sorted(os.listdir(a)) == before and "quality-report.json" not in before
    report = prepare_session(a)
    assert len(loaded.examples) == report.examples_emitted == 36
    assert [
        (e.example.window_start_timestamp_ms, e.example.label) for e in loaded.examples
    ] == [(start, label) for start, _end, label, _count in report.example_windows]


class _FailingStream:
    """A text stream that raises OSError(EIO) at one chosen operation."""

    def __init__(self, stream, fail):
        self._stream = stream
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stream.close()
        return False

    def _maybe(self, name):
        if self._fail == name:
            raise OSError(errno.EIO, "constructed failure")

    def write(self, text):
        self._maybe("write")
        return self._stream.write(text)

    def flush(self):
        self._maybe("flush")
        return self._stream.flush()

    def fileno(self):
        return self._stream.fileno()


@pytest.mark.parametrize("fail", ["write", "flush", "fsync"])
@pytest.mark.parametrize("target", [1, 2], ids=[".gitignore", "model"])
def test_failed_artifact_write_leaves_nothing_loadable(tmp_path, monkeypatch, fail, target):
    """Fail the Nth artifact file at one stage. File 1 is .gitignore, file 2 the model."""
    a = _session(tmp_path, "a")
    out = tmp_path / "model"
    real_fdopen = os.fdopen
    real_fsync = os.fsync
    writes = []
    syncs = []

    def fdopen(handle, mode="r", *args, **kwargs):
        stream = real_fdopen(handle, mode, *args, **kwargs)
        if "w" not in mode:
            return stream
        writes.append(handle)
        return _FailingStream(stream, fail) if len(writes) == target and fail != "fsync" else stream

    def fsync(handle):
        syncs.append(handle)
        if fail == "fsync" and len(syncs) == target:
            raise OSError(errno.EIO, "constructed fsync failure")
        return real_fsync(handle)

    real_write = train._write_artifact

    def faulty_write(destination, artifact):
        # Faults apply only while the artifact is written, not to the prepare snapshot.
        with monkeypatch.context() as m:
            m.setattr(train.os, "fdopen", fdopen)
            m.setattr(train.os, "fsync", fsync)
            return real_write(destination, artifact)

    monkeypatch.setattr(train, "_write_artifact", faulty_write)
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], out)
    monkeypatch.undo()
    assert exc.value.code == "output_write_failed"
    assert len(writes) == target
    assert not out.exists()
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(out)
    assert exc.value.code == "model_missing"


def test_codex_model_fsync_reproduction_fails_safely(tmp_path, monkeypatch, capsys):
    # Second fsync is the model file (the first is .gitignore), as in the audit.
    a = _session(tmp_path, "a")
    out = tmp_path / "model"
    real = os.fsync
    calls = []

    def fail_second(handle):
        calls.append(handle)
        if len(calls) == 2:
            raise OSError(errno.EIO, "constructed fsync failure")
        return real(handle)

    monkeypatch.setattr(train.os, "fsync", fail_second)
    rc = train.main(["fit", "--session", str(a), "--out", str(out), "--allow-constructed"])
    monkeypatch.undo()
    captured = capsys.readouterr()
    assert rc == 2 and captured.out == "" and captured.err == "output_write_failed\n"
    assert not out.exists()
    with pytest.raises(train.TrainingError):
        train.load_model(out)


@pytest.mark.parametrize("stage", ["rename", "verify"])
def test_failed_publication_removes_the_published_name(tmp_path, monkeypatch, stage):
    a = _session(tmp_path, "a")
    out = tmp_path / "model"
    if stage == "rename":
        def fail(*_args):
            raise OSError(errno.EIO, "constructed rename failure")

        monkeypatch.setattr(train.os, "rename", fail)
    else:
        def fail(_directory):
            raise train.TrainingError("invalid_model")

        monkeypatch.setattr(train, "load_model", fail)
    with pytest.raises(train.TrainingError) as exc:
        _fit([a], out)
    monkeypatch.undo()
    assert exc.value.code == "output_write_failed"
    assert not out.exists()


def test_unpublished_or_partial_artifact_is_refused(tmp_path):
    _a, _b, model = _trained(tmp_path)
    (model / train.PARTIAL_FILE).write_text((model / "model.json").read_text())
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code == "model_publication_incomplete"
    (model / "model.json").unlink()
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code == "model_publication_incomplete"
    (model / train.PARTIAL_FILE).unlink()
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code == "model_missing"
    # A truncated published file is refused, not partly read.
    _a2, _b2, other = _trained(tmp_path / "second")
    text = (other / "model.json").read_text()
    (other / "model.json").write_text(text[: len(text) // 2])
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(other)
    assert exc.value.code == "invalid_model"


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda d: d.update(classes=[[], {}, "neutral"]), id="unhashable_classes"),
        pytest.param(lambda d: d.update(classes=["neutral", "bad label!", "mouth_open"]), id="bad_class_name"),
        pytest.param(lambda d: d.update(classes=["neutral", "unknown", "mouth_open"]), id="unknown_class"),
        pytest.param(lambda d: d["scaler_mean"].__setitem__(0, 10**1000), id="huge_integer_mean"),
        pytest.param(lambda d: d["coef"][1].__setitem__(5, -(10**400)), id="huge_integer_coef"),
        pytest.param(lambda d: d["intercept"].__setitem__(0, 10**500), id="huge_integer_intercept"),
        pytest.param(lambda d: d["training"]["recordings"][0].update(participant_id=[]), id="participant_list"),
        pytest.param(lambda d: d["training"]["recordings"][0].update(participant_id="bad id!"), id="participant_text"),
        pytest.param(lambda d: d["training"]["recordings"][0].update(recording_id=""), id="recording_empty"),
        pytest.param(lambda d: d["training"]["recordings"][0].update(sha256="xyz"), id="sha256"),
        pytest.param(lambda d: d["training"]["recordings"][0].update(byte_length=-1), id="byte_length"),
        pytest.param(lambda d: d["training"].update(timestamp_provenance="guess"), id="provenance"),
        pytest.param(lambda d: d["training"].update(timestamp_provenance=[]), id="provenance_list"),
        pytest.param(lambda d: d["training"].update(class_counts={"neutral": 1}), id="class_counts"),
        pytest.param(lambda d: d["extraction"].update(length=-1), id="negative_length"),
        pytest.param(lambda d: d["extraction"].update(length=0), id="zero_length"),
        pytest.param(lambda d: d["extraction"].update(stride=True), id="bool_stride"),
        pytest.param(lambda d: d["extraction"].update(version=7), id="version_type"),
    ],
)
def test_invalid_model_fields_are_controlled_refusals(tmp_path, edit):
    _a, _b, model = _trained(tmp_path)
    data = json.loads((model / "model.json").read_text())
    edit(data)
    (model / "model.json").write_text(json.dumps(data))
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code == "invalid_model"


def test_cli_unhashable_classes_has_no_traceback(tmp_path):
    _a, _b, model = _trained(tmp_path)
    data = json.loads((model / "model.json").read_text())
    data["classes"] = [[], {}, "neutral"]
    (model / "model.json").write_text(json.dumps(data))
    result = subprocess.run(
        [sys.executable, "-m", "ml.train", "evaluate", "--model", str(model), "--session", str(tmp_path / "unused")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 2 and result.stdout == "" and result.stderr == "invalid_model\n"
