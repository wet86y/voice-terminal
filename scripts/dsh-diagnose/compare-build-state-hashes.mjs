/**
 * Verify `build-state.json` against the DSH runtime's own hash recipe.
 *
 * `scripts/setup-dsh.ps1` writes these hashes with PowerShell `Get-TreeHash`, and
 * `voice_terminal/dsh/runtime.py::_tree_hash` re-derives them before starting the
 * DSH child. When the two recipes disagree the runtime refuses to start DSH even
 * though `setup-dsh.ps1 -ValidateOnly` reports success, so this check is the one
 * that explains "DSH unavailable: hash 漂移".
 *
 * Usage: node scripts/dsh-diagnose/compare-build-state-hashes.mjs
 */
import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { BUILD_STATE, BUNDLE_DIRS, PLUGIN_DIRS, REPO_ROOT } from './lib/dsh-paths.mjs'

/** Mirror `DshRuntimeManager._tree_hash`: lowercased path order, upper-case digests. */
function treeHash(paths) {
  const rows = []
  for (const root of paths) {
    const files = []
    const walk = target => {
      const info = statSync(target)
      if (info.isFile()) { files.push(target); return }
      for (const entry of readdirSync(target, { withFileTypes: true })) {
        const full = join(target, entry.name)
        if (entry.isDirectory()) walk(full)
        else if (entry.isFile()) files.push(full)
      }
    }
    walk(root)
    files.sort((left, right) => {
      const a = left.toLowerCase()
      const b = right.toLowerCase()
      return a < b ? -1 : a > b ? 1 : 0
    })
    for (const file of files) {
      const rel = relative(REPO_ROOT, file).split('\\').join('/')
      const digest = createHash('sha256').update(readFileSync(file)).digest('hex').toUpperCase()
      rows.push(`/${rel}:${digest}`)
    }
  }
  return createHash('sha256').update(rows.join('\n'), 'utf8').digest('hex')
}

const source = PLUGIN_DIRS.plugin
const artifact = BUNDLE_DIRS.plugin
const expected = {
  plugin_runtime_artifacts_sha256: treeHash([
    join(artifact, 'package.json'), join(artifact, 'cordis.patch.yml'), join(artifact, 'components.json'), join(artifact, 'lib'),
  ]),
  plugin_build_inputs_sha256: treeHash([
    join(source, 'src'), join(source, 'preset'), join(source, 'package.json'), join(source, 'components.json'),
    join(source, 'build.mjs'), join(source, 'check.mjs'), join(source, 'host.patch.yml'), join(source, 'tsconfig.json'),
    join(source, 'tsconfig.host.json'), join(source, 'tsconfig.client.json'), join(REPO_ROOT, 'scripts/build-dsh-client.mjs'),
  ]),
  shadow_prompt_sha256: treeHash([
    join(REPO_ROOT, 'config/shadow_agent/instructions.md'), join(REPO_ROOT, 'config/dsh/shadow-agent.rules.md'),
  ]),
}

const state = JSON.parse(readFileSync(BUILD_STATE, 'utf8').replace(/^\uFEFF/, ''))
let drifted = 0
for (const [key, value] of Object.entries(expected)) {
  const match = state[key] === value
  if (!match) drifted += 1
  console.log(`${match ? 'MATCH' : 'DRIFT'} ${key}`)
  if (!match) console.log(`   build-state = ${state[key]}\n   runtime     = ${value}`)
}

if (drifted > 0) {
  console.log(`\n${drifted} key(s) drift — the DSH runtime will refuse to start.`)
  console.log('Re-run scripts\\setup-dsh.ps1; if the drift persists, its Get-TreeHash no longer matches')
  console.log('voice_terminal/dsh/runtime.py::_tree_hash (path ordering and text encoding must both agree).')
}
process.exit(drifted === 0 ? 0 : 1)
