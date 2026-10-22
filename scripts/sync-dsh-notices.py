"""DSH notices（代理宿主第三方声明）：保留锁定上游正文，仅规范跨仓库相对链接。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def synchronize(source: Path, check: bool) -> dict:
    lock = json.loads((ROOT / 'config/dsh/runtime.lock.json').read_text(encoding='utf-8'))
    repository = lock['repository'].removesuffix('.git')
    commit = lock['commit']
    if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+', repository) or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('上游仓库或锁定提交格式无效，拒绝生成不确定来源的链接。')

    def git(*args: str) -> bytes:
        return subprocess.check_output(['git', '-C', str(source), *args], stderr=subprocess.PIPE)

    original = git('show', f'{commit}:THIRD_PARTY_NOTICES.md')
    destination = ROOT / 'config/dsh/licenses'
    if (destination / 'LICENSE').read_bytes() != git('show', f'{commit}:LICENSE'):
        raise ValueError('许可证快照与锁定上游不一致，请审阅后单独更新；本工具不改写许可证正文。')
    links = []

    def replace(match: re.Match) -> str:
        value = match.group(1)
        address = urlsplit(value)
        if address.scheme or address.netloc or not address.path or value == 'LICENSE':
            return match.group(0)
        path = unquote(address.path).rstrip('/')
        if path.startswith('/') or '..' in PurePosixPath(path).parts or '\\' in path or address.query:
            raise ValueError(f'上游相对引用需要人工确认：{value}')
        # 直接核对提交中的对象，不受源码缓存当前分支及未提交改动影响。
        kind = git('cat-file', '-t', f'{commit}:{path}').decode().strip()
        if kind not in {'blob', 'tree'}:
            raise ValueError(f'上游引用不是文件或目录：{value}')
        route = 'tree' if kind == 'tree' else 'blob'
        target = f'{repository}/{route}/{commit}/{quote(path, safe="/@")}'
        if kind == 'tree':
            target += '/'
        if address.fragment:
            target += '#' + address.fragment
        links.append({'original': value, 'target': target})
        return f']({target})'

    body = re.sub(r'\]\(([^)\s]+)\)', replace, original.decode('utf-8'))
    digest = hashlib.sha256(original).hexdigest()
    header = (f'<!-- 本项目由 scripts/sync-dsh-notices.py（第三方声明同步工具）生成。\n'
              f'来源：{repository}/blob/{commit}/THIRD_PARTY_NOTICES.md\n'
              f'上游原文 SHA-256（内容摘要）：{digest}\n'
              '仅将上游仓库内相对链接改为固定提交链接；下方上游生成命令适用于上游仓库。 -->\n\n')
    expected = (header + body).encode('utf-8')
    output = destination / 'THIRD_PARTY_NOTICES.md'
    if check:
        if output.read_bytes() != expected:
            raise ValueError('第三方声明与锁定来源或规范化链接不一致，请先运行本工具同步。')
    else:
        output.write_bytes(expected)
    return {'commit': commit, 'source_sha256': digest, 'rewritten_links': len(links), 'check_only': check,
            'fields': {'commit': '锁定上游提交', 'source_sha256': '上游原文内容摘要',
                       'rewritten_links': '已核对并转换的相对引用数量', 'check_only': '是否只检查而不写入'}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / '.local/build/dsh/source', help='已包含锁定提交的上游源码缓存；不自动下载或切换分支。')
    parser.add_argument('--check', action='store_true', help='只核对许可证、来源和转换结果，不写文件。')
    args = parser.parse_args()
    try:
        print(json.dumps(synchronize(args.source, args.check), ensure_ascii=False))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'第三方声明同步失败：{error}\n')


if __name__ == '__main__':
    main()
