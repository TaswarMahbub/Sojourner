# control/

Laptop shared control. The only publisher of motor commands (docs/contracts.md, "Messages").

## Two layers

| Layer | Modules | Decides |
| --- | --- | --- |
| Policy | `cues.py`, `task.py`, `tiers.py`, `confidence.py`, `task_controller.py` | What motion to **request** |
| Authority | `publisher.py`, `status_gate.py`, `session.py`, `auth.py`, `durable.py`, `transport.py`, `reset.py` | Whether anything is **allowed** to move |

The policy layer produces at most one `MotionRequest` per tick and passes it to `Publisher.tick()`. It never builds, seals or sends a datagram, and it never touches the status gate or the session. `Publisher.tick()` still enforces the status gate, both latches, the rearm generation, the freshness binding, perception staleness, the reviewed-label rule, the velocity ranges and every stop condition. If the policy layer is wrong, the system is still safe: a bad request can be refused, but it cannot get around a check. `tests/test_task_controller.py` checks this structure with an AST test.

The policy layer is the gesture-to-velocity mapping that the contract says "requires a later reviewed change". It needs the Hardware/ML safety review before merge.

## Collaborative autonomy model

The user decides and the system steers. The chair is always running a task. Hold (stopped) is the default task, and it is also the state after any stop. A cue replaces or changes the task, and the task then runs on its own. Nobody has to hold a position to keep moving.

| Cue | Effect |
| --- | --- |
| Stop | Hold. Beats every other cue in the same tick. Never queued |
| Cancel | Hold, and drops any proposal. Beats everything except Stop |
| Go | Forward at the speed setting |
| Left / Right | One 15° heading increment, then the previous task continues. While in Hold, the increment waits and runs when Go starts |
| Slower / Faster | Speed setting ± 0.05 m/s, clamped to [0.05, 0.30] |
| Through | Proposes a committed straight run at ≤ 0.10 m/s. It starts only after Confirm, and only Stop or Cancel ends it. **This is not doorway detection.** There is no obstacle sensing in this build |
| Confirm | Accepts the pending proposal. Does nothing if there is none |

The task also returns to Hold when the signal is lost (frame or gesture missing, invalid, stale or `unknown`), when confidence falls below the floor, and whenever the publisher fails to send a requested motion as motion. After any of these, the user has to cue again.

## Tiers

All three tiers produce the same `Cue` stream (`tiers.py`). The calibration profile supplies the tier, the bindings, the thresholds and the deadbands. Two things the profile does not carry are passed in explicitly:

- **`cue_map`**: which side of which discrete channel means which cue. Tier 2 needs it. Each entry is a `CueBinding(channel, "above" | "below", cue)`, where the direction is relative to the channel's measured rest mean, so one channel can carry two different cues (one per side). The plain form `{channel: cue}` still works and binds the side calibration measured. A side can be bound only if its threshold lies outside the deadband. For a channel bound on both sides, the deadband has to fit entirely between the two thresholds; a binding that overlaps is refused. The profile's `stop_binding` channel is always Stop, and nothing else may be bound to either side of it.
- **Crossings must be sustained.** A crossing needs two consecutive frames past the threshold, matching calibration's `min_sustained_frames`, so control detects exactly what calibration counted. A single-frame spike is not a cue.
- **Scan options and `dwell_ms`**: Tier 3 only. The default dwell is 1500 ms, which is provisional.

**The chosen cue map** is in `cue_map.py` and comes from probe session 20261122T073621Z:

| Channel and side | Cue |
| --- | --- |
| jaw open | Go while the task is Hold, Stop while it is moving |
| head lift | Faster |
| head down | Slower |
| turn left | Left |
| turn right | Right |
| roll | unbound |

Go and Stop share the jaw so that Stop stays on the most reliable, fastest channel, and so that no incidental movement can start the chair: no other gesture reaches above 0.13 of the jaw threshold. `CueBinding(..., moving_cue=STOP)` is the context-dependent form, and on the stop channel it is the only alternative to plain Stop. The module docstring lists the seven known residual risks with their numbers.

**Go lockout.** Any stop the user did not cue starts a 2.5 s lockout (`GO_LOCKOUT_MS` in `task_controller.py`). That covers signal loss, low confidence, and the publisher not sending a requested motion (which includes a fault and an estop). During the lockout the jaw reads as Stop, never Go, and no cue from any source can take the task out of Hold. The lockout also resets the pending turn and the speed setting to defaults when it starts and again when it ends. It is there because the jaw's meaning follows the task state, and right after an uncued stop the task is Hold while the chair may still look like it is moving. Run `python tests/run_doorway_e2e.py [probe_report.json]` to see the map end to end.

