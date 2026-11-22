import { useEffect, useState } from 'react'
import { validateCameraFrame } from '../contracts/cameraFrame.js'

export const STALE_AFTER_MS = 250

// Subscribes to any camera source ({ kind, subscribe }) and validates every message.
// `isStale` is true when no message has arrived for STALE_AFTER_MS, measured on this
// browser's clock (the producer's clock may be on another machine).
export function useCameraStream(source, { staleAfterMs = STALE_AFTER_MS } = {}) {
  const [latest, setLatest] = useState(null)
  const [isStale, setIsStale] = useState(false)

  useEffect(() => {
    let receivedAtMs = null
    const unsubscribe = source.subscribe((message) => {
      receivedAtMs = Date.now()
      setLatest({ message, validation: validateCameraFrame(message), receivedAtMs })
      setIsStale(false)
    })
    const timer = setInterval(() => {
      setIsStale(receivedAtMs !== null && Date.now() - receivedAtMs > staleAfterMs)
    }, 50)
    return () => {
      unsubscribe()
      clearInterval(timer)
    }
  }, [source, staleAfterMs])

  return { latest, isStale, sourceKind: source.kind }
}
