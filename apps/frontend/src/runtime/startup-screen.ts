import icon from '../../../desktop/boot/app-icon.svg'
import '../../../desktop/boot/startup.css'
import { APPLICATION_EXIT_EVENT } from './application-motion'

export { icon as startupIcon } // startupIcon（所有启动画面共用的应用图标）。

// preflight exit（预检阶段退出）：产品组件挂载前也沿用同一秒的退出画面。
if (typeof window !== 'undefined') window.addEventListener(APPLICATION_EXIT_EVENT, () => {
  const screen = document.querySelector<HTMLElement>('.startup-screen:not(.application-startup)')
  if (!screen) return
  const root = screen.closest<HTMLElement>('#app')
  if (root) root.dataset.applicationExiting = 'true' // applicationExiting（预检期间已请求退出，拒绝迟到挂载）。
  screen.classList.add('is-exiting')
  screen.setAttribute('inert', '')
  const title = screen.querySelector('.startup-title')
  const status = screen.querySelector('.startup-status')
  if (title) title.textContent = '正在退出应用'
  if (status) { status.textContent = '正在关闭本地组件'; status.classList.remove('is-error') }
  const progress = screen.querySelector<HTMLElement>('.startup-progress')
  if (progress) { progress.dataset.indeterminate = 'true'; progress.setAttribute('aria-label', '应用退出进度') }
  screen.querySelector('.startup-progress-fill')?.removeAttribute('style')
})
