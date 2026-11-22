# handoff_sim.md — living state of the 3D simulator build

**Branch:** `mridhul/sim-3d` · **Last verified:** 22 November 2026, 05:35 ET

## How to use this file

`handoff.md` is the spec and does not change much. **This file is the state** —
what is built, what is verified, what is decided, what is broken, and what comes
next. Read this first when resuming.

Every build session appends to the **Build log** at the bottom. If you are an
agent picking this up: read §1–§5, do the work in §6, then append to §9.

---

## 1. Current state

| | |
| --- | --- |
| Branch | `mridhul/sim-3d` (off `davina/frontend-simulator-ui`, with `origin/main` merged) |
| Integration worktree | `../sojourner-integration` on `mridhul/integration`, owns `src/sources/` |
| Tests on this branch | **198 passing** (164 after Phase 1, +34 for Phase 2) |
| Tests on `mridhul/integration` | 91 — different baseline, it has no simulator |
| Dev server | `cd frontend && npm run dev` → `localhost:5173`, 3D section below the 2D sim |
| Phases complete | 0, 1, 2. Phase 3 shell partially built. |

### Verified by hand in a browser, 05:35 ET

Not claimed — observed:

- Scene renders. Zero console errors.
- Moves only when armed and commanded (`task_state manual`, `output +0.200 m/s`).
- Clearance to the left rail reads `0.685 m` = 1.01 − 0.325. Correct.
- Clearance at spawn reads `0.177 m (back_wall)` = 0.60 − 0.4225. Correct.
- Collision halts the avatar at `x 10.978` = 11.40 − 0.4225, to the millimetre,
  reporting `clearance 0.000 m` / `contact end_wall`. It stops; it does not slide.
- **Watchdog is visible and named.** Drop link mid-drive → `halted: watchdog`,
  output zeroed. This is a *Done means* item and it is satisfied.
- Top-down inset renders corridor, doorway gap and room correctly.

### Not yet verified by hand

- A full traversal into the room, and whether all five metrics actually emit.
- E-stop latch, stale challenge, forged MAC (buttons exist, untested by hand).
- Anything about appearance — the scene is an untextured grey box.

---

## 2. Verified world geometry — do not re-derive

These are settled and confirmed against the running build. Changing any of them
means a metrics re-baseline.

| Quantity | Value | Note |
| --- | --- | --- |
| Corridor extent (X) | **−0.60 → 11.40** | 12.0 m long; spawn is the origin |
| Spawn | (0, 0), heading 0 | Back wall 0.60 m behind — **see issue I-2** |
| Corridor clear width | 2.20 m | walls at y = ±1.10 |
| Rail face (limiting surface) | y = ±1.01 | 0.09 m inset = 0.05 standoff + 0.04 dia |
| Effective corridor width | 2.02 m | |
| Wall thickness | 0.12 m | right wall occupies y ∈ [−1.22, −1.10] |
| Doorway centre | x = 7.50 | 3.0 m straight final approach |
| Doorway clear width | **0.815 m** | ADA 404.2.3. x ∈ [7.0925, 7.9075] |
| Clearance per side | **82.5 mm** | the benchmark |
| Room | 3.60 m along corridor × 4.20 m deep | x ∈ [5.70, 9.30], y ∈ [−1.22, −5.42] |
| Avatar footprint | 0.650 × 0.845 m | width from contract, length from rig STL |
| Avatar height | 1.058 m | depends on `DECK_TOP_Z` — CONFIRM pending |
| Pivot | footprint centre | skid-steer, **not** a rear axle |
| Swing radius | 0.533 m | turn in place sweeps 1.066 m |
| Optimal path | **8.4101 m** | bend (7.4175, −0.6850); legs 7.4491 + 0.9610 |
| Straight-line floor | 7.6777 m | illegal, reference only |
| Aperture for footprint centre | 0.1650 m | = 2 × 82.5 mm — use as a self-check |
| Fixed timestep | 1/120 s | accumulator clamped at 0.25 s |
| Near-miss threshold | 0.05 m | fixed; emitted in the metrics object |

---

## 3. Rulings already made — closed, do not reopen

1. **Spawn** at (0,0) with the back wall at −0.60: accepted (but see I-2).
2. **Room orientation**: 3.60 m *along* the corridor, 4.20 m *deep*. `handoff.md`
   contradicted itself; Phase 3's table was right, Phase 7 T7.3 was wrong and has
   been corrected.
3. **Optimal path**: taut path for the inscribed circle (r = 0.325 m), bending
   around the inflated near-jamb corner treated as a point. Documented as
   slightly optimistic. 8.4101 m.
4. **Hesitation**: availability derived from receiver state — armed, no latch,
   output velocities zero. Committed = next accepted command with non-zero
   velocity. Exact for discrete cueing, degenerate in continuous manual mode.
5. **Door leaf**: excluded from collision (it sits behind the rail face). The
   door **opens into the room**, which avoids leaf/rail interference.

### Protected files — never edit

