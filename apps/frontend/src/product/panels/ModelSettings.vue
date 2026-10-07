<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useProductContext } from '../product-context';
import { productApi } from '../api';
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import FixedModelCredentials from '../../interface/FixedModelCredentials.vue';
const { setNotice, setError } = useProductContext();
const deepseek = ref(''); // deepseek（语言服务密钥草稿）：仅保留在当前表单内存。
const minimax = ref(''); // minimax（语音合成密钥草稿）：仅保留在当前表单内存。
const configured = ref({ deepseek: false, minimax: false }); // configured（是否已有密钥）。
const loading = ref(true); // loading（正在读取配置状态）。
const saving = ref(false); // saving（正在保存）。
const disabled = computed(() => loading.value || saving.value);
onMounted(async () => {
  try {
    const result = await productApi.fixedCredentials();
    configured.value = { deepseek: result.providers.deepseek.configured, minimax: result.providers.minimax.configured };
  } catch (error) { setError(error); }
  finally { loading.value = false; }
});
async function save() {
  if (disabled.value) return;
  saving.value = true;
  try {
    const result = await productApi.saveFixedCredentials({ deepseek_api_key: deepseek.value, minimax_api_key: minimax.value });
    configured.value = { deepseek: result.providers.deepseek.configured, minimax: result.providers.minimax.configured };
    deepseek.value = ''; minimax.value = '';
    setNotice(result.message || '接口密钥已保存，重启应用后生效。');
  } catch (error) { setError(error); }
  finally { saving.value = false; }
}
</script>
<template>
  <SettingsPageHeader section="models" />
  <FixedModelCredentials v-model:deepseek="deepseek" v-model:minimax="minimax" :configured="configured" :disabled="disabled" :saving="saving" @save="save" />
</template>
