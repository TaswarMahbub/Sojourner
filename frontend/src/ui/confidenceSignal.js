// The derived signal behind the confidence ring. Pure functions, no React, no three.js.
// See confidenceConstants.js for what each term means and where each number comes from.

import {
  FRESHNESS_BAD_MS, FRESHNESS_WARN_MS, FRESHNESS_ZERO_MS, RING_MAX_PX, RING_MIN_PX, SWITCH_FRAMES, SWITCH_MARGIN,
  TERM_BAD_BELOW, TERM_WARN_BELOW, TRACKING_TERM, marginDenominator,
} from './confidenceConstants.js'

const clamp01 = (v) => Math.max(0, Math.min(1, v))
const HEAD_POSE_CHANNELS = ['yaw_deg', 'pitch_deg', 'roll_deg']

// The channel's current value in a camera frame, or null if the frame does not carry it.
export function channelValue(frame, channel) {
  if (HEAD_POSE_CHANNELS.includes(channel)) return frame.head_pose?.[channel] ?? null
  const v = frame.features?.[channel]
  return typeof v === 'number' ? v : null
}

// How far one bound channel sits past its threshold, in deadband widths, 0 to 1.
// Its deviation from rest is taken in the binding's direction. null when it cannot be measured.
export function channelMargin(frame, binding) {
  const x = channelValue(frame, binding.channel)
  const denominator = marginDenominator(binding)
  if (x === null || !(denominator > 0)) return null
  const deviation = binding.direction * (x - binding.rest_mean)
  return clamp01((deviation - binding.threshold.offset) / denominator)
}

// The margins of every bound channel in this frame: { channel: margin }.
export function marginsFor(frame, profile) {
  const out = {}
  for (const b of profile?.bindings ?? []) {
    const m = channelMargin(frame, b)
    if (m !== null) out[b.channel] = m
  }
  return out
}

// Active channel with hysteresis (ruling: max margin, but switch only when another channel beats
// the current one by SWITCH_MARGIN for SWITCH_FRAMES consecutive frames, so the ring does not
// flicker between channels). Pure: takes the previous state, returns the next.
export const NO_PICK = Object.freeze({ channel: null, challenger: null, count: 0 })

export function pickActive(state, margins) {
  const entries = Object.entries(margins)
  if (entries.length === 0) return NO_PICK
  const [best, bestMargin] = entries.reduce((a, b) => (b[1] > a[1] ? b : a))
  if (state.channel === null || !(state.channel in margins)) return { channel: best, challenger: null, count: 0 }
  if (best === state.channel || bestMargin < margins[state.channel] + SWITCH_MARGIN) return { ...state, challenger: null, count: 0 }
  const count = state.challenger === best ? state.count + 1 : 1
  if (count >= SWITCH_FRAMES) return { channel: best, challenger: null, count: 0 }
  return { channel: state.channel, challenger: best, count }
}

const toneOfTerm = (v) => (v < TERM_BAD_BELOW ? 'bad' : v < TERM_WARN_BELOW ? 'warn' : 'good')
const toneOfAge = (ageMs) => (ageMs >= FRESHNESS_BAD_MS ? 'bad' : ageMs >= FRESHNESS_WARN_MS ? 'warn' : 'good')
const TONE_RANK = { good: 0, warn: 1, bad: 2 }

// The ring's state. Inputs:
//   latest         the camera stream's latest checked message ({ message, validation, receivedAtMs }), or null
//   nowMs          the browser clock, on the same clock as receivedAtMs
//   margin         the active channel's margin, or null when no command is being applied
// Returns { value, thicknessPx, tone, worst, terms }. value is null (floor thickness, neutral
// colour) when there is no camera frame at all: there is nothing to derive a confidence from.
export function confidenceSignal({ latest, nowMs, margin }) {
  if (!latest) return { value: null, thicknessPx: RING_MIN_PX, tone: 'neutral', worst: null, terms: null }
  const frame = latest.validation.valid ? latest.message : null
  const ageMs = Math.max(0, nowMs - latest.receivedAtMs)
  const terms = {
    tracking: frame ? (TRACKING_TERM[frame.tracking_state] ?? 0) : 0,
    freshness: clamp01(1 - ageMs / FRESHNESS_ZERO_MS),
  }
  const tones = { tracking: frame ? toneOfTerm(terms.tracking) : 'bad', freshness: toneOfAge(ageMs) }
  if (margin !== null) {
    terms.margin = margin
    tones.margin = toneOfTerm(margin)
  }
  const value = Object.values(terms).reduce((a, b) => a * b, 1)
  // Colour is the worst single term; on a tie, the first named (tracking, freshness, margin).
  const worst = Object.keys(tones).reduce((a, b) => (TONE_RANK[tones[b]] > TONE_RANK[tones[a]] ? b : a))
  return {
    value,
    thicknessPx: RING_MIN_PX + (RING_MAX_PX - RING_MIN_PX) * value,
    tone: tones[worst],
    worst: tones[worst] === 'good' ? null : worst,
    terms: { ...terms, ageMs, rejected: !frame },
  }
}

// The ring's state in words, for screen readers and the tooltip.
export function describeSignal(signal) {
  if (signal.value === null) return 'Signal confidence: no camera data'
  const t = signal.terms
  const reason = {
    tracking: t.rejected ? 'camera message rejected' : t.tracking === 0 ? 'face lost' : 'partial face',
    freshness: `frame ${Math.round(t.ageMs)} ms old`,
    margin: 'active movement barely past its threshold',
  }[signal.worst]
  return `Signal confidence: ${{ good: 'good', warn: 'weak', bad: 'poor' }[signal.tone]}${reason ? `, ${reason}` : ''}`
}
