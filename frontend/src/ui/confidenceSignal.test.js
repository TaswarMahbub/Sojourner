import { describe, expect, it } from 'vitest'
import broad from '../fixtures/calibration-profiles/broad.json'
import { NO_PICK, channelMargin, confidenceSignal, describeSignal, marginsFor, pickActive } from './confidenceSignal.js'
import { RING_MAX_PX, RING_MIN_PX, SWITCH_FRAMES } from './confidenceConstants.js'

const jaw = broad.bindings.find((b) => b.channel === 'jawOpen')
const frame = (changes = {}) => ({ tracking_state: 'tracking', features: {}, head_pose: null, confidence: null, ...changes })
const latest = (message, receivedAtMs = 1000, valid = true) => ({ message, validation: { valid, errors: [] }, receivedAtMs })
// jawOpen value that sits `m` deadband widths past its threshold.
const jawAt = (m) => jaw.rest_mean + jaw.threshold.offset + m * jaw.deadband.offset

describe('confidenceSignal', () => {
  it('tracking, fresh, no command: full thickness, good', () => {
    expect(confidenceSignal({ latest: latest(frame()), nowMs: 1000, margin: null })).toMatchObject({ value: 1, thicknessPx: RING_MAX_PX, tone: 'good', worst: null })
  })

  it('covering the lens (face lost) thins the ring to its floor, never to zero, and turns it bad', () => {
    const s = confidenceSignal({ latest: latest(frame({ tracking_state: 'lost' })), nowMs: 1000, margin: null })
    expect(s.thicknessPx).toBe(RING_MIN_PX)
    expect(s.thicknessPx).toBeGreaterThan(0)
    expect(s).toMatchObject({ tone: 'bad', worst: 'tracking' })
  })

  it('a partial face halves the thickness range and warns', () => {
    const s = confidenceSignal({ latest: latest(frame({ tracking_state: 'partial' })), nowMs: 1000, margin: null })
    expect(s.thicknessPx).toBeCloseTo(RING_MIN_PX + (RING_MAX_PX - RING_MIN_PX) * 0.5)
    expect(s).toMatchObject({ tone: 'warn', worst: 'tracking' })
  })

  it('freshness reaches 0 at 250 ms, but its colour warns at 120 ms and is bad at 180 ms', () => {
    const at = (age) => confidenceSignal({ latest: latest(frame()), nowMs: 1000 + age, margin: null })
    expect(at(119).tone).toBe('good')
    expect(at(120)).toMatchObject({ tone: 'warn', worst: 'freshness' })
    expect(at(180)).toMatchObject({ tone: 'bad', worst: 'freshness' })
    expect(at(250).value).toBe(0)
    expect(at(250).thicknessPx).toBe(RING_MIN_PX)
  })

  it('colour comes from the worst term, not the product', () => {
    // Three middling terms: the product is low, but nothing is badly wrong.
    const middling = confidenceSignal({ latest: latest(frame({ tracking_state: 'partial' })), nowMs: 1000 + 100, margin: 0.7 })
    expect(middling.value).toBeLessThan(0.33)
    expect(middling.tone).toBe('warn')
    // One term badly wrong, the rest perfect: bad, and it names that term.
    expect(confidenceSignal({ latest: latest(frame()), nowMs: 1000, margin: 0.1 })).toMatchObject({ tone: 'bad', worst: 'margin' })
  })

  it('a rejected camera message counts as no tracking', () => {
    expect(confidenceSignal({ latest: latest(null, 1000, false), nowMs: 1000, margin: null })).toMatchObject({ value: 0, tone: 'bad', worst: 'tracking' })
  })

  it('no camera data at all: floor thickness, neutral colour, value null', () => {
    expect(confidenceSignal({ latest: null, nowMs: 0, margin: null })).toEqual({ value: null, thicknessPx: RING_MIN_PX, tone: 'neutral', worst: null, terms: null })
  })

  it('describes itself in words', () => {
    expect(describeSignal(confidenceSignal({ latest: latest(frame({ tracking_state: 'lost' })), nowMs: 1000, margin: null }))).toBe('Signal confidence: poor, face lost')
    expect(describeSignal(confidenceSignal({ latest: latest(frame()), nowMs: 1150, margin: null }))).toBe('Signal confidence: weak, frame 150 ms old')
    expect(describeSignal(confidenceSignal({ latest: null, nowMs: 0, margin: null }))).toBe('Signal confidence: no camera data')
  })
})

describe('channelMargin', () => {
  it('is 0 at the threshold, 1 one deadband past it, clamped beyond', () => {
    expect(channelMargin(frame({ features: { jawOpen: jawAt(0) } }), jaw)).toBeCloseTo(0)
    expect(channelMargin(frame({ features: { jawOpen: jawAt(0.5) } }), jaw)).toBeCloseTo(0.5)
    expect(channelMargin(frame({ features: { jawOpen: jawAt(3) } }), jaw)).toBe(1)
    expect(channelMargin(frame({ features: { jawOpen: jaw.rest_mean } }), jaw)).toBe(0)
  })

  it('reads head-pose channels from head_pose, in the binding direction', () => {
    const yaw = broad.bindings.find((b) => b.channel === 'yaw_deg')
    const past = yaw.rest_mean + yaw.direction * (yaw.threshold.offset + yaw.deadband.offset)
    expect(channelMargin(frame({ head_pose: { yaw_deg: past, pitch_deg: 0, roll_deg: 0 } }), yaw)).toBeCloseTo(1)
  })

  it('is null for a channel the frame does not carry', () => {
    expect(channelMargin(frame(), jaw)).toBeNull()
  })
})

describe('pickActive (hysteresis)', () => {
  it('takes the largest margin first', () => {
    expect(pickActive(NO_PICK, { jawOpen: 0.4, browInnerUp: 0.2 }).channel).toBe('jawOpen')
  })

  it('does not switch for a small lead or a brief one', () => {
    let s = pickActive(NO_PICK, { jawOpen: 0.4, browInnerUp: 0.2 })
    s = pickActive(s, { jawOpen: 0.4, browInnerUp: 0.5 }) // leads by 0.1: below SWITCH_MARGIN
    expect(s.channel).toBe('jawOpen')
    for (let i = 0; i < SWITCH_FRAMES - 1; i += 1) s = pickActive(s, { jawOpen: 0.1, browInnerUp: 0.9 })
    expect(s.channel).toBe('jawOpen')
    s = pickActive(s, { jawOpen: 0.4, browInnerUp: 0.2 }) // the lead stops: the count resets
    for (let i = 0; i < SWITCH_FRAMES - 1; i += 1) s = pickActive(s, { jawOpen: 0.1, browInnerUp: 0.9 })
    expect(s.channel).toBe('jawOpen')
  })

  it('switches after a clear lead for SWITCH_FRAMES consecutive frames', () => {
    let s = pickActive(NO_PICK, { jawOpen: 0.4, browInnerUp: 0.2 })
    for (let i = 0; i < SWITCH_FRAMES; i += 1) s = pickActive(s, { jawOpen: 0.1, browInnerUp: 0.9 })
    expect(s.channel).toBe('browInnerUp')
  })

  it('marginsFor covers only bound channels the frame carries', () => {
    expect(Object.keys(marginsFor(frame({ features: { jawOpen: jawAt(0.2), eyeBlinkLeft: 1 } }), broad))).toEqual(['jawOpen'])
  })
})
