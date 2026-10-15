<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{
  src?: string // src（参考音频播放地址）。
  filename: string // filename（音频文件名称）。
  durationMs: number // durationMs（音频时长，单位为毫秒）。
  busy: boolean // busy（音色任务正在执行）。
  transcribing: boolean // transcribing（正在识别校对文本）。
  active: boolean // active（当前附件拥有播放焦点）。
}>()
const emit = defineEmits<{ play: []; transcribe: []; remove: []; error: [message: string] }>()
const audio = ref<HTMLAudioElement>() // audio（原生音频播放器）。
const playing = ref(false) // playing（实际播放状态）。
const currentTime = ref(0) // currentTime（当前播放秒数）。
const mediaDuration = ref(0) // mediaDuration（播放器读取的实际秒数）。
const duration = computed(() => mediaDuration.value || props.durationMs / 1000)
const progress = computed(() => duration.value ? Math.min(100, currentTime.value / duration.value * 100) : 0)
function timeLabel(seconds: number) {
  const value = Math.max(0, Math.floor(seconds))
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`
}
function pause() { audio.value?.pause(); playing.value = false }
async function togglePlayback() {
  const player = audio.value
  if (!player || !props.src) return
  if (playing.value) { pause(); return }
  emit('play')
  try { await player.play() }
  catch { emit('error', '参考音频暂时无法播放，请重试或重新上传。') }
}
function updateTime() { currentTime.value = audio.value?.currentTime || 0 }
function loaded() {
  const value = audio.value?.duration
  if (value && Number.isFinite(value)) mediaDuration.value = value
}
function seek(event: Event) {
  if (!audio.value || !duration.value) return
  audio.value.currentTime = Number((event.target as HTMLInputElement).value)
  updateTime()
}
watch(() => props.active, active => { if (!active) pause() })
watch(() => props.src, () => { pause(); currentTime.value = 0; mediaDuration.value = 0 })
onBeforeUnmount(pause)
</script>

<template>
  <div class="audio-attachment" role="group" aria-label="参考音频播放器">
    <audio v-if="src" ref="audio" :src="src" preload="metadata" @loadedmetadata="loaded" @durationchange="loaded" @timeupdate="updateTime" @play="playing = true" @pause="playing = false" @ended="playing = false" @error="playing = false; emit('error', '参考音频加载失败，请重试或重新上传。')"></audio>
    <button type="button" class="audio-play" :aria-label="playing ? '暂停参考音频' : '播放参考音频'" :title="playing ? '暂停参考音频' : '播放参考音频'" :disabled="!src" @click="togglePlayback">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path v-if="playing" d="M8 5v14M16 5v14" /><path v-else d="m9 5 10 7-10 7Z" /></svg>
    </button>
    <div class="audio-details">
      <div class="audio-summary"><strong :title="filename">{{ filename }}</strong><button type="button" class="transcribe-action" :disabled="busy" @click="emit('transcribe')"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4h12v9H9l-4 3v-3H4ZM7 7h6M7 10h4" /></svg>{{ transcribing ? '正在识别…' : '开始校对文本' }}</button></div>
      <div class="audio-timeline"><input type="range" aria-label="参考音频播放进度" min="0" :max="duration || 1" step="0.01" :value="currentTime" :disabled="!src || !duration" :style="{ '--audio-progress': `${progress}%` }" :aria-valuetext="`${timeLabel(currentTime)} / ${timeLabel(duration)}`" @input="seek"><small>{{ timeLabel(currentTime) }} / {{ timeLabel(duration) }}</small></div>
    </div>
    <button type="button" class="audio-remove" aria-label="移除参考音频" title="移除参考音频" :disabled="busy" @click="emit('remove')"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6 6 8 8M14 6l-8 8" /></svg></button>
  </div>
</template>

<style scoped>
.audio-attachment { display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: var(--panel-soft); min-width: 0; }
audio { display: none; }
button { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.audio-play { width: 36px; height: 36px; padding: 8px; border-radius: 50%; border-color: color-mix(in srgb, var(--accent) 25%, transparent); background: color-mix(in srgb, var(--accent) 10%, transparent); color: var(--accent); }
.audio-play:hover:enabled { background: color-mix(in srgb, var(--accent) 18%, transparent); }
.audio-details { flex: 1; min-width: 0; display: grid; gap: 7px; }
.audio-summary { display: flex; align-items: center; gap: 8px; min-width: 0; }
.audio-summary strong { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: .75rem; }
.transcribe-action { gap: 4px; border: 0; padding: 0; min-height: 24px; border-radius: 4px; color: var(--accent); font-size: .625rem; }
.transcribe-action svg { width: 15px; height: 15px; }
.audio-timeline { display: flex; align-items: center; gap: 9px; }
.audio-timeline input { appearance: none; min-width: 30px; min-height: 16px; height: 16px; margin: 0; padding: 0; border: 0; border-radius: 0; background: transparent; }
.audio-timeline input::-webkit-slider-runnable-track { height: 3px; border-radius: 3px; background: linear-gradient(to right, var(--accent) var(--audio-progress), var(--line-strong) var(--audio-progress)); }
.audio-timeline input::-webkit-slider-thumb { appearance: none; height: 9px; width: 9px; margin-top: -3px; border-radius: 50%; background: var(--accent); }
.audio-timeline input::-moz-range-track { height: 3px; border-radius: 3px; background: var(--line-strong); }
.audio-timeline input::-moz-range-progress { height: 3px; background: var(--accent); }
.audio-timeline input::-moz-range-thumb { height: 9px; width: 9px; border: 0; border-radius: 50%; background: var(--accent); }
.audio-timeline small { flex-shrink: 0; font-size: .625rem; font-variant-numeric: tabular-nums; }
.audio-remove { width: 26px; height: 26px; border-radius: 8px; padding: 4px; color: var(--muted); }
.audio-remove:hover:enabled { color: var(--danger); border-color: var(--danger); }
@media (max-width: 400px) { .audio-attachment { gap: 8px; padding: 10px; }.audio-summary { flex-wrap: wrap; gap: 2px; }.audio-summary strong { flex-basis: 100%; } }
</style>
