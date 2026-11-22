import { createMockCameraSource } from './mockCameraSource.js'
import { DEFAULT_PUBLISHER_URL, createWebSocketCameraSource } from './websocketCameraSource.js'

// Picks the camera source from the page address:
//   http://localhost:5173/                       the in-browser mock (default)
//   http://localhost:5173/?source=live           the perception publisher on ws://127.0.0.1:8765
//   http://localhost:5173/?source=live&port=9000 the publisher on another local port
// Only a port can be chosen, never a host, so the dashboard only ever connects to this machine.
export function selectCameraSource(search = globalThis.location?.search ?? '') {
  const params = new URLSearchParams(search)
  if (params.get('source') !== 'live') return createMockCameraSource()

  const port = Number(params.get('port'))
  const url = Number.isInteger(port) && port >= 1 && port <= 65535
    ? `ws://127.0.0.1:${port}`
    : DEFAULT_PUBLISHER_URL
  return createWebSocketCameraSource({ url })
}
