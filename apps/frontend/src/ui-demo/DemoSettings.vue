<script setup lang="ts">
import { computed, reactive, ref, toRef, watch } from 'vue'
import SilenceTimeoutControl from '../interface/SilenceTimeoutControl.vue'
import LocalComponents from '../interface/LocalComponents.vue'
import SettingsPageHeader from '../interface/SettingsPageHeader.vue'
import TimePicker from '../interface/TimePicker.vue'
import WakeTaskSchedule from '../interface/WakeTaskSchedule.vue'
import InstructionEditorRow from '../interface/InstructionEditorRow.vue'
import SpeakerRecordingFlow from '../interface/SpeakerRecordingFlow.vue'
import WakeTaskTabs from '../interface/WakeTaskTabs.vue'
import FixedModelCredentials from '../interface/FixedModelCredentials.vue'
import { useLocalComponentsDemo } from './useLocalComponentsDemo'
import {
  demoAmbientRecords, demoInstructions,
  demoSettings, demoSpeakerPrompts, demoSpeakerTemplates, demoWakeTasks,
  longIdentifier, longText, previewRows, primaryPersonaDocuments,
  type DemoWakeTask, type PreviewScene, type SettingsTab,
} from './demo-data'

const props = defineProps<{
  /** tab（当前展示的设置页）。 */ tab: SettingsTab
  /** scene（固定展示场景）。 */ scene: PreviewScene
}>()
const emit = defineEmits<{
  /** feedback（展示操作提示）。 */ feedback: [message: string]
  /** confirm（展示确认弹窗）：apply（应用配置）或 exit（退出应用）。 */ confirm: [purpose: 'apply' | 'exit']
}>()
const form = reactive(structuredClone(demoSettings))
const documents = reactive(Object.fromEntries(primaryPersonaDocuments.map(item => [item.id, item.value])))
const activeDocument = ref('personality')
const currentDocument = computed(() => primaryPersonaDocuments.find(item => item.id === activeDocument.value) || primaryPersonaDocuments[0])
const instructions = reactive(structuredClone(demoInstructions))
const recordingPreview = ref(false) // recordingPreview（声纹录制面板是否展开）。
const speakerSegments = ref([0]) // speakerSegments（已完成的演示片段），初始保留一个示例。
const speakerRecording = ref<number | null>(null) // speakerRecording（当前录制的演示片段）。
const speakerCanSubmit = computed(() => speakerSegments.value.length === demoSpeakerPrompts.length && speakerRecording.value === null && !loading.value) // speakerCanSubmit（录制完成后允许保存）。
const editingTask = ref('')
const addingTask = ref(false) // addingTask（添加表单是否展开）。
const deepseekKey = ref('') // deepseekKey（演示语言服务密钥草稿）。
const minimaxKey = ref('') // minimaxKey（演示语音合成密钥草稿）。
const taskView = ref<'pending' | 'archived'>('pending') // taskView（当前演示任务分类）。
const tasks = ref(previewRows(demoWakeTasks, props.scene))
const templates = computed(() => previewRows(demoSpeakerTemplates, props.scene))
const ambient = computed(() => previewRows(demoAmbientRecords, props.scene))
const instructionRows = computed(() => props.scene === 'empty' ? [] : instructions)
const loading = computed(() => props.scene === 'loading')
const pendingTasks = computed(() => tasks.value.filter(item => item.status === '待执行'))
const archivedTasks = computed(() => tasks.value.filter(item => item.status !== '待执行'))
const visibleTasks = computed(() => taskView.value === 'pending' ? pendingTasks.value : archivedTasks.value)
function feedback(label: string) { emit('feedback', label) }
function toggleSpeakerRecording() {
  speakerRecording.value = null
  recordingPreview.value = !recordingPreview.value
}
function recordSpeakerSegment(index: number) {
  if (loading.value || (speakerRecording.value !== null && speakerRecording.value !== index)) return
  if (speakerRecording.value === index) {
    if (!speakerSegments.value.includes(index)) speakerSegments.value.push(index)
    speakerRecording.value = null
  } else speakerRecording.value = index
}
function cancelSpeakerRecording() {
  speakerRecording.value = null
  speakerSegments.value = [0]
  recordingPreview.value = false
}
function saveSpeakerRecording() {
  if (!speakerCanSubmit.value) return
  recordingPreview.value = false
  feedback('声纹资料已保存')
}
const localComponents = useLocalComponentsDemo(toRef(props, 'scene'), feedback)
watch(() => props.scene, scene => { tasks.value = previewRows(demoWakeTasks, scene); editingTask.value = ''; addingTask.value = false; cancelSpeakerRecording() })
watch(() => props.tab, () => { speakerRecording.value = null })
const taskCanSave = computed(() => Boolean(form.taskText.trim()) && (form.taskRule === 'at'
  ? /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(form.taskWhen)
  : /^\d{2}:\d{2}$/.test(form.taskTime) && (form.taskRule !== 'weekly' || Boolean(form.taskWeekdays))))
