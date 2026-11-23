<script setup lang="ts">
import FloatingWindowShell from '../../interface/floating/FloatingWindowShell.vue'
import VoiceOrb from '../../interface/VoiceOrb.vue'
import { useFloatingPreview } from './preview-port'
import '../../interface/floating/floating-window.css'

const { snapshot, run } = useFloatingPreview('ready')
</script>

<template>
  <FloatingWindowShell v-if="snapshot" :model="snapshot.model" :frame="snapshot.orb_frame"
    @return-to-workbench="run({ kind: 'return' })" @drag="origin => run({ kind: 'drag', ...origin })" @toggle-silence="run({ kind: 'toggle' })">
    <template #content="{ model }"><VoiceOrb :model="model" /></template>
  </FloatingWindowShell>
</template>
