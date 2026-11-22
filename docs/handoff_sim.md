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
| Tests on this branch | **249 passing** in the working tree (246 at `18e7a72`; +3 `stepPose.test.js`) |
| **Re-baselines** | **0 of 2 spent — 2 remain.** 6.1 did not spend one: the artless snapshot has not changed since it was created in `c21d4c6` (`git log -- frontend/test/artless/__snapshots__`). stepPose extraction and I-12 also left it byte-identical. |
| Tests on `mridhul/integration` | 91 — different baseline, it has no simulator |
| Dev server | `cd frontend && npm run dev` → `localhost:5173`, 3D section below the 2D sim. `?post=0` disables the post chain |
| Phases complete | 0, 1, 2, 3 (shell), 5a (lighting, committed `f32d145`). |
| Phase 6 (post) | **Built, NOT committed** — held for the demo-hardware decision (I-8). Frame loop fix (I8), harness, test-timeout fix also uncommitted. |
| Perf harness | `cd frontend && npm run perf -- --configs full,noAO --repeat 3 --interleave` (dev server running; options in the `scripts/perf/run.js` header). GPU time via timer queries. Windows tested; macOS path written, not yet run |

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
| Corridor extent (X) | **−0.80 → 11.20** | 12.0 m long; spawn is the origin. Moved by 6.1 (`875cd28`); artless unchanged |
| Spawn | (0, 0), heading 0 | Back wall 0.80 m behind (I-2 fixed): 0.267 m clear when turning in place |
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
| Fixed timestep | 1/120 s | **Corrected 22 Nov:** the 0.25 s clamp (`MAX_ACCUMULATOR_S`) is only `FixedClock`'s default; `Simulation` passes `maxFrameMs = Infinity`. The live bound is the caller's **50 ms per frame** (`frameLoop.js` / `useSimulation.js`). See I-12 |
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
- **I8** A render error must never stop the simulation. Each frame
  re-registers rAF before any other work, then ticks the simulation,
  then updates the readout and renders, each inside its own
  try/catch. A run in which the render always throws must be
  step-for-step identical in simulated state and metrics to a run in
  which it never throws. Enforced by frameLoop.test.js.

**Re-baselines: 0 of 2 spent, 2 remain** (count lives in §1). Swapping the artless input from synthetic
to real recordings is a *new snapshot for a different input*, not a re-baseline.

---

## 4. Known issues

