import { describe, expect, it } from 'vitest'
import {
  REASONS, angleFor, ease, formatInUnit, niceMax, ringsFor, scaleFor, shapePoints, spokesFrom, unboundFrom,
} from './envelope.js'
import broad from '../fixtures/calibration-profiles/broad.json'
import twoNarrow from '../fixtures/calibration-profiles/two-narrow.json'
import singleEyebrow from '../fixtures/calibration-profiles/single-eyebrow.json'

describe('envelope spokes from a profile', () => {
  it('has one spoke per bound channel, in binding order', () => {
    expect(spokesFrom(broad)).toHaveLength(6)
    expect(spokesFrom(twoNarrow).map((s) => s.channel)).toEqual(['jawOpen', 'yaw_deg'])
    expect(spokesFrom(singleEyebrow).map((s) => s.channel)).toEqual(['browOuterUpLeft'])
  })

  it("computes SNR the way perception does (matches the profile's own enrolment metrics)", () => {
    for (const p of [broad, twoNarrow, singleEyebrow]) {
      for (const s of spokesFrom(p)) expect(s.snr).toBeCloseTo(p.enrolment_metrics.channels[s.channel].snr, 9)
    }
  })

  it('puts the deadband inside the threshold, and both inside the movement', () => {
    for (const s of spokesFrom(broad)) {
      expect(s.deadbandSnr).toBeLessThan(s.thresholdSnr)
      expect(s.thresholdSnr).toBeLessThan(s.snr)
    }
  })

  it('lists every unbound channel with its status and reasons', () => {
    const unbound = unboundFrom(singleEyebrow)
    expect(unbound.length).toBe(singleEyebrow.enrolment_metrics.ranking.length - 1)
    expect(unbound.every((u) => u.status === 'disqualified' && u.reasons.length > 0)).toBe(true)
  })

  it('has a plain-words explanation for every reason the Python can give', () => {
    const seen = new Set([broad, twoNarrow, singleEyebrow].flatMap((p) => unboundFrom(p).flatMap((u) => u.reasons)))
    for (const reason of seen) expect(REASONS[reason]).toBeTruthy()
    expect(Object.keys(REASONS).sort()).toEqual([
      'excursion_below_floor', 'false_positive_in_held_out_rest', 'insufficient_data', 'rest_suspect', 'snr_below_floor', 'zero_variance',
    ])
  })
})

describe('scale', () => {
  it('rounds up to 1, 2, 2.5 or 5 times a power of ten, never below 10', () => {
    expect(niceMax(131)).toBe(200)
    expect(niceMax(59.3)).toBe(100)
    expect(niceMax(21)).toBe(25)
    expect(niceMax(250)).toBe(250)
    expect(niceMax(3)).toBe(10)
  })

  it('rescales to fit the strongest bound movement', () => {
    expect(scaleFor(spokesFrom(broad))).toBe(200)
    expect(scaleFor(spokesFrom(twoNarrow))).toBe(50)
    expect(scaleFor(spokesFrom(singleEyebrow))).toBe(100)
    expect(ringsFor(200)).toEqual([50, 100, 150, 200])
  })
})

describe('shape', () => {
  const near = (p, q) => {
    expect(p.x).toBeCloseTo(q.x, 9)
    expect(p.y).toBeCloseTo(q.y, 9)
  }

  it('points the first spoke straight up and spaces the rest evenly', () => {
    near({ x: Math.cos(angleFor(0, 4)), y: Math.sin(angleFor(0, 4)) }, { x: 0, y: -1 })
    near({ x: Math.cos(angleFor(1, 4)), y: Math.sin(angleFor(1, 4)) }, { x: 1, y: 0 })
  })

  it('one channel is a sliver: three points, a thin base, the tip at its value', () => {
    const pts = shapePoints([50], 100, 100, 3)
    expect(pts).toHaveLength(3)
    near(pts[1], { x: 0, y: -50 })
    expect(Math.hypot(pts[0].x - pts[2].x, pts[0].y - pts[2].y)).toBeCloseTo(6, 9)
  })

  it('two channels make a thin diamond; three or more a polygon through each value', () => {
    expect(shapePoints([50, 25], 100, 100)).toHaveLength(4)
    const tri = shapePoints([100, 50, 100], 100, 100)
    expect(tri).toHaveLength(3)
    expect(Math.hypot(tri[1].x, tri[1].y)).toBeCloseTo(50, 9)
  })

  it('never draws past the rim, and draws nothing for no channels', () => {
    const [p] = shapePoints([500, 1, 1], 100, 100)
    expect(Math.hypot(p.x, p.y)).toBeCloseTo(100, 9)
    expect(shapePoints([], 100, 100)).toEqual([])
  })
})

describe('units and animation helpers', () => {
  it('writes every value with its unit', () => {
    expect(formatInUnit(0.2264, 'score')).toBe('0.23 score')
    expect(formatInUnit(29.247, 'deg')).toBe('29.2°')
    expect(formatInUnit(1.71, 'cm')).toBe('1.7 cm')
  })

  it('eases from 0 to 1 and clamps', () => {
    expect(ease(-1)).toBe(0)
    expect(ease(0.5)).toBe(0.5)
    expect(ease(2)).toBe(1)
  })
})
