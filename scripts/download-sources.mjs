/** Download sources（统一下载策略）：与原生和后端读取同一配置。 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
export const downloadPolicy = JSON.parse(readFileSync(new URL('../config/download-sources.json', import.meta.url), 'utf8'))
/** registryCandidates（候选注册表）：私有注册表不进入公共回退列表。 */
export function registryCandidates(requested) {
  const registry = requested.replace(/\/$/u, '')
  return downloadPolicy.npm_registries.includes(registry) ? downloadPolicy.npm_registries : [requested]
}
/** selectedRegistry（有效注册表）：保留明确的私有配置，只替换公开规范来源的传输渠道。 */
export function selectedRegistry(requested, env, cwd) {
  if (!downloadPolicy.npm_registries.includes(requested.replace(/\/$/u, ''))) return requested
  const lower = Object.fromEntries(Object.entries(env).map(([key, value]) => [key.toLowerCase(), value]))
  if (lower.npm_config_registry) return lower.npm_config_registry
  const paths = [join(cwd, '.npmrc'), lower.npm_config_userconfig || join(env.USERPROFILE || env.HOME || homedir(), '.npmrc'), lower.npm_config_globalconfig].filter(Boolean)
  for (const path of paths) {
    let content
    try { content = readFileSync(path, 'utf8') } catch (error) { if (error.code === 'ENOENT') continue; throw error }
    const values = [...content.matchAll(/^\s*registry\s*=\s*(.*?)\s*$/gimu)]
    if (values.length) return values.at(-1)[1].replace(/^["']|["']$/gu, '')
  }
  return requested
}
export function registryEnvironment(env, registry) {
  const result = { ...env }
  const isPublic = downloadPolicy.npm_registries.includes(registry.replace(/\/$/u, ''))
  for (const key of Object.keys(result)) if (key.toLowerCase() === 'npm_config_registry' || isPublic && key.toLowerCase() === 'npm_config_disturl') delete result[key]
  result.npm_config_registry = registry
  if (isPublic) result.npm_config_disturl = registry === downloadPolicy.npm_registries[0] ? downloadPolicy.node_dist : downloadPolicy.node_dist_official
  result.npm_config_fetch_timeout = '20000' // fetch_timeout（网络请求超时）：覆盖安装脚本子进程。
  result.npm_config_fetch_retries = '1' // fetch_retries（单源重试次数）：避免回退前长时间等待。
  return result
}
/** canRetryRegistry（来源错误分类）：用户取消和本机构建错误不重试。 */
export function canRetryRegistry(error) {
  return !error.cancelled && /E(?:404|408|429|500|502|503|504|AI_AGAIN|CONNRESET|CONNREFUSED|TIMEDOUT|NETUNREACH|NOTFOUND|TARGET)|FETCH_ERROR|ERR_SOCKET_TIMEOUT|INTEGRITY|TAR_BAD_ARCHIVE|CERT_HAS_EXPIRED|UNABLE_TO_VERIFY_LEAF_SIGNATURE/iu.test(error.installLog ?? '')
}