| id | Issue | Severity |
| --- | --- | --- |
| ~~I-1~~ | **Fixed** in `875cd28`: camera arm never shortens below swing radius + 0.17 m; it goes behind a wall (hidden) rather than into the avatar. | — |
| ~~I-2~~ | **Fixed** in `875cd28`: back wall to −0.80 m; 0.267 m clear turning in place. No re-baseline spent. | — |
| **I-3** | CI runs only pytest. `npm test`, `npm run lint` and `test:artless` never run in CI, so I1 and I4 are documented but not enforced. Needs an npm job in `tests.yml` — only Mridhul may edit it. **22 Nov: Mridhul reports CI now runs the frontend job; not visible in `tests.yml` on this branch or on `origin/main` as last fetched (`407d1b5`). Verify, then close.** | High until verified |
| **I-4** | `stopReason.js` infers latch reasons from outside the receiver, and watchdog by elimination. Works, can drift. Clean fix means editing a parity-tested protected file and re-running the Python oracle. | Medium — decision pending |
| **I-5** | `constants.js` cites `docs/answers-for-davinas-claude.md` by filename, which conflicts with the no-AI-tool-names rule. Rename to `docs/sim-decisions.md` and update both citations. | Low |
| **I-6** | Bundle is 985 kB, unsplit; **1,629 kB (538 kB gzip) with the uncommitted post chain** — `n8ao` ships a neural-denoise model even with it off. | Low — Phase 9 |
| **I-7** | Metrics log to console only. Correct per spec, but nothing surfaces them yet. | Low — Phase 8 |
| **I-8** | **60 fps at 1080p is not met on the Dell Latitude 7430 (Iris Xe).** GPU ms (timer query): no post 15.6; full §6.4 chain 72–82; ladder floor (all 5 rungs) 28.9. The ladder cannot reach 60 fps on this GPU: it needs a render-scale rung below pixel ratio 1.0, and the base frame itself has no headroom (I-10). Demo hardware decision open (Mridhul collecting chip strings). | **High — acceptance fails** |
| **I-9** | The environment map carries most of the fill. With `environmentIntensity` 0 the reveal drops 104 → 34 luma, the wall 119 → 79; the sun peaks at 26–29 luma; the end window barely "terminates the corridor in light". Re-tune exposure and levels when materials land. | Medium — look |
| **I-10** | The nine RectAreaLights cost **8.9 ms of the 15.6 ms** base GPU frame (6.7 ms with them off). The committed §6.3 rig leaves < 1 ms of headroom before any post. A §6.3 finding, not §6.4. | High — with I-8 |
| **I-11** | Shadow type deviates from the §6.3 table: `PCFSoftShadowMap` was removed in three r187; using `PCFShadowMap`, radius 4. Verified the PCF path consumes `shadow.radius` (`shadowmap_pars_fragment`: Vogel disk × `shadowRadius * texelSize`), so the 4-texel penumbra is real. No acne (sun-patch contribution sd 1.4–1.6 luma, 0 collapsed pixels). | Low — recorded deviation |
| ~~I-12~~ | **Fixed (uncommitted):** per-frame cap raised 50 → 250 ms (`MAX_FRAME_MS`, `src/view/frameLoop.js`). An 80 ms frame is never clipped, so simulated time tracks wall time down to 4 fps; a long stall still cannot dump its length into one frame. `test:artless` byte-identical, so the cap is not in the deterministic replay path. History: §2 had said 0.25 s at the accumulator; the live bound was 50 ms at the caller, giving r = 50 ÷ frame ms (0.90 at ~22 fps, 0.60 at 12 fps). | — |
| **I-13** | *(I-12 fix removes the r < 1 skew above 4 fps; quantisation remains. Design recorded in §9, not implemented.)* **Hesitation is computed in simulated time** (`metrics.js`: `tMs` = `sim.timeMs` for both availability and commit). It measures a human, so at r < 1 it under-reports by (1 − r): 10% at ~22 fps, 40% at 12 fps. It is also quantised to the 100 ms (simulated) command period. Time to clear is mixed: rover motion belongs in simulated time, the human's dwell inside it does not. Collisions, near-misses, path efficiency and cues per metre are spatial/counts and correct in simulated time. | **High — corrupts data at low fps** |
| **I-14** | *(Blocker 1 for live cue input — ruling in §9, not implemented.)* The receiver's 250 ms watchdog runs in **simulated** time (`setMonotonic(floor(sim.timeMs))`; the receiver's Unix clock is never synchronised, so `Date.now()` stamps only set a relative command lifetime). Its relationship to the drill sender is preserved (both simulated), but its wall-clock latency stretches to 250 ÷ r ms: ~280 ms at ~22 fps, ~420 ms at 12 fps. The watchdog still trips and is still named; only how long it takes in real time changes. | Medium — demo-visible at low fps |
| **I-15** | D3D shader warning X3595 (gradient instruction in a varying-iteration loop) comes from **N8AO's Poisson denoise pass** (`standardDenoiseMaterial`, run `denoiseIterations` = 2 times): 8 samples × 2 implicit-gradient `texture2D` fetches per pixel in a loop the compiler cannot unroll. Third-party code; its cost is not yet measured. | Low until measured |
| **I-16** | Door lining `#E0DDD6` (reveal faces + lintel soffit) is a material pulled forward from Phase 4 to make the reveal legible. Re-check the reveal-vs-wall gap when Phase 4 materials land. | Low |
| **I-17** | The four tests that failed under machine load (receiver parity, I1 lint check, two artless) were vitest's 5 s timeout on 0.4–1.4 s deterministic tests. Committed as 60 s; **now 30 s** (`testTimeout` and `hookTimeout`, `vite.config.js`): covers a 10x slowdown of the slowest 1.4 s test, and a real hang fails fast. **Cause unconfirmed** — not reproducible with CPU saturation alone; the attribution to machine load is by elimination. | Medium — cause unconfirmed |

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

