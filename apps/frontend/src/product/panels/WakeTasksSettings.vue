<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import WakeTaskSchedule from '../../interface/WakeTaskSchedule.vue';
const {
  settings,
  saving,
  wakeTasks,
  editingWakeTaskId,
  wakeTaskDraft,
  refreshWakeTasks,
  editWakeTask,
  saveWakeTask,
  removeWakeTask,
  formatTime,
} = useProductContext();
</script>
<template>
  <SettingsPageHeader section="wakes" />
  <div class="settings-stack">
    <p class="instruction-hint">
      任意 DSH（影子代理宿主）会话都可创建任务；DSH（影子代理宿主）插件统一保存和调度，到期投递至当前语音关联会话。冻结的参考上下文供模型按需读取。
    </p>
    <div class="wake-task-toolbar">
        <span>
          待执行
          {{ wakeTasks.filter((task) => task.status === "pending").length }} 项
          · 已归档
          {{ wakeTasks.filter((task) => task.status === "archived").length }} 项
        </span>
      <div class="wake-task-toolbar-actions"><button class="outline-button wake-task-button" type="button" :disabled="saving" @click="refreshWakeTasks"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13 3M4.9 14.9a8 8 0 0 0 13 3" /></svg>刷新</button></div>
    </div>
    <div v-if="wakeTasks.length" class="wake-task-list">
      <article
        v-for="task in wakeTasks"
        :key="task.task_id"
        class="model-accordion-card wake-task-card"
      >
        <div class="wake-task-summary">
          <div>
            <div class="pool-entry-title">
              <strong>{{ task.text }}</strong
              ><span class="entry-status" :class="task.status">{{
                task.status === "pending"
                  ? "待执行"
                  : task.status === "archived"
                    ? "已归档"
                    : "已取消"
              }}</span>
            </div>
            <small
              >规则：{{
                task.rule_kind === "daily"
                  ? "每日"
                  : task.rule_kind === "weekly"
                    ? "每周"
                    : task.rule_kind === "cron"
                      ? "Cron（定时表达式）"
                      : task.rule_kind === "every"
                        ? "固定间隔"
                        : "单次"
              }}
              · 下次：{{ task.when ? formatTime(task.when) : "—"
              }}<template v-if="task.repeat">
                · 每 {{ task.repeat }} 重复</template
              ></small
            ><small
              >参考上下文：{{ task.has_reference ? "已冻结，可按需读取" : "无"
              }}<template v-if="task.last_triggered_at">
                · 最近触发：{{ formatTime(task.last_triggered_at) }}</template
              ></small
            >
          </div>
          <div class="pool-entry-actions">
            <button
              v-if="task.status === 'pending'"
              class="outline-button wake-task-button"
              type="button"
              :aria-expanded="editingWakeTaskId === task.task_id"
              :disabled="saving"
              @click="
                editingWakeTaskId === task.task_id
                  ? (editingWakeTaskId = '')
                  : editWakeTask(task)
              "
            >
              {{ editingWakeTaskId === task.task_id ? "收起" : "修改" }}</button
            ><button
              v-if="task.status === 'pending'"
              class="outline-button wake-task-button danger-action"
              type="button"
              :disabled="saving"
              @click="removeWakeTask(task)"
            >
              取消任务
            </button>
          </div>
        </div>
        <div v-if="editingWakeTaskId === task.task_id" class="model-edit-panel wake-task-editor">
          <WakeTaskSchedule v-model:rule-kind="wakeTaskDraft.ruleKind" v-model:when="wakeTaskDraft.when" v-model:time="wakeTaskDraft.ruleText" v-model:weekdays="wakeTaskDraft.weekdays" :disabled="saving" advanced>
            <template #cron><label class="form-field"><span>定时表达式</span><input v-model="wakeTaskDraft.ruleText" type="text" :disabled="saving" /></label></template>
            <template #interval><label class="form-field task-interval-field"><span>固定重复间隔</span><input v-model="wakeTaskDraft.repeat" placeholder="例如 1h（1 小时）、1d（1 天）" type="text" :disabled="saving" /></label></template>
          </WakeTaskSchedule>
          <label class="form-field wake-task-text-field"><span>任务说明</span><textarea v-model="wakeTaskDraft.text" maxlength="500" rows="2" :disabled="saving"></textarea></label>
          <div class="wake-task-editor-actions">
              <button
                class="outline-button wake-task-button"
                type="button"
                @click="editingWakeTaskId = ''"
              >
                取消</button
              ><button
                class="save-button compact-save"
                type="button"
                :disabled="
                  saving ||
                  !wakeTaskDraft.text.trim() ||
                  (['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
                    ? !wakeTaskDraft.ruleText.trim()
                    : !wakeTaskDraft.when) ||
                  (wakeTaskDraft.ruleKind === 'weekly' && !wakeTaskDraft.weekdays) ||
                  (wakeTaskDraft.ruleKind === 'every' && !wakeTaskDraft.repeat)
                "
                @click="saveWakeTask"
              >
                保存修改
              </button>
          </div>
        </div>
      </article>
    </div>
    <div v-else class="pool-empty">
      还没有唤醒任务。可在 DSH 会话中使用 schedule_wake（创建唤醒）工具添加。
    </div>
  </div>
</template>
