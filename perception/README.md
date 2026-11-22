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

## Calibration

Calibration measures which movements a specific person can actually produce, ranks them, and writes a per-user profile. It is system identification: a prompt is the input, the channel trajectories are the output, and scoring measures each channel's response with means, standard deviations, correlations and a fixed ranking heuristic. Nothing is trained.

The work is split like the camera source:

- `calibration.py` scores a recorded session. Standard library only, no camera. Everything in it is tested without hardware.
- `calibrate.py` runs the live protocol: it drives the source, issues prompts, records to disk, and then calls `calibration.py` on the file it wrote. cv2, mediapipe and numpy are imported inside functions only.

```text
python -m perception.calibrate --source camera --user ID
python -m perception.calibrate --source fake --user ID        dry run, not measurement
python -m perception.calibrate --rescore PATH.jsonl           score an old recording, no camera
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--source {fake,camera}` | `fake` | Which source to calibrate from. The fake stream ignores prompts, so it never produces a binding |
| `--user ID` | `anonymous` | Stored in the recording and the profile |
| `--out-dir DIR` | `captures/calibration` | Where the recording and profile go. `captures/` is ignored by git; both files are human data |
| `--warmup S` | `2` | Seconds before the protocol while exposure settles. Brightness and frame rate are measured here for the session header |
| `--events {text,json}` | `text` | `text`: one readable line per event on stderr. `json`: one JSON event per line on stdout |
| `--no-window` | off | Camera only. Skip the prompt window |
| `--rescore PATH` | none | Score an existing recording and write its profile. Opens no source |
| `--rate`, `--seed`, `--camera`, `--mirrored` | | As for `publish.py` |

### Protocol

60 seconds after the warm-up. With the camera, a plain prompt window (text only, never the camera image) shows the current prompt, the time left in the segment, and whether the face is tracked. Space starts, Esc aborts.

| Segment | Duration | Prompt |
| --- | --- | --- |
| Free exploration | 15 s | "Move however you can." No instruction about what to move |
| Guided probes | 30 s | Six probes, 5 s each: three 900 ms repetitions with a 500 ms release between them, then a 1.3 s rest |
| Rest baseline | 10 s | "Hold still." |
| Confirm and bind | 5 s | The top-ranked channels are shown. Repeating the top channel's movement accepts it |

The six probes and their candidate channels: brows (`browInnerUp`, `browOuterUpLeft`, `browOuterUpRight`), jaw (`jawOpen`), smile (`mouthSmileLeft`, `mouthSmileRight`), turn left (`yaw_deg`), tilt up (`pitch_deg`), tilt toward the left shoulder (`roll_deg`). Head prompts are directional so the three repetitions go the same way. Eye closure is not probed because the person cannot read the release prompt with their eyes shut; blinks and gaze are still examined by discovery.

### Recording

One JSONL file per session. Scoring reads only this file, so a scoring change never needs the camera again.

| `record_type` | Contents |
| --- | --- |
| `session_header` | First line. `recording_schema_version`, `session_id`, `user_id`, `source`, `measured`, the protocol (timings and probes), and the warm-up `brightness` and `frame_rate_hz` |
| `marker` | `segment`, `probe`, `repetition`, `prompt`, `start_ms` (segment boundary), `prompt_issued_ms` (when the prompt was shown), `planned_duration_ms` |
| `frame` | `message`, exactly as published, and the image `brightness` |
| `scoring` | The tier and top channels computed before confirm |
| `confirm_result` | `accepted`, `channel`, `detected_ms` |
| `session_end` | `ended_ms`, and whole-session brightness and frame rate |

Markers, scoring, confirm and session boundaries are emitted as structured events: each is a dictionary passed to every listener and written to the recording. The calibration UI should drive its prompts from these events (`--events json` prints them on stdout); it should not parse console text.

Brightness and frame rate are recorded because both move with lighting (19.94 Hz at brightness 105 and 29.80 Hz at 135, above), and so does the noise floor. A profile's thresholds are valid only for the lighting they were measured under.

### Scoring

Rest reference: the rest baseline, after its first second. Mean and sample standard deviation per channel, with `effective_std = max(std, noise_floor)` using the Phase 2 floors, so a pinned channel cannot score infinitely.

Thresholds and deadbands are offsets from that measured rest mean, never absolute values. Head pitch rests at about +9.8 degrees on a laptop camera, and a threshold measured from 0 would fire continuously. Activation is `rest_mean ± max(5 × effective_std, 0.5 × minimum excursion)`; the deadband is `rest_mean ± max(3 × effective_std, 0.3 × minimum excursion)`. The floor exists because a truly still rest gives a tiny std: in the still camera session 5 × std put jawOpen's threshold 0.0054 above rest, inside the ordinary variation of a resting face between movements. With the fraction at 0.5, a channel that clears the minimum excursion reaches at least twice its threshold, the same headroom the SNR floor gives. The factor 5 is generous on purpose: real rest noise has blinks and slow drift, and at 3 a rest-only channel crosses several times a minute. The SNR floor of 10 keeps a surviving channel's range at least twice its threshold offset.