function editTask(item: DemoWakeTask) {
  if (editingTask.value === item.id) { editingTask.value = ''; return }
  addingTask.value = false
  editingTask.value = item.id
  Object.assign(form, { taskRule: item.ruleKind, taskWhen: item.when.replace(' ', 'T'), taskTime: item.when.slice(-5), taskWeekdays: item.weekdays, taskText: item.text })
}
function addTask() {
  taskView.value = 'pending'
  if (addingTask.value) return
  const next = new Date(Date.now() + 60 * 60 * 1000), pad = (value: number) => String(value).padStart(2, '0')
  const when = `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}T${pad(next.getHours())}:${pad(next.getMinutes())}`
  Object.assign(form, { taskRule: 'at', taskWhen: when, taskTime: when.slice(-5), taskWeekdays: '', taskText: '' })
  editingTask.value = ''
  addingTask.value = true
}
function saveTask() {
  if (!taskCanSave.value || loading.value) return
  const days = form.taskWeekdays.split(',').filter(Boolean).map(Number)
  const dayNames = ['一', '二', '三', '四', '五', '六', '日']
  const rule = form.taskRule === 'daily' ? `每天 ${form.taskTime}` : form.taskRule === 'weekly' ? `每周${days.map(day => dayNames[day - 1]).join('、')} ${form.taskTime}` : '单次'
  let when = form.taskWhen.replace('T', ' ')
  if (form.taskRule !== 'at') {
    const now = new Date(), next = new Date(now), [hour, minute] = form.taskTime.split(':').map(Number)
    next.setHours(hour, minute, 0, 0)
    for (let offset = 0; offset < 8; offset++) {
      if (next > now && (form.taskRule === 'daily' || days.includes((next.getDay() + 6) % 7 + 1))) break
      next.setDate(next.getDate() + 1)
    }
    const pad = (value: number) => String(value).padStart(2, '0')
    when = `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())} ${form.taskTime}`
  }
  const values = { text: form.taskText.trim(), ruleKind: form.taskRule, weekdays: form.taskWeekdays, rule, when }
  if (addingTask.value) tasks.value.unshift({ id: `wake-${crypto.randomUUID()}`, ...values, status: '待执行', hasReference: false })
  else {
    const task = tasks.value.find(item => item.id === editingTask.value)
    if (task) Object.assign(task, values)
  }
  feedback(addingTask.value ? '定时任务已添加' : '定时任务修改已保存')
  addingTask.value = false
  editingTask.value = ''
}
function deleteTask(item: DemoWakeTask) {
  tasks.value = tasks.value.filter(task => task.id !== item.id)
  feedback('归档任务已删除')
}
function saveKeys() {
  deepseekKey.value = ''; minimaxKey.value = ''
  feedback('接口密钥已保存，重启应用后生效。')
}
function cancelTask(item: DemoWakeTask) {
  item.status = '已取消'
  if (editingTask.value === item.id) editingTask.value = ''
  feedback('定时任务已取消')
}
</script>

