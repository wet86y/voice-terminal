import type {
  AgentEpisodeSnapshot,
  AgentTrace,
  InputRecord,
  LatencyValues,
  PlaybackQueueItem,
  ShadowProductSnapshot,
  SpeechOutputRecord,
  VoiceReplayFixture,
} from '../types'

// Extracted from the local logs:
//   logs/voice-server-llm-context.jsonl
//   logs/voice-server.log.1
// Conversation: conversation_d85249391e8b (2026-09-14 22:24–22:28 +08:00)
// Only the public product projection is retained here; prompts, tool
// arguments and raw provider payloads are intentionally not replayed.
const conversationId = 'conversation_d85249391e8b'
const currentTurnId = 'input_20260914_222740_000036'
const currentSpeechId = '94b7a0c08de243a581fe85c9f3066a7b'
const currentResponse = '那就好，看来是真跑通了。老大这一路调下来不容易，有需要随时喊我。'

const emptyLatency: LatencyValues = {
  wake_detection_ms: null,
  asr_first_text_ms: null,
  speech_end_confirmation_ms: null,
  llm_first_token_ms: null,
  llm_request_to_first_tts_text_ms: null,
  tts_text_to_first_audio_ms: null,
  first_audio_to_playback_ms: null,
  input_end_to_playback_ms: null,
  speech_end_to_playback_ms: null,
  turn_total_ms: null,
  first_segment_ms: null,
  smart_turn_ms: null,
  sensevoice_refine_ms: null,
  commit_to_final_llm_ms: null,
  asr_final_to_commit_ms: null,
  refine_duration_ms: null,
  refine_to_redecision_ms: null,
  agent_request_to_speak_ms: null,
  tts_prewarm_requested_ms: null,
  tts_prewarm_ready_ms: null,
  tts_checkout_ms: null,
  tts_cold_start_ms: null,
  replacement_count: null,
  speech_end_to_first_pcm_ms: null,
  first_pcm_to_player_submit_ms: null,
}

function latency(overrides: Partial<LatencyValues> = {}): LatencyValues {
  return { ...emptyLatency, ...overrides }
}

const shadowSummary = '已完成：读取 docs 下模型相关文档，确认主链基线为官方 DeepSeek、模型 ID deepseek-flash、Responses + strict json_schema；运行时配置仍需老大从 config、.env 或进程环境确认。'

const shadowTimeline: ShadowProductSnapshot['timeline'] = [
  {
    state: 'done',
    label: '状态汇报',
    text: '已读取 docs 下模型相关文档，确认主链基线为官方 DeepSeek / deepseek-flash。',
    timestamp: '2026-09-14T22:24:33+08:00',
  },
  {
    state: 'done',
    label: '后台处理中',
    text: '继续检索运行时配置与环境覆盖，Shadow 完成多轮文件搜索。',
    timestamp: '2026-09-14T22:25:18+08:00',
  },
  {
    state: 'done',
    label: '发布观察',
    text: '已把最新会话上下文同步给主语音 Agent，等待下一步确认。',
    timestamp: '2026-09-14T22:25:29+08:00',
  },
  {
    state: 'done',
    label: '状态汇报',
    text: 'Shadow 自身思考模型未在可读文档中记载，不能把推测当成真机配置。',
    timestamp: '2026-09-14T22:25:43+08:00',
  },
  {
    state: 'done',
    label: '等待你的输入',
    text: '当前阻塞点：无法读取 config、.env 和运行进程；可以提供配置，或按文档口径收尾。',
    timestamp: '2026-09-14T22:25:43+08:00',
  },
]

