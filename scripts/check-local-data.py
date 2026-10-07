"""Data acceptance（数据完整性验收）：迁移摘要、身份与隔离备份恢复。"""
from pathlib import Path
import json
import shutil
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.distribution import digest, require_stopped
from voice_terminal.local_files import backup_user_data, remove_tree
from voice_terminal.local_update import update_lock
from voice_terminal.paths import atomic_json


def main():
    local = ROOT / '.local'
    require_stopped(ROOT, local)
    data = local / 'data'
    record = json.loads((data / 'state/layout-migration.json').read_text(encoding='utf-8'))
    normalized = {row['path']: row for row in record.get('normalizations', [])}
    changes = {
        'config/model-registry.json': '本机模型注册按唯一资源路径重新计算本机标识',
        'dsh/session-mappings.json': '同一会话恢复后更新激活时间',
        'state/webview2-access.json': '新渲染路径权限检查记录',
        'state/startup-result.json': '实机重启的进程身份与就绪记录',
    }
    backup = local / 'backups/user-data'
    old = json.loads((backup / 'dsh/session-mappings.json').read_text(encoding='utf-8-sig'))
    voice = json.loads((data / 'history/active-voice-session.json').read_text(encoding='utf-8-sig'))['voice_session_id']
    session_id = old['sessions'][voice]['dsh_session_id']
    active_cache = f'dsh/storages/session_projcache/sessions/{session_id}.json'
    active_log = next(p for p in (data / 'dsh/sessions').glob(f'*/{session_id}/session.v*.jsonl*')).relative_to(data).as_posix()
    changes[active_cache] = '原代理会话正常恢复后更新投影序号及收件箱'
    changes[active_log] = '原代理会话保留全部原记录并追加正常恢复事件'
    verified = []
    for relative, before in record['before'].items():
        path = data / relative
        if relative == 'state/active.json':
            # 旧组件选择属于明确退役的元数据，不能要求恢复为运行兼容副本。
            cleanup = json.loads((data / 'state/layout-cleanup.json').read_text(encoding='utf-8'))
            from voice_terminal.local_layout import read_portable
            assert read_portable(local)['schema'] == 2
            assert cleanup['finished'] and '.local/data/state/active.json' in cleanup['completed']
            assert not path.exists(), '旧启用副本不得重新成为运行来源'
            verified.append({'path': relative, 'before': before, 'after': None,
                             'reason': '按计划退役旧组件启用记录，迁移来源及原摘要保留在迁移记录'})
            continue
        assert path.is_file(), '原持久数据文件丢失：' + relative
        after = digest(path)
        expected = normalized.get(relative, {}).get('after', before)
        assert after == expected or relative in changes, '未经解释的持久文件变化：' + relative
        if relative in normalized:
            original = data / 'imports/layout-originals' / (before + '.json')
            assert original.is_file() and digest(original) == before, '路径归一化原记录未保全'
        verified.append({'path': relative, 'before': before, 'after': after,
                         'reason': changes.get(relative) if after != expected else ('仅路径归一化' if relative in normalized else '内容不变')})
    # 原会话绑定不变；时间戳属于正常运行状态。
    new = json.loads((data / 'dsh/session-mappings.json').read_text(encoding='utf-8-sig'))
    assert old['sessions'].keys() == new['sessions'].keys()
    for key, value in old['sessions'].items():
        assert value['dsh_session_id'] == new['sessions'][key]['dsh_session_id']
    assert digest(backup / 'history/active-voice-session.json') == digest(data / 'history/active-voice-session.json')
    probe = local / 'tmp/data-records.mjs'
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("""import {readSessionRecords,sessionFiles} from '../../scripts/dsh-diagnose/lib/dsh-paths.mjs';
import {join} from 'node:path';
const rel=process.argv[2];
const before=await readSessionRecords(join('.local/backups/user-data',rel));
const after=await readSessionRecords(join('.local/data',rel));
if(!before.every((r,i)=>JSON.stringify(r)===JSON.stringify(after[i]))) throw Error('原会话记录不完整');
let sessions=0,records=0;
for(const p of sessionFiles()){const r=await readSessionRecords(p);sessions++;records+=r.length;}
console.log(JSON.stringify({sessions,records,original_records:before.length,appended_records:after.length-before.length}));
""", encoding='utf-8')
    try:
        result = subprocess.run([str(local / 'runtimes/dsh/node.exe'), str(probe), active_log], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', check=True)
        sessions = json.loads(result.stdout)
    finally:
        remove_tree(probe, local)
    with update_lock(local):
        saved = backup_user_data(local)
        restored = local / 'tmp/restore-user-data'
        remove_tree(restored, local)
        shutil.copytree(backup, restored)
        assert all(digest(restored / row['path']) == row['sha256'] for row in saved['files'])
        assert digest(restored / 'history/active-voice-session.json') == digest(data / 'history/active-voice-session.json')
        remove_tree(restored, local)
    previous_path = data / 'state/layout-data-acceptance.json'
    previous = json.loads(previous_path.read_text(encoding='utf-8')) if previous_path.exists() else {}
    report = {'schema': 1, 'files': verified, 'sessions': sessions, 'first_sessions': previous.get('first_sessions', previous.get('sessions', sessions)), 'restored_files': len(saved['files']),
              'draft_sha256': digest(ROOT / 'docs/download-distribution-plan.md'),
              'fields': {'schema': '验收格式', 'files': '原持久文件逐项摘要', 'path': '相对数据路径', 'before': '迁移前摘要', 'after': '现用摘要',
                         'reason': '变化说明', 'sessions': '代理会话读取结果', 'first_sessions': '首次迁移验收会话记录证明', 'records': '读取记录数量', 'original_records': '原会话保留记录', 'appended_records': '正常恢复追加记录',
                         'restored_files': '隔离目录恢复并验证数量', 'draft_sha256': '保留且不提交的用户草稿摘要'}}
    atomic_json(data / 'state/layout-data-acceptance.json', report)
    print(f'原持久文件核对通过：{len(verified)}；代理会话：{sessions["sessions"]}；隔离恢复验证：{len(saved["files"])} 个文件。')


if __name__ == '__main__':
    main()
