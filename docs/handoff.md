# Handoff: the 3D simulator

**To:** Davina · **From:** Mridhul · **Date:** 21 November 2026 · **Revised:** 22 November 2026 (rev 2)

> **Revision note (22 Nov, rev 1).** The scene direction changed. We are no longer
> building a flat grey-box corridor — we are building a warm, well-lit,
> photographically soft hospital corridor, a third-person camera, and a clinician
> room you can turn into. Sections changed: **What you are building**, **Scope
> limits**, **Visual language**, **Hard stop**, **Done means**, plus a new
> **Phasewise build** that is the bulk of this document. Sections **unchanged and
> still binding**: *Input: where motion comes from*, *Rules you must not bend*,
> *What to record*. Those three are the contract. The art pass does not get to
> touch them — and §Phase 1 builds a test that proves it cannot.
>
> **Revision note (22 Nov, rev 2) — the avatar changed.** It is no longer a
> wheelchair asset. It is **the rover itself**: a Hiwonder 4WD Chassis Car Kit
> with our rig mounted on its top deck, uniformly scaled to the 0.65 m footprint
> the contract specifies. Full generative spec in **The avatar asset**, which is
> written so you can build it from the STL and a vendor drawing without asking me
> anything. Two consequences: the pivot model in *Input* is corrected from
> rear-axle to skid-steer centre, and the avatar's length drops from 1.05 m to
> 0.845 m, which requires one deliberate `test:artless` re-baseline. The corridor,
> the doorway and all five metrics are otherwise untouched.

## Why this moved to you

The rover will not power up reliably, so the simulator has to carry more of the
demo than planned — not just the middle beat where a judge drives the avatar
through a doorway, but very likely the closing beat too, which was going to be
the rover driving and halting on signal loss.

It also moved because you already built the hard half. `src/sim/motorReceiver.js`
is the state machine that decides what the avatar should be doing: task states,
both latches, the watchdog, sequence rules, all parity-tested against the Python
oracle. That part is renderer-agnostic and it is finished. What is left is the
drawing.

Build the control envelope first. This is the job after it.

---

## What you are building

A single hospital corridor, rendered to look like a photograph of a real place,
with the rover as the avatar — our rig on a Hiwonder 4WD chassis — driven by the
motor command stream, followed by a third-person camera, and one doorway on the
right-hand wall that opens into a clinician consult room the judge can turn into
and enter.

Three.js, WebGL2, real-world scale throughout. The corridor is **12.0 m** long and
**2.20 m** clear between walls. The doorway sits at **7.50 m** from spawn, giving a
straight **3.0 m** final approach after the last turn. Finished ceiling is **2.70 m**.
The corridor terminates in a window, so the far end glows and the space has a
destination rather than a wall.

The look is warm and welcoming, not institutional: 3000 K ceiling troughs, 2700 K
indirect cove light washing one wall, late-afternoon daylight through the end
window and the consult-room window. Soft shadows, soft occlusion in the corners,
no hard black anywhere. The target is the feel of a hospice or a community clinic
— somewhere a person would not mind being — because that is the environment our
users are actually assessed in.

**The benchmark doorway and the clinician room door are the same door.** One
corridor, one doorway, one target, as the report requires. Entering the room *is*
clearing the doorway. The room is not a second level or a reward; it is where the
attempt ends and the metrics are emitted.

The benchmark task is doorway traversal, and that is not an arbitrary choice:
clearing a doorway cleanly is a standard practical measure of powered-chair
competence, because it forces the driver to commit to a heading, judge clearance,
and correct mid-move. The whole scene exists to measure that one thing.

### Why the scene got more real, not less

The old draft of this section said flat materials, no textures, trivial geometry.
I was wrong about that, and the reason matters, because you will have to defend
the change to a judge.

The thing we were protecting against is **game grammar** — scores, chimes,
particles, win states, a camera that swoops. That ban stands and is absolute. But
visual *fidelity* is not game grammar. In assessment terms it is the opposite:
the closer the test environment resembles the environment the patient will
actually navigate, the more the result transfers. A grey box measures whether
someone can drive a grey box. A corridor with a handrail at the right height, a
door frame with real reveal depth, and a floor with real specularity measures
whether someone can get into a consult room.

So the split is now explicit and you should hold it hard:

| Layer | Direction |
| --- | --- |
| **The scene** | Photoreal, warm, soft, dynamic. As good as you can make it. |
| **The instrument** | Flat, unchanged, legible. Exactly your existing panels. |
| **Game feedback** | Still banned. No score, no chime, no particles, no swoop. |

The instrument overlay is DOM, never in-scene, so the post-processing chain
cannot touch it and the numbers stay crisp. That separation is also what keeps
the demo legible: a photograph of a corridor, with an instrument laid over it.

### The Gaussian-splat look, without Gaussian splats

You flagged the splat work in `Floorplan2Walkthru` and you were right that it is
the best-looking thing in the references. We cannot use the technique. Read its
README closely: the splat path is an offline workflow — capture a 360° panorama
per room, restyle it through an image model, convert it with WorldLabs Marble —
that sits outside the live app entirely. The author's own caveats are that splats
struggle at doorways and small isolated objects, and that geometry drifts between
captures.

A doorway-clearance benchmark cannot be built on a representation that is
unreliable at doorways and carries no collision geometry. Metrics 1–3 would be
unmeasurable. So we reproduce the *look* with a conventional pipeline. The look is
decomposable, and each property has a cheap deterministic equivalent:

| What makes a splat read as "real" | How we get it |
| --- | --- |
| Omnidirectional bounced light, no hard black shadows | Baked lightmaps from Blender Cycles + HDRI ambient; exactly one shadow-casting light |
| No CG specular "pop" | Roughness floor of 0.25 on every material; no mirror surfaces; no SSR |
| Soft occlusion that follows real corners | N8AO at large radius, low intensity, warm occlusion colour |
| Depth falloff — distance goes soft | Very mild depth of field, focus ~4 m ahead |
| Filmic highlight rolloff, warm cast | AgX tone mapping, exposure ~1.0, warm grade LUT |
| Dense high-frequency surface detail everywhere | No clean flat planes: every surface gets albedo + roughness + normal |
| Reads as *captured*, not *rendered* | Subtle film grain (~3.5%) and a light vignette |

Grain and the roughness floor are doing more work here than anything expensive.
A render with no pure blacks, no pure speculars, and a little grain reads as a
photograph. The same render with a clean mirror floor and crushed shadows reads as
a game, and then we have lost the argument about medical framing for an entirely
visual reason.

---

## The avatar asset

The avatar is the rover: a **Hiwonder 4WD Chassis Car Kit** with our rig mounted
on its top deck, uniformly scaled to the 0.65 m footprint the contract specifies.

This section is written to be generative. With the STL and a vendor drawing in
hand you should be able to build the whole asset from it without asking me
anything. Where I do not have a trustworthy number I have said so rather than
guessing it into the repo.

### Why it is scaled, and how to say so to a judge

At true size the assembly is 0.14 m wide. The contract's test is 0.65 m of chair
in a 0.81 m opening — 8 cm per side — and a 0.14 m avatar clears that opening by
34 cm per side, which measures nothing at all. So the avatar is scaled by one
uniform factor to restore the clearance the contract specifies.

If asked: the geometry is the rover's own, scaled to the footprint the contract
defines, so the clearance test and the control envelope both stay valid. Say it
plainly — it is a deliberate modelling decision, not an accident, and it is far
more defensible than a 34 cm cakewalk presented as a competence measure.

**Do not scale the corridor. Do not scale the velocities.** One constant governs
the whole thing:

```
AVATAR_SCALE = 0.650 / 0.140 = 4.642857142857143
```

