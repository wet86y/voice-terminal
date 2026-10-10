<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
import SpeakerRecordingFlow from '../../interface/SpeakerRecordingFlow.vue';
import { ref, watch } from 'vue';
const {
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
const recordingExpanded = ref(false); // recordingExpanded（录制面板）：收起时保留已完成片段。
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
      <strong>需要重新注册声纹。</strong
      >{{
        speakerStaleReason
      }}
      特征管线更换后，旧模板已停止参与身份判断。拦截模式会暂停放行输入，请重新录制三段。
    </p>
  </div>

  <div class="settings-stack">
    <label class="form-field"
      ><span>运行模式</span
      ><select v-model="speakerForm.mode" :disabled="saving" @change="saveSpeakerControls">
        <option value="off">关闭</option>
        <option value="observe">仅标记</option>
        <option value="filter" :disabled="!speakerFilterAllowed">
          拦截 · 仅确认主人时放行
        </option></select
      ><small v-if="!speakerFilterAllowed"
        >注册主人声纹且
        Worker（工作进程）就绪后才能启用拦截。</small
      ><small v-else
        >拦截会等待本段身份判定；只有确认的主人输入进入对话，未知、非主人及服务不可用时的输入进入环境记录。短句可能因人声证据不足被拦截。</small
      ></label
    >
    <label class="switch-row"
      ><div><strong>仅主人可打断</strong><span>身份未知时保留当前播放；确认是主人后停止，最多等待约 2.5 秒。关闭声纹模式时也可单独启用。</span></div><input
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
  <details class="speaker-advanced">
      <summary>高级设置 · 阈值基线</summary>
      <div class="speaker-thresholds">
        <label class="form-field"
          ><span
            >主人确认阈值
            <b>{{ Number(speakerForm.confirm_threshold).toFixed(2) }}</b></span
          ><input
            v-model.number="speakerForm.confirm_threshold"
            type="range"
            min="0.01"
            max="1"
            step="0.01"
          /><small>达到此分数且证据充足才确认是主人。</small></label
        ><label class="form-field"
          ><span
            >非主人确认界限
            <b>{{
              Number(speakerForm.continuity_threshold).toFixed(2)
            }}</b></span
          ><input
            v-model.number="speakerForm.continuity_threshold"
            type="range"
            min="0"
            max="0.99"
            step="0.01"
          /><small
            >低于此分数且证据充足才确认是非主人；两阈值之间保持未知。必须低于主人确认阈值。</small
          ></label
        ><button
          class="ghost-action"
          type="button"
          @click="restoreSpeakerDefaults"
        >
          恢复默认阈值（0.60 / 0.50）
        </button>
      </div>
      <p class="instruction-hint">{{ String(speakerStatus.model_id || 'WeSpeaker') }}（声纹模型）使用独立 CPU（中央处理器）工作进程。声纹只在本地旁路观察和路由，不阻塞 VAD（语音活动检测）及 ASR（自动语音识别）；普通请求在未注册、超时或出错时保守放行，主人打断需要确认。</p>
      <p class="instruction-hint">阈值是当前测试基线，不代表安全等级；不提供 anti-spoofing（防伪与变声检测），不用于付款、删除或门锁等高风险操作的身份认证。</p>
  </details>

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
        >与运行时同一口径（2 秒滑窗 +
        整段聚合），本次测试不会改变运行模式。</small
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
    <div v-else-if="speakerTemplates.length" class="speaker-template-list">
      <article v-for="template in speakerTemplates" :key="String(template.template_id)" class="speaker-template-row">
        <div class="speaker-template-copy"><strong :title="String(template.label || '自定义')">{{ String(template.label || '自定义') }}</strong><time>{{ String(template.created_at || '').replace('T', ' ').slice(0, 16) }}</time></div>
        <span class="speaker-template-state" :class="{ active: template.include_in_centroid !== false }">{{ template.include_in_centroid === false ? '未纳入主中心' : '已纳入主中心' }}</span>
        <div class="speaker-template-actions"><button class="outline-button speaker-compact-button" type="button" :disabled="saving || speakerTestRecording" @click="setSpeakerTemplateCentroid(String(template.template_id), template.include_in_centroid === false)">{{ template.include_in_centroid === false ? '纳入主中心' : '移出主中心' }}</button><button class="outline-button danger-action speaker-compact-button" type="button" :disabled="saving || speakerTestRecording" @click="deleteSpeakerTemplate(String(template.template_id))">删除</button></div>
      </article>
    </div>
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
        saving ||
        Number(speakerForm.continuity_threshold) >=
          Number(speakerForm.confirm_threshold)
      "
      @click="saveSpeaker"
    >
      {{ saving ? "正在保存…" : "保存声纹阈值" }}
    </button>
  </footer>
</template>
