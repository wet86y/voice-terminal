"""锁定可选 GPU（图形处理器）依赖；只查询官方摘要与大小，不下载大型包。"""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import unquote
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def resolve(name: str, version: str) -> dict:
    """resolve（解析官方包）：固定 Windows（操作系统）与 CPython（解释器）3.12 架构。"""
    profile = version.split('+')[1]
    with urlopen(f'https://download.pytorch.org/whl/{profile}/{name}/', timeout=30) as response:
        index = response.read().decode()
    filename = f'{name}-{version}-cp312-cp312-win_amd64.whl'
    matches = [url for url in re.findall(r'href="([^"]+)"', index)
               if unquote(url.split('#')[0]).endswith(filename)]
    if len(matches) != 1:
        raise ValueError(f'固定显卡依赖没有唯一官方包：{filename}')
    url = matches[0].replace('https://download-r2.pytorch.org/', 'https://download.pytorch.org/')
    digest = url.split('#sha256=')[-1]
    if not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('官方显卡依赖缺少 SHA-256（安全散列摘要）')
    url = url.split('#')[0]
    from urllib.request import Request
    with urlopen(Request(url, method='HEAD'), timeout=30) as response:
        size = int(response.headers['Content-Length'])
    return dict(name=name, version=version, file=filename, url=url, size=size, sha256=digest)


if __name__ == '__main__':
    path = ROOT / 'config/runtime/voice-gpu.lock.json'
    record = json.loads(path.read_text('utf-8'))
    base = json.loads((path.parent / 'voice-runtime.lock.json').read_text('utf-8'))
    versions = {row['name']: row['version'].split('+')[0] for row in base['wheels']}
    record['wheels'] = [resolve(name, versions[name] + '+' + record['profile']) for name in ('torch', 'torchaudio')]
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('已更新两项固定显卡依赖，不改变 CPU（中央处理器）基础依赖锁。')
