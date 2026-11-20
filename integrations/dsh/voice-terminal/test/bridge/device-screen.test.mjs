import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { AttachmentBody } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/shared/attachment-transport.js'
import { installScreenCaptureTool } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/bridge/tools.js'

test('截图工具固定指定设备并拒绝空根代理身份', async () => {
  const tools = new Map(), calls = []
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'a', dshSessionId: 'd', voiceSessionId: 'v',
    epoch: 1, revision: 2, activationReady: Promise.resolve(), callbacks: { async receiveAttachment(path, value) {
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

test('截图工具按清单读取分段到达的原始双图，入库后返回附件引用', async () => {
  const tools = new Map(), writes = [], calls = []
  const agent = { id: 'root' }
  const captureId = 'a'.repeat(32)
  const images = [Buffer.alloc(950_000, 0xab), Buffer.alloc(1_990_000, 0xc7)]
  const manifest = Buffer.from(JSON.stringify({ capture_id: captureId, center_source: 'screen_center',
    attachments: images.map((data, index) => {
      const role = index === 0 ? 'global' : 'local'
      return { attachment_id: `${captureId}-${role}`, role, mime_type: index === 0 ? 'image/jpeg' : 'image/png',
        width: 1024, height: 768, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }
    }) }))
  const prefix = Buffer.alloc(4)
  prefix.writeUInt32BE(manifest.length)
  const binding = { handle: { agent }, state: 'active', activationId: 'a', dshSessionId: 'd', voiceSessionId: 'v',
    epoch: 1, revision: 2, activationReady: Promise.resolve(), callbacks: {
      async receiveAttachment(path, payload, consume, signal) {
        calls.push({ path, payload })
        const stream = new ReadableStream({ start(controller) {
          for (const part of [prefix.subarray(0, 2), prefix.subarray(2), manifest.subarray(0, 3),
            manifest.subarray(3), images[0].subarray(0, 30), images[0].subarray(30), images[1]]) {
            controller.enqueue(part)
          }
          controller.close()
        } })
        const body = new AttachmentBody(stream.getReader())
        try { return await consume(body, signal) } finally { await body.close() }
      },
    }, attachments: { imageLimits: { maxImagesPerMessage: 2, maxImageBytes: 2_000_000, maxImageDimension: 2048,
      maxImagePixels: 2_000_000, maxMessageImageBytes: 4_000_000, mediaTypes: ['image/jpeg', 'image/png'] },
    async saveImages(items) {
      writes.push(items.map(item => Buffer.from(item.data)))
      return items.map(item => ({ attachmentId: `sha256:${createHash('sha256').update(item.data).digest('hex')}`,
        mediaType: item.mediaType, name: item.name, bytes: item.data.length, width: 1024, height: 768 }))
    } } }
  installScreenCaptureTool({ tools: { register(tool) { tools.set(tool.name, tool) } } }, binding)
  const tool = tools.get('capture_screen')
  const signal = new AbortController().signal
  const result = await tool.execute({ device_id: 'remote' }, { agent, signal })
  assert.deepEqual(writes, [images])
  assert.equal(result.capture.capture_id, captureId)
  assert.deepEqual(result.visual_frames[0].content.parts.slice(1).map(part => part.role), ['global', 'local'])
  assert.equal(calls.length, 1)
  assert.equal(calls[0].payload.device_id, 'remote')
  assert.ok(JSON.stringify(result).length < 4000)
})
