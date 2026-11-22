// The simulator's receiver: a line-for-line port of the Python reference receiver,
// tests/motor_receiver.py, which applies docs/contracts.md section 3 ("Receiver procedure").
// The contract says the simulator uses the same procedure as the firmware. A parity test runs
// thousands of randomized steps through both and requires identical results and state.
//
// This is a model of the contract. It is not firmware, it drives no motors, and nothing here
// is a hardware verification. Its status messages are a drill: they cannot arm a physical rover.
//
// Method names follow the Python so the two can be read side by side.

import { SCHEMA_VERSION } from '../contracts/cameraFrame.js'
import {
  SAFETY_STOP_TASKS,
  canonicalVelocity,
  decodeDatagram,
  peekIdentity,
  signStatus,
  validateMotorCommand,
  validateMotorStatus,
  verifyCommand,
} from '../contracts/motor.js'
import { isPlainObject } from '../contracts/cameraFrame.js'

export const WATCHDOG_MS = 250
export const FRESH_MS = 250
export const FUTURE_SKEW_MS = 2000

const isPlainInt = (value) => typeof value === 'number' && Number.isInteger(value)

export class MotorReceiver {
  // key: Uint8Array. One receiver boot; a new boot_nonce requires a new boot.
  constructor(bootNonce, { key, controllerId }) {
    this.key = key
    this.controllerId = controllerId
    this.reset(bootNonce)
  }

  reset(bootNonce) {
    this.bootNonce = bootNonce
    this.monotonicMs = 0
    this.unixMs = null
    this.clockSynchronized = false
    this.freshnessId = 1
    this.challengeIssuedMono = 0
    this.rearmGeneration = 0
    this.statusChallenge = 0
    this.statusSequence = 0
    this.activeSession = null
    this.lastSequence = null
    this.lastAcceptMono = null
    this.motionDeadlineMono = null
    this.motionExpiresUnix = null
    this.lastPetMono = 0
    this.sensorState = 'unknown'
    this.physicalEstop = true
    this.softwareEstopLatch = false
    this.faultLatch = false
    this.armed = false
    this.startupLatch = true
    this.busPowered = true
    this.cutoffOpen = true
    this.cutoffHold = false
    this.driverEnabled = false
    this.firmwareFrozen = false
    this.resumePending = false
    this.linearMps = 0
    this.angularRadS = 0
  }

  // ---- Local inputs and clocks. A datagram cannot call any of these. ----

  setMonotonic(ms) {
    if (!isPlainInt(ms) || ms < this.monotonicMs) return
    this.monotonicMs = ms
    if (this.firmwareFrozen) {
      this.hardwareTick()
      return
    }
    this.enforceTimeouts()
  }

  setUnix(unixMs) {
    if (!isPlainInt(unixMs) || unixMs < 0) return
    this.unixMs = unixMs
  }

  // Stands in for a completed bounded-error clock synchronization.
  noteVerifiedClock(unixMs) {
    if (!isPlainInt(unixMs) || unixMs < 0) return
    this.unixMs = unixMs
    this.clockSynchronized = true
    if (!this.firmwareFrozen) this.enforceTimeouts()
  }

  setSensor(state) {
    if (!['asserted', 'released', 'unknown'].includes(state)) throw new Error(state)
    if (state === this.sensorState) return
    this.sensorState = state
    this.physicalEstop = state !== 'released'
    if (this.physicalEstop) this.cutoffHold = true
    this.armed = false
    this.stopOutput()
    this.bumpGeneration()
    this.rotateChallenge()
    if (!this.firmwareFrozen) this.refreshCutoff()
    else this.cutoffOpen = true
  }

  completeStartup() {
    this.startupLatch = false
    this.armed = false
    this.stopOutput()
    if (!this.firmwareFrozen) this.refreshCutoff()
  }

  loseMotorPower() {
    this.busPowered = false
    this.cutoffOpen = true
    this.driverEnabled = false
    this.armed = false
    this.stopOutput()
  }

  restoreMotorPower() {
    this.busPowered = true
    this.cutoffHold = true
    this.cutoffOpen = true
    this.driverEnabled = false
    this.armed = false
    this.stopOutput()
    this.bumpGeneration()
    this.rotateChallenge()
  }

