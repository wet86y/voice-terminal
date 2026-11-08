<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { productApi, type AnifLiveImportJob, type AnifLiveImportScan } from '../api'
import { desktopPort, type DesktopPort, type DesktopWorkspacePort } from '../desktop-port'
import { useApplicationHost } from '../application-host'

const props = withDefaults(defineProps<{ demo?: boolean; open?: boolean }>(), { demo: false })
const emit = defineEmits<{
  'update:open': [value: boolean]
  completed: [value: {
    model_id: string
    revision?: string | null
    task_id: string
    scan_id?: string
    reference_candidates?: AnifLiveImportJob['reference_candidates']
  }]
}>()
const desktop = desktopPort() as DesktopPort & DesktopWorkspacePort
const application = useApplicationHost()
const localOpen = ref(false)
const open = computed({
  get: () => props.open ?? localOpen.value,
  set: (value: boolean) => {
    if (props.open === undefined) localOpen.value = value
    emit('update:open', value)
  },
})
const sourceType = ref<'local' | 'huggingface' | 'modelscope'>('local')
const sourceValue = ref('')
const scanning = ref(false)
const submitting = ref(false)
const scan = ref<AnifLiveImportScan | null>(null)
const scanError = ref('')
const scanMessage = ref('')
const selectedGpt = ref('')
const selectedSovits = ref('')
const selectedAudio = ref('')
const selectedReferences = ref<string[]>([]) // selectedReferences（选中并登记的参考音频）。
const selectedTranscript = ref('')
const modelId = ref('')
const modelName = ref('')
const referenceText = ref('')
const referenceLanguage = ref<'zh' | 'yue' | 'en' | 'ja' | 'ko'>('zh') // referenceLanguage（参考录音语言）。
const jobs = ref<AnifLiveImportJob[]>([])
const referenceDrafts = ref<Record<string, string>>({})
const referenceFiles = ref<Record<string, File | undefined>>({})
const referenceAssets = ref<Record<string, string | undefined>>({})
const transcribingTasks = ref<string[]>([])
const workingTasks = new Set<string>()
let disposed = false

const weightFiles = computed(() => scan.value?.candidates.weights ?? [])
const audioFiles = computed(() => scan.value?.candidates.audio ?? [])
const transcriptFiles = computed(() => scan.value?.candidates.transcripts ?? [])
const canSubmit = computed(() => Boolean(scan.value && selectedGpt.value && selectedSovits.value &&
  selectedGpt.value !== selectedSovits.value && modelId.value.trim() && modelName.value.trim() && !submitting.value))
const sourceLabel = computed(() => sourceType.value === 'local' ? '本地文件夹' : sourceType.value === 'huggingface' ? 'Hugging Face（模型托管平台）' : 'ModelScope（魔搭模型托管平台）')

onMounted(() => { void refreshPending() })
onBeforeUnmount(() => { disposed = true })

function updateJob(job: AnifLiveImportJob) {
  const index = jobs.value.findIndex(row => row.task_id === job.task_id)
  if (index < 0) jobs.value.unshift(job)
  else jobs.value.splice(index, 1, job)
  if (job.reference_text_draft && !referenceDrafts.value[job.task_id]) {
    referenceDrafts.value[job.task_id] = job.reference_text_draft
  }
}

async function refreshPending() {
  if (props.demo) return
  try {
    // 业务服务可能因显卡构建暂停；原生快照仍可读取并恢复进度。
    if (application.recovery?.value?.suspended && desktop.resourceJobSnapshots) {
      for (const row of await desktop.resourceJobSnapshots()) {
        if (row.job_id.startsWith('aniflive-import-') && ['queued', 'running', 'waiting', 'cancelling'].includes(row.state)) {
          const taskId = row.job_id.slice('aniflive-import-'.length)
          updateJob({ task_id: taskId, status: 'converting', phase: row.phase })
          void watchTask(taskId, true)
        }
      }
      return
    }
    const response = await productApi.pendingAnifLiveImports()
    jobs.value = response.jobs
    for (const job of response.jobs) {
      if (job.reference_text_draft) referenceDrafts.value[job.task_id] = job.reference_text_draft
      if (['queued', 'running', 'converting', 'staging', 'ready_to_convert', 'registering_references', 'reference_failed'].includes(job.status)) void watchTask(job.task_id, job.status !== 'staging' && job.status !== 'ready_to_convert')
    }
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '无法读取待处理的模型导入任务。'
  }
}

async function chooseFolder() {
  scanError.value = ''
  try {
    const folder = await desktop.chooseAnifLiveImportFolder()
    if (folder) sourceValue.value = folder
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '无法打开文件夹选择器。'
  }
}

