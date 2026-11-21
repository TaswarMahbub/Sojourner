# Sojourner data contracts

Document date: 2026-11-21

Schema version: `"4"`

Schema 4 replaces schema 3. Schema 3 was the previous unmerged revision of this pull request, and no schema 3 producer was published. Consumers implement `"4"` and reject any other `schema_version`. A motor-command consumer that rejects a datagram belonging to its active boot stops the motors and does not apply that datagram. A datagram that does not belong to the active boot is ignored, so an older schema cannot rearm motion.

This file is the interface between perception, the gesture classifier, the laptop shared-control state machine, the ESP32 firmware, the 2D simulator, and session logging. Behavior that is not written here is not part of the interface.

Schema 4 is a research control interface for a four-motor rover. It is not a medical-device specification. Confidence values are producer scores when a score exists. They are not clinical probabilities, and no label in this schema has a therapeutic meaning.

The Python modules under `tests/` check this document against mocked messages. They are not firmware and they do not verify the ESP32, the motors, or a physical emergency-stop switch.

## Versioning

Every message carries `schema_version`. For this document the value is the string `"4"`.

A change that removes a field, renames a field, changes a unit, changes a valid range, or changes stop behavior increments `schema_version` to the next integer string and is reviewed under `CONTRIBUTING.md`.

Consumers of schema `"4"` reject a message that contains a field this document does not define for that message. They also reject a missing required field, a duplicate key, a value of the wrong JSON type, a non-finite number, or a value outside its valid range. JSON object key order has no meaning. Booleans are not numbers. A decoder returns that rejection without raising.

Timestamps and identifiers that travel as JSON numbers are integers in the safe integer range, from `0` through `9007199254740991`. A timestamp is Unix epoch milliseconds in UTC, not a formatted date string. `boot_nonce` and `session_id` use `1` through `9007199254740991`.

## Messages

```text
camera
  → perception feature stream
  → gesture classification
  → laptop shared control (Mridhul)
       → motor command datagram
       → ESP32 firmware
            reads the physical emergency stop locally
            publishes motor status
       → 2D simulator, same command and same status
backend records profiles and sessions
```

Shared control is the only publisher of motor commands. It runs on the laptop, not on the ESP32. Mridhul owns that state machine. Any change that can make it publish a non-zero motor command needs a Hardware/ML safety review before merge. Perception does not publish motor commands. The classifier does not publish motor commands. The simulator does not publish a second command format.

Motor commands are UDP datagrams. UDP has no connection. The receiver does not reset its sequence because a socket opened or closed. Session identity is `boot_nonce` plus `session_id`, defined below.

An actionable camera frame has `tracking_state` `"tracking"`, every required blendshape, a valid `head_pose`, and `confidence` either `null` or inside `(0, 1]`. Every other camera frame is not actionable. `confidence: null` means the detector result did not carry a per-frame score. It does not mean the face was lost.

The gesture label `"unknown"` is not a motion request. The label `"neutral"` means no intentional gesture and is not, by itself, a motion request. Schema 4 does not map gesture labels onto velocities. That mapping requires a later reviewed change.

## 1. Camera feature stream

Publisher: perception (Mridhul).

Consumers: the gesture classifier, shared control, the dashboard, and session logging.

One message describes at most one face. The producer uses MediaPipe Face Landmarker with blendshapes and the facial transformation matrix enabled.

### What the Python result actually contains

In the current `mediapipe.tasks.python.vision.face_landmarker` API, `FaceLandmarkerResult` has three attributes:

- `face_landmarks`
- `face_blendshapes`
- `facial_transformation_matrixes`

The matching C result struct carries those arrays and their counts. It does not carry detection, presence, or tracking confidence.

`min_face_detection_confidence`, `min_face_presence_confidence`, and `min_tracking_confidence` are `FaceLandmarkerOptions`. They decide whether MediaPipe returns a face at all. Their defaults in that API are `0.5`. They are not fields of the result, and this schema does not copy them into `confidence`.

The producer sets `tracking_state` from the result it actually received:

| Result contents | `tracking_state` |
| --- | --- |
| `face_landmarks` is empty, or more than one face is present | `lost` |
| Exactly one face, and blendshapes or the pose fail the rules below | `partial` |
| Exactly one face, all 52 blendshapes are present and in range, and `head_pose` is valid | `tracking` |

