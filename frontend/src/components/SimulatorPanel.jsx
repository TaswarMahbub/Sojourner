import { useEffect, useId, useState } from 'react'
import { ARENA_HALF_M } from '../sim/simulation.js'
import { DriveTaps } from './driveTaps.js'
import './SimulatorPanel.css'

// Drive inputs. Each value is canonical (already equal to its six-digit form) and inside the
// contract's ranges: linear [-0.30, 0.30] m/s, angular [-1.00, 1.00] rad/s, positive = left turn.
const SPEED = 0.2
const TURN = 0.6
const DRIVE = {
  forward: { linear: SPEED, angular: 0, label: 'Forward', symbol: '↑' },
  back: { linear: -SPEED, angular: 0, label: 'Back', symbol: '↓' },
  left: { linear: 0, angular: TURN, label: 'Turn left', symbol: '↺' },
  right: { linear: 0, angular: -TURN, label: 'Turn right', symbol: '↻' },
}
const KEYS = { ArrowUp: 'forward', ArrowDown: 'back', ArrowLeft: 'left', ArrowRight: 'right', w: 'forward', s: 'back', a: 'left', d: 'right' }

// Pressed keys -> one drive command, or null.
function combine(pressed) {
  let linear = 0
  let angular = 0
  for (const name of pressed) {
    linear += DRIVE[name].linear
    angular += DRIVE[name].angular
  }
  if (linear === 0 && angular === 0) return null
  return { linear: Math.max(-SPEED, Math.min(SPEED, linear)), angular: Math.max(-TURN, Math.min(TURN, angular)) }
}

function nextStep(view) {
  const s = view.status
  if (view.local.firmwareFrozen) return 'The firmware is frozen. Unfreeze it, then close the cutoff again.'
  if (!view.local.startupFinished) return 'Finish startup on the rover.'
  if (!s || s.sensor_state !== 'released') return 'Release the physical e-stop.'
  if (!view.local.busPowered) return 'Restore motor power.'
  if (s.cutoff_open) return 'Close the cutoff (the explicit restoration on the rover).'
  if (!view.senderEnabled) return 'Connect the drill sender.'
  if (!view.sessionTrusted) return 'Waiting for a status that echoes the session challenge.'
  if (!s.armed) return 'Arm. The drill sends a rearm only after you ask.'
  return 'Hold a drive button to drive, or tap it to nudge for half a second. Or focus the arena and use the arrow keys.'
}

const degrees = (rad) => Math.round((rad * 180) / Math.PI)

function Arena({ view }) {
  const { pose, trail, torque } = view
  const toSvg = (p) => `${(p.x * 100).toFixed(1)},${(-p.y * 100).toFixed(1)}`
  const extent = ARENA_HALF_M * 100
  const grid = []
  for (let v = -extent; v <= extent; v += 50) {
    grid.push(<line key={`v${v}`} x1={v} y1={-extent} x2={v} y2={extent} />, <line key={`h${v}`} x1={-extent} y1={v} x2={extent} y2={v} />)
  }
  const summary = `Rover at x ${pose.x.toFixed(2)} m, y ${pose.y.toFixed(2)} m, heading ${degrees(pose.heading)} degrees. ${torque ? 'Moving.' : 'Stopped.'}`
  return (
    <svg className="arena" viewBox={`${-extent - 4} ${-extent - 4} ${2 * extent + 8} ${2 * extent + 8}`} role="img" aria-label={summary}>
      <rect className="arena__floor" x={-extent} y={-extent} width={2 * extent} height={2 * extent} />
      <g className="arena__grid">{grid}</g>
      <polyline className="arena__trail" points={trail.map(toSvg).join(' ')} />
      <g transform={`translate(${pose.x * 100} ${-pose.y * 100}) rotate(${-degrees(pose.heading)})`} className={torque ? 'rover rover--moving' : 'rover'}>
        <rect x="-15" y="-10" width="30" height="20" rx="3" />
        <path d="M 6 -6 L 15 0 L 6 6 Z" className="rover__nose" />
      </g>
    </svg>
  )
}

