// Renders the panel to HTML in each phase of a session, so a crash or a missing label is caught
// without a browser. It does not check the animation or the layout.

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import EnvelopePanel from './EnvelopePanel.jsx'
import { CalibrationSession } from '../hooks/calibrationSession.js'
import { syntheticCalibrationEvents } from '../sources/mockCalibrationStream.js'

const events = syntheticCalibrationEvents()
const sessionUntil = (done) => {
  const s = new CalibrationSession()
  for (const { event } of events) {
    if (done(event)) break
    s.receive(event)
  }
  return s.state
}
const render = (session, sourceKind = 'mock') => renderToStaticMarkup(<EnvelopePanel session={session} sourceKind={sourceKind} />)

describe('EnvelopePanel', () => {
  it('waits for the live stream', () => {
    const html = render(new CalibrationSession().state, 'live')
    expect(html).toContain('Waiting for the calibration stream')
    expect(html).toContain('LIVE CALIBRATION')
  })

  it('draws provisional spokes while calibrating, without a threshold', () => {
    const html = render(sessionUntil((e) => e.basis === 'final_rest'))
    expect(html).toContain('SYNTHETIC CALIBRATION STREAM')
    expect(html).toContain('Values are provisional')
    expect(html).toContain('envelope__shape--provisional')
    expect(html).toContain('Provisional: noise from the quiet gaps between reps')
    expect(html).not.toContain('envelope__threshold')
  })

  it('shows final values once rest is measured', () => {
    const html = render(sessionUntil((e) => e.record_type === 'calibration_profile'))
    expect(html).toContain('Values are final')
    expect(html).not.toContain('envelope__shape--provisional')
  })

  it('draws the profile, with its threshold, at the end', () => {
    const html = render(sessionUntil(() => false))
    expect(html).toContain('Bound movements (6)')
    expect(html).toContain('envelope__threshold')
    expect(html).not.toContain('failed a check')
  })

  it('lists stream problems', () => {
    const s = new CalibrationSession()
    s.receive('{not json')
    expect(render(s.state, 'live')).toContain('failed a check')
  })

  it('gives the chart a short name and puts the table in a focusable scroll region', () => {
    const html = render(sessionUntil(() => false))
    expect(html).toContain('aria-label="Control envelope chart: 6 movements. The table lists the same values."')
    expect(html).not.toMatch(/<svg[^>]*aria-describedby/)
    expect(html).toMatch(/<div class="envelope__table-wrap" role="region" aria-labelledby="[^"]+" tabindex="0"><table/)
  })
})