A peak or a crossing must hold for 2 consecutive tracking frames, which matches the median-of-3 stage in `filters.py`: single-frame spikes are ignored, as shared control would ignore them.

The seven metrics per candidate channel:

| Metric | Definition | Unit |
| --- | --- | --- |
| Usable range | Median over repetitions of the sustained peak deviation from rest | Channel unit. Comparable only within a unit family |
| Signal-to-noise | Usable range / effective std | Dimensionless. The only metric comparable across scores, degrees and centimetres, and the main ranking term |
| Repeatability | Coefficient of variation of the three repetition peaks | Unit-free, 0 is perfect |
| Independence | Pearson r with every other candidate over the whole session. Channels with \|r\| ≥ 0.8 form one cluster, and only its best member is bound | r |
| Latency | Per counted repetition, from that repetition's `prompt_issued_ms` (not the segment start) to the first sustained threshold crossing | ms |
| Fatigue drift | Last counted repetition's peak minus the first counted one's, absolute and relative | Channel unit and fraction |
| False positives | Sustained threshold crossings in held-out rest, count and per minute | Count |

**A rest window is per channel.** A rest window is only a valid rest for a given channel if that channel was not recently moved. The rest after a probe is recovery, not rest, for what that probe moved: in the still camera session jawOpen's std was 0.127 in the rest after the jaw probe against 0.00055 in the final rest, and yaw's was 8.9° after the turn probe and 13.4° after the tilt probe, because a head tilt also turns the head. So, for each channel, a probe rest qualifies only if it does not follow the channel's own probe, and the channel is back inside its threshold when the evaluated part of the rest opens (which also catches coupling, as tilt moves yaw). The false-positive count and the rest-quality comparison use only a channel's qualifying windows; the excluded ones are recorded with the reason (`own_probe`, `not_returned_to_rest`).

**False positives are measured on held-out rest.** Thresholds come from the final rest only. Crossings are counted in the channel's qualifying probe rests, after their first 500 ms, never in the final rest. Counting crossings in the same rest the threshold came from is circular and reports zero for almost every channel. A channel with no qualifying window gives `insufficient_data`, never zero.

**Which repetitions count.** Peak and latency are judged separately.

- A repetition's peak counts toward range, repeatability, drift and SNR even when its latency cannot be measured. A movement that starts before the channel has fully returned is still a real movement of that size.
- Latency is the threshold crossing, "time until the system can act", because Stop is bound to the lowest-latency channel. It is null, and left out of the latency statistic, when the channel was not below threshold at the prompt (`not_below_threshold_at_prompt`: it never came back after the previous repetition, so the next one starts already past threshold; in the first camera session that would have reported 28 ms for yaw) or crossed sooner than 100 ms after it (`anticipatory`, the standard anticipation cut-off in reaction-time research). A null latency is honest; a near-zero or redefined one is not. The 500 ms release is unchanged.
- A `missed` repetition, whose peak is below a quarter of the median of the others, is an absent response (the first browInnerUp repetition peaked at 0.044 against 0.57 and 0.62), not fatigue or poor repeatability. It is excluded from everything.

