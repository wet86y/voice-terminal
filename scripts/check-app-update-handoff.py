"""App update handoff（应用更新交接验收）：通过正式分发器验证旧版编号及退出时序。"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.workspace_assets import ManagedRun, guarded, maintenance_warning, task_folder, write_record
from voice_terminal.local_files import remove_tree


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def creation_time(pid: int) -> int:
    """creation_time（进程创建时间）：保留 Windows（操作系统）的完整百纳秒精度。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def stop_test_processes(workspace: Path) -> None:
    """stop_test_processes（回收验收进程）：仅处理本轮新建工作区内的可执行文件。"""
    owned = []
    for process in psutil.process_iter(['exe']):
        try:
            if process.info['exe'] and Path(process.info['exe']).resolve().is_relative_to(workspace):
                owned.extend(process.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
    entry = workspace / 'VoiceTerminal.exe'
    if entry.exists():
        subprocess.run([str(entry), '--exit'], cwd=workspace, env=clean_environment(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    for process in psutil.process_iter(['exe']):
        try:
            executable = process.info['exe']
            if executable and Path(executable).resolve().is_relative_to(workspace):
                process.terminate()
                process.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
    # WebView2（网页运行引擎）的映像位于系统目录，通过本轮宿主的子进程身份等待退出。
    psutil.wait_procs(owned, timeout=10)


def clean_environment() -> dict:
    """clean_environment（隔离环境）：不继承本机业务位置、密钥或后端模块路径。"""
    return {key: value for key, value in os.environ.items()
            if not key.startswith('VOICE_') and key not in ('PYTHONPATH', 'PYTHONHOME', 'WEBVIEW2_BROWSER_EXECUTABLE_FOLDER')}


def start_previous(workspace: Path) -> psutil.Process:
    entry = subprocess.Popen([str(workspace / 'VoiceTerminal.exe')], cwd=workspace, env=clean_environment(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    assert entry.wait(timeout=45) == 0, '旧版正式入口启动失败'
    executable = workspace / 'app/VoiceTerminalDesktop.exe'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for process in psutil.process_iter(['exe']):
            if process.info['exe'] and Path(process.info['exe']).resolve() == executable:
                ready_path = workspace / 'data/state/workspace-ready.json'
                try:
                    ready = json.loads(ready_path.read_text(encoding='utf-8'))
                    if ready.get('pid') == process.pid and ready.get('ready') and ready.get('role') == 'product':
                        return process
                except (OSError, ValueError):
                    pass
        time.sleep(0.1)
    raise AssertionError('未找到隔离工作区的旧版宿主')


def exit_previous(workspace: Path, parent: psutil.Process) -> None:
    subprocess.run([str(workspace / 'VoiceTerminal.exe'), '--exit'], cwd=workspace, env=clean_environment(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    parent.wait(timeout=30)


def cleanup_workspace(workspace: Path, base: Path) -> None:
    """cleanup_workspace（回收独立验收目录）：等待网页引擎异步释放缓存文件。"""
    base = guarded(ROOT, str(base))
    workspace = guarded(ROOT, str(workspace))
    if workspace.parent != base or base.parent != ROOT / '.local/tmp/tasks' / task_folder('app-update-handoff'):
        raise ValueError('更新验收只允许回收本轮受管任务的具体子工作区')
    deadline = time.monotonic() + 20
    while workspace.exists():
        try:
            remove_tree(workspace, base)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def check_case(installer: Path, previous_package: Path, base: Path, mode: str, version: str, package: dict) -> dict:
    workspace = base / mode
    workspace.mkdir()
    identity = f'验收工作区-{mode}-{time.time_ns():x}'
    write_json(workspace / 'portable.json', {
        'schema': 2, 'workspace_id': identity, 'app': 'app', 'python': 'runtimes/python/python.exe',
        'models': 'models', 'dsh': 'runtimes/dsh', 'webview2': None, 'data_dir': 'data',
        'fields': {'schema': '固定布局格式', 'workspace_id': '验收工作区编号', 'app': '程序位置',
                   'python': '后端解释器', 'models': '模型目录', 'dsh': '代理宿主', 'webview2': '网页引擎', 'data_dir': '用户数据'},
    })
    sentinels = {}
    for relative in ('data/config/update-sentinel.json', 'data/history/update-sentinel.txt', 'models/update-sentinel.bin', 'runtimes/update-sentinel.bin'):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes('本轮虚构验收数据，更新前后内容必须一致。'.encode())
        sentinels[relative] = digest(path)
    # 展开真实旧版正式包，仅添加本轮虚构数据，不复制本机用户配置或运行环境。
    with zipfile.ZipFile(previous_package) as archive:
        for name in archive.namelist():
            relative = Path(name)
            assert not relative.is_absolute() and '..' not in relative.parts and ':' not in name, '旧程序包路径越界'
        archive.extractall(workspace)
        previous = json.loads(archive.read('program-package.json'))
    executable = workspace / 'app/VoiceTerminalDesktop.exe'
    previous_hash = digest(executable)
    parent = None
    process = None
    try:
        parent = start_previous(workspace)
        created = creation_time(parent.pid)
        nonce = f'{time.time_ns():x}-{parent.pid:x}'
        config_path = workspace / f'tmp/app-update-{nonce}.json'
        update = {
            'schema': 1, 'nonce': nonce, 'target_root': str(workspace), 'workspace_id': identity,
            'parent_pid': parent.pid, 'parent_create_time': created, 'parent_executable': str(executable),
            'version': version, 'tag': f'v{version}', 'installer_size': installer.stat().st_size, 'installer_sha256': digest(installer),
            'fields': {'schema': '一次性更新配置格式', 'nonce': '旧版单次使用编号', 'target_root': '目标工作区',
                       'workspace_id': '工作区身份', 'parent_pid': '旧程序进程编号', 'parent_create_time': '旧程序创建时间',
                       'parent_executable': '旧程序可执行文件', 'version': '目标产品版本', 'tag': '对应发行标签',
                       'installer_size': '安装器字节数', 'installer_sha256': '安装器完整文件摘要'},
        }
        write_json(config_path, update)
        if mode == 'parent-exited':
            exit_previous(workspace, parent)
        process = subprocess.Popen([str(installer), '--app-update-config', str(config_path)], cwd=workspace, env=clean_environment(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        marker = config_path.with_suffix('.json.consumed')
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f'分发器提前退出：{process.returncode}')
            time.sleep(0.1)
        assert marker.read_text(encoding='utf-8') == nonce, '分发器未确认旧版单次编号'
        assert not config_path.exists(), '一次性配置未被消费'
        if mode == 'parent-waiting':
            time.sleep(1)
            assert parent.is_running(), '旧版程序意外退出'
            assert digest(executable) == previous_hash, '旧程序未退出时不应替换本体'
            exit_previous(workspace, parent)
        assert process.wait(timeout=150) == 0, '正式更新交接失败'
        release = json.loads((workspace / 'app/release.json').read_text(encoding='utf-8'))
        assert release['display_version'] == version, '安装后程序版本不符'
        assert release['build'] == package['build'], '安装后构建编号不符'
        assert json.loads((workspace / 'portable.json').read_text(encoding='utf-8'))['workspace_id'] == identity, '工作区身份发生变化'
        assert all(digest(workspace / relative) == expected for relative, expected in sentinels.items()), '原有数据或组件哨兵文件被修改'
        for row in package['files']:
            if row['path'] == 'app/release.json':
                continue  # release（发行清单）的外置组件登记由工作区事务维护。
            assert digest(workspace / row['path']) == row['sha256'], f'安装后固定文件摘要不符：{row["path"]}'
        assert not (workspace / 'tmp/update-transaction.json').exists(), '遗留未完成安装事务'
        return {'mode': mode, 'ok': True, 'previous_version': previous['version'], 'version': version, 'build': release['build'],
                'fixed_files': len(package['files']), 'preserved_files': len(sentinels), 'nonce': nonce,
                'fields': {'mode': '父进程已退出或等待退出场景', 'ok': '真实更新入口验收通过', 'previous_version': '真实旧程序版本', 'version': '安装后版本',
                           'build': '安装后构建编号', 'fixed_files': '核对固定文件数', 'preserved_files': '保留哨兵文件数', 'nonce': '旧格式单次编号'}}
    finally:
        if parent is not None and parent.is_running():
            exit_previous(workspace, parent)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        stop_test_processes(workspace)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer', type=Path, default=ROOT / '.local/releases/installer/VoiceTerminalSetup.exe', help='installer（待验证的正式分发器）')
    parser.add_argument('--previous-package', type=Path, required=True, help='previous-package（真实旧版正式程序包）')
    parser.add_argument('--previous-sha256', required=True, help='previous-sha256（旧版公开交付记录的程序包摘要）')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('交接验收需要 Windows（操作系统）')
    installer = args.installer.resolve(strict=True)
    previous_package = args.previous_package.resolve(strict=True)
    assert digest(previous_package) == args.previous_sha256.lower(), '旧版程序包与公开交付记录摘要不一致'
    package_root = ROOT / '.local/build/program-package'
    package = json.loads((package_root / 'payload/program-package.json').read_text(encoding='utf-8'))
    run = ManagedRun('app-update-handoff', ROOT)
    base = run.path
    cases = []
    try:
        for mode in ('parent-waiting', 'parent-exited'):
            result = check_case(installer, previous_package, base, mode, package['version'], package)
            cases.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            # 成功后只回收本轮独立子目录，保留验收报告。
            workspace = base / mode
            try:
                cleanup_workspace(workspace, base)
            except OSError as error:
                maintenance_warning(error)
        report = {'ok': True, 'cases': cases, 'fields': {'ok': '全部真实更新交接验收通过', 'cases': '逐场景验收记录'}}
        try:
            write_record(ROOT, '.local/cache/reports/app-update-handoff-latest.json', report)
        except (OSError, ValueError) as error:
            maintenance_warning(error)
    except Exception as error:
        try:
            write_json(base / 'report.json', {'ok': False, 'error': str(error), 'cases': cases,
                                            'fields': {'ok': '是否全部通过', 'error': '失败原因', 'cases': '已通过场景'}})
        except OSError as report_error:
            maintenance_warning(report_error)
        run.finish(1)
        raise
    run.finish(0)
    print(f'旧版更新交接验收通过：{ROOT / ".local/cache/reports/app-update-handoff-latest.json"}', flush=True)


if __name__ == '__main__':
    main()
