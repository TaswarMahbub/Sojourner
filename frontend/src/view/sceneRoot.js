// Renderer boot and the grey-box scene (handoff Phase 2, T2.1–T2.3).
//
//  - WebGLRenderer, not WebGPURenderer (T0.4). antialias off: the Phase 6 post chain owns AA.
//  - sRGB output, AgX tone mapping, exposure 1.0.
//  - Pixel ratio capped at min(devicePixelRatio, 1.5) from the start, so development never runs
//    at a resolution the demo machine cannot hit.
//  - One neutral grey MeshStandardMaterial, one HemisphereLight, one DirectionalLight.
//
// The view only READS the simulation: setPose() takes the interpolated pose the simulation hands
// out. Nothing here integrates, steps, or feeds anything back (invariants I1, I3).

import {
  AgXToneMapping,
  Color,
  DirectionalLight,
  HemisphereLight,
  MeshStandardMaterial,
  PerspectiveCamera,
  SRGBColorSpace,
  Scene,
  WebGLRenderer,
} from 'three'
import { buildCorridor } from './corridor.js'
import { createAvatarProxy } from './chairView.js'
import { FOV_DEG, createFollowRig } from './cameraRig.js'
import { rosToThree } from './frame.js'

export const MAX_PIXEL_RATIO = 1.5
export const cappedPixelRatio = (dpr = globalThis.devicePixelRatio || 1) => Math.min(dpr, MAX_PIXEL_RATIO)

export const RENDERER_SETTINGS = Object.freeze({
  antialias: false,
  outputColorSpace: SRGBColorSpace,
  toneMapping: AgXToneMapping,
  toneMappingExposure: 1.0,
})

/** The scene graph, without a renderer. Separate so it can be built and checked headless. */
export function buildScene(world) {
  const scene = new Scene()
  scene.background = new Color(0x8c8f94)
  const grey = new MeshStandardMaterial({ color: 0xa3a3a3, roughness: 0.85, metalness: 0 })
  const corridor = buildCorridor(world, grey)
  scene.add(corridor)

  const hemi = new HemisphereLight(0xffffff, 0x6b6b6b, 1.6)
  scene.add(hemi)
  // From the end-window direction, low, so walls and floor read as different planes.
  const sun = new DirectionalLight(0xffffff, 1.4)
  const from = rosToThree({ x: world.bounds.x1 + 4, y: 2, z: 5 })
  sun.position.set(from.x, from.y, from.z)
  const at = rosToThree({ x: (world.bounds.x0 + world.bounds.x1) / 2, y: 0, z: 0 })
  sun.target.position.set(at.x, at.y, at.z)
  scene.add(sun, sun.target)

  const avatar = createAvatarProxy()
  scene.add(avatar.object)
  return { scene, corridor, avatar, materials: [grey] }
}

/**
 * Create the renderer and scene on a canvas. Throws when WebGL 2 is unavailable; the caller falls
 * back to the 2D top-down view.
 */
export function createSceneRoot({ canvas, world }) {
  const renderer = new WebGLRenderer({ canvas, antialias: RENDERER_SETTINGS.antialias, powerPreference: 'high-performance' })
  renderer.outputColorSpace = RENDERER_SETTINGS.outputColorSpace
  renderer.toneMapping = RENDERER_SETTINGS.toneMapping
  renderer.toneMappingExposure = RENDERER_SETTINGS.toneMappingExposure
  renderer.setPixelRatio(cappedPixelRatio())

  const { scene, corridor, avatar, materials } = buildScene(world)
  const camera = new PerspectiveCamera(FOV_DEG, 16 / 9, 0.05, 60)
  const rig = createFollowRig(camera, world)
  avatar.setPose(world.spawn)
  rig.update(world.spawn)

  return {
    renderer,
    scene,
    camera,
    /** Draw one frame at the given (interpolated) ROS pose. */
    frame(pose) {
      avatar.setPose(pose)
      rig.update(pose)
      renderer.render(scene, camera)
    },
    resize(width, height) {
      if (!(width > 0 && height > 0)) return
      renderer.setPixelRatio(cappedPixelRatio())
      renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
    },
    dispose() {
      corridor.traverse((o) => o.geometry?.dispose())
      avatar.dispose()
      for (const m of materials) m.dispose()
      renderer.dispose()
    },
  }
}