The table and the profile report two counts per channel, because they differ: `counted` repetitions, whose peak counts, and `latency` repetitions, which also gave a valid latency (the table's `3c/1l`). A channel with fewer than two counted repetitions is `insufficient_data`.

Hard disqualifiers apply before ranking: `insufficient_data`, `zero_variance` across the whole session, `false_positive_in_held_out_rest` (any crossing), `snr_below_floor`, `excursion_below_floor` (usable range under the minimum excursion for the unit, below), and `rest_suspect` (the channel's own rest measurements disagree, below). Survivors are ranked by a weighted sum: SNR 0.40 (log scale from 10 to 1000), repeatability 0.20, independence outside the channel's own cluster 0.15, latency 0.15, fatigue 0.10. Raw range is not a separate term because it is not comparable across units.

**Minimum excursion.** SNR is a ratio, so a channel whose rest noise is near zero reaches a high SNR from a movement that is itself near zero: noseSneerLeft peaked at 2.5e-5 in the first camera session and scored a free-exploration SNR of 37. An absolute floor per unit family (`MIN_EXCURSION` in `calibration.py`, with its derivation) is applied alongside the SNR floor, to candidates and to discovery.

Channels that no probe asks for but that moved strongly in free exploration, above both the SNR floor and the minimum excursion, are listed as discovered. They are never bound, because they have no repetitions to measure.

**Rest-quality gate.** Every threshold derives from the final rest, so the session checks that rest against itself, using only this session. A first-time user has no stored baseline to compare with. The gate is per channel: a brow still relaxing in the final rest says nothing about jawOpen's rest. For each candidate channel:

- `rest_level_shift`: the two halves of the final rest (after its first second) differ in mean by more than 5 times the quieter half's std. The rest level moved by more than an activation threshold.
- `rest_halves_std_ratio`: the two halves' stds differ by more than a factor of 3.
- `probe_rest_std_ratio`: the lower median std of the channel's qualifying probe rests and the median std of the final rest, cut into windows of the same length, differ by more than a factor of 3. It needs at least two qualifying probe rests. The lower median matters when the channel's own probe rest is excluded and only two remain: the mean of two would let a single twitch decide, and that twitch is already counted as a false positive. Equal lengths matter: for a slowly drifting signal std grows with window length, so a 0.8 s probe rest compared with the whole 9 s final rest would disagree even when the person was still.

A factor of 3: each segment holds only about ten independent samples once frame-to-frame correlation is allowed for, and for ten gaussian samples per side the 99th percentile std ratio is about 2.3. A disagreement counts only when it is large enough to move a threshold by at least the minimum excursion, so a pinned channel's std moving from 1e-7 to 1e-6 cannot void a session.

**Lighting gate.** Phase 2 established that frame rate and noise floor both move with brightness, so a session whose light changed has a moving noise floor under every threshold it derived. The session records brightness min, max, median and mean over every frame, and the median brightness and frame rate of each still segment (warm-up, probe rests, final rest). `lighting_quality` is `"suspect"` when the brightest still segment is more than 1.2 times the dimmest. In Phase 2 a 1.29× brightness change (105 to 135) moved the frame rate 1.49× (19.94 to 29.80 Hz), so 1.2 sits below the smallest change known to move the operating point. Only still segments are compared, because whole-image brightness also changes when the head turns during a probe, without any change in the light. The second camera session warmed up at 100 and then ran at 165 to 217 (still segments 100 to 202, 2.02×): suspect.

A channel that fails any rest check is disqualified as `rest_suspect`, with the failed checks recorded, and the session goes on without it (`rest_quality` `"channels_disqualified"`). The session's rest is `"suspect"` only when fewer than `MIN_CHANNELS_FOR_TIER` usable channels survive. That count is 1, because one reliable channel is the least any tier needs (Tier 3, scanning); with none there is no tier to propose, and a suspect rest is the reason. The lighting gate stays session-wide, because a change in the light moves every channel's noise floor at once.

When the session is suspect, by rest or by lighting, `session_quality` is `"suspect"`, no tier is proposed, the profile has no bindings, confirm offers nothing to accept, and `calibrate` exits with status 2. The profile is still written, so the failed enrolment is on record.

**Observation, not a change: voluntary facial effort seems to take about 3 s to relax.** In two camera sessions a face that had just been moving was still relaxing well into "Hold still": the first session's smile stayed near 0.6 for about four seconds before falling to 0, and in the third session browOuterUpLeft and browOuterUpRight drifted down for about 3 s (0.096 to 0.034) before settling. The rest baseline skips only its first 1 s, so the gate flagged both, correctly: those rests were not settled. A longer settle is a candidate change, but two sessions from one person are not enough to set it, so the 1 s settle is unchanged.

Tier, from the usable channels (survivors that represent their cluster):

- Exactly one: Tier 3, scanning. One channel can only drive a selection switch, even a broad one.
- Two or more, at least one broad and repeatable (SNR ≥ 50, CV ≤ 0.25): Tier 1, proportional.
- Two or more, none broad: Tier 2, discrete.
- None: no tier proposed.
- Session suspect (rest or lighting): no tier proposed, whatever survived.

**Stop binding.** Stop goes to the usable channel with the lowest median latency, because a late stop is unsafe, but only among channels with at least 2 valid latency repetitions (`min_stop_latency_reps`). A median over one sample is not a basis for that binding, however fast the sample. If no channel qualifies, the profile records `stop_binding.channel` as null with the reason, and states that the hardware cutoff and the signal-loss stop remain the stop path. That outcome is still safe, because neither depends on a calibrated channel. They are the stop path in every case; a bound channel adds to them and never replaces them.

Every weight, floor and multiplier is in `ScoringConstants` in `calibration.py`. **They are provisional.** They come from the Phase 2 rest baselines and the protocol timing, not from calibration data, and will be tuned once real sessions exist. Each profile records the constants that produced it.

### Profile

One JSON file next to the recording, `<user>_<session>_profile.json`: `record_type` `"calibration_profile"`, its own `schema_version` (1, independent of the contract's schema), `rest_quality` (`"ok"` or `"suspect"`) and the channels responsible, `session_quality`, `lighting_quality` with the brightness record, the proposed tier and its reason, `stop_binding` (channel or null, reason, and the stop-path note), whether confirm accepted it, bindings with roles (`proportional_primary`, `proportional_secondary`, `discrete`, `scan_switch`), direction, rest mean and std, threshold, deadband, usable range and extreme, thresholds for every candidate, lighting, the scoring constants, and the full enrolment metrics. The enrolment metrics are the first data point of the user's longitudinal record.

**This profile is not part of `docs/contracts.md` yet, and it needs to be:** the ML owner consumes the per-user ranges. Until it is added there, its fields can change with its own `schema_version`.

`tests/test_calibration.py` covers scoring with synthetic recordings that have known answers, and drives the live engine with a synthetic stream that follows the prompts. Standard library and pytest only.
