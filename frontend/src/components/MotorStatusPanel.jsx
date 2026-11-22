import { useId } from 'react'
import './SimulatorPanel.css'

// One row per status fact: a word and a symbol, not only a colour.
function Fact({ label, value, tone, symbol }) {
  return (
    <div className={`fact fact--${tone}`}>
      <dt>{label}</dt>
      <dd><span aria-hidden="true">{symbol} </span>{value}</dd>
    </div>
  )
}

const yesNo = (value, { badWhen }) => ({
  value: value ? 'Yes' : 'No',
  tone: value === badWhen ? 'bad' : 'good',
  symbol: value === badWhen ? '✕' : '●',
})

export default function MotorStatusPanel({ view }) {
  const headingId = useId()
  const s = view.status
  const frozen = view.local.firmwareFrozen

  return (
    <section className="panel" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Motor status</h2>
        <span className="source-badge" title="Status from the simulated receiver. A drill status cannot arm the physical rover.">DRILL</span>
      </header>

      {frozen && <p className="status status--warn" role="status"><span aria-hidden="true">!</span> No new status: the firmware is frozen. Showing the last one.</p>}
      {!s ? (
        <p className="unavailable">Waiting for the first status.</p>
      ) : (
        <>
          <dl className="facts-grid">
            <Fact label="Armed" {...yesNo(s.armed, { badWhen: false })} />
            <Fact label="Physical e-stop" {...yesNo(s.physical_estop, { badWhen: true })} />
            <Fact label="Sensor" value={s.sensor_state} tone={s.sensor_state === 'released' ? 'good' : 'bad'} symbol={s.sensor_state === 'released' ? '●' : '✕'} />
            <Fact label="Cutoff" value={s.cutoff_open ? 'Open' : 'Closed'} tone={s.cutoff_open ? 'bad' : 'good'} symbol={s.cutoff_open ? '✕' : '●'} />
            <Fact label="Software e-stop latch" {...yesNo(s.software_estop_latch, { badWhen: true })} />
            <Fact label="Fault latch" {...yesNo(s.fault_latch, { badWhen: true })} />
            <Fact label="Commanding stop" value={s.commanding_stop ? 'Yes' : 'No, moving'} tone="neutral" symbol={s.commanding_stop ? '○' : '●'} />
            <Fact label="Clock" value={s.clock_state} tone="neutral" symbol="○" />
          </dl>
          <dl className="facts">
            <div><dt>Boot</dt><dd>{s.boot_nonce}</dd></div>
            <div><dt>Session</dt><dd>{s.session_id ?? 'none'}</dd></div>
            <div><dt>Last sequence</dt><dd>{s.last_sequence ?? 'none'}</dd></div>
            <div><dt>Status sequence</dt><dd>{s.status_sequence}</dd></div>
            <div><dt>Freshness id</dt><dd>{s.freshness_id}</dd></div>
            <div><dt>Rearm generation</dt><dd>{s.rearm_generation}</dd></div>
          </dl>
        </>
      )}
      <p className="hint">
        Rover-side state the status does not carry: motor power {view.local.busPowered ? 'on' : 'off'}, cutoff hold {view.local.cutoffHold ? 'set' : 'clear'}, firmware {frozen ? 'frozen' : 'running'}.
      </p>
    </section>
  )
}
