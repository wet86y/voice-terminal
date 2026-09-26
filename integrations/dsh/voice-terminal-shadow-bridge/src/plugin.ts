import z from '@deepseek-ai/schemastery'
import { apply as applyBridge, inject as bridgeInject, name as bridgeName } from './index.js'
import type { Config as BridgeConfig } from './index.js'

export const name = bridgeName
export const inject = bridgeInject

/** Plugin settings shape consumed by the DSH Loader（DSH 插件加载器使用的配置）. */
export interface Config extends BridgeConfig {}

/** Volatile fields are persisted by DSH Settings（volatile 字段由 DSH 设置服务持久化）. */
export const Config: z<Config> = z.object({
  routePrefix: z.string().default('/api/voice-terminal-shadow/v1'),
  maxBodyBytes: z.natural().min(1).default(2_097_152),
  workspaceId: z.string().default('').volatile(),
  defaultWorkspaceId: z.string().default('').volatile(),
  provider: z.string().default('').volatile(),
  model: z.string().default('').volatile(),
  reasoningEffort: z.string().default('').volatile(),
  fallbackNotice: z.string().default('').volatile(),
})

/** DSH plugin entry point（DSH 插件入口）. */
export function apply(ctx: any, config: Config): void {
  applyBridge(ctx, config)
}
