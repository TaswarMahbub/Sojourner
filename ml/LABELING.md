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

A held-out participant's calibration session and evaluation session must be different sessions, and different recordings. Check several session directories together without reading their frames:

```text
python -m ml.label check captures/CALIBRATION captures/EVAL
```

`check` runs `validate_split_plan` on the declared ids, and it also compares the recordings themselves. Two sessions use the same source when their recording paths resolve to the same file, when the files share a device and inode (a hard link), or when the SHA-256 and length stored at `init` are equal (a copy). Any of these fails with `recording_source_reused` or `recording_content_duplicated`. Reuse across different splits adds `recording_source_reused_across_splits`, and calibration against eval adds `calibration_eval_source_leakage`. Byte-identical files are refused even if they really were recorded separately, because nothing in the file can show that. Unique recording ids do not prove separate collection. `check` does not re-hash the files, so it compares the fingerprints taken at `init`. A future trainer still has to audit the complete example set it fits on, with the ML-2A fitting scopes.

Prepare reads the recording once. It copies the file into a private snapshot (a new 0400 file in a 0700 directory under the system temporary directory, never a hard link) and hashes the copy. The fingerprint check, the counts, `ingest_recording`, and the report all use that copy. After ingestion the copy and the original path are hashed again. If either changed, the report is blocked with `snapshot_changed` or `recording_changed_during_prepare` and no example is emitted. The snapshot is removed on every exit path. A crash can leave it behind in the temporary directory.

A confirmed interval whose start is before the first accepted frame, or whose end is after `last_timestamp_ms + 1`, adds the review reason `interval_boundary_not_observed`. Its onset or offset there was not observed. The windows still contain only real accepted frames, and no frame period is assumed. This is a review reason, not a block. At the safe-integer ceiling `9007199254740991` an end cannot be later than `last + 1`, so the reason cannot appear on that side.

## Withdrawal

Discard deletes the marker, `session.json`, `annotations.json`, and `quality-report.json`. It does not delete an unexpected file in that directory.

Every target is checked before anything is deleted. A generated name that is a symlink or not a regular file stops the discard. With `--include-recording`, the recording is deleted only if all of these hold:

- `session.json.recording_path` names a file directly inside the session directory.
- The session has a `recording_identity`, written at `init` from the same open file that was fingerprinted: the file name, the device, and the inode.
- The path's file name equals the bound name.
- That file is a regular file, not a symlink, has one hard link, and is not named like a generated file.
- Its device and inode equal the bound ones, checked on the path and again on the opened descriptor.
- Its SHA-256 and byte length equal the values stored at `init`.

Otherwise discard stops with a code (`recording_identity_mismatch`, `recording_identity_unavailable`, `symlink_not_allowed`, `recording_has_other_links`, or `recording_unreadable`) and deletes nothing. A missing recording at the bound name is treated as already deleted. A byte-identical copy, under another name or put in place of the original, has a different inode and is refused. So is the original file after it was moved to another volume or restored from a backup, because its inode changed. Pointing a session at another recording is not supported in this phase. Run `init --replace` to bind the current file, which starts a new set of annotations. A session written before `recording_identity` existed can still be prepared and discarded, but its recording is never deleted by `--include-recording`.

The recording is deleted first. If that fails, the session is left as it was (`recording_delete_failed`) and the same command can be retried. The generated files go in the order `quality-report.json`, `annotations.json`, `session.json`, marker. If one of those fails (`discard_incomplete`), a retry accepts the partial session and finishes. A marker left without `session.json` can be removed with plain `discard`. With `--include-recording` it fails with `recording_identity_unavailable`, because the recording can no longer be identified.

```text
python -m ml.label discard captures/SESSION
```

Add `--include-recording` only when the JSONL file is inside that session directory and is the file named in `session.json`. A recording that lives somewhere else is left in place.

```text
python -m ml.label discard captures/SESSION --include-recording
```

Do not commit the recording, the session directory, or the quality report.

## File safety and metadata

**Writes.** Each file is written to a new temporary file with a unique name, created exclusively with mode 0600 in the same directory, then flushed, synced, closed, and renamed over the destination with `os.replace`. A planted file or symlink at a predictable temporary name is never opened. A destination that is a symlink or not a regular file is refused. On failure only that call's temporary file is removed and the old file stays.

