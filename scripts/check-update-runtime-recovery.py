"""Runtime recovery（运行环境恢复实机验证）：大环境只重命名，不复制。"""
from pathlib import Path
import json
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, require_stopped
from voice_terminal.local_files import persistent_files, remove_tree
from voice_terminal.local_update import install, recover, update_lock
from voice_terminal.paths import atomic_json


def snapshot(local):
    return {'data': {p.relative_to(local / 'data').as_posix(): digest(p) for p in persistent_files(local / 'data')},
            'components': {name: digest(local / name / 'manifest.json') for name in ('runtimes/python', 'runtimes/dsh', 'runtimes/webview2', 'models')},
            'portable': digest(local / 'portable.json'), 'release': digest(local / 'app/release.json')}


def main():
    local = ROOT / '.local'
    require_stopped(ROOT, local)
    report = local / 'cache/acceptance/runtime-recovery.json'
    if sys.argv[1] == 'verify':
        before = json.loads(report.read_text(encoding='utf-8'))
        assert before['before'] == snapshot(local), '真实环境或数据恢复后不一致'
        assert not (local / 'tmp/update-transaction.json').exists()
        assert not (local / 'tmp/publish').exists()
        before['restored'] = True
        atomic_json(report, before)
        print('真实运行环境更新中断恢复通过；环境、程序配置及全部持久数据保持原摘要。')
        return
    assert not Path(sys.executable).resolve().is_relative_to(local / 'runtimes/python'), '不能用待换入环境执行恢复验证'
    with update_lock(local):
        recover(local)
        stage = local / 'tmp/publish'
        remove_tree(stage, local)
        candidate = stage / 'new/runtimes/python'
        candidate.mkdir(parents=True)
        (candidate / 'interruption-probe.txt').write_text('仅用于首个重命名后的中断检查，不换入业务环境', encoding='utf-8')
        atomic_json(report, {'before': snapshot(local), 'restored': False,
                            'fields': {'before': '中断前组合与持久文件摘要', 'restored': '从真实中断恢复并核对成功',
                                       'data': '持久数据摘要', 'components': '组件清单摘要', 'portable': '唯一配置摘要', 'release': '程序清单摘要'}})
        def interrupt(step):
            if step == 'renamed:runtimes/python':
                raise SystemExit(71)  # 71（已记录事务并在重命名后主动中断的验收状态）。
        install(local, ['runtimes/python'], stopped=lambda: require_stopped(ROOT, local), checkpoint=interrupt)
    raise RuntimeError('未到达中断验证检查点')


if __name__ == '__main__':
    main()
