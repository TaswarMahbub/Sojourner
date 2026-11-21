import { useId, useState } from 'react'
import { CONTROL_EXCLUDED_FEATURES } from '../contracts/cameraFrame.js'
import './TrackingStatusPanel.css'

const TOP_N = 8

// Each status has a word, a symbol and a colour, so colour is never the only signal.
const STATUS = {
  waiting: { label: 'Waiting for data', symbol: '…', tone: 'neutral' },
  stale: { label: 'No recent data', symbol: '!', tone: 'warn' },
  rejected: { label: 'Rejected message', symbol: '✕', tone: 'bad' },
  tracking: { label: 'Tracking', symbol: '●', tone: 'good' },
  partial: { label: 'Partial face', symbol: '◐', tone: 'warn' },
  lost: { label: 'Face lost', symbol: '○', tone: 'bad' },
}

function statusKey(latest, isStale) {
  if (!latest) return 'waiting'
  if (isStale) return 'stale'
  if (!latest.validation.valid) return 'rejected'
  return latest.message.tracking_state
}

export function SourceBadge({ kind }) {
  if (kind !== 'mock') return null
  return (
    <span className="source-badge" title="Synthetic data generated in the browser, not measured from a face">
      MOCK DATA
    </span>
  )
}

export default function TrackingStatusPanel({ latest, isStale, sourceKind }) {
  const [showAll, setShowAll] = useState(false)
  const headingId = useId()
  const listId = useId()

  const key = statusKey(latest, isStale)
  const status = STATUS[key]
  const valid = latest?.validation.valid
  const frame = valid ? latest.message : null

  const features = frame
    ? Object.entries(frame.features)
        .filter(([name]) => !CONTROL_EXCLUDED_FEATURES.includes(name))
        .sort((a, b) => b[1] - a[1])
    : []
  const shown = showAll ? features : features.slice(0, TOP_N)

  return (
    <section className="panel" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Facial tracking</h2>
        <SourceBadge kind={sourceKind} />
      </header>

      <p className={`status status--${status.tone}`} role="status" aria-live="polite">
        <span className="status__symbol" aria-hidden="true">{status.symbol}</span>
        {status.label}
      </p>

      {key === 'rejected' && (
        <div className="rejected">
          <p>This message failed schema 1 validation and is not displayed as data:</p>
          <ul>
            {latest.validation.errors.map((err) => <li key={err}>{err}</li>)}
          </ul>
        </div>
      )}

      {frame && (
        <dl className="facts">
          <div>
            <dt>Face score</dt>
            <dd>{frame.confidence.toFixed(2)}</dd>
          </div>
          <div>
            <dt>Features reported</dt>
            <dd>{Object.keys(frame.features).length} of 52</dd>
          </div>
        </dl>
      )}
      <p className="hint">Face score is the producer’s minimum detector score, not a probability.</p>

      {features.length > 0 && !isStale && (
        <>
          <h3>{showAll ? 'All named features' : `Top ${TOP_N} named features`}</h3>
          <ul className="features" id={listId}>
            {shown.map(([name, value]) => (
              <li key={name} className="feature">
                <span className="feature__name">{name}</span>
                <meter min="0" max="1" value={value} aria-label={name} />
                <span className="feature__value">{value.toFixed(2)}</span>
              </li>
            ))}
          </ul>
          {features.length > TOP_N && (
            <button
              type="button"
              className="button"
              aria-expanded={showAll}
              aria-controls={listId}
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll ? `Show top ${TOP_N}` : `Show all ${features.length}`}
            </button>
          )}
        </>
      )}
    </section>
  )
}
