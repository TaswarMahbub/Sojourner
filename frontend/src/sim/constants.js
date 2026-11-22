// Corridor and avatar dimensional constants. One source of truth: worldModel, collision, metrics
// and (later) the renderer all read from here, so the visible scene and the measured world cannot
// disagree about where a wall is.
//
// PROVENANCE. The doorway and avatar-width figures are NOT from docs/contracts.md and must not be
// added to it. They are external standards. Cited here so the source travels with the number.
//
// Frame: ROS REP-103 throughout. +X forward (down the corridor), +Y left, +Z up, yaw CCW positive.
// Units: metres, seconds, radians, unless a name says otherwise.
//
// Sources: docs/handoff.md (rev 2, 22 Nov 2026) and docs/sim-decisions.md
// (22 Nov 2026). Where they disagree, the answers file wins (doorway 0.815 m, handrails in the
// collision world).

// ---------------------------------------------------------------------------------------------
// Doorway and avatar (answers §7.1, verbatim values)
// ---------------------------------------------------------------------------------------------

/**
 * Doorway clear width.
 * SOURCE: ADA 2010 Standards for Accessible Design, §404.2.3 — "Door openings
 * shall provide a clear width of 32 inches (815 mm) minimum", measured between
 * the face of the door and the stop, with the door open 90 degrees.
 * We use ADA's own metric figure (815 mm), not the 32 in → 812.8 mm conversion.
 */
export const DOORWAY_CLEAR_WIDTH_M = 0.815

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
export const AVATAR_WIDTH_M = 0.65

/**
 * Per-side clearance at the doorway: 82.5 mm.
 *
 * WHY THIS IS THE BENCHMARK, and the answer to give a judge who asks:
 * ISO 7193:1985 also calls for a free space of at least 50 mm, and preferably
 * 100 mm, on both sides of the handrims. 82.5 mm sits between the minimum and
 * the preferred figure — so the doorway is deliberately neither trivial nor
 * non-compliant. That is what makes it a measure rather than a formality.
 */
export const DOORWAY_CLEARANCE_PER_SIDE_M = (DOORWAY_CLEAR_WIDTH_M - AVATAR_WIDTH_M) / 2 // 0.0825

/**
 * Uniform avatar scale: assembled chassis is 140 mm wide at true size.
 * Apply ONCE on the assembled root. Never scale sub-parts independently.
 * Do not scale the corridor. Do not scale the velocities.
 */
export const AVATAR_SCALE = AVATAR_WIDTH_M / 0.14 // 4.642857142857143
export const STL_MM_TO_SCENE = 0.001 * AVATAR_SCALE // 0.004642857142857143

/**
 * KNOWN LIMITATION — state it, do not let it be discovered later.
 * Uniform scaling to chair WIDTH does not yield chair LENGTH. The avatar is
 * 0.845 m long against ISO 7193's typical 1100–1200 mm for a real chair. The
 * avatar is chair-width but short, so swing and path geometry differ from a
 * real wheelchair. Metrics 1–3 are therefore comparable across attempts on this
 * rig, but are not directly comparable to published wheelchair figures.
 */
export const AVATAR_LENGTH_M = 0.845
export const AVATAR_HEIGHT_M = 1.058 // depends on DECK_TOP_Z — CONFIRM
export const SWING_RADIUS_M = 0.533 // sqrt(0.845² + 0.650²) / 2
export const PIVOT = 'footprint_centre' // skid-steer; NOT a rear axle

// Handrails — answers §6. Rail face, not wall face, is the limiting surface.
export const HANDRAIL_HEIGHT_M = 0.9
export const HANDRAIL_STANDOFF_M = 0.05
export const HANDRAIL_DIAMETER_M = 0.04
export const HANDRAIL_PROJECTION_M = 0.09 // 0.05 standoff + 0.04 diameter
export const CORRIDOR_CLEAR_WIDTH_M = 2.2
export const CORRIDOR_EFFECTIVE_WIDTH_M = CORRIDOR_CLEAR_WIDTH_M - 2 * HANDRAIL_PROJECTION_M // 2.02

// ---------------------------------------------------------------------------------------------
// Corridor and room layout (handoff Phase 3 dimension table)
// ---------------------------------------------------------------------------------------------

export const CORRIDOR_LENGTH_M = 12.0
export const CEILING_HEIGHT_M = 2.7
/** Doorway centre, measured along +X from spawn. Right-hand wall (−Y side). */
export const DOORWAY_CENTRE_X_M = 7.5
/**
 * Door leaf. NOT in the collision world (ruled by Mridhul, 22 Nov 2026). Held flat against the
 * corridor wall it would sit at y = −1.06, behind the handrail face at −1.01, so the rail already
 * dominates — but leaf and rail would then physically interfere. So the door is hung to open INTO
 * THE ROOM. Phase 3 visual model: hinge on the room side of the frame, leaf swung into the room.
 */
