// Live mirror of the software-only control run (demo/run_live_sim.py).
//
// The bridge streams every call made on the Python receiver oracle: clock advances, status reads,
// local inputs, and every datagram as its exact bytes, valid or not. This mirror applies the same
// calls, in the same order, to its own unmodified MotorReceiver (src/sim/motorReceiver.js), which
// is parity-tested against that oracle. This receiver judges every datagram itself. The Python
// result carried in each record is used only to count agreement; it never decides anything here.
//
// The rover moves only while this receiver reports torque, using the velocities it applied
// (invariant I5). A datagram it refused never moves the rover. Time is the oracle's monotonic
// clock from the stream, stepped at the simulator's fixed 1/120 s (I3).

import { MotorReceiver } from '../sim/motorReceiver.js'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'
import { checkPose, sweep } from '../sim/collision.js'
import { stepPose } from '../sim/stepPose.js'
import { MAX_ANGULAR_RAD_S, MAX_LINEAR_MPS, SIM_STEP_S } from '../sim/constants.js'
import { REASONS, haltReason, rejectionReason } from '../sim/stopReason.js'

const STEP_MS = SIM_STEP_S * 1000
const clamp = (v, lim) => Math.max(-lim, Math.min(lim, v))

export function hexToBytes(hex) {
  if (typeof hex !== 'string' || hex.length % 2 !== 0 || /[^0-9a-f]/i.test(hex)) throw new Error('bad key hex')
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i += 1) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16)
  return out
}

// Datagram bytes -> text, as a receiver decodes a UDP payload.
export function decodeDatagram(b64) {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i)
  return new TextDecoder('utf-8').decode(bytes)
}

// Key-order-independent comparison of two JSON values.
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`
  }
  return JSON.stringify(value ?? null)
}

export class LiveMirror {
  constructor(hello, { world = CORRIDOR_WORLD } = {}) {
    if (!hello || hello.type !== 'hello' || hello.software_only !== true) throw new Error('not a software-only mirror hello')
    this.hello = hello
    this.world = world
    this.receiver = new MotorReceiver(hello.boot_nonce, { key: hexToBytes(hello.key_hex), controllerId: hello.controller_id })
    this.pose = { ...world.spawn }
    this.clearance = checkPose(this.pose, world)
    this.contact = null
    this.monoMs = null
    this.carryMs = 0
    this.applied = 0
    this.agreed = 0
    this.disagreements = []
    this.counts = { accepted: 0, refused: 0 }
    this.taskState = 'idle'
    this.faultCause = null
    this.lastRejection = null
    this.lastDatagram = null
    this.nextIndex = 0
  }

  // Apply one record. Records must arrive in order; a gap is refused rather than guessed over.
  apply(record) {
    if (record.type !== 'op') return null
    if (record.n !== this.nextIndex) throw new Error(`mirror out of order: expected ${this.nextIndex}, got ${record.n}`)
    this.nextIndex += 1
    const { op } = record
    const rx = this.receiver
    const faultBefore = rx.faultLatch
    let result = null
    let message
    switch (op.op) {
      case 'mono':
        this.advanceTo(op.ms)
        rx.setMonotonic(op.ms)
        if (!faultBefore && rx.faultLatch) this.faultCause = REASONS.watchdog
        break
      case 'sensor': rx.setSensor(op.state); break
      case 'startup': rx.completeStartup(); break
      case 'ack_cutoff': result = rx.acknowledgeCutoff(); break
      case 'status': result = rx.status(); break
      case 'lose_power': rx.loseMotorPower(); break
      case 'restore_power': rx.restoreMotorPower(); break
      case 'freeze': rx.freezeFirmware(); break
      case 'unfreeze': rx.unfreezeFirmware(); break
      case 'poll': rx.poll(); break
      case 'sync': rx.noteVerifiedClock(op.ms); break
      case 'brownout': rx.brownoutReset(op.nonce); break
      case 'datagram': {
        const raw = decodeDatagram(op.raw_b64)
        result = rx.handleRaw(raw) // this receiver's own verdict
        try { message = JSON.parse(raw) } catch { message = null }
        if (!faultBefore && rx.faultLatch) {
          this.faultCause = message && message.task_state === 'fault' ? REASONS.faultLatch : REASONS.watchdog
        }
        const accepted = typeof result === 'string' && result.startsWith('accepted_')
        if (accepted) { this.counts.accepted += 1; if (message) this.taskState = message.task_state }
        else this.counts.refused += 1
        const reason = rejectionReason(result, message, rx, this.faultCause)
        if (reason) this.lastRejection = { reason, outcome: result, sequence: message?.sequence ?? null, monoMs: this.monoMs }
        this.lastDatagram = { outcome: result, sequence: message?.sequence ?? null, taskState: message?.task_state ?? null }
        break
      }
      default:
        throw new Error(`unknown op ${op.op}`)
    }
    this.applied += 1
    if (canonical(result) === canonical(record.result)) this.agreed += 1
    else this.disagreements.push({ n: record.n, op: op.op, mirror: result, oracle: record.result })
    return result
  }

  // Integrate the rover over (monoMs, ms] in fixed steps, with the output this receiver applied.
  advanceTo(ms) {
    if (this.monoMs === null || ms <= this.monoMs) { this.monoMs = this.monoMs === null ? ms : Math.max(this.monoMs, ms); return }
    this.carryMs += ms - this.monoMs
    this.monoMs = ms
    const rx = this.receiver
    while (this.carryMs >= STEP_MS) {
      this.carryMs -= STEP_MS
      if (!rx.motorsHaveTorque()) continue
      const v = clamp(rx.linearMps, MAX_LINEAR_MPS)
      const w = clamp(rx.angularRadS, MAX_ANGULAR_RAD_S)
      const swept = sweep(this.pose, stepPose(this.pose, v, w, SIM_STEP_S), this.world)
      this.pose = swept.pose
      this.contact = swept.hit ? swept.segmentId : null
      this.clearance = checkPose(this.pose, this.world)
    }
  }

  view() {
    const rx = this.receiver
    return {
      pose: this.pose,
      clearance: this.clearance,
      contact: this.contact,
      torque: rx.motorsHaveTorque(),
      armed: rx.armed,
      output: { linear: rx.linearMps, angular: rx.angularRadS },
      taskState: this.taskState,
      haltReason: haltReason(rx, this.faultCause),
      lastRejection: this.lastRejection,
      lastDatagram: this.lastDatagram,
      counts: { ...this.counts },
      applied: this.applied,
      agreed: this.agreed,
      disagreements: this.disagreements.length,
    }
  }
}