`frontend/src/sim/motorReceiver.js` · `drillSender.js` · `contracts/motor.js` ·
`hmac.js` · `docs/contracts.md` · anything under `tests/` · the CI workflow.

### Invariants

- **I1** `src/sim/` never imports `three`. Enforced by lint rule + a test.
- **I2** Metrics derive only from `worldModel` + `collision`, never from a mesh.
- **I3** Fixed 1/120 s step. The renderer interpolates, never integrates.
- **I4** No asset, material, light or post effect may change a number.
  `npm run test:artless` proves it.
- **I5** The avatar moves only on an accepted command.
- **I6** No state carries between attempts.
- **I7** The instrument overlay is DOM; post-processing cannot touch it.

**Re-baselines: both still unspent.** Swapping the artless input from synthetic
to real recordings is a *new snapshot for a different input*, not a re-baseline.

---

## 4. Known issues

| id | Issue | Severity |
| --- | --- | --- |
| **I-1** | At spawn the camera sits inside the avatar box; only the orange pivot post is visible until you move. Arm shortens against the back wall 0.60 m behind spawn. First impression of the scene is a floating post. | High — cosmetic but it is the first thing anyone sees |
| **I-2** | Spawn margin too thin. 0.60 m to the back wall against a 0.533 m swing radius leaves 0.067 m when turning in place — 17 mm above the near-miss threshold. An attempt opening with a reorientation contaminates metric 2. | High — corrupts data |
| **I-3** | CI runs only pytest. `npm test`, `npm run lint` and `test:artless` never run in CI, so I1 and I4 are documented but not enforced. Needs an npm job in `tests.yml` — only Mridhul may edit it. | High — the guard rails are off |
| **I-4** | `stopReason.js` infers latch reasons from outside the receiver, and watchdog by elimination. Works, can drift. Clean fix means editing a parity-tested protected file and re-running the Python oracle. | Medium — decision pending |
| **I-5** | `constants.js` cites `docs/answers-for-davinas-claude.md` by filename, which conflicts with the no-AI-tool-names rule. Rename to `docs/sim-decisions.md` and update both citations. | Low |
| **I-6** | Bundle is 985 kB, unsplit. | Low — Phase 9 |
| **I-7** | Metrics log to console only. Correct per spec, but nothing surfaces them yet. | Low — Phase 8 |

---

## 5. Blocked on the outside world

| Item | Needed for | Status |
| --- | --- | --- |
| WebSocket bridge port + framing | Gate 2 fully green | Proposed 8767, unconfirmed. Drill path works meanwhile. |
| Real recorded command files | Replacing synthetic artless input | Synthetic, badged. Fine for now. |
| Chassis calipers: `DECK_TOP_Z`, wheel dia/width, wheelbase, track | **Phase 4 and Phase 5** | Not measured. `DECK_TOP_Z` sets the silhouette, and lighting is tuned to the silhouette. **Measure before baking anything.** |
| Hardware calibration profiles | Davina's envelope panel | Synthetic practice profiles in use |

---

## 6. Next build — in this order

Fix first, then shell, then light. Lighting before materials is deliberate:
good lighting on untextured geometry looks far better than textured geometry
under default lighting, and lightmaps use UV2 so later albedo work does not
invalidate them. Expect to re-tune exposure once materials land.

### 6.1 Fix I-1 and I-2 (~20 min)

- Move spawn to **x = 0, back wall at −0.80**. Gives 0.267 m clearance when
  turning in place, comfortably clear of the 0.05 m threshold. Corridor extent
  becomes −0.80 → 11.20. Update `worldModel`, re-run `test:artless`, and if the
  snapshot changes, **that is the first authorised re-baseline** — commit it
  alone, with the reason in the message.
- Clamp the camera's minimum arm so it can never end up inside the avatar.
  Prefer clipping through a wall over clipping through the subject.

### 6.2 Finish the Phase 3 shell (~1 h)

Geometry must be final before any lighting is tuned.

- Suspended ceiling plane at **2.70 m** with a tile grid, and recessed housings
  cut into it where the light troughs go.
- Room interior shell: walls, ceiling, and the window aperture on the far wall
  (y = −5.42).
- End-of-corridor window aperture at x = 11.20, **1.60 × 1.40 m, sill 0.90 m**.
- Door frame with real **0.12 m reveal depth**, and the leaf modelled open into
  the room.
- Skirting, and a dado/crash-rail band at 0.80 m (geometry only, no material yet).
- Keep the existing test that every collision face lies on a visible surface
  within 1 mm. Extend it to the new geometry.

### 6.3 Phase 5a — real-time lighting rig, no bake (~2 h)

Deferring the Blender bake to 5b. This rig alone should carry the look.

- `PMREMGenerator` + `RoomEnvironment` as `scene.environment` at low intensity
  for soft ambient and reflections. Zero asset downloads. Do **not** set it as
  `scene.background` — the windows are emissive planes you control.
- Call `RectAreaLightUniformsLib.init()` once before creating any RectAreaLight.