  // Explicit safe restoration. Does not arm and does not restore PWM. Returns true when closed.
  acknowledgeCutoff() {
    if (this.firmwareFrozen || !this.busPowered || this.startupLatch) return false
    if (this.sensorState !== 'released' || this.physicalEstop) return false
    this.enforceTimeouts()
    this.stopOutput()
    this.cutoffHold = false
    this.pet()
    this.refreshCutoff()
    return this.cutoffOpen === false
  }

  // Power loss of the MCU: a new boot, stopped and disarmed.
  brownoutReset(newNonce) {
    this.reset(newNonce)
  }

  freezeFirmware() {
    this.firmwareFrozen = true
    this.resumePending = true
  }

  unfreezeFirmware() {
    this.firmwareFrozen = false
  }

  poll() {
    if (this.firmwareFrozen) return
    this.enforceTimeouts()
  }

  // ---- Status (contract section 4). Returns null while the firmware is frozen. ----

  status() {
    if (this.firmwareFrozen) return null
    this.statusSequence += 1
    const message = {
      schema_version: SCHEMA_VERSION,
      controller_id: this.controllerId,
      timestamp_ms: this.clockSynchronized && this.unixMs !== null ? this.unixMs : 0,
      boot_nonce: this.bootNonce,
      session_id: this.activeSession,
      last_sequence: this.lastSequence,
      status_sequence: this.statusSequence,
      freshness_id: this.freshnessId,
      rearm_generation: this.rearmGeneration,
      status_challenge: this.statusChallenge,
      clock_state: this.clockSynchronized ? 'synchronized' : 'unsynchronized',
      sensor_state: this.sensorState,
      cutoff_open: this.cutoffOpen,
      physical_estop: this.physicalEstop,
      software_estop_latch: this.softwareEstopLatch,
      fault_latch: this.faultLatch,
      armed: this.armed,
      commanding_stop: this.linearMps === 0 && this.angularRadS === 0,
    }
    message.mac = signStatus(this.key, message)
    const errors = validateMotorStatus(message)
    if (errors.length) throw new Error(errors.join('; '))
    return message
  }

  // ---- Datagrams (contract "Receiver procedure"). Returns the outcome; never throws. ----

  handleRaw(raw) {
    let message
    let identity
    let kind
    try {
      if (this.firmwareFrozen) {
        this.hardwareTick()
        return 'frozen'
      }
      this.enforceTimeouts()
      ;({ message, identity, kind } = this.decode(raw))
    } catch {
      return 'malformed'
    }
    try {
      return this.dispatch(message, identity, kind)
    } catch {
      try {
        if (this.claimsActive(identity) || this.claimsActive(message)) {
          this.stopOutput()
          return 'invalid_stop'
        }
      } catch {
        return 'malformed'
      }
      return 'malformed'
    }
  }

  motorsHaveTorque() {
    if (this.cutoffOpen || !this.busPowered) return false
    if (this.sensorState !== 'released' || this.physicalEstop) return false
    if (!this.driverEnabled || !this.armed) return false
    if (this.linearMps === 0 && this.angularRadS === 0) return false
    if (!this.firmwareFrozen && this.motionDeadlineMono !== null && this.monotonicMs >= this.motionDeadlineMono) return false
    return true
  }

  // Same keys as the Python snapshot(), so the parity test can compare them directly.
  snapshot() {
    return {
      session_id: this.activeSession,
      last_sequence: this.lastSequence,
      last_accept_ms: this.lastAcceptMono,
      physical_estop: this.physicalEstop,
      software_estop_latch: this.softwareEstopLatch,
      fault_latch: this.faultLatch,
      armed: this.armed,
      linear_mps: this.linearMps,
      angular_rad_s: this.angularRadS,
      monotonic_ms: this.monotonicMs,
      unix_ms: this.unixMs,
      clock_synchronized: this.clockSynchronized,
      freshness_id: this.freshnessId,
      challenge_issued_mono: this.challengeIssuedMono,
      rearm_generation: this.rearmGeneration,
      status_challenge: this.statusChallenge,
      motion_deadline_mono: this.motionDeadlineMono,
      last_pet_mono: this.lastPetMono,
      sensor_state: this.sensorState,
      cutoff_open: this.cutoffOpen,
      cutoff_hold: this.cutoffHold,
      driver_enabled: this.driverEnabled,
      bus_powered: this.busPowered,
      startup_latch: this.startupLatch,
      firmware_frozen: this.firmwareFrozen,
      boot_nonce: this.bootNonce,
      controller_id: this.controllerId,
    }
  }

