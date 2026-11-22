import { describe, expect, it } from 'vitest'
import { stepPose } from './stepPose.js'

describe('stepPose', () => {
  it('turns first, then advances along the new heading', () => {
    const p = stepPose({ x: 1, y: 2, heading: 0 }, 0.3, Math.PI / 2, 1)
    expect(p.heading).toBe(Math.PI / 2)
    expect(p.x).toBe(1 + 0.3 * Math.cos(Math.PI / 2))
    expect(p.y).toBe(2 + 0.3 * Math.sin(Math.PI / 2))
  })

  it('does not mutate its input', () => {
    const pose = { x: 0, y: 0, heading: 0.1 }
    stepPose(pose, 0.2, 0.5, 1 / 120)
    expect(pose).toEqual({ x: 0, y: 0, heading: 0.1 })
  })

  it('is the identity at zero velocity', () => {
    expect(stepPose({ x: 3, y: -1, heading: 2 }, 0, 0, 1 / 120)).toEqual({ x: 3, y: -1, heading: 2 })
  })
})