export default function SimulatorPanel({ view, sim }) {
  const headingId = useId()
  const [pressed, setPressed] = useState(() => new Set())
  const s = view.status

  const press = (name, down) => {
    setPressed((prev) => {
      const next = new Set(prev)
      if (down) next.add(name)
      else next.delete(name)
      sim.setDrive(combine(next))
      return next
    })
  }

  const onKey = (down) => (event) => {
    const name = KEYS[event.key]
    if (!name) return
    event.preventDefault()
    if (down === pressed.has(name)) return
    press(name, down)
  }

  // Hold to drive, tap to nudge (driveTaps.js). Created once, used from event handlers only.
  const [taps] = useState(() => new DriveTaps())
  useEffect(() => () => taps.stopAll(), [taps])

  const clearAll = () => {
    taps.stopAll()
    setPressed(new Set())
    sim.setDrive(null)
  }

  const holdButton = (name) => ({
    onPointerDown: (e) => {
      e.currentTarget.setPointerCapture?.(e.pointerId)
      taps.down(name, press)
    },
    onPointerUp: () => taps.up(name, press),
    onPointerCancel: () => taps.up(name, press),
    onKeyDown: (e) => (e.key === ' ' || e.key === 'Enter') && !e.repeat && taps.down(name, press),
    onKeyUp: (e) => (e.key === ' ' || e.key === 'Enter') && taps.up(name, press),
    onClick: () => taps.click(name, press),
    onBlur: () => taps.blur(name, press),
    'aria-pressed': pressed.has(name),
  })

  const estopPressed = s ? s.sensor_state !== 'released' : true

  return (
    <section className="panel panel--wide" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>2D rover simulator</h2>
        <span className="source-badge" title="Simulated rover and simulated receiver. Its status cannot arm the physical rover.">DRILL</span>
      </header>
      <p className="hint">
        A simulated rover, viewed from above, with a simulated receiver that runs the contract’s receiver procedure. The drill sender stands in for shared control. Nothing here talks to the physical rover.
      </p>

      <div className="sim">
        <div className="sim__arena" role="group" tabIndex={0} onKeyDown={onKey(true)} onKeyUp={onKey(false)} onBlur={clearAll} aria-label="Arena. Use the arrow keys or W A S D to drive while focused.">
          <Arena view={view} />
          <p className="sim__readout">
            <span>{view.torque ? '● Moving' : '○ Stopped'}</span>
            <span>Output {view.output.linear.toFixed(2)} m/s, {view.output.angular.toFixed(2)} rad/s</span>
          </p>
        </div>

        <div className="sim__controls">
          <p className="sim__next" role="status" aria-live="polite"><strong>Next:</strong> {nextStep(view)}</p>

          <fieldset>
            <legend>Drive (laptop operator)</legend>
            <div className="drive">
              {Object.entries(DRIVE).map(([name, d]) => (
                <button key={name} type="button" className={`button drive__${name}`} {...holdButton(name)}>
                  <span aria-hidden="true">{d.symbol}</span> {d.label}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset>
            <legend>Laptop (drill sender)</legend>
            <div className="button-row">
              {view.senderEnabled
                ? <button type="button" className="button" onClick={() => { clearAll(); sim.disconnect() }}>Disconnect</button>
                : <button type="button" className="button" onClick={() => sim.connect()}>Connect</button>}
              <button type="button" className="button" onClick={() => sim.requestArm()} disabled={!view.senderEnabled} aria-pressed={view.armRequested}>
                Arm
              </button>
              <button type="button" className="button button--danger" onClick={() => sim.softwareEstop()} disabled={!view.senderEnabled}>
                Software e-stop
              </button>
            </div>
          </fieldset>

          <fieldset>
            <legend>Rover (local, physical)</legend>
            <div className="button-row">
              <button type="button" className="button" onClick={() => sim.finishStartup()} disabled={view.local.startupFinished}>Finish startup</button>
              <button type="button" className={`button ${estopPressed ? '' : 'button--danger'}`} onClick={() => sim.setEstopPressed(!estopPressed)} aria-pressed={estopPressed}>
                {estopPressed ? 'Release e-stop' : 'Press e-stop'}
              </button>
              <button type="button" className="button" onClick={() => sim.closeCutoff()} disabled={!s?.cutoff_open}>Close cutoff</button>
              {view.local.busPowered
                ? <button type="button" className="button" onClick={() => sim.cutMotorPower()}>Cut motor power</button>
                : <button type="button" className="button" onClick={() => sim.restoreMotorPower()}>Restore motor power</button>}
              {view.local.firmwareFrozen
                ? <button type="button" className="button" onClick={() => sim.unfreezeFirmware()}>Unfreeze firmware</button>
                : <button type="button" className="button" onClick={() => sim.freezeFirmware()}>Freeze firmware</button>}
              <button type="button" className="button" onClick={() => sim.reboot()}>Reboot (new boot)</button>
            </div>
          </fieldset>

          <fieldset>
            <legend>Inject a hostile datagram</legend>
            <div className="button-row">
              <button type="button" className="button" onClick={() => sim.inject('replay')}>Replay last</button>
              <button type="button" className="button" onClick={() => sim.inject('stale')}>Old challenge</button>
              <button type="button" className="button" onClick={() => sim.inject('generation')}>Wrong generation</button>
              <button type="button" className="button" onClick={() => sim.inject('badmac')}>Forged MAC</button>
              <button type="button" className="button" onClick={() => sim.inject('wrongboot')}>Other boot</button>
              <button type="button" className="button" onClick={() => sim.inject('malformed')}>Truncated</button>
            </div>
          </fieldset>
        </div>
      </div>
    </section>
  )
}
