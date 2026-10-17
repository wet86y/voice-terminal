<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { LocalProviderSettings } from './local-components'

const props = defineProps<{ model: LocalProviderSettings }>()
const emit = defineEmits<{ saved: [] }>()
const configured = ref(props.model.configured) // configured（本机是否已有密钥）。
const draft = ref('') // draft（只保存在当前页面内存的密钥草稿）。
const saving = ref(false) // saving（正在保存密钥）。
const saveError = ref('') // saveError（保存密钥时的错误）。
const notice = ref('') // notice（保存结果说明）。
const name = computed(() => props.model.provider === 'deepseek' ? 'DeepSeek' : 'MiniMax')
watch(() => props.model.configured, value => { configured.value = value })

async function save() {
  if (saving.value || props.model.disabled || !draft.value.trim()) return
  saving.value = true
  saveError.value = ''
  notice.value = ''
  try {
    const response = await props.model.save(draft.value)
    configured.value = response.configured
    draft.value = ''
    notice.value = response.message || '接口密钥已安全保存。'
    emit('saved')
  } catch (failure) {
    saveError.value = failure instanceof Error ? failure.message : '接口密钥保存失败。'
  } finally { saving.value = false }
}
</script>

<template>
  <section class="provider-settings-detail" :data-provider="model.provider" :aria-label="`${name}（在线服务）设置`">
    <div class="provider-key-settings">
      <div class="provider-settings-heading">
        <div><h4>{{ name }}（在线服务）</h4><p>密钥保存在本机系统凭据库，不会返回到页面。</p></div>
        <span class="provider-key-state" :class="configured ? 'configured' : 'missing'">{{ model.disabled ? '正在读取' : configured ? '已配置' : '未配置' }}</span>
      </div>
      <div class="provider-key-entry">
        <label class="form-field"><span>{{ name }} API Key（接口密钥）</span><input v-model="draft" type="password" autocomplete="new-password" spellcheck="false" :disabled="model.disabled || saving" :placeholder="configured ? '已配置，填写可更新' : '填写接口密钥'" /></label>
        <button class="save-button" type="button" :disabled="model.disabled || saving || !draft.trim()" @click="save">{{ saving ? '正在保存…' : '保存密钥' }}</button>
      </div>
      <small class="provider-settings-hint">留空保留已有密钥；{{ model.provider === 'deepseek' ? '保存后供推理服务使用。' : '保存后可作为语音合成的在线方式。' }}</small>
      <p v-if="notice" class="provider-settings-message notice" role="status">{{ notice }}</p>
      <p v-if="saveError" class="provider-settings-message error" role="alert">{{ saveError }}</p>
    </div>
  </section>
</template>

<style scoped>
.provider-settings-detail{display:grid;gap:18px;width:100%;min-width:0}.provider-key-settings{display:grid;gap:12px;padding:16px;border:1px solid var(--line);border-radius:14px;background:#ffffff03}.provider-settings-heading{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px}.provider-settings-heading>div{display:grid;gap:4px;min-width:0}.provider-settings-heading h4{font-size:var(--font-body)}.provider-settings-heading p,.provider-settings-hint{font-size:var(--font-caption);line-height:1.6;color:var(--muted)}.provider-key-state{font-size:var(--font-caption);color:var(--muted)}.provider-key-state.configured{color:var(--mint)}.provider-key-state.missing{color:var(--accent)}.provider-key-entry{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;min-width:0}.provider-key-entry>.form-field{flex:0 1 420px;max-width:420px;min-width:min(100%,260px)}.provider-key-entry input{width:100%;height:42px;min-height:42px}.provider-key-entry .save-button{height:42px;min-height:42px;padding:7px 14px;font-size:var(--font-body);white-space:nowrap}.provider-settings-hint{display:block}.provider-settings-message{font-size:var(--font-caption);line-height:1.6}.provider-settings-message.error{color:var(--danger)}.provider-settings-message.notice{color:var(--mint)}
</style>
