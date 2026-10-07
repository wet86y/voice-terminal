"""Lifecycle acceptance（实机生命周期验收）：所属原生窗口、组件、退出与重启。"""
from pathlib import Path
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
import psutil
import websockets
from voice_terminal.local_layout import read_portable
from voice_terminal.paths import atomic_json
from voice_terminal.distribution import require_stopped
from voice_terminal.local_files import remove_tree


async def evaluate(url, expression):
    """CDP（浏览器调试协议）仅连接本次启动窗口，用正常桌面退出指令收尾。"""
    async with websockets.connect(url, origin='http://127.0.0.1') as connection:
        await connection.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {'expression': expression, 'awaitPromise': True, 'returnByValue': True}}))
        while True:
            response = json.loads(await connection.recv())
            if response.get('id') == 1:
                if response.get('error') or response.get('result', {}).get('exceptionDetails'):
                    raise RuntimeError('本次窗口的验收指令执行失败')
                return response.get('result', {}).get('result', {}).get('value')


async def run(mode, timeout):
    local = ROOT / '.local'
    selected = read_portable(local)
    require_stopped(ROOT, local)
    if mode == 'headless':
        await run_headless(local, selected, timeout)
        return
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env = dict(os.environ)
    env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = f'--remote-debugging-port={port} --remote-allow-origins=http://127.0.0.1'
    if mode == 'formal':
        command = [str(local / 'VoiceTerminal.exe')]
    elif mode == 'demo':
        command = [str(local / 'VoiceTerminalUIDemo.exe')]
    else:
        command = [str(local / 'build/desktop/debug/VoiceTerminalDesktop.exe'), '--root', str(ROOT), '--dev']
    registry = local / 'data/state/application/instance.json'
    previous = registry.read_bytes() if registry.exists() else b''
    launch = subprocess.Popen(command, cwd=local / 'tmp', env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    started = time.monotonic()
    native = None
    manager = None
    browser_url = None
    result = {'mode': mode, 'fields': {'mode': '实际启动入口', 'seconds': '启动等待秒数', 'state': '组件终态', 'core_available': '核心语音能力可用', 'components': '组件状态', 'exited': '正常退出且所属进程回收'}}
    try:
        while time.monotonic() - started < timeout:
            for process in psutil.process_iter(['pid', 'exe', 'create_time']):
                value = process.info['exe']
                if value and process.info['create_time'] >= time.time() - (time.monotonic() - started) - 3:
                    expected = local / ('app/VoiceTerminalDesktop.exe' if mode == 'formal' else 'build/desktop/debug/VoiceTerminalDesktop.exe')
                    if Path(value).resolve() == expected:
                        native = process
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=1) as response:
                    pages = json.load(response)
                page = next((p for p in pages if p.get('type') == 'page' and p.get('url', '').startswith('http://127.0.0.1:')), None)
                if page:
                    browser_url = page['webSocketDebuggerUrl']
                if browser_url and native:
                    if mode == 'demo':
                        ready = await evaluate(browser_url, "Boolean(document.querySelector('#app .workbench'))")
                        if ready:
                            result['state'] = 'ready'
                            break
                    elif registry.exists() and registry.read_bytes() != previous:
                        registration = json.loads(registry.read_text(encoding='utf-8-sig'))
                        manager = psutil.Process(registration['pid'])
                        snapshot = await evaluate(browser_url, "fetch('/api/v1/application/snapshot').then(r=>r.json())")
                        if snapshot and snapshot.get('startup_complete'):
                            result.update(state=snapshot['state'], core_available=snapshot['core_available'],
                                          components={p['id']: {'state': p['state'], 'error': p['error']} for p in snapshot['components']})
                            if not snapshot['core_available']:
                                raise RuntimeError('实机核心能力未达到可用状态')
                            break
            except (OSError, StopIteration, ValueError, psutil.Error):
                pass
            await asyncio.sleep(.5)
        else:
            raise TimeoutError('实机启动或界面握手验收超时')
        result['seconds'] = round(time.monotonic() - started, 2)
        assert native is not None and browser_url
        owned = [native, *native.children(recursive=True)]
        if mode == 'demo':
            assert not any(p.name().lower().startswith('python') for p in owned), '演示启动了业务解释器'
            assert not registry.exists() or registry.read_bytes() == previous, '演示改写了业务实例登记'
        else:
            assert manager and Path(manager.exe()).resolve() == local / selected['python']
            environment = manager.environ()
            assert Path(environment['VOICE_MODEL_DIR']).resolve() == local / selected['models']
            assert Path(environment['VOICE_DATA_DIR']).resolve() == local / selected['data_dir']
            assert Path(environment['VOICE_DSH_RUNTIME']).resolve() == local / selected['dsh']
        identities = [(p.pid, p.create_time()) for p in owned]
        # 发出与产品菜单相同的受限命令，随后等待全部实际进程退出。
        await evaluate(browser_url, "window.__TAURI_INTERNALS__.invoke('exit_application'); true")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            remaining = []
            for pid, created in identities:
                try:
                    if psutil.Process(pid).create_time() == created:
                        remaining.append(pid)
                except psutil.NoSuchProcess:
                    pass
            if not remaining:
                result['exited'] = True
                break
            await asyncio.sleep(.25)
        else:
            raise RuntimeError('桌面退出后仍存在本实例所属进程')
        # 实机验收成功后回收本次渲染配置；业务数据保存在独立持久目录。
        require_stopped(ROOT, local)
        profile = {'formal': 'webview2', 'development': 'webview2-source', 'demo': 'ui-demo'}[mode]
        remove_tree(local / 'cache' / profile, local)
    finally:
        if native and native.is_running():
            # 仅验收失败时按确认的父子身份回收本次进程；不能影响别的应用。
            for process in reversed(native.children(recursive=True)):
                try:
                    process.kill()
                except psutil.Error:
                    pass
            try:
                native.kill()
            except psutil.Error:
                pass
        report = local / f'cache/acceptance/{mode}.json'
        atomic_json(report, result)
    print(json.dumps(result, ensure_ascii=False))


