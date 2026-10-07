<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { WorkbenchModel } from "./workbench";
import type { ReasoningMode } from "../types";
import UiButton from "./UiButton.vue";
import ConversationMessage from "./ConversationMessage.vue";
const props = defineProps<{
  model: WorkbenchModel; // model（工作台呈现数据）。
  consoleEnabled?: boolean; // consoleEnabled（代理控制台是否可用）。
  consoleLabel?: string; // consoleLabel（代理控制台按钮文案）。
}>();
// initialMessageKeys（进入工作台时已有记录）：历史消息不重新播放逐字动效。
const initialMessageKeys = new Set(props.model.conversation.map(entry => entry.key));
const emit = defineEmits<{
  action: [];
  reset: [];
  mode: [value: ReasoningMode]; // mode（切换运行模式）。
  settings: []; // settings（打开设置页面）。
  console: []; // console（打开代理控制台）。
}>();
const conversation = ref<HTMLElement | null>(null);
const trace = ref<HTMLElement | null>(null);
const voiceColumn = ref<HTMLElement | null>(null);
const sessionActive = computed(() => props.model.sessionActive ?? ['listening', 'thinking', 'speaking'].includes(props.model.state));
const runtimeLabels: Record<string, string> = { // runtimeLabels（运行状态的中文名称）。
  standby: '等待中', listening: '聆听中', thinking: '思考中', speaking: '说话中', offline: '已关闭', error: '需检查',
};
const runtimeLabel = computed(() => runtimeLabels[props.model.state] || '等待中');
const waveformLevels = computed(() => Array.from({ length: 5 }, (_, index) => {
  const level = props.model.audioLevels?.[index];
  return sessionActive.value && ['listening', 'speaking'].includes(props.model.state) && typeof level === 'number' && Number.isFinite(level)
    ? Math.max(0, Math.min(1, level)) : 0;
}));
const voiceEnergy = computed(() => waveformLevels.value.reduce((total, level) => total + level, 0) / 5);
const latencyLabel = computed(() => props.model.latency.replace(/\bms\b/g, '毫秒'));
const layoutAnimations = new Set<Animation>(); // layoutAnimations（当前启动或停止的布局动画）。
let motionGeneration = 0; // motionGeneration（动效代次）：快速连续点击时取消过期动画。

