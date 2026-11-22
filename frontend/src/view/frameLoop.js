// The real-time frame loop (proposed invariant I8: a render error must never stop the simulation).
//
// Per animation frame, in this order:
//   1. re-register the next frame          — nothing below can stop the loop
//   2. bank elapsed time into the simulation (sim.tick) — the simulation advances first
//   3. readout callback, guarded            — a UI error cannot stop the simulation either
//   4. render the interpolated pose, guarded
// A render or readout exception is caught, counted and reported (the first one in full, then every
// `reportEvery`th), and the next frame runs as normal. The simulation never depends on the view.
//
// Elapsed time per frame is capped at `maxFrameMs` so a hidden tab or a long stall cannot reach the
// receiver as one multi-second gap (which would fire its watchdog). NOTE: when frames are slower
// than the cap, simulated time runs slower than wall time — see handoff_sim §9 clock audit (I-12).
//
// No three, no React, no DOM: tested with a fake rAF and a throwing render.

export const MAX_FRAME_MS = 50

const defaultReport = (where) => (err, count) => {
  if (count === 1) console.error(`3D ${where} failed; the simulation keeps running.`, err)
  else console.error(`3D ${where} failed ${count} times; the simulation keeps running. Last:`, err?.message ?? err)
}

/**
 * @param {object} o
 * @param {{ tick(ms:number):void, interpolatedPose():object }} o.sim
 * @param {(pose:object, now:number) => void} o.render
 * @param {(pose:object, now:number) => void} [o.onFrame] readout / UI hook, after the tick
 * @param {(cb:(t:number)=>void) => number} [o.raf]
 * @param {(id:number) => void} [o.caf]
 * @param {() => number} [o.clock] start time source (performance.now)
 * @param {number} [o.maxFrameMs]
 * @param {number} [o.reportEvery] report every Nth repeated failure after the first
 */
export function createFrameLoop({
  sim,
  render,
  onFrame = null,
  raf = (cb) => globalThis.requestAnimationFrame(cb),
  caf = (id) => globalThis.cancelAnimationFrame(id),
  clock = () => globalThis.performance.now(),
  maxFrameMs = MAX_FRAME_MS,
  reportEvery = 600,
  onRenderError = defaultReport('render'),
  onFrameError = defaultReport('readout'),
} = {}) {
  let id = 0
  let last = 0
  let running = false
  let renderErrors = 0
  let frameErrors = 0

  const loop = (now) => {
    if (!running) return
    id = raf(loop) // 1. first: nothing after this line can end the loop
    const dt = Math.min(maxFrameMs, Math.max(0, now - last))
    last = now
    sim.tick(dt) // 2. the simulation advances before anything view-side runs
    const pose = sim.interpolatedPose()
    if (onFrame) {
      try { onFrame(pose, now) } catch (err) { // 3.
        frameErrors += 1
        if (frameErrors === 1 || frameErrors % reportEvery === 0) onFrameError(err, frameErrors)
      }
    }
    try { render(pose, now) } catch (err) { // 4.
      renderErrors += 1
      if (renderErrors === 1 || renderErrors % reportEvery === 0) onRenderError(err, renderErrors)
    }
  }

  return {
    start() {
      if (running) return
      running = true
      last = clock()
      id = raf(loop)
    },
    stop() {
      running = false
      caf(id)
    },
    get running() { return running },
    get renderErrors() { return renderErrors },
    get frameErrors() { return frameErrors },
  }
}
