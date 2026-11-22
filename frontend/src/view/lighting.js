// Phase 5a — the real-time lighting rig (handoff_sim §6.3; handoff Phase 5 T5.2–T5.6). No bake.
//
// The light table, exactly:
//   Ceiling troughs ×4   RectAreaLight 1.20 × 0.30 m, every 3.0 m, 3000 K #FFB46B
//   Cove wash ×2         RectAreaLight, uplight along the LEFT wall, 2700 K #FFA957, low
//   End-window daylight  DirectionalLight, low afternoon angle, 5200 K #FFECE0 — the ONLY shadow caster
//   Window fill ×2       RectAreaLight at each aperture, matched to its emissive sky plane
//   Room task light      RectAreaLight over the couch position, 3000 K, low
//   Practicals           Emissive meshes: trough diffusers, window sky planes, exit sign. No light.
// Plus PMREM RoomEnvironment as scene.environment at low intensity (set in sceneRoot, which owns
// the renderer), never as scene.background.
//
// Shadows: exactly one caster. PCFSoftShadowMap was REMOVED in three r187 (the renderer warns and
// falls back to PCFShadowMap); PCFShadowMap now samples a Vogel disk scaled by shadow.radius, so
// "PCF soft, radius 4" is PCFShadowMap with radius 4. The shadow frustum is fitted to the building's
// bounding box in light space: everything outside a directional light's shadow camera is treated as
// LIT, so the frustum must also cover the room or the sun would reach its floor through solid walls.
//
// Area lights do not cast shadows and are not occluded. Intensities are kept low enough that what
// leaks through a wall (a corridor trough reaching the room floor at grazing angles) stays below
// what the room's own lights contribute.
//
// Invariant I4: nothing here is read by the simulation. View-only.

import {
  ClampToEdgeWrapping,
  DataTexture,
  DirectionalLight,
  DoubleSide,
  LinearFilter,
  Matrix4,
  Mesh,
  MeshBasicMaterial,
  MeshStandardMaterial,
  PCFShadowMap,
  PlaneGeometry,
  RGBAFormat,
  RectAreaLight,
  Vector3,
} from 'three'
import { RectAreaLightUniformsLib } from 'three/addons/lights/RectAreaLightUniformsLib.js'
import { CEILING_HEIGHT_M, ROOM_CEILING_HEIGHT_M, TROUGH_M, WALL_THICKNESS_M } from '../sim/constants.js'
import { rosToThree } from './frame.js'
import { roomRect } from './corridor.js'

// --- Colour temperatures (hex values from the light table) ---------------------------------------
export const KELVIN = Object.freeze({
  K3000: 0xffb46b,
  K2700: 0xffa957,
  K5200: 0xffece0,
})
/** Sky seen through the windows: a pale, slightly cool afternoon sky against the warm interior. */
export const SKY_HEX = 0xe6eef5
/** Exit sign practical (no legible text). */
export const EXIT_HEX = 0x2fbf71

// --- Levels. Checked against a rendered-frame histogram and reveal/wall/spawn pixel samples at 1080p
// (handoff_sim §9, 6.3 entry): 0 pure black, 0 clipped, reveal brighter than the wall beside it.
// Note: the environment carries most of the fill (I-9). Re-tune exposure when materials land.
export const LIGHT_LEVELS = Object.freeze({
  trough: 9.0,
  cove: 2.2,
  sun: 3.2,
  windowFill: 2.6,
  task: 3.5,
  environment: 0.32,
  diffuserEmissive: 3.0,
  skyEmissive: 1.6,
  exitEmissive: 1.2,
})
/**
 * Sun: low afternoon, from beyond the end window, swung toward −Y so its patch lands on the LEFT
 * wall. At +24° the patch lit the right wall beside the far jamb (+22.6 luma at 1.2 m) and made the
 * wall outshine the reveal. Changed 2026-11-22 with Mridhul's approval (handoff_sim §9).
 */
export const SUN = Object.freeze({ elevationDeg: 19, azimuthDeg: -24, distance: 25 })
// PCFSoftShadowMap is removed in r187; PCFShadowMap's Vogel-disk kernel consumes shadow.radius
// (shadowmap_pars_fragment: `vec2 radius = shadowRadius * texelSize`), so radius 4 is a real 4-texel
// penumbra. Recorded as a deviation from the §6.3 table in handoff_sim §9.
export const SHADOW = Object.freeze({ type: PCFShadowMap, mapSize: 2048, radius: 4, bias: -0.0004, normalBias: 0.025 })

