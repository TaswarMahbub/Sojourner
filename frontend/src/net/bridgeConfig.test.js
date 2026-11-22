import { describe, expect, it } from 'vitest'
import { DEFAULT_BRIDGE_PORT, bridgePort, bridgeUrl } from './bridgeConfig.js'

describe('motor bridge port', () => {
  it('uses the default when nothing overrides it', () => {
    expect(bridgePort({ search: '', env: {} })).toBe(DEFAULT_BRIDGE_PORT)
    expect(bridgeUrl({ search: '', env: {} })).toBe(`ws://127.0.0.1:${DEFAULT_BRIDGE_PORT}`)
  })

  it('takes the environment variable over the default', () => {
    expect(bridgePort({ search: '', env: { VITE_MOTOR_BRIDGE_PORT: '9100' } })).toBe(9100)
  })

  it('takes the page URL over the environment variable', () => {
    expect(bridgePort({ search: '?bridge_port=9200', env: { VITE_MOTOR_BRIDGE_PORT: '9100' } })).toBe(9200)
  })

  it.each(['0', '65536', '80a', '-1', '', '1e3'])('ignores an invalid port %j', (bad) => {
    expect(bridgePort({ search: `?bridge_port=${bad}`, env: { VITE_MOTOR_BRIDGE_PORT: bad } })).toBe(DEFAULT_BRIDGE_PORT)
  })
})
