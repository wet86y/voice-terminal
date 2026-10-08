"""Independent desktop qualification（独立桌面定向检查）：只在指定测试子目录使用本地小样本。"""
from __future__ import annotations
import argparse
import base64
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata
import io
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(r'D:\项目开发\测试').resolve()

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True, help='exe（验收构建的单程序入口）')
    parser.add_argument('--resume', action='store_true', help='resume（继续）：复查同一隔离目录，不删除工作区')
    args = parser.parse_args()
    base = TEST_ROOT / '桌面独立启动-0.9.0'
    if base.exists() and not args.resume:
        raise FileExistsError('验收子目录已存在，请先阅读记录；本脚本不会自动删除已有工作区。')
    base.mkdir(exist_ok=args.resume)
    executable = base / 'VoiceTerminal.exe'
    shutil.copy2(args.exe, executable)
    workspace = base / '工作区 中文和空格'
    config = base / 'qualification.json'
    record = {'registry': str(base / 'locator.json'), 'workspace_root': str(workspace), 'instance_tag': 'independent-desktop-0.9', 'headless': False, 'desktop_check': True,
              'fields': {'registry': '测试独立定位记录', 'workspace_root': '中文和空格测试工作区', 'instance_tag': '独立实例编号', 'headless': '不运行完整业务服务', 'desktop_check': '管理页面加载后记录并退出'}}
    write(config, record)
    checks = []
    server = None

    def checked(name, condition, detail=None):
        checks.append({'name': name, 'ok': bool(condition), 'detail': detail})
        write(base / 'results.json', {'ok': all(row['ok'] for row in checks), 'checks': checks,
              'fields': {'ok': '全部定向检查是否通过', 'checks': '检查记录', 'name': '检查内容', 'detail': '诊断或限制'}})
        print(f'{name}：{"通过" if condition else "失败"}', flush=True)
        if not condition:
            raise AssertionError(name)

    def native(name, request, expect=True):
        source = base / 'checks' / (name + '.json')
        write(source, request)
        result = subprocess.run([str(executable), '--qualification-config', str(config), '--native-check', str(source)],
                    cwd=base, timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
        output = json.loads(source.with_suffix('.result.json').read_text('utf-8'))
        checked(name, output['ok'] == expect, output.get('error'))
        return output.get('result', {})

    try:
        if not args.resume:
            subprocess.run([str(executable), '--qualification-config', str(config)], cwd=base, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
        desktop = json.loads(config.with_suffix('.desktop-result.json').read_text('utf-8'))
        checked('单入口、中文路径、无 Python 进入管理页', desktop['ok'] and not desktop['runtime']['interpreter_ready'] and not desktop['service_started'] and desktop['error'] is None)
        checked('初次展开文件与摘要', verify_program(workspace))
        checked('本体携带配置文档', (workspace / 'data/docs/模型运行环境配置指南.md').is_file())
        record.update(simulate_webview_missing=True)
        write(config, record)
        subprocess.run([str(executable), '--qualification-config', str(config)], cwd=base, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        guide = json.loads(config.with_suffix('.webview-result.json').read_text('utf-8'))
        checked('缺少网页引擎的引导模拟', not guide['system_modified'] and guide['online'].startswith('https://go.microsoft.com/') and guide['action'] == '引导后退出')
        record.update(simulate_webview_missing=False, headless=True, desktop_check=False)
        write(config, record)
        fixtures = base / 'fixtures'
        fixtures.mkdir(exist_ok=True)
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                requests.append({'path': self.path, 'range': self.headers.get('Range')})
                if self.path == '/redirect':
                    self.send_response(307); self.send_header('Location', '/transport.bin'); self.send_header('Content-Length', '0'); self.end_headers(); return
                file = fixtures / self.path.lstrip('/')
                if not file.is_file(): self.send_error(404); return
                size = file.stat().st_size
                range_text = self.headers.get('Range', 'bytes=0-').split('=')[-1].split('-')
                offset = int(range_text[0]); end = min(size-1, int(range_text[1]) if range_text[1] else size-1)
                self.send_response(206 if self.headers.get('Range') else 200)
                self.send_header('Content-Length', str(end-offset+1)); self.send_header('Accept-Ranges', 'bytes'); self.send_header('ETag', '"'+sha(file)+'"')
                if self.headers.get('Range'): self.send_header('Content-Range', f'bytes {offset}-{end}/{size}')
                self.end_headers()
                try:
                    with file.open('rb') as stream:
                        stream.seek(offset)
                        remaining=end-offset+1
                        while remaining and (block := stream.read(min(65536,remaining))):
                            self.wfile.write(block); self.wfile.flush();remaining-=len(block)
                            if file.name == 'transport.bin': time.sleep(.015)
                except (ConnectionError, OSError): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}'
        transport = fixtures / 'transport.bin'
        transport.write_bytes(bytes(range(256)) * 24576)
        for file in (workspace/'tmp/qualification-download.bin',workspace/'tmp/qualification-download.bin.part',workspace/'tmp/qualification-download.bin.part.identity'):
            assert file.resolve().is_relative_to(base.resolve()) and not file.is_symlink()
            file.unlink(missing_ok=True)
        file_spec = {'path': 'transport.bin', 'url': url + '/redirect', 'size': transport.stat().st_size, 'sha256': sha(transport), 'sha512': ''}
        native('下载取消', {'operation': 'download-file', 'file': file_spec, 'cancel_after_bytes': 5*1024*1024}, expect=False)
        native('跨进程续传与重定向', {'operation': 'download-file', 'file': file_spec})
        checked('续传使用范围请求', any(row['range'] and row['range'] != 'bytes=0-' for row in requests))

        # 最小解释器是已配置官方解释器的标准文件；不复制开发环境的 site-packages（第三方依赖目录）。
        interpreter = fixtures / 'python-minimal.zip'
        with zipfile.ZipFile(interpreter, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in (PROJECT / '.local/runtimes/python').iterdir():
                if file.is_file() and file.name not in {'manifest.json','runtime.json','wheels.lock.txt'}:
                    archive.write(file, file.name)
        dependency = make_wheel(fixtures)
        lock = {'schema': 1, 'python': {'version': '3.12.10', 'architecture': 'amd64', 'url': url + '/' + interpreter.name, 'size': interpreter.stat().st_size, 'sha256': sha(interpreter)},
                'groups': {key: ['idna'] for key in ['voice-core','torch-cpu','onnx-cpu','sherpa-cpu','modelscope-download']},
                'wheels': [{'name': 'idna', 'version': metadata.version('idna'), 'file': dependency.name, 'url': url + '/' + dependency.name, 'size': dependency.stat().st_size, 'sha256': sha(dependency)}]}
        runtime_lock = workspace / 'app/config/runtime/voice-runtime.lock.json'
        original_lock = (PROJECT/'config/runtime/voice-runtime.lock.json').read_bytes()
        write(runtime_lock, lock)
        native('最小私有解释器与缺失依赖组装', {'operation': 'configure', 'group': 'voice-core'})
        before = len(requests)
        native('共享依赖去重', {'operation': 'configure', 'group': 'onnx-cpu'})
        checked('依赖去重不产生额外下载', before == len(requests))
        python = workspace / 'runtimes/python/python.exe'
        identity = native('解释器版本与架构', {'operation': 'worker', 'python': str(python), 'worker_operation': 'interpreter'})
        checked('解释器基线匹配', identity['official'] and identity['version'] == '3.12.10')
        reused = native('完整安装记录复用筛选', {'operation': 'worker', 'python': str(python), 'worker_operation': 'reuse', 'args': ['--lock', str(runtime_lock), '--names', '["idna"]', '--target', str(base/'reuse-output')]})
        checked('匹配依赖可复用', 'idna' in reused['reused'])
        rejected_lock = fixtures/'rejected-lock.json'
        rejected = json.loads(json.dumps(lock)); rejected['wheels'][0]['version'] = '0.0.0'
        write(rejected_lock, rejected)
        reused = native('不匹配版本复用筛选', {'operation': 'worker', 'python': str(python), 'worker_operation': 'reuse', 'args': ['--lock', str(rejected_lock), '--names', '["idna"]', '--target', str(base/'rejected-output')]})
        checked('不匹配版本重新下载', not reused['reused'] and 'idna' in reused['rejected'])
        previous = sha(workspace/'runtimes/python/manifest.json')
        bad = json.loads(json.dumps(lock)); bad['wheels'][0]['sha256'] = '0'*64
        write(runtime_lock, bad)
        native('依赖摘要错误恢复', {'operation': 'configure', 'group': 'sherpa-cpu'}, expect=False)
        checked('失败保留现用环境', sha(workspace/'runtimes/python/manifest.json') == previous)
        write(runtime_lock, lock)

        catalog_path = workspace/'app/config/resources/catalog.json'
        catalog = json.loads(catalog_path.read_text('utf-8'))
        original_catalog = (PROJECT/'config/resources/catalog.json').read_bytes()
        model = fixtures/'model'; model.mkdir(); (model/'small.bin').write_bytes(b'local model fixture\n'*64)
        selected = next(row for row in catalog['resources'] if row['resource_id']=='smart-turn')
        selected['files']=[{'path':'small.bin','size_bytes':(model/'small.bin').stat().st_size,'sha256':sha(model/'small.bin')}]
        write(catalog_path, catalog)
        native('单模型目录原生导入', {'operation':'import-resources','source':str(model),'resource_id':'smart-turn'})
        bundle=fixtures/'resources.zip'
        with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
            archive.write(model/'small.bin','models/small.bin')
            archive.writestr('resources.json',json.dumps({'schema_version':1,'resources':[{'resource_id':'smart-turn','version':selected['version'],'files':[{'path':'small.bin','package_path':'models/small.bin'}]}]}))
        result=native('ZIP64 批量预校验与重复资源跳过',{'operation':'import-resources','source':str(bundle)})
        checked('完整相同资源跳过替换',any('跳过相同完整资源' in phase for phase in result['phases']))
        (model/'small.bin').write_bytes(b'broken')
        native('损坏导入拒绝',{'operation':'import-resources','source':str(model),'resource_id':'smart-turn'},expect=False)
        checked('损坏导入保留已安装资源',sha(workspace/'models/analysis/smart-turn/small.bin')==selected['files'][0]['sha256'])
        gpu = native('显卡依赖缺失检测', {'operation':'worker','python':str(python),'worker_operation':'gpu','args':['--names','["torch"]','--model','paraformer','--program',str(workspace/'app'),'--install-root',str(workspace)]})
        checked('显卡未就绪保持关闭',not gpu['gpu_available'])

        # 模拟事务中断：所有移动的绝对路径均先确认属于唯一验收子目录。
        current=workspace/'runtimes/python'; old=workspace/'tmp/native-old'
        for path in (current,old):
            assert path.resolve().is_relative_to(base.resolve()) and not path.is_symlink() and not path.is_junction()
        current.rename(old); current.mkdir(); (current/'broken.txt').write_text('interrupted')
        write(workspace/'tmp/native-install.json',{'target':'runtimes/python','state':'prepared','original':True})
        native('中断事务回滚',{'operation':'snapshot'})
        checked('中断后恢复原解释器',sha(current/'manifest.json')==previous and not (current/'broken.txt').exists())
        moved=base/'移动后的工作区 中文'
        assert workspace.resolve().is_relative_to(base.resolve()) and moved.resolve().is_relative_to(base.resolve())
        workspace.rename(moved); workspace=moved
        record['workspace_root']=str(workspace); write(config,record)
        native('移动工作区后的路径定位',{'operation':'initialize'})
        python=workspace/'runtimes/python/python.exe'
        identity=native('移动后解释器运行',{'operation':'worker','python':str(python),'worker_operation':'interpreter'})
        checked('移动后解释器使用目标位置',Path(identity['prefix']).resolve()==python.parent.resolve())
        moved_exe=base/'入口移动位置/VoiceTerminal.exe'; moved_exe.parent.mkdir();shutil.copy2(executable,moved_exe);executable=moved_exe
        snapshot=native('入口移动后仍读取相同定位',{'operation':'snapshot'})
        checked('入口与工作区路径独立',Path(snapshot['data']).resolve()==(workspace/'data').resolve())
        # 保留样本供复查，但恢复真实锁定清单；最小样本此后不会被误报为完整语音环境。
        (workspace/'app/config/runtime/voice-runtime.lock.json').write_bytes(original_lock)
        (workspace/'app/config/resources/catalog.json').write_bytes(original_catalog)
        write(base/'http-requests.json',requests)
        print(f'定向检查记录：{base / "results.json"}',flush=True)
    finally:
        if server: server.shutdown();server.server_close()

def verify_program(workspace):
    import sys
    sys.path.insert(0,str(PROJECT))
    from voice_terminal.distribution import verify
    record=json.loads((workspace/'app/release.json').read_text('utf-8'))
    verify(workspace/'app',record)
    return True

def make_wheel(folder):
    distribution=metadata.distribution('idna')
    rows=[];body={}
    for file in distribution.files:
        relative=file.as_posix()
        if relative.endswith('.pyc') or relative.endswith('/RECORD'):continue
        if '..' in Path(relative).parts:raise ValueError('样本包含外部依赖路径')
        data=Path(distribution.locate_file(file)).read_bytes();body[relative]=data
        rows.append([relative,'sha256='+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='),str(len(data))])
    record=f'idna-{distribution.version}.dist-info/RECORD';rows.append([record,'',''])
    stream=io.StringIO();csv.writer(stream,lineterminator='\n').writerows(rows);body[record]=stream.getvalue().encode()
    target=folder/f'idna-{distribution.version}-py3-none-any.whl'
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        for relative,data in body.items():archive.writestr(relative,data)
    return target

if __name__=='__main__':main()
