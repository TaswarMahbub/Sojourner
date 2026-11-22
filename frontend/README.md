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

## Architecture

```text
src/contracts/   everything the frontend knows about a message: names, ranges, validation
src/sources/     data sources with one interface: { kind, subscribe(listener) => unsubscribe }
src/hooks/       subscribe to a source, validate each message, track staleness
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
