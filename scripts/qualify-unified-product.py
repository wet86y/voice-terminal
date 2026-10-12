"""Unified product qualification（统一正式程序验收）：独立启动、同包摘要与保留数据重装。"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import shutil
import psutil

ROOT = Path(__file__).resolve().parents[1]
def load(path): return json.loads(path.read_text('utf-8-sig'))
def write(path, record): path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
def digest(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()
def until(test, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = test()
        if value: return value
        time.sleep(.15)
    raise TimeoutError('等待本轮正式进程或真实页面就绪超时')
def ready(root, pid):
    try:
        record = load(root / 'data/state/workspace-ready.json')
        return record if record['pid'] == pid and record['ready'] and record['role'] == 'product' else None
    except (OSError, ValueError, KeyError): return None
def stop(root, pid):
    process = psutil.Process(pid)
    expected = root / 'app/VoiceTerminalDesktop.exe'
    if Path(process.exe()).resolve() != expected.resolve(): raise ValueError('退出目标不属于本轮正式宿主')
    owned = [(p.pid, p.create_time()) for p in process.children(recursive=True)]
    subprocess.run([str(root / 'VoiceTerminal.exe'), '--exit'], cwd=os.environ['SYSTEMROOT'], check=True, timeout=25)
    process.wait(30)
    for child_pid, created in owned:
        try:
            child = psutil.Process(child_pid)
            if child.create_time() == created: child.wait(20)
        except psutil.NoSuchProcess: pass

def native_task(source, target, kind, environment, *, committed=False, expect_failure=False):
    """Native task（原生辅助事务）：使用正式入口，验收真实复制、挂载与恢复。"""
    target.mkdir(parents=True, exist_ok=True)
    if not (target / 'portable.json').exists():
        layout = load(source / 'portable.json')
        if kind == 'link': layout['workspace_id'] = uuid.uuid4().hex
        write(target / 'portable.json', layout)
    journal = target / 'tmp/workspace-task.json'
    record = {'schema':1, 'job_id':uuid.uuid4().hex, 'kind':kind, 'state':'committed' if committed else 'prepared',
              'parent_pid':0, 'helper_pid':0, 'source_root':str(source), 'target_root':str(target),
              'workspace_id':load(source/'portable.json')['workspace_id'], 'previous_locator':None,
              'registry_path':str(source/'tmp/workspace-identity.json'),
              'fields':{'schema':'原生事务格式','job_id':'本轮唯一任务编号','kind':'link（切换）或 migrate（整体迁移）','state':'模拟准备阶段或提交后中断','parent_pid':'原进程已正常退出','helper_pid':'由正式辅助程序登记','helper_create_time':'辅助进程创建时间','helper_executable':'辅助程序位置','target_pid':'目标桌面进程编号','target_create_time':'目标进程创建时间','target_executable':'目标宿主位置','source_root':'本轮独立原工作区','target_root':'本轮独立目标工作区','workspace_id':'迁移保留的编号','previous_locator':'不读取系统定位','registry_path':'本地事务身份','expected_core':'只等待真实页面，不等待模型','cleanup_started':'开始清理原受管内容','cleanup_finished':'完成原受管内容清理'}}
    write(journal, record)
    write(source/'data/state/workspace-task.json', record)
    helper = target/'tmp/workspace-helper.exe'
    shutil.copy2(source/'VoiceTerminal.exe', helper)
    if committed:
        # 从原根入口恢复，必须委托目标辅助程序后退出，避免清理时占用自身。
        subprocess.run([str(source/'VoiceTerminal.exe')], cwd=os.environ['SYSTEMROOT'], env=environment, check=True, timeout=25)
        process = None
    else:
        process = subprocess.Popen([str(helper),'--workspace-task',str(journal)], cwd=os.environ['SYSTEMROOT'], env=environment)
    final = until(lambda: value if (value:=load(journal))['state'] in {'completed','failed'} else None, 90)
    if expect_failure:
        assert final['state']=='failed'
        page = until(lambda: load(source/'data/state/workspace-ready.json') if (source/'data/state/workspace-ready.json').exists() else None)
        assert page['ready'] and page['management_ready']
        # 失败辅助程序停在原生错误提示；只结束本轮持有的进程句柄。
        if process and process.poll() is None: process.terminate();process.wait(20)
        return page['pid']
    assert final['state']=='completed'
    page = until(lambda: ready(target, final['target_pid']))
    assert page['management_ready']
    if process: process.wait(25);assert process.returncode==0
    assert not (source/'data/state/workspace-task.json').exists()
    return final['target_pid']

def cleanup_test_shortcuts(roots):
    """仅清理本轮独立工作区的受管快捷方式，核对目标与程序标记。"""
    commands = "$shell=New-Object -ComObject WScript.Shell; $desktop=[Environment]::GetFolderPath('Desktop'); $targets=@(" + ','.join("'"+str(root/'VoiceTerminal.exe').replace("'","''")+"'" for root in roots) + "); Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -File | ForEach-Object { $link=$shell.CreateShortcut($_.FullName); if ($link.Description -eq 'Voice Terminal · 受管工作区正式入口' -and $link.Arguments -eq '' -and $targets -contains $link.TargetPath) { Remove-Item -LiteralPath $_.FullName } }"
    subprocess.run(['powershell','-NoProfile','-NonInteractive','-Command',commands], check=True, capture_output=True)
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True, help='setup（带隔离验收功能的安装器）')
    args = parser.parse_args()
    build = load(ROOT / '.local/build/program-package/package-info.json')['build']
    sandbox = ROOT.parent / '测试' / ('统一正式程序验收-' + time.strftime('%Y%m%d-%H%M%S'))
    sandbox.mkdir(parents=True)
    root = sandbox / '中文 空格工作区'
    config = sandbox / 'qualification.json'
    report = sandbox / 'acceptance.json'
    write(config, {'target_root': str(root), 'shortcut': False, 'fields': {'target_root': '自动验收独立工作区', 'shortcut': '不改变真实桌面快捷方式'}})
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('VOICE_') and k.upper() not in {'PYTHONPATH','PYTHONHOME','WEBVIEW2_BROWSER_EXECUTABLE_FOLDER'}}
    results = {'build': build, 'workspace': str(root), 'checks': [], 'fields': {'build': '同一正式包构建编号', 'workspace': '无源码与运行环境的隔离工作区', 'checks': '实际运行验收项目', 'ok':'全部隔离验收通过'}}
    def install():
        result_path = sandbox / 'installer-result.json'
        result_path.unlink(missing_ok=True)
        process = subprocess.Popen([str(args.setup.resolve()), '--qualification-config', str(config)], cwd=os.environ['SYSTEMROOT'], env=environment)
        result = until(lambda: load(result_path) if result_path.is_file() else None, 90)
        if not result.get('ok'): raise AssertionError(result)
        process.wait(30)
        if process.returncode: raise AssertionError('安装器未成功退出')
        pid = result['product_pid']
        page = until(lambda: ready(root, pid))
        assert page['management_ready']
        assert psutil.pid_exists(pid)
        assert Path(psutil.Process(pid).exe()).resolve() == (root / 'app/VoiceTerminalDesktop.exe').resolve()
        return pid
    pid = install()
    results['checks'].append('安装器退出后正式宿主独立存活；没有后端环境，真实组件管理页已挂载')
    occupied = subprocess.run([str(ROOT/'.local/build/desktop/product/release/VoiceTerminal.exe'), '--install-package', str(ROOT/'.local/build/program-package/program.zip'), '--target-root', str(root)], cwd=os.environ['SYSTEMROOT'], env=environment, capture_output=True, timeout=25)
    assert occupied.returncode != 0 and '正在运行' in occupied.stderr.decode('utf-8', errors='replace')
    assert psutil.pid_exists(pid)
    results['checks'].append('目标真实正式程序运行时拒绝重装，原实例保持运行')
    assert not (root / 'pyproject.toml').exists() and not (root / 'node_modules').exists() and not (root / 'runtimes/python/python.exe').exists()
    payload = ROOT / '.local/build/program-package/payload'
    count = 0
    for row in load(payload / 'program-package.json')['files']:
        relative = row['path']
        if relative != 'app/release.json':
            assert digest(root / relative) == row['sha256'], relative
            assert digest(ROOT / '.local' / relative) == row['sha256'], '本地发布差异：' + relative
        count += 1
    results['checks'].append(f'安装器释放的 {count} 个固定文件与统一程序包一致；本地正式本体逐文件一致，组件登记单独核验')
    stop(root, pid)
    workspace_id = load(root / 'portable.json')['workspace_id']
    preserved = ['data/settings/用户设置.json', 'data/history/会话.txt', 'models/已安装模型.bin', 'runtimes/python/已安装环境.bin', 'runtimes/dsh/已安装代理.bin', 'data/speaker/campplus-zh-cn-common/保留新档案.txt']
    for relative in preserved:
        path = root / relative;path.parent.mkdir(parents=True, exist_ok=True);path.write_text('保留内容：' + relative, 'utf-8')
    hashes = {relative: digest(root / relative) for relative in preserved}
    legacy_weights=root/'models/speaker/wespeaker-cnceleb-resnet34';legacy_weights.mkdir(parents=True)
    (legacy_weights/'cnceleb_resnet34.onnx').write_bytes(b'legacy-model-fixture')
    legacy_profile=root/'data/speaker';(legacy_profile/'templates').mkdir()
    write(legacy_profile/'registry.json',{'model_id':'wespeaker-cnceleb-resnet34','schema_version':2})
    (legacy_profile/'templates/legacy.npy').write_bytes(b'legacy-profile-fixture');(legacy_profile/'probe.npy').write_bytes(b'legacy-probe-fixture')
    release = load(root / 'app/release.json')
    bindings = [{'kind':'python','path':'runtimes/python','build':'preserved','manifest_sha256':'qualification'}]
    release['components'] = bindings;write(root / 'app/release.json', release)
    # 模拟用户退出未完成迁移后，目标目录已移除；原目录引用保留供诊断，不能阻止重装。
    abandoned_target = sandbox/'已移除的 迁移目标'
    abandoned_marker = root/'data/state/workspace-task.json'
    write(abandoned_marker, {'schema':1, 'job_id':uuid.uuid4().hex, 'kind':'migrate', 'state':'prepared',
                            'source_root':str(root), 'target_root':str(abandoned_target), 'workspace_id':workspace_id,
                            'helper_pid':0, 'registry_path':str(root/'tmp/workspace-identity.json'),
                            'fields':{'schema':'事务格式','job_id':'残留迁移编号','kind':'迁移操作','state':'退出后未提交',
                                      'source_root':'有效原目录','target_root':'已移除目标','workspace_id':'原工作区编号',
                                      'helper_pid':'没有运行中的辅助程序','registry_path':'原事务身份'}})
    abandoned_hash = digest(abandoned_marker)
    # 同版本主动损坏本体：安装器必须重新释放，不依赖构建编号是否相同。
    (root / 'app/config/default.yaml').write_bytes(b'broken-body')
    pid = install()
    assert digest(root / 'app/config/default.yaml') == digest(payload / 'app/config/default.yaml')
    assert load(root / 'portable.json')['workspace_id'] == workspace_id
    installed_bindings=load(root / 'app/release.json')['components']
    assert [row for row in installed_bindings if row['kind']!='models'] == bindings
    model_binding=next(row for row in installed_bindings if row['kind']=='models')
    assert model_binding['manifest_sha256']==digest(root/'models/manifest.json')
    assert not legacy_weights.exists() and not (legacy_profile/'registry.json').exists() and not (legacy_profile/'templates').exists() and not (legacy_profile/'probe.npy').exists()
    weight=root/'models/speaker/campplus-zh-cn-common/campplus_cn_common.bin'
    assert digest(weight)=='3388cf5fd3493c9ac9c69851d8e7a8badcfb4f3dc631020c4961371646d5ada8'
    for relative, expected in hashes.items(): assert digest(root / relative) == expected, relative
    assert digest(abandoned_marker) == abandoned_hash and not abandoned_target.exists()
    results['checks'].append('同版本损坏修复保留工作区编号、用户设置、会话、模型、运行环境和组件登记')
    results['checks'].append('重装离线安装官方CAM++并删除旧WeSpeaker模型与根档案，保留新模型档案并同步模型清单及组件登记')
    results['checks'].append('退出迁移后目标事务已移除，原目录可重装和启动，旧引用及用户数据保留且不重建旧目标')
    stop(root, pid)
    # 从另一个启动工作目录再次启动正式入口。
    (root / 'data/state/workspace-ready.json').unlink(missing_ok=True)
    subprocess.run([str(root / 'VoiceTerminal.exe')], cwd=os.environ['SYSTEMROOT'], env=environment, check=True, timeout=25)
    page = until(lambda: load(root / 'data/state/workspace-ready.json') if (root / 'data/state/workspace-ready.json').exists() else None)
    assert page['management_ready'] and page['workspace_id'] == workspace_id
    stop(root, page['pid'])
    results['checks'].append('中文及空格目录、不同启动工作目录、安装器退出后的再次独立启动与正常退出通过')
    # 空工作区初始化与已有工作区切换均调用正式入口的辅助事务。
    empty = sandbox/'新的 空工作区'
    pid = native_task(root, empty, 'link', environment)
    assert all(row['kind']=='models' for row in load(empty/'app/release.json')['components'])
    assert not (empty/'data/settings/用户设置.json').exists() and not (empty/'models/已安装模型.bin').exists()
    stop(empty, pid)
    pid = native_task(empty, root, 'link', environment)
    assert load(root/'portable.json')['workspace_id']==workspace_id
    for relative, expected in hashes.items(): assert digest(root/relative)==expected
    stop(root, pid)
    results['checks'].append('空工作区从已发布本体初始化，不复制数据或可选组件；切换回已有工作区，数据及编号保留')
    # 整体迁移：保留非受管内容，只在目标真实页面挂载后清理原受管文件。
    unmanaged = root/'非受管文件.txt';unmanaged.write_text('保留原位置的非受管内容','utf-8')
    moved = sandbox/'整体迁移 目标'
    pid = native_task(root, moved, 'migrate', environment)
    assert unmanaged.is_file() and not (root/'portable.json').exists() and not (root/'app').exists()
    assert load(moved/'portable.json')['workspace_id']==workspace_id
    for relative, expected in hashes.items(): assert digest(moved/relative)==expected
    stop(moved, pid)
    results['checks'].append('整体迁移完成目标页面验证、快捷方式改指与原受管内容清理；非受管文件、用户内容及编号正确保留')
    # 目录直接搬迁后，入口不依赖旧位置或源码祖先。
    relocated = sandbox/'直接搬迁 中文目录'
    shutil.move(moved, relocated)
    subprocess.run([str(relocated/'VoiceTerminal.exe')], cwd=os.environ['SYSTEMROOT'], env=environment, check=True, timeout=25)
    page = until(lambda: load(relocated/'data/state/workspace-ready.json') if (relocated/'data/state/workspace-ready.json').exists() else None)
    assert page['workspace_id']==workspace_id and page['management_ready'];stop(relocated,page['pid'])
    results['checks'].append('整体目录手工搬迁后，无参数正式入口直接启动成功')
    # 已提交后中断恢复：目标已存在，但原内容尚未清理。
    recovery_source = sandbox/'恢复 原工作区';recovery_target = sandbox/'恢复 目标工作区'
    package = ROOT/'.local/build/program-package/program.zip'
    for candidate in (recovery_source,recovery_target):
        subprocess.run([str(relocated/'VoiceTerminal.exe'),'--install-package',str(package),'--target-root',str(candidate)],check=True,timeout=45,capture_output=True)
    shared = load(recovery_source/'portable.json')['workspace_id'];layout=load(recovery_target/'portable.json');layout['workspace_id']=shared;write(recovery_target/'portable.json',layout)
    for candidate in (recovery_source,recovery_target):write(candidate/'data/config/恢复设置.json',{'value':'原数据'})
    pid = native_task(recovery_source,recovery_target,'migrate',environment,committed=True)
    assert not (recovery_source/'portable.json').exists() and load(recovery_target/'data/config/恢复设置.json')['value']=='原数据'
    stop(recovery_target,pid)
    results['checks'].append('已提交后中断，从原正式入口委托目标辅助程序恢复，确认页面后完成清理')
    # 不明依赖联接导致复制失败：原数据保留，原正式管理页重新启动。
    failure_target=sandbox/'失败 目标';external=sandbox/'外部 原内容';external.mkdir();(external/'保留.txt').write_text('不跟随不明联接','utf-8')
    link=relocated/'data/非受管联接'
    subprocess.run(['cmd','/c','mklink','/J',str(link),str(external)],check=True,capture_output=True)
    try:
        pid=native_task(relocated,failure_target,'migrate',environment,expect_failure=True)
        assert load(relocated/'portable.json')['workspace_id']==workspace_id
        for relative, expected in hashes.items():assert digest(relocated/relative)==expected
        assert (external/'保留.txt').read_text('utf-8')=='不跟随不明联接';stop(relocated,pid)
    finally:
        assert link.is_junction() and link.resolve()==external.resolve();link.rmdir()
    results['checks'].append('迁移遇到不明联接时拒绝复制，原程序重新进入管理页，用户数据和实际依赖完整保留')
    cleanup_test_shortcuts([root,empty,moved,relocated,recovery_source,recovery_target,failure_target])
    results['ok'] = True;write(report, results)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print('验收记录：', report)
if __name__ == '__main__': main()
