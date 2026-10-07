import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdir, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { createAttachedVoiceSession, inject } from '../../../../.runtime/dsh/bundles/voice-terminal-shadow-bridge-test/lib/index.js'
import { initializeVoiceTerminalSettings } from '../../../../.runtime/dsh/bundles/voice-terminal-shadow-bridge-test/lib/settings.js'

test('Bridge 的 inject（依赖注入声明）包含设置代码调用的 llm（模型服务）', () => {
  assert.ok(inject.includes('llm'))
})

function fixture({ selection = {}, selectedWorkspace, attachmentError } = {}) {
  const operations = []
  const defaultWorkspace = {
    id: 'default-workspace',
    path: `${process.cwd()}\\workspace`,
    async status() { return 'ok' },
    async attachSession(sessionId) {
      operations.push(`attach:${sessionId}`)
      if (attachmentError) throw attachmentError
    },
  }
  const registered = selectedWorkspace ?? defaultWorkspace
  const settingsValue = { workspaceId: selection.workspaceId ?? 'default-workspace', ...selection }
  const patches = []
  const settings = {
    get() { return settingsValue },
    async update(patch) { patches.push(patch); Object.assign(settingsValue, patch) },
  }
  let createOptions
  const ctx = {
    workspaceRegistry: {
      async resolveByPath() { return defaultWorkspace },
      get(id) { return id === String(registered.id) ? registered : undefined },
    },
    llm: {
      listProviders() { return [{ id: 'global-provider' }, { id: 'voice-provider' }] },
      async listModels(provider) {
        return provider === 'voice-provider' ? [{ id: 'voice-model' }] : [{ id: 'global-model' }]
      },
      async resolveModelInfo(provider, model) {
        assert.equal(provider, 'voice-provider')
        assert.equal(model, 'voice-model')
        return { reasoning: { efforts: [{ id: 'high' }, { id: 'max' }], defaultEffort: 'high' } }
      },
    },
    agents: {
      async create(options) {
        operations.push('create')
        createOptions = options
        return { close: async () => { operations.push('close') } }
      },
    },
  }
  return { ctx, settings, operations, defaultWorkspace, registered, patches, createOptions: () => createOptions }
}

const setup = async () => {}
const globalSelection = { provider: 'global-provider', model: 'global-model', reasoningEffort: 'medium' }

test('new voice session inherits DSH global model and attaches before returning', async () => {
  const state = fixture({ selection: { workspaceId: 'default-workspace' } })
  const created = await createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-inherit', presetId: 'shadow-agent',
    globalSelection, setup,
  })

  assert.deepEqual(created.modelSelection, globalSelection)
  assert.equal(state.createOptions().meta.cwd, state.defaultWorkspace.path)
  assert.deepEqual(state.operations, ['create', 'attach:session-inherit'])
})

test('new voice session applies its workspace, model, and reasoning choices', async () => {
  const customWorkspace = {
    id: 'custom-workspace', path: 'D:\\Projects\\Voice',
    async status() { return 'ok' },
    async attachSession(sessionId) { this.attached = sessionId },
  }
  const state = fixture({
    selection: {
      workspaceId: 'custom-workspace', provider: 'voice-provider', model: 'voice-model', reasoningEffort: 'max',
    },
    selectedWorkspace: customWorkspace,
  })
  const created = await createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-custom', presetId: 'shadow-agent',
    globalSelection, setup,
  })

  assert.deepEqual(created.modelSelection, { provider: 'voice-provider', model: 'voice-model', reasoningEffort: 'max' })
  assert.equal(state.createOptions().meta.cwd, customWorkspace.path)
  assert.equal(customWorkspace.attached, 'session-custom')
})

