<script setup lang="ts">
import { useProductContext } from "../product-context";
import SettingsPageHeader from '../../interface/SettingsPageHeader.vue';
const {
  settings,
  saving,
  speakerForm,
  speakerStatus,
  speakerTestResult,
  speakerEnrollment,
  saveSpeaker,
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
  speakerSegmentLabel,
  stopSpeakerRecording,
} = useProductContext();
</script>
<template>
  <SettingsPageHeader section="speaker" />
  <p class="instruction-hint">
      声纹只在本地旁路观察和路由，不会阻塞音频采集、VAD（语音活动检测）、ASR（自动语音识别）的局部或最终转写；普通请求在未注册、超时或出错时保守放行，主人打断则必须获得确认。它不是付款、删除、门锁等高风险操作的身份认证。
  </p>

  <section class="speaker-status-card">
    <div>
      <span class="section-label">STATUS</span
      ><strong>{{ speakerServiceLabel }}</strong
      ><small
        >{{ String(speakerStatus.model_id || "WeSpeaker（中文声纹模型）") }} · CPU（中央处理器）
        独立 Worker</small
      ><small v-if="speakerStatus.last_error" class="speaker-last-error"
        >最近错误：{{ String(speakerStatus.last_error) }}</small
      >
    </div>
    <div class="speaker-status-detail">
      <span>{{ speakerHasProfile ? "已注册主人声纹" : "尚未注册声纹" }}</span
      ><span v-if="speakerStatus.worker_connected === true"
        >Worker（工作进程）已连接</span
      ><span v-else>Worker（工作进程）未连接</span
      ><span v-if="speakerRollingSummary">{{ speakerRollingSummary }}</span>
    </div>
  </section>

  <div v-if="speakerNeedsReenrollment" class="info-callout">
    <span>!</span>
    <p>
      <strong>需要重新注册声纹。</strong
      >{{
        speakerStaleReason
      }}
      特征管线更换后，旧模板与新的分数不再处于同一坐标，系统已停止用它们判定身份（不确定状态一律放行）。请重新录制三段。
    </p>
  </div>

  <div class="speaker-settings-grid">
    <label class="form-field"
      ><span>声纹模式</span
      ><select v-model="speakerForm.mode">
        <option value="off">关闭</option>
        <option value="observe">仅标记</option>
        <option value="filter" :disabled="!speakerFilterAllowed">
          拦截明确非主人
        </option></select
      ><small v-if="!speakerFilterAllowed"
        >注册主人声纹且
        Worker（工作进程）就绪后才能启用拦截；不确定状态始终放行。</small
      ><small v-else
        >仅标记只更新说话人状态；拦截会将明确非主人的转写转入环境记录。</small
      ></label
    >
    <label class="speaker-barge-toggle"
      ><input
        v-model="speakerForm.owner_only_barge_in"
        type="checkbox"
        :disabled="!speakerHasProfile && !speakerForm.owner_only_barge_in"
      /><span
        ><strong>主人打断</strong
        ><small
          >只在当前打断候选确认是主人后停止播放；最多等待约 2.5
          秒。关闭声纹模式时也可单独启用。</small
        ></span
      ></label
    >
    <div
      v-if="speakerForm.owner_only_barge_in"
      class="speaker-barge-status"
      :class="{
        effective: speakerStatus.owner_only_barge_in_effective === true,
      }"
    >
      <strong>{{
        speakerStatus.owner_only_barge_in_effective === true
          ? "主人打断已生效"
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
    </details>
  </div>
  <div class="info-callout">
    <span>!</span>
    <p>
      阈值只是当前测试基线，不代表安全等级。阈值越宽松越容易连续对话，也越可能误认；第一版不提供防伪和变声检测（anti-spoofing：不可用）。
    </p>
  </div>

  <section class="speaker-enrollment-card" aria-labelledby="speaker-enrollment-title">
    <div class="speaker-card-heading">
      <div class="speaker-card-copy"><h3 id="speaker-enrollment-title">主人声纹模板</h3><p>按日常语速录制三段，每段 5–10 秒，保持设备与距离一致。</p></div>
      <div class="speaker-card-actions">
        <button v-if="!speakerEnrollment && !speakerTestRecording" class="outline-button speaker-compact-button" type="button" @click="startSpeakerEnrollment">开始录制</button>
        <button v-if="!speakerEnrollment && !speakerTestRecording" class="outline-button speaker-compact-button" type="button" :disabled="!speakerHasProfile" @click="beginSpeakerTest">现场测试</button>
        <button v-if="speakerTestRecording && speakerServerCapture" class="outline-button speaker-compact-button speaker-stop-button" type="button" disabled>正在录制（{{ speakerRecordingSeconds }} 秒）</button>
        <button v-else-if="speakerTestRecording" class="outline-button speaker-compact-button speaker-stop-button" type="button" @click="stopSpeakerRecording">停止测试（{{ speakerRecordingSeconds }} 秒）</button>
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
    <div v-if="speakerEnrollment" class="speaker-recording-flow">
      <p v-if="speakerEnrollment.guidance" class="speaker-guidance">{{ speakerEnrollment.guidance }}</p>
      <div class="speaker-segment-list">
        <div v-for="(segment, index) in ['第 1 段', '第 2 段', '第 3 段']" :key="segment" class="speaker-segment-row" :class="{ completed: speakerEnrollment.segments.includes(index), recording: speakerRecordingSegment === index }" :data-segment="index">
          <span class="speaker-segment-index" aria-hidden="true">0{{ index + 1 }}</span>
          <div class="speaker-segment-copy">
            <div class="speaker-segment-heading"><strong>{{ segment }}</strong><span class="speaker-segment-state" role="status">{{ speakerRecordingSegment === index ? '录制中 · ' + speakerRecordingSeconds + ' 秒' : speakerEnrollment.segments.includes(index) ? '已完成' : '待录制' }}</span></div>
            <p v-if="speakerPrompt(index)" class="speaker-prompt">{{ speakerPrompt(index) }}</p>
            <label v-if="index === 2" class="speaker-template-label"><span>模板标签</span><input :id="'speaker-label-' + index" type="text" value="自定义" aria-label="自定义模板标签" :disabled="speakerRecordingSegment !== null" /></label>
          </div>
          <button v-if="speakerRecordingSegment === index && speakerServerCapture" class="outline-button speaker-compact-button speaker-segment-action speaker-stop-button" type="button" disabled>正在录制</button>
          <button v-else-if="speakerRecordingSegment === index" class="outline-button speaker-compact-button speaker-segment-action speaker-stop-button" type="button" @click="stopSpeakerRecording">停止录制</button>
          <button v-else class="outline-button speaker-compact-button speaker-segment-action" type="button" :disabled="speakerRecordingSegment !== null" @click="beginSpeakerRecording(index, speakerSegmentLabel(index, segment))">{{ speakerEnrollment.segments.includes(index) ? '重新录制' : '开始录制' }}</button>
        </div>
      </div>
      <footer class="speaker-flow-footer">
        <div class="speaker-flow-progress"><span role="status">已完成 {{ speakerEnrollment.segments.length }} / 3 段</span><div class="speaker-progress-marks" aria-hidden="true"><i v-for="index in 3" :key="index" :class="{ completed: index <= speakerEnrollment.segments.length }"></i></div></div>
        <div class="speaker-flow-actions"><button class="outline-button speaker-compact-button" type="button" @click="cancelSpeakerEnrollment">取消录制</button><button class="save-button speaker-compact-button speaker-submit-button" type="button" :disabled="!speakerEnrollmentComplete || saving" @click="commitSpeakerEnrollment">保存声纹</button></div>
      </footer>
    </div>
    <div v-else-if="speakerTemplates.length" class="speaker-template-list">
      <article v-for="template in speakerTemplates" :key="String(template.template_id)" class="speaker-template-row">
        <div class="speaker-template-copy"><strong :title="String(template.label || '自定义')">{{ String(template.label || '自定义') }}</strong><time>{{ String(template.created_at || '').replace('T', ' ').slice(0, 16) }}</time></div>
        <span class="speaker-template-state" :class="{ active: template.include_in_centroid !== false }">{{ template.include_in_centroid === false ? '未纳入主中心' : '已纳入主中心' }}</span>
        <div class="speaker-template-actions"><button class="outline-button speaker-compact-button" type="button" @click="setSpeakerTemplateCentroid(String(template.template_id), template.include_in_centroid === false)">{{ template.include_in_centroid === false ? '纳入主中心' : '移出主中心' }}</button><button class="outline-button danger-action speaker-compact-button" type="button" @click="deleteSpeakerTemplate(String(template.template_id))">删除</button></div>
      </article>
    </div>
    <div v-else class="pool-empty">还没有模板，请点击“开始录制”。</div>
  </section>

  <section class="speaker-ambient-card">
    <div class="speaker-card-heading">
      <div>
        <p class="section-label">AMBIENT CONTEXT</p>
        <h3>当前会话环境记录</h3>
        <p>
          筛选模式拦截的非本人文本只保存在内存中，不跨会话、不保存音频，也不会自动进入主
          Agent。
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
      {{ saving ? "正在保存…" : "保存声纹设置" }}
    </button>
  </footer>
</template>