/** The emissive practical for the trough and task-light diffusers. */
export function diffuserMaterial() {
  return new MeshStandardMaterial({ color: 0x000000, emissive: KELVIN.K3000, emissiveIntensity: LIGHT_LEVELS.diffuserEmissive, roughness: 1, metalness: 0 })
}

let rectLibReady = false
/** RectAreaLightUniformsLib.init() exactly once, before the first RectAreaLight. */
export function initRectAreaLights() {
  if (rectLibReady) return
  RectAreaLightUniformsLib.init()
  rectLibReady = true
}

const v3 = (p) => new Vector3(p.x, p.y, p.z)

/** A RectAreaLight at a ROS point facing along a Three.js direction. */
function rectLight({ name, color, intensity, width, height, at, facing, up = null }) {
  const l = new RectAreaLight(color, intensity, width, height)
  l.name = name
  const p = rosToThree(at)
  l.position.set(p.x, p.y, p.z)
  if (up) l.up.copy(up)
  l.lookAt(v3(p).add(facing))
  return l
}

/** Soft radial-ish falloff texture for the jamb contact shadows: alpha = f(distance from the jamb line). */
function contactTexture() {
  const W = 64
  const H = 64
  const data = new Uint8Array(W * H * 4)
  for (let j = 0; j < H; j += 1) {
    const v = Math.abs((j + 0.5) / H - 0.5) * 2 // 0 at the centre line of the long axis
    const along = 1 - Math.max(0, v - 0.55) / 0.45 // fade the ends
    for (let i = 0; i < W; i += 1) {
      const u = Math.abs((i + 0.5) / W - 0.5) * 2 // 0 on the jamb line, 1 at the edge
      const a = Math.exp(-u * 4.5) * Math.max(0, along) * (1 - u)
      const k = (j * W + i) * 4
      data[k] = 255
      data[k + 1] = 255
      data[k + 2] = 255
      data[k + 3] = Math.round(255 * a)
    }
  }
  const t = new DataTexture(data, W, H, RGBAFormat)
  t.wrapS = ClampToEdgeWrapping
  t.wrapT = ClampToEdgeWrapping
  t.magFilter = LinearFilter
  t.minFilter = LinearFilter
  t.needsUpdate = true
  return t
}

/** The building's ground-plane extent plus height, in ROS. Used to fit the shadow frustum. */
export function buildingBox(world) {
  const room = roomRect(world)
  const t = WALL_THICKNESS_M
  return {
    x0: world.bounds.x0 - t,
    x1: world.bounds.x1 + t,
    y0: room.y0 - t,
    y1: world.bounds.wallY + t,
    z0: 0,
    z1: Math.max(CEILING_HEIGHT_M, ROOM_CEILING_HEIGHT_M) + TROUGH_M.depth + 0.02,
  }
}

/** Unit vector (Three.js) from the scene toward the sun. */
export function sunDirection() {
  const e = (SUN.elevationDeg * Math.PI) / 180
  const a = (SUN.azimuthDeg * Math.PI) / 180
  // ROS: from +X (beyond the end window), swung by the azimuth (positive toward +Y), raised by e.
  const ros = { x: Math.cos(e) * Math.cos(a), y: Math.cos(e) * Math.sin(a), z: Math.sin(e) }
  return v3(rosToThree(ros)).normalize()
}

