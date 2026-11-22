import { useEffect, useRef, useState } from 'react'
import { connectPreview, previewStatus } from './previewSource.js'
import { drawPreview } from './drawMesh.js'
import { PREVIEW_STALE_MS, facePreviewUrl } from './config.js'
import './FacePreviewPip.css'

// Floating camera preview with the Face Landmarker mesh drawn over the user's face.
// The image and landmarks come from the demo's own camera and detector over a local side channel
// (demo/face_preview.py); this component never opens a camera. "Mirror view" flips only the
// picture on screen: head-pose signs and cues are computed elsewhere and are not affected.
const TICK_MS = 250

export default function FacePreviewPip({ corner = 'bottom-right', url = facePreviewUrl() }) {
  const canvasRef = useRef(null)
  const latest = useRef({ link: 'connecting', frame: null, receivedAt: 0, drawn: null })
  const [status, setStatus] = useState(() => previewStatus({ link: 'connecting', frame: null, receivedAt: 0, now: 0, staleMs: PREVIEW_STALE_MS }))
  const [expanded, setExpanded] = useState(true)
  const [mirrored, setMirrored] = useState(true)

  useEffect(() => {
    const state = latest.current
    const clear = () => {
      const canvas = canvasRef.current
      canvas?.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height)
      state.drawn = null
    }
    const draw = (frame) => {
      const img = new Image()
      img.onload = () => {
        if (state.frame !== frame) return // a newer frame arrived meanwhile: skip this one
        const canvas = canvasRef.current
        if (!canvas) return
        canvas.width = frame.image.width
        canvas.height = frame.image.height
        drawPreview(canvas.getContext('2d'), img, frame.landmarks, canvas.width, canvas.height)
        state.drawn = frame
      }
      img.src = `data:${frame.image.mime};base64,${frame.image.b64}`
    }
    const refresh = () => {
      const s = previewStatus({ ...state, now: performance.now(), staleMs: PREVIEW_STALE_MS })
      if (!s.showImage && state.drawn) clear() // stale or disconnected: never leave an old face or mesh up
      setStatus((prev) => (prev.label === s.label && prev.face === s.face && prev.showMesh === s.showMesh ? prev : s))
    }
    const connection = connectPreview({
      url,
      onLink: (link) => { state.link = link; if (link !== 'open') state.frame = null; refresh() },
      onFrame: (frame) => { state.frame = frame; state.receivedAt = performance.now(); draw(frame); refresh() },
    })
    const timer = setInterval(refresh, TICK_MS)
    return () => { clearInterval(timer); connection.close() }
  }, [url])

  const kind = status.label === 'LIVE CAMERA' ? 'face-pip--live' : status.label === 'TEST FIXTURE' ? 'face-pip--fixture' : ''
  return (
    <aside className={`face-pip face-pip--${corner} ${kind}`} aria-label="Camera preview with face mesh" data-status={status.label}>
      <div className="face-pip__bar">
        <span className="face-pip__dot" aria-hidden="true" />
        <span className="face-pip__status" role="status" aria-live="polite">
          {status.label}{status.face ? ` · ${status.face}` : ''}
        </span>
        <button type="button" className="face-pip__button" aria-pressed={mirrored} onClick={() => setMirrored((m) => !m)}
          title="Flip the picture only; does not change head-pose signs or cues">
          Mirror
        </button>
        <button type="button" className="face-pip__button" aria-expanded={expanded} aria-controls="face-pip-view"
          aria-label={expanded ? 'Collapse camera preview' : 'Expand camera preview'} onClick={() => setExpanded((e) => !e)}>
          {expanded ? '–' : '+'}
        </button>
      </div>
      <div id="face-pip-view" className="face-pip__view" hidden={!expanded}>
        <canvas ref={canvasRef} className={`face-pip__canvas${mirrored ? ' face-pip__canvas--mirrored' : ''}`}
          width={320} height={240} role="img"
          aria-label={status.showImage ? `Camera image${status.showMesh ? ' with face mesh' : ', no face mesh'}` : 'No camera image'} />
        {!status.showImage && (
          <div className="face-pip__empty">No live camera preview. Start the demo with a camera (python3 -m demo.run_live_sim --probe-report …).</div>
        )}
      </div>
    </aside>
  )
}
