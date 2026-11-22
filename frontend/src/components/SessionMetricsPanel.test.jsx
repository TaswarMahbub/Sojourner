// "Panel reads correctly after one doorway run": drive the corridor simulation through the
// doorway, take the object the attempt emits, and check the panel shows exactly those numbers.

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Simulation } from '../sim/simulation.js'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'
import SessionMetricsPanel from './SessionMetricsPanel.jsx'
import { metricRows, provenanceLabel } from './sessionMetrics.js'

const PROVENANCE = { source: 'synthetic', derived_from: 'contracts.md §3', generated: '2026-11-22', generator: 'SessionMetricsPanel.test.jsx' }
const DOWN = -Math.PI / 2

// The same scripted traversal as simulation.corridor.test.js.
function doorwayRun() {
  let unix = 1795276800000
  const emitted = []
  const sim = new Simulation({ bootNonce: 42, unixNow: () => unix, world: CORRIDOR_WORLD, provenance: PROVENANCE, onAttemptEmit: (r) => emitted.push(r) })
  const tick = (ms) => { unix += ms; sim.tick(ms) }
  const driveUntil = (drive, done, maxMs = 60000) => {
    sim.setDrive(drive)
    for (let t = 0; t < maxMs && !done(sim); t += 100) tick(100)
    sim.setDrive(null)
  }
  sim.finishStartup()
  sim.setEstopPressed(false)
  sim.closeCutoff()
  sim.connect()
  tick(300)
  sim.requestArm()
  tick(300)
  driveUntil({ linear: 0.3, angular: 0 }, (s) => s.pose.x >= 7.3)
  driveUntil({ linear: 0.05, angular: 0 }, (s) => s.pose.x >= 7.495)
  tick(300)
  driveUntil({ linear: 0, angular: -0.5 }, (s) => s.pose.heading <= DOWN + 0.12)
  driveUntil({ linear: 0, angular: -0.05 }, (s) => s.pose.heading <= DOWN + 0.004)
  tick(300)
  driveUntil({ linear: 0.15, angular: 0 }, (s) => s.attempt.state === 'emitted', 20000)
  expect(emitted).toHaveLength(1)
  return { result: emitted[0], sim }
}

describe('session metrics after one doorway run', () => {
  const { result, sim } = doorwayRun()
  const html = renderToStaticMarkup(<SessionMetricsPanel result={sim.view().attemptResult} />)

  it('the simulation view exposes the emitted object the panel reads', () => {
    expect(sim.view().attemptResult).toBe(result)
  })

  it('shows each metric from the emitted object, with its unit and fixed decimals', () => {
    expect(html).toContain('Attempt 1: Entered the room')
    expect(html).toContain(`${result.time_to_clear_s.toFixed(1)}</span><span class="session-metrics__unit"> s</span>`)
    expect(html).toContain(`>${result.collisions}</span><span class="session-metrics__unit"> contacts</span>`)
    expect(html).toContain(`${result.path_efficiency.toFixed(2)}</span><span class="session-metrics__unit"> × shortest path</span>`)
    expect(html).toContain(`${result.cues_per_m.toFixed(2)}</span><span class="session-metrics__unit"> cues/m</span>`)
    expect(html).toContain('Near misses within 0.05 m: 0')
  })

  it('labels the attempt as driven by synthetic commands', () => {
    expect(html).toContain('SYNTHETIC COMMANDS')
  })
})

describe('metricRows', () => {
  const base = {
    ended_by: 'reset', near_miss_threshold_m: 0.05, time_to_clear_s: null, collisions: 1, near_misses: 2,
    path_efficiency: null, distance_m: 3.21, optimal_path_m: 10.4, cues: 0, cues_per_m: 0,
  }

  it('shows a dash, not a number, for metrics an uncleared attempt does not have', () => {
    const [time, collisions, efficiency, cues] = metricRows(base)
    expect(time).toMatchObject({ value: null, note: 'Did not clear' })
    expect(collisions).toMatchObject({ value: '1', unit: 'contact', note: 'Near misses within 0.05 m: 2' })
    expect(efficiency).toMatchObject({ value: null, note: 'Only for an attempt that cleared' })
    expect(cues).toMatchObject({ value: '0.00', unit: 'cues/m', note: '0 cues over 3.21 m' })
    const html = renderToStaticMarkup(<SessionMetricsPanel result={base} />)
    expect(html).toContain('Reset before clearing')
    expect(html).toContain('—')
  })

  it('shows a dash for cues per metre when the rover never moved', () => {
    expect(metricRows({ ...base, distance_m: 0, cues_per_m: null })[3].value).toBeNull()
  })
})

describe('provenanceLabel', () => {
  it('never lets a synthetic attempt read as hardware', () => {
    expect(provenanceLabel({ provenance: { source: 'synthetic' } })).toBe('SYNTHETIC COMMANDS')
    expect(provenanceLabel({ provenance: { source: 'hardware' } })).toBe('HARDWARE COMMANDS')
    expect(provenanceLabel({ provenance: null })).toBe('UNKNOWN SOURCE')
  })
})

describe('before any attempt', () => {
  it('says how to finish one', () => {
    expect(renderToStaticMarkup(<SessionMetricsPanel result={null} />)).toContain('No attempt has finished yet')
  })
})