A face that passes the configured options and returns landmarks is not marked `lost` merely because the result has no numeric confidence.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"4"` |
| `timestamp_ms` | integer | yes | Capture time of the camera frame, Unix epoch milliseconds, UTC |
| `tracking_state` | string | yes | `"tracking"`, `"partial"`, or `"lost"` |
| `features` | object | yes | Named blendshape scores. See below |
| `head_pose` | object or null | yes | Head pose. `null` unless `tracking_state` is `"tracking"` |
| `confidence` | number or null | yes | A real per-frame score, or `null` when the result has none |

`timestamp_ms` is the capture time of the camera frame this message describes. It is not the time the message was later written to the database.

### Tracking state

| State | `features` | `head_pose` | `confidence` | Actionable |
| --- | --- | --- | --- | --- |
| `tracking` | All 52 names, each in `[0, 1]` | Valid pose object | `null`, or a number in `(0, 1]` | Yes |
| `partial` | Any subset of the 52 names, including empty. Present values lie in `[0, 1]` | `null` | `null`, or a number in `[0, 1]` | No |
| `lost` | Empty object `{}` | `null` | `null` | No |

### Named facial features

`features` uses the 52 Face Landmarker blendshape names. `*Left` and `*Right` are the subject's left and right, which is the MediaPipe definition. The producer publishes names for the image buffer it passed to Face Landmarker. A mirrored preview on screen does not rename the scores. If that buffer was mirrored, the producer corrects the left and right names before publishing so the message stays subject-relative.

Each score is a JSON number in `[0, 1]`. For every name except `_neutral`, `0` is the inactive value and is the neutral value used by Sojourner. `1` is the full activation of that shape as defined by MediaPipe. `_neutral` is forwarded so the set matches Face Landmarker. Consumers must not map `_neutral` to a gesture or to motion. Schema 4 does not give `_neutral` a neutral value.

A `tracking` message contains every name in this table and no other feature keys.

| Index | Name | Neutral value for control |
| --- | --- | --- |
| 0 | `_neutral` | Not used for control |
| 1 | `browDownLeft` | `0` |
| 2 | `browDownRight` | `0` |
| 3 | `browInnerUp` | `0` |
| 4 | `browOuterUpLeft` | `0` |
| 5 | `browOuterUpRight` | `0` |
| 6 | `cheekPuff` | `0` |
| 7 | `cheekSquintLeft` | `0` |
| 8 | `cheekSquintRight` | `0` |
| 9 | `eyeBlinkLeft` | `0` |
| 10 | `eyeBlinkRight` | `0` |
| 11 | `eyeLookDownLeft` | `0` |
| 12 | `eyeLookDownRight` | `0` |
| 13 | `eyeLookInLeft` | `0` |
| 14 | `eyeLookInRight` | `0` |
| 15 | `eyeLookOutLeft` | `0` |
| 16 | `eyeLookOutRight` | `0` |
| 17 | `eyeLookUpLeft` | `0` |
| 18 | `eyeLookUpRight` | `0` |
| 19 | `eyeSquintLeft` | `0` |
| 20 | `eyeSquintRight` | `0` |
| 21 | `eyeWideLeft` | `0` |
| 22 | `eyeWideRight` | `0` |
| 23 | `jawForward` | `0` |
| 24 | `jawLeft` | `0` |
| 25 | `jawOpen` | `0` |
| 26 | `jawRight` | `0` |
| 27 | `mouthClose` | `0` |
| 28 | `mouthDimpleLeft` | `0` |
| 29 | `mouthDimpleRight` | `0` |
| 30 | `mouthFrownLeft` | `0` |
| 31 | `mouthFrownRight` | `0` |
| 32 | `mouthFunnel` | `0` |
| 33 | `mouthLeft` | `0` |
| 34 | `mouthLowerDownLeft` | `0` |
| 35 | `mouthLowerDownRight` | `0` |
| 36 | `mouthPressLeft` | `0` |
| 37 | `mouthPressRight` | `0` |
| 38 | `mouthPucker` | `0` |
| 39 | `mouthRight` | `0` |
| 40 | `mouthRollLower` | `0` |
| 41 | `mouthRollUpper` | `0` |
| 42 | `mouthShrugLower` | `0` |
| 43 | `mouthShrugUpper` | `0` |
| 44 | `mouthSmileLeft` | `0` |
| 45 | `mouthSmileRight` | `0` |
| 46 | `mouthStretchLeft` | `0` |
| 47 | `mouthStretchRight` | `0` |
| 48 | `mouthUpperUpLeft` | `0` |
| 49 | `mouthUpperUpRight` | `0` |
| 50 | `noseSneerLeft` | `0` |
| 51 | `noseSneerRight` | `0` |

The index column matches the Face Landmarker blendshape order and is documentation only. The JSON object uses the names as keys.

### Head pose

`head_pose` is `null` when `tracking_state` is `partial` or `lost`. When `tracking_state` is `tracking`, it is an object:

| Field | JSON type | Definition |
| --- | --- | --- |
| `matrix` | array of 4 arrays of 4 numbers | Facial transformation matrix, defined below |
| `yaw_deg` | number | Yaw in degrees, valid range `[-90, 90]` |
| `pitch_deg` | number | Pitch in degrees, valid range `[-60, 60]` |
| `roll_deg` | number | Roll in degrees, valid range `[-45, 45]` |

Neutral orientation is `yaw_deg` `0`, `pitch_deg` `0`, and `roll_deg` `0`. There is no neutral translation. A missing face is `head_pose: null`, not a zero matrix. A zero matrix is invalid.

#### Matrix

`matrix` is the MediaPipe facial transformation matrix from the canonical face model to the detected face, written as four rows of the mathematical matrix. The last row is exactly `[0, 0, 0, 1]`.

Some MediaPipe bindings expose this matrix as a column-major list of 16 numbers. The producer converts those bindings to this row layout before publishing. After conversion, `matrix[row][col]` is the mathematical element.

The upper-left 3×3 contains a uniform scale and a rotation. The translation is the last column, in centimeters, because that is the unit of the MediaPipe canonical face model:

- `matrix[0][3]` is X, centimeters, positive toward image right.
- `matrix[1][3]` is Y, centimeters, positive up.
- `matrix[2][3]` is Z, centimeters. The camera sits at the origin and looks along negative Z, so a face in front of the camera has negative Z.

Sanity bounds for a `tracking` pose:

| Component | Valid range |
| --- | --- |
| X centimeters | `[-100, 100]` |
| Y centimeters | `[-100, 100]` |
| Z centimeters | `[-200, -5]` |

These bounds reject a pose that is behind the camera or far outside a bench setup. They are not a measured operating distance.

The camera frame is right-handed: positive X is image right, positive Y is up, and the camera looks along negative Z. This matches the OpenGL-style metric camera used by MediaPipe face geometry.

#### Angles

The producer removes scale, then extracts angles. Let `A` be the upper-left 3×3 of `matrix`. Let `n0`, `n1`, and `n2` be the L2 norms of its three columns. Let `s = (n0 + n1 + n2) / 3`.

A `tracking` pose requires `s` finite and greater than `0`, and requires each column norm to be within 1 percent of `s`:

```text
max(|ni - s|) / s <= 0.01
```

`R = A / s`. The published angles are the angles of this `R`, in degrees:

```text
yaw_deg   = degrees(atan2(R[0][2], R[2][2]))
pitch_deg = degrees(asin(clamp(R[1][2], -1, 1)))
roll_deg  = degrees(atan2(R[1][0], R[1][1]))
```

`atan2` is the two-argument arctangent from the C math library and from Python. `atan2(y, x)` is the angle of the point `(x, y)`. In the formulas above, the first argument is the sine-like element and the second is the cosine-like element. Published angles must match this extraction within `0.1` degrees.

This extraction is the inverse of the following composition for pitch inside `(-90°, 90°)`. With yaw `y`, pitch `p`, and roll `r` in radians:

```text
R = Ry(y) · Rx(-p) · Rz(r)
```

```text
Ry(y)  = [[ cos y, 0, sin y],
          [ 0,     1, 0    ],
          [-sin y, 0, cos y]]

