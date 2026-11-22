// Avatar: a manual wheelchair, built from primitives (handoff Phase 2 T2.3, superseding the grey
// box). No downloaded asset, no STL — the kit chassis and the rig STL are Phase 4 and they wait on
// the calipers; this reads as a chair now and costs nothing to replace later.
//
// WHY THIS IS SAFE TO CHANGE AT ALL (invariants I2, I4): metrics derive only from worldModel and
// collision, never from a mesh, and no asset may change a number. test:artless proves it. The
// footprint, height and pivot below are READ from sim/constants.js; they are not re-declared here.
//
// Local frame: +X forward (length), +Y up, ±Z across (width). Footprint centre at the origin.
//
//  - The drive wheels are centred on X = 0 because the pivot is the footprint centre (skid-steer,
//    not a rear axle). On a real manual chair the turn-in-place centre IS the drive axle, so
//    putting the axle on the pivot is both honest to the simulation and correct for the subject.
//  - The HANDRIM is the outermost part, at exactly AVATAR_WIDTH_M / 2. That is deliberate: the
//    handrim is the surface the doorway clearance refers to (ISO 7193 asks for 50 mm minimum and
//    100 mm preferred either side of the handrims; the doorway gives 82.5 mm). Nothing on the
//    chair may protrude past the collision footprint, or the clearance readout would disagree
//    with what the eye sees.
//  - Push-handle grips top out at exactly AVATAR_HEIGHT_M, so the silhouette fills the declared
//    envelope. That height is still flagged CONFIRM in constants.js pending the caliper pass.
//  - proxy_body is kept as an INVISIBLE box at the full footprint and height: it documents the
//    collision volume in the scene graph and is what the geometry test measures. Box3 ignores
//    visibility, and WebGLShadowMap skips invisible meshes, so it bounds without drawing.
//  - Parts are merged per material, so the whole chair is 4 draw calls rather than ~35. The scene
//    is GPU-bound (I-8); a 35-mesh avatar would also be 35 more objects in the shadow pass.

import {
  BoxGeometry,
  CylinderGeometry,
  Group,
  Matrix4,
  Mesh,
  MeshStandardMaterial,
  TorusGeometry,
} from 'three'
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js'
import { AVATAR_HEIGHT_M, AVATAR_LENGTH_M, AVATAR_WIDTH_M } from '../sim/constants.js'
import { applyPose } from './frame.js'

const HALF_W = AVATAR_WIDTH_M / 2 // 0.325 — the handrim sits exactly here
const HALF_L = AVATAR_LENGTH_M / 2 // 0.4225 — footplate and anti-tips reach exactly here
const TOP = AVATAR_HEIGHT_M // 1.058 — the push-handle grips reach exactly this

// Chair anatomy, in metres. Proportions are a real manual chair's, compressed into the envelope:
// see the KNOWN LIMITATION note in constants.js — this rig is chair-width but short.
const WHEEL = { r: 0.28, tube: 0.018, planeZ: 0.285, hubR: 0.028, spokes: 5 }
const RIM = { r: 0.228, tube: 0.011 } // handrim; outer face lands on HALF_W
const CASTER = { r: 0.07, x: 0.33, z: 0.215, width: 0.03 }
const SEAT = { y: 0.515, depth: 0.42, width: 0.44, x: 0.01 }
const BACK = { lean: 0.17, h: 0.42, x: -0.235, y: 0.725 }
const FRAME_Z = 0.235
const ANTITIP_R = 0.028

const T = (x, y, z) => new Matrix4().makeTranslation(x, y, z)
const RX = (a) => new Matrix4().makeRotationX(a)
const RZ = (a) => new Matrix4().makeRotationZ(a)
const AXIS_Z = RX(Math.PI / 2) // CylinderGeometry runs along +Y; this lays it along ±Z
const AXIS_X = RZ(-Math.PI / 2) // …and this along ±X

