// MOCK gesture source (docs/contracts.md section 2). No gesture classifier publishes yet, so the
// dashboard is built against this. Its values are generated, not produced by a model.
// The UI labels it as mock via `kind: 'mock'`.
//
// One 10 second cycle shows every case the panel must handle:
//   0.0 to 6.0 s  neutral
//   6.0 to 8.0 s  unknown
//   8.0 to 9.0 s  "nod", a label with no reviewed list entry. It is invalid on purpose: consumers
//                 must reject it and treat it as unknown. It is not a real gesture.
//   9.0 to 10.0 s nothing is sent, so the panel shows its stale state
//
// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }.

import { SCHEMA_VERSION } from '../contracts/cameraFrame.js'

export const MOCK_GESTURE_CYCLE_MS = 10000
export const MOCK_MODEL_VERSION = 'mock-0'
const FRAME_MS = 50
const WINDOW_FRAMES = 5
const CLASSIFY_DELAY_MS = 12

const round = (v) => Math.round(v * 100) / 100

// Pure function: elapsed time in the loop -> one gesture message, or null while silent.
export function buildMockGesture(elapsedMs, timestampMs) {
  const t = ((elapsedMs % MOCK_GESTURE_CYCLE_MS) + MOCK_GESTURE_CYCLE_MS) % MOCK_GESTURE_CYCLE_MS
  if (t >= 9000) return null

  let label = 'neutral'
  let confidence = round(0.75 + 0.2 * Math.sin((2 * Math.PI * t) / 3000))
  if (t >= 8000) {
    label = 'nod'
    confidence = 0.9
  } else if (t >= 6000) {
    label = 'unknown'
    confidence = 0
  }

  const windowEnd = timestampMs - CLASSIFY_DELAY_MS
  return {
    schema_version: SCHEMA_VERSION,
    timestamp_ms: timestampMs,
    source_timestamp_ms: windowEnd,
    window_start_timestamp_ms: windowEnd - (WINDOW_FRAMES - 1) * FRAME_MS,
    window_end_timestamp_ms: windowEnd,
    window_frame_count: WINDOW_FRAMES,
    label,
    confidence,
    model_version: MOCK_MODEL_VERSION,
  }
}

export function createMockGestureSource({ intervalMs = 100, now = () => Date.now() } = {}) {
  const listeners = new Set()
  let timer = null
  let startedAt = 0

  const tick = () => {
    const ts = now()
    const message = buildMockGesture(ts - startedAt, ts)
    if (message) listeners.forEach((listener) => listener(message))
  }

  return {
    kind: 'mock',
    subscribe(listener) {
      listeners.add(listener)
      if (timer === null) {
        startedAt = now()
        timer = setInterval(tick, intervalMs)
      }
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) {
          clearInterval(timer)
          timer = null
        }
      }
    },
  }
}