Rx(-p) = [[1, 0,     0    ],
          [0, cos p, sin p],
          [0, -sin p, cos p]]

Rz(r)  = [[cos r, -sin r, 0],
          [sin r,  cos r, 0],
          [0,      0,     1]]
```

On an unmirrored image, in the camera frame above:

- Positive yaw turns the nose toward image right, which is the subject's left.
- Positive pitch raises the nose.
- Positive roll lowers the subject's right ear.

If the derived angles or the translation fall outside the valid ranges, the producer must not send `tracking`. It sends `partial` or `lost`, with `head_pose` null.

### Confidence

`confidence` is `null` unless the producer read a per-frame score from the result object it actually received. The stock `FaceLandmarkerResult` has no such score, so the stock producer publishes `null`.

The producer must not invent a score. In particular it must not publish the option thresholds, `0`, or `1` as a substitute for a missing result field. `null` on a `tracking` frame remains actionable. `lost` also uses `null`, because "no face in the result" is expressed by `tracking_state`, not by a fabricated zero.

If a later MediaPipe result object exposes a real per-frame score, the producer may publish that number. A `tracking` score must lie in `(0, 1]`. A value of `0` is not a tracking confidence. The number is still not a calibrated probability and is not a measure of gesture-classification accuracy.

### Encoding illustration

This fragment shows a lost face when the result contains no landmarks. It is not a recorded frame.

```json
{
  "schema_version": "4",
  "timestamp_ms": 1795276800000,
  "tracking_state": "lost",
  "features": {},
  "head_pose": null,
  "confidence": null
}
```

A `tracking` message uses the same fields, with all 52 feature names, a `head_pose` object, and `"confidence": null` for the stock Face Landmarker. The neutral orientation inside that object is `"yaw_deg": 0`, `"pitch_deg": 0`, `"roll_deg": 0`. The matrix of a real face also carries translation and scale, so identity-with-zeros is not a required tracking example.

## 2. Gesture classification

Publisher: the gesture classifier (Hardware/ML).

Consumers: shared control, the dashboard, and session logging.

One message classifies one time window of camera feature frames. A one-frame classifier is the window whose start and end are the same frame.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"4"` |
| `timestamp_ms` | integer | yes | Time the classification was produced, Unix epoch milliseconds, UTC |
| `source_timestamp_ms` | integer | yes | `timestamp_ms` of the newest feature frame that was an input |
| `window_start_timestamp_ms` | integer | yes | `timestamp_ms` of the oldest feature frame that was an input |
| `window_end_timestamp_ms` | integer | yes | Equal to `source_timestamp_ms` |
| `window_frame_count` | integer | yes | Number of feature messages actually passed into the model, `>= 1` |
| `label` | string | yes | Gesture label, defined below |
| `confidence` | number | yes | Classifier score for `label`, in `[0, 1]` |
| `model_version` | string | yes | Identifier of the weights and label list |

The window is closed: every input frame's `timestamp_ms` satisfies `window_start_timestamp_ms <= timestamp_ms <= window_end_timestamp_ms`. Frames the model did not use are not counted and do not widen the window. Gaps inside the window are allowed. Schema 4 does not set a maximum window duration. Shared control still treats a gesture whose `window_end_timestamp_ms` is more than `250` ms behind the command clock as stale and publishes a stop.

`window_start_timestamp_ms` is less than or equal to `window_end_timestamp_ms`. `source_timestamp_ms` equals `window_end_timestamp_ms`. `timestamp_ms` is greater than or equal to `window_end_timestamp_ms`. For a one-frame model, the start, the end, and `source_timestamp_ms` are the same number and `window_frame_count` is `1`.

If any input frame was not actionable, `label` is `"unknown"`. The classifier does not invent a gesture for `partial` or `lost`.

### Label

`neutral` and `unknown` are reserved in every model version.

| Label | Meaning | Motion |
| --- | --- | --- |
| `neutral` | Every input frame was actionable, and the model assigned no intentional gesture | Does not request motion |
| `unknown` | An input frame was not actionable, the label is not in the reviewed list, or the model cannot assign a class | Does not request motion. Shared control must stop |

Any other label must appear in a reviewed label list for that exact `model_version` before a consumer treats it as known. Schema 4 does not include a label list, because no model is in the repository. A label that is not reserved and has no reviewed list entry is invalid. Consumers handle an invalid label as `unknown` and do not move the rover.

