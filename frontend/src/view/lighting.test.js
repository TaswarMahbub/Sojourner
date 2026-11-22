import { afterEach, describe, expect, it, vi } from 'vitest'
import { Box3, Color, PCFShadowMap, Vector3 } from 'three'
import { RectAreaLightUniformsLib } from 'three/addons/lights/RectAreaLightUniformsLib.js'
import { CORRIDOR_WORLD as W } from '../sim/worldModel.js'
import { JAMB_LINING_HEX, buildScene } from './sceneRoot.js'
import { DOOR_HEAD_HEIGHT_M, WALL_THICKNESS_M } from '../sim/constants.js'
import { KELVIN, SHADOW, SKY_HEX, buildingBox } from './lighting.js'
import { rosToThree } from './frame.js'

const lightsOf = (scene) => {
  const out = []
  scene.traverse((o) => { if (o.isLight) out.push(o) })
  return out
}
const named = (lights, re) => lights.filter((l) => re.test(l.name))
const hex = (l) => l.color.getHex()

describe('Phase 5a lighting rig (handoff_sim §6.3)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('exactly one light casts shadows, and it is the end-window sun', () => {
    const { scene } = buildScene(W)
    const casters = lightsOf(scene).filter((l) => l.castShadow)
    expect(casters).toHaveLength(1)
    expect(casters[0].isDirectionalLight).toBe(true)
    expect(casters[0].name).toBe('sun')
    expect(hex(casters[0])).toBe(KELVIN.K5200)
  })

  it('matches the light table: 4 troughs, 2 cove, 2 window fill, 1 task, 1 sun, nothing else', () => {
    const lights = lightsOf(buildScene(W).scene)
    const troughs = named(lights, /^trough_\d_light$/)
    expect(troughs).toHaveLength(4)
    for (const t of troughs) {
      expect(t.isRectAreaLight).toBe(true)
      expect([t.width, t.height]).toEqual([1.2, 0.3].map((v) => expect.closeTo(v, 9)))
      expect(hex(t)).toBe(KELVIN.K3000)
    }
    expect(troughs.map((t) => t.position.x)).toEqual([1.5, 4.5, 7.5, 10.5])
    const cove = named(lights, /^cove_\d$/)
    expect(cove).toHaveLength(2)
    for (const c of cove) {
      expect(hex(c)).toBe(KELVIN.K2700)
      expect(-c.position.z).toBeGreaterThan(0) // ROS +Y: the LEFT wall
    }
    const fill = named(lights, /^window_fill_/)
    expect(fill.map((l) => l.name).sort()).toEqual(['window_fill_end', 'window_fill_room'])
    for (const f of fill) expect(hex(f)).toBe(SKY_HEX)
    const task = named(lights, /^room_task_light_light$/)
    expect(task).toHaveLength(1)
    expect(hex(task[0])).toBe(KELVIN.K3000)
    expect(lights.filter((l) => l.isHemisphereLight || l.isAmbientLight || l.isPointLight || l.isSpotLight)).toHaveLength(0)
    expect(lights).toHaveLength(10)
  })

  it('shadow map: 2048², PCF with a consumed radius of 4', () => {
    const sun = lightsOf(buildScene(W).scene).find((l) => l.castShadow)
    expect(SHADOW.type).toBe(PCFShadowMap)
    expect(sun.shadow.mapSize.x).toBe(2048)
    expect(sun.shadow.mapSize.y).toBe(2048)
    expect(sun.shadow.radius).toBe(4)
  })

  it('the shadow frustum contains the whole building, so the sun cannot reach the room through a wall', () => {
    const sun = lightsOf(buildScene(W).scene).find((l) => l.castShadow)
    sun.updateMatrixWorld(true)
    sun.target.updateMatrixWorld(true)
    sun.shadow.updateMatrices(sun) // what WebGLShadowMap does before rendering the map
    const cam = sun.shadow.camera
    const b = buildingBox(W)
    for (const x of [b.x0, b.x1]) for (const y of [b.y0, b.y1]) for (const z of [b.z0, b.z1]) {
      const p = rosToThree({ x, y, z })
      const ndc = new Vector3(p.x, p.y, p.z).project(cam)
      for (const k of ['x', 'y', 'z']) expect(Math.abs(ndc[k]), `${x},${y},${z} ${k}`).toBeLessThanOrEqual(1)
    }
  })

  it('emissive practicals never cast; architecture does', () => {
    const { scene } = buildScene(W)
    scene.traverse((o) => {
      if (!o.isMesh) return
      if (o.userData.diffuser || o.userData.practical || o.userData.contactShadow) expect(o.castShadow, o.name).toBe(false)
      if (o.userData.solid) expect(o.castShadow, o.name).toBe(true)
    })
  })

  it('environment is not the background (windows are emissive planes)', () => {
    const { scene } = buildScene(W)
    expect(scene.background === null || scene.background instanceof Color).toBe(true)
    expect(scene.environmentIntensity).toBeGreaterThan(0)
    expect(scene.environmentIntensity).toBeLessThan(0.5)
  })

  it('RectAreaLightUniformsLib.init() runs once, before the first RectAreaLight is built', async () => {
    vi.resetModules()
    const lib = (await import('three/addons/lights/RectAreaLightUniformsLib.js')).RectAreaLightUniformsLib
    const spy = vi.spyOn(lib, 'init')
    const { buildScene: fresh } = await import('./sceneRoot.js')
    fresh(W)
    fresh(W)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(RectAreaLightUniformsLib).toBeDefined()
  })

  it('door lining: exactly the two reveal faces and the lintel soffit take the lining material', () => {
    const { scene } = buildScene(W)
    const lined = []
    scene.traverse((o) => {
      if (!o.isMesh || !Array.isArray(o.material)) return
      const g = o.geometry
      o.updateWorldMatrix(true, false)
      o.material.forEach((m, i) => {
        if (m.color.getHex() !== JAMB_LINING_HEX) return
        const box = new Box3()
        const p = new Vector3()
        const grp = g.groups[i]
        for (let k = grp.start; k < grp.start + grp.count; k += 1) box.expandByPoint(p.fromBufferAttribute(g.attributes.position, g.index.getX(k)).applyMatrix4(o.matrixWorld))
        lined.push({ name: o.name, i, box })
      })
    })
    expect(lined).toHaveLength(3)
    const [jA, jB] = W.doorway.jambs
    const faces = lined.filter((l) => l.i !== 3).map((l) => l.box)
    // Each reveal face: a plane at a jamb x, spanning the wall thickness (Three z = -ROS y), floor to head.
    expect(faces.map((b) => +b.min.x.toFixed(6)).sort()).toEqual([jA.x, jB.x].map((x) => +x.toFixed(6)).sort())
    for (const b of faces) {
      expect(b.max.x - b.min.x).toBeLessThan(1e-6) // float32 vertices
      expect(b.max.z - b.min.z).toBeCloseTo(WALL_THICKNESS_M, 6)
      expect(b.min.y).toBeCloseTo(0, 6)
    }
    const soffit = lined.find((l) => l.i === 3)
    expect(soffit.name).toBe('lintel')
    expect(soffit.box.min.y).toBeCloseTo(DOOR_HEAD_HEIGHT_M, 6)
  })

  it('the building box encloses every structural mesh', () => {
    const { corridor } = buildScene(W)
    const b = buildingBox(W)
    const lo = rosToThree({ x: b.x0, y: b.y1, z: b.z0 })
    const hi = rosToThree({ x: b.x1, y: b.y0, z: b.z1 })
    const box = new Box3(new Vector3(lo.x, lo.y, lo.z), new Vector3(hi.x, hi.y, hi.z)).expandByScalar(1e-6)
    corridor.updateMatrixWorld(true)
    corridor.traverse((o) => {
      if (o.isMesh && o.userData.solid) expect(box.containsBox(new Box3().setFromObject(o)), o.name).toBe(true)
    })
  })
})
