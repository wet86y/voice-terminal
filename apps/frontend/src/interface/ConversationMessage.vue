<script setup lang="ts">
import { computed } from "vue";
import { useProgressiveText } from './progressive-text';
const props = withDefaults(defineProps<{
  role: "user" | "assistant"; // role（消息发送方）：用户或语音助手。
  text: string; // text（消息正文）。
  time?: string; // time（消息时间）。
  status?: string; // status（识别或播报状态）。
  delivered?: string; // delivered（已实际播报的内容）。
  speaking?: boolean; // speaking（当前消息正在播报）。
  partial?: boolean; // partial（正在识别的临时消息）。
  pending?: boolean; // pending（等待助手回复的临时消息）。
  animateOnMount?: boolean; // animateOnMount（新增消息首次逐字出现）：已存在的记录直接呈现，更新仍逐字续写。
}>(), { animateOnMount: true });
const displayedText = useProgressiveText(() => props.text, props.animateOnMount !== false, () => Boolean(props.pending), {
  correctInPlace: () => props.role === 'user', // correctInPlace（识别改字）：更新已有正文，不清空后缀。
  streaming: () => Boolean(props.partial), // streaming（识别批次渐显）：完成后继续沿用显示进度。
});
// visibleStatus（可见消息状态）：只保留识别中、异常等需要用户关注的提示。
const visibleStatus = computed(() => {
  const status = props.status || "";
  return /^(已完成识别|已完成播报|已完整传达|正在播报|等待播放|等待播报结算|固定日志回放|已记录)/.test(status) ? "" : status;
});
</script>

<template>
  <article
    class="message"
    :class="[
      `${role}-message`,
      { speaking, 'live-asr': partial, 'assistant-pending': pending },
    ]"
    :aria-label="role === 'user' ? '你的消息' : '语音助手的消息'"
  >
    <div class="message-author">
      <span class="avatar" aria-hidden="true">
        <svg v-if="role === 'user'" viewBox="0 0 24 24">
          <circle cx="12" cy="8" r="3.5" />
          <path d="M5.5 20v-1.5a6.5 6.5 0 0 1 13 0V20" />
        </svg>
        <svg v-else viewBox="0 0 24 24">
          <path d="M5 10v4M9.5 6v12M14 4v16M18.5 8v8" />
        </svg>
      </span>
      <time v-if="time" class="message-time">{{ time }}</time>
    </div>
    <div class="message-content">
      <div class="message-bubble">
        <div v-if="pending" class="message-typing">
          <span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>
          <span>{{ text }}</span>
        </div>
        <p v-else class="message-text">
          <span v-if="displayedText !== text" class="message-text-measure" aria-hidden="true">{{ text }}</span>
          <span class="message-text-visible" aria-hidden="true">{{ displayedText }}</span>
          <span class="message-text-source">{{ text }}</span>
        </p>
      </div>
      <small v-if="visibleStatus" class="message-status">
        <span v-if="partial" class="live-dot" aria-hidden="true"></span>
        {{ visibleStatus }}
      </small>
    </div>
  </article>
</template>