Apply it once, as a uniform scale on the assembled root. Never scale sub-parts
independently.

### Part A — the rig (authoritative, measured)

File: `wheelchair_rig.stl`. The filename is legacy; it is the rig. I measured it,
so these are facts rather than assumptions:

| Property | Value |
| --- | --- |
| Format | Binary STL, **50,430 triangles**, 2,521,584 bytes |
| MD5 | `e4df8ecabb68cd1c2e9cc73341cf25a1` — check this before building |
| Units | Millimetres |
| Bounding box X | −7.000 → 175.000 (**182.000**) |
| Bounding box Y | −69.000 → 69.000 (**138.000**) |
| Bounding box Z | 0.000 → 179.942 (**179.942**) |
| Origin | Rear-centre of the base plane |
| Base plane | Exactly Z = 0 |
| Lateral symmetry | Y symmetric about 0 |

Four things follow, and each saves you an hour:

- **Z is up in the STL; Three.js is Y-up.** Rotate −90° about X once, at the
  loader, on the assembled root. Not per-part.
- **The base is already seated at Z = 0.** No re-origining, no "find the lowest
  vertex" hack.
- **Y is already centred.** No lateral translate.
- **It is chassis-matched.** 182 × 138 mm against the chassis's 180 × 140 mm —
  within 2 mm on both axes. It was designed for this deck, which is why the
  mounting problem is a single Z translate.

STL carries no UVs and no normals worth trusting. On import: weld vertices within
0.01 mm, recompute normals with a 30° smoothing angle, then generate a
box-projection UV set. 50k triangles is fine as-is against the Phase 9 budget; if
it bites, decimate to ~20k *before* texturing, never after.

### Part B — the chassis (built to spec)

**Vendor-confirmed envelope. The finished chassis model must fit inside this:**

| Property | Value |
| --- | --- |
| Product | Hiwonder 4WD Chassis Car Kit, aluminium alloy frame, TT motors — SKU 21030025 |
| Overall envelope | **180 × 140 × 89 mm** |
| Drive | 4WD skid-steer, four TT gear motors, 1:120 reduction |
| Wheels | Four rubber anti-skid tires |
| Frame | Aluminium alloy |
| Mass | 0.5 kg, 1500 g max load |
| In the box | 4WD chassis, extension bracket, 4 wheels, 4 TT motors, M4×32 pillars, M4/M3 hardware |

**Provisional internals — CONFIRM each against the vendor drawing before you
commit the model.** Source: `hiwonder.com/products/4wd-chassis-car-kit`. I do not
have these from anywhere I trust, and you should not let me guess them into the
repo:

| Part | Provisional | Status |
| --- | --- | --- |
| Deck plate | 180 × 140 × 2 mm aluminium | CONFIRM |
| `DECK_TOP_Z` | 48 mm above ground | **CONFIRM — this one matters most** |
| Wheel diameter | 65 mm | CONFIRM |
| Wheel width | 26 mm | CONFIRM |
| Wheelbase (X) | 100 mm | CONFIRM |
| Track (Y) | 115 mm | CONFIRM |
| Motor bodies | 4 × TT, ~70 × 22 × 37 mm | CONFIRM |

`DECK_TOP_Z` is the number that changes the silhouette, because it sets how high
the rig sits. Put it in the constants module with a comment naming its source and
the date you confirmed it. If the drawing ever contradicts the 89 mm envelope,
trust the envelope and tell me.

Model the chassis from primitives — deck plate, four motor bodies slung beneath,
four wheels, the extension bracket, and the corner hardware as short cylinders.
It is about twenty primitives. **Do not source a third-party GLB for this.** A
wrong model of a real product is worse than a simple right one, and this is the
part a judge might actually recognise.

### Part C — assembly, in this order

1. Build the chassis in **millimetres**, ground at Z = 0, footprint centred on
   the X/Y origin.
2. Load the rig STL. Translate X by **+6.000 mm** — the rig's footprint centre is
   at X = 84, the chassis's at X = 90. Y needs no translate.
3. Translate the rig Z by **+`DECK_TOP_Z`**, seating its Z = 0 base flush on the
   deck.
4. Parent both under one `avatarRoot`.
5. Rotate `avatarRoot` −90° about X (Z-up → Y-up).
6. Scale once: mm → m and `AVATAR_SCALE` folded into a single factor of
   **0.004642857142857143**.
7. Name the nodes `chassis`, `wheel_FL`, `wheel_FR`, `wheel_RL`, `wheel_RR`,
   `rig`. The four wheels must be separately addressable — Part E spins them.
8. Export GLB, Draco geometry, KTX2 textures.

**Assembled dimensions after scaling. These replace the wheelchair rows in the
Phase 3 dimension table:**

| Element | Unscaled | Scaled (final) |
| --- | --- | --- |
| Footprint width | 140 mm | **0.650 m** — from the contract |
| Footprint length | 182 mm | **0.845 m** |
| Overall height | `DECK_TOP_Z` + 180 mm | **~1.06 m** at the provisional deck height |
| Wheel diameter | 65 mm | ~0.302 m |
| Wheelbase | 100 mm | ~0.464 m |
| Track | 115 mm | ~0.534 m |
| Pivot point | footprint centre | footprint centre |
| Ground clearance | — | derive from the wheel; do not invent it |

**The length change needs a re-baseline.** The avatar went from 1.05 m to 0.845 m,
which changes the collision footprint and therefore metrics 1–3. Re-baseline the
`test:artless` snapshot once, deliberately, in a commit whose message says why.
That is the second and final legitimate re-baseline — the first being the
handrail decision in Phase 3. Any third one is a bug.

### Part D — materials

| Part | Treatment |
| --- | --- |
| Deck and frame | Brushed aluminium, roughness 0.42, metalness 0.85, faint anisotropy |
| Motor bodies | Matte dark grey ABS, roughness 0.70, metalness 0 |
| Tires | Black rubber, roughness 0.88, metalness 0, subtle tread normal |
| Hardware | Darkened steel, roughness 0.35, metalness 0.90 |
| Rig | Default: matte medium-grey engineering plastic, roughness 0.55. It reads as a prototype, which is honest. Tell me if you want it otherwise. |

Hold the global roughness floor of 0.25 from Phase 4. A polished aluminium deck
under warm ceiling light picks up specular crawl as the camera moves, and that
single artefact will undo the photographic look Phases 5 and 6 exist to build.

### Part E — kinematics, corrected

A 4WD skid-steer platform rotates about the **centroid of its four wheel contact
patches** — on a symmetric chassis, the geometric centre of the footprint. There
is no steering axle and no caster to orient.

Swing radius is half the footprint diagonal:

```
r_swing = sqrt(0.845² + 0.650²) / 2 = 0.533 m
```

So a turn in place sweeps **1.066 m** of diameter, which the 2.20 m corridor
affords comfortably — but it is over half the corridor width, so a mid-corridor
reorientation is a real manoeuvre and not a free action. That is good for the
benchmark.

Update `chairKinematics.js` (keep the filename; a rename churns the whole doc for
nothing). The integration is unchanged — only the point it is applied at moves,
from rear axle to footprint centre.

**Wheel animation from simulation state, never from a timer.** Both wheels on a
side share a speed:

```
v_L = v − ω · (track / 2)
v_R = v + ω · (track / 2)
ω_wheel = v_side / r_wheel
```

With `track = 0.534 m` scaled and `r_wheel = 0.151 m`, positive `ω` — a left
turn, per REP-103 — gives the left pair the lower speed. In a turn in place the
two sides counter-rotate.

