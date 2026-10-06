import { computed, onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import type { DemoRuntimeState } from './demo-data'

// envelope（音量包络）样例：五个频带分别变化，仅用于演示；不会读取麦克风或播放声音。
const microphoneFrames = [
  [0, 0, 0, 0, 0], [.08, .19, .32, .14, .05], [.18, .44, .72, .35, .12],
  [.27, .61, .92, .48, .2], [.12, .32, .54, .24, .08], [.03, .06, .1, .04, .02],
  [.11, .28, .63, .49, .17], [.22, .56, .85, .71, .3], [.14, .35, .61, .43, .21],
  [.05, .12, .25, .16, .07], [0, 0, 0, 0, 0], [.09, .3, .51, .26, .11],
  [.31, .7, .96, .62, .27], [.19, .42, .73, .54, .16], [.08, .21, .38, .19, .06],
  [.02, .04, .06, .03, .01], [.12, .37, .58, .41, .15], [.25, .53, .88, .66, .29],
  [.18, .48, .68, .36, .13], [.04, .1, .2, .12, .04], [0, 0, 0, 0, 0],
]
const playbackFrames = [
  [.03, .07, .12, .08, .02], [.16, .38, .68, .51, .22], [.3, .66, .94, .72, .31],
  [.24, .49, .76, .58, .25], [.08, .16, .28, .2, .07], [.02, .04, .07, .05, .01],
  [.18, .52, .81, .43, .15], [.26, .71, .97, .6, .24], [.11, .31, .59, .4, .17],
  [.04, .09, .18, .1, .03], [0, 0, 0, 0, 0], [.12, .29, .56, .39, .14],
  [.23, .54, .84, .69, .29], [.16, .41, .7, .48, .19], [.05, .13, .22, .15, .05],
  [.01, .03, .04, .02, .01], [.14, .33, .61, .46, .17], [.28, .62, .91, .73, .33],
  [.19, .46, .74, .55, .23], [.08, .21, .39, .27, .1], [.02, .05, .08, .04, .01],
]
const quietLevels = [0, 0, 0, 0, 0]
const phases: Array<{ state: DemoRuntimeState; duration: number }> = [
  { state: 'standby', duration: 450 },
  { state: 'listening', duration: 2800 },
  { state: 'thinking', duration: 850 },
  { state: 'speaking', duration: 3300 },
  { state: 'standby', duration: 1500 },
]
const cycleDuration = phases.reduce((total, phase) => total + phase.duration, 0)

/** useVoiceSessionDemo（会话动效样例）：state（运行状态）、initiallyActive（恢复的会话开关）只影响展示。 */
export function useVoiceSessionDemo(state: Ref<DemoRuntimeState>, initiallyActive = false) {
  const active = ref(initiallyActive || ['listening', 'thinking', 'speaking'].includes(state.value)) // active（会话开关）。
  const elapsed = ref(0) // elapsed（演示累计毫秒数）。
  const phaseElapsed = ref(0) // phaseElapsed（当前状态持续毫秒数）。
  const latencyMs = ref<number | null>(null) // latencyMs（样例首音延迟毫秒数）。
  let automatic = false // automatic（是否自动演示完整对话过程）。
  let mounted = false // mounted（组件是否已挂载）。
  let timer: ReturnType<typeof setInterval> | undefined

  const latencyPending = computed(() => active.value && state.value === 'thinking') // latencyPending（是否正在计时）。
  const audioLevels = computed(() => { // audioLevels（当前源的五频带音量样例）。
    if (!active.value || !['listening', 'speaking'].includes(state.value)) return quietLevels
    const frames = state.value === 'speaking' ? playbackFrames : microphoneFrames
    const position = phaseElapsed.value / 90
    const index = Math.floor(position) % frames.length
    const next = frames[(index + 1) % frames.length]
    return frames[index].map((level, band) => level + (next[band] - level) * (position % 1))
  })

  function tick() {
    elapsed.value += 50
    if (automatic) {
      let position = elapsed.value % cycleDuration
      for (const phase of phases) {
        if (position < phase.duration) {
          state.value = phase.state
          phaseElapsed.value = position
          if (phase.state === 'thinking') latencyMs.value = position
          if (phase.state === 'speaking') latencyMs.value = 850
          break
        }
        position -= phase.duration
      }
    } else {
      phaseElapsed.value += 50
      if (latencyPending.value) latencyMs.value = phaseElapsed.value
    }
  }
  function syncTimer() {
    if (timer !== undefined) clearInterval(timer)
    timer = undefined
    if (mounted && active.value) timer = setInterval(tick, 50)
  }
  function start() {
    elapsed.value = phaseElapsed.value = 0
    latencyMs.value = null
    automatic = true
    state.value = 'standby'
    active.value = true
  }
  function stop() {
    automatic = false
    active.value = false
    elapsed.value = phaseElapsed.value = 0
    latencyMs.value = null
    state.value = 'standby'
  }
  function selectState(value: DemoRuntimeState) {
    automatic = false
    phaseElapsed.value = 0
    state.value = value
    if (value === 'offline' || value === 'error') active.value = false
    else if (value !== 'standby') active.value = true
    latencyMs.value = value === 'thinking' ? 0 : value === 'speaking' ? 850 : null
  }
  watch(active, syncTimer)
  onMounted(() => { mounted = true; syncTimer() })
  onBeforeUnmount(() => { mounted = false; if (timer !== undefined) clearInterval(timer) })
  return { active, audioLevels, latencyMs, latencyPending, start, stop, selectState }
}
