# Machine learning

Owner: Hardware/ML.

This directory is the phase-1 feature pipeline for the gesture classifier. It consumes Schema 4 camera feature messages from `docs/contracts.md` and can publish a Schema 4 `unknown` classification. It does not publish motor commands, and it does not train a model.

No weights, checkpoints, datasets, recordings, or participant identifiers belong here. Those stay outside the repository, as `.gitignore` and `CONTRIBUTING.md` already require.

## What this phase does

`FeaturePipeline` accepts one camera feature message at a time.

1. `ml.schema` checks the message against the Schema 4 camera-feature rules, including schema `"4"`, the 52 blendshape names, head-pose geometry, timestamps, and `confidence: null`.
2. `ml.windows` puts accepted messages into a window. A rejected message is reported and is not inserted.
3. For an actionable window, `ml.features` builds one fixed vector. A window that contains any non-actionable frame produces no vector.
4. `ml.classify` emits `label: "unknown"` for that non-actionable window, with `model_version: "none"` and confidence `0`. An actionable window gets no label until a later phase has a trained model.

`ml.records` holds the data shapes a later trainer can reuse: labeled examples, train/eval/calibration splits, per-user normalization parameters, disabled labels, and a rejection threshold. Nothing in that module fits parameters or loads weights.

## Window policy

- `length` is the number of accepted frames in a window. `stride` is how many accepted frames to move forward after one. Both are integers of at least 1. A one-frame window uses `length=1`.
- Window starts are exactly `stride` accepted frames apart. With `stride < length` windows overlap, with `stride == length` they tile, and with `stride > length` the `stride - length` accepted frames after each window are skipped. For `length=2, stride=3`, frames 0 to 7 give `[0, 1]`, `[3, 4]`, `[6, 7]`. A skipped frame is still validated and still sets the newest accepted timestamp, so a later frame must be newer than it. It is in no window and is not counted.
- Timestamps must increase. An equal or older timestamp is rejected. The frames already accepted stay in the buffer.
- A message that fails validation is rejected the same way. It is not dropped silently and it is not turned into a guessed frame.
- Gaps are kept as gaps. The frame count is the number of accepted messages, not the elapsed time divided by a frame period.
- A window is actionable only when every frame is a valid `tracking` frame. One `partial` or `lost` frame makes the whole window non-actionable.

## Head-pose geometry

`ml.schema` and the oracle in `tests/contract_schema.py` apply the Schema 4 rule that the upper-left 3×3 is a uniform scale times a rotation:

- The contract's 1 percent column-norm rule is unchanged.
- After scale is removed, each pair of column directions must have |cos| at most `1e-3` (about 0.057 degrees from a right angle), and their determinant must be at least `1 - 1e-3`. This rejects reflections, singular matrices, and skewed columns whose norms agree. Schema 4 gives no number for these, so they are implementation tolerances. A MediaPipe transform is a scaled orthogonal fit and sits far inside them.
- The computation divides the 3×3 by its largest absolute entry first, so poses at scales such as `1e200` or `1e-200` do not fail because a square overflowed or underflowed. Subnormal entries (below about `2.2e-308`) lose precision and may be rejected. That fails closed.
- Both the angle extracted from the matrix and the published angle must lie in the contract range. The `0.1` degree match between them does not widen the range.
- Exactly at a range boundary, the extracted angle can round a little past the limit, especially at extreme scales, and the pose is then rejected.

## Feature vector

The vector length is 162 and does not change between sessions.

Channels, in order:

1. The 51 blendshapes other than `_neutral`, in Face Landmarker order, from `browDownLeft` through `noseSneerRight`.
2. `yaw_deg`, `pitch_deg`, `roll_deg`.

Each channel contributes three numbers, in this order: arithmetic mean, population standard deviation, newest minus oldest. The deviation divides by `N`, so a one-frame window has deviation 0. These are summaries of the window only. They are not normalization parameters, and they are not fit on a training set.

`_neutral` is stored on the camera message and is left out of the vector. The names are `ml.features.FEATURE_NAMES`.

## Example

The numbers below are constructed. They are not a recording.

```python
from ml import FeaturePipeline

frame = {
    "schema_version": "4",
    "timestamp_ms": 1795276800000,
    "tracking_state": "lost",
    "features": {},
    "head_pose": None,
    "confidence": None,
}
result = FeaturePipeline(length=1, stride=1).push(frame, produced_at_ms=1795276800040)
message = result.product.classification
# label is "unknown", model_version is "none", and the message has no motor fields
```

## Later training

`LabeledExample` keeps a participant token, a session token, a split, an annotation, and a vector of length 162. Tokens are opaque. They are not names or email addresses.

`NormalizationSpec` can be built from `train` or `calibration` only. `apply_normalization` uses the supplied mean and scale and does not estimate them. If any result would overflow to infinity or NaN, it raises `ValueError` instead. `LabeledExample` stores its own validated tuple of 162 finite floats, so changing the caller's list afterwards has no effect. `PersonalizedModelRef` can name a future `model_version`, list disabled gesture labels, and store a rejection threshold in `(0, 1]`. `weights_present` cannot be true in this phase.

## Dataset foundation

`ml.dataset` prepares metadata and local Schema 4 JSONL for a later trainer. It does not train, and it does not download an external dataset. The input line is the object `perception` already writes: one compact JSON feature message per line.

`DatasetSource` records a dataset id, a category (`consented_local`, `authorized_public`, or `constructed_fixture`), the source format and version, an extraction version, and an `AuthorizationRecord`. The authorization status is a human decision id. `IngestResult.authorization_is_proof` is always false. A constructed fixture must use authorization `constructed` and timestamp provenance `constructed`. Calling those numbers a measured capture is rejected. `reconstructed_offline` keeps the file's numbers as a timeline and sets `timestamps_are_measured_capture` to false.