/** Fit an orthographic shadow camera tightly around the building box, in light space. */
function fitShadowCamera(sun, world) {
  const b = buildingBox(world)
  const centre = rosToThree({ x: (b.x0 + b.x1) / 2, y: (b.y0 + b.y1) / 2, z: (b.z0 + b.z1) / 2 })
  const dir = sunDirection()
  sun.position.copy(v3(centre).addScaledVector(dir, SUN.distance))
  sun.target.position.set(centre.x, centre.y, centre.z)
  sun.updateMatrixWorld(true)
  sun.target.updateMatrixWorld(true)
  const view = new Matrix4().lookAt(sun.position, sun.target.position, new Vector3(0, 1, 0))
  view.setPosition(sun.position)
  const inv = view.clone().invert()
  let minX = Infinity
  let maxX = -Infinity
  let minY = Infinity
  let maxY = -Infinity
  let minZ = Infinity
  let maxZ = -Infinity
  for (const x of [b.x0, b.x1]) {
    for (const y of [b.y0, b.y1]) {
      for (const z of [b.z0, b.z1]) {
        const p = v3(rosToThree({ x, y, z })).applyMatrix4(inv)
        minX = Math.min(minX, p.x)
        maxX = Math.max(maxX, p.x)
        minY = Math.min(minY, p.y)
        maxY = Math.max(maxY, p.y)
        minZ = Math.min(minZ, p.z)
        maxZ = Math.max(maxZ, p.z)
      }
    }
  }
  const pad = 0.05
  const cam = sun.shadow.camera
  cam.left = minX - pad
  cam.right = maxX + pad
  cam.bottom = minY - pad
  cam.top = maxY + pad
  cam.near = Math.max(0.1, -maxZ - pad)
  cam.far = -minZ + pad
  cam.updateProjectionMatrix()
}

/**
 * Build the light rig and practicals for a corridor group built by corridor.js.
 * @returns {{ lights: import('three').Light[], objects: import('three').Mesh[], sun: DirectionalLight, materials: import('three').Material[], textures: import('three').Texture[] }}
 */
