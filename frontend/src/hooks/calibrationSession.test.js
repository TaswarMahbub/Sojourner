import { describe, expect, it } from 'vitest'
import broad from '../fixtures/calibration-profiles/broad.json'
import twoNarrow from '../fixtures/calibration-profiles/two-narrow.json'
import { CalibrationSession, phaseOf } from './calibrationSession.js'
import { syntheticCalibrationEvents } from '../sources/mockCalibrationStream.js'

const playAll = (session, events) => events.reduce((_, { event }) => session.receive(event), session.state)

describe('CalibrationSession', () => {
  it('plays the synthetic session with no problems: provisional, then final, then the profile', () => {
    const s = new CalibrationSession()
    const phases = new Set()
    for (const { event } of syntheticCalibrationEvents()) phases.add(phaseOf(s.receive(event)))
    expect([...phases]).toEqual(['waiting', 'provisional', 'final', 'profile'])
    expect(s.state.problems).toEqual([])
    expect(s.state.profile).toBe(broad)
    expect(s.state.header).toEqual({ session_id: 'synthetic-stream', source: 'synthetic', measured: false })
  })

  it('accepts the same session as raw text, as the websocket delivers it', () => {
    const s = new CalibrationSession()
    for (const { event } of syntheticCalibrationEvents()) s.receive(JSON.stringify(event))
    expect(s.state.problems).toEqual([])
    expect(phaseOf(s.state)).toBe('profile')
  })

  it('a session_header drops everything from the previous session', () => {
    const s = new CalibrationSession()
    playAll(s, syntheticCalibrationEvents())
    s.receive({ record_type: 'session_header', session_id: 'next', measured: true })
    expect(s.state.profile).toBeNull()
    expect(s.state.progress).toEqual([])
    expect(phaseOf(s.state)).toBe('waiting')
  })

  it('lists a profile that arrives before its final values, and still shows the profile', () => {
    const s = new CalibrationSession()
    const events = syntheticCalibrationEvents().filter(({ event }) => event.basis !== 'final_rest')
    playAll(s, events)
    expect(s.state.profile).toBe(broad)
    expect(s.state.problems.map((p) => p.error)).toContain("jawOpen: calibration_profile arrived before this channel's final_rest value")
  })

  it('lists a final value the profile disagrees with', () => {
    const s = new CalibrationSession()
    const events = syntheticCalibrationEvents().map(({ atMs, event }) => (
      event.basis === 'final_rest' && event.channel === 'yaw_deg' ? { atMs, event: { ...event, effective_std: 1 } } : { atMs, event }))
    playAll(s, events)
    expect(s.state.problems).toEqual([{ where: 'calibration_profile', error: `yaw_deg: final effective_std 1 differs from the profile's ${broad.enrolment_metrics.channels.yaw_deg.rest.effective_std}` }])
  })

  it('lists an invalid event without drawing it, and rejects text that is not JSON', () => {
    const s = new CalibrationSession()
    s.receive({ record_type: 'channel_progress', channel: 'jawOpen' })
    expect(s.state.progress).toEqual([])
    expect(s.state.problems[0].where).toBe('jawOpen')
    s.receive('{not json')
    expect(s.state.problems[0]).toEqual({ where: 'message', error: 'message: not valid JSON' })
  })

  it('rejects an integer written as a float in text', () => {
    const s = new CalibrationSession()
    const [, first] = syntheticCalibrationEvents()
    s.receive(JSON.stringify(first.event).replace(`"t_ms":${first.event.t_ms}`, `"t_ms":${first.event.t_ms}.0`))
    expect(s.state.problems[0].error).toBe('t_ms: must be written as an integer, not a float')
  })

  it('keeps an invalid profile out of the drawing and reports why', () => {
    const s = new CalibrationSession()
    s.receive({ ...broad, proposed_tier: { ...broad.proposed_tier, tier: 7 } })
    expect(s.state.profile).toBeNull()
    expect(s.state.profileErrors.length).toBeGreaterThan(0)
  })

  it('a recorded source switching profiles just replaces the profile', () => {
    const s = new CalibrationSession()
    s.receive(broad)
    s.receive(twoNarrow)
    expect(s.state.profile).toBe(twoNarrow)
    expect(s.state.problems).toEqual([])
  })

  it('ignores the other calibration events', () => {
    const s = new CalibrationSession()
    const before = s.state
    expect(s.receive({ record_type: 'marker', event: 'segment_start' })).toBe(before)
  })
})
