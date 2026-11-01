<script setup lang="ts">
import { useProductContext } from '../product-context';
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import InstructionEditorRow from '../../interface/InstructionEditorRow.vue';
const { saving, settings, activePersona, personaDocuments, customInstructions, saveShadowGate,
  savePersona, saveApprovalSettings, personaItems, addInstructionRow, instructionExpanded,
  instructionBusy, instructionErrors, saveInstructionRow, deleteInstructionRow,
  toggleInstructionEntry, currentDocument } = useProductContext();
</script>
<template>
  <SettingsPageHeader section="persona" />
  <div class="persona-layout">
    <nav class="persona-nav persona-nav-with-note" aria-label="人格文档">
      <button v-for="document in personaItems" :key="document.id" type="button" :class="{ active: activePersona === document.id }" :title="`${document.label} · ${document.owner}`" @click="activePersona = document.id"><strong>{{ document.label }}</strong><small>{{ document.owner }}</small></button>
    </nav>
    <label class="document-editor"><span :title="currentDocument.path">{{ currentDocument.path }}（文档路径）</span><textarea v-model="personaDocuments[currentDocument.id]" :aria-label="`${currentDocument.label}文档内容`" spellcheck="false"></textarea><small>文档最多 64 KB；保存后新会话生效，当前会话继续使用原人格与资料快照。</small></label>
  </div>
  <footer><button class="save-button" type="button" :disabled="saving" @click="savePersona">{{ saving ? '正在保存…' : '保存人格与用户资料' }}</button></footer>
  <section class="preview-section instruction-section" aria-label="自定义指令">
    <div class="section-row"><h3>自定义语音指令</h3><button class="ghost-action" type="button" :disabled="saving" @click="addInstructionRow">添加指令</button></div>
    <p class="instruction-hint">点击名称展开编辑，每条指令单独保存。</p>
    <div v-if="!customInstructions.length" class="pool-empty">还没有自定义指令。</div>
    <div class="instruction-list">
      <InstructionEditorRow v-for="item in customInstructions" :key="item.id" v-model:expanded="instructionExpanded[item.id]" :row="item" :disabled="saving || instructionBusy.includes(item.id)" :saved="settings.custom_instructions.some(saved => saved.id === item.id)" :error="instructionErrors[item.id]" removable @save="saveInstructionRow(item.id)" @toggle="toggleInstructionEntry(item.id, $event)" @remove="deleteInstructionRow(item.id)" />
    </div>
  </section>
  <section class="preview-section approval-settings-section" aria-label="授权确认">
    <div class="section-row"><h3>授权确认</h3></div>
    <label class="switch-row"><div><strong>语音确认</strong><span>后台需要授权时，我会说明操作并询问你；明确同意后才执行，仍可使用工作台上的授权按钮。</span></div><input v-model="settings.approvals.voice_confirmation_enabled" type="checkbox" role="switch" :disabled="saving" @change="saveApprovalSettings" /><i></i></label>
    <label class="switch-row"><div><strong>连续提权</strong><span>先语音确认本次录音窗口的第一条授权，再自动同意后续请求。进入关键词静默或手动停止录音后结束，下次唤醒重新确认。</span></div><input v-model="settings.approvals.continuous_enabled" type="checkbox" role="switch" :disabled="saving || !settings.approvals.voice_confirmation_enabled" @change="saveApprovalSettings" /><i></i></label>
  </section>
  <label class="switch-row context-gate-row"><div><strong>实验性 Shadow 上下文门禁</strong><span>Shadow（影子代理）的侧载模型只判断运行中 DSH（代理执行宿主）的新引导应进入当前步骤还是下一回合；权威上下文仍完整同步。关闭后恢复直接注入。</span></div><input v-model="settings.shadow.experimental_context_gate_enabled" type="checkbox" role="switch" :disabled="saving" @change="saveShadowGate" /><i></i></label>
</template>
<style scoped>
.instruction-list{display:grid;gap:12px}
</style>
