<script setup lang="ts">
import { computed } from 'vue';
import { settingsSections } from "./settings";
const props = defineProps<{
  section: string; // section（当前设置分区）。
  desktop?: boolean; // desktop（是否提供原生桌面操作）。
}>();
// activeSection（当前入口）：旧分区地址继续高亮合并后的页签。
const activeSection = computed(() => props.section === 'silence' ? 'wake' : props.section === 'system' ? 'compute' : props.section);
const emit = defineEmits<{
  section: [value: (typeof settingsSections)[number]["id"]];
  back: []; // back（返回工作台）。
  logs: []; // logs（打开应用日志目录）。
  exit: []; // exit（退出桌面应用）。
}>();
</script>
<template>
  <section class="settings-page">
    <div class="settings-layout">
      <aside class="settings-sidebar" aria-label="设置侧栏">
        <header class="settings-sidebar-heading">
          <button
            type="button"
            class="settings-back-button"
            aria-label="返回工作台"
            title="返回工作台"
            @click="emit('back')"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>
            <span>返回</span>
          </button>
          <strong class="settings-sidebar-title">设置</strong>
        </header>
        <nav class="settings-tabs" aria-label="设置导航">
          <button
            v-for="item in settingsSections"
            :key="item.id"
            :title="item.label"
            :aria-label="item.label"
            :aria-current="activeSection === item.id ? 'page' : undefined"
            :class="{
              active:
                activeSection === item.id,
            }"
            @click="emit('section', item.id)"
          >
            <span aria-hidden="true">{{ item.icon }}</span>
            <div>
              <strong>{{ item.label }}</strong
              ><small>{{ item.hint }}</small>
            </div>
          </button>
        </nav>
        <footer v-if="desktop" class="settings-sidebar-actions">
          <button type="button" title="打开日志目录" aria-label="打开日志目录" @click="emit('logs')">日志</button>
          <button type="button" title="退出应用" @click="emit('exit')">退出</button>
        </footer>
      </aside>
      <div class="settings-content"><slot name="warnings" /><slot /></div>
    </div>
  </section>
</template>
