#!/usr/bin/env node
// GPU frame-time harness for the 3D simulator (handoff_sim §9). Windows, macOS, Linux.
//
//   cd frontend && npm run dev            # in one terminal (the harness needs the DEV-only hook)
//   npm run perf -- --configs full,noAO --repeat 3 --interleave --cooldown 60
//
// One headless Chrome per run, fresh profile each time (no warm shader cache), the real GPU only:
// a run whose renderer string names a software rasteriser is VOID and excluded. Per run: 60 warm-up
// frames discarded, 600 sampled, GPU time from EXT_disjoint_timer_query_webgl2 (the only timing
// that counts; wall time around gl.finish() is reported as a cross-check, because Chrome/ANGLE's
// finish does not wait for the GPU). Never uses requestAnimationFrame for timing.
//
// Configs (page.js): noAreaLights, base (both run with ?post=0), full, noAO, noBloom, noDOF, noSMAA,
// noGradeVignetteNoise, ladderFloor (all five rungs spent).
//
// Drift: with --repeat, each config's per-run means are correlated with global run order
// (Spearman). A difference between two configs is reported as RESOLVED only when it exceeds twice
// the larger of their run-to-run standard deviations; anything smaller is not data.
// Measured on a Dell Latitude 7430 (i7-1265U, Iris Xe, AC, Windows Balanced plan), 2026-11-22: the
// same config drifted ~6 ms between batches minutes apart, a 60 s --cooldown did NOT reduce the
// spread (sd 1.97 vs 1.92 ms), and GPU time tracked whole-machine CPU load (ρ ≈ 0.64, n = 7) — the
// CPU and iGPU share one package power budget. So: prefer --interleave with --repeat ≥ 3 (drift then
// lands on every config alike) and read cpuAvg beside every number. --cooldown is available, not a fix.
//
// Requires Node >= 22 (global WebSocket). Options: --url, --chrome (or CHROME_PATH), --configs,
// --repeat, --interleave, --cooldown <s>, --warmup, --frames, --width, --height, --out, --headed.

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pageMeasure, pageReady } from './page.js'

const SOFTWARE = /swiftshader|llvmpipe|softpipe|software|microsoft basic render|apple software/i
const POST_OFF = new Set(['noAreaLights', 'base'])

function parseArgs(argv) {
  const a = {
    url: 'http://localhost:5173/', chrome: process.env.CHROME_PATH ?? null, configs: 'noAreaLights,base,full,ladderFloor',
    repeat: 1, interleave: false, cooldown: 0, warmup: 60, frames: 600, width: 1920, height: 1080,
    out: null, headed: false, port: 9333,
  }
  for (let i = 0; i < argv.length; i += 1) {
    const k = argv[i].replace(/^--/, '')
    if (k === 'interleave' || k === 'headed') a[k] = true
    else if (k in a) { const v = argv[i + 1]; i += 1; a[k] = typeof a[k] === 'number' ? Number(v) : v }
    else throw new Error(`unknown option --${k}`)
  }
  a.configs = a.configs.split(',').map((s) => s.trim()).filter(Boolean)
  return a
}

