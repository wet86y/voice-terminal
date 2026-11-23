import { createApp } from 'vue'
import { runDesktopPreflight } from '../../runtime/desktop-preflight'
import '../../interface/design.css'

const view = new URLSearchParams(location.search).get('view') // view（本机测试窗口角色）。
if (view === 'orb') {
  void import('./PreviewOrb.vue').then(({ default: Orb }) => createApp(Orb).mount('#app'))
} else if (view === 'display') {
  void import('./PreviewDisplay.vue').then(({ default: Display }) => createApp(Display).mount('#app'))
} else {
  void runDesktopPreflight({
    expectedMode: 'ui-preview',
    startApplication: async () => {
      const { default: Console } = await import('./PreviewConsole.vue')
      createApp(Console).mount('#app')
    },
  })
}
