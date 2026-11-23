import { createApp } from 'vue'

const page = location.search === '?view=display'
  ? import('./FloatingDisplayApp.vue')
  : import('./FloatingWindowApp.vue')

void page.then(({ default: component }) => createApp(component).mount('#app'))