`model_version` matches `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. It names one weight file and one label list. An empty string is invalid. The field has no neutral value.

### Confidence

`confidence` is the classifier's score for the published `label`. It lies in `[0, 1]`. `0` means the producer assigns no score to that label. `1` means the producer reported its maximum score. The score is not a calibrated probability.

There is no neutral confidence. For `unknown`, the producer still sends a score in `[0, 1]`. That score must not be used to override the `unknown` label.

### Encoding illustration

This illustrates a one-frame non-actionable result. It is not model output.

```json
{
  "schema_version": "4",
  "timestamp_ms": 1795276800040,
  "source_timestamp_ms": 1795276800000,
  "window_start_timestamp_ms": 1795276800000,
  "window_end_timestamp_ms": 1795276800000,
  "window_frame_count": 1,
  "label": "unknown",
  "confidence": 0,
  "model_version": "none"
}
```

`model_version` `"none"` is a legal identifier for a producer that is not yet running a trained model. It still has no labels beyond `neutral` and `unknown`.

## 3. Motor command

Publisher: laptop shared control (Mridhul), with Hardware/ML safety review for any change that can emit non-zero motion.

Consumers: ESP32 firmware and the 2D simulator. The backend may store a copy. Storage is not a consumer of the real-time stop path.

The command uses the rover body frame from ROS REP-103. Positive X is forward, positive Y is left, and positive Z is up. `linear_mps` is translation along X. `angular_rad_s` is yaw rate about Z. Positive yaw is a left turn, counterclockwise when viewed from above.

The wheel mapping for the four motors is not part of this message. Firmware owns that mapping.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"4"` |
| `controller_id` | string | yes | Authorized controller, `1` to `32` characters from `A-Z`, `a-z`, `0-9`, `_`, and `-` |
| `boot_nonce` | integer | yes | Nonce from the receiver status for this firmware boot |
| `session_id` | integer | yes | Sender session, strictly increasing across sessions from that sender |
| `sequence` | integer | yes | Sequence inside the session, from `1` through `4294967295` |
| `freshness_id` | integer | yes | Challenge copied from the newest motor status |
| `rearm_generation` | integer | yes | Generation copied from the newest motor status, from `0` upward |
| `status_challenge` | integer | yes | Sender-chosen challenge that the next status must echo |
| `issued_at_ms` | integer | yes | Unix epoch milliseconds, UTC, used as a log time and as one end of the lifetime |
| `expires_at_ms` | integer | yes | The other end of the lifetime. It is not, by itself, a freshness proof |
| `task_state` | string | yes | One of the task states below |
| `linear_mps` | number | yes | Forward velocity, meters per second |
| `angular_rad_s` | number | yes | Yaw rate, radians per second |
| `stop` | boolean | yes | Stop flag |
| `mac` | string | yes | HMAC-SHA256 of the canonical command, 64 lowercase hex characters |

There is no field a sender can use to clear the physical emergency stop, close the motor cutoff, or declare the receiver clock synchronized.

### Clocks

The receiver keeps two clocks apart.

| Clock | What it is | What it decides |
| --- | --- | --- |
| Monotonic milliseconds | A counter that starts at `0` on each boot and never moves backward | Freshness-challenge age, the active motion deadline, the software watchdog, and the hardware-pet deadline |
| Unix UTC milliseconds | Wall time | Logging. It also bounds commands, but only after a verified bounded-error synchronization |

`clock_state` starts at `unsynchronized`. Receiving a packet does not change it. The receiver does not infer a trusted clock from the time a datagram arrived. While the clock is unsynchronized, the receiver does not compare `issued_at_ms` or `expires_at_ms` with a wall clock, and it does not arm or apply motion from Unix time alone.

The command lifetime is still required:

```text
1 <= expires_at_ms - issued_at_ms <= 250
```

That difference is a duration. On an accepted motion command the receiver stores a deadline at the earlier of two times on the same monotonic counter: accept time plus that duration, and the time the authorizing freshness challenge was issued plus `250` ms. If less than `1` ms remains before that challenge bound, the receiver rejects the motion command. It commands the safe output when monotonic time reaches the deadline, even if no later packet arrives. Reaching the deadline does not by itself set the fault latch, and it is not evidence that the electrical cutoff opened. A frozen firmware does not apply this software deadline. The hardware cutoff is a separate timer.

When a verified synchronization has completed, the receiver does both checks. It still uses the monotonic deadline, and it also commands the safe output when the trusted Unix time reaches `expires_at_ms`. It rejects an active-session command whose `expires_at_ms` is already behind that trusted clock, and a command whose `issued_at_ms` is more than `2000` ms ahead of it. The Unix check is an extra bound. It does not replace the freshness challenge. This repository does not implement the synchronization protocol on an ESP32. The test hook that marks a clock synchronized stands in for a completed protocol, and a packet cannot call it.

### Freshness challenge

The receiver publishes `freshness_id` in motor status. A session start, a `rearm`, and a motion command are fresh only when they echo the current id and the monotonic age of that id is at most `250` ms. Otherwise the receiver does not arm and does not apply motion. A freshness match is not enough for motion. Motion also needs at least `1` ms remaining before the challenge's issuance time plus `250` ms. A session start and a `rearm` may still be accepted at an age of exactly `250` ms.

The receiver rotates the id when its age passes `250` ms, after every accepted datagram, and when the physical stop is asserted, when it is released, or when motor power is restored. One challenge authorizes one accepted datagram. A command that was sealed under an older id stays stale after it arrives. Arrival time is not a substitute for the id.

