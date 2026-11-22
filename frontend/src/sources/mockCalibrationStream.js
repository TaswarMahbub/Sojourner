// A SYNTHETIC calibration stream, so the live envelope can be seen before the real stream on
// ws://127.0.0.1:8766 exists. It plays one 60 s session built backwards from a saved practice
// profile (broad by default), with the protocol's timing from perception/calibration.py:
// 15 s free exploration, six probes of three reps (900 ms cue, 500 ms release) in 30 s,
// 10 s rest baseline, 5 s confirm. Then it pauses and plays the session again.
//
// Events follow docs/sim-decisions.md §2.3 and §2.4:
// - one channel_progress per channel per rep of that channel's probe, provisional;
// - the very first rep has no quiet gap behind it yet, so its basis is prior_default; every later
//   rep is interrep_quiet, with a noise estimate that settles toward the final value;
// - when rest lands, one final_rest event per channel, then the calibration_profile.
// The final values equal the profile's, so the session reconciles.
//
// Every number here is invented except the profile's own. In particular the prior (3 × the final
// noise), the settling curve, and snr = peak_deviation / effective_std for provisional events
// (as in the §2.3 example) are placeholders until Mridhul answers the open questions.

import broad from '../fixtures/calibration-profiles/broad.json'
import { PROBE_NAMES } from '../contracts/calibrationProgress.js'

export const FREE_EXPLORATION_MS = 15000
export const PROBE_MS = 5000
export const REP_MS = 900
export const RELEASE_MS = 500
export const REPS = 3
export const REST_END_MS = FREE_EXPLORATION_MS + PROBE_NAMES.length * PROBE_MS + 10000 // 55 s
export const PROFILE_MS = REST_END_MS + 5000 // 60 s
export const LOOP_PAUSE_MS = 8000

const PRIOR_FACTOR = 3

// The session's events in order, each as { atMs, event }.
export function syntheticCalibrationEvents(profile = broad) {
  const channels = Object.values(profile.enrolment_metrics.channels)
  const out = [{ atMs: 0, event: { record_type: 'session_header', session_id: 'synthetic-stream', source: 'synthetic', measured: false } }]
  let repsSoFar = 0
  PROBE_NAMES.forEach((probe, k) => {
    const probed = channels.filter((c) => c.probe === probe)
    for (let rep = 0; rep < REPS; rep += 1) {
      const atMs = FREE_EXPLORATION_MS + k * PROBE_MS + rep * (REP_MS + RELEASE_MS) + REP_MS
      const prior = repsSoFar === 0
      for (const c of probed) {
        const finalStd = c.rest.effective_std
        const effectiveStd = prior ? finalStd * PRIOR_FACTOR : finalStd * (1 + 1.5 / (repsSoFar + 1))
        const peak = Math.abs(c.usable_range) * (0.8 + 0.1 * rep)
        out.push({
          atMs,
          event: {
            record_type: 'channel_progress', t_ms: atMs, channel: c.channel, unit: c.unit, probe, rep_index: rep,
            peak_deviation: peak, effective_std: effectiveStd, snr: peak / effectiveStd,
            provisional: true, basis: prior ? 'prior_default' : 'interrep_quiet',
          },
        })
      }
      repsSoFar += 1
    }
  })
  for (const c of channels) {
    const std = c.rest.effective_std
    out.push({
      atMs: REST_END_MS,
      event: {
        record_type: 'channel_progress', t_ms: REST_END_MS, channel: c.channel, unit: c.unit, probe: null, rep_index: null,
        peak_deviation: Math.abs(c.usable_range), effective_std: std, snr: Math.abs(c.usable_range) / std,
        provisional: false, basis: 'final_rest',
      },
    })
  }
  out.push({ atMs: PROFILE_MS, event: profile })
  return out
}

// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }.
// speed > 1 plays faster (for a quick look); the event times inside the messages do not change.
export function createMockCalibrationStream({ profile = broad, speed = 1, loopPauseMs = LOOP_PAUSE_MS, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  const events = syntheticCalibrationEvents(profile)
  const listeners = new Set()
  let timer = null
  let index = 0

  // Waits until event `index` is due, sends it, then schedules the next. The first session starts
  // at once; each later one starts loopPauseMs after the previous profile.
  const schedule = (firstLoop) => {
    const gap = index === 0 ? (firstLoop ? 0 : loopPauseMs) : events[index].atMs - events[index - 1].atMs
    timer = setTimer(() => {
      listeners.forEach((listener) => listener(events[index].event))
      index = (index + 1) % events.length
      schedule(false)
    }, gap / speed)
  }

  return {
    kind: 'mock',
    subscribe(listener) {
      listeners.add(listener)
      if (timer === null) {
        index = 0
        schedule(true)
      }
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) {
          clearTimer(timer)
          timer = null
        }
      }
    },
  }
}
