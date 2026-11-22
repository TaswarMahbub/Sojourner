// Generates the three synthetic motor-command recordings in src/fixtures/commands/:
//   clean_traversal  down the corridor, turn, through the doorway into the room
//   watchdog_gap     the sender goes silent for 600 ms mid-approach, then resumes
//   estop_latch      a software e-stop mid-approach, then the operator keeps asking to drive
//
// Synthetic, and labelled so inside each file. The datagrams come from the simulator's drill
// sender (drillSender.js) talking to the simulated receiver, so they follow contracts.md §3, but
// they are signed with the drill key and do not come from hardware. Every datagram the drill sends
// is recorded, unmodified. After writing, each file is replayed with no sender running and must
// reproduce the recorded outcomes exactly, or the script fails.
//
// Run from frontend/:  node scripts/record_commands.js
// Output is deterministic: running it twice gives identical files.

import { mkdirSync, writeFileSync } from 'node:fs'
import { DRILL_CONTROLLER, Simulation } from '../src/sim/simulation.js'
import { CORRIDOR_WORLD } from '../src/sim/worldModel.js'
import { RECORDING_FORMAT_VERSION, RECORDING_TYPE, replayRecording, validateRecording } from '../src/net/recording.js'

const OUT_DIR = new URL('../src/fixtures/commands/', import.meta.url)
const BOOT_NONCE = 42
const UNIX_START_MS = 1795276800000 // fixed, so the signed datagrams are identical on every run
const GENERATED = '2026-11-22'
const SETUP = ['startup_complete', 'estop_sensor_released', 'cutoff_acknowledged']
const DOWN = -Math.PI / 2
const round3 = (x) => Math.round(x * 1000) / 1000

const PROVENANCE = {
  source: 'synthetic',
  derived_from: 'contracts.md §3',
  generated: GENERATED,
  generator: 'frontend/scripts/record_commands.js',
  signed_with: 'simulator drill key (not a rover secret)',
}

function recorder() {
  let unix = UNIX_START_MS
  const sim = new Simulation({ bootNonce: BOOT_NONCE, unixNow: () => unix, world: CORRIDOR_WORLD })
  const datagrams = []
  const outcomes = []
  const deliver = sim.deliver.bind(sim)
  sim.deliver = (raw, source, why) => {
    const outcome = deliver(raw, source, why)
    let sequence = null
    try {
      const m = JSON.parse(raw)
      if (Number.isSafeInteger(m?.sequence)) sequence = m.sequence
    } catch { /* recorded as is */ }
    datagrams.push({ sequence, receive_ms: round3(sim.timeMs), raw })
    outcomes.push(outcome)
    return outcome
  }
  const tick = (ms) => {
    unix += ms
    sim.tick(ms)
  }
  const driveUntil = (drive, done, maxMs = 60000) => {
    sim.setDrive(drive)
    for (let t = 0; t < maxMs && !done(sim); t += 100) tick(100)
    sim.setDrive(null)
  }
  // Rover-local setup at time 0, matching SETUP, then connect and arm.
  sim.finishStartup()
  sim.setEstopPressed(false)
  sim.closeCutoff()
  sim.connect()
  tick(300)
  sim.requestArm()
  tick(300)
  return { sim, tick, driveUntil, datagrams, outcomes }
}

function approachAndEnter({ driveUntil, tick }) {
  driveUntil({ linear: 0.05, angular: 0 }, (s) => s.pose.x >= 7.495)
  tick(300)
  driveUntil({ linear: 0, angular: -0.5 }, (s) => s.pose.heading <= DOWN + 0.12)
  driveUntil({ linear: 0, angular: -0.05 }, (s) => s.pose.heading <= DOWN + 0.004)
  tick(300)
  driveUntil({ linear: 0.15, angular: 0 }, (s) => s.attempt.state === 'emitted', 20000)
  tick(500)
}

const SCENARIOS = {
  clean_traversal: {
    description: 'Arm, drive 0.30 m/s down the corridor, creep to the doorway centre, turn right on the spot, drive through into the room.',
    run(r) {
      r.driveUntil({ linear: 0.3, angular: 0 }, (s) => s.pose.x >= 7.3)
      approachAndEnter(r)
    },
  },
  watchdog_gap: {
    description: 'As clean_traversal, but at x = 4 m the sender goes silent for 600 ms, more than the 250 ms watchdog. The rover must stop; the sender then resumes and completes the traversal.',
    run(r) {
      r.driveUntil({ linear: 0.3, angular: 0 }, (s) => s.pose.x >= 4)
      r.sim.disconnect()
      r.tick(600)
      r.sim.connect()
      r.tick(300)
      r.sim.requestArm()
      r.tick(300)
      r.driveUntil({ linear: 0.3, angular: 0 }, (s) => s.pose.x >= 7.3)
      approachAndEnter(r)
    },
  },
  estop_latch: {
    description: 'Drive 0.30 m/s down the corridor; at x = 5 m the sender issues a software e-stop. The operator keeps asking to drive for 3 s. The latch holds and the rover stays stopped.',
    run(r) {
      r.driveUntil({ linear: 0.3, angular: 0 }, (s) => s.pose.x >= 5)
      r.sim.setDrive({ linear: 0.3, angular: 0 })
      r.sim.softwareEstop()
      r.tick(3000)
      r.sim.setDrive(null)
      r.tick(500)
    },
  },
}

mkdirSync(OUT_DIR, { recursive: true })
for (const [scenario, { description, run }] of Object.entries(SCENARIOS)) {
  const r = recorder()
  run(r)
  const recording = {
    record_type: RECORDING_TYPE,
    format_version: RECORDING_FORMAT_VERSION,
    scenario,
    description,
    provenance: PROVENANCE,
    rover: { boot_nonce: BOOT_NONCE, controller_id: DRILL_CONTROLLER, setup: SETUP },
    end_ms: round3(r.sim.timeMs),
    datagrams: r.datagrams,
  }
  const errors = validateRecording(recording)
  if (errors.length) throw new Error(`${scenario}: ${errors.join('; ')}`)

  const replay = replayRecording(JSON.parse(JSON.stringify(recording)))
  const differs = replay.outcomes.findIndex((o, i) => o !== r.outcomes[i])
  if (differs !== -1) throw new Error(`${scenario}: replay outcome ${differs} is ${replay.outcomes[differs]}, recorded ${r.outcomes[differs]}`)
  if (JSON.stringify(replay.sim.pose) !== JSON.stringify(r.sim.pose)) throw new Error(`${scenario}: replay ended at a different pose`)

  writeFileSync(new URL(`${scenario}.json`, OUT_DIR), `${JSON.stringify(recording, null, 1)}\n`)
  const counts = {}
  r.outcomes.forEach((o) => { counts[o] = (counts[o] ?? 0) + 1 })
  console.log(scenario, `${r.datagrams.length} datagrams, ${(r.sim.timeMs / 1000).toFixed(1)} s, pose x=${r.sim.pose.x.toFixed(3)} y=${r.sim.pose.y.toFixed(3)}, attempt ${r.sim.attempt.state}`, counts)
}