`GestureAnnotation` is either an explicit half-open interval `[start_ms, end_ms)` or a clip-level label. A clip-level label does not know its onset and offset, so ingestion returns `clip_level_label_without_interval` and creates no example from it. `neutral` is a valid annotation when a person marked that interval. `unknown` is rejected, because it is a classifier outcome rather than an annotation. Frames that fall in no interval stay unlabeled. Overlapping intervals fail. Each interval is windowed by its own `WindowAssembler`, so a window cannot cross participants, sessions, clips, or annotated intervals. Rejected lines are counted by line number and reason code. They are not inserted, not interpolated, and not turned into examples. The reason does not include the JSON line.

`validate_splits` checks the plan described in `SplitReport.enforced`. Population participants stay in `train`. Held-out participants may have `calibration` and `eval` only, and those two splits must come from different sessions. A recording id has one participant, one session, and one split, and every window from that recording inherits it. An invalid plan raises `SplitPlanError` with sorted reason codes. Nothing here estimates a mean, chooses a parameter, or runs cross-validation. `SplitReport.not_enforced` lists the decisions a later experiment protocol still has to make.

### Fitting scopes

A later trainer may read rows for only one of these uses at a time:

1. **Population fitting** (global normalization, a global model, or global parameter selection) reads only `train` examples of `population_train` participants. No calibration row, no eval row, and no held-out participant contributes.
2. **Personalized fitting** adapts to one named held-out participant and reads only that participant's `calibration` examples. It refuses another participant's calibration, every `train` row, and every `eval` row. Its result is never a source for population-level model or parameter selection.
3. **Evaluation** is never a fitting source for either purpose.

The context comes from a checked plan, not from the rows' split strings:

```python
plan = validate_split_plan(roles, sessions, recordings, examples)  # raises SplitPlanError
population = fitting_scope(plan, "population")
personal = fitting_scope(plan, "personalization", "p02")  # p02 must be held_out in plan
decision = assert_examples_may_fit(rows, population)  # FittingAuthorization, or SplitPlanError
```

- `validate_split_plan` runs `validate_splits` and returns a `ValidatedSplitPlan` that keeps its own copies of the roles, the recording assignments, and the checked examples. Changing the caller's lists afterwards changes nothing. A `ValidatedSplitPlan` built directly or copied with `dataclasses.replace` is not validated, and `fitting_scope` refuses it with `unvalidated_split_plan`.
- `fitting_scope` raises `DatasetError` with one of `invalid_fitting_purpose`, `population_fit_takes_no_target`, `personalization_target_required`, `invalid_participant_id`, `personalization_target_not_in_plan`, or `personalization_target_not_held_out`.
- `assert_examples_may_fit(rows, scope)` checks every row against the plan on its own, so it does not trust `normalization_candidates`. A row must be one of the plan's checked examples (`example_not_in_plan`) and must match its recording assignment (`example_recording_mismatch`). Other refusals are `eval_excluded_from_fitting`, `calibration_excluded_from_population_fit`, `held_out_participant_excluded_from_population_fit`, `other_participant_excluded_from_personalization`, `train_excluded_from_personalization`, and `population_participant_excluded_from_personalization`. A missing scope is `fitting_scope_required`, a scope of the wrong type is `invalid_fitting_scope`, an empty request is `empty_fitting_request`, and a container that is not a sequence (a string, bytes, a dict, a set, or a generator) is `invalid_fitting_request`. All of these are `SplitPlanError` with sorted codes. A row that is not a `PreparedExample` raises `DatasetError("invalid_example")`. On success it returns a frozen `FittingAuthorization` with its own tuple of rows, in input order.
- `normalization_candidates(rows, scope)` returns the rows that scope would allow, in input order. Without a scope it returns only rows whose split is `train`. That pre-filter uses the split string alone and approves nothing. The assertion still has to be called with a scope.

These functions only decide which rows may be read. They do not stop a trainer that never calls them, and they do not detect a plan whose fields were changed with `object.__setattr__` after it was validated.

### Decoding

Each file is read as bytes and split on `\n`, `\r\n`, or `\r`, the same line breaks the earlier text reader used, so line numbers are unchanged. Each line is then decoded as strict UTF-8. A line that is not valid UTF-8 is rejected as `invalid_encoding` with its line number. It is not decoded with replacement characters, and the bytes are not kept. Blank lines are skipped as before. JSON nesting deep enough to make the decoder raise `RecursionError` is rejected as `json_depth_exceeded`. Later lines are still read in both cases. Duplicate keys, `NaN` and `Infinity`, non-object values, the 10000-node limit after decoding, and Schema 4 validation are unchanged.

This is not a memory limit. A very long line is still read into memory whole before it is decoded.

### Known limitations, unchanged in this phase

- A symlink given directly as the path is refused. A path through a symlinked parent directory is followed.
- An unhashable value, such as a list, in an enumerated metadata field (`status`, `source_category`, `timestamp_provenance`, `role`, `split`, `granularity`) raises `TypeError` instead of `DatasetError`.
- `IngestResult` and `IngestCounts` built by hand accept negative counts, and keep a list the caller passed instead of copying it. `ingest_recording` itself returns tuples and counts it computed.
- A clip-level label is reported for review even when interval labels on the same recording disagree with it. It does not block the interval examples.

Public research collections are notes in `ml/sources.md`. Their files are not Schema 4, and this tree has no adapter for them.

## Tests

From the repository root:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m pytest tests
```

`tests/test_ml_pipeline.py` and `tests/test_ml_dataset.py` build their own messages. A passing run does not mean a model was trained or that a person was recorded.
