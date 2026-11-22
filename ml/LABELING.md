# Local gesture labeling

This is a research-prototype procedure for reviewing intervals on a recording that already exists. It is not a medical-device evaluation, and it does not say how many people or minutes are enough to train a model.

The tool records what an operator claims. It cannot prove identity, permission, or that a movement happened. A keyboard prompt or a spoken cue is only a candidate until the operator confirms the onset, the offset, and the label.

## Before recording

1. Confirm permission with the participant before the camera starts. Keep the consent record outside this repository. The session stores only an opaque decision id.
2. Say which opaque participant id and session id this recording belongs to.
3. Note camera and room conditions that affect feature quality, such as light and camera distance. Do not write a name, email, or other identifying detail into that note.
4. Choose the split before recording. Population training stays `train`. A held-out participant uses separate sessions for `calibration` and `eval`. Do not put those two jobs in one session.

Record with the existing publisher. This repository does not contain another camera path.

```text
python -m perception.publish --source camera --record captures/SESSION/features.jsonl
```

`--source camera` is a live camera. `--source fake` is a constructed stream. A fake recording must be initialized as `constructed_fixture` with timestamp provenance `constructed`. Do not mark it as a measured capture.

`python -m perception.calibrate` writes a different JSONL file, with `record_type` wrappers. Do not pass that file to this tool.

Stop the publisher with Ctrl+C. The JSONL file is one Schema 4 feature object per line. Leave it out of git. `captures/` and `datasets/` are ignored.

## Review

Initialize a session directory next to the recording. The command reads the file and writes metadata. It does not modify the recording.

```text
python -m ml.label init \
  --recording captures/SESSION/features.jsonl \
  --out captures/SESSION \
  --dataset ds01 \
  --participant p01 \
  --session s01 \
  --recording-id r01 \
  --split train \
  --role population_train \
  --category consented_local \
  --provenance measured_capture \
  --claim operator_claims_camera \
  --decision decision-1 \
  --conditions "overhead light, camera at arm length"
```

For a fake-stream practice file, use `--category constructed_fixture --provenance constructed --claim constructed_fixture`.

```text
python -m ml.label overview captures/SESSION
python -m ml.label labels
```

The overview lists accepted and rejected frames and the first and last accepted `timestamp_ms`. Those are the only timeline. Do not convert a frame index into time with an assumed 30 FPS rate.

Candidate labels are `neutral`, `brow_raise`, `deliberate_blink`, `mouth_open`, `head_left`, `head_right`, and `select`. They are annotation names. They are not motor commands. A participant can use a subset with `--labels neutral,mouth_open`. An added name needs `--extra-label` and must not be `unknown`.

Mark explicit half-open intervals, `[start_ms, end_ms)`. The frame at `end_ms` belongs to the next interval.

```text
python -m ml.label mark captures/SESSION --start START --end END --label neutral
python -m ml.label confirm captures/SESSION --index 0
```

A prompted attempt:

```text
python -m ml.label mark captures/SESSION --start START --end END --label mouth_open --prompted
```

That row stays a candidate. Confirm it only after looking at the boundaries. The prompted flag remains, so the report still says the mark began as a prompt.

If the boundary is not reliable, leave the range uncertain. Uncertain rows are not training examples.

```text
python -m ml.label uncertain captures/SESSION --index 1
```

Correcting an interval returns it to candidate until it is confirmed again. Remove a row by index.

```text
python -m ml.label correct captures/SESSION --index 0 --end NEW_END
python -m ml.label remove captures/SESSION --index 2
```

A clip-level label has no onset or offset. It is stored for review and is not applied to windows. If a clip label and an interval are both present, prepare blocks until one of them is cleared.

```text
python -m ml.label clip captures/SESSION --label mouth_open
python -m ml.label clear-clip captures/SESSION
```

## What to collect

- Begin with rest that the operator labels `neutral`. Tracking by itself is not neutral.
- Ask for one deliberate gesture at a time, with rest between attempts.
- Skip any movement the participant does not want to perform.
- When an attempt is missed, incomplete, or ambiguous, mark it uncertain or leave it unlabeled. Do not confirm it as a successful gesture.
- An ordinary blink is not `deliberate_blink` unless the operator confirms that this interval was the deliberate attempt.
- Periods between gestures stay unlabeled. The tool will not fill them with `neutral`.

## Prepare and splits

```text
python -m ml.label prepare captures/SESSION
```

Prepare validates the file with the ML-2A reader, passes only confirmed intervals to `ingest_recording`, and checks `validate_split_plan`. The quality report lists counts: accepted and rejected frames, intervals per label, unlabeled frames, tracking loss, review reasons, blocks, and how many 162-number examples were produced. It does not list feature values, and it does not report accuracy.

Changing a recording from `train` to `calibration` or `eval` requires an explicit review, and the ML-2A plan can still reject it.

```text
python -m ml.label reclassify captures/SESSION --split eval --reviewed
```

A held-out participant's calibration session and evaluation session must be different sessions. Check several session directories together without reading their frames:

```text
python -m ml.label check captures/CALIBRATION captures/EVAL
```

## Withdrawal

Discard deletes the marker, `session.json`, `annotations.json`, and `quality-report.json`. It does not delete an unexpected file in that directory.

```text
python -m ml.label discard captures/SESSION
```

Add `--include-recording` only when the JSONL file is inside that session directory and is the file named in `session.json`. A recording that lives somewhere else is left in place.

```text
python -m ml.label discard captures/SESSION --include-recording
```

Do not commit the recording, the session directory, or the quality report.
