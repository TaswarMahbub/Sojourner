// test:artless — invariant I4. No asset, material, light or post effect may change a recorded
// number. This test boots the simulation with no renderer, no scene graph and no assets at all,
// replays a fixed drive timeline, and compares the emitted metrics object to a committed snapshot.
//
// The snapshot is the contract. Every later phase re-runs it. If it goes red, the change that made
// it red has reached into the instrument and must be reverted — not re-baselined.
//
// Re-baselines: exactly one is authorised, in a single commit naming both causes (answers §6): the
// handrails joining the collision world and the avatar length change (1.05 → 0.845 m). Both are
// already folded into this first snapshot. A further re-baseline is a bug; raise it with Mridhul.
//
// Determinism: the simulation runs on a fixed 1/120 s step with injected clocks, so the object is
// compared exactly, not approximately.

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { Simulation } from '../../src/sim/simulation.js'
import { CORRIDOR_WORLD } from '../../src/sim/worldModel.js'

const timeline = JSON.parse(readFileSync(new URL('./clean-traversal.timeline.json', import.meta.url), 'utf8'))

const SETUP = {
  finishStartup: (sim) => sim.finishStartup(),
  releaseEstop: (sim) => sim.setEstopPressed(false),
  closeCutoff: (sim) => sim.closeCutoff(),
  connect: (sim) => sim.connect(),
  requestArm: (sim) => sim.requestArm(),
}

/**
 * Replay the timeline.
 * @param {(ms:number) => number[]} slicer splits a duration into the frame deltas a host would tick
 * @param {(sim:Simulation) => void} [onFrame] called after every tick, as a renderer would be
 */
function replay(slicer, onFrame = () => {}) {
  const emitted = []
  let unix = timeline.unix_t0_ms
  const sim = new Simulation({
    bootNonce: timeline.boot_nonce,
    unixNow: () => unix,
    world: CORRIDOR_WORLD,
    provenance: timeline.provenance,
    onAttemptEmit: (r) => emitted.push(r),
  })
  const wait = (ms) => {
    for (const d of slicer(ms)) {
      unix += d
      sim.tick(d)
      onFrame(sim)
    }
  }
  for (const step of timeline.setup) {
    const m = /^wait (\d+)$/.exec(step)
    if (m) wait(Number(m[1]))
    else SETUP[step](sim)
  }
  for (const seg of timeline.segments) {
    sim.setDrive(seg.drive)
    wait(seg.ms)
  }
  sim.setDrive(null)
  return emitted
}

const whole = (ms) => [ms]
// A browser-like host: ~60 Hz frames with periodic dropped frames. Integer ms so time sums exactly.
const jittery = (ms) => {
  const out = []
  const pattern = [16, 17, 17, 33, 16, 17, 50, 17]
  let i = 0
  for (let done = 0; done < ms; i += 1) {
    const d = Math.min(pattern[i % pattern.length], ms - done)
    out.push(d)
    done += d
  }
  return out
}

describe('test:artless — the metrics cannot see the renderer', () => {
  it('the clean traversal emits exactly one metrics object, on room entry', () => {
    const emitted = replay(whole)
    expect(emitted).toHaveLength(1)
    expect(emitted[0].ended_by).toBe('room_entry')
    expect(emitted[0].provenance).toEqual(timeline.provenance)
  })

  it('matches the committed snapshot exactly', async () => {
    const [r] = replay(whole)
    await expect(`${JSON.stringify(r, null, 2)}\n`).toMatchFileSnapshot('./__snapshots__/clean-traversal.metrics.json')
  })

  it('a render loop reading the simulation every frame, with dropped frames, changes nothing', () => {
    const reads = []
    const rendered = replay(jittery, (sim) => {
      const p = sim.interpolatedPose()
      const v = sim.view()
      reads.push(p.x + v.alpha)
    })
    expect(reads.length).toBeGreaterThan(1000)
    expect(rendered).toEqual(replay(whole))
  })
})
