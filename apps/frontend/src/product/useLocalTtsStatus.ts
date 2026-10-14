import { onMounted, onUnmounted, ref } from 'vue'
import { productApi } from './api'
import { useApplicationHost } from './application-host'
import type { ProductLocalTtsStatus } from './types'

/**
 * useLocalTtsStatus（读取本地 TTS 状态）：首次启动时服务接口可能先于模型完成，
 * 因此持续读取到模型就绪、加载失败或本轮启动结束。
 */
export function useLocalTtsStatus(localBackendEnabled: () => boolean) {
  const application = useApplicationHost()
  const status = ref<ProductLocalTtsStatus | null>(null)
  const loading = ref(false)
  const error = ref('')
  let request: Promise<void> | undefined
  let timer: number | undefined
  let disposed = false
  let pollCount = 0

  function shouldPoll() {
    if (disposed || (!localBackendEnabled() && !status.value?.pending_selection) || (pollCount >= 120 && !status.value?.pending_selection)) return false
    if (status.value?.pending_selection) return true
    if (error.value || !status.value || status.value.load_state === 'loading') return true
    if (status.value.load_state === 'ready' && status.value.loaded_model !== status.value.local_model) return true
    return status.value.load_state === 'unloaded'
      && application.snapshot.value?.startup_complete !== true
  }

  function schedulePoll() {
    if (timer !== undefined || !shouldPoll()) return
    const delay = error.value ? 2500 : status.value?.load_state === 'loading' ? 1000 : 2000
    timer = window.setTimeout(() => {
      timer = undefined
      pollCount += 1
      void refresh()
    }, delay)
  }

  function refresh(afterPending = false): Promise<void> {
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
    if (request) return afterPending ? request.then(() => refresh()) : request
    loading.value = true
    request = productApi.ttsStatus().then(value => {
      status.value = value
      error.value = ''
    }).catch(failure => {
      error.value = failure instanceof Error ? failure.message : '无法读取本地 TTS（语音合成）状态。'
    }).finally(() => {
      loading.value = false
      request = undefined
      schedulePoll()
    })
    return request
  }

  onMounted(() => { if (localBackendEnabled()) void refresh() })
  onUnmounted(() => {
    disposed = true
    if (timer !== undefined) window.clearTimeout(timer)
  })

  return { status, loading, error, refresh }
}
