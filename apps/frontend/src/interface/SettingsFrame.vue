<script setup lang="ts">
import { settingsSections } from "./settings";
defineProps<{ section: string; status?: string }>();
const emit = defineEmits<{
  section: [value: (typeof settingsSections)[number]["id"]];
}>();
</script>
<template>
  <section class="settings-page">
    <header class="settings-heading">
      <div>
        <p class="panel-kicker">偏好设置</p>
        <h1>适合你的工作方式</h1>
        <p>调整声音、个性与协作方式，让每次对话更自然。</p>
      </div>
      <span v-if="status" class="settings-badge">{{ status }}</span>
    </header>
    <slot name="warnings" />
    <div class="settings-layout">
      <nav class="settings-tabs" aria-label="设置导航">
        <p class="settings-nav-label">应用偏好</p>
        <button
          v-for="item in settingsSections"
          :key="item.id"
          :class="{
            active:
              section === item.id ||
              (section === 'silence' && item.id === 'wake'),
          }"
          @click="emit('section', item.id)"
        >
          <span>{{ item.icon }}</span>
          <div>
            <strong>{{ item.label }}</strong
            ><small>{{ item.hint }}</small>
          </div>
        </button>
      </nav>
      <div class="settings-content"><slot /></div>
    </div>
  </section>
</template>
