# Answers to the 22 Nov questions — authoritative

**From:** Mridhul · **For:** Davina's Claude Code session · **Date:** 22 November 2026

Read this alongside `handoff.md`. Where the two disagree, **this file wins** — it
is newer and it resolves open items the handoff left open.

Every item below is either **DECIDED** (act on it) or **PENDING** (a fallback is
given — use the fallback, make the value configurable, and do not hardcode it).
Nothing here is a suggestion. If something is not covered, ask Mridhul rather
than choosing.

---

## 1 — Camera frame and mirroring · DECIDED

The observation is correct and correct for the right reason. Roll is about `+X`
with `+Y` left and `+Z` up, so positive roll raises the left side; a tilt toward
the left shoulder lowers it and reads negative. That is the unmirrored,
REP-103-consistent result.

**Act:**
- Do **not** pass `--mirrored`.
- Add a regression fixture: a ~2 s recorded clip of a deliberate left-shoulder
  tilt, asserting `roll < 0` at peak. Commit it.
- Rationale to put in the test's docstring: this guards against a webcam or
  driver swap silently flipping the sign, which would invert every lateral cue
  without failing anything else.

---

## 2 — Live calibration stream · DECIDED

**Transport:** `ws://127.0.0.1:8766`. One JSON object per WebSocket **text**
message. Same objects as `--events json`. No batching, no length prefix, no
newline framing.

### 2.1 What "provisional" is relative to

Rest is the SNR denominator and it is measured last. Resolve it this way:

1. **Noise estimate during probing:** a rolling estimate computed from the
   inter-rep quiet windows already observed in this session. The gaps between
   probe reps are quasi-rest and are the best available proxy; the estimate
   improves monotonically as the session proceeds.
2. **Before the first usable quiet window exists:** fall back to a population
   prior. Do not emit a fabricated zero and do not omit the field.
3. **At the end:** recompute every channel from the final rest measurement.

### 2.2 Provisional values are operator feedback only

Hard rules. All three are testable, so test them:

- A provisional value must never gate acceptance.
- A provisional value must never decide whether a rep counted.
- A provisional value must never be written into the stored
  `calibration_profile`.

### 2.3 Event schema

Rename the `provisional_*` fields. A field named `provisional_effective_std`
that holds a final value is a trap; carry the status in `provisional` and
`basis` instead, and keep the measurement field names stable across both phases
so a consumer can read one key.

```json
{
  "record_type": "channel_progress",
  "t_ms": 12840,
  "channel": "jawOpen",
  "unit": "score",
  "probe": "jaw",
  "rep_index": 2,
  "peak_deviation": 0.412,
  "effective_std": 0.0183,
  "snr": 22.5,
  "provisional": true,
  "basis": "interrep_quiet"
}
```

`basis` is an enum, not free text — a consumer must identify the denominator
without parsing English:

| Value | Meaning |
| --- | --- |
| `interrep_quiet` | Rolling noise estimate from observed inter-rep quiet windows |
| `prior_default` | Population prior; no usable quiet window yet |
| `final_rest` | The measured final rest |

### 2.4 Reconciliation at the end

When the final rest lands, **re-emit one corrected `channel_progress` per
channel** with `provisional: false` and `basis: "final_rest"`, *before* emitting
the final `calibration_profile`.

Without this, an operator UI is left displaying provisional numbers that the
stored profile silently disagrees with. That discrepancy will be noticed, and it
will be noticed at the worst possible time.

---

## 3 — Motor command bridge · PENDING (fallback given)

**Port allocation:** 8765 camera · 8766 calibration · **8767 motor commands**.

8767 is **proposed, not yet confirmed by Mridhul.** Therefore:

- Read the port from config or an env var with a default of `8767`.
- **Do not hardcode it anywhere.** One grep for `8767` should return the config
  default and nothing else.

**Framing — DECIDED, independent of the port:**

- One JSON object per text message.
- **One datagram per message.** No coalescing, ever.
- No re-ordering.
- Carry through the original **sequence number** and a **receive timestamp**, so
  the sequence rules and the 250 ms watchdog in `motorReceiver.js` behave exactly
  as they do over UDP.

