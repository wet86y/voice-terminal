import { computed, onMounted, ref } from 'vue'
import type { DesktopCredentialsPort } from '../product/desktop-port'
import type { FixedModelCredentials } from '../product/types'

/** useStartupCredentials（启动前接口设置）：密钥草稿只存于当前表单内存。 */
export function useStartupCredentials(port: DesktopCredentialsPort) {
  const deepseek = ref('') // deepseek（语言模型密钥草稿）。
  const minimax = ref('') // minimax（语音合成密钥草稿）。
  const configured = ref({ deepseek: false, minimax: false }) // configured（固定服务配置状态）。
  const loading = ref(true) // loading（正在读取配置）。
  const saving = ref(false) // saving（正在保存）。
  const error = ref('') // error（可向用户展示的错误）。
  const notice = ref('') // notice（保存结果）。
  const disabled = computed(() => loading.value || saving.value)
  const localTts = ref(false) // localTts（本地语音选中时在线密钥可选）。
  const ready = computed(() => !disabled.value && configured.value.deepseek && (localTts.value || configured.value.minimax))
  function update(result: FixedModelCredentials) {
    localTts.value = result.tts_backend === 'voxcpm1'
    configured.value = { deepseek: result.providers.deepseek.configured, minimax: result.providers.minimax.configured }
  }
  onMounted(async () => {
    try { update(await port.fixedCredentials()) }
    catch (failure) { error.value = String(failure) }
    finally { loading.value = false }
  })
  async function save() {
    if (disabled.value) return
    saving.value = true; error.value = ''; notice.value = ''
    try {
      const result = await port.saveFixedCredentials({ deepseek_api_key: deepseek.value, minimax_api_key: minimax.value })
      update(result); deepseek.value = ''; minimax.value = ''
      notice.value = result.message || '接口密钥已保存，启动程序时生效。'
    } catch (failure) { error.value = String(failure) }
    finally { saving.value = false }
  }
  return { deepseek, minimax, configured, loading, saving, disabled, ready, localTts, error, notice, save }
}
