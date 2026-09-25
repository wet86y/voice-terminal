import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { VoiceTerminalSettings } from './settings-contract.js'
import { VOICE_TERMINAL_SETTINGS_NAMESPACE } from './settings-contract.js'

/** Resolve the project's existing `workspace` folder and register it with DSH. */
async function ensureProjectWorkspace(ctx: any): Promise<any> {
  const path = resolve(process.cwd(), 'workspace')
  await mkdir(path, { recursive: true })
  const existing = await ctx.workspaceRegistry.resolveByPath(path)
  return existing ?? await ctx.workspaceRegistry.create(path, 'Voice Terminal')
}

/** Register persistent plugin settings and seed the project workspace on first startup. */
export async function initializeVoiceTerminalSettings(ctx: any): Promise<{
  readonly settings: any
  readonly defaultWorkspace: any
}> {
  const internal = ctx.loader?.internal
  if (internal === undefined || typeof internal.import !== 'function') {
    throw new Error('DSH loader internal import service is unavailable for the settings schema')
  }
  const schemaModuleBase = typeof ctx.baseUrl === 'string' && ctx.baseUrl.length > 0
    ? ctx.baseUrl
    : new URL('./settings.js', import.meta.url).href
  const schemaModule = await internal.import(
    '@deepseek-ai/schemastery',
    schemaModuleBase,
    {},
  ) as { default?: any }
  const z = schemaModule.default
  if (z === undefined || typeof z.object !== 'function') {
    throw new Error('DSH schemastery module did not expose its default schema builder')
  }
  const schema = z.object({
    workspaceId: z.string(),
    defaultWorkspaceId: z.string(),
    provider: z.string(),
    model: z.string(),
    reasoningEffort: z.string(),
    fallbackNotice: z.string(),
  })
  const settings = ctx.settings.register(VOICE_TERMINAL_SETTINGS_NAMESPACE, schema)
  const defaultWorkspace = await ensureProjectWorkspace(ctx)
  const current = settings.get() as VoiceTerminalSettings
  const patch: Record<string, string> = {}
  if (current.workspaceId === undefined || current.workspaceId === '') {
    patch.workspaceId = String(defaultWorkspace.id)
  }
  if (current.defaultWorkspaceId !== String(defaultWorkspace.id)) {
    patch.defaultWorkspaceId = String(defaultWorkspace.id)
  }
  if (Object.keys(patch).length > 0) await settings.update(patch)
  return { settings, defaultWorkspace }
}

type FallbackNotice = NonNullable<VoiceTerminalSettings['fallbackNotice']>

/** Persist a recoverable selection warning without hiding the host-side diagnostic. */
async function recordFallback(settings: any, notice: FallbackNotice, detail: string): Promise<void> {
  console.warn('[voice-shadow-bridge] voice session setting fallback', { reason: notice, detail })
  try {
    const current = settings.get() as VoiceTerminalSettings
    if (current.fallbackNotice !== notice) await settings.update({ fallbackNotice: notice })
  } catch (error) {
    console.error('[voice-shadow-bridge] could not persist the settings fallback notice', error)
  }
}

/** Pick a still-usable workspace, recovering to the project workspace when necessary. */
async function resolveVoiceWorkspace(ctx: any, settings: any): Promise<any> {
  const defaultWorkspace = await ensureProjectWorkspace(ctx)
  const current = settings.get() as VoiceTerminalSettings
  if (current.defaultWorkspaceId !== String(defaultWorkspace.id)) {
    await settings.update({ defaultWorkspaceId: String(defaultWorkspace.id) })
  }

  const selectedId = current.workspaceId
  const selected = selectedId ? ctx.workspaceRegistry.get(selectedId) : undefined
  if (selected !== undefined && await selected.status() === 'ok') return selected

  if (selectedId !== undefined && selectedId !== '' && selectedId !== String(defaultWorkspace.id)) {
    await recordFallback(settings, 'workspace-unavailable', `workspace id ${selectedId} is missing or its directory is unavailable`)
  }
  return defaultWorkspace
}

