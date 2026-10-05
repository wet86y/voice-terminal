import type { InputRecord, VoiceReplayFixture } from '../types'
import type { DemoMessage } from './demo-data'
import { voiceShadowReplayFixture } from './voice-shadow-replay-data'

/** ReplayActivity（回放活动）：从固定公开投影转换出的样例界面活动。 */
export interface ReplayActivity {
  /** id（活动标识）：稳定的展示条目标识。 */
  id: string
  /** label（活动名称）：界面显示的步骤名称。 */
  label: string
  /** text（活动说明）：界面显示的公开活动正文。 */
  text: string
  /** state（活动状态）：仅用于展示完成或进行中的样式。 */
  state: 'done' | 'active' | 'failed'
}

/** VoiceShadowReplayScene（影子回放样例）：纯数据形状，供 UI preview（界面样例）显示。 */
export interface VoiceShadowReplayScene {
  /** conversation（对话条目）：按记录顺序排列的固定消息。 */
  conversation: DemoMessage[]
  /** activities（影子活动）：固定公开活动投影。 */
  activities: ReplayActivity[]
  /** response（当前回答）：正在播放的固定回复正文。 */
  response: string
  /** context（上下文用量）：固定显示的上下文容量。 */
  context: string
  /** cache（缓存命中率）：固定显示的服务端缓存比例。 */
  cache: string
  /** latency（首音延迟）：从已记录的影子响应时间导出的样例值。 */
  latency: string
}

function timeLabel(record: InputRecord) {
  const match = record.completed_at.match(/T(\d{2}:\d{2})/)
  return match?.[1] ?? '22:27'
}

/** projectVoiceShadowReplay（转换影子回放）：只生成 UI（界面）可消费的静态数据。 */
export function projectVoiceShadowReplay(fixture: VoiceReplayFixture): VoiceShadowReplayScene {
  const conversation = [...fixture.recentInputs].reverse().flatMap(record => [
    { id: `${record.turn_id}-user`, role: 'user' as const, text: record.transcript, time: timeLabel(record) },
    { id: `${record.turn_id}-assistant`, role: 'assistant' as const, text: record.response, time: timeLabel(record) },
  ])
  const currentUser = fixture.current.final_text || fixture.current.partial_text || ''
  const currentResponse = fixture.current.assistant_text || fixture.current.tts_text || ''
  if (currentUser) {
    conversation.push({
      id: `${fixture.current.turn_id || 'replay-current'}-user`,
      role: 'user', text: currentUser, time: '22:27',
    })
  }
  if (currentResponse) {
    conversation.push({
      id: `${fixture.current.turn_id || 'replay-current'}-assistant`,
      role: 'assistant', text: currentResponse, time: '22:27',
    })
  }

  const activities = (fixture.shadow.activities || []).map((item, index): ReplayActivity => ({
    id: `${item.run_seq}-${item.item_id || index}`,
    label: item.stream === 'tool' ? item.tool_name || '执行工具'
      : item.stream === 'event' ? item.event_kind === 'input_required' ? '等待输入' : '状态更新'
        : item.stream === 'lifecycle' ? '影子运行状态' : '影子思考',
    text: item.text || item.delta || item.tool_status || '活动已更新。',
    state: item.phase === 'failed' ? 'failed' : item.phase === 'completed' ? 'done' : 'active',
  }))
  const used = fixture.shadow.context?.used_tokens
  const limit = fixture.shadow.context?.limit_tokens
  const context = used == null || limit == null ? '不可用' : `${Math.round(used / 1024)}K / ${Math.round(limit / 1024)}K`
  const hitRatio = fixture.shadow.provider?.cache_hit_ratio
  const cache = hitRatio == null ? '不可用' : `${Math.round(hitRatio * 100)}%`
  const latencyMs = fixture.currentLatency?.speech_end_to_playback_ms ?? fixture.currentLatency?.agent_request_to_speak_ms

  return {
    conversation,
    activities,
    response: currentResponse,
    context,
    cache,
    latency: latencyMs == null ? '—' : `${(latencyMs / 1000).toFixed(1)} s`,
  }
}

/** 固定影子回放：仅 UI preview（界面样例）入口导入，正式产品依赖图不引用此文件。 */
export const voiceShadowReplayDemo = projectVoiceShadowReplay(voiceShadowReplayFixture)
