/** Binding callback（绑定回调）：工具与后台通知共同遵守组件生命周期。 */
import { callback } from '../shared/callback.js'
import type { Json } from '../shared/protocol.js'
import type { Binding } from './contracts.js'

export function bindingCallback(binding: Binding, path: string, payload: Record<string, Json>, signal?: AbortSignal): Promise<Record<string, Json>> {
  return binding.callbacks ? binding.callbacks.send(path, payload, signal) : callback(path, payload, signal)
}
