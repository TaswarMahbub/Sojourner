import { readFileSync } from 'node:fs'
import { gunzipSync } from 'node:zlib'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { connectMirror } from './liveSource.js'
import { MOTOR_BRIDGE_PORT, motorBridgeUrl } from './config.js'

const lines = gunzipSync(readFileSync(new URL('./__fixtures__/live-session.jsonl.gz', import.meta.url)))
  .toString('utf8').trim().split('\n')

class FakeSocket {
  static last = null
  constructor(url) { this.url = url; FakeSocket.last = this; this.closed = false }
  open() { this.onopen?.() }
  send(text) { this.onmessage?.({ data: text }) }
  close() { if (!this.closed) { this.closed = true; this.onclose?.() } }
}

afterEach(() => vi.useRealTimers())

describe('motor mirror client', () => {
  it('uses the configured bridge port', () => {
    expect(MOTOR_BRIDGE_PORT).toBe(8767)
    expect(motorBridgeUrl()).toBe(`ws://127.0.0.1:${MOTOR_BRIDGE_PORT}`)
  })

  it('builds a mirror from the hello and applies every record in order', () => {
    let update = null
    connectMirror({ url: 'ws://x', WebSocketImpl: FakeSocket, onUpdate: (u) => { update = u } })
    FakeSocket.last.open()
    for (const line of lines) FakeSocket.last.send(line)
    expect(update.link).toBe('open')
    expect(update.mirror.view().applied).toBe(lines.length - 1)
    expect(update.mirror.view().disagreements).toBe(0)
  })

  it('drops the mirror on a gap and stays dropped until a new connection replays the boot', () => {
    vi.useFakeTimers()
    let update = null
    const conn = connectMirror({ url: 'ws://x', WebSocketImpl: FakeSocket, onUpdate: (u) => { update = u }, retryMs: 10 })
    const first = FakeSocket.last
    first.open()
    first.send(lines[0])
    first.send(lines[1])
    first.send(lines[3]) // record 1 is missing
    expect(update.mirror).toBeNull()
    expect(update.link).toBe('error')
    vi.advanceTimersByTime(20)
    const second = FakeSocket.last
    expect(second).not.toBe(first)
    second.open()
    second.send(lines[0])
    second.send(lines[1])
    expect(update.mirror.view().applied).toBe(1)
    conn.close()
  })

  it('refuses operations before the hello and unparseable records', () => {
    let update = null
    connectMirror({ url: 'ws://x', WebSocketImpl: FakeSocket, onUpdate: (u) => { update = u } })
    FakeSocket.last.send(lines[1])
    expect(update).toMatchObject({ mirror: null, link: 'error', error: 'operation before hello' })
    connectMirror({ url: 'ws://x', WebSocketImpl: FakeSocket, onUpdate: (u) => { update = u } })
    FakeSocket.last.send('{not json')
    expect(update).toMatchObject({ mirror: null, error: 'unparseable record' })
  })
})
