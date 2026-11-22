// Motor command and motor status, docs/contracts.md sections 3 and 4, schema "4".
// Every field name, range and rule for these messages lives in this file. Each function is a
// port of the Python reference in tests/contract_schema.py and tests/control_auth.py, and the
// tests check that both give the same results.

import { decodeJson, isIntegerField } from './json.js'
import { SCHEMA_VERSION, isPlainObject } from './cameraFrame.js'
import { equalHex, hmacSha256Hex, utf8 } from '../sim/hmac.js'

export const SAFE_INT_MAX = 9007199254740991
export const SEQUENCE_MAX = 4294967295
export const MAX_DATAGRAM_BYTES = 8192
export const MAX_JSON_DEPTH = 32
export const LINEAR_RANGE_MPS = [-0.3, 0.3]
export const ANGULAR_RANGE_RAD_S = [-1, 1]

export const STOP_TASKS = ['idle', 'hold', 'estop', 'fault', 'rearm']
export const MOVE_TASKS = ['manual', 'shared']
export const SAFETY_STOP_TASKS = ['idle', 'hold', 'estop', 'fault']
export const TASKS = [...STOP_TASKS, ...MOVE_TASKS]

const CONTROLLER_ID = /^[A-Za-z0-9_-]{1,32}$/
const MAC_HEX = /^[0-9a-f]{64}$/

export const MOTOR_FIELDS = [
  'schema_version', 'controller_id', 'boot_nonce', 'session_id', 'sequence', 'freshness_id',
  'rearm_generation', 'status_challenge', 'issued_at_ms', 'expires_at_ms', 'task_state',
  'linear_mps', 'angular_rad_s', 'stop', 'mac',
]

export const STATUS_FIELDS = [
  'schema_version', 'controller_id', 'timestamp_ms', 'boot_nonce', 'session_id', 'last_sequence',
  'status_sequence', 'freshness_id', 'rearm_generation', 'status_challenge', 'clock_state',
  'sensor_state', 'cutoff_open', 'physical_estop', 'software_estop_latch', 'fault_latch', 'armed',
  'commanding_stop', 'mac',
]

// ---- Types. "Integer" means a JSON integer: 5.0 and 5e0 decoded from text are floats. ----

export const isBool = (value) => typeof value === 'boolean'
export const isInt = (obj, key) => isIntegerField(obj, key)
const isNumber = (value) => typeof value === 'number'
const isTimestamp = (obj, key) => isInt(obj, key) && obj[key] >= 0 && obj[key] <= SAFE_INT_MAX
const isSafeId = (obj, key) => isInt(obj, key) && obj[key] >= 1 && obj[key] <= SAFE_INT_MAX

// The velocity that would be applied, or null when it is not canonical (control_auth.canonical_velocity).
// It must already equal its six-digit form. Negative zero is rejected. A JSON integer written
// "-0" is the integer 0 (Python has no negative integer zero), so it is not negative zero.
export function canonicalVelocity(obj, key) {
  let value = obj[key]
  if (!isNumber(value) || !Number.isFinite(value)) return null
  if (Object.is(value, -0) && isIntegerField(obj, key)) value = 0
  if (Object.is(value, -0)) return null
  const applied = Number(value.toFixed(6))
  if (!Number.isFinite(applied) || Object.is(applied, -0) || applied !== value) return null
  return applied
}

const isZeroVelocity = (obj, key) => canonicalVelocity(obj, key) === 0

function checkExactKeys(msg, allowed, errors) {
  for (const key of Object.keys(msg).sort()) if (!allowed.includes(key)) errors.push(`unknown field ${key}`)
  for (const key of [...allowed].sort()) if (!Object.hasOwn(msg, key)) errors.push(`missing field ${key}`)
}

function checkVelocity(msg, key, [low, high], errors) {
  const applied = canonicalVelocity(msg, key)
  if (applied === null) errors.push(`${key} is not a canonical velocity`)
  else if (applied < low || applied > high) errors.push(`${key} is outside [${low}, ${high}]`)
}

// ---- Validation. Each returns a list of errors; an empty list means valid. Never throws. ----

