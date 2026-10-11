"""Qualify npm DSH（包安装宿主验收）：独立工作区验证入口、插件工具、桥接和退出。"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from http.cookiejar import CookieJar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.config import DshConfig
from voice_terminal.dsh.distribution import validate_runtime, target_spec
from voice_terminal.dsh.runtime import DshRuntimeManager
from voice_terminal.paths import atomic_json


async def qualify(workspace: Path) -> dict:
    workspace = workspace.resolve()
    if not workspace.is_relative_to(ROOT.parent / '测试') or not workspace.name.startswith('dsh-npm-'):
        raise ValueError('只能在项目旁测试目录中的 dsh-npm-（包安装验收）独立工作区运行。')
    program, runtime = workspace / 'app', workspace / 'runtimes/dsh'
    spec = target_spec(program)
    print('正在核验独立工作区的完整 DSH（代理宿主）组件。', flush=True)
    metadata, node = validate_runtime(program, runtime, full=True)
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('VOICE_')}
    environment.update(VOICE_PROGRAM_ROOT=str(program), VOICE_INSTALL_ROOT=str(workspace), VOICE_DATA_DIR=str(workspace / 'data'), VOICE_DSH_RUNTIME=str(runtime))
    os.environ.update(environment)
    for key in tuple(os.environ):
        if key.upper().startswith('VOICE_') and key not in environment:
            del os.environ[key]
    environment['DSH_HOME'] = str(workspace / 'data/dsh')
    environment.pop('NODE_OPTIONS', None)
    environment.pop('ELECTRON_RUN_AS_NODE', None)
    config = DshConfig(home_dir=workspace / 'data/dsh', port=0, startup_timeout_ms=120000,
                       profile_patch=program / 'config/dsh/web-profile.patch.yml')
    # 先执行正式程序的配置准备，避免插件命令首次调用生成上游空白补丁。
    await DshRuntimeManager(config)._prepare_profile(modules_target=runtime / metadata['modules'])
    checks = []
    for args, expected in ((['--version'], spec['version']), (['plugin', '--profile', 'web', '--version'], spec['package_manager'].split('@')[1])):
        result = subprocess.run([str(node), str(runtime / metadata['entry']), *args], cwd=program,
                                env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60,
                                creationflags=subprocess.CREATE_NO_WINDOW, check=True)
        if result.stdout.strip() != expected:
            raise ValueError('宿主或插件管理工具实际版本不符。')
    checks.extend(['DSH（代理宿主）命令版本与目标一致', '宿主通过明确入口调用私有 pnpm（插件包管理器）'])
    print('宿主版本与私有 pnpm（插件包管理器）调用通过。', flush=True)
    ports = []
    for _ in range(2):
        manager = DshRuntimeManager(config)
        try:
            await manager.start()
            if manager.status().state != 'ready':
                raise ValueError(manager.status().message)
            health = await manager.bridge.health()
            if health.get('protocol_version') != spec['bridge_protocol']:
                raise ValueError('项目桥接协议不符。')
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            with opener.open(manager.launch_url, timeout=10) as response:
                if response.status != 200:
                    raise ValueError('网页服务未就绪。')
            ports.append(manager._actual_port)
        finally:
            process = manager._process
            await manager.close()
            if process is not None and process.returncode is None:
                raise ValueError('代理宿主退出后仍残留进程。')
            if (workspace / 'data/dsh/profiles/web/package.json.lock').exists():
                raise ValueError('正常退出遗留配置写锁，插件管理可能无法继续。')
        print('动态端口、网页、项目桥接及正常退出通过。', flush=True)
    checks.extend(['两次动态端口启动与网页响应成功', '项目插件桥接健康检查成功', '两次正常退出回收宿主进程'])
    return {'ok': True, 'workspace': str(workspace), 'version': spec['version'], 'adapter': spec['adapter'],
            'bridge_protocol': spec['bridge_protocol'], 'ports': ports, 'checks': checks,
            'fields': {'ok': '验收通过', 'workspace': '独立测试工作区', 'version': '随包目标宿主版本',
                       'adapter': '运行适配器', 'bridge_protocol': '桥接协议', 'ports': '两次实际动态端口', 'checks': '实际验收项目'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True, help='workspace（已通过原生组件流程安装的独立测试工作区）')
    args = parser.parse_args()
    report = asyncio.run(qualify(args.workspace))
    atomic_json(args.workspace / 'acceptance.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
