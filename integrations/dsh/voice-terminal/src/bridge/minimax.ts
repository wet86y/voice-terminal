import { credentialRef } from '@deepseek-ai/dsh-credentials'
import type { Context } from '../shared/services.js'

type RecordValue = Record<string, unknown> // RecordValue（内部配置对象）：不得返回接口或写日志。
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {}
const namespace = 'llm-pi-ai' // namespace（官方模型适配器设置命名空间）。
const routeId = 'minimax-cn' // routeId（用户指定的供应方编号）。
const modelId = 'MiniMax-M3.1-Flash' // modelId（官方模型标识）。
export const MINIMAX_BOOTSTRAP_VERSION = 1 // MINIMAX_BOOTSTRAP_VERSION（首次配置版本）。

/** normalizeManagedModelName（清理旧预设名称）：仅移除本程序曾添加的翻译，保留用户参数及自定名称。 */
async function normalizeManagedModelName(ctx: Context): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const descriptor = ctx.settings.describe({ redactSecrets: false }).find(item => item.ns === namespace)
    if (!descriptor) return
    const route = object(object(object(descriptor.value).providers)[routeId])
    if (!Array.isArray(route.models)) return
    const oldName = 'MiniMax M3.1 Flash（闪速版）' // oldName（上版自动生成的名称，仅用于迁移匹配）。
    if (!route.models.some(item => object(item).id === modelId && object(item).name === oldName)) return
    const models = route.models.map(item => {
      const model = structuredClone(object(item))
      return model.id === modelId && model.name === oldName ? { ...model, name: 'MiniMax M3.1 Flash' } : model
    })
    try {
      await ctx.settings.mutate(namespace, [{ op: 'set', path: ['providers', routeId, 'models'], value: models }], descriptor.revision)
      return
    } catch (error) {
      if (object(error).code === 'SETTINGS_CONFLICT' && attempt < 2) continue
      throw error
    }
  }
}

/** syncMiniMax（共享凭据及首次配置）：只操作目标路由，不触碰全局或会话模型选择。 */
export async function syncMiniMax(ctx: Context, key: string, initialize: boolean): Promise<void> {
  await ctx.credentials.set(credentialRef('MINIMAX_API_KEY'), key)
  if (!initialize) { await normalizeManagedModelName(ctx); return }
  const describe = () => {
    // 内部克隆保留原自定义参数；完整配置不经过网络返回或日志。
    const descriptor = ctx.settings.describe({ redactSecrets: false }).find(item => item.ns === namespace)
    if (!descriptor) throw new Error('MiniMax 模型适配器尚未就绪')
    return descriptor
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    const descriptor = describe()
    const routes = object(object(descriptor.value).providers)
    const previous = structuredClone(object(routes[routeId]))
    const connection = { ...previous, api: 'anthropic-messages', baseURL: 'https://api.minimaxi.com/anthropic', apiKeyEnv: 'MINIMAX_API_KEY' }
    try {
      if (!routes[routeId]) {
        // 先挂载连接，保留内置目录，待目录可读再提交显式合并。
        await ctx.settings.mutate(namespace, [{ op: 'set', path: ['providers', routeId], value: connection }], descriptor.revision)
        continue
      }
      let models: RecordValue[]
      if (Array.isArray(previous.models) && previous.models.length > 0) {
        models = previous.models.map(item => structuredClone(object(item)))
      } else {
        const catalog = await ctx.llm.listModels(routeId)
        if (!catalog.length) throw new Error('MiniMax 原模型目录尚不可读，请稍后重试')
        const overrides = object(previous.modelOverrides)
        models = catalog.map(item => ({ id: item.id, ...structuredClone(object(overrides[item.id])) }))
      }
      const model = { id: modelId, name: 'MiniMax M3.1 Flash', contextWindow: 1_000_000,
        maxTokens: 393_216, input: ['text', 'image'],
        reasoningEfforts: { low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh', max: 'max' },
        compat: { forceAdaptiveThinking: true } }
      const next: RecordValue = { ...connection, models: [...models.filter(item => item.id !== modelId), model] }
      delete next.modelOverrides // modelOverrides（目录覆盖参数）已展开到模型条目，不能与 models（显式列表）并存。
      await ctx.settings.mutate(namespace, [{ op: 'set', path: ['providers', routeId], value: next }], descriptor.revision)
      const readback = object(object(object(describe().value).providers)[routeId])
      const saved = Array.isArray(readback.models) ? readback.models.map(object) : []
      if (readback.api !== next.api || readback.baseURL !== next.baseURL || readback.apiKeyEnv !== next.apiKeyEnv
        || !saved.some(item => item.id === modelId && item.contextWindow === 1_000_000 && item.maxTokens === 393_216)
        || models.some(item => !saved.some(row => row.id === item.id))) throw new Error('MiniMax 配置读回校验失败')
      if (!(await ctx.llm.listModels(routeId)).some(item => item.id === modelId)) throw new Error('MiniMax 已保存但模型目录尚未就绪')
      return
    } catch (error) {
      if (object(error).code === 'SETTINGS_CONFLICT' && attempt < 2) continue
      throw error
    }
  }
  throw new Error('MiniMax 配置尚未完成，请重试')
}
