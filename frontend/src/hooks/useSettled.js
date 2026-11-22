import { useEffect, useState } from 'react'

// The value, once it has stayed the same for settleMs. For screen-reader announcements of a
// status that can flicker: the visible status changes at once, but a live region given the
// settled value speaks once per real change, not every time the camera wavers.
export const SETTLE_MS = 1000

export function useSettled(value, settleMs = SETTLE_MS) {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), settleMs)
    return () => clearTimeout(timer)
  }, [value, settleMs])
  return settled
}