function findChrome(explicit) {
  if (explicit) return explicit
  const c = {
    win32: [
      path.join(process.env.PROGRAMFILES ?? 'C:/Program Files', 'Google/Chrome/Application/chrome.exe'),
      path.join(process.env['PROGRAMFILES(X86)'] ?? 'C:/Program Files (x86)', 'Google/Chrome/Application/chrome.exe'),
      path.join(process.env.LOCALAPPDATA ?? '', 'Google/Chrome/Application/chrome.exe'),
    ],
    darwin: [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
      path.join(os.homedir(), 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
      '/Applications/Chromium.app/Contents/MacOS/Chromium',
    ],
    linux: ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser'],
  }[process.platform] ?? []
  const hit = c.find((p) => existsSync(p))
  if (!hit) throw new Error(`Chrome not found for ${process.platform}; pass --chrome or set CHROME_PATH`)
  return hit
}

/** GPU flags per platform. Windows: force ANGLE on D3D11 (the hardware path). macOS: Chrome's default (ANGLE on Metal). */
function gpuFlags() {
  const common = ['--ignore-gpu-blocklist', '--enable-gpu']
  if (process.platform === 'win32') return [...common, '--use-angle=d3d11']
  return common
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** Whole-machine CPU utilisation, sampled once a second from os.cpus() deltas. Portable. */
function cpuSampler() {
  const snap = () => os.cpus().reduce((a, c) => { const t = Object.values(c.times).reduce((x, y) => x + y, 0); return { idle: a.idle + c.times.idle, total: a.total + t } }, { idle: 0, total: 0 })
  let prev = snap()
  const samples = []
  const id = setInterval(() => { const s = snap(); const dt = s.total - prev.total; if (dt > 0) samples.push(100 * (1 - (s.idle - prev.idle) / dt)); prev = s }, 1000)
  return { stop() { clearInterval(id); return samples } }
}

async function withChrome(opts, fn) {
  const profile = mkdtempSync(path.join(os.tmpdir(), 'sim3d-perf-'))
  const args = [
    ...(opts.headed ? [] : ['--headless=new']), `--remote-debugging-port=${opts.port}`, `--user-data-dir=${profile}`,
    `--window-size=${opts.width},${opts.height}`, ...gpuFlags(), '--no-first-run', '--no-default-browser-check', '--hide-scrollbars', 'about:blank',
  ]
  const proc = spawn(opts.chromePath, args, { stdio: 'ignore' })
  try {
    let target = null
    for (let i = 0; i < 75 && !target; i += 1) {
      await sleep(200)
      try { target = (await (await fetch(`http://127.0.0.1:${opts.port}/json/list`)).json()).find((t) => t.type === 'page') } catch { /* not up yet */ }
    }
    if (!target) throw new Error('Chrome did not expose a page target')
    const ws = new WebSocket(target.webSocketDebuggerUrl)
    await new Promise((r, j) => { ws.addEventListener('open', r, { once: true }); ws.addEventListener('error', j, { once: true }) })
    let id = 0
    const pending = new Map()
    const logs = []
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data)
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id) }
      if (m.method === 'Runtime.exceptionThrown') logs.push(`EXCEPTION ${m.params.exceptionDetails.exception?.description ?? m.params.exceptionDetails.text}`)
      if (m.method === 'Runtime.consoleAPICalled' && (m.params.type === 'error' || m.params.type === 'warning')) logs.push(`${m.params.type}: ${m.params.args.map((x) => x.value ?? x.description).join(' ').slice(0, 300)}`)
    })
    const send = (method, params = {}) => new Promise((r) => { id += 1; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })) })
    const call = async (f, arg) => {
      const r = await send('Runtime.evaluate', { expression: `(${f.toString()})(${JSON.stringify(arg ?? null)})`, awaitPromise: true, returnByValue: true, timeout: 900000 })
      if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description ?? 'page exception')
      return r.result?.result?.value
    }
    await send('Runtime.enable')
    await send('Page.enable')
    await send('Emulation.setDeviceMetricsOverride', { width: opts.width, height: opts.height, deviceScaleFactor: 1, mobile: false })
    return await fn({ send, call, logs })
  } finally {
    proc.kill()
    await sleep(500)
    try { rmSync(profile, { recursive: true, force: true }) } catch { /* Chrome may still hold files */ }
  }
}

