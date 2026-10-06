<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import WakeTaskSchedule from '../../interface/WakeTaskSchedule.vue';
const {
  settings,
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
</script>
<template>
  <SettingsPageHeader section="wakes" />
  <div class="settings-stack">
    <p class="instruction-hint">
      手动任务保存任务说明，到期交给当时的后台主线；代理创建的冻结参考仍可按需读取。任务由 DSH（代理执行宿主）统一保存和调度。
    </p>
    <div class="wake-task-toolbar">
        <span>
          待执行
          {{ wakeTasks.filter((task) => task.status === "pending").length }} 项
          · 已归档
          {{ wakeTasks.filter((task) => task.status === "archived").length }} 项
        </span>
      <div class="wake-task-toolbar-actions"><button class="outline-button wake-task-button" type="button" :aria-expanded="addingWakeTask" :disabled="saving" @click="addWakeTask"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>添加</button><button class="outline-button wake-task-button" type="button" :disabled="saving" @click="refreshWakeTasks"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13 3M4.9 14.9a8 8 0 0 0 13 3" /></svg>刷新</button></div>
    </div>
    <section v-if="addingWakeTask" class="model-accordion-card wake-task-card wake-task-create" aria-label="添加定时任务">
      <h3>添加定时任务</h3>
      <div class="wake-task-editor">
        <WakeTaskSchedule v-model:rule-kind="wakeTaskCreateDraft.ruleKind" v-model:when="wakeTaskCreateDraft.when" v-model:time="wakeTaskCreateDraft.time" v-model:weekdays="wakeTaskCreateDraft.weekdays" :disabled="saving" />
        <label class="form-field wake-task-text-field"><span>任务说明</span><textarea v-model="wakeTaskCreateDraft.text" maxlength="500" rows="2" aria-label="任务说明" placeholder="到时需要提醒或处理什么？" :disabled="saving"></textarea></label>
        <small>手动创建只保存任务说明，不冻结会话正文。</small>
        <div class="wake-task-editor-actions"><button class="outline-button wake-task-button" type="button" :disabled="saving" @click="addingWakeTask = false">取消</button><button class="save-button compact-save" type="button" :disabled="saving || !wakeTaskCreateDraft.text.trim() || (wakeTaskCreateDraft.ruleKind === 'at' ? !wakeTaskCreateDraft.when : !wakeTaskCreateDraft.time) || (wakeTaskCreateDraft.ruleKind === 'weekly' && !wakeTaskCreateDraft.weekdays)" @click="createWakeTask">{{ saving ? '正在添加…' : '添加任务' }}</button></div>
      </div>
    </section>
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
              >{{ task.creation_source === 'manual' ? '手动添加' : task.creation_source === 'agent' ? '代理创建' : '历史来源未知' }} · 参考上下文：{{ task.has_reference ? "已冻结，可按需读取" : "无"
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
      还没有唤醒任务。点击“添加”创建，或由 DSH（代理执行宿主）中的代理创建。
    </div>
  </div>
</template>
