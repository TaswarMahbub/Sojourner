# Software-only live demo

Camera → calibrated cues → shared control → receiver oracle, shown live in the 3D corridor.
**No hardware.** The physical rover is never addressed: no UDP socket is opened, and the only
key is the labelled test key from `tests/control_auth.py`.

## What runs where

| Piece | Where it runs | Real or simulated |
| --- | --- | --- |
| Camera + Face Landmarker | `demo/run_live_sim.py`, one process, opened once | Live (or synthetic frames with `--synthetic`) |
| Calibrated cues, task controller, publisher, status gate | same process, unmodified `control/` through `tests/session_harness.py` | Real laptop software |
| Receiver that closes the loop | `tests/motor_receiver.py` contract oracle, in process | Software oracle, not firmware |
| Motor mirror bridge | `demo/motor_bridge.py`, `ws://127.0.0.1:8767` (`SOJOURNER_MOTOR_BRIDGE_PORT`) | Forwards every receiver call and every datagram, byte-identical, unfiltered |
| 3D rover | `frontend/live.html`: its own unmodified `src/sim/motorReceiver.js` re-judges every datagram | Simulated rover, moves only on output that receiver applied |
| Dashboard camera panels | `frontend/` main page, `ws://127.0.0.1:8765` relayed by the demo process | Live frames, same wire format as `perception.publish` |

The browser receiver is the parity-tested port of the Python oracle. The readout's `parity` line
counts how many calls it agrees with the oracle on; a disagreement is shown, never hidden. A
gap or bad record drops the mirror: the page then shows the rover as unknown, not as moving.

## Run it

Terminal 1, the 3D mirror (and the dashboard):

```zsh
cd frontend && npm ci && npm run dev
# open http://localhost:5173/live.html   (3D mirror)
# open http://localhost:5173/            (dashboard: camera, head pose, tracking panels)
```

Terminal 2, from the repository root. **Fallback, no camera** (scripted synthetic frames, about 30 s,
then the bridge keeps serving the session for late browsers until Ctrl-C). Synthetic frames are not
relayed to the dashboard on 8765, so nothing there can be mistaken for a camera:

```zsh
python3 -m demo.run_live_sim --synthetic
```

The script shows: Go (jaw), travel, a 15° left turn, Stop (jaw), Go, a forged-MAC copy of a
live datagram (refused, safe output commanded, then the next genuine datagram resumes), a
face-loss stop with the 2.5 s Go lockout, Go, then a dropped link: the receiver watchdog halts
and latches the rover (`halted watchdog`).

**Live camera** (needs `pip install -r perception/requirements.txt` and
`perception/models/face_landmarker.task`). Record a probe session once, then run:

```zsh
python3 -m perception.probe_session --source camera --user demo
# writes captures/calibration/<user>_<session>_probe_report.json
python3 -m demo.run_live_sim --probe-report captures/calibration/<...>_probe_report.json --seconds 300
```

Jaw open = Go from Hold and Stop while moving; head lift/down = faster/slower; turn left/right =
15° heading increments (`control/cue_map.py`). Looking away stops the rover (signal loss).

Add `--record captures/control/live_sim_<name>.jsonl` to keep the measured frames, then replay them
through the same pipeline and the browser mirror (labelled REPLAY, nothing re-detected):

```zsh
python3 -m demo.run_live_sim --replay captures/control/live_sim_<name>.jsonl --probe-report <report>
python3 -m demo.run_live_sim --replay captures/calibration/<...>_probe.jsonl --probe-report <report>
```

### Head gestures: what the measured data shows

- **Turn your head about 25–35°, hold about 1 s, and come back.** One Left/Right cue is one 15°
  heading increment at 0.5 rad/s, so it takes about 0.5 s. In the probe recording, MediaPipe lost
  the face every time the head went past about 44–58° of yaw. Losing the face is a signal-loss Stop
  with a 2.5 s Go lockout, and the lockout discards the pending turn: a large turn stops the rover
  instead of turning it.
- **Head gestures only change the task.** In Hold a turn waits for Go and a speed change shows
  nothing; Faster/Slower step the speed by 0.05 m/s. Watch the readout's `yaw` and `output` lines
  and the top-down inset: the chase camera turns with the rover, so a 15° turn looks small in 3D.
- **Sit for the calibration exactly as for the demo.** A side of a channel arms only after the value
  has been back inside its deadband. If your resting head pose is outside the calibrated deadband
  (for example, resting pitch above `deadband.upper`), that side never arms and the cue never fires.

Do **not** run `python -m perception.publish` at the same time: the demo process owns the camera
and relays its frames on 8765 itself (`--no-frame-relay` turns that off).

## Tests

```zsh
python3 -m pytest -q tests/test_live_sim_bridge.py
cd frontend && npx vitest run src/live
python3 frontend/scripts/live_mirror_fixture.py   # regenerate the mirror fixture (deterministic)
```
