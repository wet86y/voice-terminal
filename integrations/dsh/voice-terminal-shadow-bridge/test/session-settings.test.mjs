import assert from 'node:assert/strict'
import test from 'node:test'

import { createAttachedVoiceSession } from '../../../../.runtime/dsh/bundles/voice-terminal-shadow-bridge-test/lib/index.js'

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
