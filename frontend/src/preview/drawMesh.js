// Draws the preview image and its mesh on one canvas, so both share one coordinate system.
// Landmarks are normalized to the captured image: pixel = (x * width, y * height).
import { TESSELATION, CONTOURS } from './faceMeshConnections.js'

export const MESH_COLOR = 'rgba(0, 230, 255, 0.55)'
export const CONTOUR_COLOR = 'rgba(0, 255, 160, 0.95)'
export const VERTEX_COLOR = 'rgb(0, 255, 255)'

function edges(ctx, landmarks, pairs, w, h) {
  ctx.beginPath()
  for (let i = 0; i < pairs.length; i += 2) {
    const a = pairs[i] * 2
    const b = pairs[i + 1] * 2
    ctx.moveTo(landmarks[a] * w, landmarks[a + 1] * h)
    ctx.lineTo(landmarks[b] * w, landmarks[b + 1] * h)
  }
  ctx.stroke()
}

export function drawPreview(ctx, image, landmarks, w, h) {
  ctx.clearRect(0, 0, w, h)
  if (image) ctx.drawImage(image, 0, 0, w, h)
  if (!landmarks) return
  ctx.lineWidth = 0.6
  ctx.strokeStyle = MESH_COLOR
  edges(ctx, landmarks, TESSELATION, w, h)
  ctx.lineWidth = 1.2
  ctx.strokeStyle = CONTOUR_COLOR
  edges(ctx, landmarks, CONTOURS, w, h)
  ctx.fillStyle = VERTEX_COLOR
  for (let i = 0; i < landmarks.length; i += 2) ctx.fillRect(landmarks[i] * w - 1.5, landmarks[i + 1] * h - 1.5, 3, 3)
}
