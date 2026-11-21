/** 客户端定向回归：实际子进程标准流、乱序回执、输入结束、超时与错误。 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { AgentClient, runCli } from '../scripts/agent-call.mjs'

const fixture = fileURLToPath(new URL('./fixtures/agent-stdio.mjs', import.meta.url))
const client = (timeoutMs = 2000) => new AgentClient({ timeoutMs }, (_entry, args, options) => spawn(process.execPath, [fixture, ...args], options))

test('输入结束后排空异步回执，按编号匹配乱序结果', async () => {
  const connection = client()
  const slow = connection.request('slow', { text: '中文参数' })
  const fast = connection.request('fast')
  const closed = connection.close()
  const [first, second] = await Promise.all([slow, fast, closed])
  assert.equal(first.result.method, 'slow')
  assert.equal(first.result.params.text, '中文参数')
  assert.equal(second.result.method, 'fast')
  assert.notEqual(first.id, second.id)
  await assert.rejects(connection.request('late'), /输入已结束/)
})

test('持续连接支持事件、后续请求和原始错误回执', async () => {
  const connection = client()
  const events = []
  connection.on('notification', event => events.push(event))
  const subscribed = await connection.request('events')
  assert.equal(subscribed.result.method, 'events')
  const denied = await connection.request('denied')
  assert.equal(denied.error.code, -32000)
  assert.equal(events[0].method, 'trace.event')
  const next = await connection.request('next')
  assert.equal(next.result.method, 'next')
  await connection.close()
})

test('超时以专用退出码失败并回收所属转发子进程', async () => {
  const connection = client(100)
  const request = connection.request('hang')
  await assert.rejects(request, error => error.exitCode === 4 && /避免重复提交/.test(error.message))
  await assert.rejects(connection.close(), error => error.exitCode === 4)
  assert.equal(connection.closed, true)
})

test('真实断连不伪装成功且拒绝未收到回执的请求', async () => {
  const connection = client()
  const request = connection.request('disconnect')
  await assert.rejects(request, /未完整结束/)
  await assert.rejects(connection.close(), /未完整结束/)
})

test('参数错误在启动连接之前明确失败', async () => {
  await assert.rejects(runCli(['--method', 'ui.snapshot', '--params', '{}', '--params-file', 'missing.json']), /不能同时使用/)
  await assert.rejects(runCli(['--method', 'ui.snapshot', '--params', '[]']), /结构化对象/)
  await assert.rejects(runCli(['--method', 'ui.snapshot', '--params-file', '']), /读取参数失败/)
  await assert.rejects(runCli(['--method', 'module.list', '--screenshot', 'unused.jpg']), /仅适用于/)
  await assert.rejects(runCli(['--method', 'instance.status', '--timeout-ms', '0']), /正整数/)
})
