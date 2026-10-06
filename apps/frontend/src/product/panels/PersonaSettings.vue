<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
const {
  voice,
  settings,
  saving,
  shadow,
  activePersona,
  personaDocuments,
  customInstructions,
  dsh,
  saveShadowGate,
  savePersona,
  personaItems,
  instructionDraft,
  instructionFormError,
  editInstruction,
  clearInstructionDraft,
  stageInstruction,
  saveInstructionSettings,
  removeInstruction,
  currentDocument,
} = useProductContext();
</script>
<template>
  <SettingsPageHeader section="persona" />
  <section class="instruction-section" aria-label="自定义指令">
    <h3>自定义指令</h3>
    <p class="instruction-hint">
      语音模型按整句话的意思判断；影子代理会独立阅读用户原话。配置不会增加模型的工具权限。
    </p>
    <div v-if="!customInstructions.length" class="instruction-empty">
      还没有自定义指令。可在下方添加第一条。
    </div>
    <div
      v-for="item in customInstructions"
      :key="item.id"
      class="instruction-row"
    >
      <div>
        <strong>{{ item.trigger }}</strong
        ><small
          >{{ item.target === "shadow" ? "影子代理执行" : "语音链路执行" }} ·
          {{ item.instruction }}</small
        >
      </div>
      <label class="instruction-toggle" :title="item.enabled ? '停用这条指令' : '启用这条指令'"
        ><input
          v-model="item.enabled"
          type="checkbox"
          role="switch"
          :aria-label="`启用指令：${item.trigger}`"
        /><i aria-hidden="true"></i></label
      >
      <button type="button" @click="editInstruction(item)">编辑</button>
      <button type="button" @click="removeInstruction(item.id)">删除</button>
    </div>
    <div class="instruction-form">
      <h4>{{ instructionDraft.id ? "编辑指令" : "添加指令" }}</h4>
      <div class="instruction-form-fields">
      <label class="form-field"
        ><span>触发语义</span
        ><input
          v-model="instructionDraft.trigger"
          type="text"
          maxlength="200"
          placeholder="例如：帮我查查明天的天气"
        /></label
      >
      <label class="form-field"
        ><span>执行位置</span
        ><select v-model="instructionDraft.target">
          <option value="voice">语音链路</option>
          <option value="shadow">影子代理</option>
        </select></label
      >
      <label class="form-field instruction-target-field"
        ><span>{{
          instructionDraft.target === "shadow"
            ? "影子代理执行目标"
            : "语音模型执行目标"
        }}</span
        ><textarea
          v-model="instructionDraft.instruction"
          rows="1"
          maxlength="2000"
          :placeholder="
            instructionDraft.target === 'shadow'
              ? '例如：查询用户所问的资料，返回已核实的结果。'
              : '例如：暂停回应，等待用户继续说。'
          "
        ></textarea>
      </label>
      </div>
      <p class="instruction-hint">描述用户会怎样表达，不要求逐字匹配。</p>
      <p v-if="instructionFormError" class="instruction-error" role="alert">
        {{ instructionFormError }}
      </p>
      <div class="instruction-actions">
        <button type="button" @click="stageInstruction">
          {{ instructionDraft.id ? "更新条目" : "添加条目" }}</button
        ><button
          v-if="instructionDraft.id"
          type="button"
          @click="clearInstructionDraft"
        >
          取消编辑
        </button>
      </div>
    </div>
    <button
      class="save-button"
      type="button"
      :disabled="saving"
      @click="saveInstructionSettings"
    >
      {{ saving ? "正在保存…" : "保存自定义指令" }}
    </button>
    <small class="instruction-hint"
      >表单和列表中的增删改、启停会在保存后持久化，并在下一次会话或重置后生效。</small
    >
  </section>
  <div class="persona-layout">
    <nav class="persona-nav persona-nav-with-note">
      <button
        v-for="document in personaItems"
        :key="document.id"
        type="button"
        :class="{ active: activePersona === document.id }"
        :title="`${document.label} · ${document.owner}`"
        @click="activePersona = document.id"
      >
        <strong>{{ document.label }}</strong
        ><small>{{ document.owner }}</small>
      </button>
    </nav>
    <label class="document-editor"
      ><span :title="currentDocument.path">{{ currentDocument.path }}（文档路径）</span
      ><textarea
        v-model="personaDocuments[currentDocument.id]"
        :aria-label="`${currentDocument.label}文档内容`"
        spellcheck="false"
      ></textarea
      ><small>文档最多 64 KB；保存后在下一次会话生效。</small></label
    >
  </div>
  <label class="switch-row"
    ><div>
      <strong>实验性影子代理上下文门禁</strong
      ><span
        >开启后，侧载模型只判断运行中 DSH
        的新引导应进入当前步骤还是下一回合；权威上下文仍完整同步。关闭后恢复直接注入。</span
      >
    </div>
    <input
      v-model="settings.shadow.experimental_context_gate_enabled"
      type="checkbox"
      :disabled="saving"
      @change="saveShadowGate" /><i></i
  ></label>
  <div v-if="shadow.backend === 'dsh'" class="config-callout">
    <span class="callout-icon">DSH</span>
    <p>
      DSH 的执行模型和工具由
      Harness（执行环境）管理；门禁开关只控制后续上下文的引导时机。
    </p>
  </div>
  <div class="info-callout">
    <span>β</span>
    <p>
      这是实验功能。一次暂缓仅对当次请求有效；下一次请求会重新判断。暂缓正文在当前激活结束后作为新激活的下一条消息进入，且保留原始版本顺序。
    </p>
  </div>
  <footer>
    <button
      class="save-button"
      type="button"
      :disabled="saving"
      @click="savePersona"
    >
      {{ saving ? "正在保存…" : "保存人格与用户资料" }}
    </button>
  </footer>
</template>
