/** Device tools（设备工具）：仅向当前已绑定的根代理登记，并固定设备与代次。 */
import { createHash, randomUUID } from 'node:crypto'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { ApprovalService } from '@deepseek-ai/dsh-user-approval'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { AttachmentId, type FileAttachmentRef } from '@deepseek-ai/dsh-attachment'
import type { Context } from '../shared/services.js'
import type { Json } from '../shared/protocol.js'
import type { DeviceAttachmentCallback, DeviceToolBinding, DeviceToolCallback } from './types.js'
import { DeviceActionError, DeviceActionExecutor, type DeviceActionBackend, type WindowAction, type WindowActionName } from './executor.js'
import { ObservationStore, type ObservationRecord } from './observe.js'
import type { DeviceTarget, WindowSnapshot } from './types.js'
import { sanitizePath, sanitizeVisible } from './sanitize.js'
import { detectImageMediaType, looksLikeText } from './filesystem.js'

const OBSERVATION_CONFIG = {
  maxCachedObservations: 64,
  maxObservationAgeMs: 30_000,
  staleCheckTree: true,
  staleCheckPixels: true,
  staleCheckPixelsExemptMatchers: [] as readonly RegExp[],
}
const ACTION_NAMES = new Set<WindowActionName>([
  'click', 'type', 'scroll', 'key', 'move', 'window_control', 'invoke', 'multi_action',
])

