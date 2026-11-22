import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createWebSocketCameraSource, DEFAULT_PUBLISHER_URL } from './websocketCameraSource.js'
import { selectCameraSource } from './selectCameraSource.js'
import { checkMessage } from '../hooks/useCameraStream.js'

// A stand-in for the browser WebSocket that the test drives by hand.
class FakeSocket {
  static instances = []
  constructor(url) {
    this.url = url
    this.closed = false
    FakeSocket.instances.push(this)
  }
  close() { this.closed = true }
  receive(data) { this.onmessage?.({ data }) }
  drop() { this.onerror?.(); this.onclose?.() }
}

const frameText = (overrides = '') =>
  `{"schema_version": "4", "timestamp_ms": 1795276800000, "tracking_state": "lost", "features": {}, "head_pose": null, "confidence": null${overrides}}`

describe('websocket camera source', () => {
  beforeEach(() => {
    FakeSocket.instances = []
    vi.useFakeTimers()
  })
  afterEach(() => vi.useRealTimers())

  it('connects on first subscribe and passes each text message to listeners', () => {
    const source = createWebSocketCameraSource({ WebSocketImpl: FakeSocket })
    expect(source.kind).toBe('live')
    expect(FakeSocket.instances).toHaveLength(0)

    const received = []
    source.subscribe((m) => received.push(m))
    expect(FakeSocket.instances).toHaveLength(1)
    expect(FakeSocket.instances[0].url).toBe(DEFAULT_PUBLISHER_URL)

    FakeSocket.instances[0].receive(frameText())
    expect(received).toEqual([frameText()])
  })

  it('retries when the publisher is not running or stops', () => {
    const source = createWebSocketCameraSource({ WebSocketImpl: FakeSocket, retryMs: 1000 })
    source.subscribe(() => {})
    FakeSocket.instances[0].drop()
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('closes the socket and stops retrying when the last listener leaves', () => {
    const source = createWebSocketCameraSource({ WebSocketImpl: FakeSocket })
    const unsubscribe = source.subscribe(() => {})
    const socket = FakeSocket.instances[0]
    unsubscribe()
    expect(socket.closed).toBe(true)
    socket.drop()
    vi.advanceTimersByTime(5000)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('turns binary messages into something the decoder rejects', () => {
    const source = createWebSocketCameraSource({ WebSocketImpl: FakeSocket })
    const received = []
    source.subscribe((m) => received.push(m))
    FakeSocket.instances[0].receive(new ArrayBuffer(4))
    expect(checkMessage(received[0], 0).validation.valid).toBe(false)
  })
})

describe('checking live text messages', () => {
  it('accepts a valid frame and rejects duplicate keys and 5.0 timestamps', () => {
    expect(checkMessage(frameText(), 0).validation.valid).toBe(true)
    expect(checkMessage(frameText(', "confidence": null'), 0).validation.valid).toBe(false)
    expect(checkMessage(frameText().replace('1795276800000', '1795276800000.0'), 0).validation.valid).toBe(false)
    expect(checkMessage('not json', 0)).toMatchObject({ message: null, validation: { valid: false } })
  })
})

describe('choosing the camera source from the page address', () => {
  it('uses the mock unless ?source=live is given', () => {
    expect(selectCameraSource('').kind).toBe('mock')
    expect(selectCameraSource('?source=other').kind).toBe('mock')
    expect(selectCameraSource('?source=live').kind).toBe('live')
  })

  it('only ever connects to this machine, on the default or a given port', () => {
    expect(selectCameraSource('?source=live').url).toBe('ws://127.0.0.1:8765')
    expect(selectCameraSource('?source=live&port=9000').url).toBe('ws://127.0.0.1:9000')
    expect(selectCameraSource('?source=live&port=evil.example.com').url).toBe('ws://127.0.0.1:8765')
    expect(selectCameraSource('?source=live&port=70000').url).toBe('ws://127.0.0.1:8765')
  })
})
