import { useEffect, useRef, useState } from 'react'
import { FRAGMENT_SHADER, MOTION_PREFERENCE_KEY, ORB_FPS, ORB_TIME_SCALE, VERTEX_SHADER, hexToRgb, readMaxAlpha, shouldAnimate } from './orb.js'
import './BackgroundOrb.css'

// A calm, semi-transparent orb floating behind the dashboard. Decoration only: hidden from screen
// readers, never takes pointer events, and never covers content (the app sits above it). Its pause
// control sits at the end of the page, in the flow, so it never covers a panel either.
// See orb.js for the shader and the rules for when it moves.

const readPreference = () => {
  try { return globalThis.localStorage?.getItem(MOTION_PREFERENCE_KEY) !== 'off' } catch { return true }
}

function startShader(canvas, getState) {
  const gl = canvas.getContext('webgl', { premultipliedAlpha: true, alpha: true, antialias: true })
  if (!gl) return null
  const compile = (type, source) => {
    const s = gl.createShader(type)
    gl.shaderSource(s, source)
    gl.compileShader(s)
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null
  }
  const vs = compile(gl.VERTEX_SHADER, VERTEX_SHADER)
  const fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
  if (!vs || !fs) return null
  const program = gl.createProgram()
  gl.attachShader(program, vs)
  gl.attachShader(program, fs)
  gl.linkProgram(program)
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return null
  gl.useProgram(program)

  const buffer = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW)
  const aPos = gl.getAttribLocation(program, 'aPos')
  gl.enableVertexAttribArray(aPos)
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0)
  const uTime = gl.getUniformLocation(program, 'uTime')
  const uColors = ['uA', 'uB', 'uC'].map((name) => gl.getUniformLocation(program, name))
  const uMaxAlpha = gl.getUniformLocation(program, 'uMaxAlpha')

  const readColors = () => {
    const style = getComputedStyle(document.documentElement)
    ;['--color-orb-a', '--color-orb-b', '--color-orb-c'].forEach((v, i) => gl.uniform3fv(uColors[i], hexToRgb(style.getPropertyValue(v))))
    gl.uniform1f(uMaxAlpha, readMaxAlpha(style.getPropertyValue('--orb-max-alpha')))
  }
  const resize = () => {
    const dpr = Math.min(globalThis.devicePixelRatio || 1, 1.5)
    const size = Math.round(canvas.clientWidth * dpr)
    if (canvas.width !== size) { canvas.width = size; canvas.height = size }
    gl.viewport(0, 0, size, size)
  }

  let time = 0
  let last = null
  let raf = 0
  const draw = () => {
    gl.uniform1f(uTime, time)
    gl.clearColor(0, 0, 0, 0)
    gl.clear(gl.COLOR_BUFFER_BIT)
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
  }
  const frame = (now) => {
    raf = requestAnimationFrame(frame)
    if (!shouldAnimate(getState())) { last = null; return }
    if (last !== null && now - last < 1000 / ORB_FPS) return
    time += last === null ? 0 : ((now - last) / 1000) * ORB_TIME_SCALE
    last = now
    draw()
  }
  readColors()
  resize()
  draw() // the first frame always shows, even when motion is off
  raf = requestAnimationFrame(frame)
  return {
    refresh() { readColors(); resize(); draw() },
    stop() { cancelAnimationFrame(raf) },
  }
}

export default function BackgroundOrb() {
  const canvasRef = useRef(null)
  const [motionOn, setMotionOn] = useState(readPreference)
  const [reducedMotion, setReducedMotion] = useState(() => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false)
  const [noShader, setNoShader] = useState(false)
  const state = useRef({ reducedMotion, paused: !motionOn, hidden: false })

  useEffect(() => {
    state.current.reducedMotion = reducedMotion
    state.current.paused = !motionOn
    try { globalThis.localStorage?.setItem(MOTION_PREFERENCE_KEY, motionOn ? 'on' : 'off') } catch { /* private mode */ }
  }, [motionOn, reducedMotion])

  useEffect(() => {
    const shader = startShader(canvasRef.current, () => state.current)
    if (!shader) {
      setNoShader(true) // one-time capability probe: no WebGL, so show the painted stand-in
      return undefined
    }
    const motion = globalThis.matchMedia('(prefers-reduced-motion: reduce)')
    const scheme = globalThis.matchMedia('(prefers-color-scheme: dark)')
    const onMotion = () => setReducedMotion(motion.matches)
    const onScheme = () => shader.refresh()
    const onVisibility = () => { state.current.hidden = document.hidden }
    motion.addEventListener('change', onMotion)
    scheme.addEventListener('change', onScheme)
    document.addEventListener('visibilitychange', onVisibility)
    globalThis.addEventListener('resize', onScheme)
    return () => {
      shader.stop()
      motion.removeEventListener('change', onMotion)
      scheme.removeEventListener('change', onScheme)
      document.removeEventListener('visibilitychange', onVisibility)
      globalThis.removeEventListener('resize', onScheme)
    }
  }, [])

  const moving = shouldAnimate({ reducedMotion, paused: !motionOn, hidden: false })
  return (
    <>
      <div className={`orb${moving ? '' : ' orb--still'}${noShader ? ' orb--fallback' : ''}`} aria-hidden="true">
        <div className="orb__float">
          <canvas ref={canvasRef} className="orb__canvas" />
          <div className="orb__shadow" />
        </div>
      </div>
      {!reducedMotion && (
        <p className="orb__footer">
          <button type="button" className="button orb__toggle" onClick={() => setMotionOn((v) => !v)}>
            {motionOn ? 'Pause background motion' : 'Resume background motion'}
          </button>
        </p>
      )}
    </>
  )
}
