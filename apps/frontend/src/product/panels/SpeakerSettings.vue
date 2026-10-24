<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import SpeakerRecordingFlow from '../../interface/SpeakerRecordingFlow.vue';
import { SPEAKER_FIELDS, speakerControlsError } from '../speaker-policy';
import { computed, ref, watch } from 'vue';
const {
  current,
  settings,
  saving,
  speakerForm,
  speakerStatus,
  speakerTestResult,
  speakerEnrollment,
  saveSpeaker,
  saveSpeakerControls,
  startSpeakerEnrollment,
  commitSpeakerEnrollment,
  cancelSpeakerEnrollment,
  deleteSpeakerTemplate,
  setSpeakerTemplateCentroid,
  refreshAmbientContext,
  clearAmbientContext,
  formatTime,
  speakerRecordingSegment,
  speakerTestRecording,
  speakerRecordingSeconds,
  speakerServerCapture,
  speakerServiceLabel,
  speakerRollingSummary,
  speakerTemplates,
  ambientRecords,
  speakerHasProfile,
  speakerNeedsReenrollment,
  speakerStaleReason,
  speakerFilterAllowed,
  speakerEnrollmentComplete,
  speakerStateLabel,
  speakerTestReasonLabel,
  speakerTestWindowSummary,
  restoreSpeakerDefaults,
  beginSpeakerRecording,
  beginSpeakerTest,
  speakerPrompt,
  stopSpeakerRecording,
  cancelActiveSpeakerCapture,
} = useProductContext();
const sessionMetrics = computed(() => {
  const value = current.value.speaker_session ?? speakerStatus.value.session;
  return value && typeof value === 'object' ? value as Record<string, unknown> : {};
});
const recordingExpanded = ref(false); // recordingExpanded（录制面板）：收起时保留已完成片段。
const admissionLabels: Record<string, string> = {
  collecting: '首段积累中', inferring: '首段核对中', held: '首段非主人，暂缓提交',
  relaxed: '本轮主人已确认，持续放行', unconfirmed: '身份未确认，保留全文放行', closed: '本轮已结束',
  observing: '仅观察，普通输入不拦截',
};
const controlsError = computed(() => speakerControlsError(speakerForm)); // controlsError（四项草稿错误）。
watch(speakerEnrollment, value => { if (!value) recordingExpanded.value = false; });
async function toggleRecording() {
  if (recordingExpanded.value) { cancelActiveSpeakerCapture(); recordingExpanded.value = false; return; }
  if (!speakerEnrollment.value) await startSpeakerEnrollment();
  recordingExpanded.value = !!speakerEnrollment.value;
}
</script>
<template>
  <SettingsPageHeader section="speaker" />
  <p class="instruction-hint" role="status">{{ speakerServiceLabel }} · {{ speakerHasProfile ? '已注册主人声纹' : '尚未注册声纹' }} · {{ speakerStatus.worker_connected === true ? '工作进程已连接' : '工作进程未连接' }}<template v-if="speakerRollingSummary"> · {{ speakerRollingSummary }}</template></p>
  <p v-if="speakerStatus.last_error" class="instruction-error" role="alert">最近错误：{{ String(speakerStatus.last_error) }}</p>

  <div v-if="speakerNeedsReenrollment" class="info-callout">
    <span>!</span>
    <p>
      <strong>需要注册CAM++声纹。</strong
      >{{
        speakerStaleReason
      }}
      此版本使用CAM++，升级会移除旧WeSpeaker模型与档案。缺少有效CAM++档案时以观察状态运行，录制三段后恢复已保存的筛选及主人打断偏好。
    </p>
  </div>

  <div class="settings-stack">
    <label class="form-field"
      ><span>运行模式</span
      ><select v-model="speakerForm.mode" :disabled="saving" @change="saveSpeakerControls">
        <option value="off">关闭</option>
        <option value="observe">仅标记</option>
        <option value="filter" :disabled="!speakerFilterAllowed">
          拦截 · 首段核对后整轮放行
        </option></select
      ><small v-if="!speakerFilterAllowed"
        >注册主人声纹且
        Worker（工作进程）就绪后才能启用拦截。</small
      ><small v-else
        >先积累约1.2秒音频核对；达到当次阈值为主人，低于则暂缓。暂缓轮次连续三次达到正常主人阈值可恢复全部原文；无有效分数时保留全文降级，放行后异声只作标注。</small
      ></label
    >
    <label class="switch-row"
      ><div><strong>仅主人可打断</strong><span>确认是主人后停止播放，其余情况保留播放；身份判断最多等待约 2.5 秒。关闭声纹模式时也可单独启用。</span></div><input
        v-model="speakerForm.owner_only_barge_in"
        type="checkbox"
        role="switch"
        :disabled="saving || (!speakerHasProfile && !speakerForm.owner_only_barge_in)"
        @change="saveSpeakerControls"
      /><i aria-hidden="true"></i></label
    >
    <div
      v-if="speakerForm.owner_only_barge_in"
      class="speaker-barge-status"
      :class="{
        effective: speakerStatus.owner_only_barge_in_ready === true || speakerStatus.owner_only_barge_in_effective === true,
      }"
    >
      <strong>{{
        saving ? "正在应用主人打断设置…" : speakerStatus.owner_only_barge_in_effective === true
          ? "主人打断已生效"
          : speakerStatus.owner_only_barge_in_ready === true ? "主人打断已启用"
          : "主人打断当前不可用"
      }}</strong
      ><small v-if="speakerStatus.owner_only_barge_in_effective !== true">{{
        String(
          speakerStatus.owner_only_barge_in_reason ||
            (!speakerHasProfile
              ? "请先注册主人声纹。"
              : "等待声纹服务与回声消除就绪。"),
        )
      }}</small>
    </div>
  </div>
  <p v-if="current.admission_phase" class="instruction-hint" role="status">
    {{ admissionLabels[current.admission_phase] || current.admission_phase }}。
    <template v-if="current.speaker_summary?.foreign_voice_suspected">本轮存在疑似外来声纹或识别波动，文字保持完整。</template>
  </p>
  <section class="speaker-controls" aria-label="声纹参数">
    <div class="speaker-thresholds">
      <label v-for="field in SPEAKER_FIELDS" :key="field.key" class="form-field">
        <span>{{ field.label }}<b>{{ Number(speakerForm[field.key]).toFixed(2) }}</b></span>
        <input v-model.number="speakerForm[field.key]" type="range" :min="field.min" :max="field.max"
          :step="field.step" :aria-label="field.label" />
        <small>{{ field.hint }}</small>
      </label>
    </div>
    <p class="instruction-hint">轮内阈值：{{ (speakerForm.confirm_threshold - speakerForm.intra_turn_relaxation).toFixed(2) }}；跨轮阈值：{{ (speakerForm.confirm_threshold - speakerForm.continuity_relaxation).toFixed(2) }}。两种放宽不叠加，保存时校验阈值不小于0。</p>
    <p class="instruction-hint">临时声纹：{{ sessionMetrics.calibration_status === 'ready' ? '已建立' : sessionMetrics.calibration_status === 'collecting' ? '等待校准完成' : '尚未建立' }}；最近一次评分实际权重：{{ Number(sessionMetrics.effective_session_weight ?? 0).toFixed(2) }}。未建立时仅使用永久模板。</p>
    <p v-if="controlsError" class="instruction-error" role="alert">{{ controlsError }}</p>
    <button class="ghost-action" type="button" :disabled="saving" @click="restoreSpeakerDefaults">恢复四项默认值</button>
  </section>

  <section class="speaker-enrollment-card" aria-labelledby="speaker-enrollment-title">
    <div class="speaker-card-heading">
      <div class="speaker-card-copy"><h3 id="speaker-enrollment-title">主人声纹模板</h3><p>按日常语速录制三段，每段 5–10 秒，保持设备与距离一致。</p></div>
      <div class="speaker-card-actions">
        <button v-if="!speakerTestRecording" class="outline-button speaker-compact-button speaker-recording-toggle" type="button" :aria-expanded="recordingExpanded" :disabled="saving" @click="toggleRecording">{{ recordingExpanded ? '收起录制' : '开始录制' }}</button>
        <button v-if="!speakerTestRecording" class="outline-button speaker-compact-button" type="button" :disabled="recordingExpanded || !speakerHasProfile || saving" @click="beginSpeakerTest">现场测试</button>
        <template v-else><button class="outline-button speaker-compact-button" type="button" @click="cancelActiveSpeakerCapture">取消测试</button><button class="outline-button speaker-compact-button speaker-stop-button" type="button" :disabled="speakerRecordingSeconds < 5" @click="stopSpeakerRecording">{{ speakerRecordingSeconds < 5 ? '录满 5 秒后可停止' : '停止测试' }}（{{ speakerRecordingSeconds }} 秒）</button></template>
      </div>
    </div>
    <div v-if="speakerTestResult" class="speaker-test-result">
      <strong>{{ speakerStateLabel(speakerTestResult.state) }}</strong
      ><span v-if="typeof speakerTestResult.similarity === 'number'"
        >相似度 {{ Number(speakerTestResult.similarity).toFixed(2) }}</span
      ><span v-if="typeof speakerTestResult.threshold_used === 'number'"
        >判定阈值
        {{ Number(speakerTestResult.threshold_used).toFixed(2) }}</span
      ><small v-if="speakerTestReasonLabel(speakerTestResult)">{{
        speakerTestReasonLabel(speakerTestResult)
      }}</small
      ><small>{{ speakerTestWindowSummary(speakerTestResult) }}</small
      ><small
        >现场测试采用可调上下文与永久模板评分；实时会话还会叠加临时声纹和句内迟滞，本次测试不会改变运行模式。</small
      >
    </div>
    <SpeakerRecordingFlow v-if="recordingExpanded && speakerEnrollment" :completed="speakerEnrollment.segments.length" :total="3">
      <p v-if="speakerEnrollment.guidance" class="speaker-guidance">{{ speakerEnrollment.guidance }}</p>
      <div class="speaker-segment-list">
        <div v-for="(segment, index) in ['第 1 段', '第 2 段', '第 3 段']" :key="segment" class="speaker-segment-row" :class="{ completed: speakerEnrollment.segments.includes(index), recording: speakerRecordingSegment === index }" :data-segment="index">
          <span class="speaker-segment-index" aria-hidden="true">0{{ index + 1 }}</span>
          <div class="speaker-segment-copy">
            <div class="speaker-segment-heading"><strong>{{ segment }}</strong><span class="speaker-segment-state" role="status">{{ speakerRecordingSegment === index ? '录制中 · ' + speakerRecordingSeconds + ' 秒' : speakerEnrollment.segments.includes(index) ? '已完成' : '待录制' }}</span></div>
            <p v-if="speakerPrompt(index)" class="speaker-prompt">{{ speakerPrompt(index) }}</p>
          </div>
          <button v-if="speakerRecordingSegment === index" class="outline-button speaker-compact-button speaker-segment-action speaker-stop-button" type="button" :disabled="speakerRecordingSeconds < 5" @click="stopSpeakerRecording">{{ speakerRecordingSeconds < 5 ? '录满 5 秒后可停止' : '停止录制' }}</button>
          <button v-else class="outline-button speaker-compact-button speaker-segment-action" type="button" :disabled="speakerRecordingSegment !== null" @click="beginSpeakerRecording(index, segment)">{{ speakerEnrollment.segments.includes(index) ? '重新录制' : '开始录制' }}</button>
        </div>
      </div>
      <template #actions><button class="outline-button speaker-compact-button" type="button" @click="cancelSpeakerEnrollment">取消录制</button><button class="save-button speaker-compact-button speaker-submit-button" type="button" :disabled="!speakerEnrollmentComplete || saving" @click="commitSpeakerEnrollment">保存声纹</button></template>
    </SpeakerRecordingFlow>
    <details v-else-if="speakerTemplates.length" class="speaker-template-details">
      <summary class="speaker-template-summary">
        <span>已保存的语音段落</span>
        <span class="speaker-template-count">共 {{ speakerTemplates.length }} 段</span>
      </summary>
      <div class="speaker-template-list">
        <article v-for="template in speakerTemplates" :key="String(template.template_id)" class="speaker-template-row">
          <div class="speaker-template-copy"><strong :title="String(template.label || '自定义')">{{ String(template.label || '自定义') }}</strong><time>{{ String(template.created_at || '').replace('T', ' ').slice(0, 16) }}</time></div>
          <span class="speaker-template-state" :class="{ active: template.include_in_centroid !== false }">{{ template.include_in_centroid === false ? '未纳入主中心' : '已纳入主中心' }}</span>
          <div class="speaker-template-actions"><button class="outline-button speaker-compact-button" type="button" :disabled="saving || speakerTestRecording" @click="setSpeakerTemplateCentroid(String(template.template_id), template.include_in_centroid === false)">{{ template.include_in_centroid === false ? '纳入主中心' : '移出主中心' }}</button><button class="outline-button danger-action speaker-compact-button" type="button" :disabled="saving || speakerTestRecording" @click="deleteSpeakerTemplate(String(template.template_id))">删除</button></div>
        </article>
      </div>
    </details>
    <div v-else class="pool-empty">还没有模板，请点击“开始录制”。</div>
  </section>

  <section class="speaker-ambient-card">
    <div class="speaker-card-heading">
      <div>
        <p class="section-label">AMBIENT CONTEXT（环境上下文）</p>
        <h3>当前会话环境记录</h3>
        <p>
          筛选模式拦截的非本人文本只保存在内存中，不跨会话、不保存音频，也不会自动进入主
          Agent（代理）。
        </p>
      </div>
      <div>
        <button
          class="ghost-action"
          type="button"
          @click="refreshAmbientContext"
        >
          刷新</button
        ><button
          class="danger-action"
          type="button"
          :disabled="!ambientRecords.length"
          @click="clearAmbientContext"
        >
          全部清空
        </button>
      </div>
    </div>
    <div v-if="ambientRecords.length" class="ambient-record-list">
      <article
        v-for="record in ambientRecords"
        :key="String(record.turn_id) + String(record.timestamp)"
      >
        <time>{{ formatTime(String(record.timestamp || "")) }}</time>
        <p>{{ String(record.text || "") }}</p>
        <small
          >{{ speakerStateLabel(record.speaker_state) }} ·
          {{ String(record.reason || "路由到环境记录") }}</small
        >
      </article>
    </div>
    <div v-else class="pool-empty">当前会话还没有被拦截的环境文本。</div>
  </section>

  <footer>
    <button
      class="save-button"
      type="button"
      :disabled="
        saving
      "
      @click="saveSpeaker"
    >
      {{ saving ? "正在保存…" : "保存声纹参数" }}
    </button>
  </footer>
</template>

<style scoped>
/* speaker-template-details（已保存段落折叠区）：默认收起，保留原生键盘展开操作。 */
.speaker-template-details {
  margin-top: 18px;
  border-top: 1px solid var(--line);
}
.speaker-template-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 0 0;
  list-style: none;
  color: var(--text);
  font-size: var(--font-body);
  cursor: pointer;
}
.speaker-template-summary::-webkit-details-marker { display: none; }
.speaker-template-summary::after {
  content: '';
  width: 7px;
  height: 7px;
  margin: 0 3px 4px 0;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(45deg);
  flex: none;
}
.speaker-template-details[open] > .speaker-template-summary::after {
  margin-bottom: -4px;
  transform: rotate(225deg);
}
.speaker-template-summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 4px;
  border-radius: 4px;
}
.speaker-template-count {
  margin-left: auto;
  color: var(--subtle);
  font-size: var(--font-caption);
  white-space: nowrap;
}
</style>