| Light | Type | Spec |
| --- | --- | --- |
| Ceiling troughs ×4 | `RectAreaLight` | 1.20 × 0.30 m, every 3.0 m, 3000 K `#FFB46B` |
| Cove wash ×2 | `RectAreaLight` | Uplight along the **left** wall, 2700 K `#FFA957`, low |
| End-window daylight | `DirectionalLight` | **The only shadow caster.** Low afternoon angle, 5200 K `#FFECE0` |
| Window fill ×2 | `RectAreaLight` | At each aperture, matched to its emissive plane |
| Room task light | `RectAreaLight` | Over the couch position, 3000 K, low |
| Practicals | Emissive meshes | Light panels, window planes, exit sign. No real light. |

- **Exactly one shadow caster.** 2048² map, `PCFSoftShadowMap`, radius 4, frustum
  tight around the corridor volume. Tune bias until floor acne is gone.
- **No pure black anywhere.** Add a very low hemisphere fill if corners crush.
  Check with a histogram, not by eye.
- **Light the doorway deliberately.** The jamb reveal faces must read brighter
  than the wall around them, with a soft contact shadow at the floor, so the
  opening is legible as an opening. This is functional, not decorative — the
  judge has to see 8 cm.
- Terminate the corridor in light: the end window is the composition. The eye
  should be drawn down the corridor.

**Acceptance:** no pure black, no blown highlight, exactly one shadow caster,
60 fps at 1080p, `test:artless` still green.

### 6.4 Phase 6 — post-processing (~1.5 h)

Use **pmndrs `postprocessing`**, not the legacy `EffectComposer`.

| # | Effect | Parameters |
| --- | --- | --- |
| 1 | `RenderPass` | — |
| 2 | `N8AOPostPass` | `aoRadius: 0.60` m, `distanceFalloff: 1.0`, `intensity: 1.8`, colour `#2a2420`, quality Medium, denoise on |
| 3 | `BloomEffect` | `luminanceThreshold: 0.90`, `intensity: 0.25`, `radius: 0.60` |
| 4 | `DepthOfFieldEffect` | focus ~4.0 m ahead, `bokehScale: 2.0`, very mild |
| 5 | Colour grade | Warm LUT, slight shadow lift |
| 6 | `VignetteEffect` | `offset: 0.25`, `darkness: 0.50` |
| 7 | `NoiseEffect` | `opacity: 0.035`, premultiplied |
| 8 | `SMAAEffect` | last |

- **No SSR.** It flickers under a moving camera and buys nothing here.
- Grain is not optional — it is what makes the image read as captured rather
  than computed, and it kills banding in the warm wall falloff for free.
- The DOM overlay, the intent cone and the top-down inset stay **out** of this
  chain. Blurring an instrument is a bug.
- Adaptive ladder: over 20 ms sustained for 2 s → DOF off → AO quality down →
  AO off → shadow 1024 → pixel ratio 1.0. Never degrade the simulation.
- `?post=0` must disable the whole chain and still look acceptable.

**Acceptance:** side-by-side screenshot with post on/off committed; 60 fps at
1080p with the full chain; `test:artless` green.

---

## 7. After that

| Phase | Work | Est. |
| --- | --- | --- |
| 4 | Materials (PBR, roughness floor 0.25) + the rover asset: Hiwonder chassis from primitives + rig STL, scaled by `0.004642857142857143`. **Needs the calipers.** | 2.5 h |
| 5b | Blender lightmap bake to UV2, KTX2. Optional if 5a reads well enough. | 2 h |
| 7 | Real camera rig: critically damped spring arm, 2.60 m back, 1.80 m up, 55° FOV, no swoop | 1.5 h |
| 8 | Instrument overlay: state readout, top-down inset, intent cone, metrics emission | 2 h |
| 9 | Performance, 30-attempt soak, context-loss recovery | 2 h |
| 10 | Validation: fault parade, scale audit, game-grammar audit | 1.5 h |

---

## 8. Resume checklist

```bash
cd C:\Users\sharm\workspace\sojourner
git status                    # expect clean
cd frontend
npm test                      # expect 198+ passing
npm run test:artless          # expect green
npm run lint                  # expect clean
npm run dev                   # localhost:5173
```

If `test:artless` fails and you did not deliberately change world geometry,
something reached the metrics that should not have. Find it before continuing.

---

## 9. Build log — append, never rewrite

### 2026-11-22 ~05:20 — Phases 1 and 2
Metric-truth layer: `constants`, `clock`, `worldModel`, `collision`, `metrics`,
`attempt`, all with tests. Fixed-timestep accumulator replaced the frame-dependent
`tick()`. Corridor world opt-in so the 2D arena still works. `test:artless` and
the `three`-import lint rule added. Phase 2 grey box: WebGL2, AgX, pixel ratio
capped, geometry from `worldModel`, render interpolates only, driven through the
existing DrillSender. 198 tests.

### 2026-11-22 05:35 — Browser verification
Driven by hand. Clearance, collision and watchdog readings all confirmed correct
against hand calculation. Issues I-1 and I-2 found. See §4.

### _next entry goes here_
