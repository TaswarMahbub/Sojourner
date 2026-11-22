import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { DEFAULT_MAX_ALPHA, FRAGMENT_SHADER, hexToRgb, readMaxAlpha, shouldAnimate } from './orb.js'
import BackgroundOrb from './BackgroundOrb.jsx'

describe('background orb', () => {
  it('moves only when nothing asks it to rest', () => {
    expect(shouldAnimate({ reducedMotion: false, paused: false, hidden: false })).toBe(true)
    expect(shouldAnimate({ reducedMotion: true, paused: false, hidden: false })).toBe(false)
    expect(shouldAnimate({ reducedMotion: false, paused: true, hidden: false })).toBe(false)
    expect(shouldAnimate({ reducedMotion: false, paused: false, hidden: true })).toBe(false)
  })

  it('reads theme colours as 0..1 RGB, falling back to grey', () => {
    expect(hexToRgb('#ff8000')).toEqual([1, 128 / 255, 0])
    expect(hexToRgb(' c9a97e ')).toEqual([201 / 255, 169 / 255, 126 / 255])
    expect(hexToRgb('not a colour')).toEqual([0.6, 0.6, 0.6])
  })

  it('blends the three theme tones and stays semi-transparent', () => {
    for (const u of ['uA', 'uB', 'uC', 'uTime', 'uMaxAlpha']) expect(FRAGMENT_SHADER).toMatch(new RegExp(`uniform \\w+ ${u};`))
    // Opacity never exceeds the theme's cap (centre: 0.6 + 0.4 = 1 times uMaxAlpha).
    expect(FRAGMENT_SHADER).toMatch(/alpha = \(1\.0 - smoothstep\(0\.96, 1\.0, r\)\) \* \(0\.6 \+ 0\.4 \* n\.z\) \* uMaxAlpha/)
  })

  it('calls smoothstep only with its edges in increasing order (GLSL leaves the reverse undefined)', () => {
    const calls = [...FRAGMENT_SHADER.matchAll(/smoothstep\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,/g)]
    expect(calls.length).toBeGreaterThan(0)
    for (const [, edge0, edge1] of calls) expect(Number(edge0)).toBeLessThan(Number(edge1))
    // Gentle shading (0.90 to 1.05), the range the contrast check in themes.css assumes.
    expect(FRAGMENT_SHADER).toContain('col *= 0.90 + 0.15 * light')
  })

  it('reads the theme opacity cap, clamped, with a safe default', () => {
    expect(readMaxAlpha(' 0.55')).toBe(0.55)
    expect(readMaxAlpha('3')).toBe(1)
    expect(readMaxAlpha('')).toBe(DEFAULT_MAX_ALPHA)
  })

  it('is decoration: hidden from screen readers, with a pause control for its motion', () => {
    const html = renderToStaticMarkup(createElement(BackgroundOrb))
    expect(html).toMatch(/<div class="orb" aria-hidden="true">/)
    expect(html).toContain('Pause background motion')
  })
})
