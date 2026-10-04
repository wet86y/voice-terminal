<script setup lang="ts">
import { useProductContext } from "../product-context";
const {
  settings,
  saving,
  computeStatus,
  refreshComputeStatus,
  saveComputeSettings,
  computeModelRows,
  formatComputeBytes,
  deviceLabel,
  requestedDeviceLabel,
  computeChangePending,
  hasUnsupportedGpuChoice,
  serviceLabel,
} = useProductContext();
</script>
<template>
  <header>
    <p class="panel-kicker">本地模型运行设备</p>
    <h2>为本地推理选择计算设备</h2>
    <p>
      逐项设置本地模型的运行设备。保存后需要完全重启应用；TTS（语音合成）不在此页管理。
    </p>
  </header>
  <section class="compute-resource-card">
    <div class="compute-resource-grid">
      <div>
        <small>系统可用内存</small
        ><strong>{{
          formatComputeBytes(
            computeStatus?.hardware.system_memory.available_bytes,
          )
        }}</strong
        ><span
          >总量
          {{
            formatComputeBytes(
              computeStatus?.hardware.system_memory.total_bytes,
            )
          }}</span
        >
      </div>
      <div>
        <small>GPU（图形处理器）</small
        ><strong>{{
          computeStatus?.hardware.gpu.available
            ? computeStatus.hardware.gpu.name
            : "未检测到可用 CUDA 显卡"
        }}</strong
        ><span
          >可用显存
          {{ formatComputeBytes(computeStatus?.hardware.gpu.free_bytes) }} /
          {{
            formatComputeBytes(computeStatus?.hardware.gpu.total_bytes)
          }}</span
        >
      </div>
    </div>
    <p>
      显存数字是整张 GPU
      的系统可用量；当前运行库无法准确拆分每个模型的显存占用。GPU
      选项要求对应推理后端已安装。
    </p>
  </section>
  <div v-if="!computeStatus" class="pool-empty">
    正在读取硬件能力与工作进程状态…
  </div>
  <div v-else class="compute-model-list">
    <article
      v-for="model in computeModelRows"
      :key="model.id"
      class="compute-model-card"
    >
      <div class="compute-model-heading">
        <div>
          <strong>{{ model.title }}</strong
          ><small>{{ model.detail }}</small>
        </div>
        <span
          class="entry-status"
          :class="computeStatus.models[model.id].service"
          >{{ serviceLabel(computeStatus.models[model.id].service) }}</span
        >
      </div>
      <div class="compute-model-stats">
        <span
          >当前请求：{{
            requestedDeviceLabel(
              computeStatus.models[model.id].requested_device,
            )
          }}</span
        >
        <span
          >实际设备：{{
            deviceLabel(computeStatus.models[model.id].device)
          }}</span
        >
        <span
          >工作进程常驻内存：{{
            formatComputeBytes(
              computeStatus.models[model.id].process_memory_rss_bytes,
            )
          }}</span
        >
        <span v-if="model.id === 'sensevoice'"
          >内部 FSMN-VAD：{{
            deviceLabel(computeStatus.models.sensevoice.internal_vad_device)
          }}</span
        >
      </div>
      <p
        v-if="computeStatus.models[model.id].load_error"
        class="compute-model-error"
      >
        {{ computeStatus.models[model.id].load_error }}
      </p>
      <p
        v-else-if="!computeStatus.models[model.id].gpu_available"
        class="compute-model-note"
      >
        GPU 不可用：{{
          computeStatus.models[model.id].gpu_unavailable_reason ||
          "对应 GPU 推理后端未就绪。"
        }}
      </p>
      <label class="compute-device-field"
        ><span>保存后的运行偏好</span
        ><select v-model="settings.compute.devices[model.id]" :disabled="saving">
          <option value="auto">
            自动（优先尝试 GPU，失败或显存不足时回退 CPU）
          </option>
          <option value="cpu">仅 CPU（中央处理器）</option>
          <option
            value="cuda"
            :disabled="!computeStatus.models[model.id].gpu_available"
          >
            GPU（CUDA）
          </option>
        </select></label
      >
    </article>
  </div>
  <div v-if="computeChangePending()" class="info-callout">
    <span>i</span>
    <p>
      <strong>设置尚未运行。</strong
      >保存后需重启整个应用；当前会话和工作进程会继续使用现有设备，重启后状态会同步更新。
    </p>
  </div>
  <footer class="compute-settings-footer">
    <button
      class="outline-button"
      type="button"
      :disabled="saving"
      @click="refreshComputeStatus"
    >
      刷新状态</button
    ><button
      class="save-button"
      type="button"
      :disabled="saving || hasUnsupportedGpuChoice()"
      @click="saveComputeSettings"
    >
      {{ saving ? "正在保存…" : "保存设备设置" }}
    </button>
  </footer>
</template>
