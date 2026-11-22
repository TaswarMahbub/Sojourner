import { useEffect, useState } from 'react'
import { NO_PICK, confidenceSignal, describeSignal, marginsFor, pickActive } from './confidenceSignal.js'
import './ConfidenceRing.css'

// A ring around the task_state badge (its children): thickness from the derived signal's product,
// colour from its worst term. DOM only (I7), outside the post chain. See confidenceConstants.js.
//
// Props:
//   camera         the camera stream hook's result: { latest } (receivedAtMs on the browser clock)
//   profile        the validated calibration profile, or null (no margin term without one)
//   commandActive  true while an accepted command is moving the rover; in hold, idle or any stop
//                  there is no active channel, so the margin term is left out
//   children       the badge

const NOW_PERIOD_MS = 50 // how often the frame's age is re-measured, so the ring thins while frames stop

function useNow(periodMs) {
  const [now, setNow] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), periodMs)
    return () => clearInterval(timer)
  }, [periodMs])
  return now
}

// The active channel, updated once per camera frame (not per render), with hysteresis.
function useActiveChannel(latest, profile) {
  const [state, setState] = useState({ latest: null, pick: NO_PICK })
  if (state.latest !== latest) {
    const frame = latest?.validation.valid ? latest.message : null
    setState({ latest, pick: frame ? pickActive(state.pick, marginsFor(frame, profile)) : state.pick })
  }
  return state.pick.channel
}

export default function ConfidenceRing({ camera, profile, commandActive, children }) {
  const now = useNow(NOW_PERIOD_MS)
  const latest = camera?.latest ?? null
  const channel = useActiveChannel(latest, profile)
  const frame = latest?.validation.valid ? latest.message : null
  const margin = commandActive && channel && frame ? (marginsFor(frame, profile)[channel] ?? null) : null
  const signal = confidenceSignal({ latest, nowMs: now || latest?.receivedAtMs || 0, margin })
  const text = describeSignal(signal)

  return (
    <span
      className={`confidence-ring confidence-ring--${signal.tone}`}
      style={{ '--ring-width': `${signal.thicknessPx.toFixed(1)}px` }}
      title={channel && margin !== null ? `${text} (active: ${channel})` : text}
      data-thickness={signal.thicknessPx.toFixed(1)}
    >
      {children}
      <span className="visually-hidden">{text}</span>
    </span>
  )
}