### Next session starts here

In this order:

1. **Demo hardware** — decision pending Mridhul's chip strings (I-8).
2. **Half-res AO, measured FIRST** — X3595 is in N8AO's denoise pass (I-15), and half
   resolution quarters its pixel count.
3. **Render-scale rung** below pixel ratio 1.0 (I-8).
4. **Hesitation fields** — design in §9 (22 Nov, ~09:20). Moves the artless snapshot: spends a re-baseline.
5. **Watchdog clock mode** — ruling in §9 (22 Nov, ~09:20). Call site only, not `motorReceiver.js`.
6. **Fetch `davina/frontend-3d-sim`**, cherry-pick its `tests.yml` job, add `test:artless` to it,
   close I-3.
7. **Screenshots** (post on/off).

Settled, do not re-investigate: composer `HalfFloatType`, no MSAA. Ranks 3–5 of the per-effect
cost (SMAA, bloom, grade/vignette/noise) are **unmeasured** and must never be tabled as data.

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

### 2026-11-22 ~06:45–10:30 — 6.1–6.3 committed; 6.4 built and held; clock audit
Resumed after a context loss mid-6.3. All numbers below are measured on a Dell Latitude 7430,
renderer `ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x000046A8) Direct3D11 vs_5_0 ps_5_0, D3D11)`,
1920×1080 buffer, pixel ratio 1, unless stated. Luma = Rec.709 weights on the 8-bit output.

**Commits.** `c6e2f5a` (6.2 shell; touched no protected file). `0559de0` geometry amendment:
troughs phased so one centres on the doorway (x = 1.5, 4.5, 7.5, 10.5), committed alone before any
light. Its stated reason ("reveal lit head-on") was **wrong** — measured, the doorway trough reaches
the reveal at cos ≈ 0.22 and the wall at ≈ 0.65 (removing it: reveal −4.5, wall −7.7 luma). Comment
corrected in `f32d145`; the phasing is kept. `f32d145` Phase 5a lighting. 227 tests at that commit.

**Re-baselines.** 6.1 did not change the artless snapshot (file last touched in `c21d4c6`).
**Both re-baselines remain unspent (2 of 2).**

**Decisions made without Mridhul** (each reversible):
- Shadow type `PCFShadowMap`, radius 4 (`PCFSoftShadowMap` removed in r187). Verified radius is
  consumed on the PCF path. Deviation from the §6.3 table → I-11.
- Shadow frustum fitted to the whole building, not the corridor alone: outside a directional
  light's frustum counts as lit, so a corridor-only frustum would let the sun reach the room floor
  through the wall.
- `diffuserEmissive` practical material for the troughs; `lighting.js` returns `objects`, not a group.
- Post chain: **AgX tone mapping placed inside the chain as a `ToneMappingEffect`**, after bloom and
  DOF (scene-linear HDR) and before the grade, vignette and grain (display-referred). The §6.4
  table has no tone-mapping row; with a composer, three applies `renderer.toneMapping` only when
  drawing to the screen, so without it the chain runs untone-mapped. Validated: a tone-only chain
  matches the direct `?post=0` render to a **mean 0.06 luma** (frame mean 125.56 vs 125.55); without
  the effect the difference is **8.93**. **Grain (`NoiseEffect`) is applied after tone mapping**, in
  the same pass, so it dithers the display-referred warm falloff it is there to de-band.
- Pass grouping: RenderPass → N8AO → EffectPass(Bloom) → EffectPass(DOF + ToneMapping + LUT +
  Vignette + Noise) → EffectPass(SMAA). Table order kept; SMAA last.
- DOF "focus ~4.0 m ahead" read as 4.0 m from the camera; "very mild" = `focusRange` 4.0.
- Warm LUT generated in code (no asset): `out = (0.025 + 0.975·in) × (1.0, 0.985, 0.94)`, sRGB
  domain — lifts black, never exceeds 1, monotonic.