An authenticated safety stop (`idle`, `hold`, `estop`, or `fault`) may still stop the motors when its challenge has expired. That stop does not refresh the watchdog, does not move `last_sequence` forward, and does not arm. A newer authenticated motion command for the active session that is rejected commands the safe output. The rejection reasons are an expired challenge, the wrong `rearm_generation`, trusted-clock expiry, less than `1` ms of remaining lifetime, the physical stop, a sensor that is not `released`, either software latch, an open cutoff, an unpowered motor bus, unfinished startup, or `armed` false. That rejection does not accept the sequence, does not refresh the watchdog, does not clear either latch, and does not change `armed`. An older or replayed motion command, a lower session, a wrong boot, a command for a different session that is not a valid session start, and a bad MAC for a session that is not active leave the active command unchanged.

### Command authentication

The demonstration control channel is HMAC-SHA256, as specified by RFC 2104, over a canonical encoding of the fields. The Python standard library `hmac` and `hashlib` are the primitives used by the contract tests. This is not a new cipher, and it is not an integration of DTLS or mbedTLS. Firmware verification of the same MAC is unverified until a bench test.

The pre-shared key and the one `controller_id` bound to it are provisioned out of band. They are not in this repository. Wi-Fi association, a source IP address, and a public `boot_nonce` are not application authorization. A message whose `controller_id` is not the provisioned controller is not authorized, including when it was signed with the same key bytes.

The command MAC input is the prefix `sojourner-command-mac-v1` and a newline, then each field below as `name=value` and a newline, in this order: `schema_version`, `controller_id`, `boot_nonce`, `session_id`, `sequence`, `freshness_id`, `rearm_generation`, `status_challenge`, `issued_at_ms`, `expires_at_ms`, `task_state`, `linear_mps`, `angular_rad_s`, `stop`. Integers are decimal. Booleans are `true` or `false`. Each velocity is the applied value printed with exactly six digits after the decimal point, and the applied value is that same decimal. A velocity that is not already equal to its six-digit form is rejected. That includes a value that would round onto a different velocity, and it includes negative zero. The receiver applies the canonical value. It does not apply a different raw float that prints the same way. The `mac` field is not an input. The MAC is lowercase hex.

The status MAC uses the prefix `sojourner-status-mac-v1` and the status fields in the order listed in the status section, except `mac`. A null session or sequence is the token `null`.

The MAC does not encrypt the datagram. It does not by itself provide freshness, sequence order, or rearm-generation binding. Those checks stay beside it. A bad MAC for the active boot and active session commands the safe output and is not accepted. A bad MAC for any other boot or session is ignored. The labeled key in `tests/control_auth.py` is a test fixture. It must not be installed on a rover.

### Sessions on UDP

Each firmware boot chooses a new `boot_nonce` and publishes it in motor status. The nonce stays constant until the next boot. The receiver starts that boot stopped, disarmed, with the cutoff open and the sensor state `unknown`. It forgets every previous session because those packets carry the old nonce.

The sender persists the highest `session_id` it has used and the highest `status_sequence` it has accepted for each boot. A restart that loses that memory must not treat a replayed status as a new observation.

The sender:

1. Chooses a new `status_challenge` and sends a session start only after a status whose MAC verifies for the provisioned controller.
2. Copies `boot_nonce`, `freshness_id`, and `rearm_generation` from that status. It does not invent them.
3. Chooses a `session_id` strictly greater than every session id it has already used.
4. Sends a session-start datagram: sequence `1`, `task_state` `"idle"`, `stop` true, both velocities `0`, the copied freshness and generation, and the new challenge.
5. Trusts the session only after a later status echoes that challenge, names the same boot and session, and passes the status gate below.
6. Sends `rearm` when the operator has asked to arm and a current authenticated status shows the physical control released, the sensor released, and the cutoff closed. The software estop latch and the fault latch may still be set. `rearm` is the datagram that clears them, so the sender does not require those latches to be clear first, and it does not require `armed`. The operator request is a local decision on the laptop. It is not a bit the rover trusts from the network. The rover still requires the fresh authenticated `rearm`.
7. Sends non-zero motion only when the status gate authorizes it. The command uses the freshness id and rearm generation from that same status, and a sequence greater than `last_sequence`. Motion authorization still requires both software latches to be clear and `armed` to be true.

Inside one session, `sequence` starts at `1` and then increases. Gaps are allowed. Wraparound is not allowed. After `4294967295` the sender starts a greater `session_id` instead of wrapping. Replaying the previous session's `rearm` does not arm the replacement session. A sender that restarts and repeats the active session id cannot rearm; it has to open a greater session.

At the linear limit of `0.30` m/s, `250` ms of continued motion would travel `0.075` m. An accepted motion command ends no later than `250` ms after the authorizing challenge was issued, when the firmware is running the same monotonic timer the challenge uses. It is not left applied until `500` ms after that issuance.

### Task state

| `task_state` | Meaning | Motion fields | `stop` |
| --- | --- | --- | --- |
| `idle` | No active task. Does not clear latches or the physical emergency stop | Both `0` | `true` |
| `manual` | Operator input is the only motion source | Inside the ranges below | `false` to move, `true` to stop |
| `shared` | The message already combines operator and assist input | Inside the ranges below | `false` to move, `true` to stop |
| `hold` | The task is retained and motion is held | Both `0` | `true` |
| `estop` | Software emergency stop. Sets the software latch | Both `0` | `true` |
| `fault` | The publisher is reporting a failure. Sets the fault latch | Both `0` | `true` |
| `rearm` | Deliberate request to clear software latches and arm | Both `0` | `true` |