const shadowActivities: NonNullable<ShadowProductSnapshot['activities']> = [
  { run_seq: 7, item_id: 'context-7', stream: 'thinking', phase: 'completed', text: '上下文已同步', timestamp: '2026-09-14T22:24:31+08:00' },
  { run_seq: 7, item_id: 'thinking-7', stream: 'thinking', phase: 'completed', text: '已整理会话目标，正在核对模型与运行时配置。', timestamp: '2026-09-14T22:24:34+08:00' },
  { run_seq: 7, item_id: 'tool-7-search', stream: 'tool', phase: 'completed', tool_name: 'files_search', tool_status: 'completed', text: '工具执行完成', timestamp: '2026-09-14T22:25:17+08:00' },
  { run_seq: 7, item_id: 'event-7-progress', stream: 'event', phase: 'completed', event_kind: 'progress', text: '继续检索运行时配置与环境覆盖。', timestamp: '2026-09-14T22:25:18+08:00' },
  { run_seq: 7, item_id: 'tool-7-publish', stream: 'tool', phase: 'completed', tool_name: 'publish_main_event', tool_status: 'completed', text: '事件已传递', timestamp: '2026-09-14T22:25:29+08:00' },
  { run_seq: 7, item_id: 'event-7-observation', stream: 'event', phase: 'completed', event_kind: 'observation', text: '已把最新会话上下文同步给主语音 Agent。', timestamp: '2026-09-14T22:25:29+08:00' },
  { run_seq: 7, item_id: 'thinking-7-status', stream: 'thinking', phase: 'completed', text: '已核对可读状态，等待用户确认运行时配置。', timestamp: '2026-09-14T22:25:43+08:00' },
  { run_seq: 7, item_id: 'event-7-waiting', stream: 'event', phase: 'completed', event_kind: 'user_input_required', text: '当前阻塞点：等待用户确认运行时配置。', timestamp: '2026-09-14T22:25:43+08:00' },
  { run_seq: 7, item_id: 'run-7', stream: 'lifecycle', phase: 'completed', text: 'Shadow Agent 已完成本轮处理，等待输入', timestamp: '2026-09-14T22:25:43+08:00' },
]

const shadow: ShadowProductSnapshot = {
  available: true,
  active: false,
  phase: 'waiting',
  user_input_required: true,
  focus: '继续核对运行时配置与 Shadow 自身模型',
  summary: shadowSummary,
  timeline: shadowTimeline,
  activities: shadowActivities,
  last_activity_at: '2026-09-14T22:25:43+08:00',
  context: {
    used_tokens: 41069,
    limit_tokens: 524288,
    usage_percent: 7.83,
  },
  provider: {
    input_tokens: 40569,
    cached_tokens: 40192,
    cache_hit_ratio: 0.9907071902191328,
  },
}

const trace: AgentTrace[] = [
  { trace_seq: 38, kind: 'USER_INPUT_FINALIZED', conversation_id: conversationId, turn_id: currentTurnId },
  { trace_seq: 39, kind: 'AGENT_INVOKED', conversation_id: conversationId, turn_id: currentTurnId },
  { trace_seq: 40, kind: 'LLM_REQUEST_COMPLETED', conversation_id: conversationId, turn_id: currentTurnId, status: 'strict_valid' },
  { trace_seq: 41, kind: 'SPEAK_ACCEPTED', conversation_id: conversationId, turn_id: currentTurnId, speech_id: currentSpeechId },
  { trace_seq: 42, kind: 'TTS_STARTED', conversation_id: conversationId, turn_id: currentTurnId, speech_id: currentSpeechId },
]

const activeSpeech: SpeechOutputRecord = {
  speech_id: currentSpeechId,
  conversation_id: conversationId,
  trace_id: '80a1593c96a94ae9b6dcf2fa485b9be7',
  turn_id: currentTurnId,
  request_id: '80a1593c96a94ae9b6dcf2fa485b9be7',
  output_generation: 1,
  text: currentResponse,
  status: 'SPEAKING',
  emotion_requested: 'happy',
  emotion_applied: 'happy',
  kind: 'formal',
  delivery: { status: 'partial', heard_text: '那就好，看来是真跑通了。' },
}

const agent: AgentEpisodeSnapshot = {
  conversation_id: conversationId,
  trace_id: '80a1593c96a94ae9b6dcf2fa485b9be7',
  request_id: '80a1593c96a94ae9b6dcf2fa485b9be7',
  context_chars: 25568,
  context_limit_tokens: 524288,
  context_token_estimate: 6392,
  context_usage_percent: 1.22,
  agent_busy: false,
  user_speaking: false,
  current_action: 'speak',
  request_count: 32,
  max_request_concurrency: 1,
  playback_state: 'speaking',
  active_speech: activeSpeech,
  speech_queue: [],
  speak_snapshot: { queue_status: 'speaking', text: currentResponse, text_only_queue: true },
  reasoning: { text: '用户表达了调试链路已经跑通的兴奋，继续用简短、适合播报的语气回应。' },
  trace,
  outputs: [activeSpeech],
}

const playbackQueue: PlaybackQueueItem[] = [{
  speech_id: currentSpeechId,
  conversation_id: conversationId,
  request_id: '80a1593c96a94ae9b6dcf2fa485b9be7',
  turn_id: currentTurnId,
  output_generation: 1,
  text: currentResponse,
  state: 'playing',
  emotion_requested: 'happy',
  emotion_applied: 'happy',
}]

