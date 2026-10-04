"""Desktop acceptance（桌面进程验收）：真实目录启动、所属进程退出及长时间资源采样。"""
from __future__ import annotations

import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor
import psutil


def identity(process):
    return {'pid': process.pid, 'created_at': process.create_time(), 'name': process.name()}


def alive(record):
    try:
        return abs(psutil.Process(record['pid']).create_time() - record['created_at']) < 0.01
    except psutil.Error:
        return False


def main():
    parser = argparse.ArgumentParser(description='验证本实例进程退出与资源，不会按端口猜测或停止其他应用')
    parser.add_argument('--root', type=Path, required=True, help='已组装的应用根目录')
    parser.add_argument('--action', choices=('launch', 'status', 'exit', 'crash', 'soak'), default='status')
    parser.add_argument('--cwd', type=Path, help='启动工作目录，验证入口不依赖当前位置')
    parser.add_argument('--seconds', type=int, default=3600, help='soak（长时间检查）的采样总时长')
    parser.add_argument('--report', type=Path, required=True, help='诊断报告，不含凭据或用户会话内容')
    parser.add_argument('--restricted-path', action='store_true', help='启动时仅保留 Windows（操作系统）内置程序目录')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    portable = json.loads((root / 'portable.json').read_text(encoding='utf-8-sig'))
    data = Path(portable['data_dir'])
    data = (data if data.is_absolute() else root / data).resolve()
    if args.action == 'launch':
        environment = dict(os.environ)
        if args.restricted_path:
            system = Path(os.environ['SystemRoot'])
            environment['PATH'] = os.pathsep.join(map(str, (system / 'System32', system, system / 'System32/WindowsPowerShell/v1.0')))
        subprocess.Popen([str(root / 'VoiceTerminal.exe')], cwd=args.cwd or root, env=environment,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    registry = data / 'state/application/instance.json'
    deadline = time.monotonic() + 45
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    while True:
        try:
            record = json.loads(registry.read_text(encoding='utf-8-sig'))
            origin = record['url']
            address = urlsplit(origin)
            if address.scheme != 'http' or address.hostname != '127.0.0.1' or address.path not in ('', '/'):
                raise ValueError('实例登记不是本地入口地址')
            active = data / 'state/active.json'
            selected = json.loads(active.read_text(encoding='utf-8-sig')) if active.is_file() else portable
            program = (root / selected['app']).resolve(strict=True)
            program.relative_to(root)
            manager = psutil.Process(record['pid'])
            command = manager.cmdline()
            if not any('voice_terminal.application.launch' == value for value in command) or not any(str(program).casefold() in value.casefold() for value in command):
                raise ValueError('登记的管理进程不属于指定程序目录')
            native = manager.parent()
            if not native or Path(native.exe()).resolve() != program / 'VoiceTerminalDesktop.exe':
                raise ValueError('登记的父进程不是本实例桌面程序')
            client.open(origin, timeout=5).close()
            with client.open(origin + '/api/v1/application/snapshot', timeout=5) as response:
                snapshot = json.load(response)
            if snapshot['instance_id'] != record['instance_id']:
                raise ValueError('登记的实例身份与当前入口不一致')
            break
        except (OSError, psutil.Error, ValueError, KeyError) as error:
            if args.action != 'launch' or time.monotonic() >= deadline:
                raise RuntimeError('无法确认当前实例归属：' + str(error)) from error
            time.sleep(0.25)
    owned = [identity(native), *[identity(process) for process in native.children(recursive=True)]]
    result = {'schema': 1, 'action': args.action, 'instance_id': record['instance_id'], 'origin': origin,
              'native_pid': native.pid, 'manager_pid': manager.pid, 'processes': owned, 'samples': [],
              'fields': {'created_at': '进程创建时间', 'processes': '按身份确认的所属进程', 'samples': '资源与应用状态采样',
                         'rss_bytes': '常驻内存总字节，含共享页重复计入', 'private_commit_bytes': '私有提交内存总字节',
                         'elapsed_seconds': '测量经过秒数', 'remaining': '退出后仍存在的同一身份进程'}}
    started = time.monotonic()
    if args.action in ('exit', 'crash'):
        if args.action == 'exit':
            request = Request(origin + '/api/v1/application/exit', b'{}', {'Content-Type': 'application/json', 'Origin': origin})
            client.open(request, timeout=5).close()
        else:
            native.kill()  # 仅结束经路径、父子关系及实例身份确认的本次外壳。
        while any(alive(process) for process in owned) and time.monotonic() - started < 20:
            time.sleep(0.1)
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['remaining'] = [process for process in owned if alive(process)]
    else:
        end = started + (max(1, args.seconds) if args.action == 'soak' else 0)
        while True:
            with client.open(origin + '/api/v1/application/snapshot', timeout=5) as response:
                snapshot = json.load(response)
            processes = [native, *native.children(recursive=True)]
            measurements = {}
            for process in processes:
                try:
                    key = (process.pid, process.create_time())
                    memory = process.memory_info()
                    measurements[key] = (memory.rss, getattr(memory, 'private', None))
                except psutil.Error:
                    continue
            sample = {'elapsed_seconds': round(time.monotonic() - started, 3), 'state': snapshot['state'],
                      'core_available': snapshot['core_available'], 'startup_complete': snapshot['startup_complete'],
                      'components': [{'id': row['id'], 'state': row['state']} for row in snapshot['components']],
                      'rss_bytes': sum(row[0] for row in measurements.values()),
                      'private_commit_bytes': sum(row[1] for row in measurements.values()) if all(row[1] is not None for row in measurements.values()) else None,
                      'process_count': len(measurements)}
            result['samples'].append(sample)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            if time.monotonic() >= end:
                break
            time.sleep(min(30, end - time.monotonic()))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'action': args.action, 'native_pid': native.pid, 'manager_pid': manager.pid,
                      'samples': len(result['samples']), 'elapsed_seconds': result.get('elapsed_seconds'),
                      'remaining': result.get('remaining'), 'report': str(args.report)}, ensure_ascii=False))
    if result.get('remaining'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
