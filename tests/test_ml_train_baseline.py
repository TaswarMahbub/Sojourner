"""Constructed checks for the ML-4A baseline-relative feature mode.

Every frame is built here. None is a participant recording or a measured
camera timestamp, so every call uses ``allow_constructed=True``.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sklearn")

import numpy as np  # noqa: E402
from tests.test_ml_train import _frames, _session  # noqa: E402

from ml import train  # noqa: E402
from ml.dataset import SplitPlanError  # noqa: E402
from ml.labeling import load_session_examples  # noqa: E402

ALL = ("neutral", "mouth_open", "brow_raise")
SHIFT = {"browInnerUp": 0.25, "browOuterUpLeft": 0.2, "mouthShrugLower": 0.1, "eyeSquintLeft": 0.15}


def _shifted(order, jitter, shift):
    frames, _spans = _frames(order, jitter)
    for frame in frames:
        for name, value in shift.items():
            frame["features"][name] = frame["features"][name] + value
    return frames


def _make(base, name, *, shift=None, order=ALL, jitter=0, **kwargs):
    frames = _shifted(order, jitter, shift) if shift else None
    return _session(base, name, order=order, jitter=jitter, frames=frames, **kwargs)


def _train_set(base):
    return [
        _make(base, "p01a", participant="p01"),
        _make(base, "p01b", participant="p01", jitter=2, shift={"browInnerUp": 0.1}),
        _make(base, "p03", participant="p03", jitter=3, shift=SHIFT),
    ]


def _fit(sessions, out, **kwargs):
    kwargs.setdefault("allow_constructed", True)
    kwargs.setdefault("feature_mode", "baseline-relative")
    return train.fit_population(sessions, out, **kwargs)


# ``_frames`` uses jitter modulo 5, and training uses 0, 2 and 3. Held-out jitter
# 1 or 4 keeps every recording byte-distinct from training, as the overlap check needs.
def _held(base, name, participant, *, split, shift=None, order=None, jitter=4, **kwargs):
    order = order or (("neutral",) if split == "calibration" else ALL)
    return _make(
        base, name, participant=participant, split=split, role="held_out",
        order=order, jitter=jitter, shift=shift, **kwargs,
    )


def _spy_scaler(monkeypatch):
    fitted = []
    numpy, sklearn, lr, scaler = train._libraries()

    class Scaler(scaler):
        def fit(self, X, y=None):
            fitted.append(np.array(X, copy=True))
            return super().fit(X, y)

    monkeypatch.setattr(train, "_libraries", lambda: (numpy, sklearn, lr, Scaler))
    return fitted


def _rows(directory):
    return train._ordered(load_session_examples(directory).examples)


def test_each_training_session_subtracts_its_own_neutral_mean(tmp_path, monkeypatch):
    sessions = _train_set(tmp_path)
    fitted = _spy_scaler(monkeypatch)
    summary = _fit(sessions, tmp_path / "model")
    assert summary.feature_mode == "baseline-relative"
    (X,) = fitted
    # Training rows are ordered by recording id, then window time.
    expected = []
    for directory in sorted(sessions, key=lambda d: load_session_examples(d).assignment.recording_id):
        rows = _rows(directory)
        raw = np.asarray([r.example.features for r in rows])
        neutral = raw[[r.example.label == "neutral" for r in rows]].mean(axis=0)
        expected.append(raw - neutral)
    assert np.array_equal(X, np.vstack(expected))


def test_two_sessions_of_one_person_get_separate_baselines(tmp_path, monkeypatch):
    sessions = _train_set(tmp_path)
    fitted = _spy_scaler(monkeypatch)
    _fit(sessions, tmp_path / "model")
    (X,) = fitted
    start = 0
    means = []
    for directory in sorted(sessions, key=lambda d: load_session_examples(d).assignment.recording_id):
        rows = _rows(directory)
        block = X[start : start + len(rows)]
        start += len(rows)
        neutral = block[[r.example.label == "neutral" for r in rows]]
        assert np.allclose(neutral.mean(axis=0), 0.0, atol=1e-12)
        raw = np.asarray([r.example.features for r in rows])
        means.append(raw[[r.example.label == "neutral" for r in rows]].mean(axis=0))
    # p01's two sessions differ by the browInnerUp shift, so a per-person mean would be wrong.
    index = train.FEATURE_NAMES.index("browInnerUp.mean")
    assert means[0][index] != pytest.approx(means[1][index])


def test_baseline_fit_is_deterministic_and_marks_its_mode(tmp_path):
    sessions = _train_set(tmp_path)
    _fit(sessions, tmp_path / "m1")
    _fit(list(reversed(sessions)), tmp_path / "m2")
    first = (tmp_path / "m1" / "model.json").read_bytes()
    assert first == (tmp_path / "m2" / "model.json").read_bytes()
    assert json.loads(first)["feature_mode"] == "baseline-relative"
    assert train.load_model(tmp_path / "m1")["feature_mode"] == "baseline-relative"


def test_raw_mode_is_unchanged(tmp_path):
    sessions = _train_set(tmp_path)
    train.fit_population(sessions, tmp_path / "default", allow_constructed=True)
    train.fit_population(sessions, tmp_path / "raw", allow_constructed=True, feature_mode="raw")
    default = (tmp_path / "default" / "model.json").read_bytes()
    assert default == (tmp_path / "raw" / "model.json").read_bytes()
    assert "feature_mode" not in json.loads(default)
    assert "feature_mode" not in train.load_model(tmp_path / "raw")


def test_training_baseline_refusals(tmp_path):
    good = _make(tmp_path, "good", participant="p01")
    no_neutral = _make(tmp_path, "noneutral", participant="p03", order=("mouth_open", "brow_raise"), jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        _fit([good, no_neutral], tmp_path / "m1")
    assert exc.value.code == "training_baseline_insufficient"
    assert not (tmp_path / "m1").exists()
    two = _make(tmp_path, "two", participant="p05", order=("mouth_open", "brow_raise"), jitter=2)
    with pytest.raises(train.TrainingError) as exc:
        _fit([two], tmp_path / "m2", classes=("mouth_open", "brow_raise"))
    assert exc.value.code == "baseline_requires_neutral_class"
    for bad in ("baseline", "", None, ["raw"]):
        with pytest.raises(train.TrainingError) as exc:
            _fit([good], tmp_path / "m3", feature_mode=bad)
        assert exc.value.code == "invalid_feature_mode"


def test_too_few_neutral_windows_is_refused(tmp_path, monkeypatch):
    sessions = _train_set(tmp_path)
    monkeypatch.setattr(train, "MIN_BASELINE_WINDOWS", 13)  # each fixture neutral has 12
    with pytest.raises(train.TrainingError) as exc:
        _fit(sessions, tmp_path / "model")
    assert exc.value.code == "training_baseline_insufficient"


@pytest.fixture
def model(tmp_path):
    _fit(_train_set(tmp_path), tmp_path / "model")
    return tmp_path / "model"


def test_constant_offset_is_removed_at_evaluation(tmp_path, model):
    plain_cal = _held(tmp_path, "p08cal", "p08", split="calibration", jitter=1)
    plain_eval = _held(tmp_path, "p08eval", "p08", split="eval", jitter=4)
    moved_cal = _held(tmp_path, "p09cal", "p09", split="calibration", jitter=1, shift=SHIFT)
    moved_eval = _held(tmp_path, "p09eval", "p09", split="eval", jitter=4, shift=SHIFT)
    plain = train.evaluate_held_out(model, [plain_eval], calibration=[plain_cal], allow_constructed=True)
    moved = train.evaluate_held_out(model, [moved_eval], calibration=[moved_cal], allow_constructed=True)
    assert plain.feature_mode == moved.feature_mode == "baseline-relative"
    assert plain.calibration_recordings == 1 and plain.examples == 36
    assert plain.confusion == moved.confusion and plain.accuracy == moved.accuracy


def test_baseline_comes_only_from_calibration_neutral_rows(tmp_path, model, monkeypatch):
    cal = _held(tmp_path, "cal", "p09", split="calibration", order=("neutral", "mouth_open"), jitter=1)
    ev = _held(tmp_path, "eval", "p09", split="eval")
    calls = []
    real = train.assert_examples_may_fit

    def spy(rows, scope):
        calls.append((scope.purpose, scope.target_participant_id, {(r.example.split, r.example.label) for r in rows}, len(rows)))
        return real(rows, scope)

    monkeypatch.setattr(train, "assert_examples_may_fit", spy)
    train.evaluate_held_out(model, [ev], calibration=[cal], allow_constructed=True)
    assert calls == [("personalization", "p09", {("calibration", "neutral")}, 12)]


def test_evaluation_never_refits_the_scaler_or_classifier(tmp_path, model, monkeypatch):
    cal = _held(tmp_path, "cal", "p09", split="calibration", jitter=1)
    ev = _held(tmp_path, "eval", "p09", split="eval")
    numpy, sklearn, _lr, _scaler = train._libraries()

    class Refuse:
        def __init__(self, *args, **kwargs):
            raise AssertionError("evaluation must not construct an estimator")

    monkeypatch.setattr(train, "_libraries", lambda: (numpy, sklearn, Refuse, Refuse))
    result = train.evaluate_held_out(model, [ev], calibration=[cal], allow_constructed=True)
    assert result.examples == 36


def test_refused_personalization_scope_scores_nothing(tmp_path, model, monkeypatch):
    cal = _held(tmp_path, "cal", "p09", split="calibration", jitter=1)
    ev = _held(tmp_path, "eval", "p09", split="eval")

    def refuse(rows, scope):
        raise SplitPlanError(("eval_excluded_from_fitting",))

    monkeypatch.setattr(train, "assert_examples_may_fit", refuse)
    with pytest.raises(SplitPlanError):
        train.evaluate_held_out(model, [ev], calibration=[cal], allow_constructed=True)


def test_calibration_and_evaluation_roles_cannot_be_swapped(tmp_path, model):
    cal = _held(tmp_path, "cal", "p09", split="calibration", jitter=1)
    ev = _held(tmp_path, "eval", "p09", split="eval")
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [cal], calibration=[ev], allow_constructed=True)
    assert exc.value.code == "evaluation_requires_held_out_eval_sessions"
    other_eval = _held(tmp_path, "eval2", "p09", split="eval", jitter=6, session="s-eval2b", recording="r-eval2b")
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[other_eval], allow_constructed=True)
    assert exc.value.code == "calibration_session_invalid"


def test_calibration_must_be_a_different_session_and_recording(tmp_path, model):
    ev = _held(tmp_path, "eval", "p09", split="eval")
    same_session = _held(tmp_path, "cal", "p09", split="calibration", jitter=1, session="s-eval")
    with pytest.raises(SplitPlanError):
        train.evaluate_held_out(model, [ev], calibration=[same_session], allow_constructed=True)
    # Same bytes as the eval recording, new ids: refused as calibration/eval source leakage.
    copy = _held(tmp_path, "copy", "p09", split="calibration", order=ALL)
    with pytest.raises(SplitPlanError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[copy], allow_constructed=True)
    assert "calibration_eval_source_leakage" in exc.value.codes


def test_training_participant_cannot_supply_calibration(tmp_path, model):
    ev = _held(tmp_path, "eval", "p09", split="eval")
    cal = _held(tmp_path, "cal", "p01", split="calibration", jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[cal], allow_constructed=True)
    assert exc.value.code == "participant_overlap_with_training"


def test_missing_mismatched_and_conflicting_calibration(tmp_path, model):
    ev = _held(tmp_path, "eval", "p09", split="eval")
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], allow_constructed=True)
    assert exc.value.code == "calibration_required"
    other = _held(tmp_path, "other", "p07", split="calibration", jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[other], allow_constructed=True)
    assert exc.value.code == "calibration_participant_not_evaluated"
    ev2 = _held(tmp_path, "eval7", "p07", split="eval", jitter=6)
    cal9 = _held(tmp_path, "cal9", "p09", split="calibration", jitter=2)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev, ev2], calibration=[cal9], allow_constructed=True)
    assert exc.value.code == "calibration_missing"
    cal9b = _held(tmp_path, "cal9b", "p09", split="calibration", jitter=3)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[cal9, cal9b], allow_constructed=True)
    assert exc.value.code == "conflicting_calibration"
    no_neutral = _held(tmp_path, "nn", "p09", split="calibration", order=("mouth_open",), jitter=4)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(model, [ev], calibration=[no_neutral], allow_constructed=True)
    assert exc.value.code == "calibration_baseline_insufficient"


def test_raw_model_refuses_calibration(tmp_path):
    train.fit_population(_train_set(tmp_path), tmp_path / "raw", allow_constructed=True)
    ev = _held(tmp_path, "eval", "p09", split="eval")
    cal = _held(tmp_path, "cal", "p09", split="calibration", jitter=1)
    with pytest.raises(train.TrainingError) as exc:
        train.evaluate_held_out(tmp_path / "raw", [ev], calibration=[cal], allow_constructed=True)
    assert exc.value.code == "calibration_not_used_by_raw_model"
    assert train.evaluate_held_out(tmp_path / "raw", [ev], allow_constructed=True).feature_mode == "raw"


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda d: d.update(feature_mode="raw"), id="explicit_raw"),
        pytest.param(lambda d: d.update(feature_mode="other"), id="unknown_mode"),
        pytest.param(lambda d: d.update(feature_mode=["baseline-relative"]), id="list_mode"),
        pytest.param(lambda d: d.update(classes=["brow_raise", "mouth_open", "rest"],
                                        training={**d["training"], "class_counts": {"brow_raise": 1, "mouth_open": 1, "rest": 1}}),
                     id="no_neutral_class"),
    ],
)
def test_invalid_feature_mode_artifact_is_refused(model, edit):
    data = json.loads((model / "model.json").read_text())
    edit(data)
    (model / "model.json").write_text(json.dumps(data))
    with pytest.raises(train.TrainingError) as exc:
        train.load_model(model)
    assert exc.value.code == "invalid_model"


def test_cli_baseline_fit_and_evaluate(tmp_path, capsys):
    sessions = _train_set(tmp_path)
    args = ["fit", "--out", str(tmp_path / "m"), "--allow-constructed", "--features", "baseline-relative"]
    for directory in sessions:
        args += ["--session", str(directory)]
    assert train.main(args) == 0
    assert "feature_mode=baseline-relative" in capsys.readouterr().out
    cal = _held(tmp_path, "cal", "p09", split="calibration", jitter=1)
    ev = _held(tmp_path, "eval", "p09", split="eval")
    assert train.main(["evaluate", "--model", str(tmp_path / "m"), "--session", str(ev),
                       "--calibration", str(cal), "--allow-constructed"]) == 0
    out = capsys.readouterr().out
    assert "feature_mode=baseline-relative" in out and "calibration_recordings=1" in out
    assert train.main(["evaluate", "--model", str(tmp_path / "m"), "--session", str(ev), "--allow-constructed"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "calibration_required\n"
    assert train.main(args[:-2] + ["--features", "bogus"]) == 2
    assert capsys.readouterr().err == "operator_input_invalid\n"
    raw = ["fit", "--out", str(tmp_path / "r"), "--allow-constructed", "--session", str(sessions[0])]
    assert train.main(raw) == 0
    assert "feature_mode" not in capsys.readouterr().out
