<script setup lang="ts">
import { computed } from 'vue'
import { settingsSections, type SettingsSection } from './settings'

const props = defineProps<{
  /** section（当前设置分区）：标题与导航保持一致。 */
  section: SettingsSection
  /** titleId（标题元素标识）：供页面的无障碍说明关联。 */
  titleId?: string
  /** title（自定义标题）：用于合并页面中的独立设置区域。 */
  title?: string
  /** description（自定义说明）：用于补充独立设置区域的用途。 */
  description?: string
  /** compact（紧凑标题）：用于同页内的次级设置区域。 */
  compact?: boolean
}>()
// heading（当前页面文案）：各页面共用名称与简短说明。
const heading = computed(() => settingsSections.find(item => item.id === props.section)!)
const headingTitle = computed(() => props.title ?? heading.value.label) // headingTitle（显示标题）：优先采用区域标题。
const headingDescription = computed(() => props.description ?? heading.value.description) // headingDescription（显示说明）：优先采用区域说明。
</script>

<template>
  <header class="settings-page-heading" :class="{ 'settings-page-heading-compact': compact }">
    <div class="settings-page-heading-copy">
      <h2 :id="titleId">{{ headingTitle }}</h2>
      <p>{{ headingDescription }}</p>
    </div>
    <!-- actions（页面操作）：在标题区右侧居中，空间不足时自动换行。 -->
    <div v-if="$slots.actions" class="settings-page-heading-actions"><slot name="actions" /></div>
  </header>
</template>

<style scoped>
/* page heading（页面标题）：沿用本地组件的两层标题，字号不随窗口缩放。 */
.settings-page-heading{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px 16px;min-width:0}
.settings-page-heading-copy{min-width:0}
.settings-page-heading h2{font-size:var(--font-heading);font-weight:600;line-height:1.6;letter-spacing:-.025em;margin:0 0 6px}
.settings-page-heading-compact h2{font-size:var(--font-section);line-height:1.4;letter-spacing:0;margin-bottom:2px}
.settings-page-heading p{font-size:var(--font-caption);line-height:1.65;color:var(--muted);margin:0}
.settings-page-heading-actions{display:flex;align-items:center;gap:8px;flex-shrink:0;max-width:100%;margin-left:auto}
.settings-page-heading-actions :deep(button){min-height:var(--compact-control-height);padding:6px 10px;font-size:var(--font-body);max-width:100%}
</style>
