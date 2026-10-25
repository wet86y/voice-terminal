<script setup lang="ts">
import { computed, ref } from 'vue';
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import WakeTaskSchedule from '../../interface/WakeTaskSchedule.vue';
import WakeTaskTabs from '../../interface/WakeTaskTabs.vue';
import { validWakeTriggerCount, wakeIntervalSeconds } from '../../interface/wake-task-interval';
const {
  saving,
  wakeTasks,
  addingWakeTask,
  wakeTaskCreateDraft,
  addWakeTask,
  createWakeTask,
  editingWakeTaskId,
  wakeTaskDraft,
  refreshWakeTasks,
  editWakeTask,
  saveWakeTask,
  removeWakeTask,
  formatTime,
} = useProductContext();
const taskView = ref<'pending' | 'archived'>('pending'); // taskView（当前任务分类）。
const pendingTasks = computed(() => wakeTasks.value.filter(task => task.status === 'pending'));
const archivedTasks = computed(() => wakeTasks.value.filter(task => task.status !== 'pending'));
const visibleTasks = computed(() => taskView.value === 'pending' ? pendingTasks.value : archivedTasks.value);
function addTask() { taskView.value = 'pending'; addWakeTask(); }
function intervalIsValid(hours: string, minutes: string) { return wakeIntervalSeconds(hours, minutes) !== null; }
function countIsValid(count: string) { return validWakeTriggerCount(count); }
function deliveredCount(task: { occurrence: number }) { return Math.max(0, task.occurrence - 1); }
function formatRepeat(value: string) {
  const match = /^(\d+)s$/u.exec(value);
  if (!match) return value;
  const seconds = Number(match[1]);
  if (seconds % 60 !== 0) return `${seconds} 秒`;
  const minutes = seconds / 60;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return hours && remainder ? `${hours} 小时 ${remainder} 分钟` : hours ? `${hours} 小时` : `${minutes} 分钟`;
}
</script>
<template>
  <SettingsPageHeader section="wakes" />
  <div class="settings-stack">
    <p class="instruction-hint">
      手动任务保存任务说明，到期交给当时的后台主线；代理创建的冻结参考仍可按需读取。任务由 DSH（代理执行宿主）统一保存和调度。
    </p>
    <div class="wake-task-toolbar">
      <WakeTaskTabs v-model="taskView" :pending="pendingTasks.length" :archived="archivedTasks.length" />
      <div class="wake-task-toolbar-actions"><button class="outline-button wake-task-button" type="button" :aria-expanded="addingWakeTask" :disabled="saving" @click="addTask"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>添加</button><button class="outline-button wake-task-button" type="button" :disabled="saving" @click="refreshWakeTasks"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13 3M4.9 14.9a8 8 0 0 0 13 3" /></svg>刷新</button></div>
    </div>
    <section v-if="addingWakeTask" class="model-accordion-card wake-task-card wake-task-create" aria-label="添加定时任务">
      <h3>添加定时任务</h3>
      <div class="wake-task-editor">
        <WakeTaskSchedule v-model:rule-kind="wakeTaskCreateDraft.ruleKind" v-model:when="wakeTaskCreateDraft.when" v-model:time="wakeTaskCreateDraft.time" v-model:weekdays="wakeTaskCreateDraft.weekdays" v-model:interval-hours="wakeTaskCreateDraft.intervalHours" v-model:interval-minutes="wakeTaskCreateDraft.intervalMinutes" v-model:loop="wakeTaskCreateDraft.loop" v-model:trigger-count="wakeTaskCreateDraft.triggerCount" :disabled="saving" />
        <label class="form-field wake-task-text-field"><span>任务说明</span><textarea v-model="wakeTaskCreateDraft.text" maxlength="500" rows="2" aria-label="任务说明" placeholder="到时需要提醒或处理什么？" :disabled="saving"></textarea></label>
        <small>手动创建只保存任务说明，不冻结会话正文。</small>
        <div class="wake-task-editor-actions"><button class="outline-button wake-task-button" type="button" :disabled="saving" @click="addingWakeTask = false">取消</button><button class="save-button compact-save" type="button" :disabled="saving || !wakeTaskCreateDraft.text.trim() || (wakeTaskCreateDraft.ruleKind === 'at' && !wakeTaskCreateDraft.when) || (['daily', 'weekly'].includes(wakeTaskCreateDraft.ruleKind) && !/^\d{2}:\d{2}(?::\d{2})?$/.test(wakeTaskCreateDraft.time)) || (wakeTaskCreateDraft.ruleKind === 'weekly' && !wakeTaskCreateDraft.weekdays) || (wakeTaskCreateDraft.ruleKind === 'every' && (!intervalIsValid(wakeTaskCreateDraft.intervalHours, wakeTaskCreateDraft.intervalMinutes) || (!wakeTaskCreateDraft.loop && !countIsValid(wakeTaskCreateDraft.triggerCount))))" @click="createWakeTask">{{ saving ? '正在添加…' : '添加任务' }}</button></div>
      </div>
    </section>
    <div v-if="visibleTasks.length" class="wake-task-list">
      <article
        v-for="task in visibleTasks"
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
                      ? "自定义规则"
                      : task.rule_kind === "every"
                        ? "间隔"
                        : "单次"
              }}
              · 下次：{{ task.when ? formatTime(task.when) : "—"
              }}<template v-if="task.repeat">
                · 间隔 {{ formatRepeat(task.repeat) }}<template v-if="task.loop !== false">（循环）</template></template
              ><template v-if="task.rule_kind === 'every' && task.loop === false">
                · 共 {{ task.trigger_count }} 次（已触发 {{ Math.min(deliveredCount(task), task.trigger_count || 0) }} 次）</template
              ></small
            ><small
              >{{ task.creation_source === 'manual' ? '手动添加' : task.creation_source === 'agent' ? '代理创建' : '历史来源未知' }} · 参考上下文：{{ task.has_reference ? "已冻结，可按需读取" : "无"
              }}<template v-if="task.last_triggered_at">
                · 最近触发：{{ formatTime(task.last_triggered_at) }}</template
              ></small
            >
            <small v-if="task.status === 'pending' && task.rule_kind === 'cron'">该任务的自定义规则请通过 DSH（代理执行宿主）任务工具修改。</small>
          </div>
          <div class="pool-entry-actions">
            <button
              v-if="task.status === 'pending' && task.rule_kind !== 'cron'"
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
              class="outline-button wake-task-button danger-action"
              type="button"
              :disabled="saving"
              @click="removeWakeTask(task)"
            >
              {{ task.status === 'pending' ? '取消任务' : '删除' }}
            </button>
          </div>
        </div>
        <div v-if="editingWakeTaskId === task.task_id" class="model-edit-panel wake-task-editor">
          <WakeTaskSchedule v-model:rule-kind="wakeTaskDraft.ruleKind" v-model:when="wakeTaskDraft.when" v-model:time="wakeTaskDraft.ruleText" v-model:weekdays="wakeTaskDraft.weekdays" v-model:interval-hours="wakeTaskDraft.intervalHours" v-model:interval-minutes="wakeTaskDraft.intervalMinutes" v-model:loop="wakeTaskDraft.loop" v-model:trigger-count="wakeTaskDraft.triggerCount" :disabled="saving" />
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
                    : wakeTaskDraft.ruleKind === 'every'
                      ? !intervalIsValid(wakeTaskDraft.intervalHours, wakeTaskDraft.intervalMinutes) || (!wakeTaskDraft.loop && !countIsValid(wakeTaskDraft.triggerCount))
                      : !wakeTaskDraft.when) ||
                  (wakeTaskDraft.ruleKind === 'weekly' && !wakeTaskDraft.weekdays)
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
      {{ taskView === 'archived' ? '还没有已归档任务。' : '还没有待执行任务。点击“添加”创建任务。' }}
    </div>
  </div>
</template>
