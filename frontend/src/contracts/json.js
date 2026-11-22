// Strict JSON decoding shared by every contract message (docs/contracts.md, schema rules):
// - a decoder returns a rejection as a result and never throws
// - duplicate JSON keys are rejected (JSON.parse would silently keep the last one)
// - integer fields reject numbers written as floats, e.g. 5.0 or 5e0, matching the
//   Python oracle in tests/contract_schema.py where such values decode as float
// NaN and Infinity are not valid JSON text, so JSON.parse already refuses them.

// Container object or array -> set of keys whose number was written with a "." or exponent.
const floatNotation = new WeakMap()

// Scans text that JSON.parse already accepted, in one pass. Returns { duplicate, floats }:
// duplicate is the first key repeated within one object, or null;
// floats lists [containerPath, key] for every number written with a "." or an exponent.
// This reads the raw text itself, so it does not depend on the reviver's context.source,
// which Node 20 does not provide.
function scan(text) {
  const stack = [] // per open container: { path, keys: Set, key } for objects, { path, keys: null, index } for arrays
  const floats = []
  const top = () => stack[stack.length - 1]
  const here = () => (top().keys ? top().key : top().index)
  let i = 0
  while (i < text.length) {
    const ch = text[i]
    if (ch === '"') {
      let j = i + 1
      while (text[j] !== '"') j += text[j] === '\\' ? 2 : 1
      const token = text.slice(i, j + 1)
      i = j + 1
      while (i < text.length && ' \t\n\r'.includes(text[i])) i += 1
      const frame = top()
      if (text[i] === ':' && frame?.keys) {
        const key = JSON.parse(token)
        if (frame.keys.has(key)) return { duplicate: key, floats }
        frame.keys.add(key)
        frame.key = key
      }
      continue
    }
    if (ch === '-' || (ch >= '0' && ch <= '9')) {
      let j = i
      while (j < text.length && /[-+.eE0-9]/.test(text[j])) j += 1
      if (stack.length && /[.eE]/.test(text.slice(i, j))) floats.push([top().path, here()])
      i = j
      continue
    }
    if (ch === '{' || ch === '[') {
      const path = stack.length ? [...top().path, here()] : []
      stack.push(ch === '{' ? { path, keys: new Set(), key: null } : { path, keys: null, index: 0 })
    } else if (ch === '}' || ch === ']') {
      stack.pop()
    } else if (ch === ',' && stack.length && !top().keys) {
      top().index += 1
    }
    i += 1
  }
  return { duplicate: null, floats }
}

// Returns { ok: true, value } or { ok: false, error }. Never throws.
export function decodeJson(text) {
  if (typeof text !== 'string') return { ok: false, error: 'message: not JSON text' }
  let value
  try {
    value = JSON.parse(text)
  } catch {
    return { ok: false, error: 'message: not valid JSON' }
  }
  const { duplicate, floats } = scan(text)
  if (duplicate !== null) return { ok: false, error: `message: duplicate JSON key "${duplicate}"` }
  for (const [path, key] of floats) {
    const holder = path.reduce((node, step) => node[step], value)
    let keys = floatNotation.get(holder)
    if (!keys) floatNotation.set(holder, (keys = new Set()))
    keys.add(key)
  }
  return { ok: true, value }
}

// True when obj[key] is a JSON integer: an integer value not written as 5.0 or 5e0.
// Objects built in JavaScript (mocks, tests) have no float notation recorded.
export function isIntegerField(obj, key) {
  return Number.isInteger(obj[key]) && !floatNotation.get(obj)?.has(key)
}
