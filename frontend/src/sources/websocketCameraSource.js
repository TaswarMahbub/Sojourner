import { RETRY_MS, createWebSocketSource } from './websocketSource.js'

// Live camera source: reads the perception publisher's websocket (perception/publish.py).
// Each websocket message is one camera frame as raw JSON text. The text is passed on as is,
// so the hook decodes it with the strict decoder (duplicate keys, 5.0 integers).
//
// The publisher serves either measured camera frames (--source camera) or synthetic ones
// (--source fake), and the message shape is the same for both. This source cannot tell them
// apart, so the UI must not call its data "measured".
//
// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }; see websocketSource.js.
// If the publisher is not running, or stops, this source keeps retrying every retryMs.
// While it has no connection, no messages arrive and the dashboard shows "No recent data".

export const DEFAULT_PUBLISHER_URL = 'ws://127.0.0.1:8765'

export { RETRY_MS }

export function createWebSocketCameraSource({ url = DEFAULT_PUBLISHER_URL, ...options } = {}) {
  return createWebSocketSource({ url, ...options })
}
