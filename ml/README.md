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

## Tests

From the repository root:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m pytest tests
```

`tests/test_ml_pipeline.py` builds its own messages. A passing run does not mean a model was trained or that a person was recorded.
