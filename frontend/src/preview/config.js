// Local face-preview side channel (demo/face_preview.py). Override with VITE_FACE_PREVIEW_PORT.
export const FACE_PREVIEW_PORT = Number(import.meta.env?.VITE_FACE_PREVIEW_PORT ?? 8768)
export const facePreviewUrl = (host = '127.0.0.1') => `ws://${host}:${FACE_PREVIEW_PORT}`
// A preview older than this is treated as disconnected: the image and mesh are cleared.
export const PREVIEW_STALE_MS = 1000
