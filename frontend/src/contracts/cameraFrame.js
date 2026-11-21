// Camera feature stream, docs/contracts.md section 1, schema "4".
// Every field name, range and rule for this message lives in this file.
// If the contract changes, this is the only frontend file that should change.

import { decodeJson, isIntegerField } from './json.js'

export const SCHEMA_VERSION = '4'

export const TRACKING_STATES = ['tracking', 'partial', 'lost']

// The 52 MediaPipe Face Landmarker blendshape names, in contract index order.
export const BLENDSHAPE_NAMES = [
  '_neutral',
  'browDownLeft', 'browDownRight', 'browInnerUp', 'browOuterUpLeft', 'browOuterUpRight',
  'cheekPuff', 'cheekSquintLeft', 'cheekSquintRight',
  'eyeBlinkLeft', 'eyeBlinkRight',
  'eyeLookDownLeft', 'eyeLookDownRight', 'eyeLookInLeft', 'eyeLookInRight',
  'eyeLookOutLeft', 'eyeLookOutRight', 'eyeLookUpLeft', 'eyeLookUpRight',
  'eyeSquintLeft', 'eyeSquintRight', 'eyeWideLeft', 'eyeWideRight',
  'jawForward', 'jawLeft', 'jawOpen', 'jawRight',
  'mouthClose', 'mouthDimpleLeft', 'mouthDimpleRight', 'mouthFrownLeft', 'mouthFrownRight',
  'mouthFunnel', 'mouthLeft', 'mouthLowerDownLeft', 'mouthLowerDownRight',
  'mouthPressLeft', 'mouthPressRight', 'mouthPucker', 'mouthRight',
  'mouthRollLower', 'mouthRollUpper', 'mouthShrugLower', 'mouthShrugUpper',
  'mouthSmileLeft', 'mouthSmileRight', 'mouthStretchLeft', 'mouthStretchRight',
  'mouthUpperUpLeft', 'mouthUpperUpRight', 'noseSneerLeft', 'noseSneerRight',
]

// `_neutral` is forwarded but must never drive a gesture or motion.
export const CONTROL_EXCLUDED_FEATURES = ['_neutral']

export const ANGLE_RANGES_DEG = {
  yaw_deg: [-90, 90],
  pitch_deg: [-60, 60],
  roll_deg: [-45, 45],
}

export const TRANSLATION_BOUNDS_CM = {
  x: [-100, 100],
  y: [-100, 100],
  z: [-200, -5],
}

export const SCALE_TOLERANCE = 0.01
export const ANGLE_TOLERANCE_DEG = 0.1
export const MAX_TIMESTAMP_MS = 9007199254740991

const FRAME_FIELDS = ['schema_version', 'timestamp_ms', 'tracking_state', 'features', 'head_pose', 'confidence']
const POSE_FIELDS = ['matrix', 'yaw_deg', 'pitch_deg', 'roll_deg']
const BLENDSHAPE_SET = new Set(BLENDSHAPE_NAMES)

const toDeg = (rad) => (rad * 180) / Math.PI
const toRad = (deg) => (deg * Math.PI) / 180
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

export function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

// obj[key] is a JSON integer timestamp in the safe range (Unix epoch ms).
export function isTimestampMs(obj, key) {
  return isIntegerField(obj, key) && obj[key] >= 0 && obj[key] <= MAX_TIMESTAMP_MS
}

// Pushes an error for every missing or undefined key. Shared by the contract modules.
export function checkExactKeys(obj, allowed, label, errors) {
  for (const key of Object.keys(obj)) {
    if (!allowed.includes(key)) errors.push(`${label}: unknown field "${key}"`)
  }
  for (const key of allowed) {
    if (!Object.hasOwn(obj, key)) errors.push(`${label}: missing field "${key}"`)
  }
}

// Contract "Angles" section: remove the uniform scale, then extract angles.
// Returns null when the scale is not finite or not positive.
export function anglesFromMatrix(matrix) {
  const colNorm = (c) => Math.hypot(matrix[0][c], matrix[1][c], matrix[2][c])
  const norms = [colNorm(0), colNorm(1), colNorm(2)]
  const s = (norms[0] + norms[1] + norms[2]) / 3
  if (!Number.isFinite(s) || s <= 0) return null
  const R = (r, c) => matrix[r][c] / s
  return {
    scale: s,
    scaleSpread: Math.max(...norms.map((n) => Math.abs(n - s))) / s,
    yaw_deg: toDeg(Math.atan2(R(0, 2), R(2, 2))),
    pitch_deg: toDeg(Math.asin(clamp(R(1, 2), -1, 1))),
    roll_deg: toDeg(Math.atan2(R(1, 0), R(1, 1))),
  }
}

// Contract composition R = Ry(yaw) · Rx(-pitch) · Rz(roll), plus scale and translation (cm).
// Used by mock sources and tests. Real frames come from perception.
export function poseMatrixFromAngles({ yaw_deg, pitch_deg, roll_deg }, translationCm = [0, 0, -50], scale = 1) {
  const [y, p, r] = [toRad(yaw_deg), toRad(pitch_deg), toRad(roll_deg)]
  const Ry = [[Math.cos(y), 0, Math.sin(y)], [0, 1, 0], [-Math.sin(y), 0, Math.cos(y)]]
  const Rx = [[1, 0, 0], [0, Math.cos(p), Math.sin(p)], [0, -Math.sin(p), Math.cos(p)]]
  const Rz = [[Math.cos(r), -Math.sin(r), 0], [Math.sin(r), Math.cos(r), 0], [0, 0, 1]]
  const mul = (A, B) => A.map((row, i) => B[0].map((_, j) => row.reduce((sum, _v, k) => sum + A[i][k] * B[k][j], 0)))
  const R = mul(mul(Ry, Rx), Rz)
  return [
    [...R[0].map((v) => v * scale), translationCm[0]],
    [...R[1].map((v) => v * scale), translationCm[1]],
    [...R[2].map((v) => v * scale), translationCm[2]],
    [0, 0, 0, 1],
  ]
}

