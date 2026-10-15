<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { desktopPort } from './desktop-port'
import FloatingWindowShell from './FloatingWindowShell.vue'
import VoiceOrb from '../interface/VoiceOrb.vue'
import { EMPTY_FLOATING_WINDOW_MODEL, FLOATING_WINDOW_MODEL_EVENT, readFloatingCaption, type FloatingWindowModel } from './floating-window'
import '../interface/design.css'
import './floating-window.css'

const model = ref<FloatingWindowModel>({ ...EMPTY_FLOATING_WINDOW_MODEL })
const port = desktopPort()
let unlisten: (() => void) | undefined
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
  if (!port.available || !port.getFloatingModel || !port.listen) return
  try {
    unlisten = await port.listen<FloatingWindowModel>(FLOATING_WINDOW_MODEL_EVENT, event => acceptModel(event.payload))
    if (disposed) { unlisten(); return }
    acceptModel(await port.getFloatingModel())
  } catch {
    if (model.value.revision === 0) model.value = { ...EMPTY_FLOATING_WINDOW_MODEL, state: 'offline' }
  }
}

function returnToWorkbench() {
  void port.returnToWorkbench?.().catch(() => undefined)
}

function drag() {
  void port.startFloatingDrag?.().catch(() => undefined)
}

function toggleSilence() {
  if (!model.value.canControl) return
  void port.controlFloatingSession?.(model.value.silenced ? 'start' : 'silence').catch(() => undefined)
}

onMounted(() => { void initialize() })
onBeforeUnmount(() => { disposed = true; unlisten?.() })
</script>

<template>
  <FloatingWindowShell :model="model" @return-to-workbench="returnToWorkbench" @drag="drag" @toggle-silence="toggleSilence">
    <template #content="{ model: orbModel }">
      <VoiceOrb :model="orbModel" />
    </template>
  </FloatingWindowShell>
</template>