const mean = (a) => a.reduce((x, y) => x + y, 0) / a.length
const sd = (a) => { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / (a.length - 1)) }
const pct = (a, p) => { const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(s.length * p))] }
const stats = (a) => (a.length ? { n: a.length, mean: +mean(a).toFixed(2), p50: +pct(a, 0.5).toFixed(2), p95: +pct(a, 0.95).toFixed(2), p99: +pct(a, 0.99).toFixed(2), max: +Math.max(...a).toFixed(2) } : null)
function spearman(xs, ys) {
  const rank = (v) => { const idx = v.map((x, i) => [x, i]).sort((a, b) => a[0] - b[0]); const r = new Array(v.length); idx.forEach(([, i], k) => { r[i] = k + 1 }); return r }
  if (xs.length < 3) return null
  const rx = rank(xs); const ry = rank(ys); const n = xs.length
  return +(1 - (6 * rx.reduce((s, r, i) => s + (r - ry[i]) ** 2, 0)) / (n * (n * n - 1))).toFixed(3)
}

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  if (typeof WebSocket === 'undefined') throw new Error('Node >= 22 required (global WebSocket)')
  opts.chromePath = findChrome(opts.chrome)
  const plan = []
  if (opts.interleave) { for (let r = 0; r < opts.repeat; r += 1) for (const c of opts.configs) plan.push(c) }
  else for (const c of opts.configs) for (let r = 0; r < opts.repeat; r += 1) plan.push(c)

  console.log(`chrome ${opts.chromePath}\nplatform ${process.platform} ${os.release()} · ${os.cpus()[0]?.model} ×${os.cpus().length}\nplan ${plan.join(' → ')}${opts.cooldown ? ` (cooldown ${opts.cooldown} s)` : ''}\n`)
  const runs = []
  for (let i = 0; i < plan.length; i += 1) {
    const cfg = plan[i]
    if (i > 0 && opts.cooldown > 0) { process.stdout.write(`  cooling ${opts.cooldown} s…\n`); await sleep(opts.cooldown * 1000) }
    const startedAt = new Date().toISOString()
    const cpu = cpuSampler()
    let rec
    try {
      rec = await withChrome(opts, async ({ send, call, logs }) => {
        const u = new URL(opts.url)
        if (POST_OFF.has(cfg)) u.searchParams.set('post', '0')
        await send('Page.navigate', { url: u.toString() })
        let ready = false
        for (let k = 0; k < 150 && !ready; k += 1) { await sleep(200); ready = await call(pageReady).catch(() => false) }
        if (!ready) throw new Error(`window.__sim3d not found at ${u} — is the DEV server running?`)
        await sleep(800)
        const r = await call(pageMeasure, { cfg, warmup: opts.warmup, frames: opts.frames, width: opts.width, height: opts.height })
        return { ...r, logs }
      })
    } catch (err) { rec = { error: String(err.message ?? err) } }
    const cpuSamples = cpu.stop()
    const run = { index: i, cfg, startedAt, cpuAvg: cpuSamples.length ? +mean(cpuSamples).toFixed(1) : null, cpuMax: cpuSamples.length ? +Math.max(...cpuSamples).toFixed(1) : null }
    if (rec.error) Object.assign(run, { void: true, reason: rec.error })
    else {
      const sw = SOFTWARE.test(rec.renderer)
      Object.assign(run, {
        renderer: rec.renderer, vendor: rec.vendor, buffer: rec.buffer, rendererPixelRatio: rec.rendererPixelRatio, post: rec.post, passes: rec.passes,
        areaLights: rec.areaLights, shadowMap: rec.shadowMap, ladder: rec.ladder, timerQuery: rec.timerQuery,
        gpu: stats(rec.gpuMs), wallFinish: stats(rec.wallFinishMs), gpuDisjoint: rec.gpuDisjoint, gpuLost: rec.gpuLost, warnings: rec.logs,
        void: sw || !rec.timerQuery, reason: sw ? `software renderer: ${rec.renderer}` : (!rec.timerQuery ? 'no EXT_disjoint_timer_query_webgl2: no GPU time' : null),
      })
    }
    runs.push(run)
    const g = run.gpu
    console.log(`[${i + 1}/${plan.length}] ${cfg.padEnd(22)} ${run.void ? `VOID (${run.reason})` : `gpu mean ${g.mean} p50 ${g.p50} p95 ${g.p95} p99 ${g.p99} ms`}  cpu ${run.cpuAvg}%`)
    if (!run.void && i === 0) console.log(`  renderer: ${run.renderer}`)
  }

  // Summary: per config across repeats, drift vs run order, resolvability.
  const valid = runs.filter((r) => !r.void)
  const byCfg = {}
  for (const r of valid) (byCfg[r.cfg] ??= []).push(r)
  const summary = {}
  for (const [cfg, rs] of Object.entries(byCfg)) {
    const means = rs.map((r) => r.gpu.mean)
    summary[cfg] = { runs: rs.length, gpuMeanOfMeans: +mean(means).toFixed(2), runToRunSd: +sd(means).toFixed(2), perRun: means, runIndex: rs.map((r) => r.index), driftSpearman: spearman(rs.map((r) => r.index), means) }
  }
  const all = valid.map((r) => r.gpu.mean)
  const resolved = []
  const names = Object.keys(summary)
  for (let i = 0; i < names.length; i += 1) for (let j = i + 1; j < names.length; j += 1) {
    const a = summary[names[i]]; const b = summary[names[j]]
    const noise = 2 * Math.max(a.runToRunSd, b.runToRunSd)
    const diff = Math.abs(a.gpuMeanOfMeans - b.gpuMeanOfMeans)
    resolved.push({ a: names[i], b: names[j], diffMs: +diff.toFixed(2), noiseMs: +noise.toFixed(2), resolved: a.runs > 1 && b.runs > 1 && diff > noise })
  }
  console.log('\nsummary (GPU ms, mean of per-run means; sd across runs):')
  for (const [cfg, s] of Object.entries(summary)) console.log(`  ${cfg.padEnd(22)} ${String(s.gpuMeanOfMeans).padStart(7)} ± ${s.runToRunSd}  runs ${s.perRun.join(', ')}${s.driftSpearman !== null ? `  drift ρ=${s.driftSpearman}` : ''}`)
  if (opts.repeat < 2) console.log('  (repeat < 2: no run-to-run variance, so no difference between configs is resolvable)')
  const out = { machine: { platform: process.platform, release: os.release(), cpu: os.cpus()[0]?.model, cores: os.cpus().length, node: process.version }, opts: { ...opts, chromePath: opts.chromePath }, runs, summary, resolved, driftAll: spearman(valid.map((r) => r.index), all) }
  const file = opts.out ?? path.join('perf-results', `perf-${new Date().toISOString().replace(/[:.]/g, '-')}.json`)
  mkdirSync(path.dirname(file), { recursive: true })
  writeFileSync(file, JSON.stringify(out, null, 1))
  console.log(`\nwrote ${file}`)
}

main().catch((err) => { console.error(err); process.exit(1) })
