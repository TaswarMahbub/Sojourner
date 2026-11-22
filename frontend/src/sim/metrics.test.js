import { describe, expect, it } from 'vitest'
import { MetricsRecorder, median } from './metrics.js'

const cmd = (tMs, linear, angular = 0, extra = {}) => ({ tMs, accepted: true, taskState: 'manual', linear, angular, stop: false, ...extra })
const stepAt = (clearance, extra = {}) => ({ from: { x: 0, y: 0 }, to: { x: 0, y: 0 }, clearance, penetration: 0, blocked: false, ...extra })

describe('median', () => {
  it('handles empty, odd and even lists', () => {
    expect(median([])).toBeNull()
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
  })
})

describe('MetricsRecorder', () => {
  it('counts accepted transitions only; keep-alive repeats and rejections are not cues', () => {
    const m = new MetricsRecorder()
    m.command(cmd(0, 0.2), true)
    m.command(cmd(100, 0.2), true) // repeat
    m.command({ ...cmd(150, 0.3), accepted: false }, true) // rejected
    m.command(cmd(200, 0.2, 0.5), true)
    m.command(cmd(300, 0, 0, { stop: true, taskState: 'idle' }), true)
    m.command(cmd(400, 0, 0, { stop: true, taskState: 'idle' }), true)
    expect(m.cues).toBe(3)
  })

  it('does not count transitions before the attempt runs, but remembers the last one', () => {
    const m = new MetricsRecorder()
    m.command(cmd(0, 0, 0, { stop: true, taskState: 'idle' }), false)
    m.command(cmd(100, 0, 0, { stop: true, taskState: 'idle' }), true)
    expect(m.cues).toBe(0)
  })

  it('time to clear runs from the first accepted non-zero command', () => {
    const m = new MetricsRecorder()
    m.command(cmd(50, 0, 0, { stop: true, taskState: 'idle' }), true)
    m.command(cmd(1000, 0.3), true)
    m.cleared(13500)
    expect(m.summary(10).time_to_clear_s).toBe(12.5)
  })

  it('one scrape is one collision; it does not also count as a near-miss', () => {
    const m = new MetricsRecorder()
    for (const c of [0.2, 0.04, 0.001, 0.0005, 0.001, 0.03, 0.2]) m.step(stepAt(c))
    m.finish()
    expect(m.collisions).toBe(1)
    expect(m.nearMisses).toBe(0)
    expect(m.minClearanceM).toBe(0.0005)
  })

  it('a blocked step counts as contact even if clearance reads above the tolerance', () => {
    const m = new MetricsRecorder()
    m.step(stepAt(0.003, { blocked: true }))
    expect(m.collisions).toBe(1)
  })

  it('two separate dips below 0.05 m without contact are two near-misses', () => {
    const m = new MetricsRecorder()
    for (const c of [0.2, 0.049, 0.03, 0.06, 0.2, 0.04, 0.05]) m.step(stepAt(c))
    m.finish()
    expect(m.nearMisses).toBe(2)
    expect(m.collisions).toBe(0)
  })

  it('an open near-miss episode is closed at finish()', () => {
    const m = new MetricsRecorder()
    m.step(stepAt(0.04))
    m.finish()
    expect(m.nearMisses).toBe(1)
  })

  it('path efficiency and cues per metre come from integrated distance', () => {
    const m = new MetricsRecorder()
    m.command(cmd(0, 0.3), true)
    m.step({ ...stepAt(1), from: { x: 0, y: 0 }, to: { x: 3, y: 4 } })
    m.cleared(1000)
    const s = m.summary(4)
    expect(s.distance_m).toBe(5)
    expect(s.path_efficiency).toBe(1.25)
    expect(s.cues_per_m).toBe(0.2)
  })

  it('path efficiency and time to clear are null when the room was never entered', () => {
    const m = new MetricsRecorder()
    m.command(cmd(0, 0.3), true)
    m.step({ ...stepAt(1), to: { x: 1, y: 0 } })
    const s = m.summary(4)
    expect(s.cleared).toBe(false)
    expect(s.path_efficiency).toBeNull()
    expect(s.time_to_clear_s).toBeNull()
  })

  it('hesitation is the dwell from availability to the next committed cue', () => {
    const m = new MetricsRecorder()
    m.cueAvailable(0)
    m.cueAvailable(500) // first signal wins
    m.command(cmd(1200, 0.2), true)
    m.command(cmd(1300, 0.2), true) // repeat: not a cue
    m.cueAvailable(2000)
    m.command(cmd(2400, 0.1), true)
    m.command(cmd(3000, 0.3), true) // cue with no availability mark: no dwell
    const s = m.summary(1)
    expect(s.hesitation_s).toEqual([1.2, 0.4])
    expect(s.hesitation_median_s).toBeCloseTo(0.8, 12)
  })
})
