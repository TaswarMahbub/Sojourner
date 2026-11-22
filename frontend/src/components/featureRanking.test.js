import { describe, expect, it } from 'vitest'
import { orderFeatures, rankFeatures } from './featureRanking.js'

describe('feature ranking held between re-ranks', () => {
  it('ranks names from highest to lowest and leaves out excluded names', () => {
    expect(rankFeatures({ a: 0.1, b: 0.9, _neutral: 1, c: 0.5 }, ['_neutral'])).toEqual(['b', 'c', 'a'])
  })

  it('keeps the held order while showing the latest values', () => {
    const ranking = ['eyeLookDownRight', 'eyeLookDownLeft']
    // The left score has overtaken the right one since the ranking was taken.
    const latest = { eyeLookDownRight: 0.62, eyeLookDownLeft: 0.64 }
    expect(orderFeatures(latest, ranking)).toEqual([['eyeLookDownRight', 0.62], ['eyeLookDownLeft', 0.64]])
  })

  it('drops names missing from the latest frame and appends new ones by value', () => {
    const ranking = ['a', 'gone', 'b']
    expect(orderFeatures({ a: 0.2, b: 0.1, new1: 0.05, new2: 0.9 }, ranking)).toEqual([
      ['a', 0.2], ['b', 0.1], ['new2', 0.9], ['new1', 0.05],
    ])
  })

  it('sorts by current value before any ranking exists', () => {
    expect(orderFeatures({ a: 0.1, b: 0.9, _neutral: 1 }, [], ['_neutral'])).toEqual([['b', 0.9], ['a', 0.1]])
  })
})
