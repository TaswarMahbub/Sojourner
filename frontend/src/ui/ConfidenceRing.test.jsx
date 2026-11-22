import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import ConfidenceRing from './ConfidenceRing.jsx'

const camera = (tracking_state) => ({ latest: { message: { tracking_state, features: {}, head_pose: null, confidence: null }, validation: { valid: true, errors: [] }, receivedAtMs: 1000 } })
const render = (props) => renderToStaticMarkup(<ConfidenceRing profile={null} commandActive={false} {...props}><span>manual</span></ConfidenceRing>)

describe('ConfidenceRing', () => {
  it('wraps the badge and keeps its text', () => {
    expect(render({ camera: camera('tracking') })).toContain('<span>manual</span>')
  })

  it('is full width while tracking and thins to its 1 px floor when the lens is covered', () => {
    expect(render({ camera: camera('tracking') })).toContain('data-thickness="6.0"')
    const covered = render({ camera: camera('lost') })
    expect(covered).toContain('data-thickness="1.0"')
    expect(covered).toContain('confidence-ring--bad')
    expect(covered).toContain('Signal confidence: poor, face lost')
  })

  it('with no camera data: floor thickness, neutral', () => {
    const html = render({ camera: { latest: null } })
    expect(html).toContain('data-thickness="1.0"')
    expect(html).toContain('confidence-ring--neutral')
  })
})
