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
- The frame rate is whatever the camera and detector sustain. It is not paced to a fixed rate, and it depends on the lighting: see [Frame rate depends on lighting](#frame-rate-depends-on-lighting).
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

## Filtering

`filters.py` is a downstream stage. It is not part of the published camera feature stream: `publish.py` emits the raw Face Landmarker scores, and the frontend, the backend and the classifier consume message 1 as published. Calibration and shared control apply this stage to the stream after receiving it. Standard library only.

A channel is one scalar per tracking frame: a blendshape score, `yaw_deg`, `pitch_deg`, `roll_deg`, or the head translation `x_cm`, `y_cm`, `z_cm` (the last column of the matrix). Each channel runs a causal moving median (odd window, 1 is pass-through) and then a one-pole low-pass whose smoothing factor is `1 - exp(-2*pi*cutoff_hz/rate_hz)`. `FilterBank` applies a `ChannelConfig` per channel to whole messages and returns a value and a validity flag per channel.

Gap handling:

- Only `tracking` frames update filter state. A `partial` or `lost` frame is not fed as 0.0 or as the last value, and a missing channel is never read as 0.0. Its outputs are marked not valid.
- If consecutive tracking frames are more than `reset_after_ms` apart (default 200), the filter resets instead of resuming. A one-pole that resumes across a 450 ms dropout, which has been measured during a fast head turn, blends pre-gap and post-gap values into a slow ramp that looks like a deliberate gesture.
- Output is not valid, and its value is `None`, until the median window is full. The one-pole is then seeded with the first median output, not with 0.0.

`group_delay_ms(config, rate_hz)` reports the added latency: `(W-1)/2` frames for a median of window `W`, about `1/(2*pi*cutoff_hz)` for the one-pole, and their total.

## Rest baseline

`baseline.py` measures the noise floor that thresholds are set against, while the subject holds still:

```text
python -m perception.baseline --source camera --out PATH.json
python -m perception.baseline --source fake --out PATH.json
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--source {fake,camera}` | `fake` | Which source to measure. Only `camera` is measurement |
| `--seconds S` | `10` | Seconds of frames to measure, by `timestamp_ms` |
| `--settle S` | `1` | Seconds of frames discarded first, while camera exposure settles |
| `--out PATH` | required | JSON record to write. A camera baseline is derived from human data: keep it out of git |
| `--rate`, `--seed` | `30.0`, `11` | Fake source only |
| `--camera`, `--mirrored` | `0`, off | Camera source only |

For each channel, over tracking frames only and on raw published values, the record holds `count`, `mean`, `std` (sample standard deviation, `n - 1`), `min`, `max` and `peak_to_peak`, plus:

| Field | Meaning |
| --- | --- |
| `distinct_values` | Number of distinct raw values seen |
| `zero_variance` | Every value identical, so `std` is exactly 0. A truly dead channel |
| `noise_floor` | Smallest `std` accepted as measured noise for the channel's unit |
| `below_noise_floor` | `std < noise_floor`. The channel is pinned and its rest noise is not usable |
| `range` | Full-scale range: 1.0 for a blendshape score, the width of the contract's valid range for pose |
| `snr` | `range / max(std, noise_floor)`. Always finite. When `below_noise_floor` is true it comes from the floor, not from the channel |

For the run it holds total frames, frames per state, `lost_fraction` (frames not tracking over all frames), `frame_rate_hz` measured from `timestamp_ms`, brightness (mean, min, max), and a `lighting` field that states the record is valid only for the lighting it was measured under.

### Pinned channels and the noise floor

In both camera baselines measured so far (brightness 105 and 135), five blendshapes stayed pinned near zero: `cheekSquintLeft`, `cheekSquintRight`, `_neutral`, `noseSneerLeft` and `noseSneerRight`. They were not exactly zero. Every value was under 3e-6 and their `std` was between 6e-9 and 4.0e-7, so `zero_variance` is false for them and `distinct_values` is large. The quietest other channel, `cheekPuff`, had `std` 7.7e-7 and 1.9e-6. The score noise floor, `NOISE_FLOORS["score"] = 5e-7` in `baseline.py`, sits in that gap in both runs, so exactly these five are reported `below_noise_floor`. The gap is narrow, under a factor of two on each side. The degree and centimeter floors, 0.01, only guard against dividing by zero; the smallest pose `std` measured was 0.30 degrees and 0.19 cm. A rest baseline shows that a channel is silent at rest. It cannot show that the channel will respond to a deliberate movement.

### Frame rate depends on lighting

The camera lengthens its exposure in dim light, which caps the frame rate. Two rest baselines on the development laptop, with nothing else changed in the code:

| Mean brightness | Frame rate |
| --- | --- |
| 105.3 / 255 | 19.94 Hz |
| 135.4 / 255 | 29.80 Hz |

The 20 frames per second reported earlier came from the room, not from the code. Any frame rate quoted from this repository moves with the lighting, and so does everything derived from it: the median's group delay, and channel noise. A baseline is valid only for the lighting it was measured under, and its record says so. Before you compare baselines across sessions, compare their brightness first, or a lighting change will look like a change in the user.

Filter latency, from `filters.group_delay_ms`, at the measured rates:

| Configuration | Rate | Median | One-pole | Total |
| --- | --- | --- | --- | --- |
| Directional: median 3, one-pole 2 Hz | 29.8 Hz | 33.6 ms | 79.6 ms | 113.1 ms |
| Stop: median 1, one-pole 8 Hz | 29.8 Hz | 0.0 ms | 19.9 ms | 19.9 ms |
| Directional: median 3, one-pole 2 Hz | 19.94 Hz | 50.2 ms | 79.6 ms | 129.7 ms |
| Stop: median 1, one-pole 8 Hz | 19.94 Hz | 0.0 ms | 19.9 ms | 19.9 ms |

The one-pole term does not depend on the frame rate, so better lighting shortens only the median term. At 29.8 Hz the directional configuration is 113.1 ms, well over the 20 ms filtering budget, and most of that is the 2 Hz one-pole. The stop configuration fits, at 19.9 ms.

Brightness is the mean grayscale value of the captured image, 0 to 255, computed by `mediapipe_source.grayscale_brightness` (BT.601 luma via `cv2.COLOR_BGR2GRAY`). That function is the single definition of brightness in this repository. It is recorded so that a clinician-facing caveat, that a decline may reflect worse lighting rather than worse motor control, can be checked against the lighting the baseline was measured under. The fake source has no image and records `null`. Brightness is a diagnostic on `CameraFrame` and is not published.

The record carries its own `schema_version` (currently 2; version 1 lacked the per-channel floor fields and `lighting`) and `record_type` `"rest_baseline"`. **This record is not part of `docs/contracts.md` yet**; its version is independent of the contract's schema.

`tests/test_filters.py` and `tests/test_baseline.py` cover both modules with the standard library and pytest only.
