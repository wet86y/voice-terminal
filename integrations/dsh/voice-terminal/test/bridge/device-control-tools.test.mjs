import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { installDeviceTools } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/tools.js'

const snapshot = { windowId: 10, processId: 20, executablePath: 'C:\\app.exe', title: 'App',
  className: 'Window', rect: { x: 0, y: 0, width: 800, height: 600 }, foreground: true,
  treeHash: 'tree-1', shotHash: 'pixels-1', elementCount: 0 }

function fixture(approvalResult = 'allowed-once') {
  const tools = new Map(), calls = [], approvals = []
  const remoteBytes = Buffer.alloc(128 * 1024 + 7, 0x5a)
  const sourceFiles = new Map()
  let uploadedBytes = Buffer.alloc(0)
  let streamedSaves = 0
  const agent = { id: 'root' }
  const binding = { handle: { agent }, state: 'active', activationId: 'activation-1', dshSessionId: 'dsh-1',
    voiceSessionId: 'voice-1', epoch: 3, revision: 4, activationReady: Promise.resolve() }
  const approval = { async request(value) { approvals.push(value); return approvalResult } }
  const callback = async (path, payload, signal) => {
    calls.push({ path, payload, signal })
    if (path.endsWith('/device.observe')) {
      return { result: 'accepted', device_id: payload.device_id, connection_generation: 2,
        executor_generation: 5, snapshot, elements: [], pixels: [] }
    }
    if (path.endsWith('/device.action')) {
      return { result: 'accepted', task: { request_id: payload.request_id, device_id: payload.device_id,
        operation: payload.operation, status: 'completed', result: { ok: true } } }
    }
    if (path.endsWith('/device.terminal')) {
      return { result: 'accepted', task: { request_id: payload.request_id, device_id: payload.device_id,
        operation: `terminal.${payload.action}`, status: 'succeeded',
        result: { terminal_id: payload.terminal_id, state: payload.action === 'create' ? 'ready' : 'completed' } } }
    }
    if (path.endsWith('/device.file')) {
      const taskResult = payload.action === 'list'
        ? { path: payload.path, entries: [{ name: 'report.bin', kind: 'file', bytes: 3 }], truncated: false }
        : payload.action === 'write'
          ? { transfer_id: 'f'.repeat(32), bytes: payload.attachment.bytes,
            sha256: String(payload.attachment.attachment_id).slice('sha256:'.length), name: payload.attachment.name,
            path: payload.path, connection_generation: 2, executor_generation: 5 }
          : payload.max_bytes > 128 * 1024
            ? { path: payload.path, name: 'large.bin', bytes: remoteBytes.length,
              sha256: createHash('sha256').update(remoteBytes).digest('hex'), transfer_id: 'e'.repeat(32),
              streamed: true, connection_generation: 2, executor_generation: 5 }
            : (() => {
          const data = Buffer.from([0, 255, 1])
          return { path: payload.path, name: 'report.bin', bytes: data.length,
            sha256: createHash('sha256').update(data).digest('hex'), data_base64: data.toString('base64') }
        })()
      return { result: 'accepted', task: { request_id: payload.request_id, device_id: payload.device_id,
        operation: `file.${payload.action}`, status: payload.action === 'write' ? 'receiving' : 'succeeded', result: taskResult } }
    }
    if (path.endsWith('/device.file.transfer')) {
      if (payload.action === 'read_chunk') {
        const chunk = remoteBytes.subarray(payload.offset, payload.offset + payload.limit)
        return { result: 'accepted', transfer: { transfer_id: payload.transfer_id, offset: payload.offset,
          bytes: chunk.length, total_bytes: remoteBytes.length,
          sha256: createHash('sha256').update(remoteBytes).digest('hex'),
          chunk_sha256: createHash('sha256').update(chunk).digest('hex'), data_base64: chunk.toString('base64') } }
      }
      if (payload.action === 'upload_chunk') {
        if (payload.offset !== uploadedBytes.length) throw new Error('unexpected upload offset')
        uploadedBytes = Buffer.concat([uploadedBytes, Buffer.from(payload.data_base64, 'base64')])
        return { result: 'accepted', transfer: { transfer_id: payload.transfer_id, next_offset: uploadedBytes.length } }
      }
      if (payload.action === 'finish_upload') {
        if (uploadedBytes.length !== payload.expected_bytes
          || createHash('sha256').update(uploadedBytes).digest('hex') !== payload.expected_sha256) throw new Error('bad uploaded stream')
        return { result: 'accepted', task: { request_id: payload.parent_request_id, device_id: payload.device_id,
          operation: 'file.write', status: 'succeeded', result: { bytes: uploadedBytes.length, sha256: payload.expected_sha256 } } }
      }
      return { result: 'accepted', transfer: { closed: true } }
    }
    throw new Error(`unexpected tool callback: ${path}`)
  }
  const attachments = {
    imageLimits: { maxImageBytes: 1024, maxImagesPerMessage: 4, maxMessageImageBytes: 2048, maxImagePixels: 1024, maxImageDimension: 1024, mediaTypes: ['image/png'] },
    async saveImages(items) { return items.map(item => ({ attachmentId: createHash('sha256').update(item.data).digest('hex'),
      mediaType: item.mediaType, bytes: item.data.length, width: 1, height: 1, name: item.name })) },
    async saveFile(item) { return { attachmentId: createHash('sha256').update(item.data).digest('hex'), name: item.name ?? 'file.bin', bytes: item.data.length } },
    async saveFileStream(item) {
      streamedSaves += 1
      const chunks = []
      for await (const chunk of item.data) chunks.push(Buffer.from(chunk))
      const data = Buffer.concat(chunks)
      return { attachmentId: `sha256:${createHash('sha256').update(data).digest('hex')}`,
        name: item.name ?? 'file.bin', bytes: data.length }
    },
    async *readFileStream(ref) {
      const data = sourceFiles.get(ref.attachmentId)
      if (!data) throw new Error('attachment is not available')
      for (let offset = 0; offset < data.length; offset += 8192) yield new Uint8Array(data.subarray(offset, offset + 8192))
    },
  }
  binding.attachments = attachments
  const context = { tools: { register(tool) { tools.set(tool.name, tool) } },
    get(name) { return name === 'approval' ? approval : undefined } }
  installDeviceTools(context, binding, callback)
  const exec = { agent, callId: 'call-1', signal: new AbortController().signal }
  return { tools, calls, approvals, exec, sourceFiles, remoteBytes, get uploadedBytes() { return uploadedBytes },
    get streamedSaves() { return streamedSaves } }
}

