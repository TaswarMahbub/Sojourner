// Hold to drive, tap to nudge, for the simulator's drive buttons.
//
// - A press held for at least TAP_MS drives until it is released.
// - A press shorter than TAP_MS becomes a nudge: it drives for NUDGE_MS, then stops.
// - A click with no press before it also nudges. VoiceOver's VO-Space sends only a click, so
//   without this a screen-reader user could not drive with the buttons at all.
// Only event handlers call these methods, never render. Each takes set(name, down), the function
// that turns one drive input on or off, so the class holds no reference to the component.

export const TAP_MS = 200
export const NUDGE_MS = 500
// A click this soon after a release belongs to that press, not to an assistive technology.
export const CLICK_AFTER_RELEASE_MS = 400

export class DriveTaps {
  constructor({ now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
    this.now = now
    this.setTimer = setTimer
    this.clearTimer = clearTimer
    this.buttons = {}
  }

  button(name) {
    return (this.buttons[name] ??= { startedAt: null, releasedAt: -Infinity, timer: null })
  }

  cancelNudge(b) {
    this.clearTimer(b.timer)
    b.timer = null
  }

  nudge(name, set) {
    const b = this.button(name)
    this.cancelNudge(b)
    set(name, true)
    b.timer = this.setTimer(() => {
      b.timer = null
      set(name, false)
    }, NUDGE_MS)
  }

  down(name, set) {
    const b = this.button(name)
    this.cancelNudge(b)
    b.startedAt = this.now()
    set(name, true)
  }

  up(name, set) {
    const b = this.button(name)
    if (b.startedAt === null) return
    const heldMs = this.now() - b.startedAt
    b.startedAt = null
    b.releasedAt = this.now()
    if (heldMs < TAP_MS) this.nudge(name, set)
    else set(name, false)
  }

  click(name, set) {
    const b = this.button(name)
    if (b.startedAt === null && this.now() - b.releasedAt > CLICK_AFTER_RELEASE_MS) this.nudge(name, set)
  }

  // Focus left a button mid-hold: stop it. A pending nudge still ends on its own.
  blur(name, set) {
    const b = this.button(name)
    if (b.startedAt === null) return
    b.startedAt = null
    set(name, false)
  }

  // Stop every hold and pending nudge. The caller clears all inputs at once.
  stopAll() {
    for (const b of Object.values(this.buttons)) {
      this.cancelNudge(b)
      b.startedAt = null
    }
  }
}
