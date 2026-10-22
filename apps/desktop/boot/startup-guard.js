// Startup guard（首屏兜底）：独立于界面模块与外部样式，加载失败时仍保留可操作画面。
(() => {
  let finished = false;
  let failed = false;
  const root = () => document.getElementById('app');
  let timer;
  function start() { window.clearTimeout(timer); finished = false; failed = false; timer = window.setTimeout(() => fail('启动等待超时，请重新加载；若仍然失败，请查看启动日志或使用完整性维护。'), 30000); }
  function ready() { finished = true; window.clearTimeout(timer); }
  function fail(reason) {
    if (finished || root()?.dataset.applicationExiting === 'true') return;
    failed = true;
    ready();
    const target = root();
    if (!target) return;
    const icon = target.querySelector('.startup-icon')?.cloneNode(true);
    const screen = document.createElement('section');
    screen.className = 'startup-screen'; screen.setAttribute('role', 'alert');
    const content = document.createElement('div'); content.className = 'startup-content';
    if (icon) content.append(icon);
    const title = document.createElement('h1'); title.className = 'startup-title'; title.textContent = '界面加载未完成';
    const detail = document.createElement('p'); detail.className = 'startup-status is-error';
    detail.textContent = String(reason instanceof Error ? reason.message : reason);
    const actions = document.createElement('div'); actions.className = 'startup-actions';
    const retry = document.createElement('button'); retry.className = 'startup-button'; retry.textContent = '重新加载'; retry.onclick = () => window.location.reload();
    actions.append(retry);
    const native = window.__TAURI_INTERNALS__;
    if (native) {
      const exit = document.createElement('button'); exit.className = 'startup-button'; exit.textContent = '退出应用';
      exit.onclick = () => { Promise.resolve().then(() => native.invoke('exit_application')).catch(error => { detail.textContent = String(error); }); };
      actions.append(exit);
    }
    content.append(title, detail, actions); screen.append(content); target.replaceChildren(screen);
  }
  window.__VOICE_STARTUP__ = { start, ready, fail, get failed() { return failed; } }; // start（重新计时）、ready（界面已接管）、fail（加载失败）、failed（已中止挂载）。
  start();
  window.addEventListener('error', event => {
    if (event.target instanceof HTMLScriptElement) fail('界面脚本加载失败，请重新加载或校验程序完整性。');
    else if (event.error || event.message) fail(event.error || event.message);
  }, true);
  window.addEventListener('unhandledrejection', event => fail(event.reason || '界面初始化失败，请重新加载。'));
  window.addEventListener('vite:preloadError', () => fail('界面资源加载失败，请重新加载或校验程序完整性。'));
})();
