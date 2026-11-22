import { useEffect, useState } from 'react'
import { Simulation } from '../sim/simulation.js'

export const TICK_MS = 20
// A hidden browser tab can pause timers. A long pause must not look like a 2-second gap to the
// receiver (that would fire the watchdog), so each real tick advances simulated time by at most this.
const MAX_STEP_MS = 50

// Runs one simulation in real time and re-renders with a fresh snapshot on every tick.
// Returns [view, sim]: the snapshot to draw, and the simulation for the controls to call.
export function useSimulation() {
  const [sim] = useState(() => new Simulation())
  const [view, setView] = useState(() => sim.view())

  useEffect(() => {
    let last = performance.now()
    const timer = setInterval(() => {
      const now = performance.now()
      sim.tick(Math.min(MAX_STEP_MS, now - last))
      last = now
      setView(sim.view())
    }, TICK_MS)
    return () => clearInterval(timer)
  }, [sim])

  return [view, sim]
}
