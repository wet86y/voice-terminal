/**
 * Shared helpers for the DSH diagnostics: locate the project's DSH runtime,
 * decode stored session logs through the shipped frame decoder, and list the
 * session artifacts under the project DSH home.
 *
 * The decoder is imported from the pinned DSH build so every script reads logs
 * exactly the way the running host does.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { zstdDecompressSync } from 'node:zlib'

/** Repository root, derived from this module's location (`scripts/dsh-diagnose/lib/`). */
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')

/** Project DSH runtime root (`.runtime/dsh`). */
export const DSH_ROOT = join(REPO_ROOT, '.runtime', 'dsh')

/** Project DSH data directory (`DSH_HOME`). */
export const DSH_HOME = join(DSH_ROOT, 'home')

/** Stored session transcripts root. */
export const SESSIONS_ROOT = join(DSH_HOME, 'sessions')

/** Session projection cache directory. */
export const PROJ_CACHE_DIR = join(DSH_HOME, 'storages', 'session_projcache', 'sessions')

/** Workspace store that lists sessions per workspace. */
export const WORKSPACE_STORE = join(DSH_HOME, 'storages', 'workspace.json')

/** Voice↔DSH session binding store. */
export const SESSION_MAPPINGS = join(DSH_ROOT, 'session-mappings.json')

/** Project DSH build audit file. */
export const BUILD_STATE = join(DSH_ROOT, 'build-state.json')

// 单一安装产物；组件别名只用于兼容现有诊断调用。
export const PLUGIN_BUNDLE = join(DSH_ROOT, 'bundles', 'voice-terminal')
export const PLUGIN_SOURCE = join(REPO_ROOT, 'integrations', 'dsh', 'voice-terminal')
export const BUNDLE_DIRS = { plugin: PLUGIN_BUNDLE, bridge: PLUGIN_BUNDLE, wakeTasks: PLUGIN_BUNDLE, preset: PLUGIN_BUNDLE }
export const PLUGIN_DIRS = {
  plugin: PLUGIN_SOURCE,
  bridge: join(PLUGIN_SOURCE, 'src/bridge'),
  wakeTasks: join(PLUGIN_SOURCE, 'src/wake-tasks'),
  preset: join(PLUGIN_SOURCE, 'preset/shadow-agent'),
}

let cachedDecoder

/** Import the pinned DSH session-persistence frame decoder. */
export async function loadDecoder() {
  if (cachedDecoder !== undefined) return cachedDecoder
  const lib = join(DSH_ROOT, 'source', 'packages', 'session', 'session-persistence-jsonl', 'lib', 'types', 'zstd.js')
  cachedDecoder = await import(pathToFileURL(lib).href)
  return cachedDecoder
}

/** Every stored transcript path, at most `project/session` deep. */
export function* sessionFiles(root = SESSIONS_ROOT, depth = 0) {
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const full = join(root, entry.name)
    if (entry.isDirectory()) {
      if (depth < 2) yield* sessionFiles(full, depth + 1)
    } else if (/session\.v[0-9]+\.jsonl(\.zstd)?$/.test(entry.name)) {
      yield full
    }
  }
}

/**
 * Decode one stored transcript into its JSONL records.
 * @param path transcript path.
 * @returns parsed records, header first; unparsable rows are skipped.
 */
export async function readSessionRecords(path) {
  const decoder = await loadDecoder()
  const raw = readFileSync(path)
  const scan = decoder.scanZstdFrames(raw)
  const parts = []
  try {
    for (const chunk of decoder.decode(raw, scan.frames)) parts.push(Buffer.from(chunk).toString('utf8'))
  } catch {
    parts.push(decodeFramesIndependently(raw))
  }
  return parts.join('').trim().split('\n')
    .map(line => { try { return JSON.parse(line) } catch { return null } })
    .filter(record => record !== null)
}

/** Fallback decode: decompress each Zstandard frame separately. */
function decodeFramesIndependently(raw) {
  const magic = Buffer.from([0x28, 0xb5, 0x2f, 0xfd])
  const offsets = []
  let index = 0
  while ((index = raw.indexOf(magic, index)) !== -1) { offsets.push(index); index += 1 }
  let text = ''
  offsets.forEach((start, position) => {
    const end = position + 1 < offsets.length ? offsets[position + 1] : raw.length
    try { text += zstdDecompressSync(raw.subarray(start, end)).toString('utf8') } catch { /* torn frame */ }
  })
  return text
}

/** Owning session id for one transcript or cache path. */
export function sessionIdOf(path) {
  return path.split(/[\\/]/).slice(-2)[0]
}