function resetScan() {
  scan.value = null
  selectedGpt.value = ''; selectedSovits.value = ''; selectedAudio.value = ''; selectedTranscript.value = ''
  selectedReferences.value = []
  referenceText.value = ''; scanError.value = ''; scanMessage.value = ''
}

async function scanSource() {
  if (props.demo) { scanError.value = '演示界面未连接模型导入接口。'; return }
  if (!sourceValue.value.trim()) { scanError.value = sourceType.value === 'local' ? '请选择模型文件夹。' : '请输入公开模型仓库页面地址。'; return }
  scanning.value = true; scanError.value = ''; scanMessage.value = ''; resetScan()
  try {
    const result = await productApi.scanAnifLiveImport(sourceType.value, sourceValue.value.trim())
    scan.value = result
    const gpt = result.candidates.weights.find(row => row.role === 'gpt')
    const sovits = result.candidates.weights.find(row => row.role === 'sovits')
    selectedGpt.value = gpt?.file_id ?? ''
    selectedSovits.value = sovits?.file_id ?? ''
    selectedAudio.value = result.candidates.audio[0]?.file_id ?? ''
    selectedReferences.value = result.candidates.audio.map(item => item.file_id)
    // 转写只使用可唯一配对的资料，不能把列表中的第一份旁注猜成基础录音转写。
    selectedTranscript.value = ''
    const lastPart = result.source.replace(/\\/g, '/').split('/').filter(Boolean).at(-1) || '导入模型'
    modelName.value = lastPart.replace(/[-_]+/g, ' ')
    modelId.value = `import-${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
    scanMessage.value = result.revision ? `仓库版本已固定：${result.revision}` : `已发现 ${result.files.length} 个文件。`
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '模型来源扫描失败。'
  } finally { scanning.value = false }
}

async function createImport() {
  if (!scan.value || !canSubmit.value) return
  submitting.value = true; scanError.value = ''
  try {
    const useTranscriptFile = !referenceText.value.trim() ? selectedTranscript.value || undefined : undefined
    const task = await productApi.startAnifLiveImport({
      scan_id: scan.value.scan_id, selected_gpt: selectedGpt.value, selected_sovits: selectedSovits.value,
      model_id: modelId.value.trim(), name: modelName.value.trim(),
      ...(selectedAudio.value ? { reference_audio: selectedAudio.value } : {}),
      selected_reference_files: selectedReferences.value,
      ...(useTranscriptFile ? { reference_text_file: useTranscriptFile } : {}),
      reference_text: referenceText.value.trim(), reference_language: referenceLanguage.value,
    })
    updateJob(task)
    scanMessage.value = '已确认导入。正在复制所选文件；网页仓库文件现在才会开始下载。'
    void watchTask(task.task_id, false)
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '模型包导入任务创建失败。'
  } finally { submitting.value = false }
}

function wait(milliseconds: number) { return new Promise(resolve => window.setTimeout(resolve, milliseconds)) }

async function watchTask(taskId: string, conversionAlreadyStarted: boolean) {
  if (workingTasks.has(taskId) || props.demo) return
  workingTasks.add(taskId)
  let started = conversionAlreadyStarted
  try {
    while (!disposed) {
      // 转换运行于桌面进程，不能依赖被转换任务主动停止的业务接口。
      const native = conversionAlreadyStarted || started
        ? (await desktop.resourceJobSnapshots?.())?.find(row => row.job_id === `aniflive-import-${taskId}`) : undefined
      if (native && ((['queued', 'running', 'waiting', 'cancelling'].includes(native.state) && native.phase !== '正在登记音色参考')
        || application.recovery?.value?.suspended)) {
        const previous = jobs.value.find(row => row.task_id === taskId)
        updateJob({ ...previous, task_id: taskId, status: 'converting', phase: native.phase,
          progress: native.total_bytes ? Math.min(100, Math.round((native.completed_bytes ?? 0) * 100 / native.total_bytes)) : undefined,
          error: native.error })
        await wait(1100)
        continue
      }
      if (application.recovery?.value?.suspended) { await wait(1100); continue }
      const job = await productApi.getAnifLiveImport(taskId)
      updateJob(job)
      if (job.status === 'ready_to_convert' && !started) {
        const nativeTask = await desktop.startAnifLiveImport(taskId)
        started = true
        scanMessage.value = `转换任务 ${nativeTask.job_id} 已交给桌面资源管理器。`
        continue
      }
      if (['queued', 'running', 'converting', 'registering_references'].includes(job.status)) started = true
      if (['complete', 'failed', 'cancelled', 'pending_reference', 'review_reference', 'reference_failed'].includes(job.status)) {
        if (job.status === 'complete') {
          const counts = job.reference_registration
          scanMessage.value = `模型“${job.name || job.model_id || ''}”已登记。${counts ? `参考资料已就绪 ${counts.ready} 项，待完善 ${counts.pending} 项。` : ''}`
          open.value = false
          if (job.model_id) emit('completed', {
            model_id: job.model_id,
            ...(job.revision !== undefined ? { revision: job.revision } : {}),
            task_id: job.task_id,
            ...(job.scan_id ? { scan_id: job.scan_id } : {}),
            ...(job.reference_candidates ? { reference_candidates: job.reference_candidates } : {}),
          })
        }
        return
      }
      await wait(1100)
    }
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '读取模型导入进度失败。'
  } finally { workingTasks.delete(taskId) }
}

function setReferenceFile(taskId: string, event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (file) { referenceFiles.value[taskId] = file; referenceAssets.value[taskId] = undefined }
}

async function transcribeReference(job: AnifLiveImportJob) {
  const file = referenceFiles.value[job.task_id]
  if (!file || transcribingTasks.value.includes(job.task_id)) return
  scanError.value = ''
  transcribingTasks.value.push(job.task_id)
  try {
    const assetId = (await productApi.uploadTtsAsset('aniflive', file)).asset.asset_id
    referenceAssets.value[job.task_id] = assetId
    let operation = await productApi.startTtsTranscribe('aniflive', assetId)
    while (operation.state === 'running') {
      await wait(400)
      operation = await productApi.getTtsOperation(operation.operation_id)
    }
    if (operation.state !== 'succeeded' || !operation.result?.text) {
      throw new Error(operation.error || '参考录音转写没有返回文本，请手动填写。')
    }
    referenceDrafts.value[job.task_id] = operation.result.text
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '参考录音转写失败，请手动填写。'
  } finally {
    transcribingTasks.value = transcribingTasks.value.filter(id => id !== job.task_id)
  }
}

async function saveReference(job: AnifLiveImportJob) {
  const text = (referenceDrafts.value[job.task_id] || '').trim()
  if (!text) { scanError.value = '请填写参考录音的准确转写。'; return }
  submitting.value = true; scanError.value = ''
  try {
    let assetId = referenceAssets.value[job.task_id]
    const file = referenceFiles.value[job.task_id]
    if (!assetId && file) {
      const imported = await productApi.uploadTtsAsset('aniflive', file)
      assetId = imported.asset.asset_id
      referenceAssets.value[job.task_id] = assetId
    }
    const updated = await productApi.completeAnifLiveImportReference(job.task_id, {
      ...(assetId ? { reference_asset_id: assetId } : {}), reference_text: text, reference_language: referenceLanguage.value,
    })
    updateJob(updated)
    void watchTask(job.task_id, false)
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '保存参考资料失败。'
  } finally { submitting.value = false }
}

async function retryConversion(job: AnifLiveImportJob) {
  submitting.value = true; scanError.value = ''
  try {
    await desktop.startAnifLiveImport(job.task_id)
    void watchTask(job.task_id, true)
  } catch (failure) {
    scanError.value = failure instanceof Error ? failure.message : '无法重试模型转换。'
  } finally { submitting.value = false }
}

function sizeLabel(size?: number | null) {
  if (typeof size !== 'number') return '大小未知'
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(2)} GiB（吉二进制字节）`
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MiB（二进制兆字节）`
  return `${Math.max(1, Math.ceil(size / 1024))} KiB（二进制千字节）`
}
</script>

<template>
  <section v-if="open" class="aniflive-import-card">
    <div class="import-heading">
      <div><h3>导入 AnifLive 模型包</h3><p>递归扫描本地模型文件夹，或扫描公开 Hugging Face、ModelScope 仓库。</p></div>
      <button class="outline-button" type="button" :disabled="scanning || submitting" @click="open = false">关闭导入</button>
    </div>
    <p class="instruction-hint">仅导入 GPT-SoVITS V2ProPlus（语音模型版本）权重配对；路径和文件名只用于识别候选，最终兼容性由现有安全检查器及转换验证确认。没有参考录音时可先保存为待补参考模型。</p>
    <p v-if="scanError" class="instruction-error" role="alert">{{ scanError }}</p>
    <p v-if="scanMessage" class="instruction-hint" role="status">{{ scanMessage }}</p>

    <section v-for="job in jobs" :key="job.task_id" class="import-job">
      <div class="import-job-heading"><strong>{{ job.name || job.model_id || '模型导入任务' }}</strong><span>{{ job.phase }}</span></div>
      <progress v-if="['staging', 'queued', 'running', 'converting', 'registering_references'].includes(job.status)" :value="job.progress" max="100" :aria-label="`${job.name || '模型导入'}进度`" />
      <p v-if="job.progress !== undefined && ['staging', 'converting'].includes(job.status)" class="instruction-hint">当前阶段 {{ job.progress }}%</p>
      <p v-if="job.error" class="instruction-error">{{ job.error }}</p>
      <div v-if="['pending_reference', 'review_reference'].includes(job.status)" class="reference-repair">
        <label v-if="!job.reference_audio_staged" class="form-field"><span>参考录音（3–10 秒）</span><input type="file" accept="audio/wav,audio/mpeg,audio/flac,audio/ogg,.wav,.mp3,.flac,.ogg" @change="setReferenceFile(job.task_id, $event)"></label>
        <button v-if="!job.reference_audio_staged && referenceFiles[job.task_id]" class="outline-button" type="button" :disabled="transcribingTasks.includes(job.task_id)" @click="transcribeReference(job)">{{ transcribingTasks.includes(job.task_id) ? '正在识别转写…' : '按现有音色库流程识别转写' }}</button>
        <label class="form-field"><span>{{ job.status === 'review_reference' ? '核对并校正参考转写' : '参考录音准确转写' }}</span><textarea v-model="referenceDrafts[job.task_id]" maxlength="2000" rows="3" placeholder="请输入录音中实际说出的内容" /></label>
        <label class="form-field"><span>参考语言</span><select v-model="referenceLanguage"><option value="zh">中文（普通话）</option><option value="yue">粤语</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></select></label>
        <button class="outline-button" type="button" :disabled="submitting || (!job.reference_audio_staged && !referenceAssets[job.task_id] && !referenceFiles[job.task_id])" @click="saveReference(job)">保存参考并继续转换</button>
      </div>
      <button v-if="job.status === 'ready_to_convert'" class="outline-button" type="button" :disabled="submitting" @click="retryConversion(job)">开始转换</button>
      <button v-if="['failed', 'cancelled', 'reference_failed'].includes(job.status) && job.stage_ready" class="outline-button" type="button" :disabled="submitting" @click="retryConversion(job)">{{ job.status === 'reference_failed' ? '重试参考登记' : '继续导入' }}</button>
      <p v-if="job.status === 'failed' && !job.stage_ready" class="instruction-hint">本次暂存未完成；请重新扫描来源并创建新的导入任务，来源文件夹不会被修改。</p>
    </section>

    <div class="import-panel">
      <div class="import-source-tabs">
        <button type="button" :disabled="scanning || submitting" :class="{ active: sourceType === 'local' }" @click="sourceType = 'local'; sourceValue = ''; resetScan()">本地文件夹</button>
        <button type="button" :disabled="scanning || submitting" :class="{ active: sourceType === 'huggingface' }" @click="sourceType = 'huggingface'; sourceValue = ''; resetScan()">Hugging Face</button>
        <button type="button" :disabled="scanning || submitting" :class="{ active: sourceType === 'modelscope' }" @click="sourceType = 'modelscope'; sourceValue = ''; resetScan()">ModelScope</button>
      </div>
      <div v-if="sourceType === 'local'" class="source-row">
        <input v-model="sourceValue" type="text" aria-label="本地模型文件夹路径" placeholder="选择本地模型文件夹" readonly>
        <button class="outline-button" type="button" :disabled="scanning || submitting" @click="chooseFolder">选择文件夹</button>
      </div>
      <label v-else class="form-field"><span>{{ sourceLabel }}公开模型仓库页面</span><input v-model="sourceValue" type="url" :disabled="scanning || submitting" placeholder="https://huggingface.co/用户/仓库 或 https://modelscope.cn/models/用户/仓库"></label>
      <button class="outline-button" type="button" :disabled="scanning || submitting || !sourceValue.trim()" @click="scanSource">{{ scanning ? '正在扫描…' : '扫描文件' }}</button>

      <template v-if="scan">
        <p class="instruction-hint">来源：{{ scan.source }}<template v-if="scan.revision"> · 固定修订：{{ scan.revision }}</template> · 文件 {{ scan.files.length }} 个 · GPT 权重候选 {{ weightFiles.length }} 个 · 音频候选 {{ audioFiles.length }} 个 · 转写候选 {{ transcriptFiles.length }} 个</p>
        <details class="import-file-list"><summary>查看扫描到的仓库文件（不会把无关文件作为导入条件）</summary><ul><li v-for="file in scan.files" :key="file.file_id"><span>{{ file.path }}</span><small>{{ sizeLabel(file.size) }}</small></li></ul></details>
        <div class="import-grid">
          <label class="form-field"><span>GPT 权重</span><select v-model="selectedGpt"><option v-for="file in weightFiles" :key="file.file_id" :value="file.file_id">{{ file.path }} · {{ file.role === 'gpt' ? '疑似 GPT' : file.role === 'sovits' ? '疑似 SoVITS' : '待检查' }}</option></select></label>
          <label class="form-field"><span>SoVITS 权重</span><select v-model="selectedSovits"><option v-for="file in weightFiles" :key="file.file_id" :value="file.file_id">{{ file.path }} · {{ file.role === 'sovits' ? '疑似 SoVITS' : file.role === 'gpt' ? '疑似 GPT' : '待检查' }}</option></select></label>
        </div>
        <div class="import-grid">
          <label class="form-field"><span>模型编号（英文唯一标识）</span><input v-model="modelId" maxlength="50" autocomplete="off"></label>
          <label class="form-field"><span>模型名称</span><input v-model="modelName" maxlength="80"></label>
        </div>
        <label class="form-field"><span>参考录音候选（可暂不选择）</span><select v-model="selectedAudio"><option value="">稍后补充参考录音</option><option v-for="file in audioFiles" :key="file.file_id" :value="file.file_id">{{ file.path }} · {{ sizeLabel(file.size) }}</option></select></label>
        <details class="import-file-list"><summary>选择需要登记的参考录音（已选 {{ selectedReferences.length }} / {{ audioFiles.length }}）</summary><ul><li v-for="file in audioFiles" :key="file.file_id"><label><input v-model="selectedReferences" type="checkbox" :value="file.file_id"> {{ file.path }}</label><small>{{ sizeLabel(file.size) }}</small></li></ul><p class="instruction-hint">默认选中的基础参考始终保留；没有配对文本或超出长度的录音会作为待完善资料保存。</p></details>
        <label class="form-field"><span>参考转写候选（选择后导入前会显示供核对）</span><select v-model="selectedTranscript"><option value="">手动输入转写</option><option v-for="file in transcriptFiles" :key="file.file_id" :value="file.file_id">{{ file.path }}</option></select></label>
        <label class="form-field"><span>参考录音准确转写（框架转换前需要提供）</span><textarea v-model="referenceText" maxlength="2000" rows="3" placeholder="没有参考音频时可以留空并稍后补充"></textarea></label>
        <label class="form-field"><span>参考语言</span><select v-model="referenceLanguage"><option value="zh">中文（普通话）</option><option value="yue">粤语</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option></select></label>
        <p class="instruction-hint">网页扫描只读取公开文件清单。确认导入后才下载你选择的权重和参考资料；本地来源只读，项目缓存中的任务暂存仅在转换、验证及登记成功后清理。</p>
        <button class="save-button" type="button" :disabled="!canSubmit || scanning" @click="createImport">{{ submitting ? '正在准备…' : '确认下载并转换' }}</button>
      </template>
    </div>
  </section>
</template>

<style scoped>
.aniflive-import-card,.import-panel { display:grid; gap:14px; }
.aniflive-import-card { padding:18px; border:1px solid var(--line-strong); border-radius:16px; background:var(--panel); }
.import-heading,.import-job-heading,.source-row { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.import-heading h3 { margin:0 0 5px; }
.import-heading p,.import-job-heading span { margin:0; color:var(--text-muted); font-size:.9rem; }
.import-source-tabs { display:flex; flex-wrap:wrap; gap:8px; }
.import-source-tabs button,.outline-button { min-height:38px; padding:7px 12px; border:1px solid var(--line-strong); border-radius:9px; background:transparent; color:inherit; cursor:pointer; }
.import-source-tabs button.active { border-color:var(--accent); color:var(--accent); }
.source-row input { flex:1; min-width:0; }
.import-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.import-job { display:grid; gap:10px; padding:13px; border:1px solid var(--line); border-radius:12px; }
.import-job progress { width:100%; }
.reference-repair { display:grid; gap:11px; }
.import-file-list { max-height:220px; overflow:auto; border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
.import-file-list ul { display:grid; gap:5px; padding-left:20px; }
.import-file-list li { overflow-wrap:anywhere; }
.import-file-list small { margin-left:8px; color:var(--text-muted); }
@media(max-width:650px) { .import-grid { grid-template-columns:minmax(0,1fr); } .import-heading,.source-row { align-items:stretch; flex-direction:column; } }
</style>
