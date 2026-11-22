// Proposed invariant I8: a render error must never stop the simulation.
import { describe, expect, it, vi } from 'vitest'
import { Simulation } from '../sim/simulation.js'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'
import { MAX_FRAME_MS, createFrameLoop } from './frameLoop.js'

/** A hand-cranked requestAnimationFrame: frames run only when the test pumps them. */
function fakeRaf() {
  let queue = []
  let nextId = 1
  let t = 0
  return {
    raf: (cb) => { queue.push({ id: nextId, cb }); nextId += 1; return nextId - 1 },
    caf: (id) => { queue = queue.filter((f) => f.id !== id) },
    clock: () => t,
    pending: () => queue.length,
    pump(frames, dtMs = 1000 / 60) {
      for (let i = 0; i < frames; i += 1) {
        t += dtMs
        const due = queue
        queue = []
        for (const f of due) f.cb(t)
      }
    },
  }
}

/** A corridor simulation, armed and holding a straight-ahead drive (setup ticks the sim directly). */
function armedCorridorSim() {
  const sim = new Simulation({ bootNonce: 42, world: CORRIDOR_WORLD })
  sim.finishStartup()
  sim.setEstopPressed(false)
  sim.closeCutoff()
  sim.connect()
  sim.tick(300)
  sim.requestArm()
  sim.tick(300)
  sim.setDrive({ linear: 0.25, angular: 0 })
  return sim
}

const quiet = () => {}

describe('frame loop — a render error never stops the simulation (I8)', () => {
  it('render throwing on EVERY frame: the simulation keeps ticking, moving and accruing metrics', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const render = vi.fn(() => { throw new Error('GPU lost') })
    const loop = createFrameLoop({ sim, render, raf: f.raf, caf: f.caf, clock: f.clock, onRenderError: quiet })
    loop.start()

    const t0 = sim.timeMs
    const x0 = sim.pose.x
    f.pump(120) // 2 s
    const mid = { t: sim.timeMs, x: sim.pose.x, d: sim.attempt.metrics.distanceM, cues: sim.attempt.metrics.cues }
    f.pump(120) // 2 s more

    expect(render).toHaveBeenCalledTimes(240)
    expect(loop.renderErrors).toBe(240)
    expect(loop.running).toBe(true)
    expect(f.pending()).toBe(1) // the next frame is still registered
    // Simulated time advanced by the full 4 s of frames (each well under the 50 ms cap).
    expect(sim.timeMs - t0).toBeCloseTo(4000, -1)
    // The rover moved, and kept moving, under accepted commands.
    expect(mid.x).toBeGreaterThan(x0 + 0.3)
    expect(sim.pose.x).toBeGreaterThan(mid.x + 0.3)
    // The attempt is running and its metrics accrue across both halves.
    expect(sim.attempt.state).toBe('running')
    expect(mid.d).toBeGreaterThan(0.3)
    expect(sim.attempt.metrics.distanceM).toBeGreaterThan(mid.d + 0.3)
    expect(sim.attempt.metrics.firstMotionMs).not.toBeNull()
    expect(mid.cues).toBeGreaterThan(0)
  })

  it('the throwing run is step-for-step identical to a run whose render succeeds', () => {
    const run = (render) => {
      const sim = armedCorridorSim()
      const f = fakeRaf()
      const loop = createFrameLoop({ sim, render, raf: f.raf, caf: f.caf, clock: f.clock, onRenderError: quiet })
      loop.start()
      f.pump(180)
      return { pose: sim.pose, timeMs: sim.timeMs, distance: sim.attempt.metrics.distanceM, cues: sim.attempt.metrics.cues }
    }
    const ok = run(() => {})
    const broken = run(() => { throw new Error('shader compile failed') })
    const flaky = (() => { let n = 0; return () => { n += 1; if (n % 3 === 0) throw new Error('context lost') } })()
    expect(broken).toEqual(ok)
    expect(run(flaky)).toEqual(ok)
  })

  it('a readout (UI) error does not stop the simulation or the render either', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const render = vi.fn()
    const loop = createFrameLoop({ sim, render, onFrame: () => { throw new Error('setState after unmount') }, raf: f.raf, caf: f.caf, clock: f.clock, onFrameError: quiet })
    loop.start()
    const t0 = sim.timeMs
    f.pump(60)
    expect(render).toHaveBeenCalledTimes(60)
    expect(loop.frameErrors).toBe(60)
    expect(sim.timeMs - t0).toBeCloseTo(1000, -1)
  })

  it('the simulation ticks BEFORE render: render always sees this frame’s simulated time', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const seen = []
    const loop = createFrameLoop({ sim, render: () => seen.push(sim.timeMs), raf: f.raf, caf: f.caf, clock: f.clock })
    const t0 = sim.timeMs
    loop.start()
    f.pump(3, 25)
    // 25 ms per frame = exactly 3 steps of 1/120 s each.
    expect(seen.map((t) => +(t - t0).toFixed(6))).toEqual([25, 50, 75])
  })

  it('reports the first render error in full, then rate-limits', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const report = vi.fn()
    const loop = createFrameLoop({ sim, render: () => { throw new Error('x') }, raf: f.raf, caf: f.caf, clock: f.clock, onRenderError: report, reportEvery: 100 })
    loop.start()
    f.pump(250)
    expect(report.mock.calls.map((c) => c[1])).toEqual([1, 100, 200])
  })

  it('caps elapsed time per frame at MAX_FRAME_MS (a stall cannot fire the watchdog)', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const loop = createFrameLoop({ sim, render: () => {}, raf: f.raf, caf: f.caf, clock: f.clock })
    loop.start()
    const t0 = sim.timeMs
    f.pump(1, 5000)
    expect(sim.timeMs - t0).toBeCloseTo(MAX_FRAME_MS, 6)
    expect(sim.view().haltReason).toBeFalsy()
  })

  it('stop() cancels the registered frame and nothing runs after', () => {
    const sim = armedCorridorSim()
    const f = fakeRaf()
    const render = vi.fn()
    const loop = createFrameLoop({ sim, render, raf: f.raf, caf: f.caf, clock: f.clock })
    loop.start()
    f.pump(2)
    loop.stop()
    const t = sim.timeMs
    f.pump(10)
    expect(render).toHaveBeenCalledTimes(2)
    expect(sim.timeMs).toBe(t)
    expect(f.pending()).toBe(0)
  })
})
