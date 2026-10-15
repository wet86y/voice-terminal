/** disableContextMenu（禁用网页右键菜单）：覆盖启动、管理、工作台和样例页面。 */
export function disableContextMenu(target: Document) {
  const prevent = (event: Event) => event.preventDefault()
  target.addEventListener('contextmenu', prevent, { capture: true })
  return () => target.removeEventListener('contextmenu', prevent, { capture: true })
}

if (typeof document !== 'undefined') disableContextMenu(document)
