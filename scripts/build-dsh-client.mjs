import { cp, mkdir, readFile, rm, writeFile, copyFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const bridgeDir = resolve(process.argv[2] ?? resolve(repoRoot, 'integrations/dsh/voice-terminal-shadow-bridge'))
const artifactDir = resolve(process.argv[3] ?? resolve(repoRoot, '.runtime/dsh/bundles/voice-terminal-shadow-bridge'))
const sourceDir = resolve(process.argv[4] ?? resolve(repoRoot, '.runtime/dsh/source'))
const stageRoot = resolve(sourceDir, 'packages/client/voice-terminal-shadow-bridge-build')

function assertContained(parent, target) {
  const rel = relative(parent, target)
  if (!rel || rel === '..' || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new Error(`temporary DSH client build directory escaped its root: ${target}`)
  }
}

assertContained(resolve(sourceDir, 'packages/client'), stageRoot)
if (existsSync(stageRoot)) {
  throw new Error(`temporary DSH client build directory already exists; refusing to overwrite it: ${stageRoot}`)
}
const manifest = JSON.parse(await readFile(resolve(bridgeDir, 'package.json'), 'utf8'))
if (manifest.name !== '@voice-terminal/dsh-shadow-bridge') {
  throw new Error(`unexpected DSH Bridge package name: ${manifest.name}`)
}

try {
  await mkdir(stageRoot, { recursive: true })
  await cp(resolve(bridgeDir, 'src'), resolve(stageRoot, 'src'), { recursive: true })
  await copyFile(resolve(bridgeDir, 'package.json'), resolve(stageRoot, 'package.json'))
  await writeFile(resolve(stageRoot, 'tsdown.config.ts'), [
    "import { clientBundle } from '../tsdown.client.ts'",
    "export default clientBundle('@voice-terminal/dsh-shadow-bridge', ['src/index.ts'], { hostPhase: true })",
    '',
  ].join('\n'), 'utf8')

  const tsdown = resolve(sourceDir, 'node_modules/tsdown/dist/run.mjs')
  if (!existsSync(tsdown)) throw new Error(`DSH tsdown build tool is missing: ${tsdown}`)
  const env = { ...process.env }
  delete env.DSH_BUILD_FACE
  const built = spawnSync(process.execPath, [tsdown, '--config', 'tsdown.config.ts'], {
    cwd: stageRoot,
    env,
    stdio: 'inherit',
    windowsHide: true,
  })
  if (built.error) throw built.error
  if (built.status !== 0) throw new Error(`DSH browser plugin build failed with exit ${built.status}`)

  const outputLib = resolve(stageRoot, 'lib')
  const artifactLib = resolve(artifactDir, 'lib')
  await mkdir(artifactLib, { recursive: true })
  for (const file of ['client.js', 'client.js.map']) {
    const source = resolve(outputLib, file)
    if (!existsSync(source)) throw new Error(`DSH browser plugin artifact is missing: ${source}`)
    await copyFile(source, resolve(artifactLib, file))
  }
  console.log(`Built DSH browser plugin into ${artifactLib}`)
} finally {
  await rm(stageRoot, { recursive: true, force: true })
}
