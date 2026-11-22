import { describe, expect, it } from 'vitest'
import { describeAngle, faceGlyph, formatDegrees, summarizePose } from './headPose.js'
import { poseMatrixFromAngles } from '../contracts/cameraFrame.js'

describe('head pose display helpers', () => {
  it('formats signed degrees with one decimal', () => {
    expect(formatDegrees(12.34)).toBe('+12.3°')
    expect(formatDegrees(-5)).toBe('−5.0°')
    expect(formatDegrees(0)).toBe('0.0°')
  })

  it('uses the contract sign conventions for direction words', () => {
    expect(describeAngle('yaw_deg', 20).direction).toBe('nose toward image right')
    expect(describeAngle('yaw_deg', -20).direction).toBe('nose toward image left')
    expect(describeAngle('pitch_deg', 10).direction).toBe('nose up')
    expect(describeAngle('pitch_deg', -10).direction).toBe('nose down')
    expect(describeAngle('roll_deg', 10).direction).toBe('tilted toward image left')
    expect(describeAngle('roll_deg', -10).direction).toBe('tilted toward image right')
    expect(describeAngle('yaw_deg', 1.5).direction).toBe('facing the camera')
  })

  it('places each value along its own valid range', () => {
    expect(describeAngle('yaw_deg', 0).position).toBe(0.5)
    expect(describeAngle('yaw_deg', 90).position).toBe(1)
    expect(describeAngle('pitch_deg', -60).position).toBe(0)
    expect(describeAngle('roll_deg', 22.5).position).toBe(0.75)
  })

  it('moves the drawn nose the same way the contract rotation moves the real one', () => {
    // The nose points along +Z in the face model. R's third column is where it ends up:
    // x is image right, y is up. The SVG y axis points down, so its sign flips.
    for (const angles of [{ yaw_deg: 30, pitch_deg: 0, roll_deg: 0 }, { yaw_deg: 0, pitch_deg: 20, roll_deg: 0 }, { yaw_deg: -25, pitch_deg: -15, roll_deg: 0 }]) {
      const m = poseMatrixFromAngles(angles)
      const glyph = faceGlyph(angles)
      expect(Math.sign(glyph.noseX)).toBe(Math.sign(Math.round(m[0][2] * 1e6)))
      expect(Math.sign(glyph.noseY)).toBe(-Math.sign(Math.round(m[1][2] * 1e6)))
    }
  })

  it('turns the drawing so positive roll lowers the image-left side', () => {
    // The face's image-left side (-X) ends up at R · (-1, 0, 0). Positive roll should move it down (y < 0).
    const m = poseMatrixFromAngles({ yaw_deg: 0, pitch_deg: 0, roll_deg: 15 })
    expect(-m[1][0]).toBeLessThan(0)
    // SVG rotate(-15) is counterclockwise on screen, which lowers the left side.
    expect(faceGlyph({ yaw_deg: 0, pitch_deg: 0, roll_deg: 15 }).rotateDeg).toBe(-15)
  })

  it('summarizes the pose in words for screen readers', () => {
    expect(summarizePose({ yaw_deg: 20, pitch_deg: 0, roll_deg: -10 })).toBe(
      'Yaw +20.0°, nose toward image right; Pitch 0.0°, level; Roll −10.0°, tilted toward image right',
    )
  })
})
