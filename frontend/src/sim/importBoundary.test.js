// Invariant I1: nothing under src/sim/ imports three.js or the view layer. The ESLint rule in
// eslint.config.js is the primary guard; this test runs that rule from `npm test` so the boundary
// fails the suite as well as the linter, and adds a source scan that also catches dynamic import()
// and require(), which the lint rule does not see.

import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { ESLint } from 'eslint'

const frontendRoot = fileURLToPath(new URL('../..', import.meta.url))
const simDir = fileURLToPath(new URL('.', import.meta.url))

const simFiles = () => readdirSync(simDir, { recursive: true })
  .filter((f) => /\.(js|jsx|mjs)$/.test(f))
  .map((f) => join(simDir, f))

const eslint = new ESLint({ cwd: frontendRoot })
const restrictedImportErrors = async (code, file = 'src/sim/__probe__.js') => {
  const [result] = await eslint.lintText(code, { filePath: join(frontendRoot, file) })
  return result.messages.filter((m) => m.ruleId === 'no-restricted-imports')
}

describe('invariant I1 — src/sim/ never imports three or the view layer', () => {
  it('the lint rule rejects a three import under src/sim/', async () => {
    expect(await restrictedImportErrors("import * as THREE from 'three'\nexport default THREE\n")).toHaveLength(1)
    expect(await restrictedImportErrors("import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'\nexport default OrbitControls\n")).toHaveLength(1)
    expect(await restrictedImportErrors("export { x } from 'three'\n")).toHaveLength(1)
  })

  it('the lint rule rejects a view-layer import under src/sim/', async () => {
    expect(await restrictedImportErrors("import { x } from '../view/sceneRoot.js'\nexport default x\n")).toHaveLength(1)
  })

  it('the lint rule does not apply outside src/sim/', async () => {
    expect(await restrictedImportErrors("import * as THREE from 'three'\nexport default THREE\n", 'src/view/__probe__.js')).toHaveLength(0)
  })

  it('every file under src/sim/ passes the rule', async () => {
    const results = await eslint.lintFiles(simFiles())
    const hits = results.flatMap((r) => r.messages.filter((m) => m.ruleId === 'no-restricted-imports').map((m) => `${relative(frontendRoot, r.filePath)}:${m.line} ${m.message}`))
    expect(hits).toEqual([])
  })

  it('no file under src/sim/ reaches three or the view layer by dynamic import or require', () => {
    const banned = /(?:import\s*\(|require\s*\()\s*['"](?:three(?:\/[^'"]*)?|[^'"]*\/view(?:\/[^'"]*)?)['"]/
    const hits = simFiles().filter((f) => !f.endsWith('importBoundary.test.js') && banned.test(readFileSync(f, 'utf8')))
    expect(hits.map((f) => relative(frontendRoot, f))).toEqual([])
  })
})
