// Phase 6 — post-processing (handoff_sim §6.4). pmndrs `postprocessing`, not three's EffectComposer.
//
// The table, in order:
//   1 RenderPass
//   2 N8AOPostPass      aoRadius 0.60 m, distanceFalloff 1.0, intensity 1.8, colour #2a2420, Medium, denoise on
//   3 BloomEffect       luminanceThreshold 0.90, intensity 0.25, radius 0.60
//   4 DepthOfField      focus 4.0 m from the camera, bokehScale 2.0, very mild (focusRange 4.0)
//   5 Colour grade      warm LUT + slight shadow lift (generated here, no asset)
//   6 VignetteEffect    offset 0.25, darkness 0.50
//   7 NoiseEffect       opacity 0.035, premultiplied
//   8 SMAAEffect        last
//
// Not in the table but required (handoff_sim §9): AgX tone mapping as an effect between 4 and 5.
// With a composer, the scene renders into a float target and three applies renderer.toneMapping
// only when drawing to the screen, so without this the chain would run untone-mapped. Bloom and DOF
// stay in scene-linear HDR (before it); the grade, vignette and grain are display-referred (after).
//
// No SSR. The DOM overlay, the intent cone and the top-down inset are never in this chain (I7).
// `?post=0` bypasses the composer entirely: the renderer draws straight to the canvas with AgX.
//
// Invariant I4: nothing here is read by the simulation. View-only.

import { Color, HalfFloatType } from 'three'
import {
  BloomEffect,
  DepthOfFieldEffect,
  EffectComposer,
  EffectPass,
  LUT3DEffect,
  LookupTexture,
  NoiseEffect,
  RenderPass,
  SMAAEffect,
  SMAAPreset,
  ToneMappingEffect,
  ToneMappingMode,
  VignetteEffect,
} from 'postprocessing'
import { N8AOPostPass } from 'n8ao'

export const POST = Object.freeze({
  ao: Object.freeze({ aoRadius: 0.6, distanceFalloff: 1.0, intensity: 1.8, color: 0x2a2420, quality: 'Medium', denoiseIterations: 2 }),
  bloom: Object.freeze({ luminanceThreshold: 0.9, intensity: 0.25, radius: 0.6, mipmapBlur: true }),
  dof: Object.freeze({ focusDistance: 4.0, focusRange: 4.0, bokehScale: 2.0 }),
  grade: Object.freeze({ size: 32, lift: 0.025, gain: Object.freeze([1.0, 0.985, 0.94]) }),
  vignette: Object.freeze({ offset: 0.25, darkness: 0.5 }),
  noise: Object.freeze({ opacity: 0.035, premultiply: true }),
  smaa: Object.freeze({ preset: SMAAPreset.MEDIUM }),
})

/** Is the chain enabled for this page? `?post=0` disables it. */
export const postEnabledFromSearch = (search = globalThis.location?.search ?? '') => new URLSearchParams(search).get('post') !== '0'

/**
 * The warm grade as a pure function of an sRGB-encoded colour in [0, 1]: a slight shadow lift, then a
 * warm gain that never exceeds 1, so the grade can neither clip nor crush.
 */
export function gradeColor([r, g, b], { lift, gain } = POST.grade) {
  const l = (v, k) => (lift + (1 - lift) * v) * gain[k]
  return [l(r, 0), l(g, 1), l(b, 2)]
}

/** The warm LUT, generated (zero downloads). */
export function warmLut(size = POST.grade.size) {
  const lut = LookupTexture.createNeutral(size)
  const d = lut.image.data
  for (let i = 0; i < d.length; i += 4) {
    const [r, g, b] = gradeColor([d[i], d[i + 1], d[i + 2]])
    d[i] = r
    d[i + 1] = g
    d[i + 2] = b
  }
  lut.needsUpdate = true
  lut.name = 'warm-grade'
  return lut
}

/**
 * Adaptive ladder (§6.4): frame time over `thresholdMs` sustained for `sustainMs` steps down one rung,
 * then the window restarts so each rung gets a fair trial. One-way: it never steps back up (no
 * oscillation in a demo). A gap over `pauseMs` (hidden tab, breakpoint) resets the window instead of
 * counting. Pure, so it is tested without a GPU. Never touches the simulation.
 */
export function createLadder(rungs, { thresholdMs = 20, sustainMs = 2000, pauseMs = 250 } = {}) {
  let level = 0
  let windowMs = 0
  let count = 0
  return {
    get level() { return level },
    get applied() { return rungs.slice(0, level).map((r) => r.name) },
    /** Feed one frame interval; returns the rung name applied on this call, or null. */
    sample(dtMs) {
      if (!(dtMs > 0) || dtMs > pauseMs) { windowMs = 0; count = 0; return null }
      windowMs += dtMs
      count += 1
      if (windowMs < sustainMs) return null
      // "Over 20 ms sustained for 2 s": the mean frame time across a full 2 s window exceeds 20 ms.
      const sustained = windowMs / count > thresholdMs
      windowMs = 0
      count = 0
      if (!sustained || level >= rungs.length) return null
      const rung = rungs[level]
      level += 1
      rung.apply()
      return rung.name
    },
  }
}

/**
 * Build the composer. Returns null-safe handles the scene root uses for resize, the ladder and
 * disposal.
 */
export function createPostChain(renderer, scene, camera) {
  const composer = new EffectComposer(renderer, { frameBufferType: HalfFloatType, multisampling: 0 })
  composer.addPass(new RenderPass(scene, camera))

  const ao = new N8AOPostPass(scene, camera, 1, 1)
  ao.configuration.aoRadius = POST.ao.aoRadius
  ao.configuration.distanceFalloff = POST.ao.distanceFalloff
  ao.configuration.intensity = POST.ao.intensity
  ao.configuration.color = new Color(POST.ao.color)
  ao.configuration.denoiseIterations = POST.ao.denoiseIterations
  ao.setQualityMode(POST.ao.quality)
  composer.addPass(ao)

  const bloom = new BloomEffect(POST.bloom)
  composer.addPass(new EffectPass(camera, bloom))

  const dof = new DepthOfFieldEffect(camera, POST.dof)
  const toneMapping = new ToneMappingEffect({ mode: ToneMappingMode.AGX })
  const lut = warmLut()
  const grade = new LUT3DEffect(lut)
  const vignette = new VignetteEffect(POST.vignette)
  const noise = new NoiseEffect({ premultiply: POST.noise.premultiply })
  noise.blendMode.opacity.value = POST.noise.opacity
  const dofPass = new EffectPass(camera, dof, toneMapping, grade, vignette, noise)
  composer.addPass(dofPass)

  const smaa = new SMAAEffect(POST.smaa)
  composer.addPass(new EffectPass(camera, smaa))

  return { composer, ao, bloom, dof, dofPass, toneMapping, grade, lut, vignette, noise, smaa }
}
