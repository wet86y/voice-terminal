<script setup lang="ts">
defineProps<{
  role: "user" | "assistant"; // role（消息发送方）：用户或语音助手。
  text: string; // text（消息正文）。
  time?: string; // time（消息时间）。
  status?: string; // status（识别或播报状态）。
  delivered?: string; // delivered（已实际播报的内容）。
  speaking?: boolean; // speaking（当前消息正在播报）。
  partial?: boolean; // partial（正在识别的临时消息）。
  pending?: boolean; // pending（等待助手回复的临时消息）。
}>();
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
    <span class="avatar" aria-hidden="true">
      <svg v-if="role === 'user'" viewBox="0 0 24 24">
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5.5 20v-1.5a6.5 6.5 0 0 1 13 0V20" />
      </svg>
      <svg v-else viewBox="0 0 24 24">
        <path d="M5 10v4M9.5 6v12M14 4v16M18.5 8v8" />
      </svg>
    </span>
    <div class="message-content">
      <header class="message-header">
        <strong>{{ role === "user" ? "你" : "语音助手" }}</strong>
        <time v-if="time">{{ time }}</time>
      </header>
      <div class="message-bubble">
        <div v-if="pending" class="message-typing">
          <span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span>
          <span>{{ text }}</span>
        </div>
        <p v-else class="message-text">{{ text }}</p>
        <p v-if="delivered" class="delivery-copy">{{ delivered }}</p>
      </div>
      <small v-if="status" class="message-status">
        <span v-if="partial" class="live-dot" aria-hidden="true"></span>
        {{ status }}
      </small>
    </div>
  </article>
</template>
