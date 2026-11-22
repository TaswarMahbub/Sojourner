// Numbers for the confidence ring around the task_state badge. Every value cites its source.
//
// There is no per-frame perception confidence to read: stock MediaPipe gives none, so camera
// frames carry confidence: null (contracts.md §1), and the contract forbids inventing one.
// The ring is DERIVED from three terms, each 0 to 1 (Mridhul, 22 Nov 2026, ring ruling):
//   tracking   from the frame's tracking_state
//   freshness  from the frame's age against the staleness limit
//   margin     how far the active channel sits past its threshold, relative to its deadband
// Thickness comes from their product; colour from the worst single term (min), because the
// product conflates causes and min names the one that is actually wrong (ruling 3b).
// PLACEHOLDER_CONFIDENCE (tests/session_harness.py) is a gesture stand-in and is never used here.

import { STALE_AFTER_MS } from '../hooks/useMessageStream.js'

// tracking_state → tracking term. SOURCE: Davina's proposal, accepted in the 22 Nov ruling.
export const TRACKING_TERM = { tracking: 1, partial: 0.5, lost: 0 }

// Freshness falls linearly from 1 at age 0 to 0 at the staleness limit, so it reaches 0 exactly
// when the dashboard calls data stale. SOURCE: STALE_AFTER_MS (250 ms, useMessageStream.js, the
// same 250 ms as the contract's watchdog); ruling 3c keeps it reaching 0 there.
export const FRESHNESS_ZERO_MS = STALE_AFTER_MS

// Colour for freshness turns earlier than the watchdog, so the ring is visibly dying before the
// watchdog latches. SOURCE: ruling 3c ("warn around 120 ms and bad around 180 ms").
export const FRESHNESS_WARN_MS = 120
export const FRESHNESS_BAD_MS = 180

// Colour bands for the tracking and margin terms. SOURCE: Davina's proposal (0.66 / 0.33),
// accepted in the 22 Nov ruling.
export const TERM_WARN_BELOW = 0.66
export const TERM_BAD_BELOW = 0.33

// Ring thickness: 1 px at a product of 0, 6 px at 1. Never 0, so a weak signal still looks like a
// working ring, not a broken one; colour carries the bottom end. SOURCE: ruling 3a ("1px to 6px").
export const RING_MIN_PX = 1
export const RING_MAX_PX = 6

// Margin denominator: the active channel's deadband offset from its calibration profile binding
// (bindings[].deadband.offset). SOURCE: perception/calibration.py build_profile(): the deadband
// offset is scoring_constants.deadband_k (3.0) times the channel's effective_std, raised in step
// with the threshold when the threshold was floored (threshold.floored). A margin of 1 means
// "one deadband's width past the threshold".
// Ruling 3d: the denominator lives here, not inline.
export const marginDenominator = (binding) => binding.deadband.offset

// Active-channel hysteresis, for continuous manual mode where no channel is named by the
// receiver: switch only when another channel's margin beats the current one by this much for
// this many consecutive frames. SOURCE: PROPOSED by Davina, 22 Nov; values not yet confirmed.
export const SWITCH_MARGIN = 0.15
export const SWITCH_FRAMES = 5
