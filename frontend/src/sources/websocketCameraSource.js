// Live camera source: reads the perception publisher's websocket (perception/publish.py).
// Each websocket message is one camera frame as raw JSON text. The text is passed on as is,
// so the hook decodes it with the strict decoder (duplicate keys, 5.0 integers).
//
// The publisher serves either measured camera frames (--source camera) or synthetic ones
// (--source fake), and the message shape is the same for both. This source cannot tell them
// apart, so the UI must not call its data "measured".
//
// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }.
// If the publisher is not running, or stops, this source keeps retrying every retryMs.
// While it has no connection, no messages arrive and the dashboard shows "No recent data".

export const DEFAULT_PUBLISHER_URL = 'ws://127.0.0.1:8765'
export const RETRY_MS = 1000

export function createWebSocketCameraSource({
  url = DEFAULT_PUBLISHER_URL,
  retryMs = RETRY_MS,
  WebSocketImpl = globalThis.WebSocket,
} = {}) {
  const listeners = new Set()
  let socket = null
  let retryTimer = null

  const connect = () => {
    retryTimer = null
    socket = new WebSocketImpl(url)
    socket.onmessage = (event) => {
      // Binary messages are not part of the contract; pass a marker the decoder rejects.
      const text = typeof event.data === 'string' ? event.data : null
      listeners.forEach((listener) => listener(text))
    }
    socket.onclose = () => {
      socket = null
      if (listeners.size > 0) retryTimer = setTimeout(connect, retryMs)
    }
    // An error is always followed by close, which schedules the retry.
    socket.onerror = () => {}
  }

  const disconnect = () => {
    clearTimeout(retryTimer)
    retryTimer = null
    if (socket) {
      socket.onclose = null
      socket.onmessage = null
      socket.close()
      socket = null
    }
  }

  return {
    kind: 'live',
    url,
    subscribe(listener) {
      listeners.add(listener)
      if (!socket && retryTimer === null) connect()
      return () => {
        listeners.delete(listener)
        if (listeners.size === 0) disconnect()
      }
    },
  }
}
