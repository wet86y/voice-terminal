"""开发机预制 AnifLive（本地合成）的少量源码依赖；目标设备只解包固定安装包。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    path = ROOT / 'config/runtime/aniflive-runtime.lock.json'
    lock = json.loads(path.read_text(encoding='utf-8'))
    output = ROOT / '.local/build/aniflive-runtime/wheels'
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / '.local/tmp/aniflive-wheel-build'
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'TEMP': str(scratch), 'TMP': str(scratch),
           'PIP_CACHE_DIR': str(ROOT / '.local/cache/pip')}
    for row in lock['wheels']:
        if not row.get('needs_build'):
            continue
        if row['name'] == 'tensorrt-cu12-libs':
            index = 'https://pypi.nvidia.com/tensorrt-cu12-libs/'
            with urlopen(index, timeout=45) as response:
                text = response.read().decode()
            wanted = f'tensorrt_cu12_libs-{row["version"]}-py3-none-win_amd64.whl'
            links = [link for link in re.findall('href="([^"]+)"', text) if link.split('#')[0].endswith(wanted)]
            if len(links) != 1 or '#sha256=' not in links[0]:
                raise RuntimeError('NVIDIA（英伟达）包源没有唯一固定推理运行库。')
            url, sha = urljoin(index, links[0]).split('#sha256=')
            with urlopen(Request(url, method='HEAD'), timeout=45) as response:
                size = int(response.headers['Content-Length'])
            row.update(file=wanted, url=url, size=size, sha256=sha)
        else:
            source = row['sources'][0]
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--disable-pip-version-check',
                            '--no-deps', '--no-build-isolation', '--wheel-dir', str(output),
                            source['url'] + '#sha256=' + source['digests']['sha256']],
                           env=env, cwd=ROOT, check=True)
            pattern = row['name'].replace('-', '_') + '-' + row['version'] + '-*.whl'
            candidates = list(output.glob(pattern))
            if len(candidates) != 1:
                raise RuntimeError('开发机预编译包产物不唯一。')
            wheel = candidates[0]
            with wheel.open('rb') as stream:
                sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            row.update(file=wheel.name, url='', size=wheel.stat().st_size, sha256=sha,
                       bundled='tools/aniflive-wheels/' + wheel.name,
                       provenance={'source': source, 'python': sys.version})
        row.pop('needs_build')
        path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('已准备固定依赖：' + row['name'], flush=True)


if __name__ == '__main__':
    main()
