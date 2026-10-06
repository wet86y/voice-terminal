import { computed, onBeforeUnmount, reactive, ref, watch, type Ref } from 'vue'
import type { LocalComponentRow, LocalComponentsModel } from '../interface/local-components'
import { demoComputeModels, demoSystemComponents, longIdentifier, previewRows, type PreviewScene } from './demo-data'

/** useLocalComponentsDemo（本地组件样例）：状态、重试与保存均在展示内存中完成。 */
export function useLocalComponentsDemo(scene: Ref<PreviewScene>, feedback: (message: string) => void) {
  const selectedDevices = reactive<Record<string, string>>({})
  const savedDevices = reactive<Record<string, string>>({})
  const recovered = reactive(new Set<string>())
  const retrying = reactive(new Set<string>())
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const sampledAt = ref('2026-10-03 14:20:00')
  const runtimes: Record<string, string> = {
    paraformer: 'PyTorch（推理框架）', sensevoice: 'PyTorch（推理框架）', voice_vad: 'PyTorch（推理框架）',
    kws: 'Sherpa-ONNX（唤醒引擎）', smart_turn: 'ONNX（推理引擎）', wespeaker: 'ONNX（推理引擎）',
    dsh: 'Node.js（执行环境）', voice: 'Python（语音进程）', web: 'WebView2（桌面引擎）',
  }
  const sourceRows = [
    ...demoComputeModels.map(item => ({ ...item, description: '本地语音处理模型。', pid: item.id === 'paraformer' ? '21048' : item.id === 'sensevoice' ? '21052' : '21044', commit: `${Math.round(Number.parseInt(item.memory) * 1.25)} MB`, cpu: '0.3%' })),
    ...demoSystemComponents.filter(item => ['dsh', 'voice', 'web'].includes(item.id)).sort((a, b) => ['dsh', 'voice', 'web'].indexOf(a.id) - ['dsh', 'voice', 'web'].indexOf(b.id)).map(item => ({ ...item, device: 'local', gpu: false, description: item.phase })),
  ]
  function cancelRetries() {
    timers.forEach(timer => clearTimeout(timer))
    timers.clear()
    retrying.clear()
  }
  watch(scene, () => { cancelRetries(); recovered.clear() })
  onBeforeUnmount(cancelRetries)

  const rows = computed<LocalComponentRow[]>(() => previewRows(sourceRows, scene.value).map(item => {
    const baseId = item.id.replace(/-\d+$/, '')
    const busy = retrying.has(item.id)
    const loading = scene.value === 'loading' || busy
    const failed = scene.value === 'error' && ['sensevoice', 'dsh'].includes(baseId) && !recovered.has(item.id)
    const available = !loading && !failed
    const model = demoComputeModels.find(row => row.id === baseId)
    return {
      id: item.id,
      name: scene.value === 'long' ? `${item.name} · ${longIdentifier}` : item.name,
      state: loading ? 'loading' : failed ? 'failed' : 'ready',
      status: busy ? '正在恢复' : loading ? '启动中' : failed ? '不可用' : '运行中',
      available, retryable: failed && !loading, busy: loading,
      runtime: runtimes[baseId],
      residentMemory: available ? item.memory : '—', privateMemory: available ? item.commit : '—',
      vram: item.device === 'cuda' ? available ? '740 MB' : '—' : undefined,
      description: item.description,
      error: failed ? baseId === 'dsh' ? '代理执行宿主未启动，请重试。' : '模型加载失败，请重试。' : undefined,
      note: '同一进程可承载多个模型，内存总量按进程去重。',
      location: '本机',
      facts: [
        { label: '进程标识', value: available ? item.pid : '未运行' },
        { label: '处理器占用', value: available ? item.cpu : '—' },
      ],
      device: model ? {
        value: selectedDevices[item.id] ?? savedDevices[item.id] ?? model.device,
        current: model.device === 'cuda' ? 'GPU（图形处理器）' : 'CPU（中央处理器）',
        pending: (savedDevices[item.id] ?? model.device) !== model.device,
        options: [
          { value: 'auto', label: '自动选择' }, { value: 'cpu', label: 'CPU（中央处理器）' },
          { value: 'cuda', label: 'GPU（图形处理器）', disabled: !model.gpu },
        ],
      } : undefined,
    }
  }))
  const model = computed<LocalComponentsModel>(() => {
    const failed = rows.value.some(row => row.state === 'failed')
    const loading = rows.value.some(row => row.state === 'loading')
    const empty = scene.value === 'empty'
    return {
      status: empty ? '暂无数据' : failed ? '部分组件不可用' : loading ? '组件启动中' : '运行正常',
      state: empty ? 'disabled' : failed ? 'failed' : loading ? 'loading' : 'ready',
      sampledAt: empty ? '—' : sampledAt.value,
      resources: [
        { id: 'resident', label: '常驻内存', used: empty ? '未提供' : '2.01 GB', total: empty ? '未提供' : '32 GB' },
        { id: 'commit', label: '提交内存', used: empty ? '未提供' : '2.45 GB', total: empty ? '未提供' : '48 GB' },
        { id: 'vram', label: '显存', used: empty ? '未提供' : '2.2 GB', total: empty ? '未提供' : '8 GB' },
      ],
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