export function createAvatarProxy() {
  const group = new Group()
  group.name = 'avatar'

  // Four buckets, one per material. Each part is baked into avatar-local space, then merged.
  const frame = [] // powder-coated tube
  const rubber = [] // tyres
  const metal = [] // handrims, spokes, axle, footplate
  const fabric = [] // sling seat, backrest, arm pads

  const put = (bucket, geo, matrix) => { geo.applyMatrix4(matrix); bucket.push(geo) }
  /** Add a part at ±z, mirrored. */
  const pair = (bucket, make, z, matrix = null) => {
    for (const s of [1, -1]) {
      const m = T(0, 0, s * z)
      if (matrix) m.multiply(matrix)
      put(bucket, make(), m)
    }
  }

  // ---- Drive wheels, on the pivot -------------------------------------------------------------
  pair(rubber, () => new TorusGeometry(WHEEL.r - WHEEL.tube, WHEEL.tube, 8, 24), WHEEL.planeZ, T(0, WHEEL.r, 0))
  pair(metal, () => new CylinderGeometry(WHEEL.hubR, WHEEL.hubR, 0.055, 12), WHEEL.planeZ, T(0, WHEEL.r, 0).multiply(AXIS_Z))
  for (const s of [1, -1]) {
    for (let i = 0; i < WHEEL.spokes; i += 1) {
      const phi = (i * 2 * Math.PI) / WHEEL.spokes
      const len = WHEEL.r - WHEEL.tube - WHEEL.hubR
      const m = T(0, WHEEL.r, s * WHEEL.planeZ).multiply(RZ(phi)).multiply(T(0, WHEEL.hubR + len / 2, 0))
      put(metal, new BoxGeometry(0.014, len, 0.009), m)
    }
  }
  // Handrim: the widest part of the chair, and the part that clears the jamb.
  // 8 radial segments, not 6: an even count puts a vertex on the lateral axis, so the faceted
  // tube reaches the full 11 mm instead of sin(60) of it and the rim lands exactly on HALF_W.
  pair(metal, () => new TorusGeometry(RIM.r, RIM.tube, 8, 24), HALF_W - RIM.tube, T(0, WHEEL.r, 0))
  put(metal, new CylinderGeometry(0.012, 0.012, WHEEL.planeZ * 2, 10), T(0, WHEEL.r, 0).multiply(AXIS_Z))

  // ---- Front casters --------------------------------------------------------------------------
  pair(rubber, () => new CylinderGeometry(CASTER.r, CASTER.r, CASTER.width, 14), CASTER.z, T(CASTER.x, CASTER.r, 0).multiply(AXIS_Z))
  for (const s of [1, -1]) {
    for (const o of [1, -1]) {
      put(frame, new BoxGeometry(0.016, 0.15, 0.008), T(CASTER.x, CASTER.r + 0.05, s * CASTER.z + o * 0.024))
    }
    put(frame, new CylinderGeometry(0.014, 0.014, 0.13, 10), T(CASTER.x, CASTER.r + 0.125, s * CASTER.z))
  }

  // ---- Frame ----------------------------------------------------------------------------------
  // Side rails run from the caster stems back to the axle plates; uprights carry the seat.
  pair(frame, () => new CylinderGeometry(0.015, 0.015, 0.56, 10), FRAME_Z, T(0.06, 0.3, 0).multiply(AXIS_X))
  pair(frame, () => new CylinderGeometry(0.014, 0.014, 0.2, 10), FRAME_Z, T(-0.18, 0.4, 0))
  pair(frame, () => new CylinderGeometry(0.014, 0.014, 0.2, 10), FRAME_Z, T(0.2, 0.4, 0))
  pair(frame, () => new CylinderGeometry(0.014, 0.014, 0.44, 10), FRAME_Z, T(0.02, 0.49, 0).multiply(AXIS_X))
  pair(frame, () => new BoxGeometry(0.07, 0.11, 0.012), 0.258, T(0, WHEEL.r, 0))
  put(frame, new CylinderGeometry(0.014, 0.014, FRAME_Z * 2, 10), T(0.06, 0.3, 0).multiply(AXIS_Z))

  // ---- Seat, backrest, push handles -----------------------------------------------------------
  put(fabric, new BoxGeometry(SEAT.depth, 0.045, SEAT.width), T(SEAT.x, SEAT.y, 0))
  put(fabric, new BoxGeometry(0.045, BACK.h, SEAT.width), T(BACK.x, BACK.y, 0).multiply(RZ(BACK.lean)))
  pair(frame, () => new CylinderGeometry(0.013, 0.013, 0.15, 10), 0.185, T(-0.268, 0.965, 0).multiply(RZ(BACK.lean)))
  pair(frame, () => new CylinderGeometry(0.013, 0.013, 0.1, 12), 0.185, T(-0.3, TOP - 0.013, 0).multiply(AXIS_X))

  // ---- Armrests -------------------------------------------------------------------------------
  pair(fabric, () => new BoxGeometry(0.3, 0.028, 0.055), 0.245, T(0.02, 0.715, 0))
  pair(frame, () => new CylinderGeometry(0.012, 0.012, 0.21, 8), 0.245, T(-0.12, 0.6, 0))
  pair(frame, () => new CylinderGeometry(0.012, 0.012, 0.21, 8), 0.245, T(0.16, 0.6, 0))

  // ---- Legrests and footplate -----------------------------------------------------------------
  pair(frame, () => new CylinderGeometry(0.014, 0.014, 0.39, 10), 0.145, T(0.27, 0.315, 0).multiply(RZ(0.313)))
  put(metal, new BoxGeometry(0.155, 0.012, 0.34), T(0.345, 0.105, 0))

  // ---- Anti-tips ------------------------------------------------------------------------------
  // Real chairs carry these, and they are what reaches furthest back. They matter here because the
  // collision box stops the footprint CENTRE at half-length from a wall: a chair shorter than its
  // own footprint would halt with a visible gap, and the readout would look wrong when it is right.
  // Footplate and anti-tip wheels now land on +/- AVATAR_LENGTH_M / 2 exactly.
  pair(frame, () => new CylinderGeometry(0.012, 0.012, 0.323, 8), 0.2, T(-0.307, 0.164, 0).multiply(RZ(-0.5706)))
  pair(rubber, () => new CylinderGeometry(ANTITIP_R, ANTITIP_R, 0.022, 10), 0.2, T(-(HALF_L - ANTITIP_R), ANTITIP_R, 0).multiply(AXIS_Z))

  // ---- Merge, one mesh per material -----------------------------------------------------------
  const materials = {
    frame: new MeshStandardMaterial({ color: 0x2f3d4f, roughness: 0.42, metalness: 0.55 }),
    rubber: new MeshStandardMaterial({ color: 0x18191c, roughness: 0.92, metalness: 0 }),
    metal: new MeshStandardMaterial({ color: 0xb4b9c0, roughness: 0.32, metalness: 0.8 }),
    fabric: new MeshStandardMaterial({ color: 0x262a31, roughness: 0.95, metalness: 0 }),
    marker: new MeshStandardMaterial({ color: 0xc8501e, roughness: 0.6, metalness: 0 }),
  }
  const merged = []
  for (const [key, parts] of [['frame', frame], ['rubber', rubber], ['metal', metal], ['fabric', fabric]]) {
    const geo = mergeGeometries(parts)
    for (const p of parts) p.dispose()
    const mesh = new Mesh(geo, materials[key])
    mesh.name = `chair_${key}`
    group.add(mesh)
    merged.push(geo)
  }

  // Collision volume, drawn by nothing. Box3 ignores visibility; the shadow pass skips it.
  const body = new Mesh(new BoxGeometry(AVATAR_LENGTH_M, AVATAR_HEIGHT_M, AVATAR_WIDTH_M), materials.frame)
  body.position.y = AVATAR_HEIGHT_M / 2
  body.name = 'proxy_body'
  body.visible = false
  group.add(body)

  // Pivot marker: a stub on the floor at the footprint centre, between the drive wheels. Kept from
  // the proxy so the skid-steer pivot stays visible while debugging, but it no longer impales the
  // seat the way a full-height post would.
  const post = new Mesh(new CylinderGeometry(0.018, 0.018, 0.09, 10), materials.marker)
  post.position.y = 0.045
  post.name = 'pivot_marker'
  group.add(post)

  return {
    object: group,
    setPose: (pose) => applyPose(group, pose),
    dispose: () => {
      for (const g of merged) g.dispose()
      body.geometry.dispose()
      post.geometry.dispose()
      for (const m of Object.values(materials)) m.dispose()
    },
  }
}
