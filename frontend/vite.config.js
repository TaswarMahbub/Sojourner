import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // Every test here is deterministic: simulated time only, no wall-clock assertions. The only thing
    // machine load can change is how long a CPU-bound test takes (the receiver parity replay, the
    // ESLint-driven I1 check, the artless traversals: 0.4-1.4 s on a quiet machine). Vitest's 5 s
    // default turned that into failures on a loaded machine; a CI runner is a loaded machine. The
    // timeout exists to catch hangs, not to measure speed, so it is set well clear of load.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
})
