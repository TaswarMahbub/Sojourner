import { useId, useState } from 'react'
import { ENDED_BY, metricRows, provenanceLabel } from './sessionMetrics.js'
import './SessionMetricsPanel.css'

// Session metrics: the numbers each finished attempt emitted, newest first. DOM only, display
// only (handoff I2, I7). An attempt appears once it has ended, on room entry or reset; a running
// attempt has no numbers yet.

// Keeps every distinct emitted result seen this session, newest first. The simulation emits a
// new frozen object per attempt, so identity tells a new attempt from a re-render.
function useAttempts(result) {
  const [attempts, setAttempts] = useState([])
  if (result && attempts[0] !== result) setAttempts([result, ...attempts])
  return attempts
}

export default function SessionMetricsPanel({ result }) {
  const headingId = useId()
  const attempts = useAttempts(result)
  const [latest, ...earlier] = attempts

  return (
    <section className="panel session-metrics" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Session metrics</h2>
        {latest && <span className="source-badge" title="Where this attempt's motor commands came from">{provenanceLabel(latest)}</span>}
      </header>

      {!latest && <p className="unavailable">No attempt has finished yet. Drive through the doorway into the room, or reset the attempt.</p>}

      {latest && (
        <>
          <p className="session-metrics__ended" role="status">
            Attempt {attempts.length}: {ENDED_BY[latest.ended_by] ?? latest.ended_by}
          </p>
          <dl className="session-metrics__list">
            {metricRows(latest).map((row) => (
              <div key={row.key} className="session-metrics__row">
                <dt>{row.label}</dt>
                <dd>
                  <span className="session-metrics__value">{row.value ?? '—'}</span>
                  {row.value !== null && <span className="session-metrics__unit"> {row.unit}</span>}
                  <span className="session-metrics__note">{row.note}</span>
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}

      {earlier.length > 0 && (
        <details className="session-metrics__earlier">
          <summary>Earlier attempts this session ({earlier.length})</summary>
          <div className="session-metrics__table-wrap" role="region" aria-label="Earlier attempts" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Attempt</th>
                  <th scope="col">Ended</th>
                  <th scope="col">Time to clear (s)</th>
                  <th scope="col">Collisions</th>
                  <th scope="col">Path efficiency (×)</th>
                  <th scope="col">Cues per metre</th>
                </tr>
              </thead>
              <tbody>
                {earlier.map((a, i) => {
                  const [time, collisions, efficiency, cues] = metricRows(a)
                  return (
                    <tr key={`${a.started_at_ms}-${a.ended_at_ms}`}>
                      <th scope="row">{earlier.length - i}</th>
                      <td>{ENDED_BY[a.ended_by] ?? a.ended_by}</td>
                      <td>{time.value ?? '—'}</td>
                      <td>{collisions.value}</td>
                      <td>{efficiency.value ?? '—'}</td>
                      <td>{cues.value ?? '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </section>
  )
}