/** Resolve an explicit model route against DSH's live provider/model directory. */
async function resolveVoiceModelSelection(ctx: any, settings: any, globalSelection: any): Promise<any> {
  const current = settings.get() as VoiceTerminalSettings
  const hasModelOverride = Boolean(current.provider || current.model)
  if (!hasModelOverride && !current.reasoningEffort) return { ...globalSelection }
  if (hasModelOverride && (!current.provider || !current.model)) {
    await recordFallback(settings, 'model-unavailable', 'saved provider/model override is incomplete')
    return { ...globalSelection }
  }

  try {
    const providerId = current.provider ?? globalSelection.provider
    const modelId = current.model ?? globalSelection.model
    const providers = ctx.llm.listProviders()
    const provider = providers.find((entry: any) => entry.id === providerId)
    if (provider === undefined) throw new Error(`provider ${providerId} is not routable`)
    const models = await ctx.llm.listModels(providerId)
    if (!models.some((entry: any) => entry.id === modelId)) {
      throw new Error(`model ${providerId}/${modelId} is not available`)
    }
    const info = await ctx.llm.resolveModelInfo(providerId, modelId)
    const efforts = info.reasoning?.efforts ?? []
    const defaultEffort = info.reasoning?.defaultEffort
    if (current.reasoningEffort && efforts.length > 0
      && !efforts.some((effort: any) => effort.id === current.reasoningEffort)) {
      await recordFallback(settings, 'reasoning-unavailable', `reasoning effort ${current.reasoningEffort} is not supported by ${providerId}/${modelId}`)
      return {
        provider: providerId,
        model: modelId,
        ...(defaultEffort === undefined ? {} : { reasoningEffort: defaultEffort }),
      }
    }
    if (current.reasoningEffort && efforts.length === 0) {
      await recordFallback(settings, 'reasoning-unavailable', `model ${providerId}/${modelId} does not expose reasoning effort choices`)
    }
    const selection = hasModelOverride
      ? { provider: providerId, model: modelId }
      : { ...globalSelection }
    return current.reasoningEffort && efforts.some((effort: any) => effort.id === current.reasoningEffort)
      ? { ...selection, reasoningEffort: current.reasoningEffort }
      : selection
  } catch (error) {
    await recordFallback(settings, 'model-unavailable', String(error instanceof Error ? error.message : error))
    return { ...globalSelection }
  }
}

/** Create one new voice session and durably attach it before returning to the Bridge. */
export async function createAttachedVoiceSession(options: {
  readonly ctx: any
  readonly settings: any
  readonly sessionId: string
  readonly presetId: string
  readonly globalSelection: any
  readonly setup: (agentCtx: any) => Promise<void>
}): Promise<{ readonly handle: any; readonly workspace: any; readonly modelSelection: any }> {
  const workspace = await resolveVoiceWorkspace(options.ctx, options.settings)
  const modelSelection = await resolveVoiceModelSelection(options.ctx, options.settings, options.globalSelection)
  const handle = await options.ctx.agents.create({
    sessionId: options.sessionId,
    agentOptions: modelSelection,
    meta: { cwd: workspace.path, agentPreset: options.presetId },
    setup: options.setup,
  })
  try {
    await workspace.attachSession(options.sessionId)
  } catch (error) {
    await handle.close().catch(() => undefined)
    console.error('[voice-shadow-bridge] could not attach the new voice session to its workspace', {
      session_id: options.sessionId,
      workspace_id: String(workspace.id),
      error: String(error instanceof Error ? error.message : error),
    })
    throw new Error(`Voice DSH session was created but workspace attachment failed: ${String(error instanceof Error ? error.message : error)}`)
  }
  return { handle, workspace, modelSelection }
}
