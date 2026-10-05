<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { WorkbenchModel } from "./workbench";
import UiButton from "./UiButton.vue";
const props = defineProps<{ model: WorkbenchModel }>();
const emit = defineEmits<{
  action: [];
  reset: [];
  mode: [value: "fast" | "balanced" | "deep"];
  expand: [];
  models: [];
}>();
const conversation = ref<HTMLElement | null>(null);
const trace = ref<HTMLElement | null>(null);
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
onBeforeUnmount(() => observer?.disconnect());
</script>
<template>
  <section class="workbench" aria-label="语音工作台">
    <aside class="shadow-column panel">
      <header class="column-heading">
        <div>
          <p class="panel-kicker">✦ 后台协作</p>
          <h2>影子代理</h2>
        </div>
        <button
          class="ghost-action"
          :aria-expanded="model.shadow.expanded"
          @click="emit('expand')"
        >
          {{ model.shadow.expanded ? "收起" : "展开" }}
        </button>
      </header>
      <div class="shadow-summary-card">
        <span class="status-pill">{{ model.shadow.status }}</span>
        <h3>{{ model.shadow.title }}</h3>
        <p v-if="model.shadow.focus">{{ model.shadow.focus }}</p>
      </div>
      <section class="trace-section">
        <header>
          <span>运行轨迹</span
          ><small>{{ model.shadow.trace.length }} 项活动</small>
        </header>
        <div
          ref="trace"
          class="shadow-trace-box"
          @scroll="followTrace = atTail(trace)"
        >
          <ol class="timeline">
            <li v-for="item in model.shadow.trace" :key="item.key">
              <i></i>
              <div>
                <strong>{{ item.title }}</strong>
                <p v-if="item.text">{{ item.text }}</p>
                <small>{{ item.status }} · {{ item.time }}</small>
              </div>
            </li>
            <li v-if="!model.shadow.trace.length" class="empty-state">
              对话开始后，协作进展会显示在这里。
            </li>
          </ol>
        </div>
      </section>
      <section class="shadow-event">
        <p class="section-label">最新进展</p>
        <p>{{ model.shadow.event || "等待新的进展" }}</p>
      </section>
      <div v-if="model.shadow.expanded" class="info-callout">
        {{ model.shadow.status }} · {{ model.shadow.focus || "暂无待处理事项" }}
      </div>
      <footer class="panel-resource-footer">
        <span
          >上下文 <strong>{{ model.shadow.context }}</strong></span
        ><span
          >缓存 <strong>{{ model.shadow.cache }}</strong></span
        >
      </footer>
    </aside>
    <section class="system-column panel">
      <p class="panel-kicker"><i class="eyebrow-dot"></i>语音助手</p>
      <div class="system-heading">
        <h2>{{ model.title }}</h2>
        <span class="status-pill">{{
          model.state === "offline"
            ? "离线"
            : model.state === "error"
              ? "需检查"
              : "本机运行"
        }}</span>
      </div>
      <div class="orb-stage" :class="`orb-${model.state}`">
        <span class="orb-ring"></span><span class="orb-ring second"></span>
        <div class="voice-orb"><i></i><i></i><i></i><i></i><i></i></div>
      </div>
      <p class="orb-caption">{{ model.description }}</p>
      <UiButton :disabled="model.disabled" @click="emit('action')">{{
        model.action
      }}</UiButton
      ><button
        v-if="model.canReset"
        class="ghost-action"
        @click="emit('reset')"
      >
        重置当前会话
      </button>
      <div class="metric-card">
        <span>本轮响应</span><strong>{{ model.latency }}</strong
        ><small>{{
          model.latencyPending ? "等待开始播报" : "从说完到开口"
        }}</small>
        <div class="metric-spark">
          <i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i>
        </div>
      </div>
      <section class="mode-picker">
        <div class="section-row">
          <span>运行模式</span><small>下一轮生效</small>
        </div>
        <div class="mode-options">
          <button
            v-for="mode in [
              { id: 'fast', label: '快速' },
              { id: 'balanced', label: '均衡' },
              { id: 'deep', label: '深度' },
            ]"
            :key="mode.id"
            :class="{ selected: model.mode === mode.id }"
            :disabled="model.modeDisabled ?? model.disabled"
            @click="emit('mode', mode.id as 'fast' | 'balanced' | 'deep')"
          >
            {{ mode.label }}
          </button>
        </div>
      </section>
      <button class="model-summary" @click="emit('models')">
        <span
          ><small>语音模型</small><strong>{{ model.voiceModel }}</strong></span
        ><span
          ><small>协作模型</small><strong>{{ model.shadowModel }}</strong></span
        ><span v-if="model.ttsModel"
          ><small>语音合成</small><strong>{{ model.ttsModel }}</strong></span
        ><span><small>协作推理</small><strong>High（高强度）</strong></span
        ><em>设置 ↗</em>
      </button>
    </section>
    <section class="conversation-column panel">
      <header class="column-heading">
        <div>
          <p class="panel-kicker">会话</p>
          <h2>从一句话开始</h2>
        </div>
        <span class="status-pill">语音交互</span>
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
          <article
            v-for="entry in model.conversation"
            :key="entry.key"
            class="message"
            :class="[`${entry.role}-message`, { speaking: entry.speaking }]"
          >
            <header>
              <span class="avatar">{{
                entry.role === "user" ? "你" : "✦"
              }}</span
              ><strong>{{ entry.role === "user" ? "你" : "语音助手" }}</strong
              ><time>{{ entry.time }}</time>
            </header>
            <p>{{ entry.text }}</p>
            <small v-if="entry.status">{{ entry.status }}</small>
            <p v-if="entry.delivered" class="delivery-copy">
              {{ entry.delivered }}
            </p>
          </article>
          <div v-if="model.partial" class="live-asr">
            <span class="live-dot"></span><span>正在识别</span>
            <p>{{ model.partial }}</p>
          </div>
          <div v-if="model.thinking" class="assistant-pending">
            <i></i><i></i><i></i><span>正在准备回复</span>
          </div>
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