export function validateMotorCommand(msg) {
  try {
    const errors = []
    if (!isPlainObject(msg)) return ['message is not an object']
    checkExactKeys(msg, MOTOR_FIELDS, errors)
    if (errors.length) return errors
    if (msg.schema_version !== SCHEMA_VERSION) errors.push('schema_version is not 4')
    if (typeof msg.controller_id !== 'string' || !CONTROLLER_ID.test(msg.controller_id)) errors.push('controller_id is invalid')
    if (!isSafeId(msg, 'boot_nonce')) errors.push('boot_nonce is invalid')
    if (!isSafeId(msg, 'session_id')) errors.push('session_id is invalid')
    if (!isSafeId(msg, 'freshness_id')) errors.push('freshness_id is invalid')
    if (!isInt(msg, 'rearm_generation') || msg.rearm_generation < 0 || msg.rearm_generation > SAFE_INT_MAX) errors.push('rearm_generation is invalid')
    if (!isSafeId(msg, 'status_challenge')) errors.push('status_challenge is invalid')
    if (typeof msg.mac !== 'string' || !MAC_HEX.test(msg.mac)) errors.push('mac is invalid')
    if (!isInt(msg, 'sequence') || msg.sequence < 1 || msg.sequence > SEQUENCE_MAX) errors.push('sequence is invalid')
    if (!isTimestamp(msg, 'issued_at_ms') || !isTimestamp(msg, 'expires_at_ms')) {
      errors.push('command timestamps are invalid')
    } else {
      const lifetime = msg.expires_at_ms - msg.issued_at_ms
      if (lifetime < 1 || lifetime > 250) errors.push('command lifetime is outside 1..250 ms')
    }
    const task = msg.task_state
    if (!TASKS.includes(task)) errors.push('task_state is invalid')
    if (!isBool(msg.stop)) errors.push('stop is not a boolean')
    checkVelocity(msg, 'linear_mps', LINEAR_RANGE_MPS, errors)
    checkVelocity(msg, 'angular_rad_s', ANGULAR_RANGE_RAD_S, errors)
    const bothZero = isZeroVelocity(msg, 'linear_mps') && isZeroVelocity(msg, 'angular_rad_s')
    if (STOP_TASKS.includes(task)) {
      if (msg.stop !== true || !bothZero) errors.push('stop task must have stop true and zero motion')
    } else if (MOVE_TASKS.includes(task) && msg.stop === true && !bothZero) {
      errors.push('stop true requires zero motion')
    } else if (MOVE_TASKS.includes(task) && msg.stop === false && (!isNumber(msg.linear_mps) || !isNumber(msg.angular_rad_s))) {
      errors.push('motion values are invalid')
    }
    return errors
  } catch {
    return ['rejected']
  }
}

export function validateMotorStatus(msg) {
  try {
    const errors = []
    if (!isPlainObject(msg)) return ['message is not an object']
    checkExactKeys(msg, STATUS_FIELDS, errors)
    if (errors.length) return errors
    if (msg.schema_version !== SCHEMA_VERSION) errors.push('schema_version is not 4')
    if (typeof msg.controller_id !== 'string' || !CONTROLLER_ID.test(msg.controller_id)) errors.push('controller_id is invalid')
    if (!isTimestamp(msg, 'timestamp_ms')) errors.push('timestamp_ms is not a timestamp')
    if (!isSafeId(msg, 'boot_nonce')) errors.push('boot_nonce is invalid')
    if (!isSafeId(msg, 'freshness_id')) errors.push('freshness_id is invalid')
    if (!isInt(msg, 'rearm_generation') || msg.rearm_generation < 0 || msg.rearm_generation > SAFE_INT_MAX) errors.push('rearm_generation is invalid')
    if (!isInt(msg, 'status_challenge') || msg.status_challenge < 0 || msg.status_challenge > SAFE_INT_MAX) errors.push('status_challenge is invalid')
    if (!isInt(msg, 'status_sequence') || msg.status_sequence < 1 || msg.status_sequence > SAFE_INT_MAX) errors.push('status_sequence is invalid')
    if (!['unsynchronized', 'synchronized'].includes(msg.clock_state)) errors.push('clock_state is invalid')
    if (!['asserted', 'released', 'unknown'].includes(msg.sensor_state)) errors.push('sensor_state is invalid')
    if (!isBool(msg.cutoff_open)) errors.push('cutoff_open is not a boolean')
    if (typeof msg.mac !== 'string' || !MAC_HEX.test(msg.mac)) errors.push('mac is invalid')
    const session = msg.session_id
    const sequence = msg.last_sequence
    if (session !== null && !isSafeId(msg, 'session_id')) errors.push('session_id is invalid')
    if (sequence !== null && (!isInt(msg, 'last_sequence') || sequence < 1 || sequence > SEQUENCE_MAX)) errors.push('last_sequence is invalid')
    if (session === null && sequence !== null) errors.push('last_sequence requires a session')
    if (session !== null && sequence === null) errors.push('session requires last_sequence')
    for (const name of ['physical_estop', 'software_estop_latch', 'fault_latch', 'armed', 'commanding_stop']) {
      if (!isBool(msg[name])) errors.push(`${name} is not a boolean`)
    }
    if (msg.sensor_state === 'released' && msg.physical_estop !== false) errors.push('released sensor contradicts physical_estop')
    if (msg.sensor_state !== 'released' && msg.physical_estop !== true) errors.push('unknown or asserted sensor requires physical_estop')
    if (msg.armed === true && (msg.physical_estop !== false || msg.software_estop_latch !== false || msg.fault_latch !== false || msg.cutoff_open !== false || msg.sensor_state !== 'released')) {
      errors.push('armed status contradicts a stop condition')
    }
    if (session === null && msg.armed === true) errors.push('armed requires an active session')
    return errors
  } catch {
    return ['rejected']
  }
}

