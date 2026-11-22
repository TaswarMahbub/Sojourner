// The simulator's drill sender: a stand-in for laptop shared control, the motor-command publisher
// (docs/contracts.md section 3, "Sessions on UDP", and section 4, "Publisher status gate").
// It exists so the simulator has traffic to show. It is not the shared-control publisher and it
// sends nothing over a network. It signs with a drill key that is not a rover secret.
//
// It follows the protocol a real sender must follow:
// - It only acts on a status whose MAC verifies, whose schema is valid, whose controller is ours,
//   and whose status_sequence is above the highest it has accepted for that boot.
// - Every command copies boot_nonce, freshness_id and rearm_generation from that status. It never
//   invents them.
// - After a session start it trusts the session only once a later status echoes its challenge.
// - Every later command needs a status that echoes the outstanding challenge, received within
//   250 ms, for a challenge issued within 250 ms. Sending a command issues a new challenge, so
//   one status authorizes at most one command. A cached status is never reused.
// - Motion needs that status to show armed, the sensor released, the cutoff closed, and both
//   latches clear. Rearm needs an explicit local operator request and the sensor released and
//   the cutoff closed; it does not need the latches clear.
//
// Simplifications, because this is a drill: high-water marks are kept in memory only (the
// contract's persisted state is a laptop duty), and challenge ids restart with each page load.

import { SCHEMA_VERSION } from '../contracts/cameraFrame.js'
import { signCommand, validateMotorStatus, verifyStatus } from '../contracts/motor.js'

export const GATE_MS = 250
export const COMMAND_LIFETIME_MS = 250

export class DrillSender {
  constructor({ key, controllerId, unixNow = () => Date.now() }) {
    this.key = key
    this.controllerId = controllerId
    this.unixNow = unixNow
    this.challengeHighWater = 0
    this.sessionHighWater = 0
    this.statusHighWater = new Map() // boot_nonce -> highest accepted status_sequence
    this.boot = null
    this.session = null // { id, sequence, trusted }
    this.outstanding = null // { id, issuedMono }
    this.current = null // { status, receivedMono }, the newest accepted status
    this.armRequested = false
  }

  // Returns null when accepted, or the reason the status was refused.
  receiveStatus(status, nowMono) {
    if (!verifyStatus(this.key, status) || status.controller_id !== this.controllerId) return 'bad MAC or controller'
    if (validateMotorStatus(status).length) return 'invalid schema'
    const high = this.statusHighWater.get(status.boot_nonce) ?? 0
    if (status.status_sequence <= high) return 'repeated or older status_sequence'
    if (this.boot !== null && status.boot_nonce !== this.boot) {
      if (status.armed) return 'armed status from a different boot'
      // A disarmed status from a new boot may be adopted. It authorizes nothing yet.
      this.session = null
      this.outstanding = null
    }
    this.statusHighWater.set(status.boot_nonce, status.status_sequence)
    this.boot = status.boot_nonce
    if (this.session && this.outstanding && status.session_id === this.session.id && status.status_challenge === this.outstanding.id) {
      this.session.trusted = true
    }
    this.current = { status, receivedMono: nowMono }
    return null
  }

  requestArm() {
    this.armRequested = true
  }

  // A status that may authorize the next command, or null. It must echo the outstanding challenge
  // and both ages must be within 250 ms.
  gatedStatus(nowMono) {
    const cur = this.current
    if (!cur || !this.session || !this.session.trusted || !this.outstanding) return null
    const s = cur.status
    if (s.boot_nonce !== this.boot || s.session_id !== this.session.id) return null
    if (s.status_challenge !== this.outstanding.id) return null
    if (nowMono - this.outstanding.issuedMono > GATE_MS || nowMono - cur.receivedMono > GATE_MS) return null
    // The next sequence must be greater than last_sequence.
    if (s.last_sequence === null || this.session.sequence + 1 <= s.last_sequence) return null
    return s
  }

  // The session is stuck when its outstanding challenge has aged out without being echoed:
  // the last command was not accepted, so no status will echo it. The drill opens a new session.
  sessionStuck(nowMono) {
    return Boolean(this.session && this.outstanding && nowMono - this.outstanding.issuedMono > GATE_MS && !this.gatedStatus(nowMono))
  }

  // Decide the next datagram for this cycle. drive = { linear, angular } or null for no input.
  // Returns { message, why } or null when nothing may be sent.
  next(nowMono, drive) {
    const cur = this.current
    if (!cur || nowMono - cur.receivedMono > GATE_MS) return null

    if (!this.session || this.sessionStuck(nowMono)) return { message: this.sessionStart(cur.status, nowMono), why: 'session start' }

    const s = this.gatedStatus(nowMono)
    if (!s) return null

    if (this.armRequested && s.sensor_state === 'released' && !s.physical_estop && !s.cutoff_open) {
      this.armRequested = false
      return { message: this.command(s, nowMono, 'rearm', true, 0, 0), why: 'operator asked to arm' }
    }
    const motionAllowed = s.armed && !s.cutoff_open && s.sensor_state === 'released' && !s.physical_estop && !s.software_estop_latch && !s.fault_latch
    if (drive && motionAllowed && (drive.linear !== 0 || drive.angular !== 0)) {
      return { message: this.command(s, nowMono, 'manual', false, drive.linear, drive.angular), why: 'operator input' }
    }
    // No input, or motion not authorized: keep the session alive with an ordinary stop.
    return { message: this.command(s, nowMono, 'idle', true, 0, 0), why: drive && !motionAllowed ? 'motion not authorized by status' : 'no input' }
  }

  softwareEstop(nowMono) {
    const s = this.gatedStatus(nowMono)
    if (!s) return null
    return { message: this.command(s, nowMono, 'estop', true, 0, 0), why: 'operator software e-stop' }
  }

  sessionStart(status, nowMono) {
    this.sessionHighWater += 1
    this.session = { id: this.sessionHighWater, sequence: 1, trusted: false }
    return this.seal(
      {
        boot_nonce: status.boot_nonce,
        session_id: this.session.id,
        sequence: 1,
        freshness_id: status.freshness_id,
        rearm_generation: status.rearm_generation,
        task_state: 'idle',
        linear_mps: 0,
        angular_rad_s: 0,
        stop: true,
      },
      nowMono,
    )
  }

  command(status, nowMono, task, stop, linear, angular) {
    this.session.sequence += 1
    return this.seal(
      {
        boot_nonce: status.boot_nonce,
        session_id: this.session.id,
        sequence: this.session.sequence,
        freshness_id: status.freshness_id,
        rearm_generation: status.rearm_generation,
        task_state: task,
        linear_mps: linear,
        angular_rad_s: angular,
        stop,
      },
      nowMono,
    )
  }

  // Issues a new challenge (retiring the previous one), stamps the lifetime, and signs.
  seal(fields, nowMono) {
    this.challengeHighWater += 1
    this.outstanding = { id: this.challengeHighWater, issuedMono: nowMono }
    this.current = null // the status that authorized this command cannot authorize another
    const issued = this.unixNow()
    const message = {
      schema_version: SCHEMA_VERSION,
      controller_id: this.controllerId,
      ...fields,
      status_challenge: this.outstanding.id,
      issued_at_ms: issued,
      expires_at_ms: issued + COMMAND_LIFETIME_MS,
    }
    message.mac = signCommand(this.key, message)
    return message
  }
}
