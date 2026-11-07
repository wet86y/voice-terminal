"""从已验证开发环境解析 AnifLive（本地合成）依赖闭包及固定下载身份。"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from voice_terminal.tts.aniflive_environment import EXPORT_VERSIONS


def read_url(url):
    cache = ROOT / '.local/cache/aniflive-lock'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    if path.is_file():
        return path.read_bytes()
    for attempt in range(3):
        try:
            with urlopen(url, timeout=20) as response:
                data = response.read()
            path.write_bytes(data)
            return data
        except OSError:
            if attempt == 2:
                raise
            time.sleep(.3 * (attempt + 1))


def distributions(paths):
    result = {}
    for directory in paths:
        for package in metadata.distributions(path=[str(directory)]):
            result.setdefault(canonicalize_name(package.metadata['Name']), package)
    return result


def closure(roots, installed):
    pending = list(roots)
    found = {}
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        name = canonicalize_name(requirement.name)
        package = installed.get(name)
        if package is None or package.version not in requirement.specifier:
            raise ValueError(f'依赖闭包不满足：{requirement}，实际 {package.version if package else "未安装"}')
        if name in found:
            continue
        found[name] = package.version
        pending.extend(package.requires or [])
    return found


def resolve_artifact(item):
    name, version = item
    if name in {'torch', 'torchaudio'}:
        index_url = f'https://download.pytorch.org/whl/cu128/{name}/'
        index = read_url(index_url).decode()
        wanted = f'{name}-{version}-cp312-cp312-win_amd64.whl'
        links = [s for s in re.findall('href="([^"]+)"', index) if unquote(s.split('#')[0]).endswith(wanted)]
        if len(links) != 1:
            raise ValueError(f'缺少唯一官方显卡预编译包：{wanted}')
        url, sha = links[0].split('#sha256=')
        from urllib.request import Request
        with urlopen(Request(url, method='HEAD'), timeout=45) as response:
            size = int(response.headers['Content-Length'])
        return {'name': name, 'version': version, 'file': wanted, 'url': url, 'size': size, 'sha256': sha}
    data = json.loads(read_url(f'https://pypi.org/pypi/{name}/{version}/json'))
    tags = set(sys_tags())
    candidates = []
    for row in data['urls']:
        if row['packagetype'] != 'bdist_wheel' or row.get('yanked'):
            continue
        if tags.intersection(parse_wheel_filename(row['filename'])[3]):
            candidates.append(row)
    candidates.sort(key=lambda row: (not row['filename'].endswith('win_amd64.whl'), row['filename']))
    if not candidates:
        sources = [{k: row[k] for k in ('filename', 'url', 'size', 'digests')} for row in data['urls'] if row['packagetype'] == 'sdist']
        return {'name': name, 'version': version, 'needs_build': True, 'sources': sources}
    row = candidates[0]
    return {'name': name, 'version': version, 'file': row['filename'], 'url': row['url'],
            'size': row['size'], 'sha256': row['digests']['sha256']}


def resolve(item):
    try:
        row = resolve_artifact(item)
        print(f'已解析依赖：{item[0]} {item[1]}', flush=True)
        return row
    except Exception as error:
        print(f'依赖来源未解析：{item[0]} {item[1]}：{error}', flush=True)
        return {'name': item[0], 'version': item[1], 'resolution_error': str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-site', type=Path, required=True, help='runtime-site（已验证推理依赖目录）')
    parser.add_argument('--export-site', type=Path, required=True, help='export-site（导出专用覆盖目录）')
    args = parser.parse_args()
    source = ROOT / '.local/runtimes/aniflive-tts/source'
    roots = []
    for relative in ('requirements/base.txt', 'requirements/torch-cu128.txt', 'requirements/tensorrt11-cu12.txt'):
        roots.extend(line.strip() for line in (source / relative).read_text(encoding='utf-8').splitlines()
                     if line.strip() and not line.lstrip().startswith(('#', '--')))
    runtime = closure(roots, distributions([args.runtime_site]))
    export_roots = [f'{name}=={version}' for name, version in EXPORT_VERSIONS.items()]
    export = closure(export_roots, distributions([args.export_site, args.runtime_site]))
    identities = sorted(set(runtime.items()) | set(export.items()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        wheels = list(pool.map(resolve, identities))
    lock = {'schema': 1, 'runtime': runtime, 'export': export, 'wheels': wheels,
            'fields': {'schema': '依赖锁格式', 'runtime': '完整推理依赖闭包', 'export': '导出兼容闭包',
                       'wheels': '固定下载包', 'name': '包名称', 'version': '固定版本', 'file': '固定文件名',
                       'url': '官方固定下载地址', 'size': '包字节数', 'sha256': '包内容摘要',
                       'needs_build': '开发构建机需先准备预编译包，目标机器不得现场编译依赖',
                       'sources': '已固定的官方源码输入'}}
    destination = ROOT / 'config/runtime/aniflive-runtime.lock.json'
    destination.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'packages': len(wheels), 'needs_build': [r['name'] for r in wheels if r.get('needs_build')]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
