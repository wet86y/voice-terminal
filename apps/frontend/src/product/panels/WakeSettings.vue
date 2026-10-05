<script setup lang="ts">
import { useProductContext } from "../product-context";
const {
  settings,
  saving,
  wakeForm,
  maxWakeKeywords,
  addWakeKeyword,
  removeWakeKeyword,
  silenceSeconds,
  saveWake,
  saveSilence,
  quietHoursForm,
  quietStatus,
  saveQuietHours,
} = useProductContext();
</script>
<template>
  <header>
    <p class="panel-kicker">唤醒与静默</p>
    <h2>需要时醒来，安静时等待</h2>
    <p>管理唤醒短语、对话等待时间与每日静默时段。</p>
  </header>
  <div class="wake-silence-layout">
    <section class="preference-card">
      <header class="preference-heading">
        <span class="preference-index">01</span>
        <div>
          <h3>唤醒词</h3>
          <p>选择短语与识别灵敏度</p>
        </div>
      </header>
      <div class="settings-stack">
        <label class="switch-row"
          ><div>
            <strong>启用关键词唤醒</strong
            ><span
              >开启后本次语音会话持续监听唤醒词；关闭后直接进入语音识别</span
            >
          </div>
          <input v-model="wakeForm.enabled" type="checkbox" /><i></i
        ></label>
        <div class="form-field">
          <span
            >唤醒词
            <b>{{ wakeForm.keywords.length }} / {{ maxWakeKeywords }}</b></span
          >
          <div
            v-for="(item, index) in wakeForm.keywords"
            :key="`wake-keyword-${index}`"
            class="wake-keyword-row"
          >
            <input
              v-model="wakeForm.keywords[index]"
              type="text"
              autocomplete="off"
              :placeholder="index === 0 ? '例如：七三' : '例如：你好七三'"
            /><button
              type="button"
              class="wake-keyword-remove"
              :disabled="wakeForm.keywords.length <= 1"
              :aria-label="`删除第 ${index + 1} 个唤醒词`"
              @click="removeWakeKeyword(index)"
            >
              ×
            </button>
          </div>
          <button
            type="button"
            class="wake-keyword-add"
            :disabled="wakeForm.keywords.length >= maxWakeKeywords"
            @click="addWakeKeyword"
          >
            + 添加唤醒词
          </button>
          <div class="range-label">
            <small>命中任意一个即唤醒</small
            ><small>最多 {{ maxWakeKeywords }} 个说法</small>
          </div>
          <small
            >建议把称呼说得长一点（如"你好七三""七三同学"），短词在噪声里更容易漏检；支持中文、英文和常见标点，后端会自动生成并验证每个标签。</small
          >
        </div>
        <label class="form-field"
          ><span
            >识别灵敏度 <b>{{ wakeForm.sensitivity }}%</b></span
          ><input
            v-model.number="wakeForm.sensitivity"
            type="range"
            min="1"
            max="99"
          />
          <div class="range-label">
            <small>更严格 · 少误触发</small><small>更容易唤醒</small>
          </div>
          <small
            >灵敏度越高越容易唤醒；实测 75% 以上已是最宽松，低于 50%
            基本无法唤醒，建议保持最高。</small
          ></label
        >
      </div>
      <footer>
        <button
          class="save-button"
          type="button"
          :disabled="saving"
          @click="saveWake"
        >
          {{ saving ? "正在保存…" : "保存唤醒词设置" }}
        </button>
      </footer>
    </section>
    <section class="preference-card">
      <header class="preference-heading">
        <span class="preference-index">02</span>
        <div>
          <h3>静默等待</h3>
          <p>长期无有效输入后回到待机</p>
        </div>
      </header>
      <div class="silence-control">
        <span>最终静默等待</span>
        <div>
          <input
            v-model.number="silenceSeconds"
            min="5"
            max="90"
            type="number"
          /><b>秒</b>
        </div>
        <input
          v-model.number="silenceSeconds"
          min="5"
          max="90"
          step="1"
          type="range"
        />
        <div class="silence-scale">
          <small>5 秒</small><small>默认 30 秒</small><small>90 秒</small>
        </div>
      </div>
      <div class="info-callout">
        <span>i</span>
        <p>
          SmartTurn（语音结束判断）的短语结束阈值：{{
            settings.silence.smart_turn_hard_silence_ms
          }}
          毫秒（只读）。保存后，新的最终静默窗口将在下一次会话生效。
        </p>
      </div>
      <footer>
        <button
          class="save-button"
          type="button"
          :disabled="saving"
          @click="saveSilence"
        >
          {{ saving ? "正在保存…" : "保存最终静默设置" }}
        </button>
      </footer>
    </section>
    <section class="preference-card">
      <header class="preference-heading">
        <span class="preference-index">03</span>
        <div>
          <h3>每日静默</h3>
          <p>按你的作息减少主动发声</p>
        </div>
      </header>
      <section class="silence-control">
        <h3>被动静默时间</h3>
        <strong>{{
          quietStatus?.passive_quiet
            ? "当前处于被动静默"
            : quietStatus?.user_interaction
              ? "用户交互已开启，可正常回复"
              : "当前按普通待机策略运行"
        }}</strong>
        <p>
          每天按本地时间暂停待机中的主动播报，后台任务继续。通过
          KWS（关键词唤醒）唤醒后可以正常对话。
        </p>
        <label
          ><input
            v-model="quietHoursForm.enabled"
            type="checkbox"
            :disabled="!settings.wake.enabled || saving"
          />启用被动静默</label
        >
        <label
          >每日开始时间
          <input v-model="quietHoursForm.start" type="time" :disabled="saving"
        /></label>
        <label
          >每日结束时间
          <input v-model="quietHoursForm.end" type="time" :disabled="saving"
        /></label>
        <p v-if="!settings.wake.enabled">
          请先开启 KWS（关键词唤醒），并在下一次会话使其生效。
        </p>
        <p>
          支持跨午夜；开始时间包含、结束时间不包含。关闭后恢复普通运行，未传达内容由后续交接整理承接。
        </p>
        <button
          class="save-button"
          type="button"
          :disabled="saving || quietHoursForm.start === quietHoursForm.end"
          @click="saveQuietHours"
        >
          保存被动静默设置
        </button>
      </section>
    </section>
  </div>
</template>
