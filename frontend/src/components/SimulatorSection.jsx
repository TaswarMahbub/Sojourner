import SimulatorPanel from './SimulatorPanel.jsx'
import MotorStatusPanel from './MotorStatusPanel.jsx'
import CommandHistoryPanel from './CommandHistoryPanel.jsx'
import { useSimulation } from '../hooks/useSimulation.js'

// The simulator re-renders on every tick, so its state lives here rather than in App,
// and the camera and gesture panels are not re-rendered with it.
export default function SimulatorSection() {
  const [view, sim] = useSimulation()
  return (
    <div className="app__grid sim-grid">
      <SimulatorPanel view={view} sim={sim} />
      <MotorStatusPanel view={view} />
      <CommandHistoryPanel view={view} />
    </div>
  )
}
