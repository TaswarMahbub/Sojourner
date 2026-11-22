# Offline gesture baseline (ML-3)

`python -m ml.train` fits one small population classifier on reviewed label sessions and, when real held-out data exists, scores it. It is a research baseline. It is not connected to `FeaturePipeline`, it does not publish a gesture message, and it never builds or sends a motor command.

Install the dependency with `python -m pip install -r requirements.txt` (scikit-learn, which brings numpy and scipy). `import ml` does not load it.

## Model

- Input: the 162-number ML-1 vectors (`ml.features.FEATURE_NAMES`) of confirmed intervals, exactly as `python -m ml.label prepare` counts them.
- `StandardScaler`, fit on the training rows only.
- `LogisticRegression` with fixed settings: `C=1.0`, L2 penalty (the library default), `class_weight="balanced"`, `solver="lbfgs"`, `max_iter=1000`, `tol=1e-4`.
- Nothing is tuned. No hyperparameter is chosen on any data, so no row is used for selection.
- The result is deterministic: the same sessions, in any order, give a byte-identical `model.json`.

## Training

```text
python -m ml.train fit \
  --session captures/SESSION_A [--session captures/SESSION_B ...] \
  --out captures/models/ml3-YYYYMMDD-HHMMSS \
  [--classes neutral,mouth_open,brow_raise]
```

The steps, in order. Any refusal prints one code to stderr, exits 2, and writes nothing.

1. `--out` must be a local path that does not exist yet, whose parent does exist. Inside this repository it must be under `captures/` or `datasets/`. A symlink is refused.
2. `check_collection` validates the sessions together: declared ids, and reuse of one recording by path, inode, or SHA-256 and length.
3. Each session goes through `load_session_examples`. It runs the same checks as `prepare`, including the private snapshot, the SHA-256 and timeline match against `init`, and the session's own split plan. It returns the examples in memory and writes nothing, not even `quality-report.json`. A blocked session refuses with its block code.
4. Every timeline must be `measured_capture`. A `constructed_fixture` set needs `--allow-constructed`, and measured and constructed sessions cannot be mixed.
5. Every session must be `split=train` with `participant_role=population_train`. A calibration or eval session is refused, not filtered.
6. All examples go through one `validate_split_plan`, then `fitting_scope(plan, "population")`, then `assert_examples_may_fit`. The tuple it returns is exactly the set of rows that fit the scaler and the model.
7. Each vector must be 162 finite numbers. Each label must be in `--classes`, and every class must be present (`missing_class`, `label_not_in_classes`).

The output reports examples and intervals per class, the settings, the iteration count, and `training_resubstitution_accuracy`. That is the accuracy on the rows the model was fit on. It says nothing about new data, and the output also prints `held_out_evaluation=none`.

## Artifact

`--out` is created with mode 0700 and holds two files, both 0600:

- `.gitignore` containing `*`, so the directory stays out of git wherever it is placed.
- `model.json`: classes, the 162 feature names, scaler mean and scale, coefficients, intercepts, settings, library versions, window settings, and the training recordings' opaque ids, SHA-256, and length. It contains no feature vector, timestamp, or annotation.

It is JSON, not a pickle, so loading it runs no code. `load_model` reads it as strict JSON (no duplicate keys, `NaN`, or `Infinity`) and checks every field's shape before use. The model weights are still derived from participant data: keep the directory local, and do not commit, upload, or share it.

## Evaluation

```text
python -m ml.train evaluate --model captures/models/ml3-... --session captures/EVAL_SESSION [...]
```

This reports window accuracy, per-class support and correct counts, and a confusion table only when every session is genuinely held out:

- `split=eval` and `participant_role=held_out` (`evaluation_requires_held_out_eval_sessions`);
- a participant who is not among the training recordings (`participant_overlap_with_training`);
- a recording whose id, or SHA-256 and length, is not a training recording (`recording_overlap_with_training`);
- the same timestamp provenance and window settings as training.

Windows from one recording are never divided between training and evaluation. There is no random split, and there is no cross-validation. Overlapping windows from one recording are strongly correlated, so a window count is not a count of independent attempts.

## What valid evaluation needs

A training session alone cannot be evaluated, because its windows were fit. Valid held-out evaluation needs, at minimum:

- one or more other people, recorded with consent and labeled the same way, initialized as `--split eval --role held_out`, each with all classes the model uses;
- separate recordings, never a copy or re-label of a training recording;
- ideally several held-out participants and sessions on different days, so one person's or one session's quirks do not decide the number.

Personalization (fitting on a held-out person's `calibration` session) is not implemented in this phase.
