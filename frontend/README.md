# Frontend

Owner: Davina.

This directory will hold the React application: the operator dashboard, the 2D rover simulator, and the visualizations of features, gestures, and commands.

Planned responsibilities:

- Show tracking state, named features, head pose, gesture label, confidence, and model version.
- Drive the 2D simulator from the motor-command contract, including sequence, task state, and the stop flag.
- Display a rejected or expired command as not applied. The simulator uses the same acceptance rules as the firmware.
- Keep on-screen wording free of clinical, diagnostic, and therapeutic claims.

The simulator is a view of the command stream. It does not publish a second command format and it does not bypass a stop.

## Commands

Run these from `frontend/`. Node.js 20.19+ or 22.12+ is required (the Vite requirement).

| Command | What it does |
| --- | --- |
| `npm install` | Install dependencies |
| `npm run dev` | Start the dashboard at http://localhost:5173 |
| `npm test` | Run the Vitest unit tests once |
| `npm run lint` | Run ESLint |
| `npm run build` | Build the production bundle into `dist/` |

The tests in `src/contracts/` check the frontend against schema `"4"` in `docs/contracts.md`. They also decode perception's sample frames in `perception/fixtures/frame_*.json` and require zero errors.

## Camera data: mock or live

By default the dashboard uses a mock camera source, labelled "MOCK DATA". It is generated in the browser, not measured from a face.

To read the perception publisher instead, add `?source=live` to the address: http://localhost:5173/?source=live. The panels are then labelled "PUBLISHER". The dashboard connects to `ws://127.0.0.1:8765` and retries every second while the publisher is not running. Use `&port=N` for another local port. Only a port can be chosen, never a host.

Start the publisher from the repository root, in a second terminal (see `perception/README.md`):

```text
python -m pip install -r perception/requirements.txt
python -m perception.publish --source fake
python -m perception.publish --source camera
```

`--source fake` needs no camera. `--source camera` needs Google's Face Landmarker model at `perception/models/face_landmarker.task` and camera permission for the terminal. The two send the same message shape, so the dashboard cannot tell them apart: check which one is running before you treat the data as measured.

## Gesture panel

No gesture classifier publishes yet, so the Gesture panel always uses a mock source, labelled "MOCK DATA", even with `?source=live`. Its 10 second loop shows every case: `neutral`, then `unknown`, then a label with no reviewed list entry (`nod`), which must be rejected and treated as `unknown`, then a second of silence so the panel shows "No recent data". `nod` is not a real gesture.

Schema 4 allows only `neutral` and `unknown` until a reviewed label list exists for a `model_version`. A reviewed list goes in `REVIEWED_LABELS` in `src/contracts/gesture.js`, with its review, when a model ships. The validator in that file follows `validate_gesture` in `tests/contract_schema.py`.

## 2D simulator

The simulator section shows a rover from above, its motor status, and every datagram with its outcome. Everything in it is labelled "DRILL": the rover, the receiver and the sender are simulated, and nothing talks to the physical rover. The drill status cannot arm the physical rover.

To drive it, follow the "Next" hint: finish startup, release the e-stop, close the cutoff, connect the drill sender, arm, then hold a drive button (or focus the arena and use the arrow keys or W A S D).

- The rover moves only while the simulated receiver has torque, at the velocities it accepted. A datagram that was ignored or rejected never moves it.
- The drill sender (`src/sim/drillSender.js`) stands in for shared control. Every command copies the boot, freshness id and rearm generation from a status whose MAC verifies and that echoes the sender's outstanding challenge, received within 250 ms. One status authorizes at most one command. It sends `rearm` only after you press Arm. With no input it sends ordinary `idle` stops to keep the session alive. Its high-water marks are in memory only.
- The rover controls (e-stop, cutoff, motor power, freeze, reboot) model local inputs on the rover, not network traffic.
- The injection buttons send hostile datagrams (a replay, an old challenge, the wrong generation, a forged MAC, another boot, a truncated datagram) so you can see the receiver refuse them.
- `src/sim/simulation.test.js` runs these flows without a browser.

## Simulator receiver

`src/sim/motorReceiver.js` is the simulator's receiver for motor commands (contract section 3) and the status it publishes (section 4). It is a line-for-line port of the Python reference receiver, `tests/motor_receiver.py`, because the contract requires the simulator to use the same receiver procedure as the firmware. Command and status validation and the MAC live in `src/contracts/motor.js`.

`src/sim/motorReceiver.parity.test.js` replays steps recorded from the Python receiver and requires identical outcomes, statuses and state after every step. The steps are seeded random scenarios, both protocol-following and hostile. To regenerate the fixture after a change to the Python receiver, run this from the repository root:

```text
python3 frontend/scripts/receiver_parity.py
```

The fixture (`src/sim/__fixtures__/receiver-parity.json.gz`) is gzipped JSON. It contains synthetic test traffic signed with the labelled test key from `tests/control_auth.py`, not recordings. That key is a test fixture and must not be installed on a rover.

The browser's HMAC is asynchronous, and the receiver procedure is a strict sequence, so the simulator uses its own synchronous SHA-256 and HMAC (`src/sim/hmac.js`). The tests check it against Node's crypto. It is for the simulator only.

This is a model of the contract. It drives no motors, and a passing test is not a hardware verification.

## Architecture

```text
src/contracts/   everything the frontend knows about a message: names, ranges, validation
src/sources/     data sources with one interface: { kind, subscribe(listener) => unsubscribe }
src/hooks/       subscribe to a source, validate each message, track staleness (useMessageStream is shared)
src/sim/         the simulator: receiver model, drill sender, simulation (no display code)
src/components/  display only
```

- Contract knowledge lives only in `src/contracts/`. A contract change should touch only that folder.
- Every message is validated before it is displayed. A message that fails validation is shown as rejected, with its errors, and never as data.
- Raw JSON text goes through `decodeJson` in `src/contracts/json.js`. It rejects duplicate keys and integers written as floats, and it returns a rejection instead of throwing.
- `confidence: null` on a camera frame means the detector gave no score. It does not mean the face was lost.
- A source's `kind` (`'mock'` or `'live'`) is kept on the source object, not inside messages, because schema 4 consumers reject unknown fields.
- Staleness is measured on the browser's own clock, as time since the last message arrived. The producer's clock may be on another machine. Data is marked stale after 250 ms.
- The live source (`src/sources/websocketCameraSource.js`) passes each message on as raw text, and the hook decodes it with `decodeJson`. The mock passes objects, which are validated directly. `src/sources/selectCameraSource.js` picks the source from the address.
- Mock sources live in `src/sources/`, not `src/data/`, because the repository's root `.gitignore` ignores `data/`.
- Theme colours, radius, border width and shadow are CSS variables in `src/themes.css`. Components use only those variables.