function cancelLayoutAnimations() {
  layoutAnimations.forEach(animation => animation.cancel());
  layoutAnimations.clear();
}
watch(sessionActive, async () => {
  const generation = ++motionGeneration;
  const elements = [...(voiceColumn.value?.querySelectorAll<HTMLElement>('.voice-session-action, .metric-card, .voice-presentation, .voice-orb, .orb-ring') || [])];
  const before = elements.map(element => element.getBoundingClientRect());
  const widths = elements.map(element => getComputedStyle(element).width);
  cancelLayoutAnimations();
  await nextTick();
  if (generation !== motionGeneration || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  // FLIP（先记录旧位置，再计算新位置并插值）：使用同一个按钮下移、还原，保留键盘焦点。
  elements.forEach((element, index) => {
    if (!element.isConnected || typeof element.animate !== 'function') return;
    const after = element.getBoundingClientRect();
    const source = before[index];
    const presentation = element.classList.contains('voice-presentation');
    const shape = element.matches('.voice-orb, .orb-ring');
    const x = source.x - after.x;
    const y = presentation ? source.y + source.height / 2 - after.y - after.height / 2 : source.y - after.y;
    const sizing = element.classList.contains('voice-session-action');
    // 主体和两层轮廓同速放大、还原，避免启动时先出现大轮廓。
    const animation = element.animate(shape ? [
      { width: widths[index] }, { width: getComputedStyle(element).width },
    ] : [
      { transform: `translate(${x}px, ${y}px)`, ...(sizing ? { width: `${source.width}px` } : {}) },
      { transform: 'translate(0, 0)', ...(sizing ? { width: `${after.width}px` } : {}) },
    ], { duration: 440, easing: 'cubic-bezier(.22, 1, .36, 1)' });
    layoutAnimations.add(animation);
    animation.onfinish = () => layoutAnimations.delete(animation);
  });
});
const followConversation = ref(true),
  followTrace = ref(true);
let observer: ResizeObserver | null = null;
function atTail(element: HTMLElement | null) {
  return (
    !element ||
    element.scrollHeight - element.clientHeight - element.scrollTop < 30
  );
}
function scroll() {
  if (followConversation.value && conversation.value)
    conversation.value.scrollTop = conversation.value.scrollHeight;
  if (followTrace.value && trace.value)
    trace.value.scrollTop = trace.value.scrollHeight;
}
watch(
  () => [
    props.model.conversation,
    props.model.partial,
    props.model.shadow.trace,
  ],
  async () => {
    await nextTick();
    scroll();
  },
  { deep: true },
);
onMounted(() => {
  scroll();
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(scroll);
    if (conversation.value) observer.observe(conversation.value);
    if (trace.value) observer.observe(trace.value);
  }
});
onBeforeUnmount(() => { observer?.disconnect(); motionGeneration += 1; cancelLayoutAnimations(); });
</script>
<template>
  <section class="workbench" aria-label="语音工作台">
    <aside class="shadow-column panel" aria-labelledby="shadow-panel-title">
      <header class="column-heading">
        <h2 id="shadow-panel-title" class="panel-kicker">后台协作</h2>
      </header>
      <div class="shadow-regions">
      <section class="shadow-summary-card shadow-region" aria-label="当前协作状态">
        <header class="shadow-region-heading"><span>当前协作</span><small>{{ model.shadow.status }}</small></header>
        <div class="shadow-summary-content">
        <p>{{ model.shadow.title }}</p>
        <p v-if="model.shadow.focus">{{ model.shadow.focus }}</p>
        </div>
      </section>
      <section class="trace-section shadow-region">
        <header class="shadow-region-heading">
          <span>运行轨迹</span>
        </header>
        <div
          ref="trace"
          class="shadow-trace-box"
          @scroll="followTrace = atTail(trace)"
        >
          <p v-if="!model.shadow.trace.length" class="shadow-region-placeholder">
            {{ model.shadow.status === "运行中" ? "等待 DSH 动作输出" : "等待 DSH 运行" }}
          </p>
          <div v-else class="trace-current-activity">
            <header class="trace-current-heading">
              <i class="trace-current-mark" aria-hidden="true"></i>
              <strong>{{ model.shadow.trace[0]?.title }}</strong>
              <small>{{ model.shadow.trace[0]?.status }}</small>
            </header>
            <p v-if="model.shadow.trace[0]?.text" class="trace-current-text">
              {{ model.shadow.trace[0]?.text }}
            </p>
          </div>
        </div>
      </section>
      <section class="shadow-event shadow-region">
        <header class="shadow-region-heading"><span>最新进展</span></header>
        <div class="shadow-event-content"><p>{{ model.shadow.event || "等待新的进展" }}</p></div>
      </section>
      </div>
      <footer class="shadow-panel-footer">
        <div class="panel-resource-footer">
          <span
            >上下文 <strong>{{ model.shadow.context }}</strong></span
          ><span
            >缓存命中 <strong>{{ model.shadow.cache }}</strong></span
          >
        </div>
        <button
          type="button"
          class="outline-button console-button"
          title="在浏览器中打开 DSH（代理执行宿主）控制台"
          :disabled="!consoleEnabled"
          @click="emit('console')"
        >
          <span>{{ consoleLabel || '代理控制台' }}</span>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M7 7h10v10" /></svg>
        </button>
      </footer>
    </aside>
    <section ref="voiceColumn" class="system-column panel" :class="{ 'session-active': sessionActive }" :data-voice-state="model.state" aria-labelledby="voice-panel-title">
      <header class="voice-heading">
        <div class="voice-heading-row">
          <h2 id="voice-panel-title" class="panel-kicker">语音助手</h2>
          <div class="voice-heading-actions">
            <span class="status-pill">{{
              model.state === "offline"
                ? "离线"
                : model.state === "error"
                  ? "需检查"
                  : "本机运行"
            }}</span>
            <button
              type="button"
              class="workbench-settings-button compact-settings-button"
              aria-label="打开设置"
              title="打开设置"
              @click="emit('settings')"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 3-.7 2.4-2.4 1L3.8 6l-1.6 2.8 1.8 1.7v3l-1.8 1.7L3.8 18l2.1-.4 2.4 1L9 21h6l.7-2.4 2.4-1 2.1.4 1.6-2.8-1.8-1.7v-3l1.8-1.7L20.2 6l-2.1.4-2.4-1L15 3Z" /><circle cx="12" cy="12" r="3" /></svg>
              <span>设置</span>
            </button>
          </div>
        </div>
      </header>
      <div class="voice-focus">
        <div class="voice-presentation">
          <div class="orb-stage" :class="`orb-${model.state}`" :style="{ '--voice-energy': voiceEnergy }" aria-hidden="true">
            <span class="orb-ring"></span><span class="orb-ring second"></span>
            <div class="voice-orb">
              <span class="voice-waveform">
                <i v-for="(level, index) in waveformLevels" :key="index" :style="{ '--voice-level': level }"></i>
              </span>
            </div>
          </div>
          <div class="voice-runtime-state" :class="`voice-state-${model.state}`" role="status" aria-live="polite" aria-atomic="true">
            <i></i><span>{{ runtimeLabel }}</span>
          </div>
        </div>
        <div class="voice-controls">
          <UiButton class="voice-session-action" :class="{ 'voice-stop-button': sessionActive }" :disabled="model.disabled" @click="emit('action')">
            <i v-if="sessionActive" class="voice-stop-icon" aria-hidden="true"></i>{{ model.action }}
          </UiButton>
          <button
            v-if="model.canReset && !sessionActive"
            class="ghost-action"
            @click="emit('reset')"
          >
            重置当前会话
          </button>
          <div
            class="metric-card"
            :title="model.latencyPending ? '等待开始播报' : '从说完到开口的响应时间'"
          >
            <span>{{ sessionActive ? model.latencyPending ? '延迟计时' : '首音延迟' : '本轮响应' }}</span><strong>{{ latencyLabel }}</strong>
            <small v-if="model.latencyPending && !sessionActive">等待开始播报</small>
          </div>
          <Transition name="voice-mode">
            <section v-if="!sessionActive" class="mode-picker" title="切换模式后将在下一轮对话生效">
              <div class="section-row"><span>运行模式</span></div>
              <div class="mode-options">
                <button
                  v-for="mode in [
                    { id: 'fast', label: '快速' },
                    { id: 'deep', label: '深度' },
                  ]"
                  :key="mode.id"
                  :class="{ selected: model.mode === mode.id }"
                  :disabled="model.modeDisabled ?? model.disabled"
                  @click="emit('mode', mode.id as ReasoningMode)"
                >
                  {{ mode.label }}
                </button>
              </div>
            </section>
          </Transition>
        </div>
      </div>
    </section>
    <section class="conversation-column panel" aria-labelledby="conversation-panel-title">
      <header class="column-heading">
        <h2 id="conversation-panel-title" class="panel-kicker">会话</h2>
        <button
          type="button"
          class="workbench-settings-button"
          aria-label="打开设置"
          title="打开设置"
          @click="emit('settings')"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 3-.7 2.4-2.4 1L3.8 6l-1.6 2.8 1.8 1.7v3l-1.8 1.7L3.8 18l2.1-.4 2.4 1L9 21h6l.7-2.4 2.4-1 2.1.4 1.6-2.8-1.8-1.7v-3l1.8-1.7L20.2 6l-2.1.4-2.4-1L15 3Z" /><circle cx="12" cy="12" r="3" /></svg>
          <span>设置</span>
        </button>
      </header>
      <div
        ref="conversation"
        class="conversation-scroll"
        @scroll="followConversation = atTail(conversation)"
      >
        <div class="conversation-stream">
          <div
            v-if="!model.conversation.length && !model.partial"
            class="conversation-empty"
          >
            <span>◌</span>
            <h3>今天，想聊些什么？</h3>
            <p>开始语音会话后，直接说出你的想法。</p>
          </div>
          <ConversationMessage
            v-for="entry in model.conversation"
            :key="entry.key"
            :role="entry.role"
            :text="entry.text"
            :time="entry.time"
            :animate-on-mount="!initialMessageKeys.has(entry.key)"
            :status="entry.status"
            :speaking="entry.speaking"
            :delivered="entry.delivered"
          />
          <ConversationMessage
            v-if="model.partial"
            role="user"
            :text="model.partial"
            status="正在识别"
            partial
          />
          <ConversationMessage
            v-if="model.thinking"
            role="assistant"
            text="正在准备回复"
            pending
          />
        </div>
      </div>
      <footer class="conversation-footer">
        <span class="footer-mic"><i></i>{{ model.conversationHint }}</span>
        <div class="panel-resource-footer">
          <span
            >上下文 <strong>{{ model.voiceContext || "—" }}</strong></span
          ><span
            >缓存 <strong>{{ model.voiceCache || "—" }}</strong></span
          >
        </div>
      </footer>
    </section>
  </section>
</template>
