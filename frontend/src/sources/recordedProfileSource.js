// Recorded calibration profiles (provisional profile schema 1), one at a time.
// Until calibration publishes a live stream, the envelope is built against saved profiles.
// select(name) switches profile, which is how the rescale between profiles is shown.
//
// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }.

import broad from '../fixtures/calibration-profiles/broad.json'
import twoNarrow from '../fixtures/calibration-profiles/two-narrow.json'
import singleEyebrow from '../fixtures/calibration-profiles/single-eyebrow.json'

export const PRACTICE_PROFILES = [
  { name: 'broad', label: 'Broad mobility', profile: broad },
  { name: 'two-narrow', label: 'Two narrow channels', profile: twoNarrow },
  { name: 'single-eyebrow', label: 'One eyebrow', profile: singleEyebrow },
]

export function createRecordedProfileSource(profiles = PRACTICE_PROFILES, initial = profiles[0].name) {
  const listeners = new Set()
  let current = profiles.find((p) => p.name === initial) ?? profiles[0]

  return {
    kind: 'recorded',
    profiles,
    get selected() {
      return current.name
    },
    select(name) {
      const next = profiles.find((p) => p.name === name)
      if (!next || next === current) return
      current = next
      listeners.forEach((listener) => listener(current.profile))
    },
    subscribe(listener) {
      listeners.add(listener)
      listener(current.profile)
      return () => listeners.delete(listener)
    },
  }
}
