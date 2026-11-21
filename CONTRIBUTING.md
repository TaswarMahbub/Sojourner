# Contributing to Sojourner

Sojourner is a research prototype. Changes land through a reviewed pull request. `main` is the integration branch.

The current tree is documentation and empty module directories. Pull requests that add behavior also add the tests described below.

## Branch naming

Start from an up-to-date `main`.

```text
<owner>/<area>-<short-description>
```

Use lowercase letters, numbers, and hyphens. Keep the description short and specific.

| Owner | Branch prefix | Areas |
| --- | --- | --- |
| Hardware/ML | `hardware/` | `ml`, `firmware` |
| Mridhul | `mridhul/` | `perception`, and the laptop shared-control state machine |
| Bailey | `bailey/` | `backend` |
| Davina | `davina/` | `frontend` |

Use the area you are changing: `perception`, `ml`, `firmware`, `backend`, `frontend`, `docs`, or `tests`. A contract change uses `docs`.

Examples:

- `mridhul/perception-feature-stream`
- `hardware/ml-label-map`
- `hardware/firmware-watchdog`
- `bailey/backend-sessions`
- `davina/frontend-simulator`
- `hardware/docs-motor-stop-rule`

One branch addresses one change. Do not combine a firmware motor change with an unrelated dashboard change.

## Pull request workflow

1. Rebase or merge the latest `main` before asking for review.
2. Fill in the pull request body with:
   - a short description of the change
   - the paths touched
   - the contract sections changed, or "none"
   - whether sequence handling, timestamps, units, ranges, task state, stop behavior, or the watchdog changed
   - how you tested the change
   - confirmation that the pull request adds no credentials, face recordings, datasets, or model weights
3. Request one other teammate as a reviewer.
4. Request the area owner when the change modifies that area and you are not the owner.
5. Request a safety review from Hardware/ML when any safety-review rule below applies.
6. Merge only after the required reviews are approved. The author does not merge their own safety-reviewed motor or firmware change.
7. Delete the branch after it merges.

Direct commits to `main` are reserved for repository administrators during the initial setup. Feature work uses a pull request.

`.github/workflows/tests.yml` runs `python3 -m pytest tests` on pull requests and on pushes to `main`. Reviewers still read the contract diff. A green workflow does not replace a Hardware/ML safety review and does not verify the rover.

## Testing expectations

- Contract checks run with `python3 -m pytest tests`. Do not add a test that asserts invented accuracy, latency, or clinical performance.
- The first pull request that adds executable application code in an area documents that area's test command in its `README.md` and includes tests for the behavior it adds.
- A pure documentation change says how the author checked it. For `docs/contracts.md`, the author walks through units, ranges, neutral values, and stop behavior in the pull request body.
- A contract change keeps `docs/contracts.md` as the only specification. Examples in that file are encoding illustrations. Tests must not treat them as recorded model output.
- Firmware and motor-command changes describe the bench check of the stop path. State the supply state, whether the wheels were free to turn, and what happened when the command stream stopped. A motor-path change is not merged from code review alone.
- Simulator changes use the motor-command contract. A second, simulator-only command format is a contract change and needs review.
- Backend changes that store commands or features must tolerate a stopped stream and must not become part of the motor watchdog. Add a test that a database outage is separate from the firmware stop path once that code exists.

## Safety-review rules

Hardware/ML reviews and approves the pull request before merge when it does any of the following:

1. Changes motor-command fields, units, valid ranges, sequence rules, expiry, task states, or stop behavior.
2. Changes ESP32 code that drives motors, arms a driver, clears a fault, or implements the watchdog.
3. Weakens a limit, lengthens the 250 ms command lifetime or watchdog deadline, or lets a consumer keep applying an older command.
4. Adds or changes a path that can publish a non-zero motor command, including shared control and any test tool that talks to hardware.
5. Changes how `lost`, `partial`, `unknown`, an invalid message, or an expired message becomes a stop.

These rules stay in force for that review:

- The safe state is stopped. Linear velocity is 0 m/s, angular velocity is 0 rad/s, and the driver is not still applying a previous non-zero command.
- Every new state, mode, and error path names how it reaches that safe state.
- `tracking_state` other than `tracking`, a gesture label of `unknown`, a stale feature frame, an invalid message, and an expired command do not produce motion.
- The firmware may use limits that are stricter than `docs/contracts.md`. It must not exceed them.
- The laptop shared-control state machine is owned by Mridhul. A change that can publish non-zero motion still needs Hardware/ML approval before merge.
- Clearing the software estop latch or a watchdog fault takes its own accepted `rearm` command with `stop` true and zero motion. An `idle` command does not clear those latches. A network command does not clear a physical emergency stop. The physical control must be released, and a later `rearm` is what arms the receiver. The `rearm` command does not also request motion.
- Bench work with the motors powered uses a vehicle that cannot reach a person. The stop path is checked before any non-zero command.

## Credentials, data, and claims

- Do not commit `.env` files, keys, tokens, database URLs, Vultr credentials, or service-account files.
- Do not commit face images, video, exported landmark dumps, or other recordings of a person. Local datasets stay outside the repository.
- Do not commit model weights or training outputs. A reviewed label list may be added under `ml/` when a real model version exists. It lists labels only. It does not invent accuracy figures.
- Do not write clinical, diagnostic, or therapeutic claims into documentation, the dashboard, model cards, or pull requests.
- Do not present a schematic JSON example as a measured result.

## Contract changes

`docs/contracts.md` schema `"2"` is binding. A change that redefines a field, a unit, a range, or stop behavior increments `schema_version` and updates every producer and consumer of that message in the same pull request. While no producer exists, the contract pull request stands alone and names the owners who must review the next implementation.

Owners for a contract edit:

| Message | Primary reviewer | Also review |
| --- | --- | --- |
| Camera feature stream | Mridhul | Hardware/ML, when classifier inputs change |
| Gesture classification | Hardware/ML | Mridhul, when the feature inputs change |
| Motor command and motor status | Hardware/ML, as a safety review | Mridhul, because he owns the laptop publisher. Davina, because the simulator consumes the command. Bailey, when session logging changes |

Consumers reject a `schema_version` they do not implement. A motor datagram that belongs to the active boot and fails validation stops the motors. A datagram from another boot or an earlier session is ignored.