- N8AO "denoise on" = `denoiseIterations` 2 (the library default), `setQualityMode('Medium')`.
- Dependencies pinned exact: `postprocessing@6.39.6`, `n8ao@2.0.1` (newer n8ao 2.0.2/2.1.0 were under
  two weeks old). Bundle impact → I-6.
- Adaptive ladder: mean frame time over a full 2 s window > 20 ms steps one rung; one-way (never
  steps back up); a gap > 250 ms (hidden tab) resets the window. DOF-off rebuilds the pass without
  DOF (SKIP would keep its blur passes running).
- Frame loop extracted to `src/view/frameLoop.js` (proposed I8, below). Readout now updates before render.
- Test `testTimeout`/`hookTimeout` 60 s in `vite.config.js` (I-17).

**Decided by Mridhul:** reveal legibility by a door-lining material `#E0DDD6` on the reveal faces
and lintel soffit, plus sun azimuth +24° → −24°. No light in the table can make a reveal facing
along the corridor brighter than the wall beside it; a soffit downlight was measured and failed
below 1.2 m. → I-16.

**6.3 acceptance (1080p, six poses: spawn, mid, approach, turning, room, look-back).**
- Histogram, `?post=0`: **0 pure-black pixels, 0 clipped (RGB 255,255,255), 0 with any channel at
  255** in every pose; min luma 53.44, max 216.15. With post on: 0 / 0 / 0; min 37.75, max 208.63.
- Reveal − wall, far jamb, z 0.3–1.8 m: as first built **−47.1** (worst). After the lining + sun
  flip: `?post=0` worst **+14.5**; post ON worst **+11.0** (AO in the recess + vignette). Near jamb
  during the turn: +46.8/+47.9 (off), +40.7/+46.7 (on). Both states pass.
- Spawn region x ∈ [−0.80, 0.90] (52 floor/wall points, raycast-verified visible): min luma 94.9
  (from spawn) and 95.4 (looking back), mean 129.8 / 122.3. Not crushed.
- Exactly one shadow caster (`sun`), asserted in `lighting.test.js`. No acne: inside the sun patch
  the sun's contribution has sd 1.4–1.6 luma and 0 pixels below 25% of its peak.
- `RectAreaLightUniformsLib.init()` once, first statement of `buildLighting`, spy-tested.
- **60 fps at 1080p: FAILED** → I-8.

**Timing method (after two false starts).** rAF is not a timing instrument here (vsync-quantised,
throttled, one headed run stalled 9 min). And on Chrome/ANGLE/D3D11 **`gl.finish()` does not wait
for the GPU**: run "base" measured wall+finish p50 1.8 ms against a timer-query p50 15.11 ms for the
same frames. All GPU numbers are now `EXT_disjoint_timer_query_webgl2`, manual loop, 60 warm-up +
600 sampled, 0 disjoint. Harness: `frontend/scripts/perf/` (`npm run perf`).

| | Config | GPU mean ms | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- |
| a | no post, 9 RectAreaLights off | 6.68 | 6.58 | 8.26 | 8.89 |
| b | no post, lights on | 15.62 | 15.11 | 17.66 | 29.33 |
| c | full §6.4 chain | 72–82 (7 runs) | | | |
| d | ladder floor: all 5 rungs spent | 28.91 | 28.24 | 34.65 | 37.65 |

Per-effect (full minus one, single runs): N8AO ≈ 40 ms, DOF ≈ 20 ms — both above noise. **SMAA,
bloom and grade/vignette/noise are NOT measured**: their single-run differences (~9–15 ms) sit
inside the ~6 ms between-batch drift of the same config. Run-to-run: `full` ×4 back-to-back
81.0, 77.8, 76.9, 77.1 (sd 1.9); ×3 with 60 s cooldown 71.2, 71.5, 74.7 (sd 2.0). Cooldown did not
help; GPU time tracked whole-machine CPU load (ρ ≈ 0.64) — shared package power. Use interleaved
repeats. At DPR 1 the pixel-ratio rung is a no-op, so the floor above is at the full 1080p buffer.