test('设备动作在用户一次性审批后固定本机目标并复核窗口身份', async () => {
  const f = fixture()
  const observed = await f.tools.get('device_observe').execute({ device_id: 'local', window_id: 10 }, f.exec)
  const result = await f.tools.get('device_control').execute({ device_id: 'local', based_on: observed.observation_id,
    action: 'click', arguments: { x: 12, y: 15 } }, f.exec)
  assert.equal(result.result, 'accepted')
  assert.equal(f.approvals.length, 1)
  assert.equal(f.approvals[0].toolName, 'device_control')
  assert.equal(f.approvals[0].agent, f.exec.agent)
  assert.equal(f.approvals[0].callId, f.exec.callId)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.observe')).length, 4)
  const action = f.calls.find(item => item.path.endsWith('/device.action'))
  assert.equal(action.payload.device_id, 'local')
  assert.equal(action.payload.operation, 'click')
  assert.equal(action.payload.arguments.request.hardware, false)
  assert.equal(action.payload.arguments.focusFallback, false)
  await assert.rejects(f.tools.get('device_control').execute({ device_id: 'remote', based_on: observed.observation_id,
    action: 'click', arguments: { x: 12, y: 15 } }, f.exec), /不属于指定设备/)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.action')).length, 1)
})

test('未获一次性审批时不派发桌面动作', async () => {
  const f = fixture('denied')
  const observed = await f.tools.get('device_observe').execute({ device_id: 'local', window_id: 10 }, f.exec)
  await assert.rejects(f.tools.get('device_control').execute({ device_id: 'local', based_on: observed.observation_id,
    action: 'click', arguments: { x: 12, y: 15 } }, f.exec), /未获授权/)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.action')).length, 0)
  assert.equal(f.approvals.length, 1)
})

test('设备终端固定目标、会话编号和只读命令，并对每条命令请求一次性审批', async () => {
  const f = fixture()
  const created = await f.tools.get('device_terminal').execute({ action: 'create', device_id: 'remote' }, f.exec)
  assert.equal(created.result, 'accepted')
  assert.equal(created.task.result.state, 'ready')
  const run = await f.tools.get('device_terminal').execute({ action: 'input', device_id: 'remote',
    terminal_id: created.terminal_id, command_id: 'hostname' }, f.exec)
  assert.equal(run.task.operation, 'terminal.input')
  assert.equal(f.approvals.length, 1)
  assert.equal(f.approvals[0].toolName, 'device_terminal')
  assert.match(f.approvals[0].reason, /hostname/)
  const sent = f.calls.find(item => item.path.endsWith('/device.terminal') && item.payload.action === 'input')
  assert.equal(sent.payload.device_id, 'remote')
  assert.equal(sent.payload.command_id, 'hostname')
  assert.equal(typeof sent.payload.terminal_id, 'string')
  await assert.rejects(f.tools.get('device_terminal').execute({ action: 'input', device_id: 'remote',
    terminal_id: created.terminal_id, command_id: 'powershell' }, f.exec), /command_id.*must be one of/)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.terminal')).length, 2)
})