**Model the slip; do not fake rolling.** Skid-steer turns by scrubbing its tires
sideways. Animating the wheels as if rolling cleanly through a turn is a visible
lie, and a judge who has driven one of these will clock it immediately. The
wheels spin at the speeds above and the chassis yaws; nothing needs to pretend
the contact patch is doing something it isn't.

### Part F — what not to add

No occupant figure, no human model, no hands on anything. Same reasoning as
before: it invites the scene to be read as a character in a game, and it raises
representation questions we have not worked through. The platform is the avatar.

---

## Input: where motion comes from

*(Unchanged from the 21 Nov draft. This section is the contract.)*

The avatar is driven by schema 4 motor commands — `docs/contracts.md` section 3.
The fields that matter to you:

| Field | Meaning |
| --- | --- |
| `task_state` | `idle`, `manual`, `shared`, `hold`, `estop`, `fault`, `rearm` |
| `linear_mps` | forward velocity, metres per second, range `[-0.30, 0.30]` |
| `angular_rad_s` | yaw rate, radians per second, range `[-1.00, 1.00]` |
| `stop` | when true, both velocities are zero |

Body frame is ROS REP-103: **+X forward, +Y left, +Z up.** Positive
`angular_rad_s` is a **left turn**, counter-clockwise viewed from above. Get that
sign right once and the rest follows.

**Open item, and it blocks you — ask me on day one.** My publisher emits UDP
datagrams. A browser cannot receive UDP. I will run a bridge that republishes
the command stream over a localhost WebSocket, the same shape as the camera
stream on 8765. Agree the port with me before you write the client, and build
against a recorded command file until the bridge exists so you are never blocked
waiting on me.

### Getting that sign right once — the frame mapping, derived

Since the scene is now 3D and asymmetric, the ROS→Three.js mapping is worth
writing down rather than discovering twice. Keep the simulation entirely in the
ROS frame as plain numbers (`x`, `y`, `yaw`) and convert only at render time.

The chair is planar, so yaw is the only rotation and you never need quaternions.
Use:

```
three.position.x =  ros.x
three.position.y =  ros.z          // ground plane, 0
three.position.z = -ros.y
three.rotation.y =  ros.yaw        // no sign flip — see below
```

Check it, because this is the bug that costs an afternoon. A ROS left turn takes
the forward vector from `+X_ros` toward `+Y_ros`, which under the mapping above
is from `+X_three` toward `−Z_three`. A Three.js rotation about `+Y` by positive
φ sends `(1,0,0)` to `(cos φ, 0, −sin φ)` — also `+X` toward `−Z`. The signs
agree, so yaw passes through unchanged. Write that as a unit test (§Phase 1,
T1.3) and never think about it again.

### The pivot point — corrected in rev 2

An earlier draft of this section told you to pivot about a rear drive axle. That
was right for a manual wheelchair and is **wrong for the avatar we are actually
building**. A 4WD skid-steer platform has no steering axle: it rotates about the
centroid of its four wheel contact patches, which on a symmetric chassis is the
geometric centre of the footprint.

Pivot at the footprint centre. All four corners then swing symmetrically, with a
swing radius of 0.533 m. Full derivation, the wheel-speed equations and the slip
behaviour are in **The avatar asset**, Part E. Get this right before you tune
anything against the doorway, because the pivot point is what decides whether a
corner clips the frame, and the frame is metric 2.

---

## Rules you must not bend

*(Unchanged from the 21 Nov draft.)*

**A rejected datagram is shown as rejected, never animated as motion.** Your
`motorReceiver.js` already decides this. Honour its verdict. The same applies
once 250 ms pass with no accepted datagram — the watchdog fires, the receiver
goes to the safe state, and the avatar must stop.

If the avatar keeps gliding while the real rover would have stopped, the
simulator is lying about the single thing it exists to demonstrate. Show the
rejection: a visible state readout naming what happened — `watchdog`,
`estop latch`, `stale challenge`, `not armed` — is worth more here than smooth
animation.

**Use real units and real dimensions.** The metrics are the point, and they only
mean something at true scale. A standard doorway is 0.81 m wide. A manual
wheelchair is roughly 0.65 m wide. That leaves about 8 cm of clearance each side,
which is exactly what makes doorway traversal a real test rather than a formality.
At the contract's 0.30 m/s ceiling, a 3 m approach takes at least ten seconds —
so do not speed the scene up to make it feel better.

**Integrate on a fixed timestep.** Accumulate elapsed time and step the physics
at a constant interval rather than integrating a raw `requestAnimationFrame`
delta. A dropped frame must not teleport the avatar, or your path-efficiency and
collision numbers become noise.

---

## What to record, per attempt

*(Unchanged from the 21 Nov draft.)*

These five are the deliverable as much as the scene is. They feed the clinician
report.

1. **Time to clear the doorway**, seconds.
2. **Collisions, and near-misses** within a clearance threshold. State the
   threshold in metres and keep it fixed.
3. **Path efficiency** against the optimal line — distance travelled divided by
   the shortest legal path.
4. **Cues issued per metre travelled.** This is the fatigue proxy and it is the
   number the whole shared-control argument rests on. It is how we show that
   discrete cueing is less tiring than holding a position.
5. **Hesitation** — dwell time before each committed cue.

Emit them as a structured object at the end of each attempt, not as text on
screen. The backend will store them and I may need to read them for the session
report.

Fix the near-miss threshold at **0.05 m** and record it in the emitted object so
the number is self-describing. Do not tune it later to make results look better.

---

## Scope limits

**No score. No levels. No menus. No start screen. No sound.** One corridor, one
doorway, one target. That is from the project report and it has not changed.

> "It must read as a clinical assessment tool, because if it reads as a game then
> the medical framing collapses and the judge files the whole project under 'they
> made a game'."

What has changed is my reading of what causes that collapse. It is not fidelity.
It is reward structure, and camera language, and anything that congratulates the
user. A judge who sees a beautiful, quiet, well-lit corridor with a clearance
readout does not think "game." A judge who sees a grey box with a success chime
does.

So the limits now read:

**Still forbidden, absolutely.** Score or points. Levels, stages, progression.
Menus, title screens, difficulty select. Sound of any kind. Success or failure
states. Particles, confetti, screen flash, hit-stop. A camera that swoops, shakes,
punches FOV, or does anything the judge did not cause. Leaderboards. Timers
presented as a challenge rather than a measurement. Anything that rewards.

**Now expected.** Real-world dimensions on every object. PBR materials with
albedo, roughness and normal. Baked indirect lighting. Warm practical lights.
Soft shadows and soft occlusion. The rover avatar, built per *The avatar asset*.
Props that make the space read as a clinic. A third-person camera that follows calmly and
predictably.

**The hard constraint on all of it:** no asset, material, light or post effect
may alter a recorded number. §Phase 1 builds the test that enforces this
(`test:artless` — replay the recorded command file with every GLB and texture
stubbed out, and assert the metrics object is identical). If that test ever goes
red, the art pass has reached into the instrument and must be reverted.

---

## Visual language

Two languages now, and they must not blend.

### The scene — photographic

- Warm, soft, evenly lit. No pure black, no pure white, no mirror surfaces.
- Everything at true scale. A handrail at 0.90 m, a door head at 2.10 m, a
  light switch at 1.05 m. Judges notice wrong heights without knowing why.
- Depth through light, not through contrast: the corridor gets brighter toward
  the window at the far end, so the eye is drawn down it.
- No visual element exists to be noticed. If a prop draws attention to itself,
  it is wrong for this scene.

### The instrument — unchanged

Same palette and conventions as your existing panels — this has to look like part
of the same instrument, not a separate product.

- One palette across the dashboard, and it must read legibly on top of a warm
  photographic image. Check contrast against the brightest and darkest frames
  of the corridor, not against a grey background.
