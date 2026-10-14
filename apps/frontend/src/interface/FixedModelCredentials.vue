<script setup lang="ts">
defineProps<{
  configured?: { deepseek: boolean; minimax: boolean }; // configured（两家服务是否已有密钥）。
  disabled?: boolean; // disabled（加载或保存期间禁用操作）。
  saving?: boolean; // saving（正在保存密钥）。
  startup?: boolean; // startup（启动前配置）：保存后即可启动，无需先运行后端。
  minimaxOptional?: boolean; // minimaxOptional（在线语音密钥可选）：已选择本地语音。
}>();
const deepseek = defineModel<string>('deepseek', { required: true }); // deepseek（DeepSeek 语言模型服务的接口密钥草稿）。
const minimax = defineModel<string>('minimax', { required: true }); // minimax（MiniMax 语音合成服务的接口密钥草稿）。
const emit = defineEmits<{ save: [] }>();
</script>
<template>
  <form class="fixed-model-credentials" @submit.prevent="emit('save')">
    <section v-for="provider in ['deepseek', 'minimax'] as const" :key="provider" class="fixed-provider-card" :aria-label="provider === 'deepseek' ? 'DeepSeek（语言模型服务）' : 'MiniMax（语音合成服务）'">
      <header>
        <div><h3>{{ provider === 'deepseek' ? 'DeepSeek' : 'MiniMax' }}</h3><p>{{ provider === 'deepseek' ? '语言模型服务' : '语音合成服务' }}</p></div>
        <span class="entry-status" :class="{ configured: configured?.[provider] }">{{ configured?.[provider] ? '已配置' : '未配置' }}</span>
      </header>
      <p v-if="provider === 'minimax' && minimaxOptional" class="instruction-hint">当前使用本地语音，在线语音接口密钥可选。</p>
      <label class="form-field"><span>{{ provider === 'deepseek' ? 'DeepSeek' : 'MiniMax' }} API Key（接口密钥）</span>
        <input v-if="provider === 'deepseek'" v-model="deepseek" type="password" autocomplete="new-password" spellcheck="false" :disabled="disabled" :placeholder="configured?.deepseek ? '已配置，填写可更新' : '填写接口密钥'" />
        <input v-else v-model="minimax" type="password" autocomplete="new-password" spellcheck="false" :disabled="disabled" :placeholder="configured?.minimax ? '已配置，填写可更新' : '填写接口密钥'" />
      </label>
    </section>
    <footer class="fixed-credentials-footer"><p>{{ startup ? '组件下载期间也可保存接口密钥。留空保留已有密钥，启动程序时生效。' : '使用项目内固定模型。留空保留已有密钥，保存后重启应用生效。' }}</p><button class="save-button" type="submit" :disabled="disabled || (!deepseek.trim() && !minimax.trim())">{{ saving ? '正在保存…' : '保存接口密钥' }}</button></footer>
  </form>
</template>
