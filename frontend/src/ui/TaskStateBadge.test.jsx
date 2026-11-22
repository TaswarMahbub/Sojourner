import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import TaskStateBadge from './TaskStateBadge.jsx'
import { taskStateText } from './taskState.js'
import Simulator3D from '../components/Simulator3D.jsx'

describe('task_state badge', () => {
  it('reads the task state, or the stop reason when halted', () => {
    expect(taskStateText({ taskState: 'manual', haltReason: null })).toBe('manual')
    expect(taskStateText({ taskState: 'manual', haltReason: 'watchdog' })).toBe('halted: watchdog')
  })

  it('marks a halt and names itself for screen readers', () => {
    const html = renderToStaticMarkup(<TaskStateBadge view={{ taskState: 'idle', haltReason: 'watchdog' }} />)
    expect(html).toContain('task-badge--halted')
    expect(html).toContain('Task state: </span>halted: watchdog')
  })
})

describe('3D simulator overlay mount points', () => {
  const camera = { latest: { message: { tracking_state: 'tracking', features: {}, head_pose: null, confidence: null }, validation: { valid: true, errors: [] }, receivedAtMs: 0 } }
  const html = renderToStaticMarkup(<Simulator3D camera={camera} profile={null} />)

  it('puts the confidence ring around the task_state badge in the header', () => {
    expect(html).toMatch(/<span class="confidence-ring[^"]*"[^>]*><span class="task-badge[^"]*">/)
  })

  it('follows the simulator with the session metrics panel', () => {
    expect(html).toContain('Session metrics')
    expect(html).toContain('No attempt has finished yet')
  })

  it('still renders with no camera or profile (ring at its floor, neutral)', () => {
    const bare = renderToStaticMarkup(<Simulator3D />)
    expect(bare).toContain('confidence-ring--neutral')
    expect(bare).toContain('task-badge')
  })
})