- No skeuomorphic gauges, no glow, no animated gradients. Instrumentation, not a
  landing page.
- Every number on screen carries a unit.
- Nothing on screen you cannot explain to a judge in one sentence. If a panel
  needs a paragraph, cut the panel.
- DOM, not WebGL. Post-processing must never touch the readout.

### Two scene elements that are instrumentation, not decoration

**The intent cone.** Draw the predicted trajectory of the active task ahead of
the avatar, integrated forward from the current command for 2.0 s using the same
kinematics the simulation uses — not an approximation. It proves shared control
is predictable rather than mysterious, and it is genuinely useful rather than
decorative: feed-forward preview is what lets someone with slow input commit to
a cue confidently. Render it as a flat unlit ribbon on the floor, excluded from
bloom and DOF.

**The top-down inset.** A small orthographic 2D plan in one corner — chair
footprint, wall lines, doorway gap, live clearance in metres. This is the only
reliable way to make 8 cm of clearance legible from a third-person camera, and it
is instrument-styled, so it reads as a clinical overlay rather than a minimap.

Note what that means for the hard stop below: **the 2D fallback view and the
top-down inset are the same code.** Build it in Phase 1, it ships either way.
Nothing is wasted.

---

## Hard stop

Restated, because the old wording would now fail against a scene that cannot be
built in two hours.

The two-hour clock applies to **driving, not to looking**. Phase 2 produces a
grey-box corridor — boxes and planes, no assets — that is driven by a real
command stream with all five metrics emitting. That is the gate.

**If the grey box is not driving from a real command stream within two hours,
ship the 2D top-down version instead.** A rectangle with a gap in a wall, seen
from above. The report says that is genuinely enough, and a working 2D scene
beats a half-finished 3D one at every point on the rubric. Make that call on the
clock, not on how close it feels.

Everything from Phase 3 onward is an **art pass: additive, reversible, and never
on the critical path.** If you run out of time at Phase 5, you ship Phase 4 and
it still works. If a texture fails to load, the scene falls back to its grey-box
material and the metrics are unaffected. Build it in that order and the hard stop
stops being a cliff.

The metrics, the receiver rules and the state readout are the same either way,
so nothing is wasted by switching.

---

# Phasewise build

Eleven phases. Each has a goal, a gate, and a rollback. **Do not start a phase
until the previous gate is green.** The gates are the whole point: they are what
let you stop at any phase and still have something shippable.

Times are working estimates for one person, not commitments.

| # | Phase | Est. | Gate |
| --- | --- | --- | --- |
| 0 | Preflight and scaffold | 45 m | Dev server runs, recorded commands replay |
| 1 | Metric-truth layer (headless) | 2.5 h | All sim tests green, `test:artless` exists |
| 2 | **Grey-box drivable scene** | 1.5 h | **HARD STOP GATE — driving from real stream** |
| 3 | Architecture at true scale | 2 h | Walls, doorway, room match the dimension table |
| 4 | Materials and assets | 2.5 h | Avatar and props in, metrics re-baselined once |
| 5 | Lighting | 3 h | Warm, soft, no pure blacks, one shadow caster |
| 6 | Post-processing | 1.5 h | Splat-feel table satisfied |
| 7 | Third-person camera rig | 1.5 h | Calm follow, no clipping, no swoop |
| 8 | Instrument overlay | 2 h | Readout, inset, intent cone, all DOM-correct |
| 9 | Performance and resilience | 2 h | 60 fps budget, 30 attempts, flat memory |
| 10 | Validation and acceptance | 1.5 h | Every line in *Done means* demonstrated |

---

## Phase 0 — Preflight and scaffold

**Goal.** Nothing is blocked and nothing is unpinned.

**T0.1 — Clear the blocker.** Ask me the WebSocket port and message framing
before you write a line of client code. This is the only true blocker in the
project.

**T0.2 — Get the recorded command file.** Ask me for it. You need three
recordings: a clean traversal, one with a watchdog gap mid-approach, one with an
e-stop latch. Until they exist, synthesise them yourself from the schema so you
are never idle.

**T0.3 — Pin versions.** `three@0.187.0` exactly. Vite. `postprocessing`
(pmndrs), `n8ao`, `three-mesh-bvh`. No version ranges — a judged demo does not
get to be surprised by a minor bump. Commit the lockfile.

**T0.4 — Stay on `WebGLRenderer`.** The three.js docs still describe
`WebGPURenderer` as experimental and name `WebGLRenderer` the recommended choice
for pure WebGL 2 work. Thirty judges on unknown hardware is not where we debut a
renderer. Revisit after the demo, never before.

**T0.5 — Module layout.** Create it empty, now, so nothing lands in the wrong
place later:

```
src/
  sim/                  # NO three.js imports allowed in this folder
    motorReceiver.js      # EXISTING — do not touch
    clock.js              # fixed-timestep accumulator
    worldModel.js         # analytic 2D truth: walls, doorway, optimal path
    chairKinematics.js    # differential drive, rear-axle pivot
    collision.js          # oriented-rectangle vs segment, swept
    metrics.js            # the five numbers
    attempt.js            # attempt lifecycle, emits the object
  net/
    commandSource.js      # interface both sources implement
    wsCommandSource.js    # bridge client
    fileCommandSource.js  # recorded playback, deterministic
  view/
    sceneRoot.js
    corridor.js
    clinicianRoom.js
    chairView.js
    lighting.js
    post.js
    cameraRig.js
    intentCone.js
  ui/                   # DOM only, no WebGL
    stateReadout.js
    metricsPanel.js
    topDownInset.js       # also the 2D fallback
test/
  sim/                  # unit tests, headless, fast
  parity/               # existing Python-oracle parity
  artless/              # the no-assets metrics test
tools/
  bake/                 # Blender lightmap bake script
```

**T0.6 — Lint rule.** Add an import restriction: nothing in `src/sim/` may
import `three` or anything from `src/view/`. Make it a CI failure. This is
invariant I1 and a linter enforces it for free.

**Gate 0.** `npm run dev` serves a blank page. `npm test` runs and passes zero
tests. The recorded file parses. The lint rule fails if you deliberately add a
`three` import to `src/sim/`.

**Rollback.** None needed; nothing exists yet.

---

## Phase 1 — Metric-truth layer, headless

**Goal.** Every number the project reports is computed, tested, and finished —
before any rendering exists. After this phase the metrics are closed for
modification.

This is the most important phase in the build and it has no graphics in it at
all.

**T1.1 — `worldModel.js`.** The corridor, the doorway and the room as analytic 2D
geometry: a list of wall segments in the ground plane, the doorway as a gap with
its two jamb points, the room as a closed polygon, and the optimal legal path as
a polyline. Nothing here references a mesh. This is the authoritative world; the
visible geometry will later be built *from* these numbers, never alongside them.

Use the dimension table in Phase 3. Export it as data, so the renderer and the
simulation cannot disagree about where a wall is.

**T1.2 — `clock.js`.** Fixed-timestep accumulator at **1/120 s**. Accumulate real
elapsed time, step the simulation in whole 1/120 s increments, carry the
remainder, and expose the leftover fraction so the renderer can interpolate pose
between steps. Clamp the accumulator at 0.25 s so a tab that was backgrounded
does not run a thousand catch-up steps. Render interpolates; render never
integrates.

**T1.3 — `chairKinematics.js`.** Skid-steer differential drive, pivot at the
footprint centre (see *The avatar asset*, Part E):

```
yaw += omega * dt
x   += v * cos(yaw) * dt
y   += v * sin(yaw) * dt
```