// ---- MAC (contract "Command authentication"). ----

const COMMAND_PREFIX = 'sojourner-command-mac-v1\n'
const STATUS_PREFIX = 'sojourner-status-mac-v1\n'

const COMMAND_MAC_FIELDS = [
  ['schema_version', 'str'], ['controller_id', 'str'], ['boot_nonce', 'int'], ['session_id', 'int'],
  ['sequence', 'int'], ['freshness_id', 'int'], ['rearm_generation', 'int'], ['status_challenge', 'int'],
  ['issued_at_ms', 'int'], ['expires_at_ms', 'int'], ['task_state', 'str'], ['linear_mps', 'vel'],
  ['angular_rad_s', 'vel'], ['stop', 'bool'],
]

const STATUS_MAC_FIELDS = [
  ['schema_version', 'str'], ['controller_id', 'str'], ['timestamp_ms', 'int'], ['boot_nonce', 'int'],
  ['session_id', 'int_or_null'], ['last_sequence', 'int_or_null'], ['status_sequence', 'int'],
  ['freshness_id', 'int'], ['rearm_generation', 'int'], ['status_challenge', 'int'], ['clock_state', 'str'],
  ['sensor_state', 'str'], ['cutoff_open', 'bool'], ['physical_estop', 'bool'], ['software_estop_latch', 'bool'],
  ['fault_latch', 'bool'], ['armed', 'bool'], ['commanding_stop', 'bool'],
]

// Throws when a field cannot be encoded, as the Python _encode raises.
function encodeField(msg, name, kind) {
  if (!Object.hasOwn(msg, name)) throw new Error(`missing ${name}`)
  const value = msg[name]
  if (kind === 'int_or_null' && value === null) return 'null'
  if (kind === 'int' || kind === 'int_or_null') {
    if (!isInt(msg, name)) throw new Error('not an int')
    return String(value)
  }
  if (kind === 'bool') {
    if (!isBool(value)) throw new Error('not a bool')
    return value ? 'true' : 'false'
  }
  if (kind === 'str') {
    if (typeof value !== 'string') throw new Error('not a string')
    return value
  }
  const applied = canonicalVelocity(msg, name)
  if (applied === null) throw new Error('noncanonical velocity')
  return applied.toFixed(6)
}

function macBody(prefix, fields, msg) {
  return utf8(prefix + fields.map(([name, kind]) => `${name}=${encodeField(msg, name, kind)}\n`).join(''))
}

export const signCommand = (key, msg) => hmacSha256Hex(key, macBody(COMMAND_PREFIX, COMMAND_MAC_FIELDS, msg))
export const signStatus = (key, msg) => hmacSha256Hex(key, macBody(STATUS_PREFIX, STATUS_MAC_FIELDS, msg))

function macMatches(key, msg, prefix, fields) {
  if (!isPlainObject(msg) || !(key instanceof Uint8Array)) return false
  if (typeof msg.mac !== 'string' || !MAC_HEX.test(msg.mac)) return false
  try {
    return equalHex(msg.mac, hmacSha256Hex(key, macBody(prefix, fields, msg)))
  } catch {
    return false
  }
}

