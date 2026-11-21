# Sojourner data contracts

Document date: 2026-11-21

Schema version: `"1"`

This file is the schema 1 interface between perception, the gesture classifier, shared control, the ESP32 firmware, the 2D simulator, and session logging. Code that produces or consumes these messages follows this file. Behavior that is not written here is not part of the interface.

Schema 1 is a research control interface for a four-motor rover. It is not a medical-device specification. Confidence values are producer scores. They are not clinical probabilities, and no label in this schema has a therapeutic meaning.

## Versioning

Every message carries `schema_version`. For this document the value is the string `"1"`.

A change that removes a field, renames a field, changes a unit, changes a valid range, or changes stop behavior increments `schema_version` to the next integer string and is reviewed under `CONTRIBUTING.md`. Consumers reject a `schema_version` they do not implement. A motor-command consumer that rejects a message stops the motors and does not apply it.

Consumers of schema `"1"` reject a message that contains a field this document does not define for that message. They also reject a missing required field, a value of the wrong JSON type, a non-finite number, or a value outside its valid range. JSON object key order has no meaning.

Timestamps are integer Unix epoch milliseconds in UTC. They are JSON numbers in the safe integer range, from `0` through `9007199254740991`. A timestamp is not a formatted date string.

## Messages

```text
camera feature stream  →  gesture classification  →  motor command
                                                          ↓
                                              ESP32 firmware and 2D simulator
```

Shared control is the only publisher of motor commands. Perception does not publish motor commands. The classifier does not publish motor commands. The simulator does not publish a second command format.

An actionable camera frame has `tracking_state` `"tracking"`, a `confidence` inside `(0, 1]`, every required blendshape, and a valid `head_pose`. Every other camera frame is not actionable. The gesture label `"unknown"` is not a motion request. The label `"neutral"` means no intentional gesture and is not, by itself, a motion request. Schema 1 does not map gesture labels onto velocities. That mapping requires a later reviewed change.

## 1. Camera feature stream

Publisher: perception (Mridhul).

Consumers: the gesture classifier, the dashboard, and session logging. Shared control may also read tracking state so it can stop when the face is not actionable.

One message describes at most one face. Zero detected faces produce `tracking_state` `"lost"`. This schema does not define how to choose among multiple faces. If the detector returns more than one face, the producer emits `"lost"`.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"1"` |
| `timestamp_ms` | integer | yes | Capture time of the camera frame, Unix epoch milliseconds, UTC |
| `tracking_state` | string | yes | `"tracking"`, `"partial"`, or `"lost"` |
| `features` | object | yes | Named blendshape scores. See below |
| `head_pose` | object or null | yes | Head pose. `null` unless `tracking_state` is `"tracking"` |
| `confidence` | number | yes | Overall face score in `[0, 1]` |

`timestamp_ms` is the capture time of the camera frame this message describes. It is not the time the message was later written to the database.

### Tracking state

| State | Meaning | `features` | `head_pose` | `confidence` | Actionable |
| --- | --- | --- | --- | --- | --- |
| `tracking` | Exactly one face, and blendshapes and pose passed validation | All 52 names, each in `[0, 1]` | Valid pose object | `(0, 1]` | Yes |
| `partial` | A face was seen, but blendshapes or pose failed validation | Any subset, including empty. Present values must still lie in `[0, 1]` | `null` | `[0, 1]` | No |
| `lost` | No usable face | Empty object `{}` | `null` | `0` | No |

### Named facial features

`features` uses the 52 Face Landmarker blendshape names. `*Left` and `*Right` are the subject's left and right, which is the MediaPipe definition. The producer publishes names for the image buffer it passed to Face Landmarker. A mirrored preview on screen does not rename the scores. If that buffer was mirrored, the producer corrects the left and right names before publishing so the message stays subject-relative.

Each score is a JSON number in `[0, 1]`. For every name except `_neutral`, `0` is the inactive value and is the neutral value used by Sojourner. `1` is the full activation of that shape as defined by MediaPipe. `_neutral` is forwarded so the set matches Face Landmarker. Consumers must not map `_neutral` to a gesture or to motion. Schema 1 does not give `_neutral` a neutral value.

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

`confidence` is the minimum of the Face Landmarker detection, presence, and tracking scores that the producer actually received for the frame. If none of those scores is available, the producer sends `lost` and `confidence` `0`.

`0` means the producer reports no face confidence. `1` means every score included in the minimum was `1`. The value is not a calibrated probability and is not a measure of gesture-classification accuracy.

### Encoding illustration

This fragment shows field types for a lost face. It is not a recorded frame.

```json
{
  "schema_version": "1",
  "timestamp_ms": 1795276800000,
  "tracking_state": "lost",
  "features": {},
  "head_pose": null,
  "confidence": 0
}
```

A `tracking` message uses the same fields, with all 52 feature names, a `head_pose` object, and a non-zero confidence. The neutral orientation inside that object is `"yaw_deg": 0`, `"pitch_deg": 0`, `"roll_deg": 0`. The matrix of a real face also carries translation and scale, so identity-with-zeros is not a required tracking example.

## 2. Gesture classification

Publisher: the gesture classifier (Hardware/ML).

Consumers: shared control, the dashboard, and session logging.

One message classifies one camera feature frame.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"1"` |
| `timestamp_ms` | integer | yes | Time the classification was produced, Unix epoch milliseconds, UTC |
| `source_timestamp_ms` | integer | yes | `timestamp_ms` of the camera feature message that was classified |
| `label` | string | yes | Gesture label, defined below |
| `confidence` | number | yes | Classifier score for `label`, in `[0, 1]` |
| `model_version` | string | yes | Identifier of the weights and label list |