### 3.1 The bridge must not filter — this is the important one

**The bridge forwards every datagram, valid or not, unmodified.**

If the bridge drops malformed, stale-challenge, replayed or unarmed datagrams,
the simulator never sees them and cannot show the rejection. Showing rejections
is the single thing the simulator exists to prove. `motorReceiver.js` is the only
component permitted to judge a datagram.

Write a test: feed a known-bad datagram through the bridge and assert it arrives
at the client byte-identical, and that the readout names the rejection reason.

---

## 4 — Recorded command files · PENDING (fallback given)

Existence not yet confirmed by Mridhul. **Synthesise and badge — that plan is
approved.** Build all three: clean traversal, watchdog gap mid-approach, e-stop
latch.

Two requirements on the badge:

1. **Inside the file.** A top-level field, e.g.
   `"provenance": {"source": "synthetic", "derived_from": "contracts.md §3", "generated": "<ISO date>", "generator": "<script path>"}`.
2. **Propagated into the metrics object.** Every attempt driven from a synthetic
   file emits metrics carrying that provenance through.

A synthetic-derived metric must be impossible to mistake for a hardware-derived
one in the session report. If Mridhul later supplies real recordings, swap them
in and the provenance field changes to `"hardware"` — no other code changes.

---

## 5 — Chassis dimensions · DECIDED (method), PENDING (values)

Do not wait on a vendor drawing. **The kit is in the building. It will not power
up, which does not stop a caliper.**

**Measure off the physical chassis:** deck top height, wheel diameter, wheel
width, wheelbase, track, motor body dimensions. Record who measured and on what
date, in the constants module, next to each value. That is stronger provenance
than a web drawing.

**Confirmed envelope:** 180 × 140 × 89 mm. If measurement disagrees with the
envelope, **trust the measurement** and tell Mridhul.

**`DECK_TOP_Z` is the critical one.** It sets how high the rig sits, which sets
the silhouette, and Phase 5's lighting is tuned to the silhouette. Measure it
**before** anything is baked. Baking against a provisional deck height means
baking twice.

Until measured, keep the Part B provisional values and keep them flagged
`CONFIRM` in code. **Do not bake, and do not export a final GLB, against
unconfirmed values.**

---

## 6 — Handrails in the collision world · DECIDED: YES

They are in the real corridor, and the avatar is ~1.058 m tall against a 0.90 m
rail, so contact is physically possible. They belong in the collision world.

**Geometry:** the rail face, not the wall face, is the limiting surface.

```
rail projection from wall face = 0.05 (standoff) + 0.04 (diameter) = 0.09 m
effective corridor clear width = 2.20 − (2 × 0.09) = 2.02 m
margin over swing diameter     = 2.02 − 1.0661 = 0.954 m
```

**Act:** inset the corridor wall collision segments by 0.09 m along every run
where a rail exists. Where the rail is interrupted, the segment reverts to the
wall face.

**The doorway test is unaffected.** ADA 404.2.3 permits no projection into a
door's clear opening width below 865 mm, so the rails are interrupted across the
opening. The clear width stays 0.815 m and the per-side clearance stays
82.5 mm.

**Re-baseline:** fold this into the **same single commit** as the avatar length
change (1.05 → 0.845 m). Two authorised re-baselines exist; spend one, not both.
The commit message must state both causes.

---

## 7 — Provenance of 0.650 m and 0.815 m · DECIDED

Correct: they are **not** in `contracts.md`, and they must **not** be added to
it. They are external standards, not our contract. Cite them in the constants
module.

**One value changes.** The doorway is **0.815 m, not 0.81 m** — take ADA's own
metric figure rather than the rounding in the handoff.

### 7.1 Write this constants module

