export type DemoRuntimeState = 'offline' | 'standby' | 'listening' | 'thinking' | 'speaking' | 'error'
export type ReasoningMode = 'fast' | 'balanced' | 'deep'

export interface RuntimePresentation {
  label: string
  description: string
  latency: string
  pulse: string
  timestamp: string
}

export const runtimePresentation: Record<DemoRuntimeState, RuntimePresentation> = {
  offline: {
    label: '语音助手已关闭',
    description: '本机语音运行时未启动，不会采集或处理音频。',
    latency: '—',
    pulse: 'offline',
    timestamp: '等待启动',
  },
  standby: {
    label: '全天候待机',
    description: '正在等待唤醒词，所有语音处理仍在本机完成。',
    latency: '682 ms',
    pulse: 'standby',
    timestamp: '刚刚',
  },
  listening: {
    label: '正在聆听',
    description: '识别到语音输入，你可以自然地说完一句话。',
    latency: '682 ms',
    pulse: 'listening',
    timestamp: '现在',
  },
  thinking: {
    label: '正在理解',
    description: '已确认你的语音内容，正在组织下一步回答。',
    latency: '728 ms',
    pulse: 'thinking',
    timestamp: '现在',
  },
  speaking: {
    label: '正在回答',
    description: '正在实际播放回复；你可以随时说话打断我。',
    latency: '728 ms',
    pulse: 'speaking',
    timestamp: '现在',
  },
  error: {
    label: '需要注意',
    description: '语音服务暂时不可用。检查设备或稍后重新启动助手。',
    latency: '—',
    pulse: 'error',
    timestamp: '连接中断',
  },
}

export const stateChoices: Array<{ value: DemoRuntimeState; label: string }> = [
  { value: 'offline', label: '关闭' },
  { value: 'standby', label: '待机' },
  { value: 'listening', label: '聆听' },
  { value: 'thinking', label: '理解' },
  { value: 'speaking', label: '播报' },
  { value: 'error', label: '异常' },
]

export const primaryPersonaDocuments = [
  {
    id: 'instructions',
    label: '交互指令',
    path: 'config/persona/instructions.md',
    value: '# 交互指令\n\n回答自然、简洁。用户插话时立即停止当前表达，优先理解新的内容。',
  },
  {
    id: 'personality',
    label: '助手人格',
    path: 'config/persona/personality.md',
    value: '# 助手人格\n\n冷静、可靠、有分寸。需要主动推进时，先说明正在做什么。',
  },
  {
    id: 'user',
    label: '用户资料',
    path: 'config/persona/user.md',
    value: '# 用户资料\n\n默认使用中文交流；工作时间内优先给出可执行的结论。',
  },
  {
    id: 'shadow',
    label: 'Shadow 指令',
    path: 'config/shadow_agent/instructions.md',
    value: '# Shadow Agent 执行规则\n\n从权威会话上下文理解用户意图。仅执行只读工具，并持续给出简洁进度。',
  },
]

export type DemoModelCapability = 'llm' | 'tts' | 'local_kws' | 'local_asr' | 'local_vad' | 'local_analysis_endpoint' | 'local_analysis_refine'
export type DemoModelOrigin = 'runtime_config' | 'product_managed' | 'local_scan'
export type DemoModelStatus = 'verified' | 'inherited' | 'healthy' | 'needs_check'
export type DemoReasoningEffort = 'off' | 'low' | 'medium' | 'high' | 'max'
export type DemoContextTokens = 131072 | 262144 | 524288 | 1048576

export const modelContextOptions: Array<{ value: DemoContextTokens; label: string }> = [
  { value: 131072, label: '128K' },
  { value: 262144, label: '256K' },
  { value: 524288, label: '512K' },
  { value: 1048576, label: '1M' },
]

export const reasoningEffortOptions: Array<{ value: DemoReasoningEffort; label: string; shortLabel: string }> = [
  { value: 'off', label: '关闭', shortLabel: 'Off' },
  { value: 'low', label: '低', shortLabel: 'Low' },
  { value: 'medium', label: '中', shortLabel: 'Medium' },
  { value: 'high', label: '高', shortLabel: 'High' },
  { value: 'max', label: '最大', shortLabel: 'Max' },
]

