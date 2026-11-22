import { useEffect, useId, useState } from 'react'
import {
  REASONS, angleFor, ease, formatInUnit, lerp, ringsFor, scaleFor, shapePoints, spokesFrom, toPath, unboundFrom,
} from './envelope.js'
import './EnvelopePanel.css'

const RADIUS = 100
const RESCALE_MS = 900
const ROLE_LABEL = {
  proportional_primary: 'proportional, primary',
  proportional_secondary: 'proportional, secondary',
  discrete: 'discrete switch',
  scan_switch: 'scanning switch',
}

const prefersReducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

// The rescale: when the profile changes, the scale slides from the old maximum to the new one
// while the old shape fades out and the new one fades in. Instant with reduced motion.
function useRescale(profile) {
  const spokes = spokesFrom(profile)
  const target = { spokes, scale: scaleFor(spokes) }
  const [anim, setAnim] = useState({ profile, from: null, progress: 1 })

  if (anim.profile !== profile) {
    const shownScale = anim.from ? lerp(anim.from.scale, scaleFor(spokesFrom(anim.profile)), ease(anim.progress)) : scaleFor(spokesFrom(anim.profile))
    setAnim({ profile, from: { spokes: spokesFrom(anim.profile), scale: shownScale }, progress: 0 })
  }

  useEffect(() => {
    if (!anim.from || anim.progress >= 1) return undefined
    if (prefersReducedMotion()) {
      const id = requestAnimationFrame(() => setAnim((a) => ({ ...a, progress: 1 })))
      return () => cancelAnimationFrame(id)
    }
    let start = null
    let id = requestAnimationFrame(function frame(now) {
      start ??= now
      const progress = Math.min(1, (now - start) / RESCALE_MS)
      setAnim((a) => ({ ...a, progress }))
      if (progress < 1) id = requestAnimationFrame(frame)
    })
    return () => cancelAnimationFrame(id)
    // Restart only when a new transition begins.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anim.profile])

  const t = ease(anim.progress)
  const scale = anim.from ? lerp(anim.from.scale, target.scale, t) : target.scale
  return { target, from: anim.from, t, scale }
}

function Field({ spokes, scale, opacity, idPrefix }) {
  const n = spokes.length
  if (n === 0) return null
  const values = spokes.map((s) => s.snr)
  return (
    <g opacity={opacity}>
      {spokes.map((s, i) => {
        const a = angleFor(i, n)
        return <line key={`${idPrefix}-axis-${s.channel}`} className="envelope__axis" x1="0" y1="0" x2={RADIUS * Math.cos(a)} y2={RADIUS * Math.sin(a)} />
      })}
      <path className="envelope__shape" d={toPath(shapePoints(values, scale, RADIUS))} />
      <path className="envelope__threshold" d={toPath(shapePoints(spokes.map((s) => s.thresholdSnr), scale, RADIUS, RADIUS * 0.02))} />
      {spokes.map((s, i) => {
        const a = angleFor(i, n)
        const r = (Math.min(s.snr, scale) / scale) * RADIUS
        const lx = (RADIUS + 14) * Math.cos(a)
        const ly = (RADIUS + 14) * Math.sin(a)
        const anchor = Math.abs(Math.cos(a)) < 0.3 ? 'middle' : Math.cos(a) > 0 ? 'start' : 'end'
        return (
          <g key={`${idPrefix}-label-${s.channel}`}>
            <circle className="envelope__tip" cx={r * Math.cos(a)} cy={r * Math.sin(a)} r="2.2" />
            <text className="envelope__label" x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle">
              <tspan x={lx}>{s.channel}</tspan>
              <tspan x={lx} dy="11" className="envelope__label-value">{formatInUnit(s.range, s.unit)} · {Math.round(s.snr)}×</tspan>
            </text>
          </g>
        )
      })}
    </g>
  )
}

export default function EnvelopePanel({ latest, sourceKind, source }) {
  const headingId = useId()
  const tableId = useId()
  const valid = latest?.validation.valid
  const profile = valid ? latest.message : null
  const { target, from, t, scale } = useRescale(profile)
  const unbound = unboundFrom(profile)
  const tier = profile?.proposed_tier
  const synthetic = profile && profile.measured === false

  return (
    <section className="panel panel--wide" aria-labelledby={headingId}>
      <header className="panel__header">
        <h2 id={headingId}>Control envelope</h2>
        {sourceKind === 'recorded' && (
          <span className="source-badge" title="A saved calibration profile, not a live calibration">
            {synthetic ? 'SYNTHETIC PROFILE' : 'RECORDED PROFILE'}
          </span>
        )}
      </header>
      <p className="hint">
        Each spoke is one movement this person can use. Its length is how far the movement reaches above its own resting jitter, which has no unit, so every spoke shares one scale. The scale rescales to fit the strongest movement found. Profile format: provisional, profile schema 1.
      </p>

      {source?.profiles && (
        <fieldset className="envelope__picker">
          <legend>Profile</legend>
          {source.profiles.map((p) => (
            <label key={p.name}>
              <input type="radio" name={`${headingId}-profile`} value={p.name} checked={source.selected === p.name} onChange={() => source.select(p.name)} />
              {p.label}
            </label>
          ))}
        </fieldset>
      )}

      {!latest && <p className="unavailable">Waiting for a calibration profile.</p>}
      {latest && !valid && (
        <div className="rejected">
          <p>This calibration profile failed validation and is not drawn:</p>
          <ul>{latest.validation.errors.map((e) => <li key={e}>{e}</li>)}</ul>
        </div>
      )}
      {profile?.session_quality === 'suspect' && (
        <p className="status status--bad" role="status">
          <span aria-hidden="true">✕</span> Calibration suspect: no movement is bound and no control tier is proposed. {tier.reason}
        </p>
      )}

      {profile && (
        <div className="envelope">
          <div className="envelope__figure">
            <svg viewBox="-170 -140 340 280" role="img" aria-labelledby={tableId} aria-describedby={tableId}>
              {ringsFor(scale).map((v, i, all) => (
                <g key={`ring-${i}`}>
                  <circle className="envelope__ring" r={((i + 1) / all.length) * RADIUS} />
                  <text className="envelope__ring-label" x="3" y={-((i + 1) / all.length) * RADIUS - 2}>{Math.round(v)}×</text>
                </g>
              ))}
              {from && t < 1 && <Field spokes={from.spokes} scale={scale} opacity={1 - t} idPrefix="from" />}
              <Field spokes={target.spokes} scale={scale} opacity={from ? t : 1} idPrefix="to" />
              <circle className="envelope__centre" r="2" />
            </svg>
            <p className="envelope__legend">
              <span><span className="swatch swatch--shape" aria-hidden="true" /> Reach: how far each movement goes</span>
              <span><span className="swatch swatch--threshold" aria-hidden="true" /> Dashed: activation threshold</span>
              <span>Rings: multiples of resting jitter (×), rim {Math.round(target.scale)}×</span>
            </p>
          </div>

          <div className="envelope__facts">
            {tier && (
              <p className="envelope__tier">
                <strong>{tier.tier === null ? 'No control tier' : `Tier ${tier.tier}, ${tier.mode}`}</strong>
                <span>{tier.reason}</span>
              </p>
            )}
            <table id={tableId} className="envelope__table">
              <caption>Bound movements ({target.spokes.length})</caption>
              <thead>
                <tr><th scope="col">Movement</th><th scope="col">Role</th><th scope="col">Reach</th><th scope="col">× resting jitter</th><th scope="col">Threshold</th></tr>
              </thead>
              <tbody>
                {target.spokes.length === 0 && <tr><td colSpan="5">None</td></tr>}
                {target.spokes.map((s) => (
                  <tr key={s.channel}>
                    <th scope="row">{s.channel}</th>
                    <td>{ROLE_LABEL[s.role]}</td>
                    <td>{formatInUnit(s.range, s.unit)}</td>
                    <td>{Math.round(s.snr)}×</td>
                    <td>{s.thresholdSnr.toFixed(1)}×</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {unbound.length > 0 && (
              <details className="envelope__unbound">
                <summary>Measured but not bound ({unbound.length})</summary>
                <ul>
                  {unbound.map((u) => (
                    <li key={u.channel}>
                      <code>{u.channel}</code>{' '}
                      {u.status === 'redundant'
                        ? `moves together with ${u.redundantWith}, which is bound instead.`
                        : u.reasons.map((r) => REASONS[r] ?? r).join(' ')}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