`timestamp_ms` must be greater than or equal to `source_timestamp_ms`.

### Label

`neutral` and `unknown` are reserved in every model version.

| Label | Meaning | Motion |
| --- | --- | --- |
| `neutral` | The source frame was actionable, and the model assigned no intentional gesture | Does not request motion |
| `unknown` | The source frame was not actionable, the label is not in the reviewed list, or the model cannot assign a class | Does not request motion. Shared control must stop |

Any other label must appear in a reviewed label list for that exact `model_version` before a consumer treats it as known. Schema 1 does not include a label list, because no model is in the repository. A label that is not reserved and has no reviewed list entry is invalid. Consumers handle an invalid label as `unknown` and do not move the rover.

`model_version` matches `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. It names one weight file and one label list. An empty string is invalid. The field has no neutral value.

### Confidence

`confidence` is the classifier's score for the published `label`. It lies in `[0, 1]`. `0` means the producer assigns no score to that label. `1` means the producer reported its maximum score. The score is not a calibrated probability.

There is no neutral confidence. For `unknown`, the producer still sends a score in `[0, 1]`. That score must not be used to override the `unknown` label.

A source frame that is not actionable produces `label` `"unknown"`. The classifier does not invent a gesture for `partial` or `lost`.

### Encoding illustration

This illustrates a non-actionable result. It is not model output.

```json
{
  "schema_version": "1",
  "timestamp_ms": 1795276800040,
  "source_timestamp_ms": 1795276800000,
  "label": "unknown",
  "confidence": 0,
  "model_version": "none"
}
```

`model_version` `"none"` is a legal identifier for a producer that is not yet running a trained model. It still has no labels beyond `neutral` and `unknown`.

## 3. Motor command

Publisher: shared control only.

Consumers: ESP32 firmware and the 2D simulator. The backend may store a copy. Storage is not a consumer of the real-time stop path.

The command uses the rover body frame from ROS REP-103. Positive X is forward, positive Y is left, and positive Z is up. `linear_mps` is translation along X. `angular_rad_s` is yaw rate about Z. Positive yaw is a left turn, counterclockwise when viewed from above.

The wheel mapping for the four motors is not part of this message. Firmware owns that mapping.

### Fields

| Field | JSON type | Required | Definition |
| --- | --- | --- | --- |
| `schema_version` | string | yes | `"1"` |
| `sequence` | integer | yes | Command sequence number |
| `issued_at_ms` | integer | yes | Time the command was created, Unix epoch milliseconds, UTC |
| `expires_at_ms` | integer | yes | Time after which the command must not be applied |
| `task_state` | string | yes | One of the task states below |
| `linear_mps` | number | yes | Forward velocity, meters per second |
| `angular_rad_s` | number | yes | Yaw rate, radians per second |
| `stop` | boolean | yes | Stop flag |

### Sequence

`sequence` is an integer from `1` through `4294967295`. The first command on a connection uses `1` or any integer in that range. Each later accepted command on that connection uses a strictly greater sequence. Gaps are allowed. Wraparound is not allowed. When the publisher would pass `4294967295`, it opens a new connection instead.

The receiver stores the last accepted sequence for the connection. A message with `sequence` less than or equal to that value is not accepted. A duplicate does not refresh the watchdog and does not change the motors. After a new connection, the receiver forgets the previous sequence. The first command it accepts on the new connection has `stop` true.

Only a valid, unexpired message is accepted. An invalid or expired message does not advance the stored sequence. The publisher may correct it and send the same sequence again, or it may send a greater sequence. Either way, the receiver has already applied the stop behavior below.

### Timestamps

`expires_at_ms` is greater than `issued_at_ms`. Their difference is the command lifetime:

```text
1 <= expires_at_ms - issued_at_ms <= 250
```

The lifetime is an interface limit. It is not a measured round-trip time. At the linear limit of `0.30` m/s, `250` ms of continued motion would travel `0.075` m, which is why an old command is not left applied.

A receiver that has a clock rejects a command whose `expires_at_ms` is already in the past, and rejects a command whose `issued_at_ms` is more than `2000` ms ahead of the receiver clock. A receiver without a trusted clock still enforces the watchdog below.

### Task state

| `task_state` | Meaning | Motion fields | `stop` |
| --- | --- | --- | --- |
| `idle` | No active task | Both `0` | `true` |
| `manual` | Operator input is the only motion source | Inside the ranges below | `false` to move, `true` to stop |
| `shared` | The message already combines operator and assist input | Inside the ranges below | `false` to move, `true` to stop |
| `hold` | The task is retained and motion is held | Both `0` | `true` |
| `estop` | Latched emergency stop | Both `0` | `true` |
| `fault` | The publisher or a previous check failed | Both `0` | `true` |

A message that does not match its row in this table is invalid. `manual` and `shared` are the only states that may request non-zero motion, and only when `stop` is `false`. When `stop` is `true`, both motion fields are `0` in every state. Firmware does not add assist on top of the message. Shared control has already combined the sources before publishing.

### Linear and angular motion

| Field | Unit | Valid range | Neutral value | Positive direction |
| --- | --- | --- | --- | --- |
| `linear_mps` | meters per second | `[-0.30, 0.30]` | `0` | Forward, body +X |
| `angular_rad_s` | radians per second | `[-1.00, 1.00]` | `0` | Left turn, positive yaw about body +Z |

These ranges are the schema 1 interface limits. They are not a measured top speed of the rover. Firmware may clamp more tightly. Firmware must not apply a value outside these ranges. A message outside these ranges is invalid.

When `stop` is `true`, both motion fields must be `0`. That is the neutral motor command.

### Stop behavior

The safe state is stopped: `linear_mps` is `0`, `angular_rad_s` is `0`, and the motor driver is not still applying an older non-zero command. The firmware reaches that state by commanding zero wheel effort. Schema 1 does not require a mechanical brake. It does require that the previous non-zero effort is cleared.

An accepted command with `task_state` `"estop"` sets the estop latch. The watchdog sets a fault latch. Opening a new connection clears the stored sequence and does not clear either latch.

The receiver takes the first matching action and then stops reading the list:

1. Stale sequence. If `sequence` is less than or equal to the last accepted sequence on this connection, ignore the message. Leave the motors, the watchdog timer, and any latch as they are.
2. Invalid or expired message. This includes a wrong `schema_version`, a field this schema does not define, a missing field, a motion value outside its range, `stop` true with a non-zero motion field, a task state that does not match its table row, a lifetime outside `1` to `250` ms, `expires_at_ms` already in the past, or `issued_at_ms` more than `2000` ms ahead of a trusted receiver clock. Do not accept the message. Go to the safe state. Leave the stored sequence unchanged.
3. Latch set. If either latch is set, accept only a newer valid command with `task_state` `"idle"`, `stop` true, and both motion fields `0`. Accepting it applies the safe state, clears the latch, and refreshes the watchdog. Every other message is not accepted, and the motors stay stopped.
4. First command requests motion. If this connection has not yet accepted a command, and this message has `stop` false or a non-zero motion field, do not accept it. Go to the safe state.
5. Valid stop. If `stop` is true and both motion fields are `0`, accept the command, go to the safe state, and refresh the watchdog. A `task_state` of `"estop"` sets the estop latch.
6. Valid motion. If `task_state` is `"manual"` or `"shared"`, `stop` is false, and both motion fields are inside their ranges, accept the command, apply those velocities, and refresh the watchdog.

Watchdog: if `250` ms pass with no accepted command, the receiver goes to the safe state and sets the fault latch. A stream of accepted stop commands keeps the connection alive without motion, because each accepted command refreshes the timer.

The command that clears a latch has zero motion. A later command may request motion only after that latch is clear.

The simulator uses these same acceptance and latch rules for the motion it displays. It may show a rejected message as rejected. It does not animate that message as rover motion.

### Encoding illustration

This is a valid stop command for sequence 1. It is not a log from a vehicle.

```json
{
  "schema_version": "1",
  "sequence": 1,
  "issued_at_ms": 1795276800100,
  "expires_at_ms": 1795276800350,
  "task_state": "idle",
  "linear_mps": 0,
  "angular_rad_s": 0,
  "stop": true
}
```

The lifetime in the illustration is `250` ms, which is the maximum.

## Producer duties before motion exists

Until shared control is implemented, no software in this repository publishes a motor command. A later publisher still has these duties:

- Publish a stop when the newest camera frame is missing, not actionable, or older than `250` ms.
- Publish a stop when the newest gesture label is `unknown` or the gesture message is older than `250` ms.
- Publish `estop` when a person requests an emergency stop, and leave the latch to the receiver rules above.
- Keep `linear_mps` and `angular_rad_s` at `0` whenever `stop` is true.

Those duties are interface rules. They are not a claim that a controller has been tested.
