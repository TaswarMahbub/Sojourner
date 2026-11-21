# Sojourner data contracts

Document date: 2026-11-21

Schema version: `"2"`

Schema 2 replaces schema 1. No schema 1 producer was published. Consumers implement `"2"` and reject any other `schema_version`. A motor-command consumer that rejects a datagram belonging to its active boot stops the motors and does not apply that datagram. A datagram that does not belong to the active boot is ignored, so a schema 1 datagram cannot rearm motion.

This file is the interface between perception, the gesture classifier, the laptop shared-control state machine, the ESP32 firmware, the 2D simulator, and session logging. Behavior that is not written here is not part of the interface.

Schema 2 is a research control interface for a four-motor rover. It is not a medical-device specification. Confidence values are producer scores when a score exists. They are not clinical probabilities, and no label in this schema has a therapeutic meaning.

The Python modules under `tests/` check this document against mocked messages. They are not firmware and they do not verify the ESP32, the motors, or a physical emergency-stop switch.

## Versioning

Every message carries `schema_version`. For this document the value is the string `"2"`.

A change that removes a field, renames a field, changes a unit, changes a valid range, or changes stop behavior increments `schema_version` to the next integer string and is reviewed under `CONTRIBUTING.md`.

Consumers of schema `"2"` reject a message that contains a field this document does not define for that message. They also reject a missing required field, a value of the wrong JSON type, a non-finite number, or a value outside its valid range. JSON object key order has no meaning. Booleans are not numbers.

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

The gesture label `"unknown"` is not a motion request. The label `"neutral"` means no intentional gesture and is not, by itself, a motion request. Schema 2 does not map gesture labels onto velocities. That mapping requires a later reviewed change.

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
| `schema_version` | string | yes | `"2"` |
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

Each score is a JSON number in `[0, 1]`. For every name except `_neutral`, `0` is the inactive value and is the neutral value used by Sojourner. `1` is the full activation of that shape as defined by MediaPipe. `_neutral` is forwarded so the set matches Face Landmarker. Consumers must not map `_neutral` to a gesture or to motion. Schema 2 does not give `_neutral` a neutral value.

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
  "schema_version": "2",
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
| `schema_version` | string | yes | `"2"` |
| `timestamp_ms` | integer | yes | Time the classification was produced, Unix epoch milliseconds, UTC |
| `source_timestamp_ms` | integer | yes | `timestamp_ms` of the newest feature frame that was an input |
| `window_start_timestamp_ms` | integer | yes | `timestamp_ms` of the oldest feature frame that was an input |
| `window_end_timestamp_ms` | integer | yes | Equal to `source_timestamp_ms` |
| `window_frame_count` | integer | yes | Number of feature messages actually passed into the model, `>= 1` |
| `label` | string | yes | Gesture label, defined below |
| `confidence` | number | yes | Classifier score for `label`, in `[0, 1]` |
| `model_version` | string | yes | Identifier of the weights and label list |

The window is closed: every input frame's `timestamp_ms` satisfies `window_start_timestamp_ms <= timestamp_ms <= window_end_timestamp_ms`. Frames the model did not use are not counted and do not widen the window. Gaps inside the window are allowed. Schema 2 does not set a maximum window duration. Shared control still treats a gesture whose `window_end_timestamp_ms` is more than `250` ms behind the command clock as stale and publishes a stop.

`window_start_timestamp_ms` is less than or equal to `window_end_timestamp_ms`. `source_timestamp_ms` equals `window_end_timestamp_ms`. `timestamp_ms` is greater than or equal to `window_end_timestamp_ms`. For a one-frame model, the start, the end, and `source_timestamp_ms` are the same number and `window_frame_count` is `1`.

If any input frame was not actionable, `label` is `"unknown"`. The classifier does not invent a gesture for `partial` or `lost`.

### Label

`neutral` and `unknown` are reserved in every model version.

