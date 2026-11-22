import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { replayRecording, validateRecording } from './recording.js'

const load = (name) => JSON.parse(readFileSync(new URL(`../fixtures/commands/${name}.json`, import.meta.url), 'utf8'))
const NAMES = ['clean_traversal', 'watchdog_gap', 'estop_latch']
const taskOf = (d) => JSON.parse(d.raw).task_state

describe('recorded command files', () => {
  it.each(NAMES)('%s parses, is valid, and is labelled synthetic', (name) => {
    const r = load(name)
    expect(validateRecording(r)).toEqual([])
    expect(r.scenario).toBe(name)
    expect(r.provenance).toMatchObject({ source: 'synthetic', derived_from: 'contracts.md §3' })
  })

  it('clean_traversal: every datagram is accepted and the attempt ends on room entry carrying the provenance', () => {
    const r = load('clean_traversal')
    const emitted = []
    const { outcomes } = replayRecording(r, { onAttemptEmit: (m) => emitted.push(m) })
    expect(outcomes.every((o) => o.startsWith('accepted_'))).toBe(true)
    expect(emitted).toHaveLength(1)
    expect(emitted[0]).toMatchObject({ ended_by: 'room_entry', cleared: true, collisions: 0 })
    expect(emitted[0].provenance).toEqual(r.provenance)
  })

  it('watchdog_gap: silence longer than 250 ms removes torque and the rover does not move until commands resume', () => {
    const r = load('watchdog_gap')
    const arrivals = r.datagrams.map((d) => d.receive_ms)
    let gap = [0, 0]
    for (let i = 1; i < arrivals.length; i += 1) if (arrivals[i] - arrivals[i - 1] > gap[1] - gap[0]) gap = [arrivals[i - 1], arrivals[i]]
    expect(gap[1] - gap[0]).toBeGreaterThan(250)

    let stoppedAt = null
    let moved = false
    const emitted = []
    replayRecording(r, {
      onAttemptEmit: (m) => emitted.push(m),
      onStep: (sim) => {
        if (sim.timeMs <= gap[0] + 250 || sim.timeMs >= gap[1]) return
        expect(sim.receiver.motorsHaveTorque()).toBe(false)
        if (stoppedAt === null) stoppedAt = sim.pose
        else if (sim.pose.x !== stoppedAt.x || sim.pose.y !== stoppedAt.y) moved = true
      },
    })
    expect(stoppedAt).not.toBeNull()
    expect(moved).toBe(false)
    expect(emitted[0]).toMatchObject({ ended_by: 'room_entry' })
  })

  it('estop_latch: the rover never moves after the e-stop and the latch is still set at the end', () => {
    const r = load('estop_latch')
    const estop = r.datagrams.find((d) => taskOf(d) === 'estop')
    expect(estop).toBeDefined()
    let poseAtEstop = null
    const { sim, outcomes } = replayRecording(r, {
      onStep: (s) => {
        if (s.timeMs <= estop.receive_ms) return
        if (poseAtEstop === null) poseAtEstop = s.pose
        expect(s.pose).toEqual(poseAtEstop)
      },
    })
    expect(outcomes[r.datagrams.indexOf(estop)]).toBe('accepted_stop')
    expect(sim.receiver.snapshot().software_estop_latch).toBe(true)
    expect(sim.attempt.state).not.toBe('emitted')
  })
})

describe('validateRecording', () => {
  const good = () => load('estop_latch')

  it('rejects a missing or unknown provenance source', () => {
    const r = good()
    r.provenance.source = 'measured'
    expect(validateRecording(r)).toContain('provenance.source: must be one of synthetic, hardware')
    delete r.provenance
    expect(validateRecording(r)).toContain('provenance: must be an object')
  })

  it('rejects arrivals out of order and an end before the last arrival', () => {
    const r = good()
    r.datagrams[1].receive_ms = r.datagrams[0].receive_ms - 1
    expect(validateRecording(r)).toContain('datagrams[1].receive_ms: arrivals must be in order')
    const s = good()
    s.end_ms = 0
    expect(validateRecording(s)).toContain('end_ms: must be a number at or after the last arrival')
  })

  it('accepts a datagram the receiver will refuse: the file is judged, its datagrams are not', () => {
    const r = good()
    r.datagrams.push({ sequence: null, receive_ms: r.end_ms, raw: '{not json' })
    expect(validateRecording(r)).toEqual([])
    expect(replayRecording(r).outcomes.at(-1)).toBe('malformed')
  })
})