test('an unavailable saved model falls back to the current DSH global model', async () => {
  const state = fixture({ selection: { provider: 'voice-provider', model: 'removed-model' } })
  const created = await createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-model-fallback', presetId: 'shadow-agent',
    globalSelection, setup,
  })

  assert.deepEqual(created.modelSelection, globalSelection)
  assert.equal(state.settings.get().fallbackNotice, 'model-unavailable')
})

test('an unavailable workspace falls back to the project workspace and records a notice', async () => {
  const invalidWorkspace = {
    id: 'removed-workspace', path: 'D:\\Projects\\Gone',
    async status() { return 'missing-dir' },
  }
  const state = fixture({
    selection: { workspaceId: 'removed-workspace' },
    selectedWorkspace: invalidWorkspace,
  })
  const created = await createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-workspace-fallback', presetId: 'shadow-agent',
    globalSelection, setup,
  })

  assert.equal(created.workspace.id, state.defaultWorkspace.id)
  assert.equal(state.createOptions().meta.cwd, state.defaultWorkspace.path)
  assert.equal(state.settings.get().fallbackNotice, 'workspace-unavailable')
})

test('a workspace persistence failure closes the new agent and prevents startup', async () => {
  const state = fixture({ attachmentError: new Error('disk write failed') })
  await assert.rejects(createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-attach-fails', presetId: 'shadow-agent',
    globalSelection, setup,
  }), /workspace attachment failed/)
  assert.deepEqual(state.operations, ['create', 'attach:session-attach-fails', 'close'])
})

test('已有有效工作区时不登记运行目录的空工作区', async () => {
  const state = fixture({ selection: { workspaceId: 'custom-workspace' }, selectedWorkspace: {
    id: 'custom-workspace', path: 'D:\\Projects\\Voice', async status() { return 'ok' },
    async attachSession() {},
  } })
  state.ctx.workspaceRegistry.resolveByPath = async () => { throw new Error('不应创建或解析备用目录') }
  const opened = await createAttachedVoiceSession({
    ctx: state.ctx, settings: state.settings, sessionId: 'session-existing-workspace', presetId: 'shadow-agent', globalSelection, setup,
  })
  assert.equal(opened.workspace.id, 'custom-workspace')
})

test('便携程序跨版本启动只登记一个数据目录工作区，并保留用户选择', async () => {
  const root = await mkdtemp(join(tmpdir(), 'voice-workspace-resume-'))
  const originalCwd = process.cwd()
  const originalData = process.env.VOICE_DATA_DIR
  const selection = {}
  const registered = new Map()
  let created = 0
  const config = Object.fromEntries(['workspaceId', 'defaultWorkspaceId', 'provider', 'model', 'reasoningEffort', 'fallbackNotice']
    .map(key => [key, { get: () => selection[key] ?? '' }]))
  const ctx = {
    settings: { async update(_namespace, patch) { Object.assign(selection, patch) } },
    workspaceRegistry: {
      get(id) { return [...registered.values()].find(workspace => workspace.id === id) },
      async resolveByPath(path) { return registered.get(path) },
      async create(path, title) {
        const workspace = { id: `workspace-${++created}`, path, title, async status() { return 'ok' } }
        registered.set(path, workspace)
        return workspace
      },
    },
  }
  try {
    process.env.VOICE_DATA_DIR = join(root, 'data')
    for (const version of ['version-one', 'version-two']) {
      const app = join(root, 'apps', version)
      await mkdir(app, { recursive: true })
      process.chdir(app)
      await initializeVoiceTerminalSettings(ctx, config)
    }
    assert.equal(created, 1)
    assert.equal([...registered.values()][0].path, join(root, 'data', 'workspace'))
    assert.equal(selection.workspaceId, 'workspace-1')
    assert.equal(selection.defaultWorkspaceId, 'workspace-1')
  } finally {
    process.chdir(originalCwd)
    if (originalData === undefined) delete process.env.VOICE_DATA_DIR
    else process.env.VOICE_DATA_DIR = originalData
    await rm(root, { recursive: true, force: true })
  }
})
