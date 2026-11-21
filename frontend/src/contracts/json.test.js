import { describe, expect, it } from 'vitest'
import { decodeJson, isIntegerField } from './json.js'

describe('strict JSON decoding (contract decoding rules)', () => {
  it('decodes valid JSON', () => {
    expect(decodeJson('{"a": 1, "b": [1, {"c": "x"}]}')).toEqual({ ok: true, value: { a: 1, b: [1, { c: 'x' }] } })
  })

  it('returns a rejection instead of throwing on invalid text', () => {
    expect(decodeJson('{"a": ')).toEqual({ ok: false, error: 'message: not valid JSON' })
    expect(decodeJson('{"a": NaN}').ok).toBe(false)
    expect(decodeJson('{"a": Infinity}').ok).toBe(false)
    expect(decodeJson(undefined).ok).toBe(false)
  })

  it('rejects duplicate keys at any depth, including escaped spellings', () => {
    expect(decodeJson('{"a": 1, "a": 2}').error).toMatch(/duplicate JSON key "a"/)
    expect(decodeJson('{"o": {"k": 1, "k": 2}}').ok).toBe(false)
    expect(decodeJson('{"a": 1, "\\u0061": 2}').ok).toBe(false)
  })

  it('allows the same key in different objects and keys that look like values', () => {
    expect(decodeJson('[{"a": 1}, {"a": 2}]').ok).toBe(true)
    expect(decodeJson('{"a": "a", "b": "a:"}').ok).toBe(true)
    expect(decodeJson('{"a": {"a": 1}}').ok).toBe(true)
  })

  it('treats 5.0 and 5e0 as floats, not integers, like the Python oracle', () => {
    const { value } = decodeJson('{"i": 5, "f": 5.0, "e": 5e0, "x": 5.5}')
    expect(isIntegerField(value, 'i')).toBe(true)
    expect(isIntegerField(value, 'f')).toBe(false)
    expect(isIntegerField(value, 'e')).toBe(false)
    expect(isIntegerField(value, 'x')).toBe(false)
    expect(isIntegerField({ i: 5 }, 'i')).toBe(true)
  })
})
