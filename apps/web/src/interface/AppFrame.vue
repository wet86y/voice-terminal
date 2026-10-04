<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
defineProps<{
  page: string;
  runtime?: string;
  status: string;
  consoleEnabled?: boolean;
  consoleLabel?: string;
  desktop?: boolean;
  preview?: boolean;
}>();
const emit = defineEmits<{
  page: [value: "home" | "settings"];
  console: [];
  logs: [];
  exit: [];
}>();
const hidden = ref(false);
function visibility() {
  hidden.value = document.visibilityState === "hidden";
}
onMounted(() => {
  visibility();
  document.addEventListener("visibilitychange", visibility);
});
onBeforeUnmount(() =>
  document.removeEventListener("visibilitychange", visibility),
);
</script>
<template>
  <main
    class="app-shell"
    :class="[
      `runtime-${runtime || 'standby'}`,
      { 'app-hidden': hidden, 'is-preview': preview },
    ]"
  >
    <header class="app-topbar">
      <button
        class="brand"
        aria-label="回到工作台"
        @click="emit('page', 'home')"
      >
        <span class="brand-symbol"><i></i><i></i><i></i></span
        ><span
          ><strong>Voice Terminal</strong
          ><small>语音助手 · 随时陪伴</small></span
        >
      </button>
      <nav class="page-nav" aria-label="主导航">
        <button
          :class="{ active: page === 'home' }"
          @click="emit('page', 'home')"
        >
          工作台</button
        ><button
          :class="{ active: page === 'settings' }"
          @click="emit('page', 'settings')"
        >
          设置
        </button>
      </nav>
      <div class="app-actions">
        <span class="connection-status"
          ><i
            :class="{ offline: runtime === 'offline' || runtime === 'error' }"
          ></i
          >{{ status }}</span
        ><button
          class="outline-button console-button"
          :disabled="!consoleEnabled"
          @click="emit('console')"
        >
          {{ consoleLabel || "代理控制台" }}
        </button>
        <details v-if="desktop" class="app-menu">
          <summary aria-label="应用菜单">•••</summary>
          <div>
            <button @click="emit('logs')">打开日志目录</button
            ><button @click="emit('exit')">退出应用</button>
          </div>
        </details>
      </div>
    </header>
    <slot name="banner" />
    <slot />
    <slot name="overlays" />
  </main>
</template>