export function buildLighting(world, corridor) {
  initRectAreaLights()
  const lights = []
  const objects = []
  const materials = []
  const textures = []
  const L = LIGHT_LEVELS
  const down = new Vector3(0, -1, 0)
  const housings = corridor.userData.housings

  // Ceiling troughs ×4, just below each diffuser, facing down. Width along the corridor (Three X).
  for (const h of housings.filter((x) => x.id.startsWith('trough_'))) {
    lights.push(rectLight({
      name: `${h.id}_light`, color: KELVIN.K3000, intensity: L.trough, width: h.x1 - h.x0, height: h.y1 - h.y0,
      at: { x: h.cx, y: h.cy, z: CEILING_HEIGHT_M + TROUGH_M.depth - 0.012 }, facing: down, up: new Vector3(0, 0, -1),
    }))
  }

  // Room task light, over the couch position. Its long side runs along ROS Y (Three Z).
  const task = housings.find((x) => x.id === 'room_task_light')
  lights.push(rectLight({
    name: `${task.id}_light`, color: KELVIN.K3000, intensity: L.task, width: task.x1 - task.x0, height: task.y1 - task.y0,
    at: { x: task.cx, y: task.cy, z: ROOM_CEILING_HEIGHT_M + TROUGH_M.depth - 0.012 }, facing: down, up: new Vector3(0, 0, -1),
  }))

  // Cove wash ×2: a concealed linear uplight high on the LEFT wall (+Y), aimed up and into the wall
  // so it washes the upper wall and the ceiling edge. Two runs, each half the corridor.
  const { x0, x1, wallY } = world.bounds
  const runLen = (x1 - x0) / 2
  const coveFacing = v3(rosToThree({ x: 0, y: 0.55, z: 0.84 })).normalize() // toward +Y (wall) and up
  for (let i = 0; i < 2; i += 1) {
    lights.push(rectLight({
      name: `cove_${i}`, color: KELVIN.K2700, intensity: L.cove, width: runLen - 0.1, height: 0.12,
      at: { x: x0 + runLen * (i + 0.5), y: wallY - 0.16, z: 2.42 }, facing: coveFacing,
    }))
  }

  // Window fill ×2, in each aperture at the inner wall face, facing into the building, sized and
  // coloured to match the sky plane seen through it. Practicals: the sky planes themselves.
  const skyMat = new MeshStandardMaterial({ color: 0x000000, emissive: SKY_HEX, emissiveIntensity: L.skyEmissive, roughness: 1, metalness: 0 })
  materials.push(skyMat)
  for (const [key, ap] of Object.entries(corridor.userData.apertures)) {
    const inward = { x: -ap.outward.x, y: -ap.outward.y }
    const facing = v3(rosToThree({ ...inward, z: 0 })).normalize()
    const zc = ap.sill + ap.height / 2
    lights.push(rectLight({
      name: `window_fill_${key}`, color: SKY_HEX, intensity: L.windowFill, width: ap.width, height: ap.height,
      at: { x: ap.centre.x + inward.x * 0.01, y: ap.centre.y + inward.y * 0.01, z: zc }, facing,
    }))
    // Sky plane: outside the glass line, larger than the aperture so the reveals frame it at any angle.
    const sky = new Mesh(new PlaneGeometry(ap.width + 1.2, ap.height + 1.0), skyMat)
    const back = ap.thickness + 0.35
    const sp = rosToThree({ x: ap.centre.x + ap.outward.x * back, y: ap.centre.y + ap.outward.y * back, z: zc })
    sky.position.set(sp.x, sp.y, sp.z)
    sky.lookAt(v3(sp).add(facing))
    sky.name = `sky_${key}`
    sky.castShadow = false // must never block the sun
    sky.receiveShadow = false
    sky.userData = { practical: true }
    objects.push(sky)
  }

  // Exit sign practical over the end window, flush-ish on the end wall, well above reach. No text.
  const exitMat = new MeshStandardMaterial({ color: 0x0a0a0a, emissive: EXIT_HEX, emissiveIntensity: L.exitEmissive, roughness: 0.6 })
  materials.push(exitMat)
  const exit = new Mesh(new PlaneGeometry(0.36, 0.15), exitMat)
  const end = corridor.userData.apertures.end
  const ep = rosToThree({ x: end.centre.x - 0.004, y: end.centre.y, z: end.head + 0.17 })
  exit.position.set(ep.x, ep.y, ep.z)
  exit.lookAt(ep.x - 1, ep.y, ep.z)
  exit.name = 'exit_sign'
  exit.userData = { practical: true }
  objects.push(exit)

  // Jamb contact shadows: a soft warm-dark decal at the foot of each jamb, so the opening reads as an
  // opening even with post-processing off (N8AO adds the general version in Phase 6).
  const tex = contactTexture()
  textures.push(tex)
  const contactMat = new MeshBasicMaterial({ color: 0x2a2420, alphaMap: tex, transparent: true, opacity: 0.55, depthWrite: false, side: DoubleSide, polygonOffset: true, polygonOffsetFactor: -2 })
  materials.push(contactMat)
  for (const j of world.doorway.jambs) {
    const span = world.doorway.revealDepth + 0.36
    const m = new Mesh(new PlaneGeometry(0.26, span), contactMat)
    const c = rosToThree({ x: j.x, y: j.y - world.doorway.revealDepth / 2, z: 0.002 })
    m.position.set(c.x, c.y, c.z)
    m.rotation.x = -Math.PI / 2
    m.name = `contact_${j.x < world.doorway.centreX ? 'near' : 'far'}`
    m.renderOrder = 1
    m.userData = { contactShadow: true }
    objects.push(m)
  }

  // End-window daylight: the only shadow caster.
  const sun = new DirectionalLight(KELVIN.K5200, L.sun)
  sun.name = 'sun'
  sun.castShadow = true
  sun.shadow.mapSize.set(SHADOW.mapSize, SHADOW.mapSize)
  sun.shadow.radius = SHADOW.radius
  sun.shadow.bias = SHADOW.bias
  sun.shadow.normalBias = SHADOW.normalBias
  fitShadowCamera(sun, world)
  lights.push(sun)

  return { lights, objects, sun, materials, textures }
}

/** Shadow flags for the architecture: everything casts and receives except emissive practicals. */
export function applyShadowFlags(root) {
  root.traverse((o) => {
    if (!o.isMesh) return
    const practical = o.userData.diffuser || o.userData.practical || o.userData.contactShadow || o.isInstancedMesh
    o.castShadow = !practical
    o.receiveShadow = !o.userData.diffuser && !o.userData.practical
  })
}

/** Free what buildLighting allocated (geometries of its own meshes, materials, textures). */
export function disposeLighting(rig) {
  for (const o of rig.objects) o.geometry.dispose()
  for (const m of rig.materials) m.dispose()
  for (const t of rig.textures) t.dispose()
  for (const l of rig.lights) l.dispose?.()
}
