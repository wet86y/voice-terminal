"""Export（明确交付导出）：仅最新整包，排除用户数据与构建缓存。"""
from pathlib import Path
import argparse
import importlib.util
import json
import os
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import verify_install, require_stopped
from voice_terminal.local_layout import read_portable
from voice_terminal.local_update import update_lock, recover
from voice_terminal.local_files import remove_tree
from voice_terminal.paths import atomic_json


def export(root: Path, output: Path) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.parent != root / 'releases':
        raise ValueError('交付导出只能放入本机 releases（最新交付目录）')
    with update_lock(root):
        if (root / 'tmp/update-transaction.json').exists():
            require_stopped(ROOT, root)
        recover(root)
        selected = read_portable(root)
        verify_install(root)
        release = json.loads((root / 'app/release.json').read_text(encoding='utf-8-sig'))
        spec = importlib.util.spec_from_file_location('assemble_desktop', ROOT / 'scripts/assemble-desktop.py')
        assembler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assembler)
        published = ['app', *(row['path'] for row in release['components']), *(row['path'] for row in release['root_files']), 'portable.json']
        if (root / '使用与环境说明.md').exists() and '使用与环境说明.md' not in published:
            published.append('使用与环境说明.md')
        temporary = root / 'tmp/export-latest.zip'
        remove_tree(temporary, root)
        try:
            record = assembler.archive(root, temporary, include_roots=published)
            output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, output)
            record['file'] = output.name
            atomic_json(output.parent / 'downloads.json', {'schema': 1, 'archives': [record], 'fields': {'file': '最新交付文件', 'size': '字节数', 'sha256': '内容摘要'}})
        finally:
            temporary.unlink(missing_ok=True)
        return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='明确导出最新交付包，覆盖同名文件；常规发布不调用本工具')
    parser.add_argument('--root', type=Path, default=ROOT / '.local', help='root（本机安装根）')
    parser.add_argument('--output', type=Path, help='output（最新交付包）')
    args = parser.parse_args()
    record = export(args.root, args.output or args.root / 'releases/VoiceTerminal-latest.zip')
    print(f'最新交付包已覆盖并记录摘要：{record["file"]}；真实用户数据已排除。')
