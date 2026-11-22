// The 2D simulator: one simulated receiver boot, the drill sender, the rover's position, and the
// command history. Time is simulated: tick(ms) advances the receiver's monotonic clock.
//
// The rover moves only while the receiver reports torque (motorsHaveTorque), using the velocities
// the receiver applied. A datagram the receiver ignored or rejected never moves the rover
// (contract: the simulator "does not animate that datagram as rover motion").
//
// The receiver publishes a status every 100 ms. The status is a drill status: it comes from this
// simulated receiver and cannot arm a physical rover.

import { MotorReceiver } from './motorReceiver.js'
import { DrillSender } from './drillSender.js'
import { utf8 } from './hmac.js'
import { signCommand } from '../contracts/motor.js'

// The drill key and controller. They are not rover secrets and must never be provisioned.
export const DRILL_KEY = utf8('sojourner-simulator-drill-key-not-for-hardware')
export const DRILL_CONTROLLER = 'sim-drill'

export const STATUS_PERIOD_MS = 100
export const ARENA_HALF_M = 1.5
export const HISTORY_LIMIT = 60

// What each receiver outcome means, for the history.
export const OUTCOMES = {
  accepted_start: { kind: 'accepted', text: 'Accepted: session started (stopped, disarmed)' },
  accepted_rearm: { kind: 'accepted', text: 'Accepted: rearmed (latches cleared, still stopped)' },
  accepted_motion: { kind: 'accepted', text: 'Accepted: motion applied' },
  accepted_stop: { kind: 'accepted', text: 'Accepted: stop applied' },
  stale_stop: { kind: 'stop', text: 'Older safety stop applied, not accepted' },
  expired_stop: { kind: 'stop', text: 'Expired safety stop applied, not accepted' },
  rejected_motion: { kind: 'rejected', text: 'Rejected motion: safe output commanded' },
  rejected_rearm: { kind: 'rejected', text: 'Rejected rearm: not armed' },
  invalid_stop: { kind: 'rejected', text: 'Invalid for the active session: safe output commanded' },
  ignored: { kind: 'ignored', text: 'Ignored: no effect' },
  malformed: { kind: 'ignored', text: 'Malformed: ignored' },
  frozen: { kind: 'ignored', text: 'Firmware frozen: not processed' },
}

let nextEntryId = 1

export class Simulation {
  constructor({ bootNonce = 1 + Math.floor(Math.random() * 1e6), unixNow = () => Date.now() } = {}) {
    this.unixNow = unixNow
    this.receiver = new MotorReceiver(bootNonce, { key: DRILL_KEY, controllerId: DRILL_CONTROLLER })
    this.sender = new DrillSender({ key: DRILL_KEY, controllerId: DRILL_CONTROLLER, unixNow })
    this.timeMs = 0
    this.lastStatusMs = -STATUS_PERIOD_MS
    this.status = null
    this.drive = null // { linear, angular } while the operator holds a drive control
    this.senderEnabled = false
    this.lastRaw = null
    this.pose = { x: 0, y: 0, heading: 0 } // meters and radians; heading 0 faces +X
    this.trail = [{ x: 0, y: 0 }]
    this.history = []
  }

  // ---- Local rover controls. These model physical inputs on the rover, not network traffic. ----

  finishStartup() { this.receiver.completeStartup(); this.publishStatus() }
  setEstopPressed(pressed) { this.receiver.setSensor(pressed ? 'asserted' : 'released'); this.publishStatus() }
  closeCutoff() { const ok = this.receiver.acknowledgeCutoff(); this.publishStatus(); return ok }
  cutMotorPower() { this.receiver.loseMotorPower(); this.publishStatus() }
  restoreMotorPower() { this.receiver.restoreMotorPower(); this.publishStatus() }
  freezeFirmware() { this.receiver.freezeFirmware() }
  unfreezeFirmware() { this.receiver.unfreezeFirmware() }
  reboot() {
    this.receiver.brownoutReset(1 + Math.floor(Math.random() * 1e6))
    this.timeMs = 0
    this.lastStatusMs = -STATUS_PERIOD_MS
    this.publishStatus()
  }

  // ---- Operator controls on the (simulated) laptop. ----

  connect() { this.senderEnabled = true }
  disconnect() { this.senderEnabled = false; this.drive = null }
  requestArm() { this.sender.requestArm() }
  setDrive(drive) { this.drive = drive }
  softwareEstop() {
    const out = this.sender.softwareEstop(this.timeMs)
    if (out) this.send(out.message, 'drill', out.why)
    else this.log({ label: 'Software e-stop not sent', source: 'drill', kind: 'ignored', text: 'No current authenticated status to copy from' })
  }

  // ---- Hostile datagrams, injected to show that the receiver refuses them. ----

