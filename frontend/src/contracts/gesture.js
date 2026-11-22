// Gesture classification, docs/contracts.md section 2, schema "4".
// Every field name, range and rule for this message lives in this file.
// The checks follow the Python reference validator, validate_gesture in tests/contract_schema.py.

import { decodeJson, isIntegerField } from './json.js'
import { SCHEMA_VERSION, checkExactKeys, isFiniteNumber, isPlainObject, isTimestampMs } from './cameraFrame.js'

export const GESTURE_FIELDS = [
  'schema_version',
  'timestamp_ms',
  'source_timestamp_ms',
  'window_start_timestamp_ms',
  'window_end_timestamp_ms',
  'window_frame_count',
  'label',
  'confidence',
  'model_version',
]

const TIMESTAMP_FIELDS = ['timestamp_ms', 'source_timestamp_ms', 'window_start_timestamp_ms', 'window_end_timestamp_ms']

// Reserved in every model version.
export const RESERVED_LABELS = ['neutral', 'unknown']

// What each reserved label means for motion (contract "Label" table).
export const RESERVED_MEANING = {
  neutral: 'Does not request motion.',
  unknown: 'Does not request motion. Shared control must stop.',
}

// Reviewed label lists, keyed by exact model_version. Schema 4 has none, because no model is
// in the repository. A reviewed list is added here, with its review, when a model ships.
export const REVIEWED_LABELS = {}

export const MODEL_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/

export function isKnownLabel(label, modelVersion) {
  return RESERVED_LABELS.includes(label) || (REVIEWED_LABELS[modelVersion] ?? []).includes(label)
}

// Returns { valid, errors }. Never throws.
export function validateGesture(msg) {
  const errors = []
  if (!isPlainObject(msg)) return { valid: false, errors: ['message: must be a JSON object'] }

  checkExactKeys(msg, GESTURE_FIELDS, 'message', errors)
  if (errors.length > 0) return { valid: false, errors }

  if (msg.schema_version !== SCHEMA_VERSION) {
    errors.push(`schema_version: expected "${SCHEMA_VERSION}", got ${JSON.stringify(msg.schema_version)}`)
  }

  for (const field of TIMESTAMP_FIELDS) {
    if (!isTimestampMs(msg, field)) errors.push(`${field}: must be an integer Unix epoch milliseconds timestamp`)
  }

  if (!isIntegerField(msg, 'window_frame_count') || msg.window_frame_count < 1) {
    errors.push('window_frame_count: must be an integer >= 1')
  }

  if (TIMESTAMP_FIELDS.every((field) => isTimestampMs(msg, field))) {
    if (msg.window_start_timestamp_ms > msg.window_end_timestamp_ms) {
      errors.push('window_start_timestamp_ms: window starts after it ends')
    }
    if (msg.source_timestamp_ms !== msg.window_end_timestamp_ms) {
      errors.push('source_timestamp_ms: must equal window_end_timestamp_ms')
    }
    if (msg.timestamp_ms < msg.window_end_timestamp_ms) {
      errors.push('timestamp_ms: classification time is before the window end')
    }
  }

  const modelVersionValid = typeof msg.model_version === 'string' && MODEL_VERSION_PATTERN.test(msg.model_version)
  if (!modelVersionValid) errors.push('model_version: must match ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')

  if (typeof msg.label !== 'string' || msg.label === '') {
    errors.push('label: must be a non-empty string')
  } else if (!isKnownLabel(msg.label, msg.model_version)) {
    errors.push(`label: "${msg.label}" has no reviewed list entry for this model_version`)
  }

  if (!isFiniteNumber(msg.confidence) || msg.confidence < 0 || msg.confidence > 1) {
    errors.push('confidence: must be a number in [0, 1]')
  }

  return { valid: errors.length === 0, errors }
}

// Raw JSON text (for example from a WebSocket) -> { valid, errors, message }. Never throws.
export function decodeGesture(text) {
  const decoded = decodeJson(text)
  if (!decoded.ok) return { valid: false, errors: [decoded.error], message: null }
  return { ...validateGesture(decoded.value), message: decoded.value }
}
