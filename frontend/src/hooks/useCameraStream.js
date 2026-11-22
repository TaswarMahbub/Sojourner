import { decodeCameraFrame, validateCameraFrame } from '../contracts/cameraFrame.js'
import { checkWith, useMessageStream } from './useMessageStream.js'

export { STALE_AFTER_MS } from './useMessageStream.js'

const CAMERA_CHECKER = { validate: validateCameraFrame, decode: decodeCameraFrame }

export function checkMessage(message, receivedAtMs) {
  return checkWith(CAMERA_CHECKER, message, receivedAtMs)
}

// Camera feature stream (contract section 1) from any source.
export function useCameraStream(source, options) {
  return useMessageStream(source, CAMERA_CHECKER, options)
}
