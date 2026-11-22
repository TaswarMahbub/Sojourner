import { useEffect, useId, useRef, useState } from 'react'
import { Simulation } from '../sim/simulation.js'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'
import { createSceneRoot } from '../view/sceneRoot.js'
import { createFrameLoop } from '../view/frameLoop.js'
import TopDownInset from '../ui/TopDownInset.jsx'
import { KEYS, combine } from '../ui/driveKeys.js'
import './Simulator3D.css'

// Phase 2 grey-box 3D simulator. One corridor simulation, driven through the existing drill path
// (DrillSender → MotorReceiver), exactly as the 2D simulator is. The frame loop banks real elapsed
// time into the simulation's fixed-step clock, then draws the pose the simulation interpolates
// between its last two steps. The view never integrates (invariant I3) and never writes back (I1).

// The frame loop (src/view/frameLoop.js) re-registers and ticks the simulation BEFORE rendering, and
// catches render errors: a render error never stops the simulation (invariant I8). It also
// caps each frame's elapsed time (MAX_FRAME_MS, 250 ms) so a hidden tab cannot dump a long stall into one frame.
// The DOM readout refreshes at this period; the canvas draws every frame.
const READOUT_MS = 33

// Attempts driven from the in-browser drill are not hardware-derived (answers §4).
const PROVENANCE = Object.freeze({
  source: 'synthetic',
  derived_from: 'contracts.md §3',
  generated: new Date().toISOString().slice(0, 10),
  generator: 'src/sim/drillSender.js (in-browser drill, keyboard drive)',
})

const f = (v, digits, width) => (v >= 0 ? '+' : '') + v.toFixed(digits).padStart(width)
const deg = (rad) => (rad * 180) / Math.PI

function readout(view, pose) {
  const rej = view.lastRejection
  const c = view.clearance
  return [
    `task_state    ${view.taskState}`,
    `halted        ${view.haltReason ?? '—'}`,
    `last refused  ${rej ? `${rej.reason}  (${rej.source}, t ${(rej.timeMs / 1000).toFixed(2)} s)` : '—'}`,
    `output        ${f(view.output.linear, 3, 6)} m/s   ${f(view.output.angular, 3, 6)} rad/s`,
    `pose          x ${f(pose.x, 3, 7)} m   y ${f(pose.y, 3, 7)} m   yaw ${f(deg(pose.heading), 1, 6)}°`,
    `clearance     ${c ? `${c.clearance.toFixed(3)} m  (${c.nearestId})` : '—'}`,
    `contact       ${view.contact ?? '—'}`,
    `attempt       ${view.attemptState}${view.attemptResult ? `  (ended by ${view.attemptResult.ended_by})` : ''}`,
    `sim time      ${(view.timeMs / 1000).toFixed(2)} s`,
  ].join('\n')
}

export default function Simulator3D() {
  const headingId = useId()
  const canvasRef = useRef(null)
  const stageRef = useRef(null)
  const [sim] = useState(() => new Simulation({
    world: CORRIDOR_WORLD,
    provenance: PROVENANCE,
    // Structured object for the backend (T8.5 wires the transport). Never shown as on-screen text.
    onAttemptEmit: (result) => console.info('attempt metrics', result),
  }))
  const [snapshot, setSnapshot] = useState(() => ({ view: sim.view(), pose: sim.interpolatedPose() }))
  const [webglError, setWebglError] = useState(null)
  const pressed = useRef(new Set())

  useEffect(() => {
    let root = null
    try {
      root = createSceneRoot({ canvas: canvasRef.current, world: CORRIDOR_WORLD })
    } catch (err) {
      console.warn('3D view unavailable; showing the 2D plan instead.', err)
      setWebglError(String(err?.message ?? err)) // eslint-disable-line react-hooks/set-state-in-effect -- one-time capability probe
    }
    // Dev-only inspection hook (pose the view, read pixels for histograms). Stripped from builds.
    if (import.meta.env.DEV && root) window.__sim3d = { sim, root }
    const stage = stageRef.current
    const observer = root ? new ResizeObserver(() => root.resize(stage.clientWidth, stage.clientHeight)) : null
    if (observer) {
      observer.observe(stage)
      root.resize(stage.clientWidth, stage.clientHeight)
    }

    let lastReadout = -Infinity
    const loop = createFrameLoop({
      sim,
      onFrame: (pose, now) => {
        if (now - lastReadout < READOUT_MS) return
        lastReadout = now
        setSnapshot({ view: sim.view(), pose })
      },
      render: (pose) => { if (root) root.frame(pose) },
    })
    loop.start()
    return () => {
      loop.stop()
      observer?.disconnect()
      root?.dispose()
    }
  }, [sim])

  const setKey = (key, down) => {
    const name = KEYS[key]
    if (!name) return false
    if (down) pressed.current.add(name)
    else pressed.current.delete(name)
    sim.setDrive(combine(pressed.current))
    return true
  }
  const releaseAll = () => {
    pressed.current.clear()
    sim.setDrive(null)
  }

  const { view, pose } = snapshot
  const prepare = () => {
    sim.finishStartup()
    sim.setEstopPressed(false)
    sim.closeCutoff()
    sim.connect()
  }

  return (
    <section className="panel panel--wide sim3d" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>3D corridor simulator (grey box)</h2>
      </header>
      <div
        ref={stageRef}
        className="sim3d__stage"
        tabIndex={0}
        aria-label="3D corridor. Focus here and use the arrow keys or W A S D to drive."
        onKeyDown={(e) => { if (setKey(e.key, true)) e.preventDefault() }}
        onKeyUp={(e) => { if (setKey(e.key, false)) e.preventDefault() }}
        onBlur={releaseAll}
      >
        {webglError
          ? <TopDownInset world={CORRIDOR_WORLD} pose={pose} clearance={view.clearance?.clearance} large />
          : <canvas ref={canvasRef} className="sim3d__canvas" />}
        <pre className="sim3d__readout" aria-live="polite">{readout(view, pose)}</pre>
        {!webglError && (
          <div className="sim3d__inset">
            <TopDownInset world={CORRIDOR_WORLD} pose={pose} clearance={view.clearance?.clearance} />
          </div>
        )}
      </div>
      <div className="button-row sim3d__controls">
        <button type="button" className="button" onClick={prepare}>Prepare rover</button>
        <button type="button" className="button" onClick={() => sim.requestArm()} disabled={!view.senderEnabled}>Arm</button>
        <button type="button" className="button button--danger" onClick={() => sim.softwareEstop()} disabled={!view.senderEnabled}>Software e-stop</button>
        {view.senderEnabled
          ? <button type="button" className="button" onClick={() => { releaseAll(); sim.disconnect() }}>Drop link</button>
          : <button type="button" className="button" onClick={() => sim.connect()}>Restore link</button>}
        <button type="button" className="button" onClick={() => sim.inject('stale')}>Inject stale challenge</button>
        <button type="button" className="button" onClick={() => sim.inject('badmac')}>Inject forged MAC</button>
        <button type="button" className="button" onClick={() => sim.resetAttempt()}>Reset attempt</button>
      </div>
      <p className="hint">
        Drill sender, not hardware. Prepare, then Arm, then focus the view and drive with the arrow keys.
        Drop link stops the datagrams: the receiver&apos;s watchdog halts the rover. {webglError ? `WebGL 2 unavailable (${webglError}); showing the 2D plan.` : ''}
      </p>
    </section>
  )
}