  // ---- Internals, in the order of the Python. ----

  decode(raw) {
    if (typeof raw !== 'string') return { message: null, identity: null, kind: 'malformed' }
    const decoded = decodeDatagram(raw)
    const identity = peekIdentity(raw)
    if (decoded.error) return { message: null, identity, kind: 'malformed' }
    return { message: decoded.value, identity, kind: 'object' }
  }

  dispatch(message, identity, kind) {
    if (kind !== 'object' || !isPlainObject(message)) {
      if (this.claimsActive(identity)) {
        this.stopOutput()
        return 'invalid_stop'
      }
      return 'malformed'
    }
    if (!verifyCommand(this.key, message) || message.controller_id !== this.controllerId) {
      if (this.claimsActive(message)) {
        this.stopOutput()
        return 'invalid_stop'
      }
      return 'ignored'
    }
    if (validateMotorCommand(message).length) {
      if (this.claimsActive(message)) {
        this.stopOutput()
        return 'invalid_stop'
      }
      return 'ignored'
    }
    if (message.boot_nonce !== this.bootNonce) return 'ignored'
    const sessionId = message.session_id
    if (this.activeSession !== null && sessionId < this.activeSession) return 'ignored'
    if (this.activeSession === null || sessionId > this.activeSession) {
      if (
        this.isSessionStart(message) &&
        this.fresh(message) &&
        this.generationOk(message) &&
        !this.unixTooFar(message) &&
        !this.unixExpired(message)
      ) {
        this.activeSession = sessionId
        this.armed = false
        this.stopOutput()
        this.markAccepted(1, message)
        return 'accepted_start'
      }
      return 'ignored'
    }
    if (this.unixTooFar(message)) {
      this.stopOutput()
      return 'invalid_stop'
    }
    const fresh = this.fresh(message)
    const generationOk = this.generationOk(message)
    if (message.sequence <= this.lastSequence) {
      if (this.isSafetyStop(message)) {
        this.applyStop(message)
        return 'stale_stop'
      }
      return 'ignored'
    }
    if (message.stop === false) {
      if (!fresh || !generationOk || this.unixExpired(message) || this.motionBlocked() || this.remainingMotionMs() < 1) {
        this.stopOutput()
        return 'rejected_motion'
      }
      const linear = canonicalVelocity(message, 'linear_mps')
      const angular = canonicalVelocity(message, 'angular_rad_s')
      if (linear === null || angular === null) {
        this.stopOutput()
        return 'invalid_stop'
      }
      this.linearMps = linear
      this.angularRadS = angular
      const lifetime = message.expires_at_ms - message.issued_at_ms
      this.motionDeadlineMono = this.monotonicMs + Math.min(lifetime, this.remainingMotionMs())
      this.motionExpiresUnix = message.expires_at_ms
      this.markAccepted(message.sequence, message)
      return 'accepted_motion'
    }
    if (!fresh || this.unixExpired(message)) {
      if (message.task_state === 'rearm' || !this.isSafetyStop(message)) {
        return message.task_state === 'rearm' ? 'rejected_rearm' : 'invalid_stop'
      }
      this.applyStop(message)
      return 'expired_stop'
    }
    if (message.task_state === 'rearm') {
      if (!generationOk || this.rearmBlocked()) {
        this.stopOutput()
        return 'rejected_rearm'
      }
      this.softwareEstopLatch = false
      this.faultLatch = false
      this.armed = true
      this.stopOutput()
      this.markAccepted(message.sequence, message)
      return 'accepted_rearm'
    }
    this.applyStop(message)
    this.markAccepted(message.sequence, message)
    return 'accepted_stop'
  }

  claimsActive(message) {
    if (!isPlainObject(message) || this.activeSession === null) return false
    return message.boot_nonce === this.bootNonce && message.session_id === this.activeSession
  }

