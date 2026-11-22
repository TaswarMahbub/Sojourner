// A websocket source for any local JSON stream: each text message is passed on as is, as raw
// text, so the hook decodes it with the strict decoder (duplicate keys, 5.0 integers).
// Binary messages are not part of any stream here; listeners get null, which the decoder rejects.
//
// Interface shared by every data source: { kind, subscribe(listener) => unsubscribe }.
// If the server is not running, or stops, this source keeps retrying every retryMs.

export const RETRY_MS = 1000

export function createWebSocketSource({
  url,
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
