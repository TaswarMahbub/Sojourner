import { selectCameraSource } from './sources/selectCameraSource.js'
import { useCameraStream } from './hooks/useCameraStream.js'
import { useGestureStream } from './hooks/useGestureStream.js'
import { createMockGestureSource } from './sources/mockGestureSource.js'
import TrackingStatusPanel from './components/TrackingStatusPanel.jsx'
import HeadPosePanel from './components/HeadPosePanel.jsx'
import GesturePanel from './components/GesturePanel.jsx'
import Simulator3D from './components/Simulator3D.jsx'
import EnvelopePanel from './components/EnvelopePanel.jsx'
import { useCalibrationProfile } from './hooks/useCalibrationProfile.js'
import { createRecordedProfileSource } from './sources/recordedProfileSource.js'

// Created once at module level so React re-renders never create a second source.
// Add ?source=live to the page address to read the perception publisher instead of the mock.
const cameraSource = selectCameraSource()
// No gesture classifier publishes yet (contract section 2), so gestures always come from the mock.
const gestureSource = createMockGestureSource()
// No live calibration stream exists yet, so the envelope reads saved profiles.
const profileSource = createRecordedProfileSource()

export default function App() {
  const camera = useCameraStream(cameraSource)
  const gesture = useGestureStream(gestureSource)
  const envelope = useCalibrationProfile(profileSource)

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
        <EnvelopePanel {...envelope} source={profileSource} />
        <TrackingStatusPanel {...camera} />
        <HeadPosePanel {...camera} />
        <GesturePanel {...gesture} />
      </div>
      <Simulator3D />
    </main>
  )
}
