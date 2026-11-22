import { useId, useState } from 'react'
import { SourceBadge } from './TrackingStatusPanel.jsx'
import { ANGLE_FIELDS, describeAngle, faceGlyph, summarizePose } from './headPose.js'
import './HeadPosePanel.css'

function unavailableReason(latest, isStale) {
  if (!latest) return 'Waiting for data.'
  if (isStale) return 'No recent data.'
  if (!latest.validation.valid) return 'The latest message was rejected, so no head pose is shown.'
  if (latest.message.head_pose === null) {
    return `No head pose while the face is ${latest.message.tracking_state}. The contract sends head pose only while tracking.`
  }
  return null
}

function FaceDrawing({ pose, mirrored }) {
  const { noseX, noseY, rotateDeg } = faceGlyph(pose, { mirrored })
  return (
    <svg className="face" viewBox="-50 -50 100 100" role="img" aria-label={`Head orientation: ${summarizePose(pose)}`}>
      <circle className="face__outline" r="40" />
      <g transform={`rotate(${rotateDeg})`}>
        <line className="face__eyeline" x1="-26" y1="-10" x2="26" y2="-10" />
        <circle className="face__eye" cx="-14" cy="-10" r="3.5" />
        <circle className="face__eye" cx="14" cy="-10" r="3.5" />
        <circle className="face__nose" cx={noseX} cy={noseY} r="5" />
      </g>
    </svg>
  )
}

function AngleRow({ angle }) {
  return (
    <li className="angle">
      <span className="angle__label">{angle.label}</span>
      <span className="angle__track" aria-hidden="true">
        <span className="angle__zero" />
        <span className="angle__marker" style={{ left: `${angle.position * 100}%` }} />
      </span>
      <span className="angle__value">{angle.text}</span>
      <span className="angle__direction">
        {angle.direction}
        <span className="visually-hidden">, valid range {angle.min}° to {angle.max}°</span>
      </span>
    </li>
  )
}

export default function HeadPosePanel({ latest, isStale, sourceKind }) {
  const headingId = useId()
  // Mirror view by default: a person watching their own head expects it to move like a mirror.
  const [mirrored, setMirrored] = useState(true)
  const reason = unavailableReason(latest, isStale)
  const pose = reason ? null : latest.message.head_pose

  return (
    <section className="panel" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Head pose</h2>
        <SourceBadge kind={sourceKind} />
      </header>

      {pose ? (
        <>
          <FaceDrawing pose={pose} mirrored={mirrored} />
          <ul className="angles">
            {ANGLE_FIELDS.map((field) => (
              <AngleRow key={field} angle={describeAngle(field, pose[field])} />
            ))}
          </ul>
        </>
      ) : (
        <p className="unavailable">{reason}</p>
      )}
      <button type="button" className="button" aria-pressed={mirrored} onClick={() => setMirrored((v) => !v)}>
        Mirror view: {mirrored ? 'on' : 'off'}
      </button>
      <p className="hint">
        {mirrored
          ? 'The drawing moves like a mirror. Left and right in the words are the person’s own.'
          : 'The drawing shows the head as the camera sees it. Left and right in the words are the person’s own.'}
      </p>
    </section>
  )
}