…applied at the footprint centre, with the footprint rigidly attached. Clamp `v` to
±0.30 and `omega` to ±1.00 — the contract's ceilings, enforced here as well as
upstream, because defence in depth costs two lines.

Unit tests: the frame-mapping check derived above; positive `omega` yields a left
turn; a stationary avatar with `omega` only rotates about the footprint centre and
each corner traces an arc of radius 0.533 m; `v_L` and `v_R` match the Part E
equations across the full command range.

**T1.4 — `collision.js`.** Do **not** use a capsule. The avatar is not radially
symmetric and the entire test is 8 cm of clearance per side — a capsule would
either fit through walls or refuse to fit through the door.

Model the footprint as an **oriented rectangle** in the ground plane: **0.650 m
wide × 0.845 m long**, centred on the pivot. Test it against
the wall segments with a swept check across each 1/120 s step, so a fast step can
never tunnel through a jamb. Return penetration depth and the minimum distance to
any segment; the latter is what feeds near-misses.

This is analytic 2D geometry — no BVH, no physics engine, no solver. It is exact,
deterministic, and runs in microseconds. Determinism is not a nice-to-have: the
same recorded input must produce the same metrics on every machine, or the
clinician report is not reproducible.

**T1.5 — `metrics.js`.** All five, from `worldModel` and `collision` only. Never
from a rendered mesh — that is invariant I2.

- *Time to clear:* from first accepted non-zero command to the moment the whole
  footprint is inside the room polygon.
- *Collisions and near-misses:* penetration events, and minimum-distance dips
  below the fixed 0.05 m threshold, de-bounced so one scrape is one event.
- *Path efficiency:* integrated travelled distance over the `worldModel`
  optimal-path length.
- *Cues per metre:* accepted command transitions divided by distance travelled.
- *Hesitation:* dwell between a command becoming available and the first
  committed cue, per cue, reported as a list plus a median.

**T1.6 — `attempt.js`.** Lifecycle: `arm → running → emitted → reset`. Emits the
structured object once, on room entry or on explicit reset. Includes the schema
version, the near-miss threshold, and the timestep, so the object documents its
own provenance.

**T1.7 — The artless test.** `test/artless/`: boot the simulation with
`fileCommandSource` replaying the clean recording, with no renderer and no assets
at all, and snapshot the metrics object. This snapshot is the contract. Every
later phase re-runs it. If Phase 5's lighting work changes a number, this test
goes red and you revert.

Add `npm run test:artless` and wire it into CI.

**Gate 1.** All sim unit tests green. Parity tests against the Python oracle still
green. `test:artless` produces a committed snapshot. Zero `three` imports under
`src/sim/`.

**Rollback.** If a metric definition turns out to be ambiguous, ask me — do not
pick an interpretation. `docs/contracts.md` is the one thing none of us gets to
interpret alone.

---

## Phase 2 — Grey-box drivable scene · **HARD STOP GATE**

**Goal.** A judge can drive, from the real stream, and get metrics. Ugly is fine.
Ugly is expected.

**T2.1 — Renderer boot.** `WebGLRenderer`, `antialias: false` (the post chain
handles AA later), `outputColorSpace: SRGBColorSpace`,
`toneMapping: AgXToneMapping`, `toneMappingExposure: 1.0`. Cap pixel ratio at
`Math.min(devicePixelRatio, 1.5)` from the very start, so you never develop
against a resolution the demo machine cannot hit.

**T2.2 — Geometry from the world model.** Build boxes and planes directly from
`worldModel`'s segment list. One `MeshStandardMaterial` in neutral grey, one
`HemisphereLight`, one `DirectionalLight`. That is the whole scene.

**T2.3 — Avatar proxy.** A box at 0.650 × 0.845 × 1.06 m with a visible marker at
the footprint centre. You want to *see* the pivot while you are debugging it.

**T2.4 — Command source.** Implement `commandSource.js` as an interface, then both
implementations behind it. `fileCommandSource` first — it is deterministic and
needs nobody. `wsCommandSource` second, once I have given you the port. The rest
of the application must not be able to tell which one is connected.

**T2.5 — Wire the receiver.** `motorReceiver.js` verdict → `chairKinematics`.
Accepted commands move the chair. Rejections and watchdog timeouts do not — they
stop it, and the reason string surfaces. Do not style the readout yet; a bare
`<pre>` is correct at this stage.

**T2.6 — Render interpolation.** Pose at render time is the interpolation between
the last two simulation states using the clock's leftover fraction. A dropped
frame must be invisible, not a teleport.

**T2.7 — Top-down inset, first version.** Build `topDownInset.js` now, as plain
2D canvas or SVG drawn from `worldModel`. This doubles as the entire 2D fallback,
so building it here is what makes the hard stop cheap to take.

**Gate 2 — this is the decision point.** With the real bridge connected: the
chair moves on accepted commands only; a watchdog gap visibly stops it with
`watchdog` named; an attempt completes and logs all five metrics;
`test:artless` still green.

**If this gate is not green at two hours, stop.** Ship the top-down inset
full-screen as the deliverable. It satisfies the rubric. Tell me immediately so I
can adjust the demo script — do not spend the art-pass time trying to rescue the
3D version.

**Rollback.** The 2D fallback *is* the rollback, and you have already built it.

---

## Phase 3 — Architecture at true scale

**Goal.** The real building, still untextured. Every dimension defensible.

**T3.1 — Dimensions.** Build to this table exactly, and put it in a shared
constants module that `worldModel` also reads, so there is one source of truth.

| Element | Value | Note |
| --- | --- | --- |
| Corridor length | 12.00 m | Spawn at 0.0 |
| Corridor clear width | 2.20 m | Allows a chair to turn; realistic non-bed corridor |
| Finished ceiling | 2.70 m | Suspended tile plane |
| Doorway position | 7.50 m | Right-hand wall; 3.0 m straight approach |
| Doorway clear width | **0.81 m** | From the contract. Do not change. |
| Door leaf | 0.90 m | Held open at ~100°, against the corridor wall |
| Door head height | 2.10 m | |
| Frame reveal depth | 0.12 m | Matters — it is what a swinging corner catches |
| Threshold | Flush, 0 mm | Level access. A lip would be a different test. |
| Handrail height | 0.90 m | Both walls, continuous, 0.04 m dia |
| Handrail standoff | 0.05 m | From wall face; reduces effective clear width |
| Clinician room | 3.60 × 4.20 m | Realistic consult room |
| Room ceiling | 2.70 m | |
| End window | 1.60 × 1.40 m | Sill 0.90 m; terminates the corridor in light |
| Room window | 1.20 × 1.40 m | On the far wall from the door |
| Avatar width | **0.650 m** | From the contract. See *The avatar asset*. |
| Avatar length | 0.845 m | Rig footprint governs, not the chassis |
| Avatar height | ~1.06 m | Depends on `DECK_TOP_Z` — CONFIRM |
| Pivot point | Footprint centre | Skid-steer; no steering axle |
| Swing radius | 0.533 m | Half the footprint diagonal |
| Wheel diameter | ~0.302 m | Scaled from 65 mm — CONFIRM |
| Track | ~0.534 m | Scaled from 115 mm — CONFIRM |
| Onboard camera height | ~1.00 m | Top of the rig, for the optional onboard view |

**Note the handrail.** At 0.90 m with a 0.05 m standoff, the rails reduce the
effective clear width at chair-handgrip height. Decide explicitly whether the
rails are part of the collision world — I think they should be, because they are
part of the real corridor — and if so, update `worldModel` and re-baseline
`test:artless` **once**, deliberately, with a commit message that says why. That
is the only legitimate reason to re-baseline that snapshot.

