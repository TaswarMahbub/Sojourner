// Where the motor-command bridge listens. The port is proposed, not confirmed
// (docs/sim-decisions.md §3), so it lives here and nowhere else: this default is the
// only place the number may appear in the code.
//
// Override, highest priority first:
//   1. the page URL: ?bridge_port=9000
//   2. the build environment: VITE_MOTOR_BRIDGE_PORT=9000 npm run dev

export const DEFAULT_BRIDGE_PORT = 8767
export const BRIDGE_HOST = '127.0.0.1'

const validPort = (text) => {
  if (typeof text !== 'string' || !/^\d{1,5}$/.test(text)) return null
  const port = Number(text)
  return port >= 1 && port <= 65535 ? port : null
}

export function bridgePort({ search = globalThis.location?.search ?? '', env = import.meta.env ?? {} } = {}) {
  return validPort(new URLSearchParams(search).get('bridge_port') ?? undefined)
    ?? validPort(env.VITE_MOTOR_BRIDGE_PORT)
    ?? DEFAULT_BRIDGE_PORT
}

export const bridgeUrl = (options) => `ws://${BRIDGE_HOST}:${bridgePort(options)}`