async def run_headless(local, selected, timeout):
    """Headless（无界面）以真实命令启动，用受管正常退出事件回收本实例。"""
    import uuid
    from voice_terminal.paths import AppPaths
    from voice_terminal.application.windows import ShutdownEvent
    event = ShutdownEvent('Local\\VoiceTerminal-acceptance-' + uuid.uuid4().hex)
    env = {**os.environ, **AppPaths.resolve(ROOT).environment(), 'VOICE_APP_SHUTDOWN_EVENT': event.name}
    report = {'mode': 'headless', 'fields': {'mode': '实际启动入口', 'seconds': '启动等待秒数', 'state': '组件终态', 'core_available': '核心能力可用', 'exited': '正常退出且子进程回收'}}
    output = local / 'cache/acceptance/headless.log'
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with output.open('wb') as log:
        launch = subprocess.Popen([str(local / selected['python']), '-B', '-X', 'utf8', str(ROOT / 'scripts/backend-entry.py'), 'voice_terminal.application.launch', '--root', str(ROOT), '--headless'], env=env, cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        native = psutil.Process(launch.pid)
        try:
            while time.monotonic() - started < timeout:
                if launch.poll() is not None:
                    raise RuntimeError('无界面宿主提前退出')
                snapshot = local / 'data/state/startup-result.json'
                if snapshot.exists():
                    value = json.loads(snapshot.read_text(encoding='utf-8'))
                    if value.get('pid') == launch.pid:
                        assert value['core_available'], '无界面核心能力不可用'
                        report.update(state=value['state'], core_available=True, seconds=round(time.monotonic() - started, 2))
                        break
                await asyncio.sleep(.5)
            else:
                raise TimeoutError('无界面宿主启动超时')
            identities = [(p.pid, p.create_time()) for p in native.children(recursive=True)]
            event.signal()
            await asyncio.to_thread(launch.wait, 25)
            assert launch.returncode == 0
            await asyncio.sleep(.5)
            for pid, created in identities:
                try:
                    assert psutil.Process(pid).create_time() != created, '无界面退出仍留有子进程'
                except psutil.NoSuchProcess:
                    pass
            report['exited'] = True
        finally:
            if launch.poll() is None:
                event.signal()
                try:
                    await asyncio.to_thread(launch.wait, 25)
                except subprocess.TimeoutExpired:
                    for child in reversed(native.children(recursive=True)):
                        try: child.kill()
                        except psutil.Error: pass
                    launch.kill()
                    launch.wait()
            event.close()
            atomic_json(local / 'cache/acceptance/headless.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本工程实机原生入口验收，默认不开始语音监听')
    parser.add_argument('mode', choices=['formal', 'demo', 'development', 'headless'], help='mode（正式、演示、源码开发、无界面）')
    parser.add_argument('--timeout', type=int, default=240, help='timeout（启动等待上限秒数）')
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.timeout))
