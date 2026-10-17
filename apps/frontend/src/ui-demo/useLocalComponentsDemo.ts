import { computed, onBeforeUnmount, reactive, ref, watch, type Ref } from 'vue'
import { formatComponentMemory, type LocalComponentCategory, type LocalComponentRow, type LocalComponentsModel, type ManagedResourceRow } from '../interface/local-components'
import { demoComputeModels, demoSystemComponents, longIdentifier, previewRows, type PreviewScene } from './demo-data'

/** useLocalComponentsDemo（本地组件样例）：状态、重试与保存均在展示内存中完成。 */
export function useLocalComponentsDemo(scene: Ref<PreviewScene>, feedback: (message: string) => void) {
  const selectedDevices = reactive<Record<string, string>>({})
  const savedDevices = reactive<Record<string, string>>({})
  const recovered = reactive(new Set<string>())
  const retrying = reactive(new Set<string>())
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const sampledAt = ref('2026-10-03 14:20:00')
  const providerConfigured = reactive({ deepseek: true, minimax: true }) // providerConfigured（演示账号是否已配置在线服务）。
  const resourceSamples: ManagedResourceRow[] = [
    { id: 'paraformer-streaming', label: 'Paraformer（流式语音识别）', state: 'installed', status: '已安装' },
    { id: 'sensevoice-small', label: 'SenseVoiceSmall（语音理解）', state: 'incomplete', status: '安装不完整', error: '缺少配置文件，可重新下载或导入。' },
    { id: 'kws-sherpa-onnx', label: 'KWS（关键词识别）', state: 'installed', status: '已安装' },
    { id: 'smart-turn', label: 'SmartTurn（语音轮次判断）', state: 'missing', status: '未安装' },
    { id: 'campplus-zh-cn-common', label: 'CAM++（中文通用声纹模型）', state: 'installed', status: '已安装' },
    { id: 'fsmn-vad', label: 'FSMN-VAD（语音活动检测）', state: 'missing', status: '未安装' },
    { id: 'streaming-punctuation', label: 'CTTransformerStreaming（流式标点）', state: 'missing', status: '未安装' },
    { id: 'tts-voxcpm1', label: 'VoxCPM1（本地语音合成模型）', state: 'missing', status: '未安装', required: false },
    { id: 'tts-voxcpm2', label: 'VoxCPM2（本地语音合成模型）', state: 'installed', status: '已安装', required: false },
    { id: 'tts-audiocpp-runtime', label: 'audio.cpp（语音合成共享运行程序）', state: 'installed', status: '已安装', required: false },
    { id: 'dsh', label: 'DSH（代理执行宿主）', state: 'missing', status: '未安装' },
    { id: 'voice-server', label: 'Voice Server（语音服务）', state: 'installed', status: '已安装' },
    { id: 'voice-runtime-cpu', label: 'CPU（中央处理器）运行依赖', state: 'installed', status: '已安装', required: true },
    { id: 'voice-runtime-gpu', label: 'GPU（图形处理器）运行依赖', state: 'missing', status: '未安装', required: false },
  ]
  const runtimes: Record<string, string> = {
    paraformer: 'PyTorch（推理框架）', sensevoice: 'PyTorch（推理框架）', voice_vad: 'PyTorch（推理框架）',
    kws: 'Sherpa-ONNX（唤醒引擎）', smart_turn: 'ONNX（推理引擎）', campplus: 'PyTorch（推理框架）',
    dsh: 'Node.js（执行环境）', voice: 'Python（语音进程）', web: 'WebView2（桌面引擎）',
  }
  const sourceRows = [
    ...demoComputeModels.map(item => ({ ...item, description: '本地语音处理模型。', pid: item.id === 'paraformer' ? '21048' : item.id === 'sensevoice' ? '21052' : '21044', commit: `${Math.round(Number.parseInt(item.memory) * 1.25)} MB`, cpu: '0.3%' })),
    ...demoSystemComponents.filter(item => ['dsh', 'voice'].includes(item.id)).sort((a, b) => ['dsh', 'voice'].indexOf(a.id) - ['dsh', 'voice'].indexOf(b.id)).map(item => ({ ...item, memory: item.id === 'voice' ? '866 MB' : item.memory, commit: item.id === 'voice' ? '1042 MB' : item.commit,
      device: 'local', gpu: false, description: item.id === 'voice' ? '统一管理桌面窗口、界面服务、应用宿主、环境检查与语音服务。' : item.phase })),
  ]
  function cancelRetries() {
    timers.forEach(timer => clearTimeout(timer))
    timers.clear()
    retrying.clear()
  }
  watch(scene, () => { cancelRetries(); recovered.clear() })
  onBeforeUnmount(cancelRetries)

  const rows = computed<LocalComponentRow[]>(() => {
    const componentRows: LocalComponentRow[] = previewRows(sourceRows, scene.value).map(item => {
    const baseId = item.id.replace(/-\d+$/, '')
    const busy = retrying.has(item.id)
    const loading = scene.value === 'loading' || busy
    const failed = scene.value === 'error' && ['sensevoice', 'dsh'].includes(baseId) && !recovered.has(item.id)
    const available = !loading && !failed
    const model = demoComputeModels.find(row => row.id === baseId)
    const resourceId: Record<string, string> = {
      paraformer: 'paraformer-streaming', sensevoice: 'sensevoice-small', voice_vad: 'fsmn-vad',
      kws: 'kws-sherpa-onnx', smart_turn: 'smart-turn', campplus: 'campplus-zh-cn-common', dsh: 'dsh', voice: 'voice-server',
    }
    const resource = resourceSamples.find(row => row.id === resourceId[baseId])
    const countedHere = !['voice_vad', 'kws', 'smart_turn'].includes(baseId)
    return {
      id: baseId === 'voice' ? item.id.replace(/^voice/, 'voice-system') : item.id,
      name: scene.value === 'long' ? `${item.name} · ${longIdentifier}` : item.name,
      category: (baseId === 'voice' ? 'voice-server' : baseId === 'dsh' ? 'inference' : ['paraformer', 'sensevoice', 'smart_turn'].includes(baseId) ? 'speech' : 'extensions') as LocalComponentCategory,
      state: loading ? 'loading' : failed ? 'failed' : 'ready',
      status: loading ? '启动中' : failed ? '不可用' : '运行中',
      available, retryable: failed && !loading, busy: loading,
      runtime: runtimes[baseId],
      residentMemory: available && countedHere ? item.memory : undefined, privateMemory: available && countedHere ? item.commit : undefined,
      vram: item.device === 'cuda' && available ? '740 MB' : undefined,
      description: loading ? busy ? '正在重新启动组件' : '等待组件启动' : item.description,
      error: failed && !loading ? baseId === 'dsh' ? '代理执行宿主未启动，请重试。' : '模型加载失败，请重试。' : undefined,
      note: countedHere ? '同一进程可承载多个模型，内存总量按进程去重。' : baseId === 'smart_turn' ? '与 SenseVoice（语音识别与分析）共享进程，资源计入该条目。' : '与 Voice Server（语音服务）共享进程，资源计入语音服务条目。',
      location: '本机',
      facts: [
        { label: '进程标识', value: available ? item.pid : '未运行' },
        { label: '处理器占用', value: available ? item.cpu : '—' },
      ],
      resource: resource && scene.value === 'error' && ['sensevoice', 'dsh'].includes(baseId)
        ? { ...resource, state: 'installed', status: '已安装', error: undefined } : resource,
      runtimeDependencies: baseId === 'voice' ? ['voice-runtime-cpu', 'tts-audiocpp-runtime', 'voice-runtime-gpu']
        .flatMap(id => resourceSamples.find(resource => resource.id === id) ?? []) : undefined,
      device: model ? {
        value: selectedDevices[item.id] ?? savedDevices[item.id] ?? model.device,
        locked: !available,
        current: model.device === 'cuda' ? 'GPU（图形处理器）' : 'CPU（中央处理器）',
        pending: (savedDevices[item.id] ?? model.device) !== model.device,
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          ...(model.gpu ? [{ value: 'cuda', label: 'GPU（图形处理器）' }] : []),
        ],
      } : undefined,
    }
    })
    if (scene.value === 'empty') return []
    const punctuation: LocalComponentRow = {
      id: 'punctuation', name: 'CTTransformerStreaming（流式标点）', category: 'speech',
      state: scene.value === 'loading' ? 'loading' : 'ready', status: scene.value === 'loading' ? '启动中' : '运行中',
      available: scene.value !== 'loading', busy: scene.value === 'loading', runtime: 'ONNX（推理引擎）',
      description: '为流式识别文字添加标点，复用识别工作进程。', facts: [],
      resource: resourceSamples.find(row => row.id === 'streaming-punctuation'),
    }
    const hotwords: LocalComponentRow = {
      id: 'hotwords', name: '热词修订', category: 'extensions', state: 'ready', status: '内置能力',
      available: true, runtime: '语音主服务内', description: '识别后修订热词；复用语音主服务，不需要独立下载。', facts: [],
    }
    const providerSettings = (provider: 'deepseek' | 'minimax') => ({
      provider,
      configured: providerConfigured[provider],
      usageHint: '演示界面不会连接服务或读取真实密钥。',
      save: async (_apiKey: string) => {
        providerConfigured[provider] = true
        return { configured: true, message: '演示配置已保存。' }
      },
    })
    const tts: LocalComponentRow = {
      id: 'tts', name: 'TTS（语音合成）', category: 'tts',
      state: 'ready', status: '本地模型已就绪', available: true,
      runtime: 'audio.cpp（原生音频推理框架）',
      description: '任一本地 VoxCPM 模型与共享运行程序就绪，或配置 MiniMax 在线 API，即可通过语音合成校验。',
      facts: [], provider: providerSettings('minimax'),
      resourceChoices: resourceSamples.filter(row => ['tts-voxcpm1', 'tts-voxcpm2'].includes(row.id)),
      sharedResource: resourceSamples.find(row => row.id === 'tts-audiocpp-runtime'),
    }
    const deepseek: LocalComponentRow = {
      id: 'deepseek', name: 'DeepSeek 在线 API（推理接口）', category: 'inference',
      state: 'ready', status: '已配置', available: true, runtime: '在线 API（推理接口）',
      description: '配置 DeepSeek 接口密钥并查询账户余额。演示界面不会连接服务或读取真实密钥。',
      facts: [], provider: providerSettings('deepseek'),
    }
    return [...componentRows, punctuation, tts, deepseek, hotwords]
  })
  const model = computed<LocalComponentsModel>(() => {
    const failed = rows.value.some(row => row.state === 'failed')
    const loading = rows.value.some(row => row.state === 'loading')
    const empty = scene.value === 'empty'
    const sumMetric = (key: 'residentMemory' | 'privateMemory' | 'vram') => empty ? undefined
      : formatComponentMemory(rows.value.reduce((sum, row) => sum + (row[key] ? Number.parseFloat(row[key]) * 1024 ** 2 : 0), 0))
    return {
      status: empty ? '暂无数据' : failed ? '部分组件不可用' : loading ? '组件启动中' : '运行正常',
      state: empty ? 'disabled' : failed ? 'failed' : loading ? 'loading' : 'ready',
      sampledAt: empty ? '—' : sampledAt.value,
      resources: [
        { id: 'resident', label: '常驻内存', used: sumMetric('residentMemory'), total: empty ? undefined : '32 GB' },
        { id: 'commit', label: '提交内存', used: sumMetric('privateMemory'), total: empty ? undefined : '48 GB' },
        { id: 'vram', label: '显存', used: sumMetric('vram'), total: empty ? undefined : '8 GB' },
      ],
      downloadResources: empty ? [] : resourceSamples,
      managedResourceJobs: scene.value === 'loading' ? [{
        id: 'sample-resource-job', resourceId: 'dsh', label: 'DSH（代理执行宿主）', state: 'running',
        phase: '正在校验固定版本安装包', completedBytes: 164_000_000, totalBytes: 289_000_000, canCancel: true,
      }] : scene.value === 'error' ? [{
        id: 'sample-resource-failure', resourceId: 'sensevoice-small', label: 'SenseVoiceSmall（语音理解）', state: 'failed',
        phase: '导入已停止，原有安装保持不变', error: '压缩包校验失败，可重试导入或下载。', canCancel: false,
      }] : [],
      resourceActionsAvailable: !empty,
      resourceLoading: scene.value === 'loading',
      resourceError: scene.value === 'error' ? '最近的资源校验未通过。' : undefined,
      resourceEventError: undefined,
      resourceTasksBusy: loading,
      resourceBatchRetry: false,
      rows: rows.value,
    }
  })
  function retry(id: string) {
    if (!rows.value.find(row => row.id === id)?.retryable || retrying.has(id)) return
    retrying.add(id)
    const timer = setTimeout(() => {
      recovered.add(id)
      retrying.delete(id)
      timers.delete(timer)
      feedback('组件已恢复')
    }, 900)
    timers.add(timer)
  }
  function device(id: string, value: string) { selectedDevices[id] = value }
  function save(id: string) {
    savedDevices[id] = selectedDevices[id] ?? rows.value.find(row => row.id === id)?.device?.value ?? 'auto'
    feedback('运行位置已保存，下次启动生效')
  }
  function refresh() { sampledAt.value = new Date().toLocaleString('zh-CN', { hour12: false }); feedback('组件状态已刷新') }
  return { model, retry, device, save, refresh }
}