export const DOOR_LEAF_M = 0.9
export const DOOR_OPENS_INTO = 'room'
export const DOOR_HEAD_HEIGHT_M = 2.1
/** Frame reveal depth = wall thickness. It is what a swinging corner catches. */
export const FRAME_REVEAL_DEPTH_M = 0.12
export const WALL_THICKNESS_M = 0.12
export const THRESHOLD_HEIGHT_M = 0 // flush, level access
/** Clinician room: 3.60 m along the corridor (X) × 4.20 m deep (−Y). The Phase 3 table
 *  (3.60 × 4.20, corridor axis first) is authoritative; ruled by Mridhul, 22 Nov 2026. */
export const ROOM_WIDTH_ALONG_CORRIDOR_M = 3.6
export const ROOM_DEPTH_M = 4.2
export const ROOM_CEILING_HEIGHT_M = 2.7

/**
 * Spawn setback: distance from the corridor's back wall to the spawn pose (footprint centre).
 * The handoff puts spawn at 0.0 and the doorway 7.50 m from spawn, inside a 12.00 m corridor, but
 * does not say where spawn sits relative to the back wall. 0.60 m is the smallest round figure that
 * lets the avatar turn in place at spawn (swing radius 0.533 m) without breaching the 0.05 m
 * near-miss threshold. Accepted by Mridhul, 22 Nov 2026.
 */
export const SPAWN_SETBACK_M = 0.6

// ---------------------------------------------------------------------------------------------
// Chassis — Hiwonder 4WD Chassis Car Kit, SKU 21030025 (handoff Part B, answers §5)
// ---------------------------------------------------------------------------------------------
// Vendor-confirmed envelope: 180 × 140 × 89 mm. Everything below is PROVISIONAL until measured off
// the physical chassis with a caliper. Record measuredBy / measuredOn next to each value when it is.
// Do not bake lighting or export a final GLB against these values while status is 'CONFIRM'.

export const CHASSIS_ENVELOPE_MM = { length: 180, width: 140, height: 89 }

export const CHASSIS_MM = {
  /** DECK_TOP_Z — the critical one: sets the rig height, the silhouette, and Phase 5 lighting. */
  deckTopZ: { value: 48, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  deckPlate: { value: { length: 180, width: 140, thickness: 2 }, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  wheelDiameter: { value: 65, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  wheelWidth: { value: 26, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  wheelbase: { value: 100, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  track: { value: 115, status: 'CONFIRM', measuredBy: null, measuredOn: null },
  motorBody: { value: { length: 70, width: 22, height: 37 }, status: 'CONFIRM', measuredBy: null, measuredOn: null },
}
export const DECK_TOP_Z_MM = CHASSIS_MM.deckTopZ.value // CONFIRM

// ---------------------------------------------------------------------------------------------
// Rig asset — public/models/wheelchair_rig.stl (handoff Part A; measured, authoritative)
// ---------------------------------------------------------------------------------------------

export const RIG_STL = {
  file: 'wheelchair_rig.stl', // legacy name; it is the rig
  md5: 'e4df8ecabb68cd1c2e9cc73341cf25a1',
  bytes: 2521584,
  triangles: 50430,
  units: 'mm',
  bbox: { x: [-7.0, 175.0], y: [-69.0, 69.0], z: [0.0, 179.942] },
  /** Rig footprint centre is X = 84; chassis footprint centre is X = 90. */
  translateXmm: 6.0,
}

// ---------------------------------------------------------------------------------------------
// Simulation and metrics
// ---------------------------------------------------------------------------------------------

/** Fixed physics timestep (invariant I3). */
export const SIM_STEP_S = 1 / 120
export const SIM_STEP_MS = 1000 / 120
/** Clock accumulator ceiling: a backgrounded tab must not run a thousand catch-up steps. */
export const MAX_ACCUMULATOR_S = 0.25

/** Contract velocity ceilings (docs/contracts.md §3), re-enforced in the integrator. */
export const MAX_LINEAR_MPS = 0.3
export const MAX_ANGULAR_RAD_S = 1.0

/** Near-miss threshold. Fixed. Recorded in every emitted object. Never tune it. */
export const NEAR_MISS_THRESHOLD_M = 0.05
/** Clearance at or below this counts as contact, for de-bouncing one scrape into one event. */
export const CONTACT_TOLERANCE_M = 0.002
/** Swept-collision resolution: max footprint-corner travel between swept samples. */
export const SWEEP_RESOLUTION_M = 0.001

export const METRICS_SCHEMA_VERSION = '1'
