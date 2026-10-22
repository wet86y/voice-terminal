/** Frontend continuity（开发界面连续会话）：受控重启后重连 HMR（源码热更新），保留页面。 */
import { createHash } from 'node:crypto'

export function frontendContinuity(session) {
  return {
    name: 'desktop-frontend-continuity',
    apply: 'serve',
    configResolved(config) {
      // webSocketToken（网页套接字令牌）从当前窗口随机会话派生，保持原有 Cookie 与来源检查。
      config.webSocketToken = createHash('sha256').update('desktop-hmr:').update(session).digest('base64url').slice(0, 12)
    },
    transform(code, id) {
      if (!id.replaceAll('\\', '/').endsWith('/vite/dist/client/client.mjs')) return
      const reconnect = /await waitForSuccessfulPing\(url\.href\);\s*location\.reload\(\);/
      if (!reconnect.test(code)) throw new Error('当前 Vite（界面开发工具）的重连协议已变化，请更新桌面连续会话适配。')
      return code.replace(reconnect, `
        const desktopRecovery = async () => window.__TAURI_INTERNALS__?.invoke('desktop_status')
          .then(value => value.runtime_recovery).catch(() => undefined);
        let recovery = await desktopRecovery();
        if (recovery?.suspended) window.__VOICE_DESKTOP_RECOVERY__ = true;
        while (recovery?.suspended) {
          await new Promise(resolve => setTimeout(resolve, 200));
          recovery = await desktopRecovery();
        }
        await waitForSuccessfulPing(url.href);
        if (window.__VOICE_DESKTOP_RECOVERY__) {
          window.__VOICE_DESKTOP_RECOVERY__ = false;
          // 新进程重建原页面已加载的模块关系，再接回现有 HMR（源码热更新）上下文。
          await Promise.all([...hmrClient.hotModulesMap.keys()].map(path => fetch(path, { cache: 'no-store' })));
          await transport.disconnect();
          await transport.connect(createHMRHandler(handleMessage));
        } else location.reload();`)
    },
  }
}