**T3.2 — Build from constants.** Walls as extruded panels with real thickness
(0.12 m), not zero-thickness planes — you need the reveal depth at the door and
you need the corner geometry for occlusion to look right.

**T3.3 — Ceiling and floor.** Suspended ceiling plane with a tile grid, recessed
light housings cut into it. Floor as one plane, seam line down the centre.

**T3.4 — The clinician room shell.** Walls, window aperture, door frame. Empty for
now.

**T3.5 — Verify against the model.** Write a test that walks the scene graph and
asserts every wall mesh's footprint matches a `worldModel` segment within 1 mm.
Visible geometry and collision geometry drifting apart is the classic way this
kind of scene starts lying.

**Gate 3.** Dimensions verified by test. `test:artless` green. Drive the grey
corridor end to end and through the door; it feels like a corridor, not a tunnel.

**Rollback.** `git revert` the phase; Phase 2 geometry still stands.

---

## Phase 4 — Materials and assets

**Goal.** Real surfaces, the rover avatar assembled, props that make it a clinic.

**T4.1 — The avatar.** Build it per **The avatar asset** — that section is the
spec and it is complete enough to work from directly. Summary of the work:
confirm the chassis internals against the vendor drawing, model the chassis from
primitives, import and clean the rig STL, assemble, scale once by
`0.004642857142857143`, export GLB.

Do this task **before** the corridor props, not after. It is the one asset whose
geometry feeds the metrics, so it needs to be settled before you re-baseline
`test:artless` — and you want exactly one re-baseline, not one per prop session.

Two gates specific to this task: the assembled chassis fits the 180 × 140 × 89 mm
envelope, and the scaled avatar measures 0.650 m wide on a ruler in the scene.
Check the second one by eye against the doorway — 8 cm per side should look like
8 cm per side.

**T4.2 — Material set.** Albedo + roughness + normal on everything, Poly Haven
CC0 textures throughout. Roughness floor of 0.25 globally — nothing in this scene
is a mirror.

| Surface | Treatment |
| --- | --- |
| Floor | Warm grey-beige vinyl sheet, roughness 0.35, faint sheen, no mirror |
| Walls | Soft off-white eggshell, roughness 0.65, subtle orange-peel normal |
| Dado / crash rail | Warm timber-effect, 0.20 m band at 0.80 m, roughness 0.45 |
| Ceiling tiles | Matte white mineral fibre, roughness 0.85 |
| Door leaf | Light timber veneer, roughness 0.40, vision panel at 1.40 m |
| Handrail | Satin stainless or timber-effect, roughness 0.30 |
| Skirting | Coved vinyl, matches floor, 0.10 m |

**T4.3 — Props: corridor.** Chosen so the space reads as a clinic and as somewhere
warm. Keep every one of them out of the collision world except where noted, and
keep every one of them out of the way of the 3 m approach.

Wall-mounted hand-sanitiser dispenser · fire extinguisher in a recess ·
illuminated exit sign over the door at the far end · nurse-call pull cord beside
the consult door · a notice board with blurred unreadable notices (no legible
text — legible text invites reading) · two upholstered waiting chairs set back in
an alcove, **in the collision world**, and placed so they never narrow the
approach · a potted plant · a framed print · a wheeled linen trolley parked well
clear of the door · a wayfinding sign with an arrow and a room number.

**T4.4 — Props: clinician room.** Height-adjustable exam couch · clinician desk
with a monitor (screen off, or a static neutral image — never live UI) · stool ·
washbasin with a mixer tap · wall-mounted BP monitor · sharps bin · curtain rail
with a half-drawn curtain · a soft task lamp · a window with a view that is just
sky and soft foliage.

**T4.5 — Optimisation.** KTX2/Basis for every texture, Draco or meshopt on
geometry. 1024² for props, 2048² for lightmaps, 512² for small items. Instance
the repeated items — ceiling tiles, light housings, floor seams.

**T4.6 — Graceful degradation.** Every asset load is wrapped: on failure, fall
back to the Phase 2 grey material and log once. A missing texture must never be a
blank screen in front of a judge.

**Gate 4.** `test:artless` green — this is the first phase where it could plausibly
break, so check it deliberately. Scene loads under 3 s on a cold cache. Every
asset's licence recorded in `assets/LICENSES.md`.

**Rollback.** Asset loading is behind one flag. `?grey=1` ships the Phase 3 scene.
Keep that flag working all the way to the demo — it is your emergency exit.

---

## Phase 5 — Lighting

**Goal.** Warm, welcoming, well lit, soft. This phase is where the scene becomes
convincing, and it is the phase most worth spending your remaining time on.

**T5.1 — Bake the indirect.** Blender Cycles, 512 samples, denoised. Bake a
lightmap atlas for all static geometry — walls, floor, ceiling, door frame, room.
UV2 channel, 2048², KTX2. Script it in `tools/bake/` so it is repeatable rather
than a thing you did once by hand at 2 a.m.

Apply via `MeshStandardMaterial.lightMap` with the UV2 attribute. This is what
gives you bounced light with no hard black shadows — the single largest
contributor to the splat-like softness, and it costs nothing at runtime.

**T5.2 — Ambient and reflections.** One Poly Haven CC0 indoor HDRI as
`scene.environment`, intensity 0.35–0.50, rotated so its bright side agrees with
the window. Do not set it as `scene.background` — the windows show a sky plane
instead, which you control.

**T5.3 — The warm rig.**

| Light | Type | Spec |
| --- | --- | --- |
| Ceiling troughs | `RectAreaLight` ×4 | 1.20 × 0.30 m, every 3.0 m, 3000 K, moderate intensity |
| Cove wash | `RectAreaLight` ×2 | Indirect uplight along the left wall, 2700 K, low |
| End-window daylight | `DirectionalLight` | **The only shadow caster.** Low afternoon angle, 5200 K |
| Window fill | `RectAreaLight` | At each aperture, matched to the sky plane, soft |
| Room task light | `RectAreaLight` | Over the couch, 3000 K, low |
| Practicals | Emissive meshes | Exit sign, light panels, room indicator. No real light. |

The 2700 K cove wash is what separates "warm and welcoming" from
"institutional." Institutional corridors are lit from directly overhead at 4000 K
with nothing on the walls. Washing one wall with low warm indirect light is the
single cheapest move that makes a clinical space feel like somewhere a person
could be comfortable.

**T5.4 — Shadows, exactly one caster.** The window directional only. 2048² map,
`PCFSoftShadowMap`, radius 4, bias tuned to kill acne on the floor. Tight shadow
camera around the corridor volume — a loose frustum wastes the whole map. Every
other light contributes through the bake. One caster is what keeps the frame
budget stable across thirty judges on unknown hardware.

**T5.5 — Terminate the corridor in light.** The end window is the composition.
Bright sky plane, slight bloom later, light falling across the floor toward the
camera. It gives the corridor depth, gives the eye somewhere to go, and makes the
space feel open rather than enclosed — which is most of "welcoming."

**T5.6 — Light the doorway deliberately.** The judge needs to read 8 cm of
clearance. Make sure the jambs are differentially lit from the wall around them —
a slightly brighter reveal face, a soft contact shadow at the floor — so the
opening is legible as an opening, not as a flat change in colour. This is a
functional lighting requirement, not an aesthetic one.

**Gate 5.** No pure black and no blown highlight anywhere in the frame — check
with a histogram, not with your eyes. Exactly one shadow-casting light. Frame time
within budget. `test:artless` green.

**Rollback.** Lightmaps are a texture assignment behind a flag; the realtime-only
rig stands on its own if the bake is bad.

---

## Phase 6 — Post-processing

**Goal.** Close the gap between "a good Three.js render" and "a photograph."