If a Tier 1 profile binds the same channel as both Stop and a proportional role, the controller refuses it. A real Tier 1 enrolment from a local calibration run did exactly that: `jawOpen` was both `stop_binding` and `proportional_primary`. For Tier 1, perception (`perception/calibration.py`) should pick a stop channel separate from the proportional channels.

## Required before any classifier label can be a cue

`cues.py` defines the cue vocabulary keyed to the placeholder model_version `cue-vocabulary-proposed-1`. **This list is proposed, not reviewed.** docs/contracts.md section 2 says a non-reserved label is invalid until a reviewed list for that exact `model_version` exists. Until that happens, two checks still reject every cue label: `tests/contract_schema.py` (`validate_gesture`) and `publisher.py` (`REVIEWED_LABELS`). The controller and the publisher both stop on such a gesture.

To make labels effective, one reviewed change must:

1. add the label list and its `model_version` to `docs/contracts.md`, agreed with the ML owner;
2. update `validate_gesture` in `tests/contract_schema.py` to accept that list for that version only;
3. update `REVIEWED_LABELS` in `publisher.py` to match;
4. make sure the classifier is trained against exactly that list.

Until then, cues come only from the calibrated channels through the tiers.

## Provisional numbers

Speed step and limits, turn increment and rate, Through speed, scan dwell, and the confidence curve (`confidence.py`: stop below 0.40, 0.25 to 1.0 linear up to 0.90, rises limited to 1.0 per second) are all provisional. None of them has been tuned on the rover.

## Running

```
python -m pytest tests -q
python tests/run_task_controller_e2e.py
```

The scripted run drives the publisher against the `MotorReceiver` contract oracle. It is not firmware and it is not a hardware test.

## Evidence: stop latency, failure drills, replay

These produce transcripts and numbers rather than features. All of them drive the unmodified controller and publisher through `tests/session_harness.py`, against the in-process `MotorReceiver` contract oracle, so none of them is a hardware test.

```
python tests/run_live_e2e.py --probe-report PROBE_REPORT.json [--model face_landmarker.task] [--seconds 60]
python tests/run_replay.py captures/control/live_<UTC>.jsonl [--fast]
python tests/drills.py a|b|c|d|e|f|all
```

**Stop latency** (`latency.py`). For each stop of a moving chair, the time from evidence to torque off, split into four parts. A cued stop is measured from the capture of the first frame past the stop threshold. A signal-loss stop is measured from the last actionable frame. The parts are: frame to cue (or loss) detected, cue to request, request to datagram sent, and datagram to receiver torque off. The summary gives the median, worst case and count of each part and of the total, compares the total with the project report's 150 ms budget, and names the part that spends the most. Two parts are over budget by design and not tuned: a sustained crossing needs two frames, so at 20 fps one frame interval is always spent, and a camera that stops delivering is caught only once the newest frame is older than 250 ms. A stop that could not be measured is listed with the reason. "Datagram to torque off" is the oracle's handling time, not the rover's: there is no radio hop and no ESP32 in it.

**Drills** (`tests/drills.py`, also run as tests by `tests/test_drills.py`): (a) camera disconnected, (b) face leaves the frame, (c) light drops, (d) a deliberate wrong cue on the unbound roll axis, (e) operator estop, rearm and recovery, (f) laptop process restart. Each drill prints a transcript, asserts its outcomes and ends with one verdict line. Drill f shows the status gate across a restart. The high-water marks survive the kill, process 1's signed status and the receiver's current status are both refused until a new challenge above the persisted mark has been issued and echoed, motion also waits for an operator rearm, and eight kinds of corrupt state all lock the gate. Drill f also records two limits. First, with a corrupt gate file the laptop cannot send even an estop, because it holds no durable challenge id; the receiver's watchdog and the physical estop are what stop the chair. Second, an older but valid gate file restored into a fresh process is accepted, because the rollback check compares against marks held in memory and a fresh process holds none.

**Replay** plays a recorded session's frames back at their recorded arrival offsets through a fresh controller, publisher, gate and receiver. Every line and the summary are prefixed `REPLAY`. Frames are re-stamped to replay time, and detector inference is not re-run.

**Speed setting.** Every transcript line shows `v`, and the summary lists each speed change, flagging the ones made in Hold (residual risk 7 in `cue_map.py`). The setting is not reset automatically.
