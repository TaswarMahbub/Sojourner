import { describe, expect, it } from 'vitest'
import { CLICK_AFTER_RELEASE_MS, DriveTaps, NUDGE_MS, TAP_MS } from './driveTaps.js'

// A fake clock and a single pending timer, both driven by hand.
function setup() {
  let t = 0
  let pending = null
  const taps = new DriveTaps({
    now: () => t,
    setTimer: (fn, ms) => (pending = { fn, ms }),
    clearTimer: (timer) => { if (pending === timer) pending = null },
  })
  const log = []
  const set = (name, down) => log.push(`${name} ${down ? 'on' : 'off'}`)
  return {
    taps, set, log,
    advance: (ms) => { t += ms },
    fire: () => { const p = pending; pending = null; p.fn(); return p.ms },
    get pending() { return pending },
  }
}

describe('DriveTaps', () => {
  it('a hold drives until it is released', () => {
    const h = setup()
    h.taps.down('forward', h.set)
    h.advance(TAP_MS + 300)
    h.taps.up('forward', h.set)
    expect(h.log).toEqual(['forward on', 'forward off'])
    expect(h.pending).toBeNull()
  })

  it('a short tap becomes a nudge that stops by itself', () => {
    const h = setup()
    h.taps.down('left', h.set)
    h.advance(50)
    h.taps.up('left', h.set)
    expect(h.log).toEqual(['left on', 'left on'])
    expect(h.fire()).toBe(NUDGE_MS)
    expect(h.log.at(-1)).toBe('left off')
  })

  it('the click that follows a tap does not nudge a second time', () => {
    const h = setup()
    h.taps.down('left', h.set)
    h.advance(50)
    h.taps.up('left', h.set)
    h.taps.click('left', h.set)
    expect(h.log).toEqual(['left on', 'left on'])
  })

  it('a click with no press before it (VoiceOver) nudges', () => {
    const h = setup()
    h.advance(CLICK_AFTER_RELEASE_MS + 1)
    h.taps.click('back', h.set)
    expect(h.log).toEqual(['back on'])
    h.fire()
    expect(h.log).toEqual(['back on', 'back off'])
  })

  it('a hold that starts during a nudge cancels the nudge timer', () => {
    const h = setup()
    h.taps.click('forward', h.set)
    h.taps.down('forward', h.set)
    expect(h.pending).toBeNull()
    h.advance(1000)
    h.taps.up('forward', h.set)
    expect(h.log.at(-1)).toBe('forward off')
  })

  it('losing focus mid-hold stops the drive; stopAll cancels a pending nudge', () => {
    const h = setup()
    h.taps.down('right', h.set)
    h.taps.blur('right', h.set)
    expect(h.log).toEqual(['right on', 'right off'])
    h.taps.click('right', h.set)
    h.taps.stopAll()
    expect(h.pending).toBeNull()
  })
})