function validateHeadPose(pose, errors) {
  if (!isPlainObject(pose)) {
    errors.push('head_pose: must be an object when tracking_state is "tracking"')
    return
  }
  checkExactKeys(pose, POSE_FIELDS, 'head_pose', errors)

  for (const [field, [lo, hi]] of Object.entries(ANGLE_RANGES_DEG)) {
    const v = pose[field]
    if (!isFiniteNumber(v)) errors.push(`head_pose.${field}: must be a finite number`)
    else if (v < lo || v > hi) errors.push(`head_pose.${field}: ${v} outside [${lo}, ${hi}]`)
  }

  const m = pose.matrix
  const shapeOk =
    Array.isArray(m) && m.length === 4 &&
    m.every((row) => Array.isArray(row) && row.length === 4 && row.every(isFiniteNumber))
  if (!shapeOk) {
    errors.push('head_pose.matrix: must be 4 rows of 4 finite numbers')
    return
  }
  if (m[3][0] !== 0 || m[3][1] !== 0 || m[3][2] !== 0 || m[3][3] !== 1) {
    errors.push('head_pose.matrix: last row must be exactly [0, 0, 0, 1]')
  }

  const t = { x: m[0][3], y: m[1][3], z: m[2][3] }
  for (const [axis, [lo, hi]] of Object.entries(TRANSLATION_BOUNDS_CM)) {
    if (t[axis] < lo || t[axis] > hi) {
      errors.push(`head_pose.matrix: translation ${axis.toUpperCase()} ${t[axis]} cm outside [${lo}, ${hi}]`)
    }
  }

  const derived = anglesFromMatrix(m)
  if (derived === null) {
    errors.push('head_pose.matrix: scale must be finite and greater than 0')
    return
  }
  if (derived.scaleSpread > SCALE_TOLERANCE) {
    errors.push('head_pose.matrix: column norms differ by more than 1% (scale is not uniform)')
  }
  for (const field of Object.keys(ANGLE_RANGES_DEG)) {
    if (isFiniteNumber(pose[field]) && Math.abs(pose[field] - derived[field]) > ANGLE_TOLERANCE_DEG) {
      errors.push(`head_pose.${field}: does not match matrix (derived ${derived[field].toFixed(2)})`)
    }
  }
}

// Returns { valid, errors }. errors lists every problem found, for display.
export function validateCameraFrame(msg) {
  const errors = []
  if (!isPlainObject(msg)) return { valid: false, errors: ['message: must be a JSON object'] }

  checkExactKeys(msg, FRAME_FIELDS, 'message', errors)

  if (msg.schema_version !== SCHEMA_VERSION) {
    errors.push(`schema_version: expected "${SCHEMA_VERSION}", got ${JSON.stringify(msg.schema_version)}`)
  }
  if (!isTimestampMs(msg, 'timestamp_ms')) {
    errors.push('timestamp_ms: must be an integer in the safe range (Unix epoch ms)')
  }

  const state = msg.tracking_state
  if (!TRACKING_STATES.includes(state)) {
    errors.push(`tracking_state: must be one of ${TRACKING_STATES.join(', ')}`)
  }

  // confidence is a real per-frame score or null ("the result carried no score").
  // null is NOT "face lost": a tracking frame with null confidence is still actionable.
  // A missing key is still an error (reported by checkExactKeys above).
  if (Object.hasOwn(msg, 'confidence')) {
    const conf = msg.confidence
    if (state === 'lost') {
      if (conf !== null) errors.push('confidence: must be null when lost')
    } else if (conf !== null) {
      if (!isFiniteNumber(conf) || conf < 0 || conf > 1) {
        errors.push('confidence: must be null or a finite number in [0, 1]')
      } else if (state === 'tracking' && conf === 0) {
        errors.push('confidence: must be null or in (0, 1] when tracking')
      }
    }
  }

  const features = msg.features
  if (!isPlainObject(features)) {
    errors.push('features: must be an object')
  } else {
    for (const [name, value] of Object.entries(features)) {
      if (!BLENDSHAPE_SET.has(name)) errors.push(`features: unknown name "${name}"`)
      else if (!isFiniteNumber(value) || value < 0 || value > 1) errors.push(`features.${name}: must be a number in [0, 1]`)
    }
    if (state === 'tracking') {
      const missing = BLENDSHAPE_NAMES.filter((n) => !Object.hasOwn(features, n))
      if (missing.length > 0) errors.push(`features: tracking requires all 52 names, missing ${missing.length}`)
    }
    if (state === 'lost' && Object.keys(features).length > 0) {
      errors.push('features: must be {} when lost')
    }
  }

  if (Object.hasOwn(msg, 'head_pose')) {
    if (state === 'tracking') validateHeadPose(msg.head_pose, errors)
    else if (msg.head_pose !== null) errors.push('head_pose: must be null unless tracking')
  }

  return { valid: errors.length === 0, errors }
}

// Contract: actionable = tracking, all blendshapes, valid pose, confidence null or in (0, 1].
export function isActionable(msg) {
  return validateCameraFrame(msg).valid && msg.tracking_state === 'tracking'
}

// Raw JSON text (for example from a WebSocket) -> { valid, errors, message }. Never throws.
export function decodeCameraFrame(text) {
  const decoded = decodeJson(text)
  if (!decoded.ok) return { valid: false, errors: [decoded.error], message: null }
  return { ...validateCameraFrame(decoded.value), message: decoded.value }
}
