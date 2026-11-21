/** Current endpoints（当前端点）：诊断复用正式代理调试入口，不猜测端口或读取旧日志。 */
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { AgentClient } from '../../agent-call.mjs'

export async function currentEndpoint(component) {
  if (!['dsh', 'voice'].includes(component)) throw new Error('诊断组件名称无效。')
  const client = new AgentClient({ timeoutMs: 10000 })
  try {
    const reply = await client.request('module.call', {
      surface: 'service', operation: 'GET /api/v1/application/snapshot', arguments: {},
    })
    if (reply.error) throw new Error(reply.error.message || '正式宿主未允许诊断查询。')
    const value = reply.result?.value?.data?.endpoints?.[component]?.url
    const url = new URL(value)
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || Number(url.port) < 1
        || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw new Error('当前受管端点尚未就绪。')
    }
    return url.origin
  } catch (error) {
    throw new Error(`无法取得当前受管端点，请按 docs/guides/agent-debug.md（代理调试指引）连接正式调试实例：${error.message}`)
  } finally { await client.close() }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  currentEndpoint(process.argv[2]).then(value => process.stdout.write(`${value}\n`))
    .catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1 })
}
