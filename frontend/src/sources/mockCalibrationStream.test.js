import { describe, expect, it } from 'vitest'
import broad from '../fixtures/calibration-profiles/broad.json'
import { validateChannelProgress } from '../contracts/calibrationProgress.js'
import { LOOP_PAUSE_MS, PROFILE_MS, REST_END_MS, createMockCalibrationStream, syntheticCalibrationEvents } from './mockCalibrationStream.js'
import { selectCalibrationSource } from './selectCalibrationSource.js'

describe('synthetic calibration events', () => {
  const events = syntheticCalibrationEvents()
  const progress = events.map((e) => e.event).filter((e) => e.record_type === 'channel_progress')

  it('says it is synthetic and every progress event is valid', () => {
    expect(events[0].event).toMatchObject({ record_type: 'session_header', measured: false })
    progress.forEach((e) => expect(validateChannelProgress(e)).toEqual([]))
  })

  it('uses the population prior only before the first quiet gap', () => {
    const prior = progress.filter((e) => e.basis === 'prior_default')
    expect(prior.length).toBeGreaterThan(0)
    expect(new Set(prior.map((e) => e.t_ms)).size).toBe(1)
    expect(prior[0].t_ms).toBe(Math.min(...progress.map((e) => e.t_ms)))
  })

  it('ends with one final value per profile channel, then the profile, at the protocol times', () => {
    const finals = progress.filter((e) => !e.provisional)
    expect(finals.map((e) => e.channel).sort()).toEqual(Object.keys(broad.enrolment_metrics.channels).sort())
    expect(finals.every((e) => e.t_ms === REST_END_MS)).toBe(true)
    expect(events.at(-1)).toEqual({ atMs: PROFILE_MS, event: broad })
  })

  it('is in time order', () => {
    events.slice(1).forEach((e, i) => expect(e.atMs).toBeGreaterThanOrEqual(events[i].atMs))
  })
})

describe('mock calibration stream', () => {
  // A hand-driven timer, so the test controls time.
  function timers() {
    let pending = null
    return {
      setTimer: (fn, ms) => { pending = { fn, ms }; return pending },
      clearTimer: () => { pending = null },
      fire: () => { const p = pending; pending = null; p.fn(); return p.ms },
      get pending() { return pending },
    }
  }

  it('starts at once, keeps the event spacing (scaled by speed), and pauses between loops', () => {
    const t = timers()
    const source = createMockCalibrationStream({ speed: 2, setTimer: t.setTimer, clearTimer: t.clearTimer })
    const got = []
    source.subscribe((e) => got.push(e))
    expect(source.kind).toBe('mock')
    expect(t.pending.ms).toBe(0)
    const all = syntheticCalibrationEvents()
    let elapsed = 0
    for (let i = 0; i < all.length; i += 1) elapsed += t.fire()
    expect(got).toEqual(all.map((e) => e.event))
    expect(elapsed).toBe(PROFILE_MS / 2)
    expect(t.pending.ms).toBe(LOOP_PAUSE_MS / 2)
  })

  it('stops its timer when the last listener leaves', () => {
    const t = timers()
    const source = createMockCalibrationStream({ setTimer: t.setTimer, clearTimer: t.clearTimer })
    const unsubscribe = source.subscribe(() => {})
    unsubscribe()
    expect(t.pending).toBeNull()
  })
})

describe('selectCalibrationSource', () => {
  it('defaults to the saved profiles', () => {
    expect(selectCalibrationSource('').kind).toBe('recorded')
  })

  it('picks the synthetic stream or the live stream from the address', () => {
    expect(selectCalibrationSource('?calibration=mock').kind).toBe('mock')
    expect(selectCalibrationSource('?calibration=live')).toMatchObject({ kind: 'live', url: 'ws://127.0.0.1:8766' })
    expect(selectCalibrationSource('?calibration=live&calibration_port=9001').url).toBe('ws://127.0.0.1:9001')
  })

  it('ignores a bad port and never takes a host', () => {
    expect(selectCalibrationSource('?calibration=live&calibration_port=evil.example:80').url).toBe('ws://127.0.0.1:8766')
    expect(selectCalibrationSource('?calibration=live&calibration_port=70000').url).toBe('ws://127.0.0.1:8766')
  })
})
