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

  it('hesitation is the dwell from the start of availability to the next accepted motion command', () => {
    const m = new MetricsRecorder()
    m.availability(true, 0)
    m.availability(true, 500) // still available: the dwell started at 0
    m.command(cmd(1200, 0.2), true)
    m.availability(false, 1210) // output now non-zero
    m.command(cmd(1300, 0.2), true) // repeat while moving: no availability, no dwell
    m.availability(true, 2000) // back at rest
    m.command(cmd(2400, 0, 0.5), true) // angular-only motion still commits
    m.availability(false, 2410)
    m.command(cmd(3000, 0.3), true) // never became available: no dwell
    const s = m.summary(1)
    expect(s.hesitation_s).toEqual([1.2, 0.4])
    expect(s.hesitation_median_s).toBeCloseTo(0.8, 12)
  })

  it('stops, rejections and pre-attempt commands do not commit a cue', () => {
    const m = new MetricsRecorder()
    m.availability(true, 0)
    m.command(cmd(100, 0, 0, { stop: true, taskState: 'idle' }), true) // keep-alive stop
    m.command({ ...cmd(200, 0.3), accepted: false }, true) // rejected
    m.command(cmd(250, 0.3), false) // attempt not running
    m.command(cmd(300, 0.3), true)
    expect(m.summary(1).hesitation_s).toEqual([0.3])
  })

  it('availability that ends without a commit (e-stop, fault) drops the pending dwell', () => {
    const m = new MetricsRecorder()
    m.availability(true, 0)
    m.availability(false, 400) // latched
    m.command(cmd(900, 0.2), true) // accepted later, but no availability preceded it
    m.availability(false, 910)
    m.availability(true, 1500)
    m.command(cmd(1700, 0.2), true)
    expect(m.summary(1).hesitation_s).toEqual([0.2])
  })
})
