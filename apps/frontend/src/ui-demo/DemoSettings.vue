<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  demoAmbientRecords, demoCapabilities, demoComputeModels, demoInstructions, demoModelEntries,
  demoSettings, demoSpeakerPrompts, demoSpeakerTemplates, demoSystemComponents, demoWakeTasks,
  longIdentifier, longText, originLabels, previewRows, primaryPersonaDocuments,
  type PreviewScene, type SettingsTab,
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
const recordingPreview = ref(false)
const editingTask = ref('')
const editingModel = ref('')
const selectedDevices = reactive(Object.fromEntries(demoComputeModels.map(item => [item.id, item.device])))
const models = computed(() => previewRows(demoModelEntries, props.scene))
const tasks = computed(() => previewRows(demoWakeTasks, props.scene))
const templates = computed(() => previewRows(demoSpeakerTemplates, props.scene))
const ambient = computed(() => previewRows(demoAmbientRecords, props.scene))
const components = computed(() => previewRows(demoSystemComponents, props.scene))
const instructionRows = computed(() => props.scene === 'empty' ? [] : instructions)
const loading = computed(() => props.scene === 'loading')
function feedback(label: string) { emit('feedback', label) }
</script>

<template>
  <article class="settings-panel panel" :class="`settings-panel-${tab}`">
    <template v-if="tab === 'wake'">
      <header><p class="panel-kicker">唤醒与静默</p><h2>让助手在需要时醒来</h2><p>在一个页面管理唤醒短语、静默等待和每日静默时段。</p></header>
      <div class="wake-silence-layout">
      <section class="preference-card wake-preference" aria-labelledby="wake-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">01</span><div><h3 id="wake-preference-title">唤醒词</h3><p>KWS（关键词唤醒） · 选择短语与识别灵敏度</p></div></header>
      <div class="settings-stack">
        <label class="switch-row"><div><strong>启用关键词唤醒</strong><span>关闭后直接进入语音识别</span></div><input v-model="form.wakeEnabled" type="checkbox" role="switch"><i></i></label>
        <div class="form-field"><span>唤醒词</span><div v-for="(_, index) in form.keywords" :key="index" class="wake-keyword-row"><input v-model="form.keywords[index]" type="text" :aria-label="`唤醒词 ${index + 1}`"><button class="ghost-action" type="button" :disabled="form.keywords.length < 2" @click="form.keywords.splice(index, 1)">移除</button></div><button class="ghost-action" type="button" :disabled="form.keywords.length >= 5" @click="form.keywords.push('')">添加唤醒短语</button><small>最多五个短语。</small></div>
        <label class="form-field"><span>识别灵敏度 <b>{{ form.sensitivity }}%</b></span><input v-model.number="form.sensitivity" type="range" min="20" max="95"><div class="range-label"><small>更严格 · 少误触发</small><small>更容易唤醒</small></div></label>
      </div><footer><button class="save-button" type="button" :disabled="loading" @click="feedback('唤醒词设置已保存')">{{ loading ? '正在保存…' : '保存唤醒词设置' }}</button></footer>
      </section>
      <section class="preference-card silence-preference" aria-labelledby="silence-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">02</span><div><h3 id="silence-preference-title">静默等待</h3><p>长期无有效输入后的等待时间</p></div></header>
      <div class="silence-control"><span>最终静默等待</span><div><input v-model.number="form.silenceSeconds" min="1" max="300" type="number" aria-label="最终静默等待秒数"><b>秒</b></div><input v-model.number="form.silenceSeconds" min="5" max="300" type="range" aria-label="调整静默等待"><div class="silence-scale"><small>快速回待机</small><small>默认 90 秒</small><small>长时间等待</small></div></div>
      <div class="info-callout"><span>i</span><p>SmartTurn（语音结束判断）处理正常语句结束；这里管理长期无有效输入后的最终检查。</p></div>
      </section>
      <section class="preference-card quiet-preference" aria-labelledby="quiet-preference-title">
      <header class="preference-heading"><span class="preference-index" aria-hidden="true">03</span><div><h3 id="quiet-preference-title">每日静默</h3><p>保留主动交互，减少时段内的主动发声</p></div></header>
      <div class="settings-stack"><label class="switch-row"><div><strong>每日被动静默</strong><span>时段内抑制主动发声，保留用户主动交互</span></div><input v-model="form.quietEnabled" type="checkbox" role="switch"><i></i></label><div class="provider-fields"><label><span>开始时间</span><input v-model="form.quietStart" type="time"></label><label><span>结束时间</span><input v-model="form.quietEnd" type="time"></label></div></div>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('静默设置已保存')">{{ loading ? '正在保存…' : '保存静默设置' }}</button></footer>
      </section>
      </div>
    </template>

    <template v-else-if="tab === 'persona'">
      <header><p class="panel-kicker">人格与指令</p><h2>定义助手如何与你协作</h2><p>人格风格与用户资料在下一次新会话中生效。</p></header>
      <div class="persona-layout"><nav class="persona-nav" aria-label="人格文档"><button v-for="document in primaryPersonaDocuments" :key="document.id" type="button" :class="{ active: activeDocument === document.id }" @click="activeDocument = document.id"><strong>{{ document.label }}</strong><small>主 LLM（大语言模型）</small></button></nav><label class="document-editor"><span>{{ currentDocument.path }}（展示文档路径）</span><textarea v-model="documents[currentDocument.id]" spellcheck="false"></textarea></label></div>
      <section class="preview-section"><div class="section-row"><h3>自定义语音指令</h3><button class="ghost-action" type="button" @click="feedback('添加指令入口')">添加指令</button></div><div v-if="!instructionRows.length" class="pool-empty">还没有自定义指令。</div><div v-for="item in instructionRows" :key="item.id" class="instruction-row"><label class="form-field"><span>触发语义</span><input v-model="item.trigger" type="text"></label><label class="form-field"><span>执行位置</span><select v-model="item.target"><option value="voice">主语音助手</option><option value="shadow">Shadow（影子代理）</option></select></label><label class="form-field"><span>执行目标</span><textarea v-model="item.instruction" rows="2"></textarea></label><label class="switch-row instruction-toggle"><div><strong>启用</strong></div><input v-model="item.enabled" type="checkbox" role="switch" :aria-label="`启用指令：${item.trigger}`"><i aria-hidden="true"></i></label></div></section>
      <label class="switch-row context-gate-row"><div><strong>实验性 Shadow 上下文门禁</strong><span>Shadow（影子代理）的侧载模型只判断运行中 DSH（代理执行宿主）的新引导应进入当前步骤还是下一回合；权威上下文仍完整同步。关闭后恢复直接注入。</span></div><input v-model="form.experimentalContextGateEnabled" type="checkbox" role="switch"><i></i></label>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('人格与指令已保存')">{{ loading ? '正在保存…' : '保存人格与指令' }}</button></footer>
    </template>

    <template v-else-if="tab === 'speaker'">
      <header><p class="panel-kicker">声纹设置</p><h2>识别是谁在与助手说话</h2><p>查看主人模板、现场测试和当前会话的环境记录。</p></header>
      <div class="settings-stack"><label class="form-field"><span>运行模式</span><select v-model="form.speakerMode"><option value="off">关闭</option><option value="observe">观察 · 记录判断结果</option><option value="filter">筛选 · 仅处理主人输入</option></select></label><label class="switch-row"><div><strong>仅主人可打断</strong><span>身份未知时保留当前播放</span></div><input v-model="form.ownerOnly" type="checkbox" role="switch"><i></i></label></div>
      <details class="speaker-advanced"><summary>高级设置 · 阈值基线</summary><div class="speaker-thresholds"><label class="form-field"><span>主人确认阈值 {{ form.confirmThreshold.toFixed(2) }}</span><input v-model.number="form.confirmThreshold" type="range" min="0.01" max="1" step="0.01"></label><label class="form-field"><span>非主人确认界限 {{ form.continuityThreshold.toFixed(2) }}</span><input v-model.number="form.continuityThreshold" type="range" min="0" max="0.99" step="0.01"></label></div></details>
      <section class="speaker-enrollment-card"><div class="speaker-card-heading"><div><p class="section-label">VOICE PROFILE（声纹资料）</p><h3>主人声纹模板</h3><p>使用日常说话方式，分别朗读三段不同的句子。</p></div><div><button class="outline-button" type="button" @click="recordingPreview = !recordingPreview">{{ recordingPreview ? '收起录制画面' : '开始录制' }}</button><button class="ghost-action" type="button" @click="feedback('现场测试：已确认主人，相似度 0.82')">现场测试</button></div></div>
        <div v-if="recordingPreview" class="speaker-recording-flow"><div class="speaker-segment-list"><div v-for="(prompt, index) in demoSpeakerPrompts" :key="prompt" class="speaker-segment-row"><span class="speaker-segment-index">0{{ index + 1 }}</span><div><strong>第 {{ index + 1 }} 段</strong><small class="speaker-prompt">「{{ prompt }}」</small><small>{{ index === 0 ? '已接收' : '尚未录制' }}</small></div><button class="outline-button" type="button" @click="feedback('录制片段画面')">{{ index === 0 ? '重新录制' : '开始录制' }}</button></div></div><footer class="speaker-flow-footer"><span>1/3 段已完成</span><button class="save-button" type="button" @click="feedback('声纹资料提交提示')">提交声纹资料</button></footer></div>
        <div v-else-if="templates.length" class="speaker-template-list"><article v-for="item in templates" :key="item.id"><div><strong>{{ props.scene === 'long' ? longIdentifier : item.label }}</strong><small>{{ item.date }}</small></div><span>{{ item.status }}</span><button class="ghost-action" type="button" @click="feedback('声纹模板中心设置')">移出主中心</button><button class="danger-action" type="button" @click="feedback('声纹模板删除提示')">删除</button></article></div><div v-else class="pool-empty">还没有声纹模板。</div>
      </section>
      <section class="speaker-ambient-card"><div class="speaker-card-heading"><div><p class="section-label">AMBIENT CONTEXT（环境上下文）</p><h3>当前会话环境记录</h3></div><button class="ghost-action" type="button" @click="feedback('环境记录刷新提示')">刷新</button></div><div v-if="ambient.length" class="ambient-record-list"><article v-for="item in ambient" :key="item.id"><time>{{ item.time }}</time><p>{{ props.scene === 'long' ? longText : item.text }}</p><small>{{ item.status }}</small></article></div><div v-else class="pool-empty">暂无环境记录。</div></section>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('声纹设置已保存')">{{ loading ? '正在保存…' : '保存声纹设置' }}</button></footer>
    </template>

    <template v-else-if="tab === 'wakes'">
      <header><p class="panel-kicker">项目级唤醒</p><h2>定时唤醒任务</h2><p>查看待执行任务、重复规则和历史归档。</p></header>
      <div class="speaker-card-heading"><span>待执行 {{ tasks.filter(item => item.status === '待执行').length }} 项</span><button class="ghost-action" type="button" @click="feedback('任务刷新提示')">刷新</button></div>
      <div v-if="tasks.length" class="wake-task-list"><article v-for="item in tasks" :key="item.id" class="model-accordion-card wake-task-card"><div class="wake-task-summary"><div><div class="pool-entry-title"><strong>{{ props.scene === 'long' ? longText : item.text }}</strong><span class="entry-status">{{ item.status }}</span></div><small>规则：{{ item.rule }} · 下次：{{ item.when }}</small><small>参考上下文：已冻结，可按需读取</small></div><div class="pool-entry-actions"><button type="button" @click="editingTask = editingTask === item.id ? '' : item.id">{{ editingTask === item.id ? '收起' : '修改' }}</button><button class="danger-action" type="button" @click="feedback('任务取消提示')">取消任务</button></div></div><div v-if="editingTask === item.id" class="model-edit-panel"><div class="provider-fields"><label><span>触发时间</span><input v-model="form.taskWhen" type="datetime-local"></label><label><span>重复规则</span><select v-model="form.taskRule"><option>每天</option><option>单次</option><option>每周</option></select></label><label class="wake-task-text-field"><span>任务说明</span><textarea v-model="form.taskText" rows="3"></textarea></label></div><button class="save-button" type="button" @click="feedback('定时任务修改已保存')">保存修改</button></div></article></div><div v-else class="pool-empty">还没有定时唤醒任务。</div>
    </template>

    <template v-else-if="tab === 'models'">
      <header class="model-header"><div><p class="panel-kicker">模型管理</p><h2>连接、分配与模型池</h2><p>分别管理服务商连接、运行角色和模型条目。</p></div><button class="outline-button" type="button" :disabled="loading" @click="feedback('模型扫描提示')">{{ loading ? '正在扫描…' : '扫描现有配置' }}</button></header>
      <div class="model-accordion-list">
        <details class="model-accordion-card" open><summary class="preview-accordion-summary"><span class="accordion-index">01</span><strong>添加模型</strong><span>服务商连接</span></summary><div class="model-card-content"><div class="provider-fields"><label><span>连接名称</span><input v-model="form.providerName" type="text"></label><label><span>服务地址</span><input v-model="form.providerUrl" type="url"></label><label><span>协议</span><select v-model="form.providerProtocol"><option>DeepSeek</option><option>OpenAI 兼容（通用模型接口）</option><option>Responses（响应接口）</option></select></label><label><span>API Key（接口密钥）</span><input v-model="form.providerKey" type="password" autocomplete="off" placeholder="展示字段，请勿填写真实密钥"></label></div><div class="provider-actions"><button class="outline-button" type="button" @click="feedback('连接验证成功提示')">发现并验证连接</button><button class="save-button" type="button" @click="feedback('加入模型池提示')">加入模型池</button></div></div></details>
        <details class="model-accordion-card" open><summary class="preview-accordion-summary"><span class="accordion-index">02</span><strong>模型配置</strong><span>运行角色</span></summary><div class="model-card-content"><div class="preview-assignment-grid"><label class="form-field"><span>语音交互主模型</span><select v-model="form.voiceModel"><option v-for="item in demoModelEntries.filter(item => item.capability === 'llm')" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label class="form-field"><span>Shadow（影子代理）模型</span><select v-model="form.shadowModel"><option v-for="item in demoModelEntries.filter(item => item.capability === 'llm')" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label class="form-field"><span>状态汇报模型</span><select v-model="form.statusModel"><option v-for="item in demoModelEntries.filter(item => item.capability === 'llm')" :key="item.id" :value="item.id">{{ item.name }}</option></select></label></div><div class="info-callout"><span>i</span><p>TTS（语音合成）使用固定在线服务；影子代理采用高强度思考。</p></div></div></details>
        <details class="model-accordion-card" open><summary class="preview-accordion-summary"><span class="accordion-index">03</span><strong>模型池</strong><span>{{ models.length }} 个条目</span></summary><div class="model-card-content"><div v-if="models.length" class="pool-entry-list"><article v-for="item in models" :key="item.id" class="pool-entry-card"><div class="pool-entry-topline"><span class="provider-badge managed">{{ item.capability === 'llm' ? '语言' : item.capability === 'tts' ? '语音' : '本地' }}</span><div class="pool-entry-heading"><div class="pool-entry-title"><strong>{{ props.scene === 'long' ? longIdentifier : item.name }}</strong><span class="entry-status">{{ item.status === 'healthy' ? '已就绪' : '可用' }}</span></div><span class="pool-entry-id">{{ item.modelId }}（模型标识） · {{ originLabels[item.origin] }}</span></div></div><div class="pool-entry-policy"><span><small>服务商</small><strong>{{ item.provider }}</strong></span><span><small>用途</small><strong>{{ item.capability === 'llm' ? 'LLM（大语言模型）' : item.capability === 'tts' ? 'TTS（语音合成）' : '本地语音处理' }}</strong></span><span><small>上下文容量</small><strong>{{ item.maxContextTokens ? `${item.maxContextTokens / 1024}K` : '不适用' }}</strong></span></div><div class="pool-entry-actions"><button type="button" @click="editingModel = editingModel === item.id ? '' : item.id">{{ editingModel === item.id ? '收起' : '编辑' }}</button><button class="danger-action" type="button" @click="feedback('模型删除提示')">删除</button></div><div v-if="editingModel === item.id" class="model-edit-panel"><label class="form-field"><span>显示名称</span><input :value="item.name" type="text"></label><label class="form-field"><span>模型标识</span><input :value="item.modelId" type="text"></label><button class="save-button" type="button" @click="feedback('模型条目已保存')">保存修改</button></div></article></div><div v-else class="pool-empty">模型池中暂无条目。</div></div></details>
      </div><footer class="model-settings-footer"><div class="restart-copy">应用配置会受控重建相关模型服务。</div><button class="save-button" type="button" :disabled="loading" @click="emit('confirm', 'apply')">应用模型配置</button></footer>
    </template>

    <template v-else-if="tab === 'compute'">
      <header><p class="panel-kicker">计算设备</p><h2>选择模型使用的计算设备</h2><p>设备偏好保存后，需要完全重启应用才会生效。</p></header>
      <div class="preview-stat-grid"><article><span>系统内存</span><strong>12.4 / 32 GB（吉字节）可用</strong></article><article><span>GPU（图形处理器）</span><strong>NVIDIA · 5.8 / 8 GB 可用</strong></article></div>
      <div v-if="props.scene !== 'empty'" class="preview-compute-list"><article v-for="item in demoComputeModels" :key="item.id" class="model-accordion-card"><div><strong>{{ item.name }}</strong><small>当前设备：{{ item.device === 'cuda' ? 'CUDA（显卡计算）' : 'CPU（中央处理器）' }} · 常驻内存 {{ item.memory }}</small><p v-if="props.scene === 'long'">{{ longIdentifier }}</p></div><label class="form-field"><span>设备偏好</span><select v-model="selectedDevices[item.id]"><option value="auto">自动选择</option><option value="cpu">CPU（中央处理器）</option><option value="cuda" :disabled="!item.gpu">CUDA（显卡计算）{{ item.gpu ? '' : ' · 不可用' }}</option></select></label></article></div><div v-else class="pool-empty">暂无模型设备信息。</div>
      <footer><button class="save-button" type="button" :disabled="loading" @click="feedback('设备偏好已保存，需要完全重启应用')">{{ loading ? '正在保存…' : '保存设备偏好' }}</button></footer>
    </template>

    <template v-else-if="tab === 'system'">
      <section class="application-system"><div class="application-heading"><div><h2>系统运行</h2><p>以下运行状态为固定样例，关闭样例窗口会退出预览。</p></div><button type="button" @click="feedback('系统状态刷新提示')">刷新状态</button></div><div class="application-summary"><strong>{{ props.scene === 'error' ? '降级运行' : loading ? '启动中' : '运行正常' }}</strong><span>{{ loading ? '正在加载模型' : '核心能力已就绪' }}</span><span>实时更新</span></div>
        <div class="application-memory-summary"><div><span>当前常驻内存（工作集之和）</span><strong>{{ props.scene === 'empty' ? '不可用' : '2.01 GiB（二进制吉字节）' }}</strong><small>包含共享页，不等于独占物理内存。</small></div><div><span>私有提交内存之和</span><strong>{{ props.scene === 'empty' ? '不可用' : '2.45 GiB（二进制吉字节）' }}</strong><small>已承诺的私有内存，不等于当前物理占用。</small></div></div><small>最近指标采样：2026-10-03 14:20:00 · 香港时间</small>
        <div v-if="components.length" class="application-component-list"><article v-for="item in components" :key="item.id" class="application-component" :class="{ failed: props.scene === 'error' && item.id.startsWith('analysis') }"><div class="application-component-title"><strong>{{ item.name }}</strong><span>{{ props.scene === 'error' && item.id.startsWith('analysis') ? '启动错误' : loading ? '启动中' : '运行中' }}</span></div><p>{{ item.phase }}</p><p v-if="props.scene === 'error' && item.id.startsWith('analysis')" class="application-error">模型加载失败，请检查模型目录后重试。</p><p v-if="props.scene === 'long'">{{ longIdentifier }}</p><div class="application-metrics"><span>PID（进程标识）<b>{{ item.pid }}</b></span><span>运行时长<b>2 小时 16 分</b></span><span>常驻内存<b>{{ item.memory }}（二进制兆字节）</b></span><span>私有提交内存<b>{{ item.commit }}（二进制兆字节）</b></span><span>CPU（中央处理器）<b>{{ item.cpu }}</b></span></div><button type="button" @click="feedback('组件重试提示')">重试组件</button></article></div><div v-else class="pool-empty">暂无受管组件信息。</div>
        <div class="application-capabilities"><h3>业务能力</h3><div v-for="capability in demoCapabilities" :key="capability"><span>{{ capability }}</span><strong>{{ props.scene === 'empty' ? '未知' : loading ? '启动中' : '已就绪' }}</strong></div></div><button class="application-exit" type="button" @click="emit('confirm', 'exit')">退出整个应用</button>
      </section>
    </template>
  </article>
</template>