**Bandwidth checks.** Composer `frameBufferType` HalfFloat; 14 render targets: 7 HalfFloat,
7 UnsignedByte, **0 Float**. **No MSAA anywhere** (context `antialias` false, composer
multisampling 0, 0 targets with samples > 0). X3595 located in N8AO's denoise → I-15.

**Render-error fault (→ proposed I8).** Before: `sim.tick`, `root.frame`, then rAF re-registration,
in one callback — any render exception stopped the simulation. Found because the ladder's last rung
threw (`renderer.getSize` without a `Vector2`; fixed). Now: re-register → tick → readout (guarded)
→ render (guarded). `frameLoop.test.js` (7 tests) fails 3 against the old shape.

**Clock audit** → I-12, I-13, I-14. Summary: one clock (simulated) drives the receiver, the
watchdog, the drill sender and every metric. When frames exceed 50 ms, simulated time runs slower
than wall time. Spatial metrics stay correct; hesitation (a human measure) does not; the watchdog
stays correct relative to the sender but slows in real time.

**Ladder, live (rAF, short cadence check only).** All five rungs fired in order at 0.1, 2.7, 5.2,
7.2, 9.2 s (gaps ≥ 2.0 s). Cadence with ladder live: 22.2 fps mean. Simulated/wall time over the
run: 31.5 s / 35.0 s.

**Uncommitted at end of session:** `post.js` (+tests), sceneRoot chain wiring and ladder, pinned
dependencies, `frameLoop.js` (+tests) and `Simulator3D.jsx`, `vite.config.js` timeouts, the perf
harness, `.gitignore`, this file. 246 tests passing, `test:artless` green, lint clean. Post on/off
screenshots not committed (chain not committed).

### 2026-11-22 ~09:20 — I8 accepted; stepPose; I-12; rulings recorded

**I8 accepted** by Mridhul; wording in §3 is verbatim.

**stepPose extraction.** `stepPose(pose, v, w, dt)` in `src/sim/stepPose.js`; `integrateInWorld`
delegates to it. Pure extract, same operation order. `test:artless` byte-identical (snapshot
untouched). +3 tests. Unblocks Davina's cone.

**I-12 fixed.** Frame cap 50 → 250 ms. `test:artless` byte-identical — the cap is not in the
replay path. The 5 s-stall test still passes with no halt.

**Test timeout** 60 → 30 s. Cause of the original timeouts unconfirmed (I-17).

**Re-baselines:** 0 of 2 spent. Verified from git: the artless snapshot has one commit, `c21d4c6`.

**HESITATION — ruling (Mridhul), NOT implemented.** Adding fields changes the metrics object
shape, which moves the artless snapshot; not spending a re-baseline in this session. Design:
`hesitation_ms` keeps its name; add a `basis` enum (`simulated` | `wall_clock`),
`quantisation_ms: 100`, and `frame_time_mean_ms` at attempt level. Replay stays `simulated` so
artless stays deterministic; live operator input is `wall_clock`. Same pattern as
provisional/basis.

**WATCHDOG — ruling (Mridhul), NOT implemented.** Simulated time is correct for replay, since the
sender is simulated too. Live cue input has a human on wall clock: at 12 fps a real 420 ms drop
goes undetected — the watchdog gets lazier the worse the frame rate. The clock source must be
mode-dependent. The fix is at the call site feeding `setMonotonic`, NOT inside `motorReceiver.js`,
so no protected file is touched.
- **Blocker 1 for live cue input:** watchdog clock mode (above).
- **Blocker 2 for live cue input:** the HMAC key — a live sender must produce datagrams the
  receiver accepts; same problem as rover-signed recordings.

**Uncommitted at end of session:** `src/sim/stepPose.js` (+test), `simulation.js`,
`frameLoop.js` (+test wording), `Simulator3D.jsx` comment, `vite.config.js`, this file.
249 tests passing, `test:artless` green, lint clean.

### _next entry goes here_
