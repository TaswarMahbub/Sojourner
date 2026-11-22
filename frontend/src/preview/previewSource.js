// WebSocket client for the face preview. Keeps only the newest valid frame; reconnects.
import { parsePreview } from './previewMessage.js'

// The preview server is optional, so reconnects back off (1.5 s doubling to 10 s) to keep an
// absent server from filling the console.
export function connectPreview({ url, onFrame, onLink, WebSocketImpl = globalThis.WebSocket, retryMs = 1500, maxRetryMs = 10000 }) {
  let socket = null
  let closed = false
  let timer = null
  let delay = retryMs
  const retry = () => { timer = setTimeout(open, delay); delay = Math.min(maxRetryMs, delay * 2) }
  const open = () => {
    if (closed) return
    try { socket = new WebSocketImpl(url) } catch { onLink('closed'); retry(); return }
    socket.onopen = () => { delay = retryMs; onLink('open') }
    socket.onmessage = (event) => {
      const frame = parsePreview(event.data)
      if (frame) onFrame(frame)
    }
    socket.onerror = () => {}
    socket.onclose = () => {
      if (closed) return
      onLink('closed')
      retry()
    }
  }
  open()
  return { close() { closed = true; clearTimeout(timer); socket?.close() } }
}

// What the PiP shows, from the link, the newest frame and its age. Pure, so it is testable.
export function previewStatus({ link, frame, receivedAt, now, staleMs }) {
  if (link !== 'open' || !frame || now - receivedAt > staleMs) return { label: 'DISCONNECTED', live: false, showImage: false, showMesh: false }
  const kind = frame.source === 'camera' ? 'LIVE CAMERA' : 'TEST FIXTURE'
  const face = { tracking: 'face tracked', partial: 'face partial', lost: 'face lost' }[frame.trackingState]
  return { label: kind, face, live: frame.source === 'camera', showImage: true, showMesh: frame.landmarks !== null }
}
