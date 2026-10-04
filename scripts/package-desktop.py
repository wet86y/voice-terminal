"""Package desktop（封装桌面包）：验收后生成完整包与独立组件包，永不遍历用户数据。"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import safe_path, verify_install
from voice_terminal.paths import atomic_json


def main():
    parser = argparse.ArgumentParser(description='校验发行目录并生成完整 ZIP（压缩包）与独立组件包')
    parser.add_argument('--root', type=Path, required=True, help='已组装、验收的应用根目录')
    parser.add_argument('--output', type=Path, required=True, help='完整压缩包文件；必须在应用目录之外且不能已存在')
    parser.add_argument('--components', type=Path, required=True, help='独立组件压缩包目录；不能已存在')
    args = parser.parse_args()
    root, output, components = args.root.resolve(strict=True), args.output.resolve(), args.components.resolve()
    if output.is_relative_to(root) or components.is_relative_to(root) or output.exists() or components.exists():
        raise ValueError('压缩产物必须放到发行目录之外的全新文件／目录，避免覆盖或将包收入自身。')
    verify_install(root)
    portable = json.loads((root / 'portable.json').read_text(encoding='utf-8-sig'))
    data = Path(portable['data_dir'])
    data = (data if data.is_absolute() else root / data).resolve()
    if any(path.is_relative_to(data) or data.is_relative_to(path) for path in (output, components)):
        raise ValueError('发布产物不能与用户数据目录重叠。')
    excluded = ()
    if data.is_relative_to(root):
        relative = data.relative_to(root)
        if not relative.parts or relative.parts[0] in {'apps', 'runtimes', 'models'}:
            raise ValueError('用户数据目录与只读发行内容重叠。')
        excluded = (relative,)
    active = data / 'state/active.json'
    selected = json.loads(active.read_text(encoding='utf-8-sig')) if active.is_file() else portable
    program = safe_path(root, selected['app'])
    release = json.loads((program / 'release.json').read_text(encoding='utf-8-sig'))
    module = importlib.util.spec_from_file_location('assemble_desktop', ROOT / 'scripts/assemble-desktop.py')
    assembler = importlib.util.module_from_spec(module)
    module.loader.exec_module(assembler)
    output.parent.mkdir(parents=True, exist_ok=True)
    components.mkdir(parents=True)
    print('整包离线验证通过，开始封装程序、运行环境与模型；用户数据已排除。', flush=True)
    # 只归档清单声明的程序、组件与根入口；未知目录即使出现在根目录也不收入发布包。
    # 新用户没有 active.json（本地启用记录），因此便携默认组合必须采用本次已验证的实际组合。
    # 本地记录可以有其他诊断／用户字段，发布配置只收入已定义的组件选择。
    selection_keys = ('schema', 'app', 'python', 'models', 'webview2', 'dsh', 'node', 'external_python', 'external_models')
    published_selection = {key:selected[key] for key in selection_keys if key in selected}
    published_portable = {'data_dir':'data', **published_selection}
    published = [selected['app'], *(item['path'] for item in release['components']),
                 *(item['path'] for item in release['root_files']), 'portable.json']
    if (root / '使用与环境说明.md').is_file():
        published.append('使用与环境说明.md')
    archives = [assembler.archive(root, output, include_roots=published,
                overrides={'portable.json':json.dumps(published_portable, ensure_ascii=False, indent=2).encode('utf-8')})]
    archives.append(assembler.archive(program, components / f"app-{release['build']}.zip"))
    for component in release['components']:
        directory = safe_path(root, component['path'])
        archives.append(assembler.archive(directory, components / f"{component['kind']}-{component['build']}.zip"))
    atomic_json(components / 'downloads.json', {'schema': 1, 'archives': archives, 'signed': False,
                'fields': {'file': '文件名称', 'size': '压缩包字节大小', 'sha256': '压缩包摘要', 'signed': '发布签名已生成；本轮为否'}})
    print(f'完整包及组件包封装完成：{output}', flush=True)


if __name__ == '__main__':
    main()
