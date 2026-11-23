import assert from 'node:assert/strict'
import test from 'node:test'
import { installRootTools } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/tools.js'

function fixture() {
  const tools = new Map(), calls = []
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'activation', dshSessionId: 'dsh',
    voiceSessionId: 'voice', epoch: 1, revision: 2, callbacks: { async send(path, payload, signal) {
      calls.push({ path, payload, signal })
      return { result: 'accepted', status: 'submitted', generation: 4, delivery: calls.length }
    } } }
  installRootTools({ tools: { register(tool) { tools.set(tool.name, tool) } } }, binding)
  let concluded = 0
  const exec = { agent, callId: 'show-1', signal: new AbortController().signal, concludeTurn() { concluded++ } }
  return { tools, calls, binding, exec, concluded: () => concluded }
}

test('后台图文工具发送同一绑定并保留取消信号，展示与清除均不结束回合', async () => {
  const f = fixture()
  const show = f.tools.get('show_floating_display')
  const args = { content: '# 后台进度\n**正在处理**', duration_seconds: 12 }
  assert.equal((await show.execute(args, f.exec)).status, 'submitted')
  await show.execute(args, f.exec)
  assert.equal(f.calls[0].payload.request_id, f.calls[1].payload.request_id)
  assert.equal(f.calls[0].path, '/api/internal/dsh-bridge/display.publish')
  assert.deepEqual(f.calls[0].payload.display, [args])
  assert.equal(f.calls[0].payload.activation_id, 'activation')
  assert.equal(f.calls[0].signal, f.exec.signal)
  await f.tools.get('clear_floating_display').execute({}, { ...f.exec, callId: 'clear-1' })
  assert.deepEqual(f.calls[2].payload.display, [{ cancel: true }])
  assert.notEqual(f.calls[0].payload.request_id, f.calls[2].payload.request_id)
  assert.equal(f.binding.activationId, 'activation')
  assert.equal(f.concluded(), 0)
})

test('图文工具校验正文、时间、额外字段和根代理身份后才发送', async () => {
  const f = fixture(), show = f.tools.get('show_floating_display')
  for (const args of [
    {}, { content: ' ', duration_seconds: 5 }, { content: '字'.repeat(4097), duration_seconds: 5 },
    ...[4, 61, 5.5, true, '5'].map(duration_seconds => ({ content: '正文', duration_seconds })),
    { content: '正文', duration_seconds: 5, device_id: 'remote' },
  ]) await assert.rejects(show.execute(args, f.exec))
  await assert.rejects(f.tools.get('clear_floating_display').execute({ cancel: true }, f.exec))
  await assert.rejects(show.execute({ content: '正文', duration_seconds: 5 },
    { ...f.exec, agent: { id: 'child' } }), /root Agent/)
  f.binding.activationId = undefined
  await assert.rejects(f.tools.get('clear_floating_display').execute({}, f.exec), /root Agent/)
  assert.equal(f.calls.length, 0)
})

test('等待定时激活登记时运行更换，图文请求不会借用新激活', async () => {
  const f = fixture()
  let ready
  f.binding.activationReady = new Promise(resolve => { ready = resolve })
  const pending = f.tools.get('show_floating_display').execute({ content: '正文', duration_seconds: 5 }, f.exec)
  f.binding.activationId = 'new-activation'
  ready()
  await assert.rejects(pending, /激活已更换/)
  assert.equal(f.calls.length, 0)
})

test('后台图文返回期间激活结束时报告失效，保留实际回执归属', async () => {
  const f = fixture()
  f.binding.callbacks.send = async () => {
    f.binding.activationId = undefined
    return { result: 'accepted', status: 'submitted', generation: 4, delivery: 1 }
  }
  await assert.rejects(f.tools.get('clear_floating_display').execute({}, f.exec), /root Agent/)
  assert.equal(f.concluded(), 0)
})
