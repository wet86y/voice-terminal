export type DemoRuntimeState = 'offline' | 'standby' | 'listening' | 'thinking' | 'speaking' | 'error'
export type ReasoningMode = 'fast' | 'balanced' | 'deep'

export interface RuntimePresentation {
  /** label（状态名称）。 */
  label: string
  /** description（状态说明）。 */
  description: string
  /** latency（展示延迟）。 */
  latency: string
  /** pulse（动画状态）。 */
  pulse: string
  /** timestamp（展示时间）。 */
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
  /** id（展示条目标识）。 */
  id: string
  /** name（模型显示名称）。 */
  name: string
  /** modelId（模型标识）。 */
  modelId: string
  /** capability（模型能力）。 */
  capability: DemoModelCapability
  /** origin（条目来源）。 */
  origin: DemoModelOrigin
  /** provider（服务商名称）。 */
  provider: string
  /** status（展示状态）。 */
  status: DemoModelStatus
  /** maxContextTokens（上下文容量）：保存在展示条目上的策略。 */
  maxContextTokens?: DemoContextTokens
  /** allowedReasoning（可用思考等级）。 */
  allowedReasoning?: DemoReasoningEffort[]
  /** path（虚构模型路径）。 */
  path?: string
  /** hidden（展示隐藏状态）。 */
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

/** PreviewScene（展示场景）：只改变样例内容和外观，不执行业务。 */
export type PreviewScene = 'normal' | 'empty' | 'long' | 'loading' | 'error'
/** PreviewOverlay（浮层展示）：启动、确认、成功和失败画面。 */
export type PreviewOverlay = 'none' | 'startup' | 'confirm' | 'success' | 'failure'
/** SettingsTab（展示设置页签）：唤醒词与静默合并，正式页面仍保持原入口。 */
export type SettingsTab = 'wake' | 'persona' | 'speaker' | 'wakes' | 'models' | 'compute' | 'system'
/** PreviewPage（展示页面）：工作台或某个设置页。 */
export type PreviewPage = 'home' | SettingsTab

export const settingsTabs: Array<{
  /** id（页签标识）。 */ id: SettingsTab
  /** label（中文名称）。 */ label: string
  /** hint（辅助说明）。 */ hint: string
  /** icon（装饰符号）。 */ icon: string
}> = [
  { id: 'wake', label: '唤醒与静默', hint: '唤醒词、等待与每日静默', icon: '◌' },
  { id: 'persona', label: '人格与指令', hint: '风格与协作方式', icon: '✦' },
  { id: 'speaker', label: '声纹设置', hint: '主人识别与模板', icon: '◎' },
  { id: 'wakes', label: '定时唤醒', hint: '任务与触发规则', icon: '◴' },
  { id: 'models', label: '模型管理', hint: '连接、分配与模型池', icon: '◇' },
  { id: 'compute', label: '计算设备', hint: '设备偏好与资源', icon: '▤' },
  { id: 'system', label: '系统运行', hint: '组件、指标与诊断', icon: '▧' },
]
export const sceneChoices: Array<{
  /** value（场景标识）。 */ value: PreviewScene
  /** label（中文名称）。 */ label: string
}> = [
  { value: 'normal', label: '正常' }, { value: 'empty', label: '空白' },
  { value: 'long', label: '长内容' }, { value: 'loading', label: '加载' }, { value: 'error', label: '错误' },
]
export const overlayChoices: Array<{
  /** value（浮层标识）。 */ value: PreviewOverlay
  /** label（中文名称）。 */ label: string
}> = [
  { value: 'none', label: '无浮层' }, { value: 'startup', label: '启动蒙版' },
  { value: 'confirm', label: '确认弹窗' }, { value: 'success', label: '成功提示' },
  { value: 'failure', label: '失败提示' },
]
/** 固定长文与无空格文本，用于检查气泡、卡片和表单的换行。 */
export const longText = '请将下午的安排整理为三个阶段：先核对待办事项，再保留连续的专注时间，最后集中处理需要沟通的内容。对于尚未确认的事项，请列出依赖和下一步，并保留足够的说明，让我稍后回来时能够继续阅读。'.repeat(4)
export const longIdentifier = 'preview-only-very-long-model-and-component-identifier-'.repeat(5)

export interface DemoMessage {
  /** id（消息标识）。 */ id: string
  /** role（发言角色）。 */ role: 'user' | 'assistant'
  /** text（展示正文）。 */ text: string
  /** time（固定展示时间）。 */ time: string
}
export const demoConversation: DemoMessage[] = [
  { id: 'user-1', role: 'user', text: '帮我把下午的安排理一理，留出一段专注写方案的时间。', time: '14:20' },
  { id: 'assistant-1', role: 'assistant', text: '可以。建议把 14:30 到 16:00 留作方案专注时间，之后集中处理待确认事项。', time: '14:20' },
  { id: 'user-2', role: 'user', text: '三点之后尽量不要安排会议，先帮我列出待确认的事项。', time: '14:21' },
  { id: 'assistant-2', role: 'assistant', text: '已记下这个偏好。我会先整理事项、依赖和下一步，等你确认后再继续。', time: '14:21' },
]
/** id（标识）由展示处补充；label（活动名称）、text（活动正文）、state（画面状态）。 */
export const demoShadowActivities = [
  { label: '读取会话上下文', text: '已确认时间偏好与当前任务。', state: 'done' },
  { label: '整理待确认事项', text: '正在核对已有安排与依赖关系。', state: 'active' },
  { label: '发布可读观察', text: '等待主助手继续处理。', state: 'done' },
]
/** id（标识）、trigger（触发语义）、target（执行位置）、instruction（目标说明）、enabled（启用）。 */
export const demoInstructions = [
  { id: 'instruction-1', trigger: '我说“等一下”时', target: 'voice', instruction: '暂停当前表达，等待我继续说。', enabled: true },
  { id: 'instruction-2', trigger: '需要整理工作安排时', target: 'shadow', instruction: '先列出待确认事项与依赖关系。', enabled: true },
]
/** id（标识）、label（模板名称）、date（录入时间）、status（展示状态）。 */
export const demoSpeakerTemplates = [
  { id: 'template-01', label: '日常说话', date: '2026-10-03 10:20', status: '已纳入主中心' },
  { id: 'template-02', label: '安静环境', date: '2026-10-03 10:25', status: '已纳入主中心' },
]
export const demoSpeakerPrompts = ['今天下午我会整理方案，请帮我保留一段连续的工作时间。', '需要确认的事情可以先列出来，我会逐项查看。', '先把当前安排记录下来，等我回来后再继续。']
/** id（标识）、time（时间）、text（正文）、status（身份判断说明）。 */
export const demoAmbientRecords = [
  { id: 'ambient-01', time: '14:12', text: '窗外有一段远处的谈话声。', status: '非本人 · 环境记录' },
  { id: 'ambient-02', time: '14:16', text: '稍后再确认这个安排。', status: '身份未知 · 环境记录' },
]
/** id（标识）、text（任务说明）、rule（重复规则）、when（触发时间）、status（状态）。 */
export const demoWakeTasks = [
  { id: 'wake-01', text: '整理今天的待办，提醒我确认剩余事项。', rule: '每天 18:00', when: '2026-10-03 18:00', status: '待执行' },
  { id: 'wake-02', text: '回顾本周方案进度，并列出下周需要推进的工作。', rule: '每周五 17:30', when: '2026-10-09 17:30', status: '待执行' },
  { id: 'wake-03', text: '检查上午的会议准备。', rule: '单次', when: '2026-10-03 09:00', status: '已归档' },
]
/** id（标识）、name（模型名称）、device（当前设备）、memory（常驻内存）、gpu（显卡可选）。 */
export const demoComputeModels = [
  { id: 'paraformer', name: 'Paraformer（流式语音识别）', device: 'cpu', memory: '420 MiB（二进制兆字节）', gpu: true },
  { id: 'sensevoice', name: 'SenseVoiceSmall（语音识别与分析）', device: 'cuda', memory: '610 MiB（二进制兆字节）', gpu: true },
  { id: 'voice_vad', name: 'FSMN-VAD（语音活动检测）', device: 'cpu', memory: '58 MiB（二进制兆字节）', gpu: false },
  { id: 'kws', name: 'KWS（关键词唤醒）', device: 'cpu', memory: '96 MiB（二进制兆字节）', gpu: false },
  { id: 'smart_turn', name: 'SmartTurn（语音结束判断）', device: 'cpu', memory: '180 MiB（二进制兆字节）', gpu: false },
  { id: 'wespeaker', name: 'WeSpeaker（声纹识别）', device: 'cpu', memory: '110 MiB（二进制兆字节）', gpu: false },
]
/** id（标识）、name（组件名称）、phase（阶段）、pid（进程号）、memory（常驻内存）、commit（提交内存）、cpu（处理器占用）。 */
export const demoSystemComponents = [
  { id: 'web', name: '网页与应用宿主', phase: '提供网页、托盘与组件监督', pid: '21040', memory: '86 MiB', commit: '102 MiB', cpu: '0.2%' },
  { id: 'voice', name: 'Voice Server（语音服务）', phase: '语音链路待机', pid: '21044', memory: '780 MiB', commit: '940 MiB', cpu: '1.1%' },
  { id: 'asr', name: 'ASR（语音识别）', phase: '模型已加载', pid: '21048', memory: '420 MiB', commit: '530 MiB', cpu: '0.3%' },
  { id: 'analysis', name: '语音分析工作进程', phase: '分析能力就绪', pid: '21052', memory: '610 MiB', commit: '730 MiB', cpu: '0.4%' },
  { id: 'dsh', name: 'DSH（代理执行宿主）', phase: '等待后台协作任务', pid: '21056', memory: '160 MiB', commit: '205 MiB', cpu: '0.1%' },
]
export const demoCapabilities = ['音频设备', 'KWS（关键词唤醒）', 'ASR（语音识别）', 'LLM（大语言模型）', 'TTS（语音合成）', 'Shadow（影子代理）', '声纹识别']

/** 固定样例列表：空白场景无条目；长内容场景扩展列表，标识保持唯一。 */
export function previewRows<T extends { id: string }>(rows: readonly T[], scene: PreviewScene): T[] {
  if (scene === 'empty') return []
  if (scene !== 'long') return rows.map(row => ({ ...row }))
  return Array.from({ length: 8 }, (_, batch) => rows.map(row => ({ ...row, id: `${row.id}-${batch}` }))).flat()
}

/** 表单初值；只供页面编辑和布局展示，不读取或保存真实配置。 */
export const demoSettings = {
  /** wakeEnabled（启用唤醒）。 */ wakeEnabled: true,
  /** keywords（唤醒短语列表）。 */ keywords: ['嘿，小爪', '小爪同学'],
  /** sensitivity（识别灵敏度百分比）。 */ sensitivity: 75,
  /** silenceSeconds（最终静默等待秒数）。 */ silenceSeconds: 90,
  /** quietEnabled（启用每日被动静默）。 */ quietEnabled: false,
  /** quietStart（静默开始时间）。 */ quietStart: '23:00',
  /** quietEnd（静默结束时间）。 */ quietEnd: '07:00',
  /** speakerMode（声纹运行模式）。 */ speakerMode: 'observe',
  /** confirmThreshold（主人确认阈值）。 */ confirmThreshold: 0.60,
  /** continuityThreshold（非主人确认界限）。 */ continuityThreshold: 0.50,
  /** ownerOnly（仅主人可打断）。 */ ownerOnly: false,
  /** experimentalContextGateEnabled（实验性上下文门禁，仅展示引导时机开关）。 */ experimentalContextGateEnabled: true,
  /** providerName（服务商名称）。 */ providerName: '我的模型连接',
  /** providerUrl（虚构服务地址）。 */ providerUrl: 'https://api.example.com/v1',
  /** providerProtocol（展示协议名称）。 */ providerProtocol: 'DeepSeek',
  /** providerKey（空白密钥展示框）。 */ providerKey: '',
  /** voiceModel（主语音模型标识）。 */ voiceModel: 'runtime-voice-llm',
  /** shadowModel（影子代理模型标识）。 */ shadowModel: 'runtime-shadow-llm',
  /** statusModel（状态汇报模型标识）。 */ statusModel: 'managed-reasoning',
  /** taskText（定时任务编辑正文）。 */ taskText: '整理今天的待办，提醒我确认剩余事项。',
  /** taskWhen（任务触发时间）。 */ taskWhen: '2026-10-03T18:00',
  /** taskRule（任务重复规则）。 */ taskRule: '每天',
}
