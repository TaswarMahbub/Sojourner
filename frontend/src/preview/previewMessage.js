// Strict check of one face_preview message (demo/face_preview.py). Anything else is refused,
// so a malformed or unexpected message can never be drawn as a face.
import { LANDMARK_COUNT } from './faceMeshConnections.js'

const STATES = new Set(['tracking', 'partial', 'lost'])
const SOURCES = new Set(['camera', 'fixture'])
const isInt = (v) => Number.isInteger(v) && v > 0

export function parsePreview(text) {
  let m
  try { m = JSON.parse(text) } catch { return null }
  if (!m || m.type !== 'face_preview' || m.schema !== 1) return null
  if (!SOURCES.has(m.source) || !STATES.has(m.tracking_state) || !Number.isInteger(m.capture_ms)) return null
  const img = m.image
  if (!img || img.mime !== 'image/jpeg' || !isInt(img.width) || !isInt(img.height) || typeof img.b64 !== 'string') return null
  let landmarks = null
  if (m.landmarks !== null) {
    if (!Array.isArray(m.landmarks) || m.landmarks.length !== LANDMARK_COUNT * 2) return null
    if (!m.landmarks.every((v) => typeof v === 'number' && Number.isFinite(v))) return null
    landmarks = m.landmarks
  }
  // A lost frame never carries a mesh, whatever the sender put in it.
  if (m.tracking_state === 'lost') landmarks = null
  return { source: m.source, captureMs: m.capture_ms, trackingState: m.tracking_state, image: img, landmarks }
}
