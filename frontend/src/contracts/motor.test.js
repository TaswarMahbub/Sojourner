import { Buffer } from 'node:buffer'
import { createHash, createHmac, randomBytes } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { canonicalVelocity, decodeDatagram, peekIdentity, validateMotorCommand } from './motor.js'
import { decodeJson } from './json.js'
import { hmacSha256Hex, sha256 } from '../sim/hmac.js'

const vel = (text) => canonicalVelocity(decodeJson(`{"v": ${text}}`).value, 'v')

describe('canonical velocities (contract "Command authentication")', () => {
  it('accepts values already equal to their six-digit form', () => {
    expect(vel('0.1')).toBe(0.1)
    expect(vel('-0.3')).toBe(-0.3)
    expect(vel('0')).toBe(0)
    expect(vel('0.000001')).toBe(0.000001)
  })

  it('rejects values that would round, negative zero, and non-numbers', () => {
    expect(vel('0.1234567')).toBeNull()
    expect(vel('1e-7')).toBeNull()
    expect(vel('-0.0')).toBeNull()
    expect(vel('true')).toBeNull()
    expect(vel('"0.1"')).toBeNull()
  })

  it('treats a JSON integer written -0 as the integer 0, as Python does', () => {
    expect(vel('-0')).toBe(0)
  })
})

describe('datagram decoding (contract "Parsing")', () => {
  it('rejects oversize, deeply nested, duplicate-key and non-finite datagrams without throwing', () => {
    expect(decodeDatagram(`{"a": "${'x'.repeat(8200)}"}`)).toEqual({ error: 'malformed' })
    expect(decodeDatagram('['.repeat(33) + ']'.repeat(33))).toEqual({ error: 'malformed' })
    expect(decodeDatagram('['.repeat(32) + ']'.repeat(32)).error).toBeUndefined()
    expect(decodeDatagram('{"a": 1, "a": 2}')).toEqual({ error: 'malformed' })
    expect(decodeDatagram('{"a": 1e400}')).toEqual({ error: 'malformed' })
    expect(decodeDatagram('{"a": NaN}')).toEqual({ error: 'malformed' })
    expect(decodeDatagram(undefined)).toEqual({ error: 'malformed' })
  })

  it('reads the top-level identity of a truncated datagram without parsing it', () => {
    expect(peekIdentity('{"boot_nonce": 7, "session_id": 12, "sequence": ')).toEqual({ boot_nonce: 7, session_id: 12 })
    expect(peekIdentity('{"x": {"boot_nonce": 7, "session_id": 12}}')).toBeNull()
    expect(peekIdentity('{"boot_nonce": 7.0, "session_id": 12}')).toBeNull()
  })
})

describe('motor command validation', () => {
  const start = {
    schema_version: '4', controller_id: 'laptop', boot_nonce: 1, session_id: 1795276800100, sequence: 1,
    freshness_id: 1, rearm_generation: 1, status_challenge: 1, issued_at_ms: 1795276800100,
    expires_at_ms: 1795276800350, task_state: 'idle', linear_mps: 0, angular_rad_s: 0, stop: true, mac: '0'.repeat(64),
  }

  it('accepts the contract illustration shape and enforces the task table', () => {
    expect(validateMotorCommand(start)).toEqual([])
    expect(validateMotorCommand({ ...start, stop: false })).toContain('stop task must have stop true and zero motion')
    expect(validateMotorCommand({ ...start, task_state: 'manual', stop: false, linear_mps: 0.31 })).toContain('linear_mps is outside [-0.3, 0.3]')
    expect(validateMotorCommand({ ...start, expires_at_ms: start.issued_at_ms + 251 })).toContain('command lifetime is outside 1..250 ms')
  })
})

describe('simulator SHA-256 and HMAC', () => {
  it('match Node crypto on random inputs', () => {
    for (let n = 0; n < 300; n += 1) {
      const message = randomBytes(n < 130 ? n : 1000 + n)
      const key = randomBytes(n % 150)
      expect(Buffer.from(sha256(message)).toString('hex')).toBe(createHash('sha256').update(message).digest('hex'))
      expect(hmacSha256Hex(key, message)).toBe(createHmac('sha256', key).update(message).digest('hex'))
    }
  })
})