| Label | Meaning | Motion |
| --- | --- | --- |
| `neutral` | Every input frame was actionable, and the model assigned no intentional gesture | Does not request motion |
| `unknown` | An input frame was not actionable, the label is not in the reviewed list, or the model cannot assign a class | Does not request motion. Shared control must stop |

Any other label must appear in a reviewed label list for that exact `model_version` before a consumer treats it as known. Schema 2 does not include a label list, because no model is in the repository. A label that is not reserved and has no reviewed list entry is invalid. Consumers handle an invalid label as `unknown` and do not move the rover.

`model_version` matches `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. It names one weight file and one label list. An empty string is invalid. The field has no neutral value.

### Confidence

`confidence` is the classifier's score for the published `label`. It lies in `[0, 1]`. `0` means the producer assigns no score to that label. `1` means the producer reported its maximum score. The score is not a calibrated probability.

There is no neutral confidence. For `unknown`, the producer still sends a score in `[0, 1]`. That score must not be used to override the `unknown` label.

### Encoding illustration

This illustrates a one-frame non-actionable result. It is not model output.

```json
{
  "schema_version": "2",
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
| `schema_version` | string | yes | `"2"` |
| `boot_nonce` | integer | yes | Nonce from the receiver status for this firmware boot |
| `session_id` | integer | yes | Sender session, strictly increasing across sessions from that sender |
| `sequence` | integer | yes | Sequence inside the session, from `1` through `4294967295` |
| `issued_at_ms` | integer | yes | Time the command was created, Unix epoch milliseconds, UTC |
| `expires_at_ms` | integer | yes | Time after which a motion command must not be applied |
| `task_state` | string | yes | One of the task states below |
| `linear_mps` | number | yes | Forward velocity, meters per second |
| `angular_rad_s` | number | yes | Yaw rate, radians per second |
| `stop` | boolean | yes | Stop flag |

There is no field a sender can use to clear the physical emergency stop.

### Sessions on UDP

Each firmware boot chooses a new `boot_nonce` and publishes it in motor status. The nonce stays constant until the next boot. The receiver starts that boot stopped and disarmed. It forgets every previous session because those packets carry the old nonce.

The sender:

1. Waits for a motor status message. It does not invent a `boot_nonce`, and it does not send motion before a status arrives.
2. Copies `boot_nonce` from the newest status.
3. Chooses a `session_id` strictly greater than every session id it has already used. It stores that high-water mark across its own restarts. A Unix-millisecond value is acceptable when it is greater than the stored mark.
4. Sends a session-start datagram: that `boot_nonce`, that `session_id`, `sequence` `1`, `task_state` `"idle"`, `stop` true, and both velocities `0`.
5. Treats the session as active only after status shows the same `boot_nonce` and `session_id` with `last_sequence` at least `1`.
6. Sends `rearm` only when that status shows `physical_estop` false.
7. Sends non-zero motion only when status shows the same boot and session, `physical_estop` false, both latches false, `armed` true, and the new `sequence` is greater than `last_sequence`.

A missing status means the sender must not issue motion. The receiver still enforces the same gate.

Inside one session, `sequence` starts at `1` on the session-start datagram and then increases. Later accepted commands use a strictly greater sequence. Gaps are allowed. Wraparound is not allowed. When the sender would pass `4294967295`, it starts a new session with a greater `session_id` instead of wrapping.

A command is expired when the receiver clock is strictly greater than `expires_at_ms`. A command is too far ahead when `issued_at_ms` is more than `2000` ms ahead of a trusted receiver clock.

The lifetime written into the datagram still has to satisfy:

```text
1 <= expires_at_ms - issued_at_ms <= 250
```

That lifetime is an interface limit. It is not a measured round-trip time. At the linear limit of `0.30` m/s, `250` ms of continued motion would travel `0.075` m, which is why an old motion command is not left applied.

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

These ranges are the schema 2 interface limits. They are not a measured top speed of the rover. Firmware may clamp more tightly. Firmware must not apply a value outside these ranges. A message outside these ranges is invalid.

When `stop` is `true`, both motion fields must be `0`. That is the neutral motor command.

### Three different stop conditions

These conditions are independent. One of them being clear does not clear the others.

| Condition | How it becomes true | What a network `idle` command does | How it becomes false |
| --- | --- | --- | --- |
| Physical emergency stop | The firmware reads the local emergency-stop control as asserted | Nothing. Idle cannot clear it, and the command has no field that clears it | The operator releases that physical control. Releasing it stops the motors and leaves the receiver disarmed. It does not clear a software latch or a fault latch |
| Software estop latch | An `estop` datagram for the active boot and active session is applied, including an older sequence | Nothing. The latch stays set | An accepted, unexpired `rearm` while the physical control is released |
| Fault latch | A `fault` datagram for the active session is applied, or the watchdog expires | Nothing. The latch stays set | The same accepted `rearm` |

The physical control is local to the rover. This schema does not assign a pin or a normally-open or normally-closed contact. Asserted means the firmware has decided the control is in the stop position. Released means the operator has returned it to the run position. Release alone does not arm the motors.

`rearm` is the only datagram that clears the software estop latch and the fault latch, and it does so only when all of the following are true:

- `boot_nonce` matches the current boot
- `session_id` is the active session
- `sequence` is strictly greater than the last accepted sequence
- the datagram is not expired and not too far ahead
- `task_state` is `"rearm"`, `stop` is true, and both velocities are `0`
- the physical emergency stop is released

That `rearm` also sets `armed` true and leaves the motors stopped. The next accepted motion command is a later datagram. A `rearm` while the physical control is asserted is rejected, leaves every latch as it was, and does not refresh the watchdog.

### Receiver procedure

The safe output is `linear_mps` `0`, `angular_rad_s` `0`, and the motor driver not still applying an older non-zero command. The firmware reaches that state by commanding zero wheel effort. Schema 2 does not require a mechanical brake. It does require that the previous non-zero effort is cleared.

On each datagram the receiver checks the watchdog first. The watchdog fires when more than `250` ms have passed since the last accepted datagram. Firing commands the safe output, sets the fault latch, and clears `armed`. It does not clear the physical emergency stop or the software estop latch, and it does not count as an accepted datagram.

The receiver then takes the first matching action:

1. Structurally invalid datagram. This includes the wrong `schema_version`, an unknown or missing field, a bad type, a motion value outside its range, a stop task that is not stopped, or a lifetime outside `1` to `250` ms. If `boot_nonce` and `session_id` name the active boot and the active session, command the safe output and do not accept the datagram. Otherwise ignore it.
2. Wrong `boot_nonce`. Ignore the datagram. An earlier boot cannot refresh the watchdog, clear a latch, arm the receiver, or change the motors.
3. `session_id` lower than the active session. Ignore the datagram. A delayed packet from an earlier session cannot rearm motion or act as a reset.
4. No active session, or a `session_id` greater than the active session. Accept the datagram only when it is a session start: `sequence` is `1`, `task_state` is `"idle"`, `stop` is true, both velocities are `0`, and the datagram is neither expired nor too far ahead. Accepting it makes that session active, stores `last_sequence` `1`, commands the safe output, clears `armed`, and refreshes the watchdog. It does not clear the physical emergency stop, the software estop latch, or the fault latch. Any other datagram for that not-yet-active session is ignored, including motion that arrives before the session start.
5. `issued_at_ms` more than `2000` ms ahead of a trusted receiver clock. Command the safe output and do not accept the datagram.
6. `sequence` less than or equal to the last accepted sequence. If the datagram is a safety stop (`stop` true, both velocities `0`, and `task_state` one of `idle`, `hold`, `estop`, or `fault`), apply it now. `estop` sets the software latch and clears `armed`. `fault` sets the fault latch and clears `armed`. This application does not refresh the watchdog, does not clear any latch, does not move `last_sequence` backward, and does not arm. `rearm` and motion at an older sequence are ignored.
7. `stop` false. If the datagram is expired, the physical emergency stop is asserted, either latch is set, or `armed` is false, command the safe output and do not accept it. Otherwise apply `linear_mps` and `angular_rad_s`, accept the sequence, and refresh the watchdog.
8. Expired datagram with `stop` true and a newer sequence. Apply the stop effects from step 6, including setting a latch when the task is `estop` or `fault`. Do not accept the sequence, do not refresh the watchdog, and do not treat an expired `rearm` as arming.
9. `task_state` `"rearm"`. If the physical emergency stop is asserted, command the safe output, do not accept, and do not refresh. If it is released, clear the software estop latch and the fault latch, set `armed` true, command the safe output, accept the sequence, and refresh the watchdog.
10. Any other newer valid stop (`idle`, `hold`, `estop`, or `fault`). Accept it, command the safe output, and refresh the watchdog. `estop` and `fault` set their latches and clear `armed`. `idle` and `hold` do not clear latches and do not clear the physical emergency stop.

A duplicate or older datagram never refreshes the watchdog. Only an accepted datagram does. Session start, an accepted stop, an accepted `rearm`, and accepted motion are the accepted cases.

The simulator uses this same procedure for the motion it displays. It may show an ignored datagram as ignored. It does not animate that datagram as rover motion.

### Encoding illustration

This is a valid session-start stop for sequence 1. It is not a log from a vehicle. `boot_nonce` `1` is only an illustration; a real boot chooses its own nonce.

```json
{
  "schema_version": "2",
  "boot_nonce": 1,
  "session_id": 1795276800100,
  "sequence": 1,
  "issued_at_ms": 1795276800100,
  "expires_at_ms": 1795276800350,
  "task_state": "idle",
  "linear_mps": 0,
  "angular_rad_s": 0,
  "stop": true
}
```

The lifetime in the illustration is `250` ms, which is the maximum. This datagram does not arm the receiver.

## 4. Motor status

Publisher: ESP32 firmware. The simulator publishes the same shape for its own mocked receiver when it is showing a drill, and that drill status cannot arm the physical rover.

Consumers: laptop shared control, the dashboard, and session logging.

The receiver publishes a status message whenever the fields below change, and at least once per `100` ms while it is powered. `100` ms is an interface rate so the sender can complete the handshake inside the watchdog window. It is not a measured radio rate.

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"2"` |
| `timestamp_ms` | integer | yes | Time the status was composed |
| `boot_nonce` | integer | yes | Nonce for this firmware boot |
| `session_id` | integer or null | yes | Active session, or `null` before the first accepted session start |
| `last_sequence` | integer or null | yes | Last accepted sequence, or `null` when `session_id` is `null` |
| `physical_estop` | boolean | yes | Local physical emergency-stop control is asserted |
| `software_estop_latch` | boolean | yes | Software estop latch |
| `fault_latch` | boolean | yes | Watchdog or publisher fault latch |
| `armed` | boolean | yes | True only after an accepted `rearm` while the physical control is released and both latches are clear |
| `commanding_stop` | boolean | yes | True when the output velocities are both `0` |

`armed` true requires `physical_estop` false, both latches false, and a non-null session. Status does not command the motors. A sender cannot clear `physical_estop` by writing this message.

## Publisher duties before motion exists

Until shared control is implemented, no software in this repository publishes a motor command. A later publisher still has these duties:

- Complete the status handshake before any non-zero motion.
- Publish a stop when the newest camera frame is missing, not actionable, or older than `250` ms.
- Publish a stop when the newest gesture label is `unknown`, any frame in its window was not actionable, or `window_end_timestamp_ms` is older than `250` ms.
- Publish `task_state` `"estop"` when the operator asks for a software emergency stop. That datagram does not replace the physical control, and it does not clear it.
- Publish `rearm` only as its own later datagram, after the physical control is released and the operator has asked to arm.
- Keep `linear_mps` and `angular_rad_s` at `0` whenever `stop` is true.

Those duties are interface rules. They are not a claim that a controller or the rover has been tested.
