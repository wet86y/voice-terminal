import type {} from '@deepseek-ai/dsh-client-locale/client'
import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-ui-settings/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-api-session-controller/client'
import type {} from '@deepseek-ai/dsh-api-workspace-controller/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-chat/client'
import { VoiceTerminalSettingsPage, type VoiceTerminalSettingsInjected } from './VoiceTerminalSettingsTab.tsx'
import { registerVoiceTerminalContextNode } from './voice-terminal-context.js'
import { VoiceTerminalContextNodeView } from './VoiceTerminalContextNodeView.tsx'
import { en, zh, type VoiceTerminalLocaleKey } from './locales.js'
import type { VoiceTerminalSettings } from '../shared/settings-contract.js'
import { VOICE_TERMINAL_SETTINGS_NAMESPACE } from '../shared/settings-contract.js'

export type { VoiceTerminalLocaleKey } from './locales.js'
export type { VoiceTerminalSettingsInjected } from './VoiceTerminalSettingsTab.tsx'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Voice Terminal settings page copy（Voice Terminal 插件设置页文案）. */
    'settings.voiceTerminal': VoiceTerminalLocaleKey
  }
}

/** Browser services required by the dedicated plugin settings page（插件设置页）. */
export const inject = ['slots', 'locale', 'configForms', 'remote', 'remote.session', 'workspaces', 'uiConversation']

/** Register Voice Terminal as an independent DSH settings page（独立设置页面）. */
export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.locale.register('settings.voiceTerminal', { zh, en }), 'voice-terminal: settings dictionaries')
  ctx.effect(() => registerVoiceTerminalContextNode(ctx), 'voice-terminal: visible DSH injections')

  const settings = ctx.configForms.get<VoiceTerminalSettings>(VOICE_TERMINAL_SETTINGS_NAMESPACE)
  const modelCatalog: VoiceTerminalSettingsInjected['modelCatalog'] = async () => {
    const response = await ctx.remote.session.modelCatalog()
    if (!response.ok) throw new Error(`session.modelCatalog failed: ${response.error.code}: ${response.error.message}`)
    return response.value
  }
  const injected = (): VoiceTerminalSettingsInjected => ({
    hooks: { preferences: settings, workspaces: ctx.workspaces.list },
    mutateSettings: ops => settings.mutate(ops),
    modelCatalog,
  })
  const t = ctx.locale.bind('settings.voiceTerminal')

  ctx.effect(() => ctx.configForms.whileServed([VOICE_TERMINAL_SETTINGS_NAMESPACE], () => ctx.slots.inject('settings.section', () => ctx.slots.register({
    name: 'settings.section',
    id: 'voice-terminal',
    order: 30,
    label: () => t('nav'),
    locale: 'settings.voiceTerminal',
    inject: injected,
  }, VoiceTerminalSettingsPage))), 'voice-terminal: settings page')

  ctx.effect(() => ctx.slots.inject('conversation.chat.node', () => ctx.slots.register({
    name: 'conversation.chat.node',
    key: 'voice-terminal-context',
    locale: 'settings.voiceTerminal',
  }, VoiceTerminalContextNodeView)), 'voice-terminal: injected-context renderer')
}
