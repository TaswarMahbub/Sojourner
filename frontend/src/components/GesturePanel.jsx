import { useId } from 'react'
import { SCHEMA_VERSION } from '../contracts/cameraFrame.js'
import { RESERVED_MEANING } from '../contracts/gesture.js'
import { SourceBadge } from './TrackingStatusPanel.jsx'
import './TrackingStatusPanel.css'
import './GesturePanel.css'

// Each state has a word and a symbol as well as a colour, so colour is never the only signal.
const STATUS = {
  waiting: { label: 'Waiting for data', symbol: '…', tone: 'neutral' },
  stale: { label: 'No recent data', symbol: '!', tone: 'warn' },
  rejected: { label: 'Rejected message', symbol: '✕', tone: 'bad' },
  neutral: { label: 'neutral', symbol: '●', tone: 'good' },
  unknown: { label: 'unknown', symbol: '?', tone: 'warn' },
  reviewed: { label: null, symbol: '◆', tone: 'good' },
}

function statusKey(latest, isStale) {
  if (!latest) return 'waiting'
  if (isStale) return 'stale'
  if (!latest.validation.valid) return 'rejected'
  const { label } = latest.message
  return label === 'neutral' || label === 'unknown' ? label : 'reviewed'
}

export default function GesturePanel({ latest, isStale, sourceKind }) {
  const headingId = useId()
  const key = statusKey(latest, isStale)
  const status = STATUS[key]
  // Stale or rejected data is not shown as current.
  const gesture = key === 'waiting' || key === 'stale' || key === 'rejected' ? null : latest.message

  return (
    <section className="panel" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Gesture</h2>
        <SourceBadge kind={sourceKind} />
      </header>

      <p className={`status status--${status.tone}`} role="status" aria-live="polite">
        <span className="status__symbol" aria-hidden="true">{status.symbol}</span>
        <span>
          {gesture ? <>Label: <span className="gesture__label">{gesture.label}</span></> : status.label}
        </span>
      </p>

      {key === 'rejected' && (
        <div className="rejected">
          <p>
            This message failed schema {SCHEMA_VERSION} validation and is treated as <code>unknown</code>.{' '}
            {RESERVED_MEANING.unknown}
          </p>
          <ul>
            {latest.validation.errors.map((err) => <li key={err}>{err}</li>)}
          </ul>
        </div>
      )}

      {gesture && (
        <>
          <p className="gesture__meaning">
            {RESERVED_MEANING[gesture.label] ?? `A reviewed label for model ${gesture.model_version}.`}
          </p>
          <dl className="facts">
            <div>
              <dt>Score</dt>
              <dd>{gesture.confidence.toFixed(2)}</dd>
            </div>
            <div>
              <dt>Model version</dt>
              <dd>{gesture.model_version}</dd>
            </div>
            <div>
              <dt>Window</dt>
              <dd>
                {gesture.window_frame_count} {gesture.window_frame_count === 1 ? 'frame' : 'frames'},{' '}
                {gesture.window_end_timestamp_ms - gesture.window_start_timestamp_ms} ms
              </dd>
            </div>
            <div>
              <dt>Classified after</dt>
              <dd>{gesture.timestamp_ms - gesture.window_end_timestamp_ms} ms</dd>
            </div>
          </dl>
          <meter className="gesture__score" min="0" max="1" value={gesture.confidence} aria-label="Score" />
        </>
      )}

      <p className="hint">
        Score is the classifier’s score for the label, from 0 to 1. It is not a probability, and for{' '}
        <code>unknown</code> it never overrides the label. “Classified after” is the time from the newest
        input frame to the classification, on the producer’s clock.
      </p>
    </section>
  )
}
