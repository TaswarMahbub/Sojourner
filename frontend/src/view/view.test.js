import { describe, expect, it } from 'vitest'
import { AgXToneMapping, Box3, Mesh, MeshStandardMaterial, Object3D, SRGBColorSpace, Vector3 } from 'three'
import { CORRIDOR_WORLD as W } from '../sim/worldModel.js'
import { AVATAR_HEIGHT_M, AVATAR_LENGTH_M, AVATAR_WIDTH_M, HANDRAIL_DIAMETER_M, HANDRAIL_HEIGHT_M } from '../sim/constants.js'
import { applyPose, rosToThree } from './frame.js'
import { buildCorridor, makeFreeSpace } from './corridor.js'
import { createAvatarProxy } from './chairView.js'
import { followCamera } from './cameraRig.js'
import { RENDERER_SETTINGS, buildScene, cappedPixelRatio } from './sceneRoot.js'

const MM = 0.001
const toRos = (v) => ({ x: v.x, y: -v.z })

describe('ROS → Three.js frame mapping', () => {
  it('maps position as (x, z, −y)', () => {
    expect(rosToThree({ x: 1, y: 2, z: 3 })).toEqual({ x: 1, y: 3, z: -2 })
  })

  it('yaw passes through unchanged: the forward vector agrees for every heading', () => {
    const o = new Object3D()
    for (let k = -8; k <= 8; k += 1) {
      const yaw = (k * Math.PI) / 8
      applyPose(o, { x: 0, y: 0, heading: yaw })
      o.updateMatrixWorld(true)
      const fwd = new Vector3(1, 0, 0).transformDirection(o.matrixWorld)
      const expected = rosToThree({ x: Math.cos(yaw), y: Math.sin(yaw) })
      expect(fwd.x).toBeCloseTo(expected.x, 12)
      expect(fwd.y).toBeCloseTo(0, 12)
      expect(fwd.z).toBeCloseTo(expected.z, 12)
    }
  })

  it('a positive (left) turn swings forward from +X toward −Z in Three.js', () => {
    const o = new Object3D()
    applyPose(o, { x: 0, y: 0, heading: 0.1 })
    o.updateMatrixWorld(true)
    const fwd = new Vector3(1, 0, 0).transformDirection(o.matrixWorld)
    expect(fwd.z).toBeLessThan(0)
  })
})

// The four ground-plane corners of a box mesh, in ROS coordinates.
function groundRect(mesh) {
  mesh.updateMatrixWorld(true)
  const g = mesh.geometry.parameters
  const pts = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sz]) => toRos(new Vector3((sx * g.width) / 2, 0, (sz * g.depth) / 2).applyMatrix4(mesh.matrixWorld)))
  return pts
}

const segDist = (p, a, b) => {
  const abx = b.x - a.x
  const aby = b.y - a.y
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * abx + (p.y - a.y) * aby) / (abx * abx + aby * aby)))
  return Math.hypot(p.x - (a.x + abx * t), p.y - (a.y + aby * t))
}
const rectBoundaryDist = (p, r) => Math.min(...r.map((q, i) => segDist(p, q, r[(i + 1) % 4])))
const inside = (p, r) => {
  let sign = 0
  for (let i = 0; i < 4; i += 1) {
    const a = r[i]
    const b = r[(i + 1) % 4]
    const c = (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)
    const s = Math.sign(c)
    if (s === 0) return false
    if (sign === 0) sign = s
    else if (s !== sign) return false
  }
  return true
}