Use **pmndrs `postprocessing`**, not the legacy `EffectComposer` — it merges
effects into fewer passes and the performance difference is substantial, which
matters because this whole chain has to fit in the frame budget.

Chain, in order:

| # | Effect | Parameters |
| --- | --- | --- |
| 1 | `RenderPass` | — |
| 2 | `N8AOPostPass` | `aoRadius: 0.60` m, `distanceFalloff: 1.0`, `intensity: 1.8`, warm occlusion colour `#2a2420`, quality `Medium`, denoise on |
| 3 | `BloomEffect` | `luminanceThreshold: 0.90`, `intensity: 0.25`, `radius: 0.60` — windows and light panels only |
| 4 | `DepthOfFieldEffect` | focus ~4.0 m ahead of the chair, `bokehScale: 2.0`, very mild |
| 5 | Colour grade | Warm LUT, slight shadow lift, highlight rolloff |
| 6 | `VignetteEffect` | `offset: 0.25`, `darkness: 0.50` |
| 7 | `NoiseEffect` | `opacity: 0.035`, premultiplied |
| 8 | `SMAAEffect` | Last |

Notes that matter:

**N8AO's radius is in world units** — 0.60 m is tuned for this corridor's scale
and will look wrong if you change the dimensions. It needs real geometry around
it to show anything, which this scene has in abundance.

**No SSR.** Screen-space reflections on a vinyl floor flicker under a moving
camera, cost a lot, and buy nothing we need. The lightmap plus a low
`envMapIntensity` gives the floor the sheen it should have.

**Grain is not optional.** 3.5% noise is what makes the image read as captured
rather than computed, and it kills gradient banding in the warm wall falloff for
free. It is the cheapest item in this table and close to the most effective.

**The overlay is outside this chain.** The DOM instrument sits above the canvas.
The intent cone and the top-down inset are excluded from bloom and DOF — blurring
an instrument is a bug, not a look.

**T6.9 — Adaptive quality.** If frame time exceeds 20 ms sustained over 2 s,
degrade in this order and log it: DOF off → AO quality down → AO off → shadow map
to 1024 → pixel ratio to 1.0. Never degrade the simulation, never degrade the
overlay. Recovery is allowed but hysteretic — do not flap.

**Gate 6.** Every row of the splat-feel table demonstrably satisfied. Side-by-side
screenshot with post off, committed to the repo. 60 fps at 1080p on the demo
machine with the full chain.

**Rollback.** `?post=0`. Scene still reads well, because Phase 5 did the real work.

---

## Phase 7 — Third-person camera rig

**Goal.** The judge can see the chair, the clearance, and the door. Nothing the
judge did not cause.

**T7.1 — Spring arm.** 2.60 m behind, 1.80 m above, look-at 0.90 m above the seat.
Fixed 55° FOV. **Critically damped, never under-damped** — overshoot reads as
game feel, and a camera that bounces makes a clinical tool look like a toy.

**T7.2 — Heading lag.** Camera yaw follows chair heading with ~250 ms of lag so
turns feel smooth without the camera whipping. Position lag shorter than
rotation lag.

**T7.3 — Occlusion handling.** Sphere-cast the arm against the collision world
each step; pull in on a hit. Inside the room, the arm shortens automatically
because the room is 4.2 m deep (3.6 m along the corridor × 4.2 m deep, per
the Phase 3 table). The camera must never clip through a wall and
must never end up outside the building — both read as broken software, and
broken software is worse for the medical framing than almost anything else.

**T7.4 — Doorway readability.** Do not auto-cinematise the approach. No dolly-in,
no FOV change, no slow-motion. The clearance is made legible by the **top-down
inset** (Phase 8) and the intent cone, which are instruments. A camera that
dramatises the moment is exactly the failure mode the report warns about.

**T7.5 — Optional onboard view.** A single keypress toggles a view from the top of
the rig, ~1.00 m, looking forward — the rover's own vantage. More useful than the
old seated view it replaces, because it is the view the real hardware would
actually have, which makes it evidence rather than a camera option. Keyed only,
no menu, no on-screen prompt beyond one line in the readout. Cut it if time is
short.

**T7.6 — Interpolation.** The camera reads interpolated pose, never raw
simulation state, or it will judder at 120 Hz against a 60 Hz display.

**Gate 7.** Drive the full corridor and through the door: no clipping, no
overshoot, no motion the judge did not cause. Clearance readable at the doorway.

**Rollback.** Fixed overhead camera. Ugly, but it never clips and the inset
carries the reading.

---

## Phase 8 — Instrument overlay

**Goal.** The clinical layer. Everything the judge needs to understand what the
system is doing, and nothing else.

**T8.1 — State readout.** DOM, your existing panel styling. Current `task_state`,
and on a rejection the reason, named: `watchdog`, `estop latch`,
`stale challenge`, `not armed`. This is the most important thing on screen — it
is the evidence that the simulator is not lying — so it gets the most prominent
position, which is probably top-left and not bottom-right.

Rejections must be *visible*, not subtle. A state change the judge misses is a
state change that did not happen, as far as the demo is concerned.

**T8.2 — Live values.** `linear_mps`, `angular_rad_s`, clearance to nearest wall,
elapsed time. Every one with a unit. Fixed decimal places so the numbers do not
jitter in width — a readout whose columns dance looks unreliable even when it is
not.

**T8.3 — Top-down inset.** Promote `topDownInset.js` from Phase 2. Chair
footprint as an oriented rectangle, wall lines, the doorway gap, live
minimum-clearance figure in metres. Instrument palette, flat, no 3D. Corner
placement, roughly 240 px.

This is the panel that makes the whole benchmark legible, and it is the reason the
hard-stop fallback was never wasted work.

**T8.4 — Intent cone.** Forward-integrate the current command for 2.0 s using
`chairKinematics` — the same code, not an approximation, or the cone will
eventually disagree with the chair and undermine the exact claim it exists to
support. Flat unlit ribbon on the floor, swept to the footprint width so it shows
clearance rather than a centre line. Excluded from bloom and DOF.

**T8.5 — Metrics emission.** The structured object to the backend on attempt end.
Not on screen. If a clinician-facing summary is wanted, that is a separate panel
and a separate conversation with me.

**T8.6 — Contrast check.** Verify the overlay against the brightest frame (facing
the end window) and the darkest (deep in the room with the task light off). A
warm photographic background is much harder to overlay than grey. Fix it with a
subtle scrim behind the panel, not by brightening the palette.

**Gate 8.** Every panel explicable to a judge in one sentence. Every number
carries a unit. Readout legible in both extreme frames. Nothing on screen rewards
the user.

**Rollback.** Cut the intent cone first — it is explicitly the last thing in,
per the original brief. Then the onboard view. Never the state readout.

---

## Phase 9 — Performance and resilience

**Goal.** Thirty judges in succession, nobody resetting anything.

**T9.1 — Budget.** 60 fps at 1080p, which is 16.6 ms, and spend at most 12 ms so
there is headroom. Under 150 draw calls. Under 900k triangles. Total payload under
25 MB. Measure on the worst machine you can find, not yours.

**T9.2 — Stateless attempts.** `resetAttempt()` restores pose, clears metrics,
resets the receiver, and touches nothing else. No disposal, no reallocation,
no re-parse — reuse every buffer. Nothing accumulates between attempts: no growing
arrays of path points, no orphaned event listeners, no trail geometry.

**T9.3 — The thirty-attempt soak.** Script it: thirty consecutive replays of the
recorded file with no page reload. Assert heap is flat within noise, frame time
has not drifted, and all thirty metrics objects are identical. Run it before the
demo and keep the output. This is the test that makes "thirty judges in
succession" a claim rather than a hope.