const recentInputs: InputRecord[] = [
  {
    conversation_id: conversationId,
    turn_id: 'input_20260914_222418_000026',
    transcript: '呃可以继续往下确认吧',
    response: '好，我让影子继续往下核，把运行时那一层也确认清楚。顺便让它把影子自己用的思考模型也一起查了。',
    completed_at: '2026-09-14T22:24:50+08:00',
    status: 'completed',
    delivery: { status: 'full' },
    latency: latency({ agent_request_to_speak_ms: 4210, turn_total_ms: 7810 }),
  },
  {
    conversation_id: conversationId,
    turn_id: 'input_20260914_222504_000027',
    transcript: '我等着你',
    response: '好，我盯着，有结果第一时间告诉你。',
    completed_at: '2026-09-14T22:25:20+08:00',
    status: 'completed',
    delivery: { status: 'full' },
    latency: latency({ agent_request_to_speak_ms: 2360, turn_total_ms: 5080 }),
  },
  {
    conversation_id: conversationId,
    turn_id: 'input_20260914_222509_000028',
    transcript: '是吧',
    response: '老大，影子那边卡在权限上了：它只有 docs 目录的只读权限，读不到 config/default.yaml、.env.local，也看不到进程里的 VOICE_LLM_PROVIDER 覆盖。所以真机到底挂的是哪个模型，还得你这边给一样东西。',
    completed_at: '2026-09-14T22:25:43+08:00',
    status: 'completed',
    delivery: { status: 'full' },
    latency: latency({ agent_request_to_speak_ms: 5120, turn_total_ms: 10500 }),
  },
  {
    conversation_id: conversationId,
    turn_id: 'input_20260914_222656_000032',
    transcript: '我这边可提没法提供给你提供任何帮助啊',
    response: '明白，那就不折腾了。按文档口径给个结论：主链是官方 DeepSeek 的 Responses 接口，强制严格 JSON Schema，模型标识 deepseek-flash。',
    completed_at: '2026-09-14T22:27:18+08:00',
    status: 'completed',
    delivery: { status: 'full' },
    latency: latency({ agent_request_to_speak_ms: 3910, turn_total_ms: 7420 }),
  },
]

export const voiceShadowReplayMeta = {
  label: '日志回放 · 2026-09-14 22:27',
  source: 'voice-server-llm-context.jsonl + voice-server.log.1',
  conversationId,
}

export const voiceShadowReplayFixture: VoiceReplayFixture = {
  state: 'SPEAKING',
  listening: false,
  canStart: true,
  wakeAlwaysOn: false,
  inputOwner: 'none',
  reasoningMode: 'balanced',
  services: {
    audio: 'READY',
    kws: 'DISABLED',
    asr: 'READY',
    llm: 'READY',
    tts: 'READY',
    web_search: 'READY',
    shadow_agent: 'READY',
    analysis: 'READY',
  },
  current: {
    turn_id: currentTurnId,
    output_generation: 1,
    committed: true,
    agent_phase: 'SPEAKING',
    paraformer_final: '完全可用啊啊',
    refined_text: '完全可用啊哈哈',
    refinement_available: true,
    tts_prewarm_started: true,
    tts_prewarm_status: 'READY',
    segments: [{ text: '完全可用啊啊' }],
    tools: [],
    audio_cues: [],
    analysis: {},
    partial_text: '',
    final_text: '完全可用啊啊',
    assistant_text: currentResponse,
    tts_text: currentResponse,
    tts_status: 'PLAYING',
    audio_buffer_ms: 1160,
    tts_returned_ms: 4350,
    tts_played_ms: 2100,
    tts_output_estimated_ms: 4800,
    tts_remaining_ms: 2700,
    delivery: { status: 'partial', heard_text: '那就好，看来是真跑通了。' },
    audio_processing_status: 'ACTIVE',
    audio_processing_error: '',
    capture_diagnostics: { dropped_chunks: 0, asr_queue_depth: 0 },
    playback_diagnostics: { buffered_ms: 1160, callback_status: 'PLAYING' },
    tts_transport: 'websocket_bidi',
    tts_scheduling: 'direct',
  },
  currentLatency: {
    asr_first_text_ms: 765,
    speech_end_confirmation_ms: 410,
    agent_request_to_speak_ms: 6107,
    tts_prewarm_requested_ms: 0,
    tts_prewarm_ready_ms: 250,
    tts_text_to_first_audio_ms: 1020,
    first_audio_to_playback_ms: 420,
    speech_end_to_playback_ms: 6107,
    turn_total_ms: 13834,
  },
  currentLatencyComplete: true,
  recentInputs,
  shadow,
  agent,
  agentTrace: trace,
  speechOutputs: [activeSpeech],
  playbackQueue,
  reasoning: agent.reasoning?.text || '',
}