  isSessionStart(m) {
    return m.sequence === 1 && m.task_state === 'idle' && m.stop === true && Number(m.linear_mps) === 0 && Number(m.angular_rad_s) === 0
  }

  isSafetyStop(m) {
    return m.stop === true && SAFETY_STOP_TASKS.includes(m.task_state) && Number(m.linear_mps) === 0 && Number(m.angular_rad_s) === 0
  }

  // Time left before challenge issuance plus 250 ms. May be negative.
  remainingMotionMs() {
    return this.challengeIssuedMono + FRESH_MS - this.monotonicMs
  }

  fresh(m) {
    return m.freshness_id === this.freshnessId && this.monotonicMs - this.challengeIssuedMono <= FRESH_MS
  }

  generationOk(m) {
    return m.rearm_generation === this.rearmGeneration
  }

  unixExpired(m) {
    if (!this.clockSynchronized || this.unixMs === null) return false
    return m.expires_at_ms <= this.unixMs
  }

  unixTooFar(m) {
    if (!this.clockSynchronized || this.unixMs === null) return false
    return m.issued_at_ms > this.unixMs + FUTURE_SKEW_MS
  }

  motionBlocked() {
    return (
      this.physicalEstop ||
      this.sensorState !== 'released' ||
      this.softwareEstopLatch ||
      this.faultLatch ||
      !this.armed ||
      this.cutoffOpen ||
      !this.busPowered ||
      this.startupLatch
    )
  }

  rearmBlocked() {
    return this.physicalEstop || this.sensorState !== 'released' || this.cutoffOpen || !this.busPowered || this.startupLatch
  }

  applyStop(m) {
    this.stopOutput()
    if (m.task_state === 'estop') {
      this.softwareEstopLatch = true
      this.armed = false
    } else if (m.task_state === 'fault') {
      this.faultLatch = true
      this.armed = false
    }
  }

  stopOutput() {
    this.linearMps = 0
    this.angularRadS = 0
    this.motionDeadlineMono = null
    this.motionExpiresUnix = null
  }

  markAccepted(sequence, m) {
    this.lastSequence = sequence
    this.lastAcceptMono = this.monotonicMs
    if (isPlainInt(m.status_challenge)) this.statusChallenge = m.status_challenge
    this.rotateChallenge()
  }

  bumpGeneration() {
    this.rearmGeneration += 1
  }

  rotateChallenge() {
    this.freshnessId += 1
    this.challengeIssuedMono = this.monotonicMs
  }

  pet() {
    this.lastPetMono = this.monotonicMs
  }

  // Hardware cutoff timer. Runs while the firmware is frozen and does not pet.
  hardwareTick() {
    if (this.monotonicMs - this.lastPetMono > WATCHDOG_MS) this.cutoffHold = true
    this.cutoffOpen = this.hardwareCutoffRequired()
    this.driverEnabled = this.busPowered && !this.cutoffOpen
  }

  hardwareCutoffRequired() {
    const petLate = this.monotonicMs - this.lastPetMono > WATCHDOG_MS
    return this.cutoffHold || this.startupLatch || !this.busPowered || this.sensorState !== 'released' || this.physicalEstop || petLate
  }

  refreshCutoff() {
    this.cutoffOpen = this.hardwareCutoffRequired()
    this.driverEnabled = this.busPowered && !this.cutoffOpen
    if (this.cutoffOpen) {
      this.armed = false
      this.stopOutput()
    }
  }

  enforceTimeouts() {
    if (this.resumePending) {
      this.resumePending = false
      if (this.monotonicMs - this.lastPetMono > WATCHDOG_MS) this.cutoffHold = true
    }
    this.pet()
    this.refreshCutoff()
    if (this.motionDeadlineMono !== null && this.monotonicMs >= this.motionDeadlineMono) this.stopOutput()
    if (this.clockSynchronized && this.unixMs !== null && this.motionExpiresUnix !== null && this.unixMs >= this.motionExpiresUnix) {
      this.stopOutput()
    }
    if (this.lastAcceptMono !== null && this.monotonicMs - this.lastAcceptMono > WATCHDOG_MS) {
      this.stopOutput()
      this.faultLatch = true
      this.armed = false
    }
    if (this.monotonicMs - this.challengeIssuedMono > FRESH_MS) this.rotateChallenge()
  }
}
