# Perception

Owner: Mridhul.

This directory holds the MediaPipe Face Landmarker integration that publishes the camera feature stream, message 1 of `docs/contracts.md`.

Responsibilities:

- Capture camera frames and record `timestamp_ms` in Unix epoch milliseconds, UTC.
- Run Face Landmarker for one face, with blendshapes and the facial transformation matrix enabled.
- Publish subject-relative blendshape names, head pose, and tracking state. Publish `confidence: null` unless the Face Landmarker result object actually contains a per-frame score. Do not copy the detection, presence, or tracking option thresholds into that field.
- Emit `lost` or `partial` when the face, the blendshapes, or the pose do not meet schema 4. Those states are not actionable.

Raw 478-point landmarks are not part of schema 4. They can be used inside this directory later. They are not written to git as datasets or recordings.

## Publishing

One entry point serves both sources on the same websocket, in the same message shape:

```text
python -m pip install -r perception/requirements.txt
python -m perception.publish --source camera
python -m perception.publish --source fake
```

**The camera source produces live measured frames.** Every message describes a real camera image that Face Landmarker processed. **The fake source does not.** Its numbers are generated, it uses no camera, and it is not recorded human data. Consumers cannot tell the two apart by message shape, so check which source is running before you treat a stream as measurement.

Frames are served on `ws://127.0.0.1:8765`, one compact JSON text message per frame, to every connected client. A client may connect and disconnect at any time. Stop the publisher with Ctrl+C.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--source {fake,camera}` | `fake` | Which source to publish |
| `--port N` | `8765` | Websocket port on 127.0.0.1 |
| `--record PATH` | none | Also write every published frame to `PATH`, one JSON object per line. Camera recordings are human data: keep them out of git |
| `--print-frames` | off | Print one line per frame: `timestamp_ms`, state, feature count, yaw/pitch/roll, confidence, the contract validator's errors, and for a non-tracking frame the checks that rejected it |
| `--frames N` | unlimited | Stop after `N` frames |
| `--rate HZ` | `30.0` | Fake source only. Frames per second |
| `--seed N` | `11` | Fake source only. Random seed |
| `--camera INDEX` | `0` | Camera source only. Device index |
| `--mirrored` | off | Camera source only. Use when the camera driver delivers a mirrored image. Swaps every `*Left` blendshape name with its `*Right` counterpart, and conjugates the matrix by `diag(-1, 1, 1)` so translation x, yaw and roll regain their unmirrored signs while y, z and pitch are unchanged. Angles are re-extracted from the corrected matrix. Prefer an unmirrored camera feed; this flag exists for drivers that cannot provide one |

### Camera source

`mediapipe_source.py` is the only file that opens the camera or loads the model. It imports `mediapipe` and `cv2` inside its functions, so nothing under `tests/` needs them.

- Camera: 640x480. On Windows it uses the DirectShow backend (`cv2.CAP_DSHOW`) because the default backend is slow to start.
- Model: `perception/models/face_landmarker.task`, resolved relative to the repository, so the command works from any directory. The `.task` file is not in git (see `.gitignore`); download Google's Face Landmarker model to that path.
- Detector: Face Landmarker in VIDEO mode, one face, blendshapes and the facial transformation matrix enabled. Detection, presence and tracking thresholds are left at their library defaults and are never published as `confidence`.
- The image passed to the detector is the camera image as captured. This program never mirrors it, and there is no preview window. If the camera driver itself mirrors the image, pass `--mirrored`.
- `timestamp_ms` is wall-clock Unix epoch milliseconds, UTC, taken as soon as the camera read returns. The detector gets separate, strictly increasing monotonic milliseconds, because `detect_for_video` raises on a repeated or decreasing timestamp.
- The frame rate is whatever the camera and detector sustain, about 20 frames per second on the development laptop. It is not paced to a fixed rate.
- A camera that cannot be opened, a missing model, or a missing package exits with a one-line message and status 1, not a traceback.

### Tracking state

Both sources build every message through `frame_builder.make_feature_message(timestamp_ms, face_count, blendshapes, matrix)`, which takes plain Python data and applies the contract's table:

- No face, or more than one: `lost`, `features` `{}`, `head_pose` null.
- One face, but blendshapes incomplete, unknown, or outside `[0, 1]`, or a pose that fails any rule (last row exactly `[0, 0, 0, 1]`; column norms within 1 percent of their mean; yaw `[-90, 90]`, pitch `[-60, 60]`, roll `[-45, 45]` degrees; x and y `[-100, 100]`, z `[-200, -5]` cm): `partial`, `head_pose` null. `features` keeps only the contract names whose scores are in `[0, 1]`; others are dropped, never clamped.
- One face, all 52 names in range, valid pose: `tracking`.

`confidence` is always `null`, because the stock Face Landmarker result carries no per-frame score.

## Synthetic feature stream

`fake_stream.py` publishes contract-valid camera feature messages (message 1 in `docs/contracts.md`) so the frontend and backend can be built against real message shapes before the camera pipeline exists.

It is a synthetic generator. It is not a camera, it does not run MediaPipe, and none of its output is recorded human data. Blendshape scores are low gaussian noise with periodic pulses on `browOuterUpLeft`, `jawOpen`, and `mouthSmileLeft`. Head pose is slow sinusoids well inside the valid ranges, with the face about 45 cm in front of the camera. `confidence` is always `null`, as it is for the stock Face Landmarker result.

Each 15 second cycle is 13.5 s of `tracking`, 1.0 s of `lost` starting at 10.0 s, and 0.5 s of `partial` starting at 12.0 s, so consumers exercise loss handling.

### Running the synthetic stream directly

`fake_stream.py` also keeps its own command line, with replay and fixture options. It needs the `websockets` package:

```text
python -m pip install -r perception/requirements.txt
python perception/fake_stream.py
```

Frames are served on `ws://127.0.0.1:8765`, one compact JSON text message per frame, to every connected client. A client may connect and disconnect at any time without stopping the publisher. Stop it with Ctrl+C.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--rate HZ` | `30.0` | Frames per second |
| `--seed N` | `11` | Random seed. The same seed and rate give the same feature and pose values |
| `--record PATH` | none | Also write every emitted frame to `PATH`, one JSON object per line |
| `--replay PATH` | none | Publish the frames in a recorded file, once, instead of generating |
| `--port N` | `8765` | Websocket port on 127.0.0.1 |
| `--once` | off | Print one pretty-printed frame to stdout and exit. Needs no extra packages |
| `--write-fixtures DIR` | off | Write the three fixture files into `DIR` and exit |

`timestamp_ms` is wall-clock time at the start of the run plus the frame offset, so live timestamps differ between runs while the values do not. A replay publishes the timestamps it recorded.

### Fixtures

One frame per tracking state, generated by `python perception/fake_stream.py --write-fixtures perception/fixtures` with seed 11 and a fixed start time:

- `perception/fixtures/frame_tracking.json`
- `perception/fixtures/frame_partial.json`
- `perception/fixtures/frame_lost.json`

Do not edit them by hand. `tests/test_fake_stream.py` fails if they differ from what the generator writes.

### Tests

`tests/test_fake_stream.py` checks the generator against the validator in `tests/contract_schema.py`. `tests/test_frame_builder.py` checks the tracking-state decision, the pose rules and `mirror_feature_names` with plain inputs, and checks that importing the camera source and the publisher loads none of `cv2`, `mediapipe`, `numpy` or `websockets`. Both need no packages beyond pytest. `websockets` is imported only inside the server functions, so `--once`, `--write-fixtures`, and the tests run on the standard library alone.
