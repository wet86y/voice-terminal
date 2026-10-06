<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import ModelAccordion from '../../interface/ModelAccordion.vue';
import ModelAssignmentField from '../../interface/ModelAssignmentField.vue';
import ModelPoolEntry from '../../interface/ModelPoolEntry.vue';
const {
  settings,
  providerForm,
  providerStep,
  discoveredModels,
  modelAssignments,
  availableLlmModels,
  visibleLocalPool,
  hiddenPool,
  registry,
  modelScanState,
  restartState,
  scanModels,
  discoverProvider,
  verifyCustomModel,
  addVerifiedProvider,
  expandedModelSections,
  editingModelId,
  deleteCandidateId,
  modelEditDraft,
  roleLabels,
  llmAssignmentSlots,
  modelContextOptions,
  reasoningOptions,
  modelPoolSummary,
  apiModelPool,
  hiddenApiModelPool,
  hiddenLocalPool,
  formatTokens,
  providerClass,
  selectedModel,
  modelStatusLabel,
  formatContext,
  reasoningSummary,
  modelPolicySummary,
  toggleModelSection,
  isEditableApiModel,
  startEditingModel,
  toggleReasoningLevel,
  saveModelEdit,
  requestDeleteModel,
  confirmDeleteModel,
  toggleHidden,
  openApplyDialog,
  selectDiscoveredModel,
} = useProductContext();
function originLabel(origin: string) { return origin === 'product_managed' ? '产品托管' : origin === 'local_scan' ? '受管目录扫描' : origin === 'runtime_config' ? '运行时配置导入' : '现有配置导入'; }
</script>
<template>
  <SettingsPageHeader section="models">
    <template #actions>
      <button
        class="outline-button"
        type="button"
        :disabled="modelScanState === 'scanning'"
        @click="scanModels"
      >
        {{
          modelScanState === "scanning" ? "正在扫描…" : "扫描现有配置与受管目录"
        }}
      </button>
    </template>
  </SettingsPageHeader>
  <div class="model-safety">
    <span>✓</span>
    <p>
      API Key（接口密钥）
      只在本次验证与写入请求中使用，不写入浏览器存储。新配置应用失败时，现有模型、语音合成和会话配置继续有效。
    </p>
  </div>
  <div class="model-accordion-list">
    <ModelAccordion v-model:open="expandedModelSections.add" index="01" title="添加模型" :hint="providerStep === 'verified' ? '服务商连接 · 已验证' : '服务商连接'">
          <div class="provider-fields">
            <label
              ><span>连接名称</span
              ><input
                v-model="providerForm.name"
                type="text"
                autocomplete="off" /></label
            ><label
              ><span>服务地址</span
              ><input
                v-model="providerForm.baseUrl"
                type="url"
                autocomplete="url" /></label
            ><label
              ><span>协议</span
              ><select v-model="providerForm.protocol">
                <option value="openai_compatible">OpenAI 兼容（通用模型接口）</option>
                <option value="deepseek">DeepSeek Responses（响应接口）</option>
                <option value="opencode_go">OpenCode Go Responses（响应接口）</option>
                <option value="responses">Responses 兼容（响应接口）</option>
                <option value="minimax">MiniMax TTS（语音合成）</option>
                <option value="manual">手动模型标识</option>
              </select></label
            ><label
              ><span>API Key（接口密钥）</span
              ><input
                v-model="providerForm.apiKey"
                autocomplete="off"
                placeholder="仅用于本次真实验证"
                type="password"
            /></label>
          </div>
          <div v-if="providerStep === 'select-model'" class="custom-model-row">
            <label
              ><span>已发现模型：选择后执行最小能力测试</span
              ><select
                :value="providerForm.customModelId"
                @change="selectDiscoveredModel"
              >
                <option value="">选择一个模型</option>
                <option
                  v-for="modelId in discoveredModels"
                  :key="modelId"
                  :value="modelId"
                >
                  {{ modelId }}
                </option>
              </select></label
            ><button
              class="outline-button"
              type="button"
              :disabled="!providerForm.customModelId"
              @click="verifyCustomModel()"
            >
              最小能力测试
            </button>
          </div>
          <div
            v-else-if="providerStep === 'custom-id'"
            class="custom-model-row"
          >
            <label
              ><span>服务不支持模型列表：自定义模型 ID</span
              ><input
                v-model="providerForm.customModelId"
                placeholder="例如 my-company-chat-01"
                type="text" /></label
            ><button
              class="outline-button"
              type="button"
              @click="verifyCustomModel()"
            >
              最小能力测试
            </button>
          </div>
          <div v-else class="provider-actions">
            <button
              class="outline-button"
              type="button"
              :disabled="providerStep === 'discovering'"
              @click="discoverProvider"
            >
              {{
                providerStep === "discovering"
                  ? "正在请求真实服务…"
                  : "发现模型"
              }}
            </button>
            <p v-if="providerStep === 'verified'" class="verify-ok">
              已通过最小真实能力测试。
            </p>
          </div>
          <div
            v-if="
              providerStep === 'verified' && providerForm.protocol !== 'minimax'
            "
            class="provider-policy-fields"
          >
            <label
              ><span>语言模型最大上下文</span
              ><select v-model.number="providerForm.maxContextTokens">
                <option
                  v-for="value in modelContextOptions"
                  :key="value"
                  :value="value"
                >
                  {{ formatTokens(value) }}
                </option>
              </select></label
            >
            <div>
              <span>允许思考等级</span>
              <div class="reasoning-options">
                <button
                  v-for="level in reasoningOptions"
                  :key="level"
                  type="button"
                  :class="{
                    active: providerForm.allowedReasoning.includes(level),
                  }"
                  @click="
                    providerForm.allowedReasoning =
                      providerForm.allowedReasoning.includes(level)
                        ? providerForm.allowedReasoning.filter(
                            (item) => item !== level,
                          )
                        : [...providerForm.allowedReasoning, level]
                  "
                >
                  {{ level }}
                </button>
              </div>
            </div>
          </div>
          <button
            v-if="providerStep === 'verified'"
            class="save-button add-model-button"
            type="button"
            @click="addVerifiedProvider"
          >
            验证成功，加入模型池
          </button>
    </ModelAccordion>

    <ModelAccordion v-model:open="expandedModelSections.config" index="02" title="模型配置" hint="运行角色">
          <div class="preview-assignment-grid">
            <ModelAssignmentField v-for="role in llmAssignmentSlots" :key="role" v-model="modelAssignments[role]" :label="roleLabels[role].title" :disabled="restartState === 'restarting'" :hint="modelPolicySummary(selectedModel(role))" :status="modelStatusLabel(selectedModel(role))" :options="[{ value: '', label: '选择一个已验证模型' }, ...availableLlmModels.map(item => ({ value: item.id, label: `${item.provider} · ${item.name}（${item.modelId}）` }))]" />
          </div>
          <div class="info-callout"><span>i</span><p>TTS（语音合成）沿用已保存的在线服务。主语音模型须支持 off（关闭思考）和 low（低强度）；影子代理使用所选模型支持的思考等级；状态汇报固定关闭思考。应用配置时确认受影响的服务并按需重启。</p></div>
    </ModelAccordion>

    <ModelAccordion v-model:open="expandedModelSections.pool" index="03" title="模型池" :hint="`${apiModelPool.length} 个条目`">
          <div class="pool-intro">
            <div>
              <p class="section-label">模型池</p>
              <h3>已同步模型条目</h3>
              <p>运行时继承模型和本地模型只读；隐藏不会删除本地文件。</p>
            </div>
            <span class="pool-total"
              ><strong>{{ apiModelPool.length }}</strong
              ><small>个 API 条目</small></span
            >
          </div>
          <section class="pool-subsection">
            <div class="pool-subsection-heading">
              <div>
                <span class="pool-section-mark llm-mark" title="LLM（大语言模型）">语言</span>
                <div>
                  <h4>文字模型</h4>
                  <p>模型级上下文和思考策略</p>
                </div>
              </div>
              <span
                >{{
                  apiModelPool.filter((item) => item.capability === "llm")
                    .length
                }}
                个</span
              >
            </div>
            <div
              v-if="apiModelPool.some((item) => item.capability === 'llm')"
              class="pool-entry-list"
            >
            <ModelPoolEntry v-for="item in apiModelPool.filter(entry => entry.capability === 'llm')" :key="item.id" :class="{ editing: editingModelId === item.id, 'delete-pending': deleteCandidateId === item.id }" :name="item.name" :identifier="item.modelId + '（模型标识） · ' + originLabel(item.origin)" badge="语言" :status="modelStatusLabel(item)" :status-class="item.status" :facts="[{ label: '服务商', value: item.provider }, { label: '用途', value: 'LLM（大语言模型）' }, { label: '上下文容量', value: formatContext(item.maxContextTokens) }, { label: '允许思考', value: reasoningSummary(item) }]">
                <template #actions><template v-if="isEditableApiModel(item)">
                  <button
                    type="button"
                    @click="
                      editingModelId === item.id
                        ? (editingModelId = null)
                        : startEditingModel(item)
                    "
                  >
                    {{ editingModelId === item.id ? "收起" : "编辑" }}</button
                  ><button
                    class="danger-action"
                    type="button"
                    @click="requestDeleteModel(item)"
                  >
                    删除
                  </button>
                </template><span v-else class="readonly-tag">运行时导入</span></template>
                <div v-if="editingModelId === item.id" class="model-edit-panel">
                  <div class="edit-fields">
                    <label
                      ><span>显示名称</span
                      ><input
                        v-model="modelEditDraft.name"
                        type="text" /></label
                    ><label
                      ><span>最大上下文</span
                      ><select v-model.number="modelEditDraft.maxContextTokens">
                        <option
                          v-for="value in modelContextOptions"
                          :key="value"
                          :value="value"
                        >
                          {{ formatTokens(value) }}
                        </option>
                      </select></label
                    >
                    <fieldset class="reasoning-fieldset">
                      <legend>允许的思考等级</legend>
                      <div class="reasoning-chip-list">
                        <button
                          v-for="level in reasoningOptions"
                          :key="level"
                          type="button"
                          :class="{
                            selected:
                              modelEditDraft.allowedReasoning.includes(level),
                          }"
                          @click="toggleReasoningLevel(level)"
                        >
                          <strong>{{ level }}</strong
                          ><small>{{
                            level === "off" ? "关闭思考" : "可用策略"
                          }}</small>
                        </button>
                      </div>
                    </fieldset>
                  </div>
                  <div class="edit-panel-footer">
                    <span>稳定模型 ID、供应商 ID 不通过普通编辑接口修改。</span>
                    <div>
                      <button
                        class="ghost-action"
                        type="button"
                        @click="editingModelId = null"
                      >
                        取消</button
                      ><button
                        class="save-button compact-save"
                        type="button"
                        @click="saveModelEdit(item)"
                      >
                        保存修改
                      </button>
                    </div>
                  </div>
                </div>
                <div
                  v-if="deleteCandidateId === item.id"
                  class="inline-delete-confirm"
                >
                  <span>确认删除这个未分配的产品托管模型？</span>
                  <div>
                    <button
                      class="ghost-action"
                      type="button"
                      @click="deleteCandidateId = null"
                    >
                      取消</button
                    ><button
                      class="confirm-delete-action"
                      type="button"
                      @click="confirmDeleteModel(item)"
                    >
                      确认删除
                    </button>
                  </div>
                </div>
            </ModelPoolEntry>
            </div>
            <div v-else class="pool-empty">暂无语言模型条目。</div>
          </section>
          <section class="pool-subsection">
            <div class="pool-subsection-heading">
              <div>
                <span class="pool-section-mark tts-mark" title="TTS（语音合成）">语音</span>
                <div>
                  <h4>语音模型</h4>
                  <p>在线语音模型清单；当前配置固定</p>
                </div>
              </div>
              <span
                >{{
                  apiModelPool.filter((item) => item.capability === "tts")
                    .length
                }}
                个</span
              >
            </div>
            <div
              v-if="apiModelPool.some((item) => item.capability === 'tts')"
              class="pool-entry-list"
            >
            <ModelPoolEntry v-for="item in apiModelPool.filter(entry => entry.capability === 'tts')" :key="item.id" :name="item.name" :identifier="item.modelId + '（模型标识） · ' + originLabel(item.origin)" badge="语音" :status="modelStatusLabel(item)" :status-class="item.status" :facts="[{ label: '服务商', value: item.provider }, { label: '用途', value: 'TTS（语音合成）' }, { label: '语音配置', value: registry.tts_profiles.find(profile => profile.model_id === item.id)?.name || '未提供' }]">
              <template #actions><span class="readonly-tag">{{ item.origin === 'product_managed' ? '产品托管' : '运行时导入' }}</span></template>
            </ModelPoolEntry>
            </div>
            <div v-else class="pool-empty">暂无语音合成模型条目。</div>
          </section>
          <details
            v-if="hiddenApiModelPool.length"
            class="local-models-disclosure"
          >
            <summary>
              <span
                ><strong>已隐藏 API 条目</strong
                ><small>可恢复，但不会自动重新应用</small></span
              ><em>{{ hiddenApiModelPool.length }} 个</em>
            </summary>
            <div class="local-model-list">
              <article
                v-for="item in hiddenApiModelPool"
                :key="item.id"
                class="local-model-row hidden-row"
              >
                <span class="provider-badge managed">API</span>
                <div>
                  <strong>{{ item.provider }} · {{ item.name }}</strong
                  ><small>{{ item.capability }} · {{ item.modelId }}</small>
                </div>
                <button type="button" @click="toggleHidden(item)">恢复</button>
              </article>
            </div>
          </details>
          <details class="local-models-disclosure">
            <summary>
              <span
                ><strong>本地模型清单</strong
                ><small>受管目录扫描结果，不参与 API 分配</small></span
              ><em
                >{{ visibleLocalPool.length }} 个可用 ·
                {{ hiddenPool.length }} 个已隐藏</em
              >
            </summary>
            <div class="local-model-list">
              <article
                v-for="item in [...visibleLocalPool, ...hiddenLocalPool]"
                :key="item.id"
                class="local-model-row"
                :class="{ 'hidden-row': item.hidden }"
              >
                <span class="provider-badge managed">本</span>
                <div>
                  <strong>{{ item.name }}</strong
                  ><small
                    >{{ item.capability }} ·
                    {{ item.path || "受管目录" }}</small
                  >
                </div>
                <button type="button" @click="toggleHidden(item)">
                  {{ item.hidden ? "恢复" : "隐藏" }}
                </button>
              </article>
            </div>
          </details>
    </ModelAccordion>
  </div>
  <footer class="model-settings-footer">
    <div class="restart-copy">
      <span v-if="restartState === 'restarting'">正在重启受影响服务…</span
      ><span v-else-if="restartState === 'complete'">✓ 新模型配置已生效</span
      ><span v-else-if="restartState === 'error'"
        >应用失败，旧运行配置已保留</span
      ><span v-else>应用配置会中止聆听/播报并受控重建相关服务。</span>
    </div>
    <button
      class="save-button"
      type="button"
      :disabled="restartState === 'restarting'"
      @click="openApplyDialog"
    >
      应用模型配置
    </button>
  </footer>
</template>
