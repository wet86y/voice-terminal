<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive } from 'vue'
import { useVoiceTerminal } from './composables/useVoiceTerminal'
import type { LatencyValues } from './types'

const terminal = reactive(useVoiceTerminal())

onMounted(() => terminal.connect())
onBeforeUnmount(() => terminal.dispose())

const stateLabels: Record<string, string> = {
  STARTING: '启动中',
  IDLE: '待机',
  LISTENING: '正在听',
  FINALIZING: '确认结束',
  THINKING: '等待回答',
  SPEAKING: '正在讲话',
  INTERRUPTING: '正在打断',
  ERROR: '异常',
}

const latencyDefinitions: Array<{ key: keyof LatencyValues; label: string }> = [
  { key: 'wake_detection_ms', label: '唤醒检测' },
  { key: 'asr_first_text_ms', label: 'ASR 首次出字' },
  { key: 'speech_end_confirmation_ms', label: '讲话结束确认' },
  { key: 'llm_first_token_ms', label: 'LLM 首 token' },
  { key: 'llm_request_to_first_tts_text_ms', label: '首段文本进入 TTS' },
  { key: 'tts_text_to_first_audio_ms', label: 'TTS 首音频' },
  { key: 'first_audio_to_playback_ms', label: '首音频到播放器' },
  { key: 'speech_end_to_playback_ms', label: '用户说完 → AI 开口' },
  { key: 'turn_total_ms', label: '本轮总时长' },
]

const stateLabel = computed(() => stateLabels[terminal.state] ?? terminal.state)
const systemLabel = computed(() => terminal.connectionLabel)
const connectionClass = computed(() => terminal.connection)
const buttonLabel = computed(() => {
  if (terminal.connection === 'connecting') return '连接中…'
  if (terminal.listening) return '停止监听'
  return '开始说话'
})
const buttonDisabled = computed(
  () => terminal.connection !== 'connected' || (!terminal.listening && !terminal.canStart),
)
const currentTurnLabel = computed(() => terminal.currentTurnId ?? '—')
const transcriptPreview = computed(
  () => terminal.current.final_text || terminal.current.partial_text || '等待后端语音事件…',
)
const currentLatencyRows = computed(() =>
  latencyDefinitions.map((definition) => ({
    ...definition,
    value: terminal.currentLatency[definition.key],
  })),
)

const serviceRows = computed(() => [
  { key: 'audio', label: 'Audio', value: terminal.services.audio },
  { key: 'kws', label: 'KWS', value: terminal.services.kws },
  { key: 'asr', label: 'ASR Worker', value: terminal.services.asr },
  { key: 'llm', label: 'LLM', value: terminal.services.llm },
  { key: 'tts', label: 'TTS / Player', value: terminal.services.tts },
])

function formatMs(value: number | null) {
  return value === null ? '—' : `${value} ms`
}

function displayText(value: string | null | undefined) {
  return value?.trim() || '—'
}

