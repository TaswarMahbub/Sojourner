import { useEffect, useState } from 'react'

export const STALE_AFTER_MS = 250

// A source may deliver an object (a mock) or raw JSON text (a websocket). Text goes through the
// strict decoder first, so duplicate keys and 5.0-style integers are rejected.
// `validate(object)` returns { valid, errors }; `decode(text)` returns { valid, errors, message }.
export function checkWith({ validate, decode }, message, receivedAtMs) {
  if (message !== null && typeof message === 'object') {
    return { message, validation: validate(message), receivedAtMs }
  }
  const { message: decoded, ...validation } = decode(message)
  return { message: decoded, validation, receivedAtMs }
}

// Subscribes to any source ({ kind, subscribe }) and checks every message with `checker`.
// `isStale` is true when no message has arrived for staleAfterMs, measured on this
// browser's clock (the producer's clock may be on another machine).
// `checker` must be a module-level constant, so the subscription is not restarted on every render.
export function useMessageStream(source, checker, { staleAfterMs = STALE_AFTER_MS } = {}) {
  const [latest, setLatest] = useState(null)
  const [isStale, setIsStale] = useState(false)

  useEffect(() => {
    let receivedAtMs = null
    const unsubscribe = source.subscribe((message) => {
      receivedAtMs = Date.now()
      setLatest(checkWith(checker, message, receivedAtMs))
      setIsStale(false)
    })
    const timer = setInterval(() => {
      setIsStale(receivedAtMs !== null && Date.now() - receivedAtMs > staleAfterMs)
    }, 50)
    return () => {
      unsubscribe()
      clearInterval(timer)
    }
  }, [source, checker, staleAfterMs])

  return { latest, isStale, sourceKind: source.kind }
}
