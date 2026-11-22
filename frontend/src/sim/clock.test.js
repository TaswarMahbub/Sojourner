import { describe, expect, it } from 'vitest'
import { FixedClock, interpolatePose } from './clock.js'

describe('FixedClock', () => {
  it('issues whole 1/120 s steps with no float drift on round milliseconds', () => {
    const c = new FixedClock()
    expect(c.advance(0.1)).toBe(12)
    expect(c.advance(0.2)).toBe(24)
    expect(c.alpha).toBeLessThan(1e-6)
    expect(c.timeS).toBeCloseTo(0.3, 12)
  })

  it('carries the remainder between calls and exposes it as alpha in [0, 1)', () => {
    const c = new FixedClock()
    expect(c.advance(0.005)).toBe(0) // 0.6 step banked
    expect(c.alpha).toBeCloseTo(0.6, 9)
    expect(c.advance(0.005)).toBe(1) // 1.2 → 1 step, 0.2 carried
    expect(c.alpha).toBeCloseTo(0.2, 9)
  })

  it('is frame-independent: the same elapsed time gives the same steps however it is sliced', () => {
    const slices = [
      [1],
      Array(1000).fill(0.001),
      Array(60).fill(1 / 60),
      [0.0167, 0.0331, 0.016, 0.2, 0.1342, 0.6],
    ]
    const totals = slices.map((s) => {
      const c = new FixedClock({ maxAccumulatorS: Infinity })
      return s.reduce((n, e) => n + c.advance(e), 0)
    })
    expect(new Set(totals)).toEqual(new Set([120]))
  })

  it('clamps banked time at 0.25 s so a backgrounded tab cannot run a thousand steps', () => {
    const c = new FixedClock()
    expect(c.advance(10)).toBe(30)
    expect(c.advance(0)).toBe(0)
  })

  it('ignores negative and non-finite elapsed time', () => {
    const c = new FixedClock()
    expect(c.advance(-1)).toBe(0)
    expect(c.advance(NaN)).toBe(0)
    expect(c.advance(Infinity)).toBe(0)
    expect(c.steps).toBe(0)
  })

  it('reset clears steps and remainder', () => {
    const c = new FixedClock()
    c.advance(0.123)
    c.reset()
    expect(c.steps).toBe(0)
    expect(c.alpha).toBe(0)
  })
})

describe('interpolatePose', () => {
  it('blends position linearly and yaw the short way round', () => {
    const p = interpolatePose({ x: 0, y: 0, heading: Math.PI - 0.1 }, { x: 1, y: 2, heading: -Math.PI + 0.1 }, 0.5)
    expect(p.x).toBeCloseTo(0.5)
    expect(p.y).toBeCloseTo(1)
    expect(Math.abs(Math.cos(p.heading) + 1)).toBeLessThan(1e-9) // halfway through ±π
  })
})
