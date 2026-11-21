/** 仅测试客户端连接管理的子进程夹具，不连接真实业务或提供调试服务。 */
import { createInterface } from 'node:readline'
if (process.argv[2] !== '--agent-stdio') process.exit(2)
const lines = createInterface({ input: process.stdin })
let pending = 0
let ended = false
function finish() { if (ended && pending === 0) process.exitCode = 0 }
lines.on('line', line => {
  const request = JSON.parse(line)
  pending++
  if (request.method === 'hang') return
  if (request.method === 'disconnect') { process.exit(1); return }
  setTimeout(() => {
    if (request.method === 'events') console.log(JSON.stringify({ jsonrpc: '2.0', method: 'trace.event', params: { point: 'test' } }))
    console.log(JSON.stringify({ jsonrpc: '2.0', id: request.id, ...(request.method === 'denied'
      ? { error: { code: -32000, message: '当前实例未开放该调试操作。' } }
      : { result: { method: request.method, params: request.params } }) }))
    pending--
    finish()
  }, request.method === 'slow' ? 30 : 0)
})
lines.on('close', () => { ended = true; finish() })
