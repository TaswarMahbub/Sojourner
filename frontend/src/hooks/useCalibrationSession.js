import { useEffect, useState } from 'react'
import { CalibrationSession, EMPTY_SESSION } from './calibrationSession.js'

// Subscribes to a calibration source (recorded, mock stream or live stream) and returns the
// session snapshot. A new subscription starts a new session, and a snapshot from another source
// is never shown, so nothing carries over between sources. A calibration session has quiet
// stretches, so there is no staleness timer.
export function useCalibrationSession(source) {
  const [state, setState] = useState({ source: null, session: EMPTY_SESSION })

  useEffect(() => {
    const tracker = new CalibrationSession()
    return source.subscribe((message) => setState({ source, session: tracker.receive(message) }))
  }, [source])

  return { session: state.source === source ? state.session : EMPTY_SESSION, sourceKind: source.kind }
}
