<script setup lang="ts">
import { resolveFloatingImage } from '../../interface/floating/floating-display'
import FloatingDisplayWindow from '../../interface/floating/FloatingDisplayWindow.vue'
import samplePicture from '../../assets/app-icon.svg?url'
import { useFloatingPreview } from './preview-port'

const { snapshot, run } = useFloatingPreview('snapshot')

function resolveImage(reference: string) {
  if (reference === 'fixture:icon') return samplePicture
  return resolveFloatingImage(reference)
}
</script>

<template>
  <FloatingDisplayWindow :request="snapshot?.active ? snapshot.request : null" :frame="snapshot?.display_frame"
    :resolve-image="resolveImage" :dispatch="run" />
</template>
