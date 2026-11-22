// Display helpers for head pose (docs/contracts.md section 1, "Head pose" > "Angles").
// Directions are relative to an unmirrored camera image, as the contract defines them:
// - positive yaw turns the nose toward image right (the subject's left)
// - positive pitch raises the nose
// - positive roll lowers the subject's right ear, which tilts the head toward image left

import { ANGLE_RANGES_DEG } from '../contracts/cameraFrame.js'

// Angles closer to zero than this are described as neutral.
export const NEUTRAL_DEG = 2

const WORDS = {
  yaw_deg: { label: 'Yaw', positive: 'nose toward image right', negative: 'nose toward image left', neutral: 'facing the camera' },
  pitch_deg: { label: 'Pitch', positive: 'nose up', negative: 'nose down', neutral: 'level' },
  roll_deg: { label: 'Roll', positive: 'tilted toward image left', negative: 'tilted toward image right', neutral: 'upright' },
}

export const ANGLE_FIELDS = Object.keys(WORDS)

export function formatDegrees(deg) {
  const sign = deg > 0 ? '+' : deg < 0 ? '−' : ''
  return `${sign}${Math.abs(deg).toFixed(1)}°`
}

// One angle → everything the panel shows for it.
export function describeAngle(field, deg) {
  const words = WORDS[field]
  const [min, max] = ANGLE_RANGES_DEG[field]
  const direction = Math.abs(deg) < NEUTRAL_DEG ? words.neutral : deg > 0 ? words.positive : words.negative
  return {
    field,
    label: words.label,
    text: formatDegrees(deg),
    direction,
    min,
    max,
    // Position of the value along its valid range, 0 at min and 1 at max.
    position: Math.min(1, Math.max(0, (deg - min) / (max - min))),
  }
}

// Geometry for the face drawing, in an SVG whose y axis points down.
// The nose moves toward image right with yaw and up with pitch. Positive roll lowers the
// image-left side of the face, which on screen is a counterclockwise turn, so SVG rotate() gets -roll.
export function faceGlyph({ yaw_deg, pitch_deg, roll_deg }, reach = 24) {
  const rad = (deg) => (deg * Math.PI) / 180
  return {
    noseX: reach * Math.sin(rad(yaw_deg)),
    noseY: -reach * Math.sin(rad(pitch_deg)),
    rotateDeg: -roll_deg,
  }
}

export function summarizePose(pose) {
  return ANGLE_FIELDS.map((field) => {
    const a = describeAngle(field, pose[field])
    return `${a.label} ${a.text}, ${a.direction}`
  }).join('; ')
}
