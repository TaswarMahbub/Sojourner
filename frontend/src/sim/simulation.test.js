import { describe, expect, it } from 'vitest'
import { Simulation } from './simulation.js'

const T0 = 1795276800000

// Startup, release the stop, close the cutoff, connect the drill, and arm.
function armed() {
  let unix = T0
  const sim = new Simulation({ bootNonce: 42, unixNow: () => unix })
  const tick = (ms) => {
    unix += ms
    sim.tick(ms)
  }
  sim.finishStartup()
  sim.setEstopPressed(false)
  expect(sim.closeCutoff()).toBe(true)
  sim.connect()
  tick(300)
  sim.requestArm()
  tick(300)
  return { sim, tick }
}

const outcomes = (sim) => sim.history.map((e) => e.outcome)

describe('2D simulator with the drill sender', () => {
  it('opens a session, waits for the status echo, and arms only after an explicit request', () => {
    const { sim } = armed()
    expect(outcomes(sim)).toContain('accepted_start')
    expect(outcomes(sim)).toContain('accepted_rearm')
    expect(sim.status.armed).toBe(true)
    expect(sim.status.session_id).not.toBeNull()
    expect(sim.view().sessionTrusted).toBe(true)
  })

  it('does not arm without the operator asking', () => {
    const sim = new Simulation({ bootNonce: 7 })
    sim.finishStartup()
    sim.setEstopPressed(false)
    sim.closeCutoff()
    sim.connect()
    sim.tick(1000)
    expect(sim.status.armed).toBe(false)
    expect(outcomes(sim)).not.toContain('accepted_rearm')
  })

  it('drives forward only on accepted motion, at the commanded speed', () => {
    const { sim, tick } = armed()
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(1000)
    expect(outcomes(sim)).toContain('accepted_motion')
    // About 0.2 m in one second, less the first status cycle before motion starts.
    expect(sim.pose.x).toBeGreaterThan(0.15)
    expect(sim.pose.x).toBeLessThanOrEqual(0.2)
    expect(Math.abs(sim.pose.y)).toBeLessThan(1e-9)
  })

  it('turns left for positive yaw rate (REP-103: counterclockwise from above)', () => {
    const { sim, tick } = armed()
    sim.setDrive({ linear: 0.1, angular: 0.5 })
    tick(1000)
    expect(sim.pose.heading).toBeGreaterThan(0.3)
    expect(sim.pose.y).toBeGreaterThan(0)
  })

  it('stops when the physical e-stop is pressed and stays stopped after release until rearmed', () => {
    const { sim, tick } = armed()
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(500)
    sim.setEstopPressed(true)
    const x = sim.pose.x
    tick(500)
    expect(sim.pose.x).toBe(x)
    sim.setEstopPressed(false)
    tick(500)
    expect(sim.pose.x).toBe(x)
    expect(sim.status.armed).toBe(false)
    // The cutoff stays open with its hold set until the explicit restoration.
    expect(sim.status.cutoff_open).toBe(true)
  })

  it('a software e-stop latches and needs a rearm', () => {
    const { sim, tick } = armed()
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(300)
    sim.softwareEstop()
    expect(sim.status.software_estop_latch).toBe(true)
    const x = sim.pose.x
    tick(500)
    expect(sim.pose.x).toBe(x)
    sim.requestArm()
    tick(600)
    expect(sim.status.software_estop_latch).toBe(false)
    tick(500)
    expect(sim.pose.x).toBeGreaterThan(x)
  })

  it('refuses injected datagrams and never moves the rover for them', () => {
    const { sim, tick } = armed()
    const before = { ...sim.pose }
    sim.disconnect()
    for (const kind of ['stale', 'generation', 'badmac', 'wrongboot', 'malformed', 'replay']) {
      const outcome = sim.inject(kind)
      // A replayed stop is an older authenticated safety stop: applied (it stops), never accepted.
      expect(['rejected_motion', 'invalid_stop', 'ignored', 'malformed', 'stale_stop']).toContain(outcome)
      tick(20)
    }
    expect(sim.pose).toEqual(before)
    expect(sim.history.filter((e) => e.source === 'injected').every((e) => e.kind !== 'accepted')).toBe(true)
  })

  it('opens the cutoff within 250 ms when the firmware freezes mid-motion', () => {
    const { sim, tick } = armed()
    sim.setDrive({ linear: 0.2, angular: 0 })
    tick(500)
    sim.freezeFirmware()
    tick(260)
    expect(sim.receiver.cutoffOpen).toBe(true)
    const x = sim.pose.x
    tick(500)
    expect(sim.pose.x).toBe(x)
  })

  it('latches a fault when commands stop arriving (watchdog)', () => {
    const { sim, tick } = armed()
    sim.disconnect()
    tick(400)
    expect(sim.status.fault_latch).toBe(true)
    expect(sim.status.armed).toBe(false)
  })

  it('collapses repeated keep-alive stops into one history entry', () => {
    const { sim, tick } = armed()
    tick(2000)
    const top = sim.history[0]
    expect(top.outcome).toBe('accepted_stop')
    expect(top.count).toBeGreaterThan(10)
  })
})
