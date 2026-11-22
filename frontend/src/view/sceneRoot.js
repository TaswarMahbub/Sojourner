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
  Vector2,
  WebGLRenderer,
} from 'three'
import { buildCorridor, disposeCorridor } from './corridor.js'
import { createAvatarProxy } from './chairView.js'
import { FOV_DEG, NEAR_M, createFollowRig } from './cameraRig.js'
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js'
import { LIGHT_LEVELS, SHADOW, applyShadowFlags, buildLighting, diffuserMaterial, disposeLighting } from './lighting.js'
import { EffectPass } from 'postprocessing'
import { createLadder, createPostChain, postEnabledFromSearch } from './post.js'

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
export function createSceneRoot({ canvas, world, post = postEnabledFromSearch() }) {
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

  // Phase 6 post chain (§6.4). `?post=0` → null, and the renderer draws straight to the canvas.
  const chain = post ? createPostChain(renderer, scene, camera) : null
  let maxPixelRatio = MAX_PIXEL_RATIO
  const ratio = () => Math.min(cappedPixelRatio(), maxPixelRatio)
  const sizeNow = () => { const v = renderer.getSize(new Vector2()); return [v.x, v.y] }

  // Adaptive ladder: DOF off → AO quality down → AO off → shadow 1024 → pixel ratio 1.0. View only.
  const rungs = []
  if (chain) {
    rungs.push({
      name: 'dof-off',
      apply() {
        // Rebuild the pass without DOF so its blur passes stop running (SKIP would still render them).
        const { composer, dofPass, toneMapping, grade, vignette, noise } = chain
        const i = composer.passes.indexOf(dofPass)
        composer.removePass(dofPass)
        dofPass.dispose()
        chain.dofPass = new EffectPass(camera, toneMapping, grade, vignette, noise)
        composer.addPass(chain.dofPass, i)
      },
    })
    rungs.push({ name: 'ao-quality-down', apply() { chain.ao.setQualityMode('Low') } })
    rungs.push({ name: 'ao-off', apply() { chain.ao.enabled = false } })
  }
  rungs.push({
    name: 'shadow-1024',
    apply() {
      lighting.sun.shadow.mapSize.set(1024, 1024)
      lighting.sun.shadow.map?.dispose()
      lighting.sun.shadow.map = null
    },
  })
  rungs.push({
    name: 'pixel-ratio-1',
    apply() {
      maxPixelRatio = 1
      const [w, h] = sizeNow()
      renderer.setPixelRatio(ratio())
      if (chain) chain.composer.setSize(w, h, false)
      else renderer.setSize(w, h, false)
    },
  })
  const ladder = createLadder(rungs)
  let lastFrameAt = null

  return {
    renderer,
    scene,
    camera,
    postEnabled: Boolean(chain),
    chain,
    ladder,
    /** Draw one frame at the given (interpolated) ROS pose. */
    frame(pose, now = performance.now()) {
      const dt = lastFrameAt === null ? 0 : now - lastFrameAt
      lastFrameAt = now
      ladder.sample(dt)
      avatar.setPose(pose)
      rig.update(pose)
      if (chain) chain.composer.render(dt / 1000)
      else renderer.render(scene, camera)
    },
    resize(width, height) {
      if (!(width > 0 && height > 0)) return
      renderer.setPixelRatio(ratio())
      if (chain) chain.composer.setSize(width, height, false)
      else renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
    },
    dispose() {
      chain?.composer.dispose()
      chain?.lut.dispose()
      disposeCorridor(corridor)
      disposeLighting(lighting)
      envTarget.dispose()
      avatar.dispose()
      for (const m of materials) m.dispose()
      renderer.dispose()
    },
  }
}
