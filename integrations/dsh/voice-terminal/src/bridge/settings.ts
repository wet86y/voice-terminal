import { SessionId } from '@deepseek-ai/dsh-session'
import { WorkspaceId } from '@deepseek-ai/dsh-workspace'
import type { ModelSelection, AgentHandle } from '@deepseek-ai/dsh-agent'
import type { Context } from '../shared/services.js'
import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import type { Config } from './index.js'
import type { VoiceTerminalSettings } from '../shared/settings-contract.js'
import { VOICE_TERMINAL_SETTINGS_NAMESPACE } from '../shared/settings-contract.js'

/** Stable fallback workspace（稳定备用工作区）：便携程序使用用户数据目录，避免跟随版本目录。 */
async function ensureProjectWorkspace(ctx: Context) {
  const path = resolve(process.env.VOICE_DATA_DIR || process.cwd(), 'workspace')
  await mkdir(path, { recursive: true })
  const existing = await ctx.workspaceRegistry.resolveByPath(path)
  return existing ?? await ctx.workspaceRegistry.create(path, 'Voice Terminal')
}

/** Adapt v0.1.7 settings forms to the bridge's existing get/update contract（适配新设置服务并保留旧接口）. */
export function createSettingsAdapter(ctx: Context, config: Config): {
  get(): VoiceTerminalSettings
  update(patch: Partial<VoiceTerminalSettings>): Promise<void>
} {
  const read = (value: string): string | undefined => value.length === 0 ? undefined : value
  return {
    get() {
      const fallbackNotice = config.fallbackNotice.get()
      return {
        workspaceId: read(config.workspaceId.get()),
        defaultWorkspaceId: read(config.defaultWorkspaceId.get()),
        provider: read(config.provider.get()),
        model: read(config.model.get()),
        reasoningEffort: read(config.reasoningEffort.get()),
        fallbackNotice: fallbackNotice === 'workspace-unavailable'
          || fallbackNotice === 'model-unavailable'
          || fallbackNotice === 'reasoning-unavailable'
          ? fallbackNotice
          : undefined,
      }
    },
    update(patch) {
      return ctx.settings.update(VOICE_TERMINAL_SETTINGS_NAMESPACE, patch)
    },
  }
}

/** Seed the project workspace on first startup using the public settings service（首次启动时登记项目工作区）. */
export async function initializeVoiceTerminalSettings(ctx: Context, config: Config): Promise<{
  readonly settings: ReturnType<typeof createSettingsAdapter>
  readonly defaultWorkspace: Awaited<ReturnType<typeof ensureProjectWorkspace>>
}> {
  const settings = createSettingsAdapter(ctx, config)
  const current = settings.get() as VoiceTerminalSettings
  const selected = current.workspaceId ? ctx.workspaceRegistry.get(WorkspaceId(current.workspaceId)) : undefined
  const defaultWorkspace = selected !== undefined && await selected.status() === 'ok'
    ? selected : await ensureProjectWorkspace(ctx)
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
async function recordFallback(settings: ReturnType<typeof createSettingsAdapter>, notice: FallbackNotice, detail: string): Promise<void> {
  console.warn('[voice-shadow-bridge] voice session setting fallback', { reason: notice, detail })
  try {
    const current = settings.get() as VoiceTerminalSettings
    if (current.fallbackNotice !== notice) await settings.update({ fallbackNotice: notice })
  } catch (error) {
    console.error('[voice-shadow-bridge] could not persist the settings fallback notice', error)
  }
}

/** Pick a still-usable workspace, recovering to the project workspace when necessary. */
async function resolveVoiceWorkspace(ctx: Context, settings: ReturnType<typeof createSettingsAdapter>) {
  const current = settings.get() as VoiceTerminalSettings
  const selectedId = current.workspaceId
  const selected = selectedId ? ctx.workspaceRegistry.get(WorkspaceId(selectedId)) : undefined
  if (selected !== undefined && await selected.status() === 'ok') return selected

  const defaultWorkspace = await ensureProjectWorkspace(ctx)
  if (current.defaultWorkspaceId !== String(defaultWorkspace.id)) {
    await settings.update({ defaultWorkspaceId: String(defaultWorkspace.id) })
  }

  if (selectedId !== undefined && selectedId !== '' && selectedId !== String(defaultWorkspace.id)) {
    await recordFallback(settings, 'workspace-unavailable', `workspace id ${selectedId} is missing or its directory is unavailable`)
  }
  return defaultWorkspace
}

/** Resolve an explicit model route against DSH's live provider/model directory. */
async function resolveVoiceModelSelection(ctx: Context, settings: ReturnType<typeof createSettingsAdapter>, globalSelection: ModelSelection) {
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
    const provider = providers.find(entry => entry.id === providerId)
    if (provider === undefined) throw new Error(`provider ${providerId} is not routable`)
    const models = await ctx.llm.listModels(providerId)
    if (!models.some(entry => entry.id === modelId)) {
      throw new Error(`model ${providerId}/${modelId} is not available`)
    }
    const info = await ctx.llm.resolveModelInfo(providerId, modelId)
    const efforts = info.reasoning?.efforts ?? []
    const defaultEffort = info.reasoning?.defaultEffort
    const chosenEffort = efforts.find(effort => effort.id === current.reasoningEffort)?.id
    if (current.reasoningEffort && efforts.length > 0
      && !efforts.some(effort => effort.id === current.reasoningEffort)) {
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
    return current.reasoningEffort && chosenEffort !== undefined
      ? { ...selection, reasoningEffort: chosenEffort }
      : selection
  } catch (error) {
    await recordFallback(settings, 'model-unavailable', String(error instanceof Error ? error.message : error))
    return { ...globalSelection }
  }
}

/** Create one new voice session and durably attach it before returning to the Bridge. */
export async function createAttachedVoiceSession(options: {
  readonly ctx: Context
  readonly settings: ReturnType<typeof createSettingsAdapter>
  readonly sessionId: string
  readonly presetId: string
  readonly globalSelection: ModelSelection
  readonly setup: (agentCtx: Context) => Promise<void>
}): Promise<{ readonly handle: AgentHandle; readonly workspace: Awaited<ReturnType<typeof resolveVoiceWorkspace>>; readonly modelSelection: ModelSelection }> {
  const workspace = await resolveVoiceWorkspace(options.ctx, options.settings)
  const modelSelection = await resolveVoiceModelSelection(options.ctx, options.settings, options.globalSelection)
  const handle = await options.ctx.agents.create({
    sessionId: SessionId(options.sessionId),
    agentOptions: modelSelection,
    meta: { cwd: workspace.path, agentPreset: options.presetId },
    setup: options.setup,
  })
  try {
    await workspace.attachSession(SessionId(options.sessionId))
  } catch (error) {
    await handle.dispose().catch(() => undefined)
    console.error('[voice-shadow-bridge] could not attach the new voice session to its workspace', {
      session_id: options.sessionId,
      workspace_id: String(workspace.id),
      error: String(error instanceof Error ? error.message : error),
    })
    throw new Error(`Voice DSH session was created but workspace attachment failed: ${String(error instanceof Error ? error.message : error)}`)
  }
  return { handle, workspace, modelSelection }
}