  inject(kind) {
    const s = this.status
    if (kind === 'replay') {
      if (!this.lastRaw) return
      return this.deliver(this.lastRaw, 'injected', 'Replay of the last datagram')
    }
    if (!s) return
    const base = {
      schema_version: '4',
      controller_id: DRILL_CONTROLLER,
      boot_nonce: s.boot_nonce,
      session_id: s.session_id ?? 1,
      sequence: (s.last_sequence ?? 0) + 1,
      freshness_id: s.freshness_id,
      rearm_generation: s.rearm_generation,
      status_challenge: 1,
      issued_at_ms: this.unixNow(),
      expires_at_ms: this.unixNow() + 250,
      task_state: 'manual',
      linear_mps: 0.3,
      angular_rad_s: 0,
      stop: false,
    }
    const signed = (m) => ({ ...m, mac: signCommand(DRILL_KEY, m) })
    if (kind === 'stale') return this.deliver(JSON.stringify(signed({ ...base, freshness_id: Math.max(1, s.freshness_id - 1) })), 'injected', 'Full-speed motion sealed under an old challenge')
    if (kind === 'generation') return this.deliver(JSON.stringify(signed({ ...base, rearm_generation: s.rearm_generation + 1 })), 'injected', 'Full-speed motion with the wrong rearm generation')
    if (kind === 'badmac') {
      const m = signed(base)
      return this.deliver(JSON.stringify({ ...m, mac: (m.mac[0] === 'f' ? 'e' : 'f') + m.mac.slice(1) }), 'injected', 'Full-speed motion with a forged MAC')
    }
    if (kind === 'wrongboot') return this.deliver(JSON.stringify(signed({ ...base, boot_nonce: s.boot_nonce + 1 })), 'injected', 'Full-speed motion for another boot')
    if (kind === 'malformed') return this.deliver(`{"boot_nonce": ${s.boot_nonce}, "session_id": ${s.session_id ?? 1}, "sequence": `, 'injected', 'Truncated datagram naming the active session')
  }

  // ---- Time. ----

  tick(ms) {
    const steps = Math.max(1, Math.round(ms / 10))
    const dt = ms / steps
    for (let i = 0; i < steps; i += 1) this.step(dt)
  }

  step(dtMs) {
    this.timeMs += dtMs
    this.receiver.setMonotonic(Math.floor(this.timeMs))
    this.integrate(dtMs / 1000)
    if (this.timeMs - this.lastStatusMs >= STATUS_PERIOD_MS) {
      this.publishStatus()
      if (this.senderEnabled) {
        const out = this.sender.next(this.receiver.monotonicMs, this.drive)
        if (out) this.send(out.message, 'drill', out.why)
      }
    }
  }

  publishStatus() {
    this.lastStatusMs = this.timeMs
    const status = this.receiver.status() // null while the firmware is frozen
    if (!status) return
    this.status = status
    this.sender.receiveStatus(status, this.receiver.monotonicMs)
  }

  integrate(dt) {
    if (!this.receiver.motorsHaveTorque()) return
    const v = this.receiver.linearMps
    const w = this.receiver.angularRadS
    const p = this.pose
    const heading = p.heading + w * dt
    const x = Math.max(-ARENA_HALF_M, Math.min(ARENA_HALF_M, p.x + v * Math.cos(heading) * dt))
    const y = Math.max(-ARENA_HALF_M, Math.min(ARENA_HALF_M, p.y + v * Math.sin(heading) * dt))
    this.pose = { x, y, heading }
    const last = this.trail[this.trail.length - 1]
    if (Math.hypot(x - last.x, y - last.y) > 0.02) this.trail = [...this.trail.slice(-300), { x, y }]
  }

  // ---- Datagrams and history. ----

  send(message, source, why) {
    this.deliver(JSON.stringify(message), source, why)
  }

  deliver(raw, source, why) {
    const outcome = this.receiver.handleRaw(raw)
    if (source === 'drill') this.lastRaw = raw
    let label
    try {
      const m = JSON.parse(raw)
      label = `${m.task_state} #${m.sequence}${m.stop ? '' : ` ${m.linear_mps} m/s, ${m.angular_rad_s} rad/s`}`
    } catch {
      label = 'Unparseable datagram'
    }
    this.log({ label, source, why, outcome, ...OUTCOMES[outcome] })
    this.publishStatus()
    return outcome
  }

  // Consecutive entries with the same label shape and outcome collapse into one with a count,
  // so a stream of keep-alive stops does not push everything else off the list.
  log(entry) {
    const shape = `${entry.source}|${entry.why}|${entry.outcome}|${entry.label.replace(/#\d+/, '')}`
    const top = this.history[0]
    if (top && top.shape === shape) {
      this.history = [{ ...top, label: entry.label, count: top.count + 1, timeMs: this.timeMs }, ...this.history.slice(1)]
      return
    }
    this.history = [{ ...entry, id: nextEntryId++, shape, count: 1, timeMs: this.timeMs }, ...this.history].slice(0, HISTORY_LIMIT)
  }

  // A plain snapshot for the UI.
  view() {
    const rx = this.receiver
    return {
      timeMs: this.timeMs,
      pose: this.pose,
      trail: this.trail,
      torque: rx.motorsHaveTorque(),
      output: { linear: rx.linearMps, angular: rx.angularRadS },
      local: {
        startupFinished: !rx.startupLatch,
        busPowered: rx.busPowered,
        firmwareFrozen: rx.firmwareFrozen,
        cutoffHold: rx.cutoffHold,
      },
      status: this.status,
      senderEnabled: this.senderEnabled,
      sessionTrusted: Boolean(this.sender.session?.trusted),
      armRequested: this.sender.armRequested,
      history: this.history,
    }
  }
}
