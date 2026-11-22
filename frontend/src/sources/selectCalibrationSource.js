import { createRecordedProfileSource } from './recordedProfileSource.js'
import { createMockCalibrationStream } from './mockCalibrationStream.js'
import { createWebSocketSource } from './websocketSource.js'

// The live calibration stream (docs/sim-decisions.md §2). This is the only place the port is set.
export const DEFAULT_CALIBRATION_PORT = 8766

// Picks the envelope's source from the page address:
//   (nothing)                                        saved practice profiles, with a picker (default)
//   ?calibration=mock                                the synthetic stream, one 60 s session on a loop
//   ?calibration=mock&calibration_speed=4            the same, four times faster
//   ?calibration=live                                the calibration stream on ws://127.0.0.1:8766
//   ?calibration=live&calibration_port=9000          the stream on another local port
// Only a port can be chosen, never a host, so the dashboard only ever connects to this machine.
export function selectCalibrationSource(search = globalThis.location?.search ?? '') {
  const params = new URLSearchParams(search)
  const mode = params.get('calibration')
  if (mode === 'mock') {
    const speed = Number(params.get('calibration_speed'))
    return createMockCalibrationStream({ speed: Number.isFinite(speed) && speed >= 1 && speed <= 20 ? speed : 1 })
  }
  if (mode === 'live') {
    const text = params.get('calibration_port') ?? ''
    const port = /^\d{1,5}$/.test(text) ? Number(text) : NaN
    const valid = port >= 1 && port <= 65535
    return createWebSocketSource({ url: `ws://127.0.0.1:${valid ? port : DEFAULT_CALIBRATION_PORT}` })
  }
  return createRecordedProfileSource()
}
