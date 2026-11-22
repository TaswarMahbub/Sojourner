// Page-side half of the perf harness. These functions are serialised with toString() and run in
// the page by run.js over the Chrome DevTools Protocol, so they must be self-contained: no imports,
// no closures over module scope. They use the DEV-only window.__sim3d hook (Simulator3D.jsx).

/** Is the 3D view up? */
export function pageReady() {
  return Boolean(window.__sim3d && window.__sim3d.root)
}

/**
 * Time one configuration. No requestAnimationFrame: the loop is driven manually. GPU time comes from
 * EXT_disjoint_timer_query_webgl2; wall time around frame()+gl.finish() is reported only as a
 * cross-check, because on Chrome/ANGLE gl.finish() does not wait for GPU completion.
 * @param {{ cfg: string, warmup: number, frames: number, width: number, height: number }} o
 */
export async function pageMeasure(o) {
  const { root } = window.__sim3d
  // Freeze the adaptive ladder unless a config spends it deliberately.
  const realSample = root.ladder.sample.bind(root.ladder)
  root.ladder.sample = () => null
  // Stop the app's own rAF loop: its next re-registration becomes a no-op.
  window.requestAnimationFrame = () => 0
  await new Promise((r) => setTimeout(r, 200))
  const stage = document.querySelector('.sim3d__stage')
  stage.style.cssText = `position:fixed;left:0;top:0;width:${o.width}px;height:${o.height}px;z-index:9999;aspect-ratio:auto`
  await new Promise((r) => setTimeout(r, 300))
  root.resize(o.width, o.height)
  const gl = root.renderer.getContext()
  const dbg = gl.getExtension('WEBGL_debug_renderer_info')
  const c = root.chain
  const cam = root.camera
  let PP = null
  if (c) PP = await import('/node_modules/.vite/deps/postprocessing.js')
  const rebuildSimplePass = (effects) => {
    const i = c.composer.passes.indexOf(c.dofPass)
    c.composer.removePass(c.dofPass)
    c.dofPass = new PP.EffectPass(cam, ...effects)
    c.composer.addPass(c.dofPass, i)
  }
  const passOf = (effect) => c.composer.passes.find((p) => p.effects && p.effects.includes(effect))
  const needsChain = (name) => { if (!c) throw new Error(`config ${name} needs the post chain (do not pass post=0)`) }
  const CONFIGS = {
    noAreaLights: () => { root.scene.traverse((x) => { if (x.isRectAreaLight) x.visible = false }) },
    base: () => {},
    full: () => needsChain('full'),
    noAO: () => { needsChain('noAO'); c.ao.enabled = false },
    noBloom: () => { needsChain('noBloom'); c.composer.removePass(passOf(c.bloom)) },
    noDOF: () => { needsChain('noDOF'); rebuildSimplePass([c.toneMapping, c.grade, c.vignette, c.noise]) },
    noSMAA: () => { needsChain('noSMAA'); c.composer.removePass(passOf(c.smaa)) },
    noGradeVignetteNoise: () => { needsChain('noGradeVignetteNoise'); rebuildSimplePass([c.dof, c.toneMapping]) },
    ladderFloor: () => { for (let r = 0; r < 5; r += 1) for (let k = 0; k < 80; k += 1) realSample(25) },
  }
  if (!CONFIGS[o.cfg]) throw new Error(`unknown config ${o.cfg}`)
  CONFIGS[o.cfg]()
  await new Promise((r) => setTimeout(r, 50))
  root.resize(o.width, o.height)

  const out = {
    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '(WEBGL_debug_renderer_info unavailable)',
    vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
    userAgent: navigator.userAgent,
    buffer: [gl.drawingBufferWidth, gl.drawingBufferHeight],
    devicePixelRatio: window.devicePixelRatio,
    rendererPixelRatio: root.renderer.getPixelRatio(),
    post: root.postEnabled,
    passes: c ? c.composer.passes.filter((p) => p.enabled).map((p) => p.name + (p.effects ? `(${p.effects.map((e) => e.name.replace('Effect', '')).join('+')})` : '')) : null,
    areaLights: (() => { let n = 0; root.scene.traverse((x) => { if (x.isRectAreaLight && x.visible) n += 1 }); return n })(),
    shadowMap: root.scene.getObjectByName('sun').shadow.mapSize.x,
    ladder: root.ladder.applied,
  }
  const tq = gl.getExtension('EXT_disjoint_timer_query_webgl2')
  out.timerQuery = Boolean(tq)
  const path = (t) => {
    const bx = 7.4175; const by = -0.685
    if (t < 0.88) { const u = t / 0.88; return { x: bx * u, y: by * u, heading: Math.atan2(by, bx) } }
    const u = (t - 0.88) / 0.12
    return { x: bx + (7.5 - bx) * u, y: by + (-1.65 - by) * u, heading: -Math.PI / 2 }
  }
  const ch = new MessageChannel()
  const yieldTask = () => new Promise((r) => { ch.port1.onmessage = () => r(); ch.port2.postMessage(0) })
  const wall = []
  const gpu = []
  const pending = []
  let disjoint = 0
  let fakeNow = 0
  const drain = () => {
    for (let k = pending.length - 1; k >= 0; k -= 1) {
      const q = pending[k]
      if (!gl.getQueryParameter(q, gl.QUERY_RESULT_AVAILABLE)) continue
      if (gl.getParameter(tq.GPU_DISJOINT_EXT)) disjoint += 1
      else gpu.push(gl.getQueryParameter(q, gl.QUERY_RESULT) / 1e6)
      gl.deleteQuery(q)
      pending.splice(k, 1)
    }
  }
  const total = o.warmup + o.frames
  for (let i = 0; i < total; i += 1) {
    let q = null
    if (tq && i >= o.warmup) { q = gl.createQuery(); gl.beginQuery(tq.TIME_ELAPSED_EXT, q) }
    const t0 = performance.now()
    fakeNow += 1000 / 60
    root.frame(path(i / total), fakeNow)
    if (q) gl.endQuery(tq.TIME_ELAPSED_EXT)
    gl.finish()
    if (i >= o.warmup) wall.push(performance.now() - t0)
    if (q) pending.push(q)
    await yieldTask() // query results land between tasks; outside the timed interval
    if (tq) drain()
  }
  for (let t = 0; t < 50 && pending.length; t += 1) { await new Promise((r) => setTimeout(r, 20)); drain() }
  out.gpuMs = gpu
  out.wallFinishMs = wall
  out.gpuDisjoint = disjoint
  out.gpuLost = pending.length
  return out
}
