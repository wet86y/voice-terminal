/** openVoiceSettings（按真实控件打开插件设置）：处理宿主异步出现的说明及登录弹窗，不绕过遮罩。 */
export async function openVoiceSettings(page) {
  const deadline = Date.now() + 30_000
  let lastError
  while (Date.now() < deadline) {
    const notice = page.getByRole('dialog', { name:'预览版说明' })
    if (await notice.isVisible()) {
      await notice.getByRole('button', { name:'继续', exact:true }).click()
      await notice.waitFor({ state:'hidden' })
      continue
    }
    const signIn = page.getByRole('dialog', { name:'开始使用', exact:true })
    if (await signIn.isVisible()) {
      await signIn.getByRole('button', { name:'关闭', exact:true }).click()
      await signIn.waitFor({ state:'hidden' })
      continue
    }
    const settings = page.getByRole('dialog', { name:'设置', exact:true })
    try {
      if (await settings.isVisible()) {
        if (await settings.getByRole('button', { name:'模型 (Model)', exact:true }).isVisible()) return settings
        await settings.getByRole('button', { name:/语音终端|Voice Terminal/ }).click({ timeout:1000 })
      } else {
        const item = page.getByRole('menuitem', { name:'设置', exact:true })
        if (await item.isVisible()) await item.click({ timeout:1000 })
        else await page.getByRole('button', { name:'账号菜单', exact:true }).click({ timeout:1000 })
      }
    } catch (error) {
      if (error.name !== 'TimeoutError') throw error
      lastError = error
    }
  }
  throw lastError ?? new Error('DSH 插件设置未在限定时间内就绪。')
}
