// The background orb's shader and the rules for when it may move. Pure: no DOM, no React.
//
// A small fragment shader colours a sphere by slowly blending the theme's three orb tones
// (--color-orb-a, -b, -c), lit softly from the upper left with a faint rim. It is semi-transparent
// and drawn on one small canvas: no three.js, no dependencies.
//
// It moves only when nothing says it should rest. Motion that starts on its own and lasts more than
// five seconds needs a way to stop it (WCAG 2.2.2), so there is a pause control; reduced motion and
// a hidden tab also stop it. Stopped, it keeps its last frame: it never disappears.

export const ORB_FPS = 30 // the shader's frame cap; the drift itself is a CSS transform
export const ORB_TIME_SCALE = 0.18 // how fast the colours move; slow on purpose
export const MOTION_PREFERENCE_KEY = 'sojourner.backgroundMotion'
export const DEFAULT_MAX_ALPHA = 0.45

// The theme's maximum orb opacity, clamped to 0..1; unreadable means the safe default.
export function readMaxAlpha(value) {
  const n = Number.parseFloat(value)
  return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : DEFAULT_MAX_ALPHA
}

export function shouldAnimate({ reducedMotion, paused, hidden }) {
  return !reducedMotion && !paused && !hidden
}

// '#c9a97e' or 'c9a97e' → [r, g, b] in 0..1. Anything unreadable falls back to a neutral grey.
export function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim())
  if (!m) return [0.6, 0.6, 0.6]
  const n = parseInt(m[1], 16)
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]
}

export const VERTEX_SHADER = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`

export const FRAGMENT_SHADER = `
precision mediump float;
varying vec2 vUv;
uniform float uTime;
uniform vec3 uA;
uniform vec3 uB;
uniform vec3 uC;
uniform float uMaxAlpha;

void main() {
  float r = length(vUv);
  if (r > 1.0) { gl_FragColor = vec4(0.0); return; }
  vec3 n = vec3(vUv, sqrt(1.0 - r * r));

  // Two slow, interleaved waves across the surface decide how the three tones mix.
  float t = uTime;
  float f = 0.5 + 0.5 * sin(3.0 * n.x + t * 1.3 + 1.5 * sin(2.0 * n.y - t));
  float g = 0.5 + 0.5 * sin(2.6 * n.y - t * 0.9 + cos(3.0 * n.x + t * 0.7));
  vec3 col = mix(mix(uA, uB, f), uC, 0.55 * g);

  // Soft light from the upper left (90 to 105 percent brightness, kept gentle so text over the
  // orb keeps its contrast; see themes.css), and a faint rim toward the third tone.
  float light = max(dot(n, normalize(vec3(-0.45, 0.55, 0.7))), 0.0);
  col *= 0.90 + 0.15 * light;
  float rim = pow(1.0 - n.z, 2.0);
  col = mix(col, uC, 0.35 * rim);

  // Semi-transparent, a little more solid at the centre, with a soft edge. Never more opaque than
  // the theme's --orb-max-alpha.
  float alpha = smoothstep(1.0, 0.96, r) * (0.6 + 0.4 * n.z) * uMaxAlpha;
  gl_FragColor = vec4(col * alpha, alpha);
}`