```js
// Corridor and avatar dimensional constants.
//
// PROVENANCE. These two figures are NOT from docs/contracts.md and must not be
// added to it. They are external standards. Cited here so the source travels
// with the number.

/**
 * Doorway clear width.
 * SOURCE: ADA 2010 Standards for Accessible Design, §404.2.3 — "Door openings
 * shall provide a clear width of 32 inches (815 mm) minimum", measured between
 * the face of the door and the stop, with the door open 90 degrees.
 * We use ADA's own metric figure (815 mm), not the 32 in → 812.8 mm conversion.
 */
export const DOORWAY_CLEAR_WIDTH_M = 0.815;

/**
 * Avatar width. Also the anchor for AVATAR_SCALE.
 * SOURCE: ISO 7193:1985, "Wheelchairs — Maximum overall dimensions", for
 * wheelchairs primarily intended for indoor use: overall width 700 mm maximum,
 * with the note that wheelchair width is generally between 600 and 700 mm.
 * 0.650 m is the midpoint of that range.
 *
 * CAVEAT: ISO 7193:1985 is WITHDRAWN. Cite it as the historical source of this
 * figure, never as a current compliance claim.
 */
export const AVATAR_WIDTH_M = 0.650;

/**
 * Per-side clearance at the doorway: 82.5 mm.
 *
 * WHY THIS IS THE BENCHMARK, and the answer to give a judge who asks:
 * ISO 7193:1985 also calls for a free space of at least 50 mm, and preferably
 * 100 mm, on both sides of the handrims. 82.5 mm sits between the minimum and
 * the preferred figure — so the doorway is deliberately neither trivial nor
 * non-compliant. That is what makes it a measure rather than a formality.
 */
export const DOORWAY_CLEARANCE_PER_SIDE_M =
  (DOORWAY_CLEAR_WIDTH_M - AVATAR_WIDTH_M) / 2; // 0.0825

/**
 * Uniform avatar scale: assembled chassis is 140 mm wide at true size.
 * Apply ONCE on the assembled root. Never scale sub-parts independently.
 * Do not scale the corridor. Do not scale the velocities.
 */
export const AVATAR_SCALE = AVATAR_WIDTH_M / 0.140; // 4.642857142857143
export const STL_MM_TO_SCENE = 0.001 * AVATAR_SCALE; // 0.004642857142857143

/**
 * KNOWN LIMITATION — state it, do not let it be discovered later.
 * Uniform scaling to chair WIDTH does not yield chair LENGTH. The avatar is
 * 0.845 m long against ISO 7193's typical 1100–1200 mm for a real chair. The
 * avatar is chair-width but short, so swing and path geometry differ from a
 * real wheelchair. Metrics 1–3 are therefore comparable across attempts on this
 * rig, but are not directly comparable to published wheelchair figures.
 */
export const AVATAR_LENGTH_M = 0.845;
export const AVATAR_HEIGHT_M = 1.058;  // depends on DECK_TOP_Z — CONFIRM
export const SWING_RADIUS_M = 0.5330;  // sqrt(0.845² + 0.650²) / 2
export const PIVOT = 'footprint_centre'; // skid-steer; NOT a rear axle

// Handrails — see §6. Rail face is the limiting surface.
export const HANDRAIL_HEIGHT_M = 0.90;
export const HANDRAIL_PROJECTION_M = 0.09; // 0.05 standoff + 0.04 diameter
export const CORRIDOR_CLEAR_WIDTH_M = 2.20;
export const CORRIDOR_EFFECTIVE_WIDTH_M =
  CORRIDOR_CLEAR_WIDTH_M - 2 * HANDRAIL_PROJECTION_M; // 2.02
```

---

## Do not do

- Do not add these dimensions to `docs/contracts.md`.
- Do not hardcode port 8767.
- Do not let the bridge filter datagrams.
- Do not let a provisional calibration value reach a stored profile or a gating
  decision.
- Do not bake lighting or export a final avatar GLB against an unconfirmed
  `DECK_TOP_Z`.
- Do not take more than one re-baseline of the `test:artless` snapshot for the
  handrail + avatar-length change.
- Do not pass `--mirrored`.

## Ask Mridhul rather than choosing

- Confirmation of port 8767.
- Whether real recorded command files exist, before you invest further in
  synthetic ones.
- Whether the two sample calibration profiles (broad and narrow) exist.
- Any case where a measurement contradicts the 180 × 140 × 89 mm envelope.
- Any need for a third `test:artless` re-baseline — that indicates a bug, not a
  scope change.
