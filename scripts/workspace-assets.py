"""Workspace assets（工作区资产）：提交检查、预算告警与已登记现场回收。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.workspace_assets import guarded, measure, policy, sweep_tasks, write_record


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments])


def check_index(root: Path = ROOT, *, strict: bool = False) -> dict:
    """check_index（暂存区检查）：校验对象本身，不读取未暂存的大文件替代内容。"""
    policy_name = 'config/development-assets.json'
    deleted = git(root, 'diff', '--cached', '--name-only', '--diff-filter=D', '-z').split(b'\0')
    if policy_name.encode() in deleted:
        return {'ok': False, 'changed_files': 1, 'changed_bytes': 0,
                'errors': [{'path': policy_name, 'reason': '开发入口依赖资产策略，禁止在普通修订中删除'}],
                'fields': {'ok': '暂存内容满足策略', 'changed_files': '违规路径数', 'changed_bytes': '尚未累计对象大小',
                           'errors': '提交拒绝项', 'path': '暂存路径', 'reason': '拒绝原因'}}
    staged_policy = subprocess.run(['git', '-C', str(root), 'show', ':' + policy_name], capture_output=True)
    # 资源、摘要和策略可原子修订；只采用暂存策略，不读取未暂存的放宽规则。
    rules = (json.loads(staged_policy.stdout) if staged_policy.returncode == 0 else policy(root))['commit']
    changed = {name.decode('utf-8') for name in git(root, 'diff', '--cached', '--name-only',
               '--diff-filter=ACMRT', '-z').split(b'\0') if name}
    errors = []
    warnings = []
    ignored = subprocess.run(['git', '-C', str(root), 'check-ignore', '--no-index', '-z', '--stdin'],
                             input=b'\0'.join(name.encode('utf-8') for name in changed) + (b'\0' if changed else b''),
                             capture_output=True)
    if ignored.returncode not in {0, 1}:
        raise RuntimeError('无法复核暂存路径的忽略规则')
    ignored_names = {name.decode('utf-8') for name in ignored.stdout.split(b'\0') if name}
    tracked = subprocess.run(['git', '-C', str(root), 'ls-tree', '-r', '--name-only', '-z', 'HEAD'], capture_output=True)
    known_names = {name.decode('utf-8') for name in tracked.stdout.split(b'\0') if name} if tracked.returncode == 0 else set()
    seen = set()
    changed_bytes = 0
    entries = [entry for entry in git(root, 'ls-files', '--stage', '-z').split(b'\0') if entry]
    oids = {entry.split(b'\t', 1)[0].split()[1] for entry in entries
            if entry.split(b'\t', 1)[1].decode('utf-8') in changed}
    sizes = {}
    if oids:
        output = subprocess.check_output(['git', '-C', str(root), 'cat-file', '--batch-check=%(objectname) %(objectsize)'],
                                         input=b'\n'.join(sorted(oids)) + b'\n')
        sizes = {oid.decode('ascii'): int(size) for oid, size in (line.split() for line in output.splitlines())}
    for entry in entries:
        if not entry:
            continue
        metadata, raw_name = entry.split(b'\t', 1)
        name = raw_name.decode('utf-8')
        if name not in changed:
            continue
        mode, oid, stage = metadata.decode('ascii').split()
        if stage != '0':
            errors.append({'path': name, 'reason': '存在未合并的暂存项'})
            continue
        path = PurePosixPath(name)
        asset = any(name.startswith(prefix + '/') for prefix in rules['asset_roots'])
        reason = None
        ignored_addition = name in ignored_names and name not in known_names and not asset
        if ignored_addition or any(part.lower() in rules['blocked_parts'] for part in path.parts) or path.parts[0].lower() in rules['blocked_roots']:
            reason = '生成物、开发依赖或真实数据禁止进入提交'
        elif mode not in {'100644', '100755'}:
            reason = '新增或变更的联接及嵌套仓库需要单独设计，禁止随源码提交'
        elif path.name.startswith('.env') and path.name != '.env.example':
            reason = '本机环境配置禁止进入提交'
        elif not asset and path.suffix.lower() in rules['blocked_suffixes']:
            reason = '二进制或生成物应使用外部资源目录和摘要，不进入普通源码路径'
        if reason:
            errors.append({'path': name, 'reason': reason})
            continue
        size = sizes[oid]
        pinned = rules['pinned_assets'].get(name)
        exempt = False
        if pinned:
            content_hash = hashlib.sha256(git(root, 'cat-file', 'blob', oid)).hexdigest()
            exempt = bool(pinned.get('reason')) and content_hash == pinned['sha256']
            if not exempt:
                warnings.append({'path': name, 'reason': '固定资产内容已变更，请与资源目录、许可证和摘要共同审查'})
        if not exempt:
            if size > rules['max_file_bytes']:
                warnings.append({'path': name, 'reason': f'单文件超过 {rules["max_file_bytes"]} 字节建议值'})
            if oid not in seen:
                changed_bytes += size
                seen.add(oid)
    if changed_bytes > rules['max_changed_bytes']:
        warnings.append({'path': '[staged]', 'reason': f'提交内容 {changed_bytes} 字节超过 {rules["max_changed_bytes"]} 字节建议值'})
    return {'ok': not errors and not (strict and warnings), 'changed_files': len(changed), 'changed_bytes': changed_bytes, 'errors': errors, 'warnings': warnings,
            'fields': {'ok': '暂存内容满足资产策略', 'changed_files': '新增或修改的暂存路径数',
                       'changed_bytes': '除固定摘要例外外的不同暂存内容对象总字节', 'errors': '明确的误提交路径，阻止提交',
                       'warnings': '大小、资源摘要建议，默认不阻止，显式严格模式才阻止',
                       'path': '暂存路径', 'reason': '违反规则及处理提示'}}


def audit(root: Path = ROOT, area: str | None = None) -> dict:
    rows = []
    budgets = policy(root)['budgets']
    if area is not None:
        budgets = {area: budgets[area]}
    for relative, budget in budgets.items():
        try:
            row = measure(guarded(root, relative))
            row.update(path=relative, budget_bytes=budget, over_budget=row['bytes'] > budget)
        except (OSError, ValueError) as error:
            row = {'path': relative, 'errors': [str(error)], 'over_budget': False}
        rows.append(row)
    return {'areas': rows, 'ok': not any(row['errors'] for row in rows),
            'over_budget': any(row['over_budget'] for row in rows), 'fields': {
                'areas': '固定资产区域；只读审计，不自动回收', 'ok': '目录均可安全读取',
                'over_budget': '超过逻辑体积告警线', 'path': '相对仓库路径', 'budget_bytes': '逻辑字节告警线',
                'bytes': '按路径累计字节，硬链接重复计数，不是可释放空间', 'files': '普通文件数',
                'reparse_points': '跳过的重解析点数', 'errors': '读取失败或安全拒绝'}}


def install_hook(root: Path = ROOT):
    if not guarded(root, '.githooks/pre-commit').is_file():
        raise RuntimeError('缺少仓库提交检查钩子，请先加载完整治理源码')
    previous = subprocess.run(['git', '-C', str(root), 'config', '--get', 'core.hooksPath'], capture_output=True, text=True)
    if previous.returncode not in {0, 1}:
        raise RuntimeError('无法读取现有提交钩子配置')
    if previous.stdout.strip() not in {'', '.githooks'}:
        raise RuntimeError('已有自定义提交钩子，请将资产检查接入现有流程，不自动覆盖')
    if not previous.stdout.strip():
        hooks = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks').decode().strip())
        if (hooks / 'pre-commit').exists():
            raise RuntimeError('已有提交前钩子，不自动覆盖；可手动调用 check-index（暂存检查）')
    subprocess.run(['git', '-C', str(root), 'config', '--local', 'core.hooksPath', '.githooks'], check=True)
    print('已启用仓库提交前资产检查；没有改写暂存内容。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['audit', 'check-index', 'prune', 'install-hook'],
                        help='audit（预算审计）、check-index（暂存检查）、prune（登记现场回收）、install-hook（启用提交检查）')
    parser.add_argument('--apply', action='store_true', help='apply（实际回收）；省略时仅列候选')
    parser.add_argument('--strict', action='store_true', help='strict（容量或提交建议也返回失败，仅显式验收使用）')
    parser.add_argument('--area', help='area（仅审计策略中一个固定预算目录）')
    parser.add_argument('--brief', action='store_true', help='brief（输出简短检查摘要）')
    parser.add_argument('--hook', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.apply and args.action != 'prune':
        parser.error('--apply（执行回收）仅适用于 prune（登记现场回收）')
    if args.action == 'install-hook':
        install_hook()
        return
    if args.action == 'check-index':
        result = check_index(strict=args.strict)
    elif args.action == 'prune':
        result = sweep_tasks(apply=args.apply)
    else:
        result = audit(area=args.area)
        write_record(ROOT, '.local/cache/acceptance/asset-budget.json', result)
    if args.brief and args.action == 'check-index':
        for row in result.get('errors', []):
            print(f"禁止提交：{row['path']}：{row['reason']}")
        for row in result.get('warnings', []):
            print(f"提交提示：{row['path']}：{row['reason']}")
        if result['ok'] and not result.get('warnings'):
            print(f"资产检查通过，检查 {result['changed_files']} 个暂存路径。")
    elif args.brief and args.action == 'audit':
        for row in result['areas']:
            print(f"资产预算：{row['path']}；逻辑体积 {row.get('bytes', 0) / 1024**3:.2f} GiB（吉比字节）；"
                  + ('读取失败' if row['errors'] else '超过告警线，请执行资产审计' if row['over_budget'] else '预算内'))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = not result.get('ok', True) or (args.strict and result.get('over_budget', False))
    # 钩子用独立退出码确认“检查已运行且发现违规”，避免把解释器或导入失败误判为违规。
    raise SystemExit(3 if args.hook and failed else int(failed))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as error:
        print('资产检查不可用：' + str(error), file=sys.stderr)
        raise SystemExit(2)
