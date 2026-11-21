// Strict JSON decoding shared by every contract message (docs/contracts.md, schema rules):
// - a decoder returns a rejection as a result and never throws
// - duplicate JSON keys are rejected (JSON.parse would silently keep the last one)
// - integer fields reject numbers written as floats, e.g. 5.0 or 5e0, matching the
//   Python oracle in tests/contract_schema.py where such values decode as float
// NaN and Infinity are not valid JSON text, so JSON.parse already refuses them.

// Container object -> set of keys whose number was written with a "." or exponent.
const floatNotation = new WeakMap()

function recordFloatNotation(key, value, context) {
  if (typeof value === 'number' && typeof context?.source === 'string' && /[.eE]/.test(context.source)) {
    let keys = floatNotation.get(this)
    if (!keys) floatNotation.set(this, (keys = new Set()))
    keys.add(key)
  }
  return value
}

// Returns the first duplicate key found, or null. Only called on text JSON.parse accepted.
function findDuplicateKey(text) {
  const stack = [] // a Set of keys for each open object, null for each open array
  let i = 0
  while (i < text.length) {
    const ch = text[i]
    if (ch === '"') {
      let j = i + 1
      while (text[j] !== '"') j += text[j] === '\\' ? 2 : 1
      const token = text.slice(i, j + 1)
      i = j + 1
      while (' \t\n\r'.includes(text[i]) && i < text.length) i += 1
      const top = stack[stack.length - 1]
      if (text[i] === ':' && top instanceof Set) {
        const key = JSON.parse(token)
        if (top.has(key)) return key
        top.add(key)
      }
      continue
    }
    if (ch === '{') stack.push(new Set())
    else if (ch === '[') stack.push(null)
    else if (ch === '}' || ch === ']') stack.pop()
    i += 1
  }
  return null
}

// Returns { ok: true, value } or { ok: false, error }. Never throws.
export function decodeJson(text) {
  if (typeof text !== 'string') return { ok: false, error: 'message: not JSON text' }
  let value
  try {
    value = JSON.parse(text, recordFloatNotation)
  } catch {
    return { ok: false, error: 'message: not valid JSON' }
  }
  const duplicate = findDuplicateKey(text)
  if (duplicate !== null) return { ok: false, error: `message: duplicate JSON key "${duplicate}"` }
  return { ok: true, value }
}

// True when obj[key] is a JSON integer: an integer value not written as 5.0 or 5e0.
// Objects built in JavaScript (mocks, tests) have no float notation recorded.
export function isIntegerField(obj, key) {
  return Number.isInteger(obj[key]) && !floatNotation.get(obj)?.has(key)
}
