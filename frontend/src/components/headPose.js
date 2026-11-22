// Display helpers for head pose (docs/contracts.md section 1, "Head pose" > "Angles").
// Sign conventions, from the contract (defined on an unmirrored camera image):
// - positive yaw turns the nose toward image right, which is the subject's left
// - positive pitch raises the nose
// - positive roll lowers the subject's right ear
// The words below name the subject's own left and right, so they read the same in mirror
// view and camera view. Only the drawing changes between the two views.

import { ANGLE_RANGES_DEG } from '../contracts/cameraFrame.js'

// Angles closer to zero than this are described as neutral.
export const NEUTRAL_DEG = 2

const WORDS = {
  yaw_deg: { label: 'Yaw', positive: 'turned to their left', negative: 'turned to their right', neutral: 'facing the camera' },
  pitch_deg: { label: 'Pitch', positive: 'nose up', negative: 'nose down', neutral: 'level' },
  roll_deg: { label: 'Roll', positive: 'tilted toward their right shoulder', negative: 'tilted toward their left shoulder', neutral: 'upright' },
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
// Camera view: the nose moves toward image right with yaw and up with pitch. Positive roll
// lowers the image-left side of the face, a counterclockwise turn on screen, so rotate() gets -roll.
// Mirror view reflects the drawing left to right: x changes sign and so does the rotation.
export function faceGlyph({ yaw_deg, pitch_deg, roll_deg }, { mirrored = false, reach = 24 } = {}) {
  const rad = (deg) => (deg * Math.PI) / 180
  const flip = mirrored ? -1 : 1
  return {
    noseX: flip * reach * Math.sin(rad(yaw_deg)),
    noseY: -reach * Math.sin(rad(pitch_deg)),
    rotateDeg: flip * -roll_deg,
  }
}

export function summarizePose(pose) {
  return ANGLE_FIELDS.map((field) => {
    const a = describeAngle(field, pose[field])
    return `${a.label} ${a.text}, ${a.direction}`
  }).join('; ')
}
