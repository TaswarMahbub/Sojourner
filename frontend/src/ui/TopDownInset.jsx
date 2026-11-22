// Top-down plan (handoff T2.7): wall and rail lines, the doorway gap, the avatar footprint as an
// oriented rectangle, and the live minimum clearance in metres. Drawn from worldModel and the
// simulation's pose only — never from the 3D scene. This is also the whole 2D fallback: when WebGL
// is unavailable, or the hard stop is taken, it is shown full size instead of the 3D view.
//
// Plain SVG, no WebGL, so no post-processing can ever touch it (invariant I7).

import { footprintCorners } from '../sim/collision.js'

const PAD = 0.3
// SVG y grows downward; ROS +Y is left. Plan view from above with +X to the right: svg y = −ros y.
const toSvg = (p) => `${p.x.toFixed(4)},${(-p.y).toFixed(4)}`

export default function TopDownInset({ world, pose, clearance, large = false }) {
  const ys = [world.bounds.wallY, ...world.room.map((p) => p.y)]
  const x0 = world.bounds.x0 - PAD
  const x1 = world.bounds.x1 + PAD
  const yTop = -Math.max(...ys) - PAD
  const yBot = -Math.min(...ys) + PAD
  const corners = footprintCorners(pose, world.footprint)
  const nose = { x: pose.x + Math.cos(pose.heading) * world.footprint.length * 0.5, y: pose.y + Math.sin(pose.heading) * world.footprint.length * 0.5 }
  const clearanceText = Number.isFinite(clearance) ? `${clearance.toFixed(3)} m` : '—'
  const summary = `Plan view. Avatar at x ${pose.x.toFixed(2)} m, y ${pose.y.toFixed(2)} m. Minimum clearance ${clearanceText}.`

  return (
    <figure className={large ? 'plan plan--large' : 'plan'}>
      <svg viewBox={`${x0} ${yTop} ${x1 - x0} ${yBot - yTop}`} role="img" aria-label={summary} preserveAspectRatio="xMidYMid meet">
        <polygon className="plan__room" points={world.room.map(toSvg).join(' ')} />
        <rect className="plan__room" x={world.bounds.x0} y={-world.bounds.wallY} width={world.bounds.x1 - world.bounds.x0} height={2 * world.bounds.wallY} />
        {world.wallFaces.map((s) => <line key={s.id} className="plan__face" x1={s.a.x} y1={-s.a.y} x2={s.b.x} y2={-s.b.y} />)}
        {world.segments.map((s) => (
          <line key={s.id} className={s.kind.startsWith('handrail') ? 'plan__rail' : 'plan__wall'} x1={s.a.x} y1={-s.a.y} x2={s.b.x} y2={-s.b.y} />
        ))}
        <polygon className="plan__footprint" points={corners.map(toSvg).join(' ')} />
        <line className="plan__heading" x1={pose.x} y1={-pose.y} x2={nose.x} y2={-nose.y} />
        <circle className="plan__pivot" cx={pose.x} cy={-pose.y} r={0.04} />
      </svg>
      <figcaption>Min clearance <span className="plan__value">{clearanceText}</span></figcaption>
    </figure>
  )
}
