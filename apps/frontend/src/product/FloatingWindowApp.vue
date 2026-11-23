<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { desktopPort } from './desktop-port'
import FloatingWindowShell from '../interface/floating/FloatingWindowShell.vue'
import { FLOATING_LAYOUT_EVENT } from '../interface/floating/floating-display'
import VoiceOrb from '../interface/VoiceOrb.vue'
import { EMPTY_FLOATING_WINDOW_MODEL, FLOATING_WINDOW_MODEL_EVENT, readFloatingCaption, type FloatingOrbFrame, type FloatingWindowModel } from '../interface/floating/floating-window'
import '../interface/design.css'
import '../interface/floating/floating-window.css'

const model = ref<FloatingWindowModel>({ ...EMPTY_FLOATING_WINDOW_MODEL })
const port = desktopPort()
const frame = ref<FloatingOrbFrame | null>(null)
let layoutRevision = 0
const unlisteners: Array<() => void> = []
interface OrbLayout {
  revision: number // revision（布局版本）。
  orb_frame: FloatingOrbFrame | null // orb_frame（菱形与识别框布局）。
}
function acceptLayout(value: OrbLayout) {
  if (!disposed && value.revision >= layoutRevision) {
    layoutRevision = value.revision
    frame.value = value.orb_frame
  }
}
let disposed = false

function acceptModel(value: FloatingWindowModel) {
  if (disposed || !value || !Number.isFinite(value.revision) || value.revision < model.value.revision) return
  if (!Array.isArray(value.audioLevels) || value.audioLevels.length !== 5) return
  model.value = {
    revision: value.revision,
    doubleClickMs: Number.isFinite(value.doubleClickMs) ? Math.max(500, Math.min(5000, value.doubleClickMs)) : 500,
    state: typeof value.state === 'string' ? value.state : 'offline',
    sessionActive: value.sessionActive === true,
    audioLevels: value.audioLevels.map(level => typeof level === 'number' && Number.isFinite(level)
      ? Math.max(0, Math.min(1, level)) : 0),
    silenced: value.silenced !== false,
    canControl: value.canControl === true,
    caption: readFloatingCaption(value.caption),
  }
}

async function initialize() {
  if (!port.available || !port.getFloatingModel || !port.floatingWindow || !port.listen) return
  try {
    for (const subscribe of [
      () => port.listen!<FloatingWindowModel>(FLOATING_WINDOW_MODEL_EVENT, event => acceptModel(event.payload)),
      () => port.listen!<OrbLayout>(FLOATING_LAYOUT_EVENT, event => acceptLayout(event.payload)),
    ]) {
      const unlisten = await subscribe()
      if (disposed) { unlisten(); return }
      unlisteners.push(unlisten)
    }
    acceptModel(await port.getFloatingModel())
    if (!disposed) acceptLayout(await port.floatingWindow<OrbLayout>({ kind: 'ready' }))
  } catch {
    if (model.value.revision === 0) model.value = { ...EMPTY_FLOATING_WINDOW_MODEL, state: 'offline' }
  }
}

function returnToWorkbench() {
  void port.returnToWorkbench?.().catch(() => undefined)
}

function drag(origin: { x: number; y: number }) {
  void port.floatingWindow?.({ kind: 'drag', ...origin }).catch(() => undefined)
}

function toggleSilence() {
  if (!model.value.canControl) return
  void port.controlFloatingSession?.(model.value.silenced ? 'start' : 'silence').catch(() => undefined)
}

onMounted(() => { void initialize() })
onBeforeUnmount(() => { disposed = true; unlisteners.forEach(unlisten => unlisten()) })
</script>

<template>
  <FloatingWindowShell :model="model" :frame="frame" @return-to-workbench="returnToWorkbench" @drag="drag" @toggle-silence="toggleSilence">
    <template #content="{ model: orbModel }">
      <VoiceOrb :model="orbModel" />
    </template>
  </FloatingWindowShell>
</template>
