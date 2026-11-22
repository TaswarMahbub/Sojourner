import { decodeGesture, validateGesture } from '../contracts/gesture.js'
import { checkWith, useMessageStream } from './useMessageStream.js'

const GESTURE_CHECKER = { validate: validateGesture, decode: decodeGesture }

export function checkGestureMessage(message, receivedAtMs) {
  return checkWith(GESTURE_CHECKER, message, receivedAtMs)
}

// Gesture classification stream (contract section 2) from any source.
export function useGestureStream(source, options) {
  return useMessageStream(source, GESTURE_CHECKER, options)
}
