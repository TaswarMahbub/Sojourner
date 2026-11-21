# Sojourner

Sojourner is a four-person WHACK 2026 research prototype. It studies adaptive facial-movement control of a four-motor rover. A camera tracks the face, a classifier reads gestures from those features, and a shared-control state machine publishes motor commands. An ESP32 applies those commands to the rover. A browser simulator consumes the same commands.

Repository: [https://github.com/TaswarMahbub/Sojourner](https://github.com/TaswarMahbub/Sojourner)

This revision contains the repository layout, ownership, schema 2 data contracts, and contract tests. It does not contain perception, classifier, firmware, backend, or frontend application code. It does not contain trained models, datasets, or deployment configuration.

## Architecture

```text
camera
  → perception          MediaPipe facial features and head pose
  → gesture classifier  personalized gesture label
  → shared control      laptop state machine owned by Mridhul
       → ESP32 firmware four motors, limits, stop, and watchdog
       → 2D simulator   same motor command, drawn in the browser
backend                 profiles, sessions, and command logs
```

Perception publishes the camera feature stream. The classifier publishes a gesture label for the feature window it actually used. Shared control is the only publisher of motor commands. The firmware and the simulator are consumers of that command. The backend records profiles and sessions; it does not decide whether the motors move.

Mridhul owns the laptop shared-control state machine. It runs on the companion computer, not on the ESP32. Its code directory is not created yet. Any change that can publish non-zero motion needs a Hardware/ML safety review. Until that code exists, `docs/contracts.md` is the behavior it has to meet.

Real-time stop behavior lives in the motor-command consumer on the ESP32. Writing a command to the database does not replace that watchdog.

## Ownership

| Area | Path | Owner | Responsibility |
| --- | --- | --- | --- |
| Firmware and machine learning | `firmware/`, `ml/` | Hardware/ML | ESP32 firmware, motor control, watchdog, training, and personalized gesture classification |
| Perception and shared control | `perception/` | Mridhul | MediaPipe, facial features, tracking, and the laptop shared-control state machine |
| Contract tests | `tests/` | Author of the interface under test | Schema and mocked protocol checks |
| Backend | `backend/` | Bailey | FastAPI, TigerData, profiles, sessions, and the Vultr deployment |
| Frontend | `frontend/` | Davina | React dashboard, 2D simulator, and visualizations |
| Contracts | `docs/contracts.md` | All owners of the affected stream | Versioned messages between those areas |

TigerData is the PostgreSQL database for profiles and sessions. Vultr is the intended deployment target. Neither is configured in this revision. Connection strings, host names, and API tokens stay in the local environment.

## Data contracts

`docs/contracts.md` is schema 2. Schema 1 is withdrawn.

- Camera feature stream: timestamp, tracking state, named facial features, head pose, and confidence. Stock MediaPipe publishes `confidence: null`.
- Gesture classification: label, confidence, model version, and the source time window.
- Motor command: boot nonce, session, sequence, timestamps, task state, linear and angular motion, and the stop flag.
- Motor status: the receiver's boot nonce, session, latches, physical emergency stop, and armed flag.

Units, ranges, neutral values, and stop behavior are defined there. A producer or consumer that needs behavior the contract does not describe needs a contract change before the code change.

## Development workflow

1. Read `docs/contracts.md` and `CONTRIBUTING.md`.
2. Branch from `main` with the name `<owner>/<area>-<short-description>`.
3. Keep a change inside the area you own unless the other owners have agreed.
4. If the change alters a message, its units, a range, or stop behavior, edit the contract in the same pull request and request the review that `CONTRIBUTING.md` requires.
5. Open a pull request into `main`. After this initial setup, feature work reaches `main` through that pull request.

## Running the contract tests

From the repository root:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m pytest tests
```

The tests check message shape and a software model of the motor-command rules. They use mocked datagrams. They do not power motors, and they do not verify the ESP32 or the physical emergency-stop control. Application directories still have no programs to run.

## Safety scope

Sojourner commands a rover in a research setting. Documentation, interface text, and model cards must not claim clinical effectiveness, diagnosis, or therapeutic benefit. Facial recordings, trained weights, and credentials stay out of git. The safe motion command is stopped: zero linear velocity, zero angular velocity, and no leftover drive from an older command.
