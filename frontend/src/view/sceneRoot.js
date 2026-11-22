// Renderer boot and the grey-box scene (handoff Phase 2, T2.1–T2.3).
//
//  - WebGLRenderer, not WebGPURenderer (T0.4). antialias off: the Phase 6 post chain owns AA.
//  - sRGB output, AgX tone mapping, exposure 1.0.
//  - Pixel ratio capped at min(devicePixelRatio, 1.5) from the start, so development never runs
//    at a resolution the demo machine cannot hit.
//  - One neutral grey MeshStandardMaterial for the architecture (materials are Phase 4).
//  - Phase 5a lighting rig from lighting.js (exactly one shadow caster, PCF radius 4), plus PMREM
//    RoomEnvironment as scene.environment at low intensity — never as scene.background.
//
// The view only READS the simulation: setPose() takes the interpolated pose the simulation hands
// out. Nothing here integrates, steps, or feeds anything back (invariants I1, I3).

import {
  AgXToneMapping,
  Color,
  MeshStandardMaterial,
  PMREMGenerator,
  PerspectiveCamera,
  SRGBColorSpace,
  Scene,
  WebGLRenderer,
} from 'three'
import { buildCorridor, disposeCorridor } from './corridor.js'
import { createAvatarProxy } from './chairView.js'
import { FOV_DEG, NEAR_M, createFollowRig } from './cameraRig.js'
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js'
import { LIGHT_LEVELS, SHADOW, applyShadowFlags, buildLighting, diffuserMaterial, disposeLighting } from './lighting.js'

export const MAX_PIXEL_RATIO = 1.5
export const cappedPixelRatio = (dpr = globalThis.devicePixelRatio || 1) => Math.min(dpr, MAX_PIXEL_RATIO)

/** Architecture grey (no materials until Phase 4). */
export const WALL_HEX = 0xa3a3a3
/**
 * Door lining (reveal faces + head soffit). Lighter than the wall so the opening reads at every
 * height: no light in the §6.3 table can make a reveal facing along the corridor brighter than the
 * wall beside it. ~30+ LRV points above the wall, per door-frame contrast practice (BS 8300).
 * Chosen by Mridhul, 2026-11-22. Pulled forward from Phase 4; re-check when materials land.
 */
export const JAMB_LINING_HEX = 0xe0ddd6

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
  const grey = new MeshStandardMaterial({ color: WALL_HEX, roughness: 0.85, metalness: 0 })
  const lining = new MeshStandardMaterial({ color: JAMB_LINING_HEX, roughness: 0.85, metalness: 0 })
  const diffuser = diffuserMaterial()
  const corridor = buildCorridor(world, { surface: grey, reveal: lining, diffuser })
  scene.add(corridor)

  const lighting = buildLighting(world, corridor)
  scene.add(...lighting.lights, lighting.sun.target, ...lighting.objects)
  scene.environmentIntensity = LIGHT_LEVELS.environment

  const avatar = createAvatarProxy()
  scene.add(avatar.object)
  applyShadowFlags(corridor)
  avatar.object.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.receiveShadow = true } })
  return { scene, corridor, avatar, lighting, materials: [grey, lining, diffuser] }
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
  renderer.shadowMap.enabled = true
  renderer.shadowMap.type = SHADOW.type

  const { scene, corridor, avatar, lighting, materials } = buildScene(world)
  // Soft ambient and reflections, zero downloads. Environment only; the windows are emissive planes.
  const pmrem = new PMREMGenerator(renderer)
  const envScene = new RoomEnvironment()
  const envTarget = pmrem.fromScene(envScene, 0.04)
  envScene.dispose()
  pmrem.dispose()
  scene.environment = envTarget.texture
  const camera = new PerspectiveCamera(FOV_DEG, 16 / 9, NEAR_M, 60)
  const rig = createFollowRig(camera, world, corridor)
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
      disposeCorridor(corridor)
      disposeLighting(lighting)
      envTarget.dispose()
      avatar.dispose()
      for (const m of materials) m.dispose()
      renderer.dispose()
    },
  }
}
