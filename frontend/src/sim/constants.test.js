import { describe, expect, it } from 'vitest'
import * as C from './constants.js'

describe('constants', () => {
  it('doorway is ADA 815 mm (answers §7 overrides the handoff 0.81 m)', () => {
    expect(C.DOORWAY_CLEAR_WIDTH_M).toBe(0.815)
  })

  it('per-side doorway clearance is 82.5 mm, between ISO 7193 minimum 50 mm and preferred 100 mm', () => {
    expect(C.DOORWAY_CLEARANCE_PER_SIDE_M).toBeCloseTo(0.0825, 12)
    expect(C.DOORWAY_CLEARANCE_PER_SIDE_M).toBeGreaterThan(0.05)
    expect(C.DOORWAY_CLEARANCE_PER_SIDE_M).toBeLessThan(0.1)
  })

  it('avatar scale maps the 140 mm chassis to 0.650 m, folded into one STL factor', () => {
    expect(C.AVATAR_SCALE).toBeCloseTo(4.642857142857143, 12)
    expect(C.STL_MM_TO_SCENE).toBeCloseTo(0.004642857142857143, 15)
    expect(140 * C.STL_MM_TO_SCENE).toBeCloseTo(C.AVATAR_WIDTH_M, 12)
  })

  it('rig STL length scales to the 0.845 m avatar length', () => {
    const lengthMm = C.RIG_STL.bbox.x[1] - C.RIG_STL.bbox.x[0]
    expect(lengthMm).toBe(182)
    expect(lengthMm * C.STL_MM_TO_SCENE).toBeCloseTo(C.AVATAR_LENGTH_M, 3)
  })

  it('swing radius is half the footprint diagonal', () => {
    expect(C.SWING_RADIUS_M).toBeCloseTo(Math.hypot(C.AVATAR_LENGTH_M, C.AVATAR_WIDTH_M) / 2, 4)
  })

  it('handrails reduce the corridor to 2.02 m effective, 0.954 m over the swing diameter', () => {
    expect(C.HANDRAIL_PROJECTION_M).toBeCloseTo(C.HANDRAIL_STANDOFF_M + C.HANDRAIL_DIAMETER_M, 12)
    expect(C.CORRIDOR_EFFECTIVE_WIDTH_M).toBeCloseTo(2.02, 12)
    expect(C.CORRIDOR_EFFECTIVE_WIDTH_M - 2 * Math.hypot(C.AVATAR_LENGTH_M, C.AVATAR_WIDTH_M) / 2).toBeCloseTo(0.954, 3)
  })

  it('every provisional chassis value is flagged CONFIRM with no fabricated measurer', () => {
    for (const [name, entry] of Object.entries(C.CHASSIS_MM)) {
      expect(entry.status, name).toBe('CONFIRM')
      expect(entry.measuredBy, name).toBeNull()
    }
    expect(C.DECK_TOP_Z_MM).toBeLessThanOrEqual(C.CHASSIS_ENVELOPE_MM.height)
  })

  it('timestep is 1/120 s and the accumulator ceiling 0.25 s', () => {
    expect(C.SIM_STEP_S).toBe(1 / 120)
    expect(C.MAX_ACCUMULATOR_S).toBe(0.25)
    expect(C.NEAR_MISS_THRESHOLD_M).toBe(0.05)
  })
})
