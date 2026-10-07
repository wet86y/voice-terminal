import type { Context } from '../shared/services.js'
/** Bridge assembly（桥接装配）：组合组件，持有唯一绑定表与释放入口。 */
import { TOKEN } from '../shared/protocol.js'
import { CALLBACK_ORIGIN } from '../shared/environment.js'
import { BRIDGE_NAME } from '../shared/identity.js'
import { initializeVoiceTerminalSettings } from './settings.js'
import { registerSessionEvents, stopRuntimeActivity } from './events.js'
import { createBridgeRoute } from './http.js'
import { stopRuntimeReminder } from './context.js'
import type { Binding, Config } from './contracts.js'
export type { Config } from './contracts.js'
export const name = BRIDGE_NAME
export const inject = ['webServer', 'agents', 'sessions', 'agentPresets', 'agentDefaultModel', 'llm', 'sessionTitle', 'settings', 'workspaceRegistry', 'loader']

export function apply(ctx: Context, config: Config): void {
  if (!TOKEN) throw new Error('VOICE_DSH_BRIDGE_TOKEN is required')
  if (!CALLBACK_ORIGIN.startsWith('http://127.0.0.1:')) throw new Error('VOICE_DSH_CALLBACK_ORIGIN must be loopback HTTP')
  if (!config.routePrefix.startsWith('/') || config.routePrefix.endsWith('/')) throw new Error('invalid routePrefix')
  const bindings = new Map<string, Binding>()
  let startupError: unknown
  const settingsReady = initializeVoiceTerminalSettings(ctx, config).catch((error) => {
    startupError = error
    console.error('[voice-shadow-bridge] plugin settings or default workspace initialization failed', error)
    throw error
  })
  // The same rejected promise is surfaced to each attempted voice open below;
  // observe it here as well so a failed first boot does not become an unhandled rejection.
  void settingsReady.catch(() => undefined)

  registerSessionEvents(ctx, bindings)
  const route = createBridgeRoute(ctx, config, bindings, settingsReady, () => startupError)
  ctx.effect(() => {
    const dispose = ctx.webServer.register(route)
    return async () => {
      for (const binding of bindings.values()) {
        binding.state = 'retired'
        stopRuntimeReminder(binding, true)
        stopRuntimeActivity(binding)
      }
      if (typeof dispose === 'function') dispose()
      await Promise.all([...bindings.values()].map(binding => binding.callbacks?.dispose()))
      await Promise.all([...bindings.values()].map(binding => binding.actionQueue))
      bindings.clear()
    }
  }, `voice-terminal-shadow-bridge: ${config.routePrefix}`)
}
