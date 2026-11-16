// 派生自上游 sanitize.spec.ts 和 filesystem.spec.ts，保留原始输入与断言。
import assert from 'node:assert/strict'
import test from 'node:test'
import { redactSensitive, sanitizePath, sanitizeText, sanitizeVisible } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/sanitize.js'
import { detectImageMediaType, looksLikeText } from '../../../../../.local/build/dsh/bundles/voice-terminal-test/lib/device-control/filesystem.js'

test('显示文本移除控制字符、合并制表符并遵循长度预算', () => {
  assert.equal(sanitizeText('hello\u0007world', 20), 'helloworld')
  assert.equal(sanitizeText('a'.repeat(10), 5), 'aaaa…')
  assert.equal(sanitizeText('a\t\t\tb', 20), 'a b')
  assert.equal(sanitizeText('abc', 0), '')
  assert.equal(sanitizePath('C:\\abc', 0), '')
  const path = sanitizePath('C:\\Users\\alice\\AppData\\Local\\Temp\\very-important-file.txt', 20)
  assert.equal(path.length, 20)
  assert.equal(path.endsWith('file.txt'), true)
  assert.equal(path.startsWith('…'), true)
})

test('凭据和令牌在显示前脱敏', () => {
  assert.equal(redactSensitive('password=hunter2 ok'), 'password=[redacted] ok')
  assert.equal(redactSensitive('api_key: sk-abc123'), 'api_key:[redacted]')
  const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'
  assert.match(redactSensitive(`token in body: ${jwt}`), /\[redacted\]/)
  assert.match(redactSensitive('Authorization: Bearer abcdef1234567890'), /Bearer \[redacted\]/)
  const result = sanitizeVisible('password=hunter2secretvalue', 15)
  assert.equal(result.endsWith('…'), true)
  assert.equal(result.includes('hunter2secretvalue'), false)
})

test('文件内容按实际签名识别图片和文本', () => {
  assert.equal(detectImageMediaType(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])), 'image/png')
  assert.equal(detectImageMediaType(Buffer.from([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg')
  assert.equal(detectImageMediaType(Buffer.from('GIF89a')), 'image/gif')
  assert.equal(detectImageMediaType(Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WEBP')])), 'image/webp')
  assert.equal(detectImageMediaType(Buffer.concat([Buffer.from('RIFF'), Buffer.alloc(4), Buffer.from('WAVE')])), undefined)
  assert.equal(detectImageMediaType(Buffer.from('hello world')), undefined)
  assert.equal(detectImageMediaType(Buffer.from([0x89, 0x50])), undefined)
  assert.equal(looksLikeText(Buffer.from('hello world\n')), true)
  assert.equal(looksLikeText(Buffer.from('héllo 世界')), true)
  assert.equal(looksLikeText(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0xff, 0xfe])), false)
  assert.equal(looksLikeText(Buffer.from([0x80, 0x80, 0x80])), false)
  assert.equal(looksLikeText(Buffer.from([0xe2, 0x82])), false)
  assert.equal(looksLikeText(Buffer.alloc(0)), true)
})
