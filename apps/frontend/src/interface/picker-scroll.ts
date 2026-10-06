/** containPickerWheel（限制选择面板滚轮）：保留内部滚动，阻止滚轮传到背景页面。 */
export function containPickerWheel(event: WheelEvent) {
  if (event.ctrlKey) return // Ctrl（控制键）组合滚轮仍交由浏览器处理。
  const panel = event.currentTarget as HTMLElement // panel（接收滚轮的浮动面板）。
  let element = event.target instanceof Element ? event.target : null // element（从鼠标所在元素向上查找）。
  while (element && panel.contains(element)) {
    if (element instanceof HTMLElement && /auto|scroll/.test(getComputedStyle(element).overflowY) && element.scrollHeight > element.clientHeight) {
      const remaining = event.deltaY < 0 ? element.scrollTop : element.scrollHeight - element.clientHeight - element.scrollTop // remaining（滚动方向剩余距离）。
      if (event.deltaY !== 0 && remaining > 1) return
      break
    }
    if (element === panel) break
    element = element.parentElement
  }
  // 无需滚动或已到边缘时取消默认行为，避免页面跟着星期、日期等面板移动。
  event.preventDefault()
}
