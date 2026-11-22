import { describe, expect, it } from 'vitest'
import broad from '../fixtures/calibration-profiles/broad.json'
import { ProgressTracker, decodeChannelProgress, validateChannelProgress } from './calibrationProgress.js'

// The example event in docs/sim-decisions.md §2.3.
const EXAMPLE = {
  record_type: 'channel_progress',
  t_ms: 12840,
  channel: 'jawOpen',
  unit: 'score',
  probe: 'jaw',
  rep_index: 2,
  peak_deviation: 0.412,
  effective_std: 0.0183,
  snr: 22.5,
  provisional: true,
  basis: 'interrep_quiet',
}
const event = (changes = {}) => ({ ...EXAMPLE, ...changes })
const final = (channel, changes = {}) => event({
  channel,
  unit: broad.enrolment_metrics.channels[channel].unit,
  effective_std: broad.enrolment_metrics.channels[channel].rest.effective_std,
  provisional: false,
  basis: 'final_rest',
  ...changes,
})

describe('validateChannelProgress', () => {
  it('accepts the §2.3 example', () => {
    expect(validateChannelProgress(EXAMPLE)).toEqual([])
  })

  it('accepts a prior_default value before any quiet gap exists', () => {
    expect(validateChannelProgress(event({ basis: 'prior_default' }))).toEqual([])
  })

  it('keeps status apart from the measurement: provisional and basis must agree', () => {
    expect(validateChannelProgress(event({ basis: 'final_rest' }))).toEqual(['provisional: a final_rest value is not provisional'])
    expect(validateChannelProgress(event({ provisional: false }))).toEqual(['provisional: must be true for basis interrep_quiet; only final_rest is final'])
  })

  it('rejects the old provisional_* field names: the stable names are required', () => {
    const old = event({ provisional_effective_std: 0.0183, provisional_snr: 22.5 })
    delete old.effective_std
    delete old.snr
    expect(validateChannelProgress(old)).toEqual(['effective_std: must be a number above 0', 'snr: must be a number >= 0'])
  })

  it('rejects basis as free text', () => {
    expect(validateChannelProgress(event({ basis: 'inter-rep quiet windows' }))).toEqual(['basis: must be one of interrep_quiet, prior_default, final_rest'])
  })

  it('does not accept a fabricated zero noise estimate', () => {
    expect(validateChannelProgress(event({ effective_std: 0 }))).toEqual(['effective_std: must be a number above 0'])
  })

  it('rejects an unknown unit, probe or a negative rep', () => {
    expect(validateChannelProgress(event({ unit: 'mm', probe: 'wink', rep_index: -1 }))).toEqual([
      'unit: must be one of score, deg, cm',
      'probe: must be one of brows, jaw, smile, turn, nod, tilt',
      'rep_index: must be an integer >= 0',
    ])
  })

  it('a final_rest event may leave probe and rep_index null (OPEN); a provisional one may not', () => {
    expect(validateChannelProgress(event({ provisional: false, basis: 'final_rest', probe: null, rep_index: null }))).toEqual([])
    expect(validateChannelProgress(event({ probe: null, rep_index: null }))).toHaveLength(2)
  })
})

describe('decodeChannelProgress', () => {
  it('decodes the example from text', () => {
    expect(decodeChannelProgress(JSON.stringify(EXAMPLE))).toEqual({ valid: true, errors: [], event: EXAMPLE })
  })

  it('rejects integers written as floats and duplicate keys', () => {
    const text = JSON.stringify(EXAMPLE).replace('"t_ms":12840', '"t_ms":12840.0')
    expect(decodeChannelProgress(text).errors).toEqual(['t_ms: must be written as an integer, not a float'])
    expect(decodeChannelProgress('{"t_ms": 1, "t_ms": 2}').valid).toBe(false)
  })
})

describe('ProgressTracker (§2.4 reconciliation)', () => {
  const channels = ['jawOpen', 'browInnerUp']

  it('accepts provisional values, then one final per channel, then a profile that agrees', () => {
    const t = new ProgressTracker()
    expect(t.apply(event({ channel: 'jawOpen', t_ms: 100 }))).toEqual([])
    expect(t.apply(event({ channel: 'browInnerUp', probe: 'brows', t_ms: 200 }))).toEqual([])
    channels.forEach((c, i) => expect(t.apply(final(c, { t_ms: 300 + i }))).toEqual([]))
    expect(t.apply(broad)).toEqual([])
    expect(t.latest.get('jawOpen').basis).toBe('final_rest')
  })

  it('flags a profile that arrives before a channel has its final value', () => {
    const t = new ProgressTracker()
    t.apply(event({ channel: 'jawOpen' }))
    expect(t.apply(broad)).toEqual(["jawOpen: calibration_profile arrived before this channel's final_rest value"])
  })

  it('flags a final value the stored profile disagrees with', () => {
    const t = new ProgressTracker()
    t.apply(final('jawOpen', { effective_std: 0.5 }))
    const stored = broad.enrolment_metrics.channels.jawOpen.rest.effective_std
    expect(t.apply(broad)).toEqual([`jawOpen: final effective_std 0.5 differs from the profile's ${stored}`])
  })

  it('flags a provisional value after the final one, a second final, and time going backwards', () => {
    const t = new ProgressTracker()
    t.apply(final('jawOpen', { t_ms: 500 }))
    expect(t.apply(event({ t_ms: 600 }))).toEqual(['jawOpen: provisional value after its final_rest value'])
    expect(t.apply(final('jawOpen', { t_ms: 700 }))).toEqual(['jawOpen: second final_rest value'])
    expect(t.apply(event({ channel: 'browInnerUp', probe: 'brows', t_ms: 400 }))).toEqual(['t_ms: 400 is before the previous event (500)'])
  })

  it('flags progress after the profile, and lets other calibration events pass', () => {
    const t = new ProgressTracker()
    expect(t.apply({ record_type: 'marker', event: 'segment_start' })).toEqual([])
    t.apply(broad)
    expect(t.apply(event())).toEqual(['jawOpen: channel_progress after the calibration_profile'])
  })

  it('does not record a rejected event as the latest value', () => {
    const t = new ProgressTracker()
    t.apply(event({ effective_std: 0 }))
    expect(t.latest.size).toBe(0)
  })
})