A message that does not match its row in this table is invalid. `manual` and `shared` are the only states that may request non-zero motion, and only when `stop` is `false`. When `stop` is `true`, both motion fields are `0` in every state. Firmware does not add assist on top of the message. Shared control has already combined the sources before publishing.

`idle` is ordinary stopped traffic. It is not an emergency-stop reset and it is not a rearm.

### Linear and angular motion

| Field | Unit | Valid range | Neutral value | Positive direction |
| --- | --- | --- | --- | --- |
| `linear_mps` | meters per second | `[-0.30, 0.30]` | `0` | Forward, body +X |
| `angular_rad_s` | radians per second | `[-1.00, 1.00]` | `0` | Left turn, positive yaw about body +Z |

These ranges are the schema 4 interface limits. They are not a measured top speed of the rover. Firmware may clamp more tightly. Firmware must not apply a value outside these ranges. A message outside these ranges is invalid. A velocity is also invalid when it is not canonical: it must already equal the value printed with six digits after the decimal point. Negative zero is invalid. The neutral value `0` is the canonical `0.000000`.

When `stop` is `true`, both motion fields must be `0`. That is the neutral motor command.

### Stop conditions and the physical cutoff

These conditions are independent. One of them being clear does not clear the others.

| Condition | How it becomes true | What a network `idle` command does | How it becomes false |
| --- | --- | --- | --- |
| Physical emergency stop | The local sensor reads `asserted` or `unknown` | Nothing. Idle cannot clear it | The sensor reads `released`. Release stops the motors, opens the cutoff, leaves the receiver disarmed, and increments `rearm_generation` |
| Software estop latch | An authenticated `estop` for the active boot and session is applied, including an older sequence | Nothing. The latch stays set | An accepted `rearm` bound to the generation from after the latest release |
| Fault latch | An authenticated `fault` for the active session is applied, or the monotonic watchdog expires | Nothing. The latch stays set | The same accepted `rearm` |
| Motor cutoff | Startup is unfinished, the motor bus is unpowered, the sensor is not `released`, the hardware pet is more than `250` ms old, or a cutoff hold is set | Nothing | An explicit restoration while the firmware is running, the bus is powered, startup is finished, and the sensor is `released`. That restoration clears the hold and PWM. It does not arm and it does not put a previous PWM value back on the drivers |

The physical control is local to the rover. Asserted means the sensor is in the stop position. `unknown` is treated as asserted, including at startup and after a brownout before the sensor has been read. Released means the operator has returned it to the run position. Release alone does not arm the motors.

The rover requires an electrical cutoff of motor power or of the driver enable. The cutoff has to remove torque even when the firmware is frozen and never runs the software watchdog. The software watchdog is a second path for a firmware loop that is still running but has not accepted a datagram. This document does not assign a pin, a contactor, or a normally-open or normally-closed contact. Those choices, and the claim that a particular board removes torque while the MCU is frozen, are unverified until a bench test. The model in `tests/motor_receiver.py` checks the contract. It is not that bench test, and it is not a verified electrical implementation.

The cutoff state in that model does not depend on the firmware executing. A frozen firmware cannot publish a new status and cannot pet the hardware watchdog. The model keeps driver enable, the time of the last hardware pet, whether the cutoff is open, and a cutoff hold. Driver enable is true only when the motor bus is powered and the cutoff is closed. Expiration of a software motion deadline does not pet the cutoff and is not proof that the cutoff opened. While the firmware is frozen, a stored non-zero output can remain and the model can still report torque until the pet age passes `250` ms. After that the cutoff opens and the hold stays set. When the firmware later runs, it commands the safe output and stays disarmed while the hold is set. It does not close the cutoff by polling, and it does not restore the retained PWM. Restoring motor power sets the hold, opens the cutoff, clears PWM, and stays disarmed. The hold clears only through the explicit restoration. Closing the cutoff still does not arm.

`rearm_generation` increments when the sensor enters `asserted` or `unknown`, when it changes to `released`, and when motor power returns without an MCU reset. A `rearm` or a motion command must carry the current generation. A `rearm` that was rejected while the stop was asserted, and any command sealed before the release or before the power return, cannot arm or move afterward. The sender has to read a status taken after that event and send a new `rearm`.

`rearm` clears the software estop latch and the fault latch only when all of the following are true:

- the MAC verifies for the provisioned controller
- `boot_nonce`, `freshness_id`, and `rearm_generation` match the receiver
- `session_id` is the active session
- `sequence` is strictly greater than the last accepted sequence
- the physical sensor is `released`, the cutoff is closed, the motor bus is powered, and startup is finished
- `task_state` is `"rearm"`, `stop` is true, and both velocities are `0`

That `rearm` sets `armed` true and leaves the motors stopped. The next accepted motion command is a later datagram. A rejected `rearm` does not refresh the watchdog.

Power-on, brownout, and loss of the MCU are a new boot: new `boot_nonce`, monotonic clock restarted, session cleared, disarmed, cutoff open, sensor `unknown`, clock unsynchronized. Finishing startup does not arm and does not invent a sensor reading. Restoring motor power without resetting the MCU keeps the boot nonce, stops the motors, stays disarmed, opens the cutoff, sets the cutoff hold, rotates freshness, and increments `rearm_generation`. It does not close the cutoff.

### Parsing

A receiver decodes a datagram without raising. Duplicate JSON keys are rejected. `NaN` and infinities are rejected. Integers that the JSON parser refuses, numbers outside the safe integer range, values of the wrong type, and non-finite or noncanonical velocities are validation errors. A validation error is a result, not an exception.

