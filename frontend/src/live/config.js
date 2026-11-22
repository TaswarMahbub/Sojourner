// Motor mirror bridge port (docs/sim-decisions.md section 3: proposed, not yet confirmed).
// Override with VITE_MOTOR_BRIDGE_PORT; this is the frontend's only default for it.
export const MOTOR_BRIDGE_PORT = Number(import.meta.env?.VITE_MOTOR_BRIDGE_PORT ?? 8767)
export const motorBridgeUrl = (host = '127.0.0.1') => `ws://${host}:${MOTOR_BRIDGE_PORT}`