**Replacing a session.** `init --replace` writes the new marker, `session.json`, and `annotations.json` into a private `.label-stage-*` directory inside the session directory, reads them back, and validates them. Only then are the existing files moved into a `.label-backup-*` directory and the new ones moved in, with the marker last. If any step fails, the files moved in are moved back out, the previous files are moved back, and both private directories are removed. The old `quality-report.json` is kept on failure and dropped on success. Codes:

- `output_write_failed`: the replacement failed and the previous session is back.
- `replacement_cleanup_incomplete`: the outcome is complete (the previous session after a failure, or the new one after success), and a private directory is left over. Remove it by hand. Until then `init` reports `directory_not_empty`, and if the leftover is a `.label-backup-*` directory that still holds the old files, every command reports `replacement_recovery_pending`. After a successful replacement those files are the old generation and can be deleted.
- `replacement_rollback_incomplete`: a rename back failed. Every previous file that could not be moved back is still in the `.label-backup-*` directory. Nothing was deleted.

After `replacement_rollback_incomplete` the session is never usable as it stands:

- The previous marker is moved back only when no file of the new generation is left in the session directory. If a new file could not be moved out, the marker stays in the backup and the directory does not load (`not_a_label_session`). Old confirmed annotations can therefore never be read beside new metadata.
- Separately, every command refuses a session directory whose `.label-backup-*` directory still holds anything, with `replacement_recovery_pending`. This holds even if a marker is present. An empty backup directory is only a cleanup leftover and does not block.

To recover the previous session by hand:

1. Do not delete the `.label-backup-*` directory or anything in it.
2. Move each file in it back into the session directory, replacing a file of the same name. Every name in the backup belongs to the previous session, so this also removes any new file left behind.
3. Remove the now empty backup directory, and any `.label-stage-*` directory (it holds only new files).
4. Run `overview` and `prepare` to check the restored session.

These are process-level guarantees. A power loss or a killed process during the swap can leave a mix of files or a private directory behind. Each rename is atomic on a local file system and the marker goes in last, so an interrupted first `init` does not load as a session. The tool does not claim full crash durability.

**Source collisions.** The recording cannot be a generated file. `init` refuses a recording in the session directory whose name, compared case-insensitively, is a generated name, a `.label-stage-` or `.label-backup-` name, or a dot-name ending in `.tmp` (`recording_collides_with_session_file`). It also compares device and inode with every existing generated file, which catches a hard link or a symlinked directory alias. The check runs before any file is created or moved.

**Loading.** The marker must be exactly `sojourner-label-session-v1` and a newline. `session.json` and `annotations.json` are read as strict UTF-8 JSON: duplicate keys at any depth, `NaN`, `Infinity`, nesting deep enough to exhaust the decoder, and files over 4 MiB are refused. Every field is then checked for its exact type and value before it is used: ids and decision ids, the recording path (absolute), enumerated values (checked as strings first), reviewed labels, window length and stride, conditions, the recording summary, the SHA-256, the split history, and every interval row. The capture claim, category, authorization status, and timestamp provenance must agree. A constructed fixture goes only with `constructed` for all three, and an operator camera claim goes only with `consented_local`, `human_authorized`, and a measured or reconstructed timeline. Annotation ids must equal the session's. This happens before every command, including discard, so a hand-edited file cannot turn a candidate into a confirmed example.

Kept as before: a split or role that the ML-2A plan rejects still produces a blocked report, and an annotation provenance that differs from the session still blocks prepare. `source_format` and `source_version` are checked for type and shape only, because ingestion always uses the Schema 4 feature format.

These checks are internal consistency, not proof. A person who can rewrite every local file, including the stored SHA-256, can make a consistent false session. Split history is a local record that can be edited, not a tamper-evident log. Parent directories that are symlinks are still followed.

**Special files.** Metadata and recordings are opened with `O_NONBLOCK` and `O_NOFOLLOW`, after an `lstat` check, and the descriptor is checked with `fstat` before anything is read. A FIFO or device in place of the marker, `session.json`, or `annotations.json` is refused at once (`not_a_label_session` for the marker, `metadata_unreadable` otherwise) instead of waiting for a writer.

**Snapshots.** If the private snapshot directory cannot be created, the command stops with `snapshot_unavailable` and changes nothing. If deleting the snapshot copy fails after a successful prepare, the report is still returned, and the copy stays in its 0700 directory under the system temporary directory until it is removed by hand.

**CLI errors.** Rejected input prints one code to stderr and exits 2, with nothing on stdout. No traceback, recording content, system path, or private string is printed. A programming error is not caught and still raises.
