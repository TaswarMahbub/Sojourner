import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
  },
  {
    // Invariant I1: the simulation never imports three.js and never reads the view layer.
    // Data flows one way, simulation → view. Enforced here and by src/sim/importBoundary.test.js.
    files: ['src/sim/**/*.{js,jsx}'],
    rules: {
      'no-restricted-imports': ['error', {
        paths: [{ name: 'three', message: 'Invariant I1: src/sim/ must not import three.' }],
        patterns: [
          { group: ['three/*', 'three/**'], message: 'Invariant I1: src/sim/ must not import three.' },
          { group: ['**/view', '**/view/**'], message: 'Invariant I1: src/sim/ must not import from src/view/.' },
        ],
      }],
    },
  },
  {
    // The perf harness CLI runs under Node (page.js beside it runs in the browser).
    files: ['scripts/perf/run.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    // Node-side test files read fixtures from disk.
    files: ['test/**/*.js'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
])