export const verifyCommand = (key, msg) => macMatches(key, msg, COMMAND_PREFIX, COMMAND_MAC_FIELDS)
export const verifyStatus = (key, msg) => macMatches(key, msg, STATUS_PREFIX, STATUS_MAC_FIELDS)

// ---- Datagram decoding (contract "Parsing"). ----

function scanExceedsDepth(raw) {
  let depth = 0
  let inString = false
  let escape = false
  for (const ch of raw.slice(0, MAX_DATAGRAM_BYTES)) {
    if (inString) {
      if (escape) escape = false
      else if (ch === '\\') escape = true
      else if (ch === '"') inString = false
      continue
    }
    if (ch === '"') inString = true
    else if (ch === '{' || ch === '[') {
      depth += 1
      if (depth > MAX_JSON_DEPTH) return true
    } else if (ch === '}' || ch === ']') depth = Math.max(0, depth - 1)
  }
  return false
}

function hasNonFinite(value) {
  const stack = [value]
  let seen = 0
  while (stack.length) {
    const node = stack.pop()
    seen += 1
    if (seen > 10000) return true
    if (typeof node === 'number' && !Number.isFinite(node)) return true
    if (Array.isArray(node)) stack.push(...node)
    else if (isPlainObject(node)) stack.push(...Object.values(node))
  }
  return false
}

// Raw datagram text -> { value } or { error: 'malformed' }. Never throws (control_auth.decode_strict).
// Rejects datagrams over 8192 bytes, nesting deeper than 32, duplicate keys, and non-finite numbers.
export function decodeDatagram(raw) {
  try {
    if (typeof raw !== 'string') return { error: 'malformed' }
    // Python cannot encode a lone surrogate as UTF-8, so such text is malformed there too.
    if (typeof raw.isWellFormed === 'function' && !raw.isWellFormed()) return { error: 'malformed' }
    if (utf8(raw).length > MAX_DATAGRAM_BYTES) return { error: 'malformed' }
    if (scanExceedsDepth(raw)) return { error: 'malformed' }
    const decoded = decodeJson(raw)
    if (!decoded.ok) return { error: 'malformed' }
    if (hasNonFinite(decoded.value)) return { error: 'malformed' }
    return { value: decoded.value }
  } catch {
    return { error: 'malformed' }
  }
}

// Reads depth-1 boot_nonce and session_id without a recursive parse. Not authentication.
// Returns { boot_nonce, session_id } or null. Never throws (control_auth.peek_object).
export function peekIdentity(raw) {
  try {
    if (typeof raw !== 'string') return null
    const found = {}
    const limit = Math.min(raw.length, MAX_DATAGRAM_BYTES)
    const isDigit = (c) => c >= '0' && c <= '9'
    let depth = 0
    let pending = null
    let i = 0
    while (i < limit) {
      const ch = raw[i]
      if (ch === '"') {
        let j = i + 1
        let text = ''
        let escaped = false
        while (j < limit) {
          const mark = raw[j]
          if (escaped) {
            text += mark
            escaped = false
          } else if (mark === '\\') escaped = true
          else if (mark === '"') break
          else text += mark
          j += 1
        }
        i = j < limit ? j + 1 : limit
        let k = i
        while (k < limit && ' \t\r\n'.includes(raw[k])) k += 1
        if (depth === 1 && k < limit && raw[k] === ':') {
          pending = text
          i = k + 1
        }
        continue
      }
      if (ch === '{' || ch === '[') {
        depth += 1
        if (depth > MAX_JSON_DEPTH) break
        i += 1
        continue
      }
      if (ch === '}' || ch === ']') {
        depth = Math.max(0, depth - 1)
        pending = null
        i += 1
        continue
      }
      if (pending !== null && depth === 1 && (ch === '-' || isDigit(ch))) {
        let j = ch === '-' ? i + 1 : i
        const digits = j
        while (j < limit && isDigit(raw[j])) j += 1
        const width = j - digits
        if (width >= 1 && width <= 16 && (j >= limit || !'.eE'.includes(raw[j]))) {
          if (pending === 'boot_nonce' || pending === 'session_id') found[pending] = Number(raw.slice(i, j)) || 0
        }
        pending = null
        i = j
        continue
      }
      if (!' \t\r\n:,'.includes(ch)) pending = null
      i += 1
    }
    return 'boot_nonce' in found && 'session_id' in found ? { boot_nonce: found.boot_nonce, session_id: found.session_id } : null
  } catch {
    return null
  }
}
