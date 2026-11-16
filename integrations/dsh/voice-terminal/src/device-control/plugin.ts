/** Device control component（设备控制组件）：作为桥接根代理的独立加载服务。 */
import type { Context } from '../shared/services.js'
import { installDeviceTools } from './tools.js'
import type { DeviceToolBinding, DeviceToolCallback } from './types.js'

/** DeviceControlService（设备控制服务）：加载行只提供当前桥接的根代理入口。 */
export interface DeviceControlService {
  installAgentTools(agentCtx: Context, binding: DeviceToolBinding, callback: DeviceToolCallback): void
}

declare module '@deepseek-ai/cordis' {
  interface Context { deviceControl: DeviceControlService }
}

export const name = 'voice-terminal-device-control'
export const inject: string[] = []

/** apply（装配）：服务受 Loader（加载器）生命周期管理。 */
export function apply(ctx: Context): void {
  ctx.provide('deviceControl', Object.freeze({ installAgentTools: installDeviceTools }))
}
