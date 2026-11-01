import assert from 'node:assert/strict'
import test from 'node:test'
import { createServer } from 'node:http'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { resolve } from 'node:path'

// officialRequire（官方模块解析）：复用已锁定宿主，不安装或模拟另一套适配器。
const source = resolve(fileURLToPath(new URL('.', import.meta.url)), '../../../../../.local/build/dsh/source')
const officialRequire = createRequire(resolve(source, 'packages/llm/llm-pi-ai/package.json'))
const load = name => import(pathToFileURL(officialRequire.resolve(name)).href)
const { Context } = await load('@deepseek-ai/cordis')
const Llm = await load('@deepseek-ai/dsh-llm')
const PiAi = await load('@deepseek-ai/dsh-llm-pi-ai')

test('实际 PiAiAdapter（模型适配器）处理 M3.1 双工具调用并配对回传结果', { timeout: 20_000 }, async () => {
  const requests = []; const headers = []
  const server = createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk)
    requests.push(JSON.parse(Buffer.concat(chunks).toString('utf8'))); headers.push(req.headers)
    const tools = requests.length === 1
    const blocks = tools ? [{ type: 'thinking', thinking: '合成测试推理', signature: 'synthetic-signature' }, ...[0, 1].map(index => ({ type: 'tool_use', id: `synthetic_${index}`, name: 'lookup', input: { key: index } }))] : [{ type: 'text', text: '合成结果已接收' }]
    const events = [
      { type: 'message_start', message: { id: 'synthetic-message', type: 'message', role: 'assistant', model: 'MiniMax-M3.1-Flash', content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 3, output_tokens: 1 } } },
      ...blocks.flatMap((content_block, index) => [{ type: 'content_block_start', index, content_block }, { type: 'content_block_stop', index }]),
      { type: 'message_delta', delta: { stop_reason: tools ? 'tool_use' : 'end_turn', stop_sequence: null }, usage: { output_tokens: 1 } },
      { type: 'message_stop' },
    ]
    res.writeHead(200, { 'content-type': 'text/event-stream' })
    for (const event of events) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`)
    res.end()
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const ctx = new Context()
  process.env.VOICE_MINIMAX_ADAPTER_TEST_KEY = 'synthetic-local-test-key'
  try {
    await ctx.plugin(Llm.default)
    await ctx.plugin(PiAi, { providers: { 'minimax-cn': {
      api: 'anthropic-messages', baseURL: `http://127.0.0.1:${server.address().port}`, apiKeyEnv: 'VOICE_MINIMAX_ADAPTER_TEST_KEY', cacheRetention: 'none',
      models: [{ id: 'MiniMax-M3.1-Flash', contextWindow: 1_000_000, maxTokens: 393_216, input: ['text', 'image'], reasoningEfforts: { low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh', max: 'max' }, compat: { forceAdaptiveThinking: true } }],
    } } })
    const history = [Llm.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '请处理两个合成值' }] })]
    const tools = [{ name: 'lookup', description: '合成只读查值，无实际动作。', parameters: { type: 'object', properties: { key: { type: 'integer' } }, required: ['key'] } }]
    async function generate(effort) {
      const assembler = new Llm.BlockAssembler()
      for await (const chunk of ctx.llm.stream({ provider: 'minimax-cn', model: 'MiniMax-M3.1-Flash', reasoningEffort: Llm.ReasoningEffortId(effort), maxTokens: 393_216, messages: history, tools })) assembler.push(chunk)
      return { message: assembler.message({ provider: 'minimax-cn', model: 'MiniMax-M3.1-Flash', replayState: assembler.replayState }), finish: assembler.finish }
    }
    const first = await generate('low')
    assert.equal(first.finish.kind, 'tool-calls')
    const calls = first.message.content.filter(block => block.type === 'tool-call')
    assert.equal(calls.length, 2)
    history.push(first.message, ...calls.map(call => Llm.createToolResultMessage({ callId: call.id, content: [{ type: 'text', text: '合成工具结果' }], isError: false })))
    assert.equal((await generate('max')).finish.kind, 'stop')
    assert.equal(requests.length, 2)
    const replay = requests[1].messages.flatMap(message => Array.isArray(message.content) ? message.content : [])
    const ids = replay.filter(block => block.type === 'tool_use').map(block => block.id)
    assert.equal(new Set(ids).size, 2)
    assert.deepEqual(replay.filter(block => block.type === 'tool_result').map(block => block.tool_use_id), ids)
    assert.equal(requests[0].thinking.type, 'adaptive')
    assert.equal(requests[0].output_config.effort, 'low')
    assert.equal(requests[1].output_config.effort, 'max')
    assert.equal(requests[0].max_tokens, 393_216)
    assert.ok(!JSON.stringify(requests).includes('block_binding'))
    assert.ok(headers.every(header => header['x-api-key'] === 'synthetic-local-test-key'))
  } finally {
    delete process.env.VOICE_MINIMAX_ADAPTER_TEST_KEY
    await ctx.fiber.dispose()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
})
