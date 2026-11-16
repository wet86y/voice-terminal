import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const helper = fileURLToPath(new URL('../../../../../.local/build/dsh/bundles/voice-terminal-test/native/win32/device-helper.ps1', import.meta.url))

function frame(value) {
  const payload = Buffer.from(JSON.stringify(value), 'utf8')
  const length = Buffer.alloc(4)
  length.writeUInt32BE(payload.length)
  return Buffer.concat([length, payload])
}

test('设备辅助进程复用管道，拒绝任意 PowerShell（系统命令行）入口', { skip: process.platform !== 'win32', timeout: 30000 }, async () => {
  const child = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-STA', '-File', helper],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
  const stdout = [], stderr = []
  child.stdout.on('data', chunk => stdout.push(chunk))
  child.stderr.on('data', chunk => stderr.push(chunk))
  const completed = new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('close', code => code === 0 ? resolve() : reject(new Error(`设备辅助进程退出：${code}，${Buffer.concat(stderr).toString('utf8')}`)))
  })
  child.stdin.end(Buffer.concat([
    frame({ protocol_version: 1, request_id: 'health-1', op: 'health' }),
    frame({ protocol_version: 1, request_id: 'deny-2', op: 'powershell', args: { script: 'exit 99' } }),
    frame({ protocol_version: 1, request_id: 'health-3', op: 'health' }),
  ]))
  await completed
  let data = Buffer.concat(stdout)
  const messages = []
  while (data.length) {
    assert.ok(data.length >= 4)
    const length = data.readUInt32BE()
    assert.ok(length > 0 && length <= 8 * 1024 * 1024 && data.length >= 4 + length)
    messages.push(JSON.parse(data.subarray(4, 4 + length).toString('utf8')))
    data = data.subarray(4 + length)
  }
  assert.deepEqual(messages.map(item => [item.request_id, item.ok]), [['health-1', true], ['deny-2', false], ['health-3', true]])
  assert.equal(typeof messages[0].result.session_id, 'number')
})