export interface DemoModelEntry {
  id: string
  name: string
  modelId: string
  capability: DemoModelCapability
  origin: DemoModelOrigin
  provider: string
  status: DemoModelStatus
  /** API model policy kept on the pool entry, not on a runtime assignment. */
  maxContextTokens?: DemoContextTokens
  allowedReasoning?: DemoReasoningEffort[]
  path?: string
  hidden?: boolean
}

export const demoModelEntries: DemoModelEntry[] = [
  { id: 'runtime-voice-llm', name: 'DeepSeek Flash', modelId: 'deepseek-flash', capability: 'llm', origin: 'runtime_config', provider: 'DeepSeek', status: 'inherited', maxContextTokens: 131072, allowedReasoning: ['off', 'low', 'medium', 'high', 'max'] },
  { id: 'runtime-shadow-llm', name: 'Shadow · DeepSeek Flash', modelId: 'deepseek-flash', capability: 'llm', origin: 'runtime_config', provider: '继承主 LLM', status: 'inherited', maxContextTokens: 262144, allowedReasoning: ['low', 'medium', 'high', 'max'] },
  { id: 'managed-reasoning', name: '深度分析模型', modelId: 'deepseek-reasoner', capability: 'llm', origin: 'product_managed', provider: 'DeepSeek', status: 'verified', maxContextTokens: 524288, allowedReasoning: ['low', 'medium', 'high', 'max'] },
  { id: 'managed-qwen', name: 'Qwen Turbo', modelId: 'qwen-turbo', capability: 'llm', origin: 'product_managed', provider: 'Qwen', status: 'verified', maxContextTokens: 262144, allowedReasoning: ['off', 'low', 'medium', 'high'] },
  { id: 'runtime-tts', name: 'MiniMax Speech 2.8 HD', modelId: 'speech-2.8-hd', capability: 'tts', origin: 'runtime_config', provider: 'MiniMax', status: 'inherited' },
  { id: 'local-kws', name: 'Sherpa KWS Zipformer', modelId: 'sherpa-onnx-kws-zipformer-zh-en-3M', capability: 'local_kws', origin: 'local_scan', provider: '受管目录', status: 'healthy', path: 'model_dir/kws/sherpa-onnx-kws-zipformer-zh-en-3M' },
  { id: 'local-asr', name: 'Paraformer Streaming', modelId: 'paraformer-zh-streaming', capability: 'local_asr', origin: 'local_scan', provider: '受管目录', status: 'healthy', path: 'model_dir/funasr/paraformer-zh-streaming' },
  { id: 'local-vad', name: 'FSMN-VAD', modelId: 'fsmn-vad', capability: 'local_vad', origin: 'local_scan', provider: '受管目录', status: 'healthy', path: 'model_dir/funasr/fsmn-vad' },
  { id: 'local-smart-turn', name: 'Smart Turn V3.2', modelId: 'smart-turn-v3.2-cpu', capability: 'local_analysis_endpoint', origin: 'local_scan', provider: '受管目录', status: 'healthy', path: 'model_dir/analysis/smart-turn' },
  { id: 'local-sensevoice', name: 'SenseVoice Small', modelId: 'sensevoice-small', capability: 'local_analysis_refine', origin: 'local_scan', provider: '受管目录', status: 'healthy', path: 'model_dir/analysis/sensevoice' },
]

export const capabilityLabels: Record<DemoModelCapability, string> = {
  llm: '文字 LLM', tts: 'TTS', local_kws: '本地 KWS', local_asr: '本地 ASR', local_vad: '本地 VAD', local_analysis_endpoint: '本地分析 · 端点', local_analysis_refine: '本地分析 · 精修',
}

export const originLabels: Record<DemoModelOrigin, string> = {
  runtime_config: '现有运行时配置', product_managed: '产品托管', local_scan: '受管目录扫描',
}
