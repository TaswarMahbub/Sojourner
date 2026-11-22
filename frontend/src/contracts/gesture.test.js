import { describe, expect, it } from 'vitest'
import { decodeGesture, validateGesture } from './gesture.js'
import { buildMockGesture } from '../sources/mockGestureSource.js'
import { checkGestureMessage } from '../hooks/useGestureStream.js'

// The contract's own encoding illustration (docs/contracts.md section 2).
const ILLUSTRATION = {
  schema_version: '4',
  timestamp_ms: 1795276800040,
  source_timestamp_ms: 1795276800000,
  window_start_timestamp_ms: 1795276800000,
  window_end_timestamp_ms: 1795276800000,
  window_frame_count: 1,
  label: 'unknown',
  confidence: 0,
  model_version: 'none',
}

const errorsFor = (overrides) => validateGesture({ ...ILLUSTRATION, ...overrides }).errors

describe('gesture classification, schema 4', () => {
  it('accepts the contract illustration and a neutral result', () => {
    expect(validateGesture(ILLUSTRATION)).toEqual({ valid: true, errors: [] })
    expect(errorsFor({ label: 'neutral', confidence: 0.8 })).toEqual([])
  })

  it('rejects unknown and missing fields', () => {
    expect(errorsFor({ extra: 1 })).toContain('message: unknown field "extra"')
    const missing = { ...ILLUSTRATION }
    delete missing.label
    expect(validateGesture(missing).errors).toContain('message: missing field "label"')
    expect(validateGesture(null).valid).toBe(false)
    expect(validateGesture([]).valid).toBe(false)
  })

  it('requires schema "4" as a string', () => {
    expect(errorsFor({ schema_version: 4 })).toHaveLength(1)
    expect(errorsFor({ schema_version: '3' })).toHaveLength(1)
  })

  it('rejects labels outside neutral and unknown, because no reviewed list exists', () => {
    expect(errorsFor({ label: 'nod' })).toEqual(['label: "nod" has no reviewed list entry for this model_version'])
    expect(errorsFor({ label: '' })).toEqual(['label: must be a non-empty string'])
    expect(errorsFor({ label: 1 })).toEqual(['label: must be a non-empty string'])
  })

  it('checks the model_version pattern', () => {
    expect(errorsFor({ model_version: 'mock-0' })).toEqual([])
    expect(errorsFor({ model_version: 'v1.2+cpu_3' })).toEqual([])
    expect(errorsFor({ model_version: '' })).toHaveLength(1)
    expect(errorsFor({ model_version: '-x' })).toHaveLength(1)
    expect(errorsFor({ model_version: 'a b' })).toHaveLength(1)
    expect(errorsFor({ model_version: 'a'.repeat(65) })).toHaveLength(1)
    expect(errorsFor({ model_version: 'a'.repeat(64) })).toEqual([])
  })

  it('requires confidence in [0, 1], and booleans are not numbers', () => {
    expect(errorsFor({ confidence: 1 })).toEqual([])
    expect(errorsFor({ confidence: -0.01 })).toHaveLength(1)
    expect(errorsFor({ confidence: 1.01 })).toHaveLength(1)
    expect(errorsFor({ confidence: true })).toHaveLength(1)
    expect(errorsFor({ confidence: null })).toHaveLength(1)
  })

  it('requires window_frame_count to be an integer >= 1', () => {
    expect(errorsFor({ window_frame_count: 0 })).toHaveLength(1)
    expect(errorsFor({ window_frame_count: 1.5 })).toHaveLength(1)
    expect(errorsFor({ window_frame_count: true })).toHaveLength(1)
  })

  it('checks the window ordering rules', () => {
    // Start after end.
    expect(errorsFor({ window_start_timestamp_ms: 1795276800001 })).toEqual(['window_start_timestamp_ms: window starts after it ends'])
    // Source is not the window end.
    expect(errorsFor({ source_timestamp_ms: 1795276799999 })).toEqual(['source_timestamp_ms: must equal window_end_timestamp_ms'])
    // Classified before the window ended.
    expect(errorsFor({ timestamp_ms: 1795276799999 })).toEqual(['timestamp_ms: classification time is before the window end'])
  })

  it('rejects timestamps that are not safe non-negative integers', () => {
    expect(errorsFor({ timestamp_ms: -1 })).toContain('timestamp_ms: must be an integer Unix epoch milliseconds timestamp')
    expect(errorsFor({ timestamp_ms: 9007199254740992 })).toContain('timestamp_ms: must be an integer Unix epoch milliseconds timestamp')
    expect(errorsFor({ timestamp_ms: '1795276800040' })).toContain('timestamp_ms: must be an integer Unix epoch milliseconds timestamp')
  })

  it('decodes text strictly: duplicate keys and 5.0 integers are rejected', () => {
    const text = JSON.stringify(ILLUSTRATION)
    expect(decodeGesture(text).valid).toBe(true)
    expect(decodeGesture(text.replace('"window_frame_count":1', '"window_frame_count":1.0')).valid).toBe(false)
    expect(decodeGesture(text.replace('}', ',"label":"neutral"}')).errors[0]).toMatch(/duplicate JSON key "label"/)
    expect(checkGestureMessage(text, 0).validation.valid).toBe(true)
    expect(checkGestureMessage('not json', 0)).toMatchObject({ message: null, validation: { valid: false } })
  })
})

describe('mock gesture source', () => {
  const T = 1795276800000

  it('sends valid neutral and unknown messages, an invalid "nod", then nothing', () => {
    const neutral = buildMockGesture(1000, T)
    const unknown = buildMockGesture(7000, T)
    const nod = buildMockGesture(8500, T)
    expect(neutral.label).toBe('neutral')
    expect(validateGesture(neutral).valid).toBe(true)
    expect(unknown.label).toBe('unknown')
    expect(validateGesture(unknown).valid).toBe(true)
    expect(nod.label).toBe('nod')
    expect(validateGesture(nod).valid).toBe(false)
    expect(buildMockGesture(9500, T)).toBeNull()
  })

  it('every message it sends in the valid part of the cycle passes validation', () => {
    for (let t = 0; t < 8000; t += 100) {
      expect(validateGesture(buildMockGesture(t, T + t)).errors).toEqual([])
    }
  })
})