A datagram larger than `8192` bytes is malformed. Nesting deeper than `32` containers is malformed. The decoder catches parser failures, including a recursion error from deeply nested JSON, and returns a malformed result. When a malformed datagram's top-level `boot_nonce` and `session_id` can be read as ordinary integers without a recursive parse, and those values name the active boot and active session, the receiver commands the safe output. That identity scan is not authentication. If the identity cannot be read that way, the receiver ignores the datagram.

The receiver checks the monotonic deadline, the watchdog, and the hardware pet before it parses, unless the firmware is frozen. A frozen firmware does not pet and does not parse into an accepted command. A parse failure cannot refresh or disable the watchdog. The watchdog still fires on a later monotonic advance.

### Receiver procedure

The safe output is `linear_mps` `0`, `angular_rad_s` `0`, and the cutoff not leaving a previous non-zero command in force. Schema 4 does not require a mechanical brake.

On each loop, including a loop with no new datagram, a running firmware pets the cutoff and then applies the deadline and the watchdog. The watchdog fires when more than `250` ms of monotonic time have passed since the last accepted datagram. Firing commands the safe output, sets the fault latch, and clears `armed`. It does not clear the physical stop or the software estop latch, and it does not count as an accepted datagram. A frozen firmware does not pet and does not apply the software motion deadline. After more than `250` ms without a pet, the cutoff opens and holds open even if the firmware's last velocity value is still non-zero. That pet timeout is the modeled hardware cutoff. The software deadline is not.

For a datagram, the running receiver takes the first matching action:

1. The payload cannot be decoded, or it is structurally invalid. This includes the wrong `schema_version`, an unknown or missing field, a bad type, a non-finite, noncanonical, or out-of-range number, a bad MAC or `controller_id`, a stop task that is not stopped, a datagram over `8192` bytes, nesting deeper than `32`, or a lifetime outside `1` to `250` ms. If the payload names the active boot and the active session, command the safe output and do not accept it. Otherwise ignore a payload that cannot be authenticated for this boot. Do not throw.
2. Wrong `boot_nonce`. Ignore the datagram.
3. `session_id` lower than the active session. Ignore the datagram.
4. No active session, or a greater `session_id`. Accept it only when it is a fresh session start for the current `rearm_generation`: sequence `1`, `idle`, `stop` true, both velocities `0`, and not rejected by a trusted clock. Accepting it makes that session active, stores sequence `1`, commands the safe output, clears `armed`, stores `status_challenge`, rotates freshness, and refreshes the watchdog. It does not clear the physical stop or either latch. Any other datagram for that session is ignored.
5. Trusted clock only: `issued_at_ms` is more than `2000` ms ahead. Command the safe output and do not accept.
6. `sequence` less than or equal to the last accepted sequence. An authenticated safety stop is applied now. `estop` sets the software latch and clears `armed`. `fault` sets the fault latch and clears `armed`. This does not refresh the watchdog, does not clear a latch, does not move the sequence backward, and does not arm. An older `rearm` or motion command is ignored.
7. `stop` false. If the challenge or the rearm generation does not match, a trusted clock says the command has expired, less than `1` ms remains before challenge issuance plus `250` ms, the physical stop is asserted, either latch is set, the cutoff is open, the motor bus is unpowered, startup is unfinished, or `armed` is false, command the safe output and do not accept. Do not refresh the watchdog, do not move the sequence, and do not clear a latch or change `armed`. Otherwise apply the canonical velocities, store the monotonic deadline at the earlier of the lifetime deadline and challenge issuance plus `250` ms, accept the sequence, store the challenge, rotate freshness, and refresh the watchdog.
8. A newer safety stop whose challenge has expired, or whose trusted Unix time has expired. Apply the stop effects from step 6. Do not accept the sequence and do not refresh the watchdog. An unfresh `rearm` is rejected and does not arm.
9. `task_state` `"rearm"` with a current challenge. If the generation, the sensor, the cutoff, bus power, or startup blocks it, command the safe output, do not accept, and do not refresh. Otherwise clear both software latches, set `armed` true, command the safe output, accept the sequence, and refresh the watchdog.
10. Any other newer authenticated stop (`idle`, `hold`, `estop`, or `fault`) with a current challenge. Accept it, command the safe output, and refresh the watchdog. `estop` and `fault` set their latches and clear `armed`. `idle` and `hold` do not clear latches and do not clear the physical stop.

Only an accepted datagram refreshes the watchdog. The simulator uses this same procedure. It may show an ignored datagram as ignored. It does not animate that datagram as rover motion.

### Encoding illustration

This is a valid session-start shape. It is not a log from a vehicle. The MAC below is not a value to copy; a real datagram computes it from the provisioned key. `boot_nonce` `1` is only an illustration.

