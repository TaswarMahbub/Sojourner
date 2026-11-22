import { selectCameraSource } from './sources/selectCameraSource.js'
import { useCameraStream } from './hooks/useCameraStream.js'
import TrackingStatusPanel from './components/TrackingStatusPanel.jsx'
import HeadPosePanel from './components/HeadPosePanel.jsx'

// Created once at module level so React re-renders never create a second source.
// Add ?source=live to the page address to read the perception publisher instead of the mock.
const cameraSource = selectCameraSource()

export default function App() {
  const camera = useCameraStream(cameraSource)

  return (
    <main className="app">
      <header className="app__header">
        <div>
          <h1>Sojourner operator dashboard</h1>
          <p>Research prototype: facial-movement control of a four-motor rover.</p>
          <p>
            {cameraSource.kind === 'live'
              ? <>Camera data: perception publisher at <code>{cameraSource.url}</code>. It may be running its camera or its synthetic fake source.</>
              : <>Camera data: in-browser mock. Add <code>?source=live</code> to the address to read the perception publisher.</>}
          </p>
        </div>
      </header>
      <div className="app__grid">
        <TrackingStatusPanel {...camera} />
        <HeadPosePanel {...camera} />
      </div>
    </main>
  )
}
