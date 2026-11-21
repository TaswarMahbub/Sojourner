import { createMockCameraSource } from './data/mockCameraSource.js'
import { useCameraStream } from './hooks/useCameraStream.js'
import TrackingStatusPanel from './components/TrackingStatusPanel.jsx'

// Created once at module level so React re-renders never create a second source.
// To use real data, replace this line with a source that has the same { kind, subscribe }.
const cameraSource = createMockCameraSource()

export default function App() {
  const camera = useCameraStream(cameraSource)

  return (
    <main className="app">
      <header className="app__header">
        <h1>Sojourner operator dashboard</h1>
        <p>Research prototype: facial-movement control of a four-motor rover.</p>
      </header>
      <div className="app__grid">
        <TrackingStatusPanel {...camera} />
      </div>
    </main>
  )
}
