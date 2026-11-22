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

Go and Stop share the jaw so that Stop stays on the most reliable, fastest channel, and so that no incidental movement can start the chair: no other gesture reaches above 0.13 of the jaw threshold. `CueBinding(..., moving_cue=STOP)` is the context-dependent form, and on the stop channel it is the only alternative to plain Stop. The module docstring lists the six known residual risks with their numbers.

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