function statusClass(status: string) {
  return status.toLowerCase().replace(/\s+/g, '-')
}
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <div class="brand-block">
        <div class="eyebrow"><span class="signal-dot"></span> LOCAL VOICE RUNTIME / V0.1</div>
        <h1>Voice Terminal</h1>
        <p>后端驱动的实时语音控制台。</p>
      </div>

      <div class="top-actions">
        <span class="connection-chip" :class="connectionClass">
          <span class="chip-dot"></span>{{ systemLabel }}
        </span>
      </div>
    </header>

    <section class="status-strip" aria-label="后端服务状态">
      <div class="status-item status-item-primary">
        <span class="status-label">SYSTEM</span>
        <span class="status-value" :class="connectionClass"><span class="status-light"></span>{{ systemLabel }}</span>
      </div>
      <div class="status-item">
        <span class="status-label">STATE</span>
        <span class="status-value">{{ stateLabel }}</span>
      </div>
      <div class="status-item">
        <span class="status-label">ASR</span>
        <span class="status-value" :class="statusClass(terminal.services.asr)"><span class="status-light"></span>{{ terminal.services.asr }}</span>
      </div>
      <div class="status-item">
        <span class="status-label">LLM</span>
        <span class="status-value" :class="statusClass(terminal.services.llm)"><span class="status-light"></span>{{ terminal.services.llm }}</span>
      </div>
      <div class="status-item">
        <span class="status-label">TTS</span>
        <span class="status-value" :class="statusClass(terminal.services.tts)"><span class="status-light"></span>{{ terminal.services.tts }}</span>
      </div>
    </section>

    <p v-if="terminal.connectionError" class="error-banner" role="alert">
      {{ terminal.connectionError }}
    </p>

    <section class="workspace-grid">
      <article class="panel listen-panel panel-dark">
        <div class="panel-heading">
          <div>
            <span class="section-index">01 / CONTROL</span>
            <h2>语音会话</h2>
          </div>
          <span class="state-badge" :class="terminal.state.toLowerCase()">{{ stateLabel }}</span>
        </div>

        <div class="listen-stage">
          <div class="stage-orbit orbit-one"></div>
          <div class="stage-orbit orbit-two"></div>
          <div class="mic-core" :class="{ active: terminal.listening }">
            <span class="mic-glyph">◖</span>
            <span class="mic-caption">{{ terminal.listening ? 'LISTENING' : 'STANDBY' }}</span>
          </div>
        </div>

        <div class="session-copy">
          <div>
            <span class="live-copy-label">CURRENT TURN</span>
            <strong>{{ currentTurnLabel }}</strong>
          </div>
          <p>{{ terminal.listening ? '后端正在持续监听，VAD / ASR 由服务端处理；开始监听后会直接进入当前语音轮。' : '点击下方按钮启动后端持续监听。' }}</p>
        </div>

        <button
          class="session-button"
          type="button"
          :disabled="buttonDisabled"
          :aria-busy="terminal.connection === 'connecting'"
          @click="terminal.toggleListening"
        >
          <span class="session-button-icon">{{ terminal.listening ? '■' : '◉' }}</span>
          {{ buttonLabel }}
          <span class="button-arrow">↗</span>
        </button>
      </article>

      <article class="panel overview-panel">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">02 / SERVICES</span>
            <h2>后端服务状态</h2>
          </div>
          <span class="server-time">{{ terminal.lastServerTimestamp || '等待服务' }}</span>
        </div>

        <div class="service-list">
          <div v-for="service in serviceRows" :key="service.key" class="service-row">
            <div class="service-name"><span class="service-mark"></span>{{ service.label }}</div>
            <span class="service-status" :class="statusClass(service.value)">{{ service.value }}</span>
          </div>
        </div>

        <div class="backend-note">
          <span class="note-mark">i</span>
          <p>前端不采集音频、不运行模型、不推导延迟。这里的所有状态都来自 Voice Server。</p>
        </div>
      </article>

      <article class="panel transcript-panel">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">03 / RECOGNITION</span>
            <h2>实时语音识别</h2>
          </div>
          <span class="service-pill" :class="statusClass(terminal.services.asr)">{{ terminal.services.asr }}</span>
        </div>

        <div class="transcript-stack" aria-live="polite">
          <div class="transcript-box partial-box">
            <span class="box-label"><span class="pulse-dot"></span> PARTIAL</span>
            <p>{{ displayText(terminal.current.partial_text) }}</p>
          </div>
          <div class="transcript-box final-box">
            <span class="box-label">FINAL / {{ currentTurnLabel }}</span>
            <p>{{ displayText(terminal.current.final_text) }}</p>
          </div>
        </div>

        <div class="metric-footer">
          <span>当前轮</span><strong>{{ currentTurnLabel }}</strong>
          <span>数据源</span><strong>VOICE SERVER</strong>
        </div>
      </article>

      <article class="panel response-panel panel-paper">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">04 / RESPONSE</span>
            <h2>LLM → TTS</h2>
          </div>
          <span class="flow-arrow">SERVER STREAM</span>
        </div>

        <div class="response-column" aria-live="polite">
          <div class="response-block">
            <span class="box-label">ASSISTANT RESPONSE</span>
            <p>{{ displayText(terminal.current.assistant_text) }}</p>
          </div>
          <div class="response-divider"><span>TTS QUEUE</span><i></i><span>{{ displayText(terminal.current.tts_status) }}</span></div>
          <div class="response-block tts-block">
            <span class="box-label">{{ displayText(terminal.current.tts_status) }} / AUDIO BUFFER</span>
            <p>{{ displayText(terminal.current.tts_text) }}</p>
            <div class="buffer-meta"><span>后端缓冲</span><strong>{{ formatMs(terminal.current.audio_buffer_ms) }}</strong></div>
          </div>
        </div>
      </article>

      <article class="panel latency-panel panel-dark">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">05 / LATENCY</span>
            <h2>当前轮延迟</h2>
          </div>
          <span class="latency-target">BACKEND DATA</span>
        </div>

        <div class="latency-hero">
          <span>用户说完 → AI 开口</span>
          <strong>{{ formatMs(terminal.currentLatency.speech_end_to_playback_ms) }}</strong>
        </div>
        <div class="latency-list">
          <div v-for="row in currentLatencyRows" :key="row.key" class="latency-row">
            <span>{{ row.label }}</span>
            <i></i>
            <strong>{{ formatMs(row.value) }}</strong>
          </div>
        </div>
        <div class="latency-footnote">{{ terminal.currentLatencyComplete ? '本轮指标已完成' : '等待后端逐步上报当前轮指标' }}</div>
      </article>

      <article class="panel history-panel">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">06 / HISTORY</span>
            <h2>最近 10 轮</h2>
          </div>
          <span class="history-count">{{ terminal.recentTurns.length }} / 10</span>
        </div>

        <div v-if="terminal.recentTurns.length" class="history-list">
          <div v-for="turn in terminal.recentTurns" :key="turn.turn_id" class="history-row">
            <div class="history-id">{{ turn.turn_id }}</div>
            <div class="history-copy">
              <p class="history-transcript">{{ displayText(turn.transcript) }}</p>
              <p class="history-response">{{ displayText(turn.response) }}</p>
              <span class="history-time">{{ displayText(turn.completed_at) }}</span>
            </div>
            <div class="history-metrics">
              <span>说完 → 开口</span><strong>{{ formatMs(turn.latency.speech_end_to_playback_ms) }}</strong>
              <span>总时长</span><strong>{{ formatMs(turn.latency.turn_total_ms) }}</strong>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">等待后端完成第一轮对话…</div>
      </article>

      <article class="panel context-panel">
        <div class="panel-heading compact-heading">
          <div>
            <span class="section-index">07 / LLM CONTEXT</span>
            <h2>实际发送给模型的上下文</h2>
          </div>
          <span class="context-meta">{{ terminal.llmContextTurnId || '等待 LLM 请求' }}</span>
        </div>

        <div v-if="terminal.llmContext.length" class="context-list">
          <div v-for="(message, index) in terminal.llmContext" :key="`${terminal.llmContextTurnId}-${index}`" class="context-row">
            <span class="context-role">{{ message.role }}</span>
            <p>{{ message.content }}</p>
          </div>
        </div>
        <div v-else class="empty-state context-empty">等待下一轮 LLM 请求…</div>
        <p class="context-note">这里展示本轮请求实际使用的 system、历史 user/assistant 消息和当前 user 消息；不是前端自行拼接的摘要。</p>
      </article>
    </section>

    <footer class="footer-note">
      <span>ALL LATENCY VALUES FROM VOICE SERVER</span>
      <span>TURN ID / SERVER SEQUENCE</span>
      <span>WEB CONSOLE · LOCALHOST</span>
    </footer>
  </main>
</template>
