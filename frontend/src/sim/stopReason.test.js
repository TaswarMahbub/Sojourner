import { describe, expect, it } from 'vitest'
import { DRILL_CONTROLLER, DRILL_KEY, Simulation } from './simulation.js'
import { signCommand } from '../contracts/motor.js'
import { CORRIDOR_WORLD } from './worldModel.js'
import { REASONS } from './stopReason.js'

const T0 = 1795276800000

function boot({ arm = true } = {}) {
  let unix = T0
  const sim = new Simulation({ bootNonce: 42, unixNow: () => unix, world: CORRIDOR_WORLD })
  const tick = (ms) => {
    unix += ms
    sim.tick(ms)
  }
  sim.finishStartup()
  sim.setEstopPressed(false)
  sim.closeCutoff()
  sim.connect()
  tick(300)
  if (arm) {
    sim.requestArm()
    tick(300)
  }
  return { sim, tick }
}

describe('stop reasons — the readout names what happened', () => {
  it('nothing is named while armed and free to move', () => {
    const { sim } = boot()
    expect(sim.view().haltReason).toBeNull()
    expect(sim.view().taskState).toBe('idle') // the drill's keep-alive stops after the rearm
  })

  it('a watchdog gap stops the rover and is named "watchdog"', () => {
    const { sim, tick } = boot()
    sim.setDrive({ linear: 0.3, angular: 0 })
    tick(1000)
    expect(sim.view().torque).toBe(true)
    expect(sim.view().taskState).toBe('manual')
    sim.disconnect() // the link drops: no more datagrams
    tick(400)
    const atStop = sim.pose
    tick(1000)
    expect(sim.pose).toEqual(atStop) // stopped, and stays stopped
    expect(sim.view().torque).toBe(false)
    expect(sim.view().haltReason).toBe(REASONS.watchdog)
  })

  it('motion after a software e-stop is rejected as "estop latch"', () => {
    const { sim, tick } = boot()
    sim.softwareEstop()
    tick(20)
    expect(sim.view().haltReason).toBe(REASONS.estopLatch)
    sim.disconnect()
    sim.inject('generation') // any well-formed motion; the latch is checked first
    expect(sim.view().lastRejection.reason).toBe(REASONS.estopLatch)
  })

  it('motion under an old challenge is "stale challenge"', () => {
    const { sim } = boot()
    sim.disconnect()
    sim.inject('stale')
    expect(sim.view().lastRejection).toMatchObject({ reason: REASONS.staleChallenge, outcome: 'rejected_motion', source: 'injected' })
  })

  it('motion before arming is "not armed"', () => {
    const { sim } = boot({ arm: false })
    expect(sim.view().haltReason).toBe(REASONS.notArmed)
    // A fresh, correctly signed motion datagram for the active session — valid in every respect
    // except that nobody armed the receiver. (The drill sender itself never sends one.)
    const st = sim.status
    const now = T0 + 300
    const m = {
      schema_version: '4', controller_id: DRILL_CONTROLLER, boot_nonce: st.boot_nonce, session_id: st.session_id,
      sequence: st.last_sequence + 1, freshness_id: st.freshness_id, rearm_generation: st.rearm_generation,
      status_challenge: st.status_challenge, issued_at_ms: now, expires_at_ms: now + 250,
      task_state: 'manual', linear_mps: 0.2, angular_rad_s: 0, stop: false,
    }
    sim.disconnect()
    expect(sim.deliver(JSON.stringify({ ...m, mac: signCommand(DRILL_KEY, m) }), 'injected', 'unarmed motion')).toBe('rejected_motion')
    expect(sim.view().lastRejection.reason).toBe(REASONS.notArmed)
    expect(sim.pose).toEqual(CORRIDOR_WORLD.spawn)
  })

  it('a wrong rearm generation is named as such', () => {
    const { sim } = boot()
    sim.disconnect()
    sim.inject('generation')
    expect(sim.view().lastRejection.reason).toBe(REASONS.wrongGeneration)
  })

  it('names forged, foreign-boot and malformed datagrams', () => {
    const { sim } = boot()
    sim.disconnect()
    sim.inject('badmac')
    expect(sim.view().lastRejection.reason).toBe(REASONS.unauthenticated)
    sim.inject('wrongboot')
    expect(sim.view().lastRejection.reason).toBe(REASONS.wrongBoot)
    sim.inject('malformed')
    expect(['malformed', REASONS.invalid]).toContain(sim.view().lastRejection.reason)
  })

  it('the reason is recorded on the history entry too', () => {
    const { sim } = boot()
    sim.disconnect()
    sim.inject('stale')
    expect(sim.history[0].reason).toBe(REASONS.staleChallenge)
  })
})