describe('grey-box geometry is built from worldModel', () => {
  const group = buildCorridor(W, new MeshStandardMaterial())
  group.updateMatrixWorld(true)
  const boxes = group.children.filter((m) => m instanceof Mesh && m.geometry.type === 'BoxGeometry')
  const rects = boxes.map((m) => ({ id: m.name, rect: groundRect(m) }))
  const isFree = makeFreeSpace(W)

  it('every wall, jamb and rail-return collision segment lies on a visible solid face, within 1 mm', () => {
    for (const s of W.segments.filter((x) => ['wall', 'jamb', 'handrail_return'].includes(x.kind))) {
      for (let i = 0; i <= 10; i += 1) {
        const p = { x: s.a.x + ((s.b.x - s.a.x) * i) / 10, y: s.a.y + ((s.b.y - s.a.y) * i) / 10 }
        const nearest = Math.min(...rects.map((r) => rectBoundaryDist(p, r.rect)))
        expect(nearest, `${s.id} sample ${i}`).toBeLessThan(MM)
      }
    }
  })

  it('no visible solid intrudes into the free space the avatar drives in by more than 1 mm', () => {
    for (const { id, rect } of rects) {
      const cx = rect.reduce((a, p) => a + p.x, 0) / 4
      const cy = rect.reduce((a, p) => a + p.y, 0) / 4
      for (const p of rect) {
        const d = Math.hypot(cx - p.x, cy - p.y)
        const inset = { x: p.x + ((cx - p.x) * MM) / d, y: p.y + ((cy - p.y) * MM) / d }
        if (id.includes('return')) continue // rail returns sit inside the rail standoff by design
        if (id === 'lintel') continue // spans the reveal, but above the 2.10 m door head
        expect(isFree(inset), `${id} corner`).toBe(false)
      }
    }
  })

  it('each handrail has its corridor-side face on the collision line, at 0.90 m', () => {
    for (const s of W.segments.filter((x) => x.kind === 'handrail')) {
      const rail = group.getObjectByName(s.id)
      const c = toRos(rail.position)
      const r = HANDRAIL_DIAMETER_M / 2
      expect(Math.abs(Math.abs(c.y) - Math.abs(s.a.y)) - r).toBeLessThan(MM)
      expect(Math.abs(c.y)).toBeGreaterThan(Math.abs(s.a.y)) // behind the face, toward the wall
      expect(rail.position.y).toBeCloseTo(HANDRAIL_HEIGHT_M, 9)
      const box = new Box3().setFromObject(rail)
      expect(Math.abs(box.max.x - box.min.x - Math.abs(s.b.x - s.a.x))).toBeLessThan(MM)
    }
  })

  it('the doorway stays open: no solid crosses the 0.815 m opening below the door head', () => {
    const [jA, jB] = W.doorway.jambs
    for (let i = 1; i < 20; i += 1) {
      const p = { x: jA.x + ((jB.x - jA.x) * i) / 20, y: -W.bounds.wallY - W.doorway.revealDepth / 2 }
      for (const { id, rect } of rects) {
        if (id === 'lintel') continue // above the door head
        expect(inside(p, rect), `${id} blocks the doorway`).toBe(false)
      }
    }
    expect(group.getObjectByName('lintel').position.y).toBeGreaterThan(2.1)
  })
})

describe('avatar proxy', () => {
  // three stores geometry as float32, so dimensions agree to ~1e-7 m, not to double precision.
  it('is 0.650 m wide, 0.845 m long, 1.058 m tall, with its pivot marker at the footprint centre', () => {
    const a = createAvatarProxy()
    a.setPose({ x: 3, y: -0.5, heading: 0 })
    a.object.updateMatrixWorld(true)
    const body = new Box3().setFromObject(a.object.getObjectByName('proxy_body'))
    expect(body.max.x - body.min.x).toBeCloseTo(AVATAR_LENGTH_M, 6)
    expect(body.max.z - body.min.z).toBeCloseTo(AVATAR_WIDTH_M, 6)
    expect(body.max.y - body.min.y).toBeCloseTo(AVATAR_HEIGHT_M, 6)
    const marker = new Vector3().setFromMatrixPosition(a.object.getObjectByName('pivot_marker').matrixWorld)
    expect(toRos(marker).x).toBeCloseTo(3, 9)
    expect(toRos(marker).y).toBeCloseTo(-0.5, 9)
    const centre = body.getCenter(new Vector3())
    expect(toRos(centre).x).toBeCloseTo(3, 9)
    expect(toRos(centre).y).toBeCloseTo(-0.5, 9)
  })
})

describe('renderer settings and camera', () => {
  it('uses sRGB output, AgX tone mapping at exposure 1.0, no MSAA', () => {
    expect(RENDERER_SETTINGS).toEqual({ antialias: false, outputColorSpace: SRGBColorSpace, toneMapping: AgXToneMapping, toneMappingExposure: 1.0 })
  })

  it('caps pixel ratio at 1.5', () => {
    expect(cappedPixelRatio(3)).toBe(1.5)
    expect(cappedPixelRatio(2)).toBe(1.5)
    expect(cappedPixelRatio(1)).toBe(1)
  })

  it('builds the scene headless with exactly one hemisphere and one directional light', () => {
    const { scene } = buildScene(W)
    const lights = []
    scene.traverse((o) => { if (o.isLight) lights.push(o.type) })
    expect(lights.sort()).toEqual(['DirectionalLight', 'HemisphereLight'])
  })

  it('the follow camera never sits outside the building, including at spawn', () => {
    const isFree = makeFreeSpace(W)
    const poses = [W.spawn, { x: 5, y: 0, heading: 0 }, { x: 7.5, y: -0.7, heading: -Math.PI / 2 }, { x: 7.5, y: -1.7, heading: -Math.PI / 2 }, { x: 11, y: 0.5, heading: Math.PI }]
    for (const p of poses) {
      const { position } = followCamera(p, isFree)
      expect(isFree(toRos(position)), JSON.stringify(p)).toBe(true)
    }
  })
})
