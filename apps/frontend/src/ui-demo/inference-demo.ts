import { ref } from 'vue'
/** demoInferenceBackend（样例已生效推理选择）：设置页与工作台共用，无网络调用。 */
export const demoInferenceBackend = ref<'deepseek' | 'minimax'>(new URLSearchParams(window.location.search).get('backend') === 'minimax' ? 'minimax' : 'deepseek')
