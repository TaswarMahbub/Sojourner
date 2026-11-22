import { useId } from 'react'
import './SimulatorPanel.css'

const SYMBOL = { accepted: '●', stop: '■', rejected: '✕', ignored: '○' }

export default function CommandHistoryPanel({ view }) {
  const headingId = useId()
  return (
    <section className="panel" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Command history</h2>
        <span className="source-badge" title="Datagrams sent to the simulated receiver">DRILL</span>
      </header>
      <p className="hint">Newest first. Repeated identical results are counted on one line. Only accepted motion moves the rover.</p>
      {view.history.length === 0 ? (
        <p className="unavailable">No datagrams yet. Connect the drill sender.</p>
      ) : (
        <ol className="history">
          {view.history.map((e) => (
            <li key={e.id} className={`history__item history__item--${e.kind}`}>
              <span className="history__time">{(e.timeMs / 1000).toFixed(1)} s</span>
              <span className="history__what">
                <code>{e.label}</code>
                {e.count > 1 && <span className="history__count"> ×{e.count}</span>}
                <span className={`history__source history__source--${e.source}`}>{e.source}</span>
                {e.why && <span className="history__why">{e.why}</span>}
              </span>
              <span className="history__outcome">
                <span aria-hidden="true">{SYMBOL[e.kind]} </span>{e.text}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
