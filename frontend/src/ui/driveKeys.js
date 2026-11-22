// Keyboard → drill drive intent, the same mapping and values as the 2D simulator panel. Each value
// is canonical and inside the contract's ranges: linear [-0.30, 0.30] m/s, angular [-1.00, 1.00]
// rad/s, positive angular = left turn (REP-103). The DrillSender turns the held intent into signed
// schema 4 commands; the receiver judges each one.

export const SPEED = 0.2
export const TURN = 0.6

export const DRIVE = {
  forward: { linear: SPEED, angular: 0 },
  back: { linear: -SPEED, angular: 0 },
  left: { linear: 0, angular: TURN },
  right: { linear: 0, angular: -TURN },
}

export const KEYS = { ArrowUp: 'forward', ArrowDown: 'back', ArrowLeft: 'left', ArrowRight: 'right', w: 'forward', s: 'back', a: 'left', d: 'right' }

/** Pressed control names → one drive intent, or null when nothing (or a cancelling pair) is held. */
export function combine(pressed) {
  let linear = 0
  let angular = 0
  for (const name of pressed) {
    linear += DRIVE[name].linear
    angular += DRIVE[name].angular
  }
  if (linear === 0 && angular === 0) return null
  return { linear: Math.max(-SPEED, Math.min(SPEED, linear)), angular: Math.max(-TURN, Math.min(TURN, angular)) }
}
