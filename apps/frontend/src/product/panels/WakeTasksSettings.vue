<script setup lang="ts">
import { useProductContext } from "../product-context";
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
  <header>
    <p class="panel-kicker">项目级唤醒</p>
    <h2>定时唤醒任务</h2>
    <p>
      任意 DSH（影子代理宿主）会话都可创建任务；DSH
      插件统一保存和调度，到期投递至当前语音关联会话。冻结的参考上下文供模型按需读取。
    </p>
  </header>
  <div class="settings-stack">
    <div class="speaker-card-heading">
      <div>
        <span class="section-label">定时任务</span>
        <p>
          待执行
          {{ wakeTasks.filter((task) => task.status === "pending").length }} 项
          · 已归档
          {{ wakeTasks.filter((task) => task.status === "archived").length }} 项
        </p>
      </div>
      <button class="ghost-action" type="button" @click="refreshWakeTasks">
        刷新
      </button>
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
              type="button"
              @click="
                editingWakeTaskId === task.task_id
                  ? (editingWakeTaskId = '')
                  : editWakeTask(task)
              "
            >
              {{ editingWakeTaskId === task.task_id ? "收起" : "修改" }}</button
            ><button
              v-if="task.status === 'pending'"
              class="danger-action"
              type="button"
              @click="removeWakeTask(task)"
            >
              取消
            </button>
          </div>
        </div>
        <div v-if="editingWakeTaskId === task.task_id" class="model-edit-panel">
          <div class="provider-fields">
            <label
              v-if="
                !['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
              "
              ><span>触发时间（项目本地时间）</span
              ><input
                v-model="wakeTaskDraft.when"
                type="datetime-local" /></label
            ><label
              v-if="
                !['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
              "
              ><span>固定重复间隔</span
              ><input
                v-model="wakeTaskDraft.repeat"
                placeholder="留空表示一次性，例如 1h、1d"
                type="text" /></label
            ><label
              v-if="
                ['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
              "
              ><span>{{
                wakeTaskDraft.ruleKind === "cron"
                  ? "Cron（定时表达式）"
                  : "本地时间 HH:mm:ss"
              }}</span
              ><input v-model="wakeTaskDraft.ruleText" type="text" /></label
            ><label
              v-if="
                ['daily', 'weekly', 'cron'].includes(wakeTaskDraft.ruleKind)
              "
              ><span>时区</span
              ><input v-model="wakeTaskDraft.timeZone" type="text" /></label
            ><label v-if="wakeTaskDraft.ruleKind === 'weekly'"
              ><span>星期（1 至 7，逗号分隔）</span
              ><input v-model="wakeTaskDraft.weekdays" type="text" /></label
            ><label class="wake-task-text-field"
              ><span>任务说明</span
              ><textarea
                v-model="wakeTaskDraft.text"
                maxlength="500"
                rows="3"
              ></textarea>
            </label>
          </div>
          <div class="edit-panel-footer">
            <span>冻结参考上下文保持不变。</span>
            <div>
              <button
                class="ghost-action"
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
                    : !wakeTaskDraft.when)
                "
                @click="saveWakeTask"
              >
                保存修改
              </button>
            </div>
          </div>
        </div>
      </article>
    </div>
    <div v-else class="pool-empty">
      还没有唤醒任务。可在 DSH 会话中使用 schedule_wake（创建唤醒）工具添加。
    </div>
  </div>
</template>
