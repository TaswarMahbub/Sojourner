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

The model is first written and fsynced as `.model.json.partial`, read back and compared byte for byte, then renamed to `model.json` and loaded once with `load_model`. If any step fails (open, write, flush, fsync, rename, or verification), every file the fit created is removed, then the directory, and the fit reports `output_write_failed`. A directory that still holds `.model.json.partial` is refused with `model_publication_incomplete`. These are process-level guarantees, not power-loss durability.

It is JSON, not a pickle, so loading it runs no code. `load_model` reads it as strict JSON (no duplicate keys, `NaN`, or `Infinity`) and checks every field before use: class names, finite numbers (an integer too large for a float is refused), training recording ids, SHA-256, and lengths, timestamp provenance, class counts, and the window settings. Any problem is `invalid_model`. The model weights are still derived from participant data: keep the directory local, and do not commit, upload, or share it.

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

Personalization (fitting on a held-out person's `calibration` session) is not implemented. The baseline-relative mode below reads a calibration session, but it fits nothing on it: it only subtracts one mean vector.

## Baseline-relative mode (ML-4A, experimental)

People differ in their resting face and head pose. One person's neutral brow height can exceed another person's brow raise, and the raw model then calls a resting face `brow_raise`. This mode removes each session's resting offset before the population model sees the vector.

```text
python -m ml.train fit --features baseline-relative --session captures/TRAIN_A [--session ...] --out captures/models/ml4a-...
python -m ml.train evaluate --model captures/models/ml4a-... \
  --session captures/EVAL_P05 --calibration captures/CAL_P05 [--session ... --calibration ...]
```

- **Baseline.** The mean 162-number vector of a session's confirmed `neutral` windows. At least `MIN_BASELINE_WINDOWS` (10) windows are required. That floor was fixed before any recording was seen.
- **Training.** Each `train` session gets its own baseline, from its own authorized neutral rows, subtracted from every row of that session. Two sessions of one person get two baselines. Every row, neutral rows included, has already passed `assert_examples_may_fit` with the population scope. Then the same `StandardScaler` and the same fixed logistic regression are fit. A session without enough neutral windows is refused (`training_baseline_insufficient`), and so is a class list without `neutral` (`baseline_requires_neutral_class`).
- **Evaluation.** Each evaluated participant needs exactly one `--calibration` session. Its baseline is the mean of that session's confirmed `neutral` rows, after `assert_examples_may_fit` with `fitting_scope(plan, "personalization", participant)`. No eval row or label is read to build it, and the population scaler and classifier are never refit. The baseline is subtracted from that participant's eval rows, then the frozen model scores them.
- **Refusals.** These are in addition to every raw-mode evaluation rule:
  - no calibration: `calibration_required`;
  - a calibration participant who is not evaluated: `calibration_participant_not_evaluated`;
  - an evaluated participant without one: `calibration_missing`;
  - two calibrations for one person: `conflicting_calibration`;
  - a session that is not `calibration`/`held_out`: `calibration_session_invalid`;
  - too few neutral windows: `calibration_baseline_insufficient`;
  - a shared session or recording id: `calibration_evaluation_overlap`, or the ML-2A split rules;
  - a byte-identical recording: `calibration_eval_source_leakage`;
  - a raw model given a calibration: `calibration_not_used_by_raw_model`.
- **Artifact.** A baseline-relative `model.json` has `"feature_mode": "baseline-relative"`. A file without that key is a raw ML-3 model, and raw artifacts are byte-identical to ML-3. The baselines themselves are not stored, because each evaluation computes its own.

### Calibration protocol for a held-out participant

Record two separate sessions with the same consent, camera, and room. They must use different `--session` and `--recording-id` values and different recording files.

1. **Calibration session.** About 15 s of relaxed rest, face toward the camera as during use. Mark that rest `neutral` and confirm it. Mark nothing else.

   ```text
   python -m ml.label init --recording captures/CAL/features.jsonl --out captures/CAL \
     --dataset ds01 --participant p05 --session s-cal --recording-id r-cal \
     --split calibration --role held_out --category consented_local \
     --provenance measured_capture --claim operator_claims_camera --decision DECISION
   ```

2. **Evaluation session.** The usual protocol, recorded separately, with `--split eval --role held_out` and new session and recording ids.

Training sessions should start with a dedicated rest of at least 10 s marked `neutral`, so each one has its own baseline.

Mark every interval on the movement you observed, not on the spoken cue. A late or short attempt is marked where it happened, or left uncertain.

### Evaluation discipline

p02 and p04 were examined while this mode was designed. Their results under it are not an unbiased estimate of improvement. Judge this mode against raw on new held-out participants, with success criteria written down before their data is scored.