```json
{
  "schema_version": "4",
  "controller_id": "laptop",
  "boot_nonce": 1,
  "session_id": 1795276800100,
  "sequence": 1,
  "freshness_id": 1,
  "rearm_generation": 1,
  "status_challenge": 1,
  "issued_at_ms": 1795276800100,
  "expires_at_ms": 1795276800350,
  "task_state": "idle",
  "linear_mps": 0,
  "angular_rad_s": 0,
  "stop": true,
  "mac": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

The lifetime in the illustration is `250` ms, which is the maximum. This datagram does not arm the receiver. The illustrated MAC does not verify.

## 4. Motor status

Publisher: ESP32 firmware. The simulator publishes the same shape for its own mocked receiver when it is showing a drill, and that drill status cannot arm the physical rover.

Consumers: laptop shared control, the dashboard, and session logging.

The receiver publishes a status message whenever the fields below change, and at least once per `100` ms while it is powered and the firmware is running. `100` ms is an interface rate so the sender can complete a challenge inside the watchdog window. It is not a measured radio rate. A frozen firmware does not publish a fresh status.

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"4"` |
| `controller_id` | string | yes | Provisioned controller |
| `timestamp_ms` | integer | yes | Trusted Unix time, or `0` while `clock_state` is `unsynchronized` |
| `boot_nonce` | integer | yes | Nonce for this firmware boot |
| `session_id` | integer or null | yes | Active session, or `null` before the first accepted session start |
| `last_sequence` | integer or null | yes | Last accepted sequence, or `null` when `session_id` is `null` |
| `status_sequence` | integer | yes | Status sequence for this boot, strictly increasing, with no wrap in the safe integer range |
| `freshness_id` | integer | yes | Current command challenge |
| `rearm_generation` | integer | yes | Current rearm generation |
| `status_challenge` | integer | yes | Challenge from the last accepted command, or `0` before any command is accepted |
| `clock_state` | string | yes | `unsynchronized` or `synchronized` |
| `sensor_state` | string | yes | `asserted`, `released`, or `unknown` |
| `cutoff_open` | boolean | yes | True when the electrical cutoff is open |
| `physical_estop` | boolean | yes | True unless `sensor_state` is `released` |
| `software_estop_latch` | boolean | yes | Software estop latch |
| `fault_latch` | boolean | yes | Watchdog or publisher fault latch |
| `armed` | boolean | yes | True only after an accepted `rearm` while the sensor is released, the cutoff is closed, and both latches are clear |
| `commanding_stop` | boolean | yes | True when the output velocities are both `0` |
| `mac` | string | yes | HMAC-SHA256 of the canonical status |

The status MAC field order is `schema_version`, `controller_id`, `timestamp_ms`, `boot_nonce`, `session_id`, `last_sequence`, `status_sequence`, `freshness_id`, `rearm_generation`, `status_challenge`, `clock_state`, `sensor_state`, `cutoff_open`, `physical_estop`, `software_estop_latch`, `fault_latch`, `armed`, `commanding_stop`.

`armed` true requires a non-null session, `physical_estop` false, `sensor_state` `released`, `cutoff_open` false, and both latches false. Status does not command the motors. A sender cannot clear `physical_estop` by writing this message.

### Publisher status gate

Shared control authorizes motion only from a status that passes every check below. A stored status object is not authorization. The gate validates the whole signed status, including `schema_version` `"4"`, before it accepts the status. A status whose MAC verifies but whose schema does not is rejected.

- The status MAC verifies for the provisioned controller and `controller_id` matches that controller.
- `status_challenge` is the challenge this sender currently has outstanding. Changing that challenge invalidates authorization from the previous status. Sending the same challenge id again does not move its issuance time forward.
- The sender's own monotonic clock has advanced at most `250` ms since it issued that challenge, and at most `250` ms since it received the status. Both ages are required. Either one being older rejects motion.
- `status_sequence` is strictly greater than the highest sequence this sender has already accepted for that `boot_nonce`. The sender persists those high-water marks across its own restarts. A repeated or older status sequence is rejected.
- After the first accepted boot, an armed status from a different boot is rejected. A disarmed status from a new boot may be adopted, and adopting it does not authorize motion.
- For motion, `armed` is true, `cutoff_open` is false, `sensor_state` is `released`, both latches are clear, and `physical_estop` is false.
- `session_id` is the session the sender is using, `freshness_id` is the id the sender will copy into the next command, and the next sequence is greater than `last_sequence`.

`rearm` uses the same current status: a verifying MAC, a valid schema, both ages inside `250` ms, and a challenge that still matches. It also requires the physical stop released, the sensor `released`, the cutoff closed, the status boot and session, the freshness id and `rearm_generation` from that status, a sequence greater than `last_sequence`, and an explicit local operator authorization. It does not require the software estop latch or the fault latch to be clear, and it does not require `armed`. The operator authorization is a local laptop boolean. The rover does not accept it as a network bit. The rover still requires a fresh authenticated `rearm` before it clears those latches.

A cached armed status does not authorize motion after a newer status says disarmed, after either `250` ms window, after the outstanding challenge changes, or after a restart that restored a high-water mark at or above the cached `status_sequence`. A status with a bad MAC, the wrong schema, a wrong boot, a repeated sequence, or a challenge the sender does not currently have outstanding is rejected.

## Publisher duties before motion exists

Until shared control is implemented, no software in this repository publishes a motor command. A later publisher still has these duties:

- Verify an authenticated, fresh, in-order status before any non-zero motion. Do not move from a cached armed status.
- Publish a stop when the newest camera frame is missing, not actionable, or older than `250` ms.
- Publish a stop when the newest gesture label is `unknown`, any frame in its window was not actionable, or `window_end_timestamp_ms` is older than `250` ms.
- Publish `task_state` `"estop"` when the operator asks for a software emergency stop. That datagram does not replace the physical control or the electrical cutoff.
- Publish `rearm` only as its own later datagram, after the operator has asked to arm and a current authenticated status shows the physical control released and the cutoff closed. The software latches do not have to be clear before that datagram.
- Keep `linear_mps` and `angular_rad_s` at `0` whenever `stop` is true.
- Persist `session_id` and per-boot `status_sequence` high-water marks across restarts.

Those duties are interface rules. They are not a claim that a controller, a radio, a cutoff circuit, or the rover has been tested.