test('拒绝终端命令时不向设备派发任务', async () => {
  const f = fixture('denied')
  const created = await f.tools.get('device_terminal').execute({ action: 'create', device_id: 'remote' }, f.exec)
  await assert.rejects(f.tools.get('device_terminal').execute({ action: 'input', device_id: 'remote',
    terminal_id: created.terminal_id, command_id: 'processes' }, f.exec), /未获一次性批准/)
  assert.equal(f.approvals.length, 1)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.terminal')).length, 1)
})

test('设备文件列举和读取均经一次性审批，二进制内容进入 DSH 原生文件附件', async () => {
  const f = fixture()
  const listed = await f.tools.get('device_file').execute({ action: 'list', device_id: 'remote', path: 'C:\\data' }, f.exec)
  assert.equal(listed.entries.length, 1)
  assert.equal(f.approvals[0].toolName, 'device_file')
  const read = await f.tools.get('device_file').execute({ action: 'read', device_id: 'remote', path: 'C:\\data\\report.bin' }, f.exec)
  assert.equal(read.presentation, 'file')
  const content = f.tools.get('device_file').output.render({}, read)
  assert.equal(content[1].type, 'file')
  assert.equal(content[1].attachment.bytes, 3)
  assert.equal(f.approvals.length, 2)
  const sent = f.calls.find(item => item.path.endsWith('/device.file') && item.payload.action === 'read')
  assert.equal(sent.payload.path, 'C:\\data\\report.bin')
  assert.equal(sent.payload.max_bytes, 128 * 1024)
})

test('拒绝设备文件授权时不向设备派发读取请求', async () => {
  const f = fixture('denied')
  await assert.rejects(f.tools.get('device_file').execute({ action: 'read', device_id: 'remote', path: 'C:\\secret.txt' }, f.exec), /未获一次性批准/)
  assert.equal(f.approvals.length, 1)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.file')).length, 0)
})

test('设备大文件读取通过有界分块进入 DSH 流式附件并校验全文件摘要', async () => {
  const f = fixture()
  const read = await f.tools.get('device_file').execute({ action: 'read', device_id: 'remote',
    path: 'D:\\approved\\large.bin', max_bytes: 256 * 1024 }, f.exec)
  assert.equal(read.presentation, 'file')
  assert.equal(read.bytes, f.remoteBytes.length)
  assert.equal(read.sha256, createHash('sha256').update(f.remoteBytes).digest('hex'))
  assert.equal(f.streamedSaves, 1)
  const parts = f.calls.filter(item => item.path.endsWith('/device.file.transfer') && item.payload.action === 'read_chunk')
  assert.equal(parts.length, 3)
  assert.deepEqual(parts.map(item => item.payload.offset), [0, 64 * 1024, 128 * 1024])
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.file.transfer') && item.payload.action === 'close').length, 1)
})

test('设备文件写入只接受已登记附件、一次性审批并按序传送摘要匹配的分块', async () => {
  const f = fixture()
  const bytes = Buffer.from('approved DSH attachment with binary marker\0 and more bytes')
  const attachmentId = `sha256:${createHash('sha256').update(bytes).digest('hex')}`
  f.sourceFiles.set(attachmentId, bytes)
  const result = await f.tools.get('device_file').execute({ action: 'write', device_id: 'remote',
    path: 'D:\\approved\\created.bin', attachment: { attachmentId, name: 'created.bin', bytes: bytes.length } }, f.exec)
  assert.equal(result.task.status, 'succeeded')
  assert.equal(f.uploadedBytes.equals(bytes), true)
  assert.equal(f.approvals.length, 1)
  assert.equal(f.approvals[0].toolName, 'device_file')
  assert.ok(f.approvals[0].reason.includes('D:\\approved\\created.bin'))
  assert.match(f.approvals[0].reason, /SHA-256/)
  const sent = f.calls.find(item => item.path.endsWith('/device.file') && item.payload.action === 'write')
  assert.equal(sent.payload.attachment.attachment_id, attachmentId)
  const finished = f.calls.find(item => item.path.endsWith('/device.file.transfer') && item.payload.action === 'finish_upload')
  assert.equal(finished.payload.expected_bytes, bytes.length)
  assert.equal(finished.payload.expected_sha256, attachmentId.slice('sha256:'.length))
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.file.transfer') && item.payload.action === 'close').length, 1)
})

test('拒绝文件写入授权时不建立附件传输', async () => {
  const f = fixture('denied')
  const bytes = Buffer.from('denied')
  const attachmentId = `sha256:${createHash('sha256').update(bytes).digest('hex')}`
  f.sourceFiles.set(attachmentId, bytes)
  await assert.rejects(f.tools.get('device_file').execute({ action: 'write', device_id: 'remote',
    path: 'D:\\approved\\denied.bin', attachment: { attachmentId, name: 'denied.bin', bytes: bytes.length } }, f.exec), /未获一次性批准/)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.file')).length, 0)
  assert.equal(f.calls.filter(item => item.path.endsWith('/device.file.transfer')).length, 0)
})
