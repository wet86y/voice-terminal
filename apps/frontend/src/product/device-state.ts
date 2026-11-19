import { shallowRef } from 'vue'
import type { ProductDeviceSnapshot } from './api'

export interface DeviceState extends ProductDeviceSnapshot {
  core_session_id: string // core_session_id（语音核心运行编号）。
  revision: number // revision（同一核心内的设备状态版本）。
  self_device_id: string // self_device_id（实时通道认证绑定的设备编号）。
  pending_device_id: string | null // pending_device_id（等候媒体就绪的目标设备）。
  can_start: boolean // can_start（服务端当前允许开始录制）。
  listening_requested: boolean // listening_requested（服务端保存的待执行监听意图）。
  devices: Array<ProductDeviceSnapshot['devices'][number] & {
    media_ready: boolean // media_ready（具备有效租约及媒体连接）。
    recording_phase: 'idle' | 'starting' | 'recording' | 'interrupted' // recording_phase（实际录制阶段）。
  }>
}

/** 同一窗口的设备管理、录制控件和原生通道监督器共用已认证的设备状态。 */
export const deviceState = shallowRef<DeviceState | null>(null)
