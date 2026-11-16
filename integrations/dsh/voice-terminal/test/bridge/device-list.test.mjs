import assert from 'node:assert/strict'
import test from 'node:test'
import { installDeviceTools } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/tools.js'

function fixture() {
  const tools = new Map(), calls = []
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'a', dshSessionId: 'd', voiceSessionId: 'v',
    epoch: 1, revision: 2, activationReady: Promise.resolve(), callbacks: { async send(path, value, signal) {
      calls.push({ path, value, signal }); return { result: 'accepted', devices: [{ device_id: 'local', connected: true }] }
    } } }
  const callback = (path, value, signal) => binding.callbacks.send(path, value, signal)
  installDeviceTools({ tools: { register(tool) { tools.set(tool.name, tool) } } }, binding, callback)
  return { tool: tools.get('device_list'), agent, binding, calls }
}

test('设备列表只由已绑定根代理读取，保留激活及取消信号', async () => {
  const f = fixture(), controller = new AbortController()
  const result = await f.tool.execute({}, { agent: f.agent, callId: 'call', signal: controller.signal })
  assert.equal(result.devices[0].device_id, 'local')
  assert.equal(f.calls[0].path, '/api/internal/dsh-bridge/device.list')
  assert.equal(f.calls[0].value.activation_id, 'a')
  assert.equal(f.calls[0].signal, controller.signal)
  await assert.rejects(f.tool.execute({}, { agent: { id: 'child' }, signal: controller.signal }), /根代理/)
  await assert.rejects(f.tool.execute({ device_id: 'B' }, { agent: f.agent, signal: controller.signal }), /不接受参数/)
  assert.equal(f.calls.length, 1)
})

test('请求期间激活变化时丢弃设备结果', async () => {
  const f = fixture()
  f.binding.callbacks.send = async () => { f.binding.activationId = 'b'; return { result: 'accepted', devices: [] } }
  await assert.rejects(f.tool.execute({}, { agent: f.agent, signal: new AbortController().signal }), /激活/)
})

test('缺少已绑定根代理时不能以空身份调用', async () => {
  const f = fixture()
  f.binding.handle = undefined
  await assert.rejects(f.tool.execute({}, { signal: new AbortController().signal }), /根代理/)
  assert.equal(f.calls.length, 0)
})
