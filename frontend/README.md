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

The dashboard currently runs on a mock camera source. The UI labels it "MOCK DATA". It is generated in the browser, not measured from a face.

The tests in `src/contracts/` check the frontend against schema `"4"` in `docs/contracts.md`. When `perception/fixtures/frame_*.json` exist in the repository, the tests also decode those files and require zero errors. Until then, those tests are reported as skipped.

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
- A source's `kind` (for example `'mock'`) is kept on the source object, not inside messages, because schema 4 consumers reject unknown fields.
- Staleness is measured on the browser's own clock, as time since the last message arrived. The producer's clock may be on another machine. Data is marked stale after 250 ms.
- Switching to live data means adding a new source in `src/sources/` with the same interface. Hooks and components do not change.
- Mock sources live in `src/sources/`, not `src/data/`, because the repository's root `.gitignore` ignores `data/`.
- Theme colours, radius, border width and shadow are CSS variables in `src/themes.css`. Components use only those variables.
