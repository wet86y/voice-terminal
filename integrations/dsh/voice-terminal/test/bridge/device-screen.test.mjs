import assert from 'node:assert/strict'
import test from 'node:test'
import { installScreenCaptureTool } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/tools.js'

test('截图工具固定指定设备并拒绝空根代理身份', async () => {
  const tools = new Map(), calls = []
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'a', dshSessionId: 'd', voiceSessionId: 'v',
    epoch: 1, revision: 2, activationReady: Promise.resolve(), callbacks: { async send(path, value) {
      calls.push({ path, value }); throw new Error('测试回调结束')
    } } }
  installScreenCaptureTool({ tools: { register(tool) { tools.set(tool.name, tool) } } }, binding)
  const tool = tools.get('capture_screen'), signal = new AbortController().signal
  await assert.rejects(tool.execute({ device_id: 'local' }, { agent, signal }), /测试回调结束/)
  assert.equal(calls[0].value.device_id, 'local')
  await assert.rejects(tool.execute({ device_id: ' ' }, { agent, signal }), /设备编号无效/)
  binding.handle = undefined
  await assert.rejects(tool.execute({}, { signal }), /根代理/)
  assert.equal(calls.length, 1)
})
