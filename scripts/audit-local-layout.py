"""Size audit（目录体积复核）：不跟随重解析点，统计逻辑文件字节。"""
from pathlib import Path
import json
import os
import stat

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())


def measure(root):
    result = {'bytes': 0, 'files': 0, 'directories': 0, 'reparse_points': 0, 'errors': []}
    stack = [str(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        result['reparse_points'] += 1
                    elif entry.is_dir(follow_symlinks=False):
                        result['directories'] += 1
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        result['files'] += 1
                        result['bytes'] += info.st_size
        except OSError as error:
            result['errors'].append({'path': str(Path(directory).relative_to(ROOT)), 'errno': error.errno})
    return result


def main():
    whole = measure(ROOT)
    areas = {}
    for relative in ('.local/app', '.local/runtimes/python', '.local/runtimes/dsh', '.local/runtimes/webview2', '.local/models',
                     '.local/data', '.local/backups/user-data', '.local/build', '.local/tools', '.local/cache', '.local/tmp', 'node_modules', '.git'):
        path = ROOT / relative
        if path.exists():
            areas[relative] = measure(path)
    report = {'total': whole, 'areas': areas, 'fields': {'total': '全工程逻辑文件体积', 'areas': '主要固定目录', 'bytes': '逻辑文件字节（硬链接各路径计数）',
             'files': '普通文件数', 'directories': '普通目录数', 'reparse_points': '只计联接本身而不遍历的重解析点数', 'errors': '读取失败项', 'path': '路径', 'errno': '错误编号'}}
    destination = ROOT / '.local/cache/acceptance/directory-size.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(bool(whole['errors']))


if __name__ == '__main__':
    main()
