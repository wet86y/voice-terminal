"""Documentation links（文档链接）：检查工程内目标与标题锚点。"""
from pathlib import Path
import json
import re
import subprocess
import unicodedata
from urllib.parse import unquote, urlsplit

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())


def anchors(path):
    result = set()
    duplicates = {}
    text = re.sub(r'^```.*?^```[^\n]*', '', path.read_text(encoding='utf-8'), flags=re.M | re.S)
    for value in re.findall(r'^#+ (.*)$', text, re.M):
        value = value.lower().replace('`', '')
        slug = ''.join(c for c in value if c in '-_ ' or unicodedata.category(c)[0] in 'LN').replace(' ', '-')
        number = duplicates.get(slug, 0)
        result.add(slug + (f'-{number}' if number else ''))
        duplicates[slug] = number + 1
    return result


def main():
    tracked = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '*.md'], text=True, encoding='utf-8').splitlines()
    files = {ROOT / p for p in tracked if (ROOT / p).is_file()}
    files.update((ROOT / 'docs').glob('*.md'))
    files.discard(ROOT / 'docs/download-distribution-plan.md')  # 用户草稿原样保留，检查不改动。
    failures = []
    checked = 0
    for path in sorted(files):
        for value in re.findall(r'\]\(([^)\s]+)(?:\s+"[^"]*")?\)', path.read_text(encoding='utf-8')):
            address = urlsplit(value.strip('<>'))
            if address.scheme or value.startswith('/') or '<' in value or '$' in value:
                continue
            target = (path.parent / unquote(address.path)).resolve() if address.path else path
            checked += 1
            if not target.exists() or (address.fragment and target.is_file() and unquote(address.fragment) not in anchors(target)):
                failures.append({'source': path.relative_to(ROOT).as_posix(), 'target': value})
    report = {'checked': checked, 'failures': failures, 'fields': {'checked': '工程内链接检查数量', 'failures': '失效目标或锚点', 'source': '来源文档', 'target': '链接目标'}}
    destination = ROOT / '.local/cache/acceptance/doc-links.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(bool(failures))


if __name__ == '__main__':
    main()