**T9.4 — Context-loss recovery.** Handle `webglcontextlost` / `restored`. Rebuild
the renderer, keep simulation state, name it in the readout. A laptop that sleeps
mid-demo must come back, not die.

**T9.5 — Bridge-loss behaviour.** If the WebSocket drops, the watchdog fires, the
chair stops, the readout says so, and reconnection is automatic with backoff. A
dropped bridge must look like a correctly handled fault — which it is — and not
like a crash.

**T9.6 — Cold-start budget.** First meaningful frame under 3 s. Preload the
critical path, lazy-load the room props, and show the grey-box scene while assets
stream in rather than a loading screen (a loading screen is a start screen, and
those are banned).

**Gate 9.** Soak test passes. Context loss recovers. Bridge loss reads as a fault.
Cold start under 3 s.

**Rollback.** The adaptive-quality ladder from Phase 6 already handles slow
hardware. If it still misses, ship with `?post=0` as the default.

---

## Phase 10 — Validation and acceptance

**Goal.** Demonstrate every line of *Done means*, on the demo machine, with the
real bridge, in one sitting.

**T10.1 — Walk the acceptance list below, one line at a time**, and record a
screen capture of each. Not as a deliverable — as evidence for yourself that you
have actually checked rather than assumed.

**T10.2 — Fault parade.** Demonstrate, in order: a clean traversal; a watchdog
timeout mid-approach; an e-stop latch; a stale challenge; an unarmed command. Each
must stop the chair and name itself. This sequence is the strongest thing in the
demo, because it is the part a judge cannot get from a game.

**T10.3 — Scale audit.** Put a 1 m reference marker in the scene temporarily and
check the corridor, the doorway, the chair and the handrail against the dimension
table on screen. Then remove it.

**T10.4 — Game-grammar audit.** Go through the forbidden list in *Scope limits*
item by item and confirm each absence. Specifically check for things that creep
in accidentally: an easing curve that feels like juice, a hover state that
glows, a camera move on reset, a colour that reads as "success."

**T10.5 — Show me.** Before the demo. Full run, faults included, and I will check
the metrics object against the contract myself.

**Gate 10.** Every *Done means* line demonstrated. `test:artless` and the soak
test green on the demo machine.

---

## Done means

- The avatar moves only when an accepted motor command says so, at true scale.
- A rejected datagram and a watchdog timeout both stop it, visibly, with the
  reason named.
- An attempt produces all five metrics as a structured object.
- Thirty judges can try it in succession without anyone resetting anything.
- Nothing on screen reads as a game.
- **The corridor reads as a real place** — warm, softly lit, dimensionally
  correct, with no pure blacks and no mirror surfaces.
- **The avatar is the rover**, rig on chassis, scaled to 0.650 m wide, pivoting
  about its footprint centre, with four wheels driven at differential speeds from
  simulation state.
- **The doorway is legible as 8 cm of clearance per side** from the third-person
  camera plus the top-down inset.
- **A judge can turn into the clinician room and arrive inside it**, and that
  arrival is what ends the attempt.
- **`test:artless` is green** — the art pass cannot reach the numbers.

---

## Invariants

Seven rules. If you break one, the project loses the thing that makes it
defensible. They are ordered by how expensive the breakage is.

- **I1.** `src/sim/` never imports `three` and never reads the scene graph. Data
  flows one way: simulation → view. Enforced by lint.
- **I2.** Metrics derive only from `worldModel` and `collision`. Never from a
  rendered mesh, a bounding box, or a raycast against visible geometry.
- **I3.** Fixed timestep, 1/120 s. The renderer interpolates; it never integrates.
- **I4.** No asset, material, light or post effect may change a recorded number.
  `test:artless` proves it.
- **I5.** The chair moves only on an accepted command. The receiver's verdict is
  final.
- **I6.** The scene carries no state between attempts.
- **I7.** The instrument overlay is DOM. Post-processing cannot touch it.

---

## Risks

| Risk | Mitigation |
| --- | --- |
| Photoreal scene reads as a game to a judge | Game grammar banned absolutely (Phase 10 audit). Defend fidelity on ecological-validity grounds: the test environment should resemble the environment of use. |
| Art pass silently changes the metrics | `test:artless` in CI from Phase 1. Re-baseline only deliberately, only once, only with a reason in the commit. |
| Art pass eats the time the control envelope needs | Phase 2 is the hard-stop gate and comes before all art. Phases 3–10 are additive and each has a rollback. |
| Bridge not ready | `fileCommandSource` is built first and is deterministic. Never idle. |
| Demo machine slower than dev machine | Pixel ratio capped from Phase 2. Adaptive ladder in Phase 6. `?post=0` and `?grey=1` always work. |
| Asset licences questioned in the appendix | Avatar is our own rig plus a chassis modelled from a vendor drawing — no third-party model. Scene textures and HDRIs CC0 from Poly Haven. Recorded in `assets/LICENSES.md`. |
| Judge challenges the 4.64× avatar scale | Answer it head-on, don't bury it: the geometry is the rover's, scaled to the footprint the contract defines, so clearance and control envelope both stay valid. An unscaled 0.14 m avatar clearing a 0.81 m door by 34 cm per side would be the weaker position. |
| `DECK_TOP_Z` guessed rather than confirmed | It is marked CONFIRM in Part B and it sets the silhouette. Confirm before Phase 5, because the lighting is tuned to the silhouette. |
| Metrics re-baselined more than twice | Exactly two are legitimate: handrails (Phase 3) and the avatar length change (Part C). A third is a bug — come to me. |
| Handrails change the effective clear width | Decide in Phase 3, re-baseline once, document it. Do not discover this in Phase 9. |
| Gaussian splats revisited under time pressure | They break at doorways and carry no collision mesh. Not a scheduling question — they cannot measure what we measure. |

---

## Ask me about

- The WebSocket bridge port and message framing. Blocking — raise it first.
- A recorded command file to develop against before the bridge exists. Three of
  them: clean, watchdog gap, e-stop latch.
- Anything in `docs/contracts.md` section 3 that reads ambiguously. Ask rather
  than guess; the contract is the one thing none of us gets to interpret alone.
- Whether the handrails join the collision world, if you would rather I made that
  call than you.
- The chassis vendor drawing, if you cannot get the `DECK_TOP_Z` and wheel
  dimensions off the product page. Do not ship the provisional numbers in Part B
  unconfirmed.
- The rig STL ships alongside this document (`wheelchair_rig.stl` — legacy name,
  2.4 MB, binary). Part A's measurements are from that exact file, so if the one
  you have hashes differently, stop and tell me before you build against it.

---

## References

- `putuhema/hospital-ai` — SvelteKit and Three.js hospital wayfinding. Worth
  reading for how it builds corridors from outlines with support posts roughly
  every 3 m, and for its daylight that tracks the visitor's clock. Its Blender
  assets are exterior modules rather than detailed interiors, so the interior
  work here is ours.
- `vedantpawar15/Hospital-Digital-Twin` — React Three Fiber and Drei hospital twin
  wired to Blynk Cloud. Useful mainly as a camera and navigation reference; its
  README does not document materials or its lighting approach.
- `Teetertater/Floorplan2Walkthru` — TypeScript, Vite, Three.js with a
  postprocessing layer; furniture from Sketchfab, HDRIs and textures from Poly
  Haven. The closest thing to our pipeline. Its splat work is an offline
  panorama-based experiment that the author reports struggles at doorways and
  drifts geometrically — read that section before anyone proposes splats again.
  Its author's own assessment of the default Three.js view is that it looks like a
  video game, which is precisely the gap Phases 5 and 6 exist to close.
