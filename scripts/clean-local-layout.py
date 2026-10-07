"""Cleanup（存量清退）：具体路径白名单、保护对象、重解析点与长路径处理。"""
from pathlib import Path
import argparse
import json
import shutil
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, require_stopped, verify_install
from voice_terminal.local_files import backup_user_data, persistent_files, remove_tree, reparse
from voice_terminal.paths import atomic_json
from voice_terminal.local_update import update_lock

LEGACY = ['.runtime', 'archive', '.pytest_cache', 'logs', 'reports', 'workspace', 'apps/desktop/target', 'apps/frontend/node_modules',
          'runtime-v05.log', 'runtime-v05.error.log', '.env.local', 'config/local.yaml', 'config/model-registry.json',
          'config/product-settings.json', 'config/continuity-state.json', 'config/wake-tasks.json', 'config/dsh-task-deliveries.json',
          'config/voice-mailbox.json', 'config/speaker', '.local/data/logs', '.local/data/cache', '.local/data/state/active.json',
          '.local/tmp/layout-migration.json', '.local/tmp/native-tests']
PROTECTED = ['.git', '.local/app', '.local/runtimes', '.local/models', '.local/data/config', '.local/data/persona',
             '.local/data/speaker', '.local/data/history', '.local/data/dsh', '.local/data/workspace', '.local/data/imports',
             '.local/backups/user-data', '.local/build', '.local/tools', 'node_modules', 'docs/download-distribution-plan.md']


def protect_source_data(local: Path):
    """源码侧的旧持久文件按摘要保全，不覆盖权威数据；凭据内容不进入输出。"""
    seen = {digest(p) for p in persistent_files(local / 'data')}
    records = []
    candidates = [ROOT / p for p in LEGACY if p.startswith('config/') or p == '.env.local']
    for origin in candidates:
        if not origin.exists() or reparse(origin):
            continue
        if origin.is_dir():
            pending = [origin]
            files = []
            while pending:
                path = pending.pop()
                if reparse(path):
                    continue
                if path.is_dir():
                    pending.extend(path.iterdir())
                elif path.suffix not in {'.pyc', '.log'}:
                    files.append(path)
        else:
            files = [origin]
        for path in files:
            checksum = digest(path)
            if checksum in seen:
                continue
            target = local / 'data/imports/source-recovery' / (checksum + path.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if digest(target) != checksum:
                raise ValueError('源码侧唯一持久文件保全失败')
            records.append({'source': path.relative_to(ROOT).as_posix(), 'path': target.relative_to(local / 'data').as_posix(), 'sha256': checksum})
            seen.add(checksum)
    atomic_json(local / 'data/state/source-recovery.json', {'files': records, 'fields': {'files': '唯一源文件保全清单', 'source': '原来源', 'path': '迁移导入路径', 'sha256': '摘要'}})


def cleanup(apply=False):
    local = ROOT / '.local'
    require_stopped(ROOT, local)
    targets = [ROOT / p for p in LEGACY]
    for target in targets:
        if target == ROOT or not target.absolute().is_relative_to(ROOT):
            raise ValueError('清理目标越出当前工程')
        for relative in PROTECTED:
            protected = ROOT / relative
            if protected == target or protected.is_relative_to(target) or target.is_relative_to(protected):
                raise ValueError('清理目标与保护对象重叠')
    record = {'schema': 1, 'paths': [p.relative_to(ROOT).as_posix() for p in targets if p.exists()], 'completed': [],
              'fields': {'paths': '已规范化并核对保护对象的具体清退路径', 'completed': '已回收路径'}}
    if not apply:
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return
    verify_install(local)
    protect_source_data(local)
    backup_user_data(local)
    journal = local / 'data/state/layout-cleanup.json'
    atomic_json(journal, record)
    for target in targets:
        remove_tree(target, ROOT)
        record['completed'].append(target.relative_to(ROOT).as_posix())
        atomic_json(journal, record)
        print('已清退：' + target.relative_to(ROOT).as_posix(), flush=True)
    record['finished'] = True  # finished（具体清退路径全部回收）。
    atomic_json(journal, record)
    print('旧架构已清退，保护对象均保留；重要数据只有一份已验证滚动备份。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='完成新入口验收后清退旧架构；删除不跟随目录联接')
    parser.add_argument('--apply', action='store_true', help='apply（执行已核对的工程内清理）')
    apply = parser.parse_args().apply
    if apply:
        with update_lock(ROOT / '.local'):
            cleanup(True)
    else:
        cleanup(False)
