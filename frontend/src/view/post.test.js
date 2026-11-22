import { describe, expect, it } from 'vitest'
import { SMAAPreset } from 'postprocessing'
import { POST, createLadder, gradeColor, postEnabledFromSearch, warmLut } from './post.js'

describe('post chain parameters match handoff_sim §6.4 exactly', () => {
  it('N8AO', () => {
    expect(POST.ao).toMatchObject({ aoRadius: 0.6, distanceFalloff: 1.0, intensity: 1.8, color: 0x2a2420, quality: 'Medium' })
    expect(POST.ao.denoiseIterations).toBeGreaterThan(0) // denoise on
  })
  it('bloom, DOF, vignette, noise, SMAA', () => {
    expect(POST.bloom).toMatchObject({ luminanceThreshold: 0.9, intensity: 0.25, radius: 0.6 })
    expect(POST.dof).toMatchObject({ focusDistance: 4.0, bokehScale: 2.0 })
    expect(POST.vignette).toEqual({ offset: 0.25, darkness: 0.5 })
    expect(POST.noise).toEqual({ opacity: 0.035, premultiply: true })
    expect(POST.smaa.preset).toBe(SMAAPreset.MEDIUM)
  })
})

describe('?post=0', () => {
  it('disables the chain only when post is exactly 0', () => {
    expect(postEnabledFromSearch('?post=0')).toBe(false)
    expect(postEnabledFromSearch('')).toBe(true)
    expect(postEnabledFromSearch('?post=1')).toBe(true)
    expect(postEnabledFromSearch('?tag=x&post=0')).toBe(false)
  })
})

describe('warm grade', () => {
  it('lifts shadows (black is not black) and never clips (white stays ≤ 1)', () => {
    const black = gradeColor([0, 0, 0])
    const white = gradeColor([1, 1, 1])
    for (const v of black) expect(v).toBeGreaterThan(0)
    for (const v of white) expect(v).toBeLessThanOrEqual(1)
  })
  it('is warm: red ≥ green ≥ blue for a neutral input', () => {
    for (const x of [0.1, 0.5, 0.9]) {
      const [r, g, b] = gradeColor([x, x, x])
      expect(r).toBeGreaterThanOrEqual(g)
      expect(g).toBeGreaterThan(b)
    }
  })
  it('is monotonic per channel', () => {
    let prev = gradeColor([0, 0, 0])
    for (let i = 1; i <= 20; i += 1) {
      const cur = gradeColor([i / 20, i / 20, i / 20])
      for (let k = 0; k < 3; k += 1) expect(cur[k]).toBeGreaterThan(prev[k])
      prev = cur
    }
  })
  it('the generated LUT applies exactly that function', () => {
    const lut = warmLut(8)
    const d = lut.image.data
    const s = 1 / 7
    // Entry (r, g, b) = (7, 0, 3): index r + g*size + b*size².
    const i = (7 + 0 * 8 + 3 * 64) * 4
    const want = gradeColor([7 * s, 0, 3 * s])
    for (let k = 0; k < 3; k += 1) expect(d[i + k]).toBeCloseTo(want[k], 6)
    lut.dispose()
  })
})

describe('adaptive ladder', () => {
  const make = () => {
    const applied = []
    const names = ['dof-off', 'ao-quality-down', 'ao-off', 'shadow-1024', 'pixel-ratio-1']
    const ladder = createLadder(names.map((name) => ({ name, apply: () => applied.push(name) })))
    return { ladder, applied }
  }
  const feed = (ladder, dt, ms) => { for (let t = 0; t < ms; t += dt) ladder.sample(dt) }

  it('never steps while frames are within budget', () => {
    const { ladder, applied } = make()
    feed(ladder, 16.7, 20000)
    feed(ladder, 19.9, 20000)
    expect(applied).toEqual([])
  })
  it('steps one rung per sustained 2 s over 20 ms, in the specified order', () => {
    const { ladder, applied } = make()
    feed(ladder, 25, 2000)
    expect(applied).toEqual(['dof-off'])
    feed(ladder, 25, 2000)
    expect(applied).toEqual(['dof-off', 'ao-quality-down'])
    feed(ladder, 25, 20000)
    expect(applied).toEqual(['dof-off', 'ao-quality-down', 'ao-off', 'shadow-1024', 'pixel-ratio-1'])
    expect(ladder.level).toBe(5)
  })
  it('a short spike does not step', () => {
    const { ladder, applied } = make()
    for (let i = 0; i < 10; i += 1) ladder.sample(40)
    feed(ladder, 16.7, 1700)
    expect(applied).toEqual([])
  })
  it('a pause (hidden tab) resets the window instead of counting', () => {
    const { ladder, applied } = make()
    feed(ladder, 25, 1900)
    ladder.sample(5000)
    feed(ladder, 25, 1900)
    expect(applied).toEqual([])
  })
  it('never steps back up', () => {
    const { ladder, applied } = make()
    feed(ladder, 25, 2000)
    feed(ladder, 8, 60000)
    expect(applied).toEqual(['dof-off'])
  })
})
