import { useEffect, useRef, useState } from 'react'
import { CORRIDOR_WORLD } from '../sim/worldModel.js'
import { createSceneRoot } from '../view/sceneRoot.js'
import { createFrameLoop } from '../view/frameLoop.js'
import TopDownInset from '../ui/TopDownInset.jsx'
import { connectMirror } from './liveSource.js'
import { motorBridgeUrl } from './config.js'
import '../components/Simulator3D.css'

// The 3D corridor, driven by the live software-only control run (demo/run_live_sim.py) instead of
// the in-browser drill. Every datagram the laptop publisher sent reaches this page's own
// MotorReceiver (src/sim/motorReceiver.js, unmodified), which judges it. The rover moves only on
// the output that receiver applied. There are no drive controls: this page only observes.
const READOUT_MS = 100
const INITIAL = Object.freeze({ link: 'connecting', error: null, mirror: null })
const f = (v, digits, width) => (v >= 0 ? '+' : '') + v.toFixed(digits).padStart(width)
const deg = (rad) => (rad * 180) / Math.PI

function readout({ link, error, mirror }) {
  if (!mirror) {
    return [
      'SOFTWARE ONLY · mirror of the in-process contract oracle · not hardware',
      `bridge        ${motorBridgeUrl()}  ${link}${error ? `  (${error})` : ''}`,
      'rover         unknown: no mirrored receiver',
      'start it with: python -m demo.run_live_sim --synthetic   (or --probe-report FILE)',
    ].join('\n')
  }
  const view = mirror.view()
  const rej = view.lastRejection
  const last = view.lastDatagram
  const c = view.clearance
  return [
    'SOFTWARE ONLY · mirror of the in-process contract oracle · not hardware',
    `bridge        ${motorBridgeUrl()}  ${link}`,
    `receiver      ${view.armed ? 'armed' : 'disarmed'}   torque ${view.torque ? 'on' : 'off'}   halted ${view.haltReason ?? '—'}`,
    `task_state    ${view.taskState}`,
    `last datagram ${last ? `#${last.sequence} ${last.taskState} → ${last.outcome}` : '—'}`,
    `last refused  ${rej ? `${rej.reason}  (#${rej.sequence ?? '?'}, ${rej.outcome})` : '—'}`,
    `datagrams     ${view.counts.accepted} accepted   ${view.counts.refused} refused`,
    `output        ${f(view.output.linear, 3, 6)} m/s   ${f(view.output.angular, 3, 6)} rad/s`,
    `pose          x ${f(view.pose.x, 3, 7)} m   y ${f(view.pose.y, 3, 7)} m   yaw ${f(deg(view.pose.heading), 1, 6)}°`,
    `clearance     ${c ? `${c.clearance.toFixed(3)} m  (${c.nearestId})` : '—'}`,
    `contact       ${view.contact ?? '—'}`,
    `parity        ${view.agreed}/${view.applied} calls agree with the Python oracle${view.disagreements ? `  (${view.disagreements} DISAGREE)` : ''}`,
  ].join('\n')
}

export default function LiveSimulator3D() {
  const canvasRef = useRef(null)
  const stageRef = useRef(null)
  const latest = useRef(INITIAL)
  const [frame, setFrame] = useState(() => ({ text: readout(INITIAL), pose: CORRIDOR_WORLD.spawn, clearance: undefined }))
  const [webglError, setWebglError] = useState(null)

  useEffect(() => {
    const connection = connectMirror({ url: motorBridgeUrl(), onUpdate: (update) => { latest.current = update } })
    let root = null
    try {
      root = createSceneRoot({ canvas: canvasRef.current, world: CORRIDOR_WORLD })
    } catch (err) {
      setWebglError(String(err?.message ?? err)) // eslint-disable-line react-hooks/set-state-in-effect -- one-time capability probe
    }
    const stage = stageRef.current
    const observer = root ? new ResizeObserver(() => root.resize(stage.clientWidth, stage.clientHeight)) : null
    if (observer) {
      observer.observe(stage)
      root.resize(stage.clientWidth, stage.clientHeight)
    }
    // The pose comes from the mirror, which advances on the oracle's clock as records arrive.
    // The frame loop only draws it (and keeps drawing if a render throws, invariant I8).
    const source = {
      tick() {},
      interpolatedPose: () => latest.current.mirror?.view().pose ?? CORRIDOR_WORLD.spawn,
    }
    let lastReadout = -Infinity
    const loop = createFrameLoop({
      sim: source,
      onFrame: (p, now) => {
        if (now - lastReadout < READOUT_MS) return
        lastReadout = now
        const update = latest.current
        setFrame({ text: readout(update), pose: p, clearance: update.mirror?.view().clearance?.clearance })
      },
      render: (p) => { if (root) root.frame(p) },
    })
    loop.start()
    return () => {
      loop.stop()
      connection.close()
      observer?.disconnect()
      root?.dispose()
    }
  }, [])

  const { text, pose, clearance } = frame
  return (
    <section className="panel panel--wide sim3d" aria-label="Live mirror, software only">
      <header className="panel__header">
        <h2>Live control → 3D corridor (software-only mirror, not hardware)</h2>
      </header>
      <div ref={stageRef} className="sim3d__stage">
        {webglError
          ? <TopDownInset world={CORRIDOR_WORLD} pose={pose} clearance={clearance} large />
          : <canvas ref={canvasRef} className="sim3d__canvas" />}
        <pre className="sim3d__readout" aria-live="polite">{text}</pre>
        {!webglError && (
          <div className="sim3d__inset">
            <TopDownInset world={CORRIDOR_WORLD} pose={pose} clearance={clearance} />
          </div>
        )}
      </div>
      <p className="hint">
        Observer only. Commands come from the laptop control run; this page&apos;s own receiver judges each one.
        {webglError ? ` WebGL 2 unavailable (${webglError}); showing the 2D plan.` : ''}
      </p>
    </section>
  )
}
