# Sojourner

Sojourner is a four-person WHACK 2026 research prototype. It studies adaptive facial-movement control of a four-motor rover. A camera tracks the face, a classifier reads gestures from those features, and a shared-control state machine publishes motor commands. An ESP32 applies those commands to the rover. A browser simulator consumes the same commands.

Repository: [https://github.com/TaswarMahbub/Sojourner](https://github.com/TaswarMahbub/Sojourner)

This revision contains the repository layout, ownership, and versioned data contracts. It does not contain application code, trained models, datasets, or deployment configuration.

## Architecture

```text
camera
  → perception          MediaPipe facial features and head pose
  → gesture classifier  personalized gesture label
  → shared control      task state and one motor command
       → ESP32 firmware four motors, limits, stop, and watchdog
       → 2D simulator   same motor command, drawn in the browser
backend                 profiles, sessions, and command logs
```

Perception publishes the camera feature stream. The classifier publishes a gesture label for one feature frame. Shared control is the only publisher of motor commands. The firmware and the simulator are consumers of that command. The backend records profiles and sessions; it does not decide whether the motors move.

The shared-control state machine does not have an owner yet. It runs on the companion computer, not on the ESP32. The team will name its owner before any control code is added. Until then, `docs/contracts.md` is the behavior that code has to meet.

Real-time stop behavior lives in the motor-command consumer on the ESP32. Writing a command to the database does not replace that watchdog.

## Ownership

| Area | Path | Owner | Responsibility |
| --- | --- | --- | --- |
| Firmware and machine learning | `firmware/`, `ml/` | Hardware/ML | ESP32 firmware, motor control, watchdog, training, and personalized gesture classification |
| Perception | `perception/` | Mridhul | MediaPipe, facial features, tracking, and integration of the feature stream |
| Backend | `backend/` | Bailey | FastAPI, TigerData, profiles, sessions, and the Vultr deployment |
| Frontend | `frontend/` | Davina | React dashboard, 2D simulator, and visualizations |
| Contracts | `docs/contracts.md` | All owners of the affected stream | Versioned messages between those areas |
| Cross-component tests | `tests/` | Author of the interface under test | Checks that span more than one area |

TigerData is the PostgreSQL database for profiles and sessions. Vultr is the intended deployment target. Neither is configured in this revision. Connection strings, host names, and API tokens stay in the local environment.

## Data contracts

`docs/contracts.md` is schema 1 for three messages:

- Camera feature stream: timestamp, tracking state, named facial features, head pose, and confidence.
- Gesture classification: label, confidence, and model version.
- Motor command: sequence number, timestamps, task state, linear and angular motion, and the stop flag.

Units, ranges, neutral values, and stop behavior are defined there. A producer or consumer that needs behavior the contract does not describe needs a contract change before the code change.

## Development workflow

1. Read `docs/contracts.md` and `CONTRIBUTING.md`.
2. Branch from `main` with the name `<owner>/<area>-<short-description>`.
3. Keep a change inside the area you own unless the other owners have agreed.
4. If the change alters a message, its units, a range, or stop behavior, edit the contract in the same pull request and request the review that `CONTRIBUTING.md` requires.
5. Open a pull request into `main`. After this initial setup, feature work reaches `main` through that pull request.

## Running locally

There is nothing to install or run yet. The module directories explain what each owner will add. The first pull request that adds executable code also adds the command that runs it and the tests for that area.

## Safety scope

Sojourner commands a rover in a research setting. Documentation, interface text, and model cards must not claim clinical effectiveness, diagnosis, or therapeutic benefit. Facial recordings, trained weights, and credentials stay out of git. The safe motion command is stopped: zero linear velocity, zero angular velocity, and no leftover drive from an older command.
