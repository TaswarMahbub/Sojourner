// WebSocket client for the motor mirror bridge. One message is one record, applied in order.
// It fails closed: an unparseable record, a gap or any mirror error drops the mirror, and the
// view then shows the rover as unknown, never as moving. A reconnect starts again from the hello,
// because the bridge replays the whole boot to each new client.

import { LiveMirror } from './liveMirror.js'

export function connectMirror({ url, onUpdate, WebSocketImpl = globalThis.WebSocket, retryMs = 1000 }) {
  let socket = null
  let mirror = null
  let closed = false
  let timer = null
  let state = { link: 'connecting', error: null }

  const emit = () => onUpdate({ mirror, ...state })

  const fail = (error) => {
    mirror = null
    state = { link: 'error', error }
    emit()
    socket?.close()
  }

  const open = () => {
    if (closed) return
    socket = new WebSocketImpl(url)
    socket.onopen = () => { state = { link: 'open', error: null }; emit() }
    socket.onmessage = (event) => {
      if (state.link === 'error') return
      let record
      try { record = JSON.parse(event.data) } catch { return fail('unparseable record') }
      try {
        if (record.type === 'hello') mirror = new LiveMirror(record)
        else if (!mirror) return fail('operation before hello')
        else mirror.apply(record)
      } catch (err) {
        return fail(String(err?.message ?? err))
      }
      emit()
    }
    socket.onclose = () => {
      if (closed) return
      mirror = null
      if (state.link !== 'error') state = { link: 'closed', error: null }
      emit()
      timer = setTimeout(() => { state = { link: 'connecting', error: null }; open() }, retryMs)
    }
  }

  open()
  return {
    close() {
      closed = true
      clearTimeout(timer)
      socket?.close()
    },
  }
}