<template>
  <article class="settings-panel panel" :class="`settings-panel-${tab}`">
    <template v-if="tab === 'wake'">
      <SettingsPageHeader section="wake" />
      <div class="wake-silence-layout">
      <section class="preference-card wake-preference" aria-labelledby="wake-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">01</span><div><h3 id="wake-preference-title">唤醒词</h3><p>KWS（关键词唤醒） · 选择短语与识别灵敏度</p></div></header>
      <div class="settings-stack">
        <label class="switch-row"><div><strong>启用关键词唤醒</strong><span>关闭后直接进入语音识别</span></div><input v-model="form.wakeEnabled" type="checkbox" role="switch"><i></i></label>
        <div class="form-field"><span>唤醒词</span><div v-for="(_, index) in form.keywords" :key="index" class="wake-keyword-row"><input v-model="form.keywords[index]" type="text" :aria-label="`唤醒词 ${index + 1}`"><button class="outline-button wake-keyword-remove" type="button" :aria-label="`移除第 ${index + 1} 个唤醒词`" :disabled="form.keywords.length < 2" @click="form.keywords.splice(index, 1)">移除</button></div><button class="ghost-action" type="button" :disabled="form.keywords.length >= 5" @click="form.keywords.push('')">添加唤醒短语</button><small>最多五个短语。</small></div>
        <label class="form-field"><span>识别灵敏度 <b>{{ form.sensitivity }}%</b></span><input v-model.number="form.sensitivity" type="range" min="20" max="95"><div class="range-label"><small>更严格 · 少误触发</small><small>更容易唤醒</small></div></label>
      </div><footer><button class="save-button" type="button" :disabled="loading" @click="feedback('唤醒词设置已保存')">{{ loading ? '正在保存…' : '保存唤醒词设置' }}</button></footer>
      </section>
      <section class="preference-card silence-preference" aria-labelledby="silence-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">02</span><div><h3 id="silence-preference-title">静默等待</h3><p>长期无有效输入后的等待时间</p></div></header>
      <SilenceTimeoutControl v-model="form.silenceSeconds" :disabled="loading" />
      <div class="info-callout"><span>i</span><p>SmartTurn（语音结束判断）处理正常语句结束；这里管理长期无有效输入后的最终检查。</p></div>
      </section>
      <section class="preference-card quiet-preference" aria-labelledby="quiet-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">03</span><div><h3 id="quiet-preference-title">每日静默</h3><p>保留主动交互，减少时段内的主动发声</p></div></header>
      <div class="settings-stack"><label class="switch-row"><div><strong>每日被动静默</strong><span>时段内抑制主动发声，保留用户主动交互</span></div><input v-model="form.quietEnabled" type="checkbox" role="switch"><i></i></label><div class="provider-fields"><label><span>开始时间</span><TimePicker v-model="form.quietStart" label="开始时间" :disabled="loading" /></label><label><span>结束时间</span><TimePicker v-model="form.quietEnd" label="结束时间" :disabled="loading" /></label></div></div>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('静默设置已保存')">{{ loading ? '正在保存…' : '保存静默设置' }}</button></footer>
      </section>
      </div>
    </template>

    <template v-else-if="tab === 'persona'">
      <SettingsPageHeader section="persona" />
      <div class="persona-layout">
        <nav class="persona-nav" aria-label="人格文档">
          <button v-for="document in primaryPersonaDocuments" :key="document.id" type="button" :class="{ active: activeDocument === document.id }" :title="`${document.label} · 主 LLM（大语言模型）`" @click="activeDocument = document.id"><strong>{{ document.label }}</strong><small>主 LLM（大语言模型）</small></button>
        </nav>
        <label class="document-editor"><span :title="`${currentDocument.path}（展示文档路径）`">{{ currentDocument.path }}（展示文档路径）</span><textarea v-model="documents[currentDocument.id]" :aria-label="`${currentDocument.label}文档内容`" spellcheck="false"></textarea></label>
      </div>
      <section class="preview-section">
        <div class="section-row"><h3>自定义语音指令</h3><button class="ghost-action" type="button" @click="feedback('添加指令入口')">添加指令</button></div>
        <div v-if="!instructionRows.length" class="pool-empty">还没有自定义指令。</div>
        <InstructionEditorRow v-for="item in instructionRows" :key="item.id" :row="item" :disabled="loading" />
      </section>
      <label class="switch-row context-gate-row"><div><strong>实验性 Shadow 上下文门禁</strong><span>Shadow（影子代理）的侧载模型只判断运行中 DSH（代理执行宿主）的新引导应进入当前步骤还是下一回合；权威上下文仍完整同步。关闭后恢复直接注入。</span></div><input v-model="form.experimentalContextGateEnabled" type="checkbox" role="switch"><i></i></label>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('人格与指令已保存')">{{ loading ? '正在保存…' : '保存人格与指令' }}</button></footer>
    </template>

    <template v-else-if="tab === 'speaker'">
      <SettingsPageHeader section="speaker" />
      <div class="settings-stack"><label class="form-field"><span>运行模式</span><select v-model="form.speakerMode"><option value="off">关闭</option><option value="observe">观察 · 记录判断结果</option><option value="filter">筛选 · 仅处理主人输入</option></select></label><label class="switch-row"><div><strong>仅主人可打断</strong><span>身份未知时保留当前播放</span></div><input v-model="form.ownerOnly" type="checkbox" role="switch"><i></i></label></div>
      <details class="speaker-advanced"><summary>高级设置 · 阈值基线</summary><div class="speaker-thresholds"><label class="form-field"><span>主人确认阈值 <b>{{ form.confirmThreshold.toFixed(2) }}</b></span><input v-model.number="form.confirmThreshold" type="range" min="0.01" max="1" step="0.01"><small>达到阈值判主人，低于阈值及证据不足时判非主人。</small></label><label class="form-field"><span>连续主人放宽量 <b>{{ form.continuityThreshold.toFixed(2) }}</b></span><input v-model.number="form.continuityThreshold" type="range" min="0" max="0.20" step="0.01"><small>向右增大放宽量，降低连续窗口阈值。</small></label></div></details>
      <section class="speaker-enrollment-card" aria-labelledby="speaker-enrollment-title">
        <div class="speaker-card-heading">
          <div class="speaker-card-copy"><h3 id="speaker-enrollment-title">主人声纹模板</h3><p>按日常语速录制三段，每段 5–10 秒，保持设备与距离一致。</p></div>
          <div class="speaker-card-actions"><button class="outline-button speaker-compact-button speaker-recording-toggle" type="button" :disabled="loading" :aria-expanded="recordingPreview" @click="toggleSpeakerRecording">{{ recordingPreview ? '收起录制' : '开始录制' }}</button><button class="outline-button speaker-compact-button" type="button" :disabled="recordingPreview || !templates.length || loading" @click="feedback('现场测试：已确认主人，相似度 0.82')">现场测试</button></div>
        </div>
        <SpeakerRecordingFlow v-if="recordingPreview" :completed="speakerSegments.length" :total="3">
          <div class="speaker-segment-list"><div v-for="(prompt, index) in demoSpeakerPrompts" :key="prompt" class="speaker-segment-row" :class="{ completed: speakerSegments.includes(index), recording: speakerRecording === index }" :data-segment="index">
            <span class="speaker-segment-index" aria-hidden="true">0{{ index + 1 }}</span>
            <div class="speaker-segment-copy"><div class="speaker-segment-heading"><strong>第 {{ index + 1 }} 段</strong><span class="speaker-segment-state" role="status">{{ speakerRecording === index ? '录制中' : speakerSegments.includes(index) ? '已完成' : '待录制' }}</span></div><p class="speaker-prompt">{{ prompt }}</p></div>
            <button class="outline-button speaker-compact-button speaker-segment-action" :class="{ 'speaker-stop-button': speakerRecording === index }" type="button" :disabled="loading || (speakerRecording !== null && speakerRecording !== index)" @click="recordSpeakerSegment(index)">{{ speakerRecording === index ? '停止录制' : speakerSegments.includes(index) ? '重新录制' : '开始录制' }}</button>
          </div></div>
          <template #actions><button class="outline-button speaker-compact-button" type="button" @click="cancelSpeakerRecording">取消录制</button><button class="save-button speaker-compact-button speaker-submit-button" type="button" :disabled="!speakerCanSubmit" @click="saveSpeakerRecording">保存声纹</button></template>
        </SpeakerRecordingFlow>
        <div v-else-if="templates.length" class="speaker-template-list"><article v-for="item in templates" :key="item.id" class="speaker-template-row">
          <div class="speaker-template-copy"><strong :title="props.scene === 'long' ? longIdentifier : item.label">{{ props.scene === 'long' ? longIdentifier : item.label }}</strong><time>{{ item.date }}</time></div><span class="speaker-template-state active">{{ item.status }}</span><div class="speaker-template-actions"><button class="outline-button speaker-compact-button" type="button" @click="feedback('声纹模板中心设置')">移出主中心</button><button class="outline-button danger-action speaker-compact-button" type="button" @click="feedback('声纹模板删除提示')">删除</button></div>
        </article></div><div v-else class="pool-empty">还没有声纹模板。</div>
      </section>
      <section class="speaker-ambient-card"><div class="speaker-card-heading"><div><p class="section-label">AMBIENT CONTEXT（环境上下文）</p><h3>当前会话环境记录</h3></div><button class="ghost-action" type="button" @click="feedback('环境记录刷新提示')">刷新</button></div><div v-if="ambient.length" class="ambient-record-list"><article v-for="item in ambient" :key="item.id"><time>{{ item.time }}</time><p>{{ props.scene === 'long' ? longText : item.text }}</p><small>{{ item.status }}</small></article></div><div v-else class="pool-empty">暂无环境记录。</div></section>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('声纹设置已保存')">{{ loading ? '正在保存…' : '保存声纹设置' }}</button></footer>
    </template>

    <template v-else-if="tab === 'wakes'">
      <SettingsPageHeader section="wakes" />
      <div class="wake-task-toolbar">
        <WakeTaskTabs v-model="taskView" :pending="pendingTasks.length" :archived="archivedTasks.length" />
        <div class="wake-task-toolbar-actions">
          <button class="outline-button wake-task-button" type="button" :aria-expanded="addingTask" :disabled="loading" @click="addTask"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>添加</button>
          <button class="outline-button wake-task-button" type="button" :disabled="loading" @click="feedback('任务列表已刷新')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5M6.1 6.1a8 8 0 0 1 13 3M4.9 14.9a8 8 0 0 0 13 3" /></svg>刷新</button>
        </div>
      </div>
      <section v-if="addingTask" class="model-accordion-card wake-task-card wake-task-create" aria-label="添加定时任务">
        <h3>添加定时任务</h3>
        <div class="wake-task-editor">
          <WakeTaskSchedule v-model:rule-kind="form.taskRule" v-model:when="form.taskWhen" v-model:time="form.taskTime" v-model:weekdays="form.taskWeekdays" :disabled="loading" />
          <label class="form-field wake-task-text-field"><span>任务说明</span><textarea v-model="form.taskText" maxlength="500" rows="2" aria-label="任务说明" placeholder="到时需要提醒或处理什么？" :disabled="loading"></textarea></label>
          <div class="wake-task-editor-actions"><button class="outline-button wake-task-button" type="button" @click="addingTask = false">取消</button><button class="save-button compact-save" type="button" :disabled="loading || !taskCanSave" @click="saveTask">添加任务</button></div>
        </div>
      </section>
      <div v-if="visibleTasks.length" class="wake-task-list">
        <article v-for="item in visibleTasks" :key="item.id" class="model-accordion-card wake-task-card" :data-task="item.id">
          <div class="wake-task-summary">
            <div><div class="pool-entry-title"><strong>{{ props.scene === 'long' ? longText : item.text }}</strong><span class="entry-status">{{ item.status }}</span></div><small>规则：{{ item.rule }} · 下次：{{ item.when }}</small><small>参考上下文：{{ item.hasReference ? '已冻结，可按需读取' : '无' }}</small></div>
            <div class="pool-entry-actions"><button v-if="item.status === '待执行'" class="outline-button wake-task-button" type="button" :aria-expanded="editingTask === item.id" :disabled="loading" @click="editTask(item)">{{ editingTask === item.id ? '收起' : '修改' }}</button><button class="outline-button wake-task-button danger-action" type="button" :disabled="loading" @click="item.status === '待执行' ? cancelTask(item) : deleteTask(item)">{{ item.status === '待执行' ? '取消任务' : '删除' }}</button></div>
          </div>
          <div v-if="editingTask === item.id" class="model-edit-panel wake-task-editor">
            <WakeTaskSchedule v-model:rule-kind="form.taskRule" v-model:when="form.taskWhen" v-model:time="form.taskTime" v-model:weekdays="form.taskWeekdays" :disabled="loading" />
            <label class="form-field wake-task-text-field"><span>任务说明</span><textarea v-model="form.taskText" maxlength="500" rows="2" aria-label="任务说明" :disabled="loading"></textarea></label>
            <div class="wake-task-editor-actions"><button class="outline-button wake-task-button" type="button" @click="editingTask = ''">取消</button><button class="save-button compact-save" type="button" :disabled="loading || !taskCanSave" @click="saveTask">保存修改</button></div>
          </div>
        </article>
      </div>
      <div v-else-if="!addingTask" class="pool-empty">{{ taskView === 'pending' ? '还没有待执行任务。' : '还没有已归档任务。' }}</div>
    </template>

    <template v-else-if="tab === 'models'">
      <SettingsPageHeader section="models" />
      <FixedModelCredentials v-model:deepseek="deepseekKey" v-model:minimax="minimaxKey" :configured="{ deepseek: props.scene !== 'empty', minimax: props.scene !== 'empty' }" :disabled="loading" :saving="loading" @save="saveKeys" />
    </template>

    <template v-else-if="tab === 'compute'">
      <LocalComponents :model="localComponents.model.value" @refresh="localComponents.refresh" @retry="localComponents.retry" @device="localComponents.device" @save="localComponents.save" @resource-download="feedback('模拟资源下载任务已开始')" @resource-import="feedback('模拟资源导入任务已开始')" @resource-import-all="feedback('模拟批量导入任务已开始')" @open-resource-source="feedback('样例不会打开外部来源页面')" @cancel-resource="feedback('模拟资源任务已取消')" />
    </template>

    <template v-else-if="tab === 'workspace'">
      <SettingsPageHeader section="workspace" />
      <section class="preference-card workspace-preview-card" aria-labelledby="workspace-preview-title">
        <header class="preference-heading"><span class="preference-index" aria-hidden="true">01</span><div><h3 id="workspace-preview-title">开发运行工作区</h3><p>开发模式只显示项目路径，不提供迁移操作。</p></div></header>
        <dl class="workspace-preview-path"><div><dt>项目路径</dt><dd>由桌面启动器提供的项目工作区</dd></div></dl>
      </section>
      <section class="preference-card workspace-preview-card" aria-labelledby="workspace-update-preview-title">
        <header class="preference-heading"><span class="preference-index" aria-hidden="true">02</span><div><h3 id="workspace-update-preview-title">应用更新</h3><p>更新检查暂未启用。</p></div></header>
        <button class="outline-button" type="button" disabled>检查更新</button>
      </section>
    </template>
  </article>
</template>