function asObject(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function contentBlocks(value: Record<string, Json>): ContentBlock[] {
  const presentation = value.presentation
  if (presentation === 'text' && typeof value.text === 'string') return [{ type: 'text', text: value.text }]
  const attachment = asObject(value.attachment)
  if (presentation === 'image' && attachment && typeof attachment.attachmentId === 'string'
    && typeof attachment.name === 'string' && typeof attachment.bytes === 'number'
    && typeof attachment.width === 'number' && typeof attachment.height === 'number'
    && ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(String(attachment.mediaType))) {
    const mediaType = String(attachment.mediaType)
    if (mediaType === 'image/png' || mediaType === 'image/jpeg' || mediaType === 'image/webp' || mediaType === 'image/gif') {
      return [{ type: 'image', attachment: { attachmentId: AttachmentId(attachment.attachmentId),
        name: attachment.name, bytes: attachment.bytes, width: attachment.width, height: attachment.height, mediaType } }]
    }
  }
  if (presentation === 'file' && attachment && typeof attachment.attachmentId === 'string'
    && typeof attachment.name === 'string' && typeof attachment.bytes === 'number') {
    const label = typeof value.path === 'string' ? value.path : String(attachment.name)
    return [{ type: 'text', text: `已读取设备文件：${sanitizePath(label, 240)}（${attachment.bytes} 字节，SHA-256 ${String(value.sha256)}）。` },
      { type: 'file', attachment: { attachmentId: AttachmentId(attachment.attachmentId), name: attachment.name, bytes: attachment.bytes } }]
  }
  return [{ type: 'text', text: JSON.stringify(value) }]
}

function exactArgs(value: unknown, fields: string[]): Record<string, unknown> {
  const row = asObject(value)
  if (!row || Object.keys(row).some(key => !fields.includes(key)) || fields.some(key => !(key in row))) {
    throw new Error(`设备工具参数必须严格包含：${fields.join('、')}。`)
  }
  return row
}

function targetOf(value: unknown): DeviceTarget {
  const row = asObject(value)
  if (!row || typeof row.device_id !== 'string'
    || typeof row.connection_generation !== 'number' || !Number.isSafeInteger(row.connection_generation) || row.connection_generation < 1
    || typeof row.executor_generation !== 'number' || !Number.isSafeInteger(row.executor_generation) || row.executor_generation < 1) {
    throw new Error('设备响应缺少有效设备编号或代次。')
  }
  return {
    deviceId: row.device_id,
    connectionGeneration: Number(row.connection_generation),
    executorGeneration: Number(row.executor_generation),
  }
}

function snapshotOf(value: unknown): WindowSnapshot {
  const row = asObject(value)
  const rect = asObject(row?.rect)
  if (!row || !rect || typeof row.windowId !== 'number' || !Number.isSafeInteger(row.windowId)
    || typeof row.processId !== 'number' || !Number.isSafeInteger(row.processId)
    || !(typeof row.executablePath === 'string' || row.executablePath === null)
    || typeof row.title !== 'string' || typeof row.className !== 'string'
    || !['x', 'y', 'width', 'height'].every(key => typeof rect[key] === 'number' && Number.isSafeInteger(rect[key]))
    || typeof row.foreground !== 'boolean' || typeof row.treeHash !== 'string' || typeof row.shotHash !== 'string'
    || typeof row.elementCount !== 'number' || !Number.isSafeInteger(row.elementCount)) {
    throw new Error('设备观察返回了无效窗口快照。')
  }
  return {
    windowId: Number(row.windowId), processId: Number(row.processId),
    executablePath: row.executablePath as string | null,
    title: row.title, className: row.className,
    rect: { x: Number(rect.x), y: Number(rect.y), width: Number(rect.width), height: Number(rect.height) },
    foreground: row.foreground, treeHash: row.treeHash, shotHash: row.shotHash,
    elementCount: Number(row.elementCount),
  }
}

function visibleSnapshot(snapshot: WindowSnapshot): Record<string, Json> {
  return {
    window_id: snapshot.windowId,
    process_id: snapshot.processId,
    executable_path: snapshot.executablePath === null ? null : sanitizePath(snapshot.executablePath, 240),
    title: sanitizeVisible(snapshot.title, 240),
    class_name: sanitizeVisible(snapshot.className, 160),
    rect: { x: snapshot.rect.x, y: snapshot.rect.y, width: snapshot.rect.width, height: snapshot.rect.height },
    foreground: snapshot.foreground,
    tree_hash: snapshot.treeHash,
    shot_hash: snapshot.shotHash,
    element_count: snapshot.elementCount,
    fields: {
      window_id: 'Windows 窗口句柄，后续动作由观察记录固定',
      process_id: '所属进程编号',
      executable_path: '所属程序路径，已截断并脱敏',
      title: '窗口标题，已截断并脱敏',
      class_name: '窗口类名',
      rect: '屏幕坐标窗口矩形',
      foreground: '窗口是否位于前台',
      tree_hash: '界面树摘要；动作前重新核对',
      shot_hash: '窗口像素摘要；动作前重新核对',
      element_count: '返回界面树元素数量',
    },
  }
}

function visibleElements(value: unknown): Record<string, Json>[] {
  if (!Array.isArray(value) || value.length > 200) throw new Error('界面元素列表无效或超过数量上限。')
  return value.map(item => {
    const row = asObject(item)
    const rect = asObject(row?.rect)
    if (!row || !rect || typeof row.elementId !== 'string' || typeof row.controlType !== 'string'
      || typeof row.name !== 'string' || typeof row.automationId !== 'string' || typeof row.enabled !== 'boolean'
      || !Array.isArray(row.patterns)
      || !['x', 'y', 'width', 'height'].every(key => typeof rect[key] === 'number' && Number.isSafeInteger(rect[key]))) {
      throw new Error('界面元素记录无效。')
    }
    return {
      element_id: row.elementId,
      control_type: sanitizeVisible(row.controlType, 80),
      name: sanitizeVisible(row.name, 240),
      automation_id: sanitizeVisible(row.automationId, 120),
      rect: { x: Number(rect.x), y: Number(rect.y), width: Number(rect.width), height: Number(rect.height) },
      enabled: row.enabled,
      patterns: row.patterns.filter((pattern): pattern is string => typeof pattern === 'string').slice(0, 16),
      fields: { element_id: 'UI Automation 运行时元素编号', control_type: '控件类型', name: '可见控件名称，已脱敏',
        automation_id: '自动化编号', rect: '屏幕坐标边界', enabled: '控件是否可操作', patterns: '支持的 UI Automation 模式' },
    }
  })
}

function visibleWindows(value: unknown): Record<string, Json>[] {
  if (!Array.isArray(value) || value.length > 1024) throw new Error('窗口清单无效或超过数量上限。')
  return value.map(item => {
    const row = asObject(item)
    if (!row || typeof row.windowId !== 'number' || !Number.isSafeInteger(row.windowId)
      || typeof row.processId !== 'number' || !Number.isSafeInteger(row.processId)
      || typeof row.title !== 'string' || typeof row.className !== 'string'
      || !(typeof row.executablePath === 'string' || row.executablePath === null)) {
      throw new Error('窗口清单包含无效记录。')
    }
    const rect = asObject(row.rect)
    if (!rect || !['x', 'y', 'width', 'height'].every(key => typeof rect[key] === 'number' && Number.isSafeInteger(rect[key]))) {
      throw new Error('窗口清单包含无效边界。')
    }
    return { window_id: Number(row.windowId), process_id: Number(row.processId),
      title: sanitizeVisible(row.title, 240), class_name: sanitizeVisible(row.className, 160),
      executable_path: row.executablePath === null ? null : sanitizePath(row.executablePath, 240),
      rect: { x: Number(rect.x), y: Number(rect.y), width: Number(rect.width), height: Number(rect.height) },
      visible: row.visible === true, minimized: row.minimized === true, maximized: row.maximized === true,
      fields: { window_id: 'Windows 窗口句柄', process_id: '所属进程编号', title: '窗口标题，已脱敏',
        class_name: '窗口类名', executable_path: '程序路径，已截断并脱敏', rect: '屏幕坐标边界',
        visible: '窗口可见状态', minimized: '最小化状态', maximized: '最大化状态' } }
  })
}

function normalizeActionArguments(name: WindowActionName, value: unknown): Record<string, unknown> {
  const row = asObject(value)
  if (!row) throw new Error('arguments（动作参数）必须是对象。')
  const requireFields = (allowed: string[], required: string[]) => {
    if (Object.keys(row).some(key => !allowed.includes(key)) || required.some(key => !(key in row))) {
      throw new Error(`${name} 动作参数必须严格包含允许字段：${allowed.join('、')}。`)
    }
  }
  const integer = (key: string, minimum = -2_147_483_648, maximum = 2_147_483_647) => {
    if (typeof row[key] !== 'number' || !Number.isSafeInteger(row[key]) || Number(row[key]) < minimum || Number(row[key]) > maximum) {
      throw new Error(`${key} 必须是有效整数。`)
    }
  }
  switch (name) {
    case 'click': {
      requireFields(['elementId', 'x', 'y', 'button', 'selectionMode'], [])
      const byElement = typeof row.elementId === 'string' && !!row.elementId
      const byPoint = row.x !== undefined && row.y !== undefined
      if (byElement === byPoint || (row.elementId !== undefined && !byElement)) throw new Error('点击必须指定 elementId（元素编号）或 x/y 坐标之一。')
      if (byPoint) { integer('x'); integer('y') }
      if (row.button !== undefined && row.button !== 'left' && row.button !== 'right') throw new Error('button 只允许 left 或 right。')
      if (row.selectionMode !== undefined && !['select', 'add', 'remove', 'toggle'].includes(String(row.selectionMode))) {
        throw new Error('selectionMode（选择模式）无效。')
      }
      return { ...row, button: row.button ?? 'left' }
    }
    case 'type':
      requireFields(['elementId', 'text'], ['elementId', 'text'])
      if (typeof row.elementId !== 'string' || !row.elementId || typeof row.text !== 'string' || row.text.length > 10_000) {
        throw new Error('输入操作要求有效元素编号和不超过 10000 字符的文本。')
      }
      return { elementId: row.elementId, text: row.text }
    case 'scroll':
      requireFields(['elementId', 'direction', 'amount'], ['direction'])
      if (row.elementId !== undefined && (typeof row.elementId !== 'string' || !row.elementId)) throw new Error('elementId 无效。')
      if (!['up', 'down', 'page-up', 'page-down'].includes(String(row.direction))) throw new Error('滚动方向无效。')
      if (row.amount !== undefined) integer('amount', 1, 100)
      return { ...row, amount: row.amount ?? 3 }
    case 'key':
      requireFields(['keys'], ['keys'])
      if (typeof row.keys !== 'string' || !row.keys.trim() || row.keys.length > 256) throw new Error('按键组合无效。')
      return { keys: row.keys }
    case 'move': {
      requireFields(['elementId', 'x', 'y', 'drag'], [])
      const byElement = typeof row.elementId === 'string' && !!row.elementId
      const byPoint = row.x !== undefined && row.y !== undefined
      if (byElement === byPoint || (row.elementId !== undefined && !byElement)) throw new Error('移动必须指定 elementId 或 x/y 坐标之一。')
      if (byPoint) { integer('x'); integer('y') }
      if (row.drag !== undefined) {
        const drag = asObject(row.drag)
        if (!drag || Object.keys(drag).some(key => !['toElementId', 'toX', 'toY'].includes(key))) throw new Error('拖动目标无效。')
        if (drag.toElementId !== undefined && (typeof drag.toElementId !== 'string' || !drag.toElementId)) throw new Error('toElementId 无效。')
        const toElement = typeof drag.toElementId === 'string'
        const toPoint = drag.toX !== undefined && drag.toY !== undefined
        if (toElement === toPoint || (drag.toX !== undefined || drag.toY !== undefined) && !toPoint) throw new Error('拖动目标必须是元素编号或 toX/toY 坐标之一。')
        if (toPoint) {
          if (typeof drag.toX !== 'number' || !Number.isSafeInteger(drag.toX)
            || typeof drag.toY !== 'number' || !Number.isSafeInteger(drag.toY)) throw new Error('拖动坐标无效。')
        }
      }
      return { ...row }
    }
    case 'window_control':
      requireFields(['action', 'x', 'y', 'width', 'height'], ['action'])
      if (!['minimize', 'maximize', 'restore', 'move', 'resize', 'close'].includes(String(row.action))) throw new Error('窗口动作无效。')
      if (row.action === 'move') { integer('x'); integer('y') }
      if (row.action === 'resize') { integer('width', 1); integer('height', 1) }
      return { ...row }
    case 'invoke':
      requireFields(['elementId', 'pattern', 'value'], ['elementId', 'pattern'])
      if (typeof row.elementId !== 'string' || !row.elementId
        || !['invoke', 'toggle', 'expand', 'collapse', 'select', 'addToSelection', 'removeFromSelection',
          'scrollIntoView', 'setValue', 'setRangeValue'].includes(String(row.pattern))) throw new Error('UI Automation 模式或元素编号无效。')
      if (row.pattern === 'setValue' && typeof row.value !== 'string') throw new Error('setValue 模式要求文本 value。')
      if (row.pattern === 'setRangeValue' && (typeof row.value !== 'number' || !Number.isFinite(row.value))) throw new Error('setRangeValue 模式要求有限数值。')
      return { ...row }
    case 'multi_action': {
      requireFields(['steps', 'continueOnError'], ['steps'])
      if (!Array.isArray(row.steps) || row.steps.length < 1 || row.steps.length > 32
        || (row.continueOnError !== undefined && typeof row.continueOnError !== 'boolean')) throw new Error('批量动作必须包含 1 至 32 步。')
      const steps = row.steps.map((value, index) => {
        const step = asObject(value)
        if (!step || (step.kind !== 'click' && step.kind !== 'type')) throw new Error(`批量动作第 ${index + 1} 步类型无效。`)
        if (step.kind === 'click') return { kind: 'click', ...normalizeActionArguments('click', Object.fromEntries(Object.entries(step).filter(([key]) => key !== 'kind'))) }
        return { kind: 'type', ...normalizeActionArguments('type', Object.fromEntries(Object.entries(step).filter(([key]) => key !== 'kind'))) }
      })
      return { steps, continueOnError: row.continueOnError === true }
    }
  }
}

function helperAction(action: WindowAction, windowId: number): { operation: string; arguments: Record<string, unknown> } {
  const input = action.arguments
  const request = { ...input, windowId, hardware: false }
  switch (action.name) {
    case 'click': return { operation: 'click', arguments: { request, focusFallback: false } }
    case 'type': return { operation: 'type', arguments: { request: { ...request, rollback: true }, focusFallback: false } }
    case 'scroll': return { operation: 'scroll', arguments: { request, focusFallback: false } }
    case 'key': return { operation: 'key', arguments: { request, focusFallback: false } }
    case 'move': return { operation: 'move', arguments: { request, focusFallback: false } }
    case 'window_control': return { operation: 'windowControl', arguments: { request, focusFallback: false } }
    case 'invoke': return { operation: 'invokePattern', arguments: { request, focusFallback: false } }
    case 'multi_action': return { operation: 'multi_action', arguments: {
      window_id: windowId, steps: input.steps, continue_on_error: input.continueOnError === true,
    } }
  }
}

/** installDeviceTools（登记设备工具）：只对已绑定根代理提供设备清单、观察和经审批动作。 */
export function installDeviceTools(agentCtx: Context, binding: DeviceToolBinding, callback: DeviceToolCallback,
  receiveAttachment: DeviceAttachmentCallback): void {
  const observations = new ObservationStore(OBSERVATION_CONFIG)
  const targets = new Map<string, DeviceTarget>()
  const ensure = (exec: ToolRunContext): string => {
    const rootId = binding.handle?.agent?.id
    if (binding.state !== 'active' || !binding.activationId || !rootId || exec.agent?.id !== rootId) {
      throw new Error('设备工具只允许当前绑定的根代理调用。')
    }
    return binding.activationId
  }
  const callbackValues = async (exec: ToolRunContext, activationId: string,
    values: Record<string, Json>, requestId: string): Promise<Record<string, Json>> => {
    await binding.activationReady
    if (ensure(exec) !== activationId) throw new Error('设备操作所属激活已更换。')
    return {
      request_id: requestId, voice_session_id: binding.voiceSessionId, dsh_session_id: binding.dshSessionId,
      epoch: binding.epoch, context_revision: binding.revision, activation_id: activationId, ...values,
    }
  }
  const post = async (path: string, exec: ToolRunContext, activationId: string,
    values: Record<string, Json>, requestId: string = randomUUID(), signal = exec.signal): Promise<Record<string, Json>> => {
    const result = await callback(path, await callbackValues(exec, activationId, values, requestId), signal)
    if (ensure(exec) !== activationId) throw new Error('设备操作返回时激活已失效。')
    return result
  }
  const rememberTargets = (devices: unknown) => {
    if (!Array.isArray(devices)) return
    for (const item of devices) {
      const row = asObject(item)
      if (row?.connected === true) {
        try { const target = targetOf(row); targets.set(target.deviceId, target) } catch { /* 无效设备不进入缓存。 */ }
      }
    }
  }
  const actionBackend = (exec: ToolRunContext, activationId: string): DeviceActionBackend => ({
    currentTarget: (deviceId: string) => targets.get(deviceId),
    async observe(record: ObservationRecord, signal: AbortSignal): Promise<WindowSnapshot> {
      if (signal.aborted) throw new DeviceActionError('CANCELLED', '设备操作已取消。')
      const result = await post('/api/internal/dsh-bridge/device.observe', exec, activationId, {
        device_id: record.target.deviceId, window_id: record.windowId,
        max_elements: record.limits.maxElements, max_depth: record.limits.maxDepth,
        include_pixels: false, connection_generation: record.target.connectionGeneration,
        executor_generation: record.target.executorGeneration,
      }, randomUUID(), signal)
      const target = targetOf(result)
      targets.set(target.deviceId, target)
      if (target.deviceId !== record.target.deviceId
        || target.connectionGeneration !== record.target.connectionGeneration
        || target.executorGeneration !== record.target.executorGeneration) {
        throw new DeviceActionError('STALE_DEVICE', '设备连接或执行器代次已变化。')
      }
      return snapshotOf(result.snapshot)
    },
    async execute(action: WindowAction, observation: ObservationRecord, target: DeviceTarget,
      signal: AbortSignal): Promise<unknown> {
      const helper = helperAction(action, observation.windowId)
      const result = await post('/api/internal/dsh-bridge/device.action', exec, activationId, {
        device_id: target.deviceId, connection_generation: target.connectionGeneration,
        executor_generation: target.executorGeneration, operation: helper.operation,
        arguments: helper.arguments as Json, timeout_ms: 30_000,
      }, action.requestId, signal)
      const task = asObject(result.task)
      if (!task || task.request_id !== action.requestId || typeof task.status !== 'string') {
        throw new DeviceActionError('OUTCOME_UNKNOWN', '设备任务回执无效，请查询原任务。')
      }
      if (task.status === 'unknown') throw new DeviceActionError('OUTCOME_UNKNOWN', '设备任务结果未知，请使用 device_task_query 查询原任务。')
      return task
    },
  })

  agentCtx.tools.register(defineTool({
    name: 'device_list',
    description: '列出核心主机当前登记设备、能力和活跃语音交互设备；local（本机别名）表示核心主机本机。',
    parameters: {},
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).length) {
        throw new Error('设备列表不接受参数。')
      }
      const activationId = ensure(exec)
      const result = await post('/api/internal/dsh-bridge/device.list', exec, activationId, {})
      if (result.result !== 'accepted' || !Array.isArray(result.devices)) throw new Error('设备列表返回无效。')
      rememberTargets(result.devices)
      return result
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_windows',
    description: '列出明确选择设备上的可见窗口、标题和所属程序；窗口内容读取请使用 device_observe。',
    parameters: { device_id: { type: 'string', description: '固定执行设备编号；请先用 device_list 查看。', required: true } },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      const row = exactArgs(args, ['device_id'])
      if (typeof row.device_id !== 'string') throw new Error('device_id（设备编号）必须为文本。')
      const activationId = ensure(exec)
      const result = await post('/api/internal/dsh-bridge/device.windows', exec, activationId,
        { device_id: row.device_id })
      const target = targetOf(result)
      targets.set(target.deviceId, target)
      return { result: 'accepted', target: { device_id: target.deviceId,
        connection_generation: target.connectionGeneration, executor_generation: target.executorGeneration },
        windows: visibleWindows(result.windows),
        fields: { target: '固定设备与连接、执行器代次', windows: '窗口清单；标题与程序路径已截断或脱敏' } }
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_observe',
    description: '明确选择设备和窗口，读取有界 UI Automation（界面自动化）树；返回 observation_id（观察编号）供后续动作引用。',
    parameters: {
      device_id: { type: 'string', description: '固定执行设备编号；必须明确指定。', required: true },
      window_id: { type: 'integer', description: '来自 device_windows 的 Windows 窗口句柄。', required: true },
      max_elements: { type: 'integer', description: '界面树元素数量上限，1 至 100；默认 100。' },
      max_depth: { type: 'integer', description: '界面树递归深度上限，1 至 16；默认 16。' },
    },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      const row = asObject(args)
      const allowed = ['device_id', 'window_id', 'max_elements', 'max_depth']
      if (!row || Object.keys(row).some(key => !allowed.includes(key)) || typeof row.device_id !== 'string'
        || typeof row.window_id !== 'number' || !Number.isSafeInteger(row.window_id)) throw new Error('观察设备编号或窗口句柄无效。')
      const activationId = ensure(exec)
      const limits = { maxElements: row.max_elements === undefined ? 100 : Number(row.max_elements),
        maxDepth: row.max_depth === undefined ? 16 : Number(row.max_depth) }
      const result = await post('/api/internal/dsh-bridge/device.observe', exec, activationId, {
        device_id: row.device_id, window_id: Number(row.window_id),
        max_elements: limits.maxElements, max_depth: limits.maxDepth, include_pixels: false,
      })
      const target = targetOf(result)
      const snapshot = snapshotOf(result.snapshot)
      const record = observations.record(snapshot, target, limits)
      targets.set(target.deviceId, target)
      return { result: 'accepted', observation_id: record.id, device_id: target.deviceId,
        connection_generation: target.connectionGeneration, executor_generation: target.executorGeneration,
        snapshot: visibleSnapshot(snapshot), elements: visibleElements(result.elements),
        fields: { observation_id: '短期观察编号；动作必须引用该观察', device_id: '观察目标设备编号',
          connection_generation: '观察时设备连接代次', executor_generation: '观察时桌面执行器代次',
          snapshot: '观察时窗口身份、矩形及内容摘要', elements: '脱敏后的界面控件树' } }
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_control',
    description: '引用 device_observe 返回的 observation_id，在窗口和进程身份复核后提交一次设备动作；每次动作均走现有 DSH 用户审批，断线或回执丢失不会自动重放。',
    parameters: {
      device_id: { type: 'string', description: '观察中固定的设备编号。', required: true },
      based_on: { type: 'string', description: 'device_observe 返回的 observation_id（观察编号）。', required: true },
      action: { type: 'string', enum: [...ACTION_NAMES], description: '窗口动作类型。', required: true },
      arguments: { type: 'object', description: '与动作类型匹配的窗口或控件参数；工具执行器会按动作类型严格校验字段。', additionalProperties: true, required: true },
    },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      const row = exactArgs(args, ['device_id', 'based_on', 'action', 'arguments'])
      if (typeof row.device_id !== 'string' || typeof row.based_on !== 'string'
        || typeof row.action !== 'string' || !ACTION_NAMES.has(row.action as WindowActionName)
        || !asObject(row.arguments)) throw new Error('设备动作参数无效。')
      const activationId = ensure(exec)
      const name = row.action as WindowActionName
      const argsForAction = normalizeActionArguments(name, row.arguments)
      // 辅助进程按元素编号重新定位，纯窗口动作只依赖窗口身份。
      const checkContent = name !== 'window_control' && (Array.isArray(argsForAction.steps)
        ? argsForAction.steps.some(step => typeof asObject(step)?.elementId !== 'string')
        : typeof argsForAction.elementId !== 'string' || asObject(argsForAction.drag)?.toX !== undefined)
      const action: WindowAction = { requestId: randomUUID(), basedOn: row.based_on, deviceId: row.device_id,
        name, arguments: argsForAction, checkContent }
      const approval = agentCtx.get('approval') as ApprovalService | undefined
      const executor = new DeviceActionExecutor(observations, actionBackend(exec, activationId), {
        async approve(candidate, observation, signal) {
          if (!approval || !exec.agent) return false
          const outcome = await approval.request({
            agent: exec.agent, toolName: 'device_control', callId: exec.callId,
            reason: `请求在设备 ${candidate.deviceId} 的窗口「${sanitizeVisible(observation.title, 160)}」执行 ${candidate.name}；任务编号 ${candidate.requestId}。`,
            signal,
          })
          return outcome === 'allowed-once'
        },
      })
      const result = await executor.execute(action, exec.signal)
      return { result: 'accepted', action: action.name, device_id: action.deviceId,
        request_id: action.requestId, outcome: result as Json,
        fields: { action: '设备动作名称', device_id: '固定执行设备编号', request_id: '持久原任务编号',
          outcome: '已审批并复核的设备任务结果；unknown（未知）必须查询原任务' } }
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_task_query',
    description: '只读查询窗口动作、终端或文件的原任务台账，设备断线时也可查询；不会重新执行。',
    parameters: {
      device_id: { type: 'string', description: '原任务固定目标设备编号。', required: true },
      task_id: { type: 'string', description: '工具回执或提示中的原任务编号。', required: true },
    },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      const row = exactArgs(args, ['device_id', 'task_id'])
      if (typeof row.device_id !== 'string' || typeof row.task_id !== 'string') throw new Error('设备编号或原任务编号无效。')
      const activationId = ensure(exec)
      const result = await post('/api/internal/dsh-bridge/device.task.query', exec, activationId,
        { device_id: row.device_id, task_id: row.task_id })
      return result
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_terminal',
    description: '在明确设备上创建受管命令会话，输入固定只读命令配置，并读取增量输出、查询状态、取消或关闭；不提供任意 shell、PowerShell 或脚本入口。输入命令需每次获得 DSH 用户的一次性批准。',
    parameters: {
      action: { type: 'string', enum: ['create', 'input', 'output', 'status', 'cancel', 'close'],
        description: '受管终端操作：create（创建）、input（输入固定只读命令配置）、output（取增量输出）、status（查询）、cancel（取消）、close（关闭）。', required: true },
      device_id: { type: 'string', description: '固定执行设备编号；必须明确指定。', required: true },
      terminal_id: { type: 'string', description: 'create 之外操作所需的设备终端会话编号。' },
      command_id: { type: 'string', enum: ['hostname', 'identity', 'processes', 'network'],
        description: 'input 时必须选择固定只读命令：hostname（主机名）、identity（当前账户）、processes（进程清单）、network（网络接口信息）。' },
      offset: { type: 'integer', description: 'output 的输出字节偏移；首次读取使用 0，后续使用返回的 next_offset（下一偏移）。' },
    },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => [{ type: 'text' as const, text: JSON.stringify(value) }] },
    async execute(args, exec: ToolRunContext) {
      const row = asObject(args)
      if (!row || Object.keys(row).some(key => !['action', 'device_id', 'terminal_id', 'command_id', 'offset'].includes(key))
        || typeof row.action !== 'string' || !['create', 'input', 'output', 'status', 'cancel', 'close'].includes(row.action)
        || typeof row.device_id !== 'string' || !row.device_id) throw new Error('设备终端参数无效。')
      const action = row.action as 'create' | 'input' | 'output' | 'status' | 'cancel' | 'close'
      if (action === 'create') {
        if ('terminal_id' in row || 'command_id' in row || 'offset' in row) throw new Error('create 只接受 action 和 device_id。')
      } else if (typeof row.terminal_id !== 'string' || !row.terminal_id) {
        throw new Error('terminal_id（终端会话编号）必填。')
      }
      if (action === 'input') {
        if (typeof row.command_id !== 'string' || !['hostname', 'identity', 'processes', 'network'].includes(row.command_id)
          || 'offset' in row) throw new Error('input 必须选择一个固定只读命令配置。')
      } else if ('command_id' in row) throw new Error('command_id 只允许用于 input 操作。')
      if (action === 'output') {
        if (typeof row.offset !== 'number' || !Number.isSafeInteger(row.offset) || row.offset < 0) {
          throw new Error('output 必须提供非负整数 offset。')
        }
      } else if ('offset' in row) throw new Error('offset 只允许用于 output 操作。')

      const activationId = ensure(exec)
      const terminalId = action === 'create' ? randomUUID() : String(row.terminal_id)
      const requestId = randomUUID()
      if (action === 'input') {
        const approval = agentCtx.get('approval') as ApprovalService | undefined
        if (!approval || !exec.agent) throw new Error('设备终端命令没有可用的 DSH 一次性审批服务。')
        const outcome = await approval.request({ agent: exec.agent, toolName: 'device_terminal', callId: exec.callId,
          reason: `请求在设备 ${row.device_id} 的受管终端会话 ${terminalId} 中输入固定只读命令 ${row.command_id}；原任务编号 ${requestId}。`,
          signal: exec.signal })
        if (outcome !== 'allowed-once') throw new Error('设备终端命令未获一次性批准。')
      }
      const values: Record<string, Json> = { device_id: row.device_id, action, terminal_id: terminalId }
      if (action === 'input') values.command_id = row.command_id as string
      if (action === 'output') values.offset = row.offset as number
      let result: Record<string, Json>
      try {
        result = await post('/api/internal/dsh-bridge/device.terminal', exec, activationId, values, requestId)
      } catch (error) {
        if (error instanceof Error && error.message.includes(requestId)) throw error
        const detail = error instanceof Error ? error.message : '未取得设备终端回执。'
        throw new Error(`${detail} 原任务编号 ${requestId}，请查询原任务确认状态。`, { cause: error })
      }
      const task = asObject(result.task)
      if (!task || task.request_id !== requestId || task.device_id !== row.device_id || typeof task.status !== 'string') {
        throw new Error(`设备终端原任务回执无效，请查询原任务 ${requestId}，不要重放。`)
      }
      if (task.status === 'unknown') throw new Error(`设备终端结果未知；请用 device_task_query 查询原任务 ${requestId}，不要重放。`)
      return { result: 'accepted', action, device_id: row.device_id, terminal_id: terminalId,
        request_id: requestId, task: task as Json,
        fields: { action: '受管终端操作名称', device_id: '固定执行设备编号', terminal_id: '绑定连接与执行器代次的终端会话编号',
          request_id: '可通过 device_task_query 查询的原任务编号', task: '原设备终端会话/命令回执；未知结果必须查询且不能重放' } }
    },
  }))

  agentCtx.tools.register(defineTool({
    name: 'device_file',
    description: '在明确设备上列出目录、读取文件或写入已存在的 DSH 文件附件；每次操作都需 DSH 用户一次性批准，写入不覆盖目标。大文件经摘要校验后使用独立 SFTP 文件任务和有界附件流。',
    parameters: {
      action: { type: 'string', enum: ['list', 'read', 'write'], description: '文件操作：list（列出目录）、read（读取文件）、write（写入已登记的 DSH 文件附件）。', required: true },
      device_id: { type: 'string', description: '固定执行设备编号；必须明确指定。', required: true },
      path: { type: 'string', description: '设备上的绝对文件或目录路径；显示给用户审批；write 目标必须尚不存在。', required: true },
      limit: { type: 'integer', description: 'list 的最多目录项数，1 至 200，默认 100。' },
      max_bytes: { type: 'integer', description: 'read 的文件字节上限，1 至 4294967296；默认 131072。' },
      attachment: { type: 'object', description: 'write 时使用的 FileAttachmentRef（DSH 已登记文件附件引用），字段为 attachmentId、name、bytes；不接受本机路径或 Base64。',
        properties: {
          attachmentId: { type: 'string', description: 'attachmentId（内容寻址附件编号）：sha256: 前缀加 64 位小写摘要。', required: true },
          name: { type: 'string', description: 'name（附件显示名称）：只作为名称，不作为路径。', required: true },
          bytes: { type: 'integer', description: 'bytes（附件字节数）：必须与 DSH 附件存储中的原始文件一致。', required: true },
        }, additionalProperties: false },
    },
    output: { schema: { type: 'object' as const, additionalProperties: true as const },
      render: (_args: unknown, value: Record<string, Json>) => contentBlocks(value) },
    async execute(args, exec: ToolRunContext) {
      const row = asObject(args)
      if (!row || Object.keys(row).some(key => !['action', 'device_id', 'path', 'limit', 'max_bytes', 'attachment'].includes(key))
        || typeof row.action !== 'string' || !['list', 'read', 'write'].includes(row.action)
        || typeof row.device_id !== 'string' || !row.device_id
        || typeof row.path !== 'string' || !row.path || row.path.length > 2048 || /[\u0000-\u001f]/u.test(row.path)) {
        throw new Error('设备文件参数无效。')
      }
      const action = row.action as 'list' | 'read' | 'write'
      let fileRef: FileAttachmentRef | undefined
      if (action === 'list') {
        if ('max_bytes' in row || 'attachment' in row || row.limit !== undefined
          && (typeof row.limit !== 'number' || !Number.isSafeInteger(row.limit) || row.limit < 1 || row.limit > 200)) {
          throw new Error('list 只接受 1 至 200 的目录项上限。')
        }
      } else if (action === 'read') {
        if ('limit' in row || 'attachment' in row || row.max_bytes !== undefined
          && (typeof row.max_bytes !== 'number' || !Number.isSafeInteger(row.max_bytes) || row.max_bytes < 1 || row.max_bytes > 4 * 1024 * 1024 * 1024)) {
          throw new Error('read 只接受 1 至 4 GiB 的字节上限。')
        }
      } else {
        const attachment = asObject(row.attachment)
        if ('limit' in row || 'max_bytes' in row || !attachment
          || Object.keys(attachment).some(key => !['attachmentId', 'name', 'bytes'].includes(key))
          || typeof attachment.attachmentId !== 'string' || !/^sha256:[0-9a-f]{64}$/u.test(attachment.attachmentId)
          || typeof attachment.name !== 'string' || !attachment.name || attachment.name.length > 240
          || typeof attachment.bytes !== 'number' || !Number.isSafeInteger(attachment.bytes)
          || attachment.bytes < 0 || attachment.bytes > 4 * 1024 * 1024 * 1024) {
          throw new Error('write 必须传入有效的 DSH FileAttachmentRef（附件引用），且附件不超过 4 GiB。')
        }
        fileRef = { attachmentId: AttachmentId(attachment.attachmentId), name: attachment.name, bytes: attachment.bytes }
      }
      const activationId = ensure(exec)
      const approval = agentCtx.get('approval') as ApprovalService | undefined
      if (!approval || !exec.agent) throw new Error('设备文件访问没有可用的 DSH 一次性审批服务。')
      const requestId = randomUUID()
      const attachmentHash = fileRef?.attachmentId.slice('sha256:'.length)
      const outcome = await approval.request({ agent: exec.agent, toolName: 'device_file', callId: exec.callId,
        reason: action === 'write'
          ? `请求在设备 ${row.device_id} 新建文件「${sanitizePath(row.path, 240)}」，内容来自附件 ${fileRef!.name}（${fileRef!.bytes} 字节，SHA-256 ${attachmentHash}）；已存在目标不会覆盖；原任务编号 ${requestId}。`
          : `请求在设备 ${row.device_id} 上${action === 'list' ? '列出目录' : '读取文件'}「${sanitizePath(row.path, 240)}」${action === 'read' ? `，最多读取 ${row.max_bytes ?? 128 * 1024} 字节` : ''}；原任务编号 ${requestId}。`,
        signal: exec.signal })
      if (outcome !== 'allowed-once') throw new Error('设备文件访问未获一次性批准。')
      const values: Record<string, Json> = { device_id: row.device_id, action, path: row.path }
      if (action === 'list') values.limit = (row.limit as number | undefined) ?? 100
      else if (action === 'read') values.max_bytes = (row.max_bytes as number | undefined) ?? 128 * 1024
      else values.attachment = { attachment_id: fileRef!.attachmentId, name: fileRef!.name, bytes: fileRef!.bytes }
      const response = await post('/api/internal/dsh-bridge/device.file', exec, activationId, values, requestId)
      const task = asObject(response.task)
      if (!task || task.request_id !== requestId || task.device_id !== row.device_id || typeof task.status !== 'string') {
        throw new Error(`设备文件原任务回执无效；请使用 device_task_query 查询 ${requestId}。`)
      }
      if (task.status === 'unknown') {
        const sourceTaskId = typeof task.source_task_id === 'string' ? task.source_task_id : requestId
        throw new Error(`设备文件结果未知；请使用 device_task_query 查询原任务 ${sourceTaskId}，不要重放。`)
      }
      if (action === 'write') {
        const ticket = asObject(task.result)
        if (task.status !== 'receiving' || !ticket || ticket.path !== row.path
          || ticket.bytes !== fileRef!.bytes || ticket.sha256 !== attachmentHash
          || typeof ticket.transfer_id !== 'string' || !/^[0-9a-f]{32}$/u.test(ticket.transfer_id)
          || typeof ticket.connection_generation !== 'number' || !Number.isSafeInteger(ticket.connection_generation)
          || typeof ticket.executor_generation !== 'number' || !Number.isSafeInteger(ticket.executor_generation)) {
          throw new Error('设备文件暂存票据与已审批附件不匹配。')
        }
        if (!binding.attachments) throw new Error('DSH 原生文件附件服务未挂载。')
        const transferCall = (transferAction: string, extras: Record<string, Json>, phase: string,
          signal: AbortSignal = exec.signal) => post('/api/internal/dsh-bridge/device.file.transfer', exec, activationId, {
          device_id: row.device_id as string, connection_generation: ticket.connection_generation as number,
          executor_generation: ticket.executor_generation as number, parent_request_id: requestId,
          transfer_id: ticket.transfer_id as string, action: transferAction, ...extras,
        }, `${requestId.slice(0, 80)}:${phase}:${createHash('sha256').update(requestId).digest('hex').slice(0, 8)}`, signal)
        let keepTicket = false
        try {
          let offset = 0
          const digest = createHash('sha256')
          for await (const sourceChunk of binding.attachments.readFileStream(fileRef!, exec.signal)) {
            for (let position = 0; position < sourceChunk.byteLength;) {
              const chunk = sourceChunk.subarray(position, Math.min(position + 64 * 1024, sourceChunk.byteLength))
              const encoded = Buffer.from(chunk).toString('base64')
              const part = await transferCall('upload_chunk', { offset, data_base64: encoded }, `part-${offset}`)
              const receipt = asObject(part.transfer)
              if (receipt?.transfer_id !== ticket.transfer_id || receipt.next_offset !== offset + chunk.byteLength) {
                throw new Error('设备文件暂存分块回执顺序或编号无效。')
              }
              digest.update(chunk)
              offset += chunk.byteLength
              position += chunk.byteLength
            }
          }
          if (offset !== fileRef!.bytes || digest.digest('hex') !== attachmentHash) {
            throw new Error('DSH 附件流的总长度或 SHA-256 与已批准引用不一致。')
          }
          keepTicket = true
          const finished = await transferCall('finish_upload', { expected_bytes: fileRef!.bytes,
            expected_sha256: attachmentHash! }, 'finish')
          const writeTask = asObject(finished.task)
          if (!writeTask || writeTask.request_id !== requestId || writeTask.device_id !== row.device_id
            || typeof writeTask.status !== 'string') throw new Error('设备文件写入原任务回执无效；请查询原任务。')
          if (writeTask.status === 'unknown') {
            keepTicket = true
            const sourceTaskId = typeof writeTask.source_task_id === 'string' ? writeTask.source_task_id : requestId
            throw new Error(`设备文件写入结果未知；请使用 device_task_query 查询原任务 ${sourceTaskId}，不要重放。`)
          }
          keepTicket = false
          if (writeTask.status !== 'succeeded') throw new Error(`设备文件写入未完成：${requestId}。`)
          return { result: 'accepted', action, device_id: row.device_id, request_id: requestId,
            task: writeTask as Json, path: sanitizePath(row.path, 240), name: fileRef!.name,
            bytes: fileRef!.bytes, sha256: attachmentHash,
            fields: { action: '设备文件操作名称', device_id: '固定执行设备编号', request_id: '原文件写入任务编号',
              task: '新建文件任务回执；已验证附件摘要，现有目标不会覆盖', path: '已审批的绝对目标路径',
              bytes: '写入字节数', sha256: '与来源附件一致的 SHA-256 摘要' } }
        } finally {
          if (!keepTicket) {
            await transferCall('close', {}, 'close', new AbortController().signal).catch(() => undefined)
          }
        }
      }
      if (task.status !== 'succeeded') throw new Error(`设备文件任务未完成：${requestId}。`)
      const data = asObject(task.result)
      if (action === 'list') {
        if (!data || typeof data.path !== 'string' || data.path !== row.path
          || !Array.isArray(data.entries) || data.entries.length > 200) throw new Error('设备目录回执格式无效。')
        const entries = data.entries.map(value => {
          const entry = asObject(value)
          if (!entry || typeof entry.name !== 'string' || !['file', 'directory', 'link', 'other'].includes(String(entry.kind))
            || !(entry.bytes === null || typeof entry.bytes === 'number' && Number.isSafeInteger(entry.bytes) && entry.bytes >= 0)) {
            throw new Error('设备目录回执包含无效项。')
          }
          return { name: sanitizeVisible(entry.name, 240), kind: String(entry.kind),
            bytes: entry.bytes as Json, fields: { name: '名称，已截断并脱敏', kind: '文件、目录、链接或其他类型', bytes: '普通文件字节数；链接不会被跟随' } }
        })
        return { result: 'accepted', action, device_id: row.device_id, path: sanitizePath(data.path, 240),
          entries, truncated: data.truncated === true,
          fields: { action: '设备文件操作名称', device_id: '固定执行设备编号', path: '已校验的目标目录',
            entries: '链接不会被跟随的有界目录项', truncated: '目录项是否达到上限' } }
      }
      if (data?.streamed === true) {
        if (typeof data.transfer_id !== 'string' || !/^[0-9a-f]{32}$/u.test(data.transfer_id)
          || typeof data.name !== 'string' || typeof data.path !== 'string' || data.path !== row.path
          || typeof data.bytes !== 'number' || !Number.isSafeInteger(data.bytes) || data.bytes <= 128 * 1024
          || data.bytes > 4 * 1024 * 1024 * 1024 || typeof data.sha256 !== 'string' || !/^[0-9a-f]{64}$/u.test(data.sha256)
          || typeof data.connection_generation !== 'number' || !Number.isSafeInteger(data.connection_generation)
          || typeof data.executor_generation !== 'number' || !Number.isSafeInteger(data.executor_generation)) {
          throw new Error('设备大文件读取票据格式无效。')
        }
        const attachments = binding.attachments
        if (!attachments) throw new Error('DSH 原生文件附件服务未挂载。')
        const { bytes, sha256, name } = data
        const payload = await callbackValues(exec, activationId, {
          device_id: row.device_id, connection_generation: data.connection_generation,
          executor_generation: data.executor_generation, parent_request_id: requestId,
          transfer_id: data.transfer_id, action: 'read_stream',
        }, randomUUID())
        const file = await receiveAttachment('/api/internal/dsh-bridge/device.file.transfer', payload,
          async (body, signal) => {
            if (ensure(exec) !== activationId) throw new Error('设备文件读取所属激活已失效。')
            return attachments.saveFileStream({ data: body.verifiedStream(bytes, sha256), signal: signal ?? exec.signal, name })
          }, exec.signal)
        if (ensure(exec) !== activationId) throw new Error('设备文件返回时激活已失效。')
        if (file.attachmentId !== `sha256:${sha256}` || file.bytes !== bytes) {
          throw new Error('DSH 附件服务返回的内容标识与设备文件摘要不匹配。')
        }
        return { result: 'accepted', action, device_id: row.device_id, path: sanitizePath(data.path, 240),
          presentation: 'file', attachment: { attachmentId: file.attachmentId, name: file.name, bytes: file.bytes },
          bytes, sha256,
          fields: { action: '设备文件操作名称', device_id: '固定执行设备编号', path: '已校验的目标文件',
            presentation: '通过有界流式 DSH 原始文件附件服务返回', attachment: '按完整长度和摘要验证的文件附件引用',
            bytes: '原始字节数', sha256: '原始字节 SHA-256 摘要' } }
      }
      if (!data || typeof data.data_base64 !== 'string' || typeof data.sha256 !== 'string'
        || !/^[0-9a-f]{64}$/u.test(data.sha256) || typeof data.bytes !== 'number' || !Number.isSafeInteger(data.bytes)
        || typeof data.path !== 'string' || data.path !== row.path
        || typeof data.name !== 'string' || data.bytes < 0 || data.bytes > 128 * 1024) throw new Error('设备文件内容回执字段无效。')
      const encoded = Buffer.from(data.data_base64, 'base64')
      if (encoded.toString('base64') !== data.data_base64 || encoded.length !== data.bytes
        || createHash('sha256').update(encoded).digest('hex') !== data.sha256) throw new Error('设备文件内容摘要或长度不匹配。')
      if (looksLikeText(encoded) && encoded.length <= 32 * 1024 && !encoded.includes(0)) {
        return { result: 'accepted', action, device_id: row.device_id, path: sanitizePath(String(data.path), 240),
          presentation: 'text', bytes: encoded.length, sha256: data.sha256,
          text: encoded.toString('utf8'), fields: { action: '设备文件操作名称', device_id: '固定执行设备编号',
            path: '已校验的目标文件', presentation: '小型 UTF-8 文本以内联方式返回', bytes: '原始文件字节数', sha256: '原始字节 SHA-256 摘要', text: '已验证 UTF-8 的小文件正文' } }
      }
      if (!binding.attachments) throw new Error('DSH 原生文件附件服务未挂载。')
      const mediaType = detectImageMediaType(encoded)
      if (mediaType && encoded.length <= binding.attachments.imageLimits.maxImageBytes) {
        try {
          const images = await binding.attachments.saveImages([{ data: encoded, mediaType, name: String(data.name) }])
          const image = images[0]
          if (!image) throw new Error('DSH 图片附件服务未返回引用。')
          const attachment: Record<string, Json> = { attachmentId: image.attachmentId, mediaType: image.mediaType,
            bytes: image.bytes, width: image.width, height: image.height }
          if (image.name !== undefined) attachment.name = image.name
          return { result: 'accepted', action, device_id: row.device_id, path: sanitizePath(String(data.path), 240),
            presentation: 'image', attachment, bytes: encoded.length, sha256: data.sha256,
            fields: { action: '设备文件操作名称', device_id: '固定执行设备编号', path: '已校验的目标文件',
              presentation: '通过 DSH 图片附件服务返回', attachment: '已验证的原生图片附件引用', bytes: '原始文件字节数', sha256: '原始字节 SHA-256 摘要' } }
        } catch { /* 无法作为图像入库时按原始文件附件返回。 */ }
      }
      const file = await binding.attachments.saveFile({ data: encoded, name: String(data.name) })
      return { result: 'accepted', action, device_id: row.device_id, path: sanitizePath(String(data.path), 240),
        presentation: 'file', attachment: { attachmentId: file.attachmentId, name: file.name, bytes: file.bytes }, bytes: encoded.length, sha256: data.sha256,
        fields: { action: '设备文件操作名称', device_id: '固定执行设备编号', path: '已校验的目标文件',
          presentation: '通过 DSH 原始文件附件服务返回', attachment: '已验证的原生文件附件引用', bytes: '原始文件字节数', sha256: '原始字节 SHA-256 摘要' } }
    },
  }))
}
