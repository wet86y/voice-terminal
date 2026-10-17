import { computed, onMounted, ref } from 'vue'
import type { DesktopCredentialsPort } from '../product/desktop-port'
import type { FixedModelCredentials } from '../product/types'
import type { FixedProviderId, LocalProviderSettings } from '../interface/local-components'

/** useStartupCredentials（启动前接口设置）：密钥草稿只保存在组件表单内存。 */
export function useStartupCredentials(port: DesktopCredentialsPort, afterSave?: () => Promise<void>) {
  const configured = ref({ deepseek: false, minimax: false }) // configured（两家在线服务是否已配置）。
  const loading = ref(true) // loading（正在读取凭据状态）。
  const saving = ref(false) // saving（正在保存单个服务密钥）。
  const error = ref('') // error（可展示的启动或保存错误）。
  const notice = ref('') // notice（最近一次保存说明）。
  const disabled = computed(() => loading.value || saving.value)
  const localTtsSnapshot = ref(false) // localTtsSnapshot（凭据接口返回的本地 TTS 就绪状态）。
  const localTtsOverride = ref<boolean | null>(null) // localTtsOverride（资源清单更新后优先使用的就绪状态）。
  const localTts = computed(() => localTtsOverride.value ?? localTtsSnapshot.value)
  const ready = computed(() => !disabled.value && configured.value.deepseek && (localTts.value || configured.value.minimax))

  function update(result: FixedModelCredentials) {
    localTtsSnapshot.value = result.local_tts_ready === true
    configured.value = { deepseek: result.providers.deepseek.configured, minimax: result.providers.minimax.configured }
  }

  onMounted(async () => {
    try { update(await port.fixedCredentials()) }
    catch (failure) { error.value = String(failure) }
    finally { loading.value = false }
  })

  async function saveProvider(provider: FixedProviderId, apiKey: string) {
    if (disabled.value || !apiKey.trim()) throw new Error('请填写接口密钥。')
    saving.value = true
    error.value = ''
    notice.value = ''
    try {
      const request = provider === 'deepseek' ? { deepseek_api_key: apiKey } : { minimax_api_key: apiKey }
      const result = await port.saveFixedCredentials(request)
      update(result)
      try { await afterSave?.() } catch { /* 资源清单刷新失败由组件页单独显示，密钥仍已安全保存。 */ }
      const message = result.message || '接口密钥已保存，启动程序时生效。'
      notice.value = message
      return { configured: result.providers[provider].configured, message }
    } catch (failure) {
      error.value = failure instanceof Error ? failure.message : String(failure)
      throw failure
    } finally { saving.value = false }
  }

  function providerSettings(provider: FixedProviderId): LocalProviderSettings {
    return {
      provider,
      configured: configured.value[provider],
      disabled: disabled.value,
      usageHint: '启动应用后可查询账户用量。',
      save: apiKey => saveProvider(provider, apiKey),
    }
  }

  function setLocalTtsReady(value: boolean | null) { localTtsOverride.value = value }

  return { configured, loading, saving, disabled, ready, localTts, error, notice, saveProvider, providerSettings, setLocalTtsReady }
}
