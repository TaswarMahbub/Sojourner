import { describe, expect, it } from 'vitest'
import { parsePreview } from './previewMessage.js'
import { previewStatus } from './previewSource.js'
import { drawPreview, VERTEX_COLOR } from './drawMesh.js'
import { CONTOURS, LANDMARK_COUNT, TESSELATION } from './faceMeshConnections.js'
import { FACE_PREVIEW_PORT } from './config.js'

const landmarks = Array.from({ length: LANDMARK_COUNT * 2 }, (_, i) => (i % 2 ? 0.25 : 0.5))
const msg = (over = {}) => JSON.stringify({
  type: 'face_preview', schema: 1, source: 'camera', capture_ms: 1795276800000, tracking_state: 'tracking',
  image: { mime: 'image/jpeg', width: 320, height: 240, b64: '' }, landmarks, ...over,
})

describe('face preview message', () => {
  it('accepts a well-formed frame and uses its own port', () => {
    expect(FACE_PREVIEW_PORT).toBe(8768)
    expect(parsePreview(msg())).toMatchObject({ source: 'camera', trackingState: 'tracking' })
  })

  it('refuses anything malformed', () => {
    for (const bad of ['{', msg({ type: 'x' }), msg({ schema: 2 }), msg({ source: 'synthetic' }),
      msg({ tracking_state: 'ok' }), msg({ landmarks: [0.1, 0.2] }), msg({ landmarks: landmarks.map(() => 'x') }),
      msg({ image: { mime: 'image/png', width: 320, height: 240, b64: '' } }), msg({ capture_ms: 1.5 })]) {
      expect(parsePreview(bad)).toBeNull()
    }
  })

  it('never carries a mesh on a lost frame', () => {
    expect(parsePreview(msg({ tracking_state: 'lost' })).landmarks).toBeNull()
  })
})

describe('preview status', () => {
  const frame = parsePreview(msg())
  const base = { link: 'open', frame, receivedAt: 1000, staleMs: 1000 }
  it('labels a real camera LIVE CAMERA and a fixture TEST FIXTURE, never the other way', () => {
    expect(previewStatus({ ...base, now: 1100 })).toMatchObject({ label: 'LIVE CAMERA', live: true, showMesh: true })
    const fixture = parsePreview(msg({ source: 'fixture' }))
    expect(previewStatus({ ...base, frame: fixture, now: 1100 })).toMatchObject({ label: 'TEST FIXTURE', live: false })
  })
  it('clears image and mesh when stale or disconnected', () => {
    for (const s of [{ ...base, now: 2101 }, { ...base, link: 'closed', now: 1100 }, { ...base, frame: null, now: 1100 }]) {
      expect(previewStatus(s)).toMatchObject({ label: 'DISCONNECTED', showImage: false, showMesh: false })
    }
  })
  it('shows the image but no mesh when tracking is lost', () => {
    const lost = parsePreview(msg({ tracking_state: 'lost' }))
    expect(previewStatus({ ...base, frame: lost, now: 1100 })).toMatchObject({ showImage: true, showMesh: false, face: 'face lost' })
  })
})

describe('mesh drawing', () => {
  function recorder() {
    const calls = []
    const ctx = new Proxy({}, {
      get: (t, k) => (k in t ? t[k] : (...a) => calls.push([k, ...a])),
      set: (t, k, v) => { t[k] = v; calls.push(['set', k, v]); return true },
    })
    return { ctx, calls }
  }
  it('maps normalized landmarks onto the same pixels as the image', () => {
    const { ctx, calls } = recorder()
    drawPreview(ctx, 'IMG', landmarks, 320, 240)
    expect(calls[1]).toEqual(['drawImage', 'IMG', 0, 0, 320, 240])
    const lines = calls.filter((c) => c[0] === 'lineTo')
    expect(lines).toHaveLength(TESSELATION.length / 2 + CONTOURS.length / 2)
    expect(lines[0]).toEqual(['lineTo', 160, 60]) // (0.5, 0.25) of a 320 x 240 image
    const dots = calls.filter((c) => c[0] === 'fillRect')
    expect(dots).toHaveLength(LANDMARK_COUNT)
    expect(dots[0]).toEqual(['fillRect', 158.5, 58.5, 3, 3]) // centred on (160, 60)
    expect(calls).toContainEqual(['set', 'fillStyle', VERTEX_COLOR])
  })
  it('draws no mesh at all without landmarks', () => {
    const { ctx, calls } = recorder()
    drawPreview(ctx, 'IMG', null, 320, 240)
    expect(calls.map((c) => c[0])).toEqual(['clearRect', 'drawImage'])
  })
})
