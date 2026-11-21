import { describe, expect, it } from 'vitest'
import {
  BLENDSHAPE_NAMES,
  anglesFromMatrix,
  isActionable,
  poseMatrixFromAngles,
  validateCameraFrame,
} from './cameraFrame.js'
import { MOCK_CYCLE_MS, buildMockFrame } from '../data/mockCameraSource.js'

// Synthetic fixtures built from the contract text. Not recorded frames.
const lostFrame = () => ({
  schema_version: '1',
  timestamp_ms: 1795276800000,
  tracking_state: 'lost',
  features: {},
  head_pose: null,
  confidence: 0,
})

const trackingFrame = (angles = { yaw_deg: 20, pitch_deg: -10, roll_deg: 5 }) => ({
  schema_version: '1',
  timestamp_ms: 1795276800000,
  tracking_state: 'tracking',
  features: Object.fromEntries(BLENDSHAPE_NAMES.map((n) => [n, 0])),
  head_pose: { matrix: poseMatrixFromAngles(angles), ...angles },
  confidence: 0.9,
})

describe('camera frame contract (schema 1)', () => {
  it('lists 52 unique blendshape names starting with _neutral', () => {
    expect(BLENDSHAPE_NAMES).toHaveLength(52)
    expect(new Set(BLENDSHAPE_NAMES).size).toBe(52)
    expect(BLENDSHAPE_NAMES[0]).toBe('_neutral')
  })

  it('accepts the lost-face encoding illustration, which is not actionable', () => {
    expect(validateCameraFrame(lostFrame())).toEqual({ valid: true, errors: [] })
    expect(isActionable(lostFrame())).toBe(false)
  })

  it('accepts a full tracking frame and marks it actionable', () => {
    expect(validateCameraFrame(trackingFrame()).errors).toEqual([])
    expect(isActionable(trackingFrame())).toBe(true)
  })

  it('rejects a wrong schema_version, an unknown field and a missing field', () => {
    expect(validateCameraFrame({ ...lostFrame(), schema_version: '2' }).valid).toBe(false)
    expect(validateCameraFrame({ ...lostFrame(), mock: true }).valid).toBe(false)
    const missing = lostFrame()
    delete missing.confidence
    expect(validateCameraFrame(missing).valid).toBe(false)
  })

  it('rejects non-integer and string timestamps', () => {
    expect(validateCameraFrame({ ...lostFrame(), timestamp_ms: 1.5 }).valid).toBe(false)
    expect(validateCameraFrame({ ...lostFrame(), timestamp_ms: '2026-11-21' }).valid).toBe(false)
  })

  it('rejects tracking with a missing blendshape or an out-of-range score', () => {
    const missing = trackingFrame()
    delete missing.features.jawOpen
    expect(validateCameraFrame(missing).valid).toBe(false)
    const high = trackingFrame()
    high.features.jawOpen = 1.2
    expect(validateCameraFrame(high).valid).toBe(false)
  })

  it('enforces per-state rules for confidence, features and head_pose', () => {
    expect(validateCameraFrame({ ...lostFrame(), confidence: 0.2 }).valid).toBe(false)
    expect(validateCameraFrame({ ...trackingFrame(), confidence: 0 }).valid).toBe(false)
    const partialWithPose = { ...trackingFrame(), tracking_state: 'partial' }
    expect(validateCameraFrame(partialWithPose).valid).toBe(false)
  })

  it('rejects a matrix whose last row is not [0, 0, 0, 1] or a zero matrix', () => {
    const f = trackingFrame()
    f.head_pose.matrix[3] = [0, 0, 0, 0]
    expect(validateCameraFrame(f).valid).toBe(false)
    const zero = trackingFrame()
    zero.head_pose.matrix = [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 1]]
    expect(validateCameraFrame(zero).valid).toBe(false)
  })

  it('rejects translation outside the sanity bounds (face behind the camera)', () => {
    const f = trackingFrame()
    f.head_pose.matrix[2][3] = 10
    expect(validateCameraFrame(f).valid).toBe(false)
  })

  it('rejects non-uniform scale and angles that do not match the matrix', () => {
    const skew = trackingFrame()
    skew.head_pose.matrix[0][0] *= 1.2
    expect(validateCameraFrame(skew).valid).toBe(false)
    const mismatch = trackingFrame()
    mismatch.head_pose.yaw_deg += 0.5
    expect(validateCameraFrame(mismatch).valid).toBe(false)
  })

  it('anglesFromMatrix inverts the contract composition, independent of scale', () => {
    const angles = { yaw_deg: -35, pitch_deg: 22, roll_deg: -12 }
    const derived = anglesFromMatrix(poseMatrixFromAngles(angles, [5, -3, -60], 1.7))
    expect(derived.yaw_deg).toBeCloseTo(angles.yaw_deg, 6)
    expect(derived.pitch_deg).toBeCloseTo(angles.pitch_deg, 6)
    expect(derived.roll_deg).toBeCloseTo(angles.roll_deg, 6)
    expect(derived.scale).toBeCloseTo(1.7, 6)
  })

  it('the mock source only emits valid schema 1 frames across its whole loop', () => {
    const seen = new Set()
    for (let t = 0; t < MOCK_CYCLE_MS; t += 50) {
      const frame = buildMockFrame(t, 1795276800000 + t)
      const result = validateCameraFrame(frame)
      expect(result.errors, `t=${t}`).toEqual([])
      seen.add(frame.tracking_state)
    }
    expect([...seen].sort()).toEqual(['lost', 'partial', 'tracking'])
  })
})
