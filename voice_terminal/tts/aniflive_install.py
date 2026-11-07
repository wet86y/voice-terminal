"""AnifLive（本地合成）的固定输入、依赖准备及模型登记共用任务。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.client import HTTPException
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from .aniflive_native import ARCHIVE_SHA256, UPSTREAM_COMMIT, contained, digest, prepare_source

_PRINT_LOCK = threading.Lock()
DEFAULT_MODEL = 'minimax-female-shaonv-zh'  # DEFAULT_MODEL（本地默认模型编号）。


def emit(phase: str, done=0, total=0):
    with _PRINT_LOCK:
        print(json.dumps({'phase': phase, 'completed_bytes': done, 'total_bytes': total}, ensure_ascii=False), flush=True)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def artifact(row: dict, target: Path, *, local: Path, program: Path) -> Path:
    """固定文件下载；中断保留部分文件，最终大小及摘要共同决定是否启用。"""
    target = contained(local, target)
    if target.is_file() and target.stat().st_size == row['size'] and digest(target) == row['sha256']:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = contained(local, target.with_name(target.name + '.part'))
    if row.get('bundled'):
        sources = [program / row['bundled'], local / 'build/aniflive-runtime/wheels' / row['file']]
        source = next((p for p in sources if p.is_file() and p.stat().st_size == row['size'] and digest(p) == row['sha256']), None)
        if source is None:
            raise RuntimeError('缺少随包预制依赖：' + row['file'])
        shutil.copyfile(source, part)
    else:
        if not str(row.get('url', '')).startswith('https://'):
            raise ValueError('下载资源必须使用锁定的安全网络地址。')
        for attempt in range(3):
            try:
                offset = part.stat().st_size if part.is_file() else 0
                if offset == row['size']:
                    if digest(part) == row['sha256']:
                        os.replace(part, target)
                        return target
                    part.unlink()
                    offset = 0
                if offset > row['size']:
                    part.unlink()
                    offset = 0
                headers = {'Range': f'bytes={offset}-'} if offset else {}
                with urlopen(Request(row['url'], headers=headers), timeout=30) as response:
                    append = offset > 0 and response.status == 206
                    if append and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise RuntimeError('下载续传范围不匹配。')
                    done = offset if append else 0
                    with part.open('ab' if append else 'wb') as stream:
                        while block := response.read(1024 * 1024):
                            done += len(block)
                            if done > row['size']:
                                raise ValueError('下载内容超过固定长度。')
                            stream.write(block)
                            emit('下载 ' + target.name, done, row['size'])
                if part.stat().st_size < row['size']:
                    raise OSError('下载提前结束，保留断点继续重试。')
                if part.stat().st_size != row['size'] or digest(part) != row['sha256']:
                    part.unlink(missing_ok=True)
                    raise ValueError('下载内容大小或摘要不匹配：' + target.name)
                break
            except (OSError, RuntimeError, HTTPException, ValueError):
                if attempt == 2:
                    raise
                time.sleep(.5 * (attempt + 1))
    if part.stat().st_size != row['size'] or digest(part) != row['sha256']:
        part.unlink(missing_ok=True)
        raise ValueError('下载内容大小或摘要不匹配：' + target.name)
    os.replace(part, target)
    return target


class AnifLiveInstallTask:
    """界面和无界面入口共用；安装不自动改变已保存的播放目标。"""

    def __init__(self, program: Path, local: Path):
        self.program = Path(program).absolute()
        self.local = contained(local, local)
        self.runtime = self.local / 'runtimes/aniflive-tts'
        self.lock_path = self.program / 'config/runtime/aniflive-runtime.lock.json'
        self.lock = json.loads(self.lock_path.read_text(encoding='utf-8'))
        self.presets = json.loads((self.program / 'config/aniflive-presets.json').read_text(encoding='utf-8'))
        self.lock_sha = digest(self.lock_path)
        self._gpu_acknowledged = False
        self.native_ack = False
        if not (self.local / 'portable.json').is_file():
            raise ValueError('AnifLive 配置需要已初始化的受管工作区。')
        from .aniflive_aliases import cleanup_aliases
        cleanup_aliases(self.local)
        shared = contained(self.local, self.local / 'models/aniflive-tts/shared')
        if shared.exists():
            for path in shared.rglob('*'):
                contained(self.local, path)  # 在语言工具读取目录摘要前拒绝内部联接。

    def prepare_source(self):
        emit('核对固定官方源码与兼容补丁')
        cache = self.local / 'cache/aniflive-tts-v1.5.0-beta-source.zip'
        row = {'url': f'https://github.com/Hiruynk/AnifLive-TTS/archive/{UPSTREAM_COMMIT}.zip',
               'size': 32876798, 'sha256': ARCHIVE_SHA256}
        artifact(row, cache, local=self.local, program=self.program)
        return prepare_source(cache, self.local)

    def prepare_dependencies(self):
        unresolved = [r['name'] for r in self.lock['wheels'] if r.get('needs_build') or r.get('resolution_error')]
        if unresolved:
            raise RuntimeError('开发构建尚未准备完整的固定依赖：' + '、'.join(unresolved))
        from .aniflive_transactions import recover_dependencies
        recover_dependencies(self.runtime)
        current_by_role = {}
        for role, folder in [('runtime', 'packages'), ('export', 'export-packages')]:
            target = contained(self.local, self.runtime / folder)
            receipt = target / 'aniflive-packages.json'
            try:
                current = json.loads(receipt.read_text(encoding='utf-8')) if receipt.is_file() else {}
                records = current.get('files')
                valid = current.get('lock_sha256') == self.lock_sha and isinstance(records, list) and bool(records)
                if valid:
                    total, done, last = sum(row['size'] for row in records), 0, time.monotonic()
                    phase = '核对隔离' + ('运行' if role == 'runtime' else '导出') + '依赖'
                    emit(phase, 0, total)
                    for row in records:
                        file = contained(target, target / row['path'])
                        if not file.is_file() or file.stat().st_size != row['size'] or digest(file) != row['sha256']:
                            valid = False
                            break
                        done += row['size']
                        if time.monotonic() - last >= .5:
                            emit(phase, done, total)
                            last = time.monotonic()
                    if valid:
                        emit(phase, done, total)
                current_by_role[role] = valid
            except (ValueError, KeyError, TypeError, OSError):
                current_by_role[role] = False
        if all(current_by_role.values()):
            emit('隔离运行与导出依赖已通过完整文件校验')
            return
        self.require_build_space()
        rows = {(r['name'], r['version']): r for r in self.lock['wheels']}
        required = set(self.lock['runtime'].items()) | set(self.lock['export'].items())
        cache = self.local / 'cache/aniflive-wheels'
        def fetch(identity):
            row = rows[identity]
            return artifact(row, cache / row['file'], local=self.local, program=self.program)
        emit('准备隔离运行依赖')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(fetch, sorted(required)))
        spec = importlib.util.spec_from_file_location('_voice_wheel_install', self.program / 'scripts/voice-environment-worker.py')
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)
        pending = []
        try:
            for role, folder in [('runtime', 'packages'), ('export', 'export-packages')]:
                target = contained(self.local, self.runtime / folder)
                wanted = self.lock[role]
                if current_by_role[role]:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix='.' + folder + '-stage-', dir=target.parent))
                backup = target.with_name('.' + folder + '-old-' + uuid4().hex)
                pending.append({'target': folder, 'stage': stage.name, 'backup': backup.name, 'original': target.exists()})
                selected = [(name, version) for name, version in wanted.items()
                            if role == 'runtime' or self.lock['runtime'].get(name) != version]
                worker.install([cache / rows[key]['file'] for key in sorted(selected)], stage)
                installed_files = [{'path': path.relative_to(stage).as_posix(), 'size': path.stat().st_size,
                                    'sha256': digest(path)} for path in sorted(stage.rglob('*')) if path.is_file()]
                atomic_json(stage / 'aniflive-packages.json', {'schema': 1, 'role': role,
                            'lock_sha256': self.lock_sha, 'packages': dict(selected), 'files': installed_files,
                            'fields': {'schema': '依赖收据格式', 'role': '运行或导出角色',
                                       'lock_sha256': '完整依赖锁摘要', 'packages': '实际安装包版本',
                                       'files': '已安装文件、大小及摘要'}})
            if pending:
                if any(row['original'] for row in pending):
                    self.acquire_gpu()
                journal = self.runtime / 'dependency-transaction.json'
                atomic_json(journal, {'schema': 1, 'state': 'prepared', 'items': pending})
                try:
                    for row in pending:
                        target, stage, backup = (contained(self.runtime, self.runtime / row[key]) for key in ('target', 'stage', 'backup'))
                        if target.exists():
                            os.rename(target, backup)
                        os.rename(stage, target)
                    atomic_json(journal, {'schema': 1, 'state': 'committed', 'items': pending})
                finally:
                    recover_dependencies(self.runtime)
        finally:
            for row in pending:
                stage = contained(self.runtime, self.runtime / row['stage'])
                if stage.exists():
                    shutil.rmtree(stage)

    def require_build_space(self):
        if shutil.disk_usage(self.local).free < 24 * 1024**3:
            raise ValueError('新建 AnifLive 候选需要至少 24 GiB（吉比字节）可用空间；原可用模型保持不变。')

    def acquire_gpu(self):
        if self._gpu_acknowledged:
            return
        emit('等待显卡构建')
        if self.native_ack and json.loads(sys.stdin.readline()) != {'continue': True}:
            raise RuntimeError('未收到受管显卡构建许可。')
        self._gpu_acknowledged = True

    def command(self, role, *arguments):
        return [sys.executable, '-S', '-B', '-X', 'utf8', str(self.program / 'scripts/run-aniflive.py'),
                '--root', str(self.local), '--role', role, '--', *map(str, arguments)]

    def run(self, command, report: Path):
        report = contained(self.local, report)
        report.parent.mkdir(parents=True, exist_ok=True)
        from ..application.windows import Job, ManagedProcess
        from .aniflive_aliases import cleanup_aliases
        owner, process = Job(), None
        report.unlink(missing_ok=True)
        try:
            process = ManagedProcess(command, cwd=self.program, env=dict(os.environ), output=report,
                outer_job=owner, event_name='Local\\VoiceTerminal.AnifLive.' + uuid4().hex,
                key='aniflive-configure')
            while (code := process.poll()) is None:
                time.sleep(.1)
            if code:
                raise RuntimeError(f'AnifLive 子任务失败，退出码 {code}；详见 {report.name}')
        finally:
            if process is not None:
                process.close()
            owner.close()
            cleanup_aliases(self.local)

    def prepare_inputs(self, model_id):
        preset = next((r for r in self.presets['models'] if r['id'] == model_id), None)
        if preset is None:
            raise ValueError('未知的 AnifLive 预设模型。')
        root = contained(self.local, self.local / 'cache/aniflive-tts/inputs' / model_id)
        records = {}
        for row in [*preset['weights'], {**preset['reference'], 'role': 'reference'}]:
            filename = Path(row.get('path') or row['asset']).name
            spec = {'file': filename, 'size': row['size_bytes'], 'sha256': row['sha256']}
            if row.get('asset'):
                source = self.program / 'config/resources' / row['asset']
                if digest(source) != row['sha256']:
                    raise ValueError('默认音色种子摘要不匹配。')
                destination = contained(self.local, root / filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source, destination)
                if digest(destination) != row['sha256']:
                    raise ValueError('默认音色输入内容不匹配。')
            else:
                spec['url'] = f'https://huggingface.co/{preset["repository"]}/resolve/{preset["revision"]}/{row["path"]}'
                destination = artifact(spec, root / filename, local=self.local, program=self.program)
            records[row['role']] = str(destination)
        text = root / 'reference.txt'
        text.write_text(preset['reference']['text'] + '\n', encoding='utf-8')
        records.update(reference_text=str(text), reference_language=preset['reference']['language'],
                       model_id=model_id, metadata_recipe=preset.get('metadata_recipe'))
        atomic_json(root / 'input.json', records)
        return root / 'input.json'

    def worker(self, role, operation, job: Path, **options):
        result = job / (operation + '-result.json')
        command = self.command(role)[:-1] + ['--module', 'voice_terminal.tts.aniflive_worker', '--',
                  operation, '--root', str(self.local), '--result', str(result)]
        for key, value in options.items():
            command.extend(['--' + key.replace('_', '-'), str(value)])
        self.run(command, job / (operation + '.log'))
        return json.loads(result.read_text(encoding='utf-8'))

    def convert(self, input_path: Path, *, native_ack=False, name=None):
        from .aniflive_models import AnifLiveModelRegistry, validate_model_id, BUILD_ADAPTER_FILES
        input_path = contained(self.local, Path(input_path).absolute())
        values = json.loads(input_path.read_text(encoding='utf-8'))
        model_id = validate_model_id(values['model_id'])
        temporary = self.local / 'tmp/aniflive-tasks'
        temporary.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix='m-', dir=temporary))
        emit('准备模型共享资源')
        shared_root = self.local / 'models/aniflive-tts/shared'
        from . import aniflive_shared
        shared_inputs = aniflive_shared.download_inputs(self.program, self.local)
        self.native_ack = self.native_ack or native_ack
        self.acquire_gpu()
        atomic_json(job / 'shared-result.json', aniflive_shared.activate(self.program, self.local, shared_inputs))
        from ..resource_distribution import ResourceManager, _install_lock
        with _install_lock(self.local):
            ResourceManager(self.program, self.local).recover_pending_transaction()
        hardware = self.worker('inference', 'probe', job)
        self.worker('export', 'prepare-base', job, input=input_path)
        values = json.loads(input_path.read_text(encoding='utf-8'))
        source_receipt = json.loads((self.runtime / 'source/windows-source-receipt.json').read_text(encoding='utf-8'))
        identity = {'gpt': digest(Path(values['gpt'])), 'sovits': digest(Path(values['sovits'])),
                    'input_provenance': values.get('metadata_provenance'),
                    'reference': digest(Path(values['reference'])),
                    'reference_text': Path(values['reference_text']).read_text(encoding='utf-8').strip(),
                    'language': values['reference_language'], 'build': self.presets['build'],
                    'source': {key: source_receipt[key] for key in ('upstream_commit', 'archive_sha256', 'patch_revision', 'files')}, 'dependency_lock': self.lock_sha,
                    'shared_lock': digest(self.program / 'config/runtime/aniflive-shared-assets.lock.json'),
                    'adapter': {name: digest(self.program / name) for name in BUILD_ADAPTER_FILES},
                    'hardware': {key: value for key, value in hardware.items() if key not in {'imports', 'free_vram_bytes'}}}
        revision = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        registry = AnifLiveModelRegistry(self.local)
        try:
            package, existing, version = registry.resolve(model_id, revision, verify_hashes=True)
            if version['identity'] == identity:
                return {'model_id': model_id, 'revision': existing, 'reused': True}
        except (ValueError, KeyError, OSError):
            pass
        package = contained(self.local, self.local / 'models/aniflive-tts/packages' / model_id / revision)
        if package.exists():
            raise RuntimeError('已有同身份候选目录但缺少有效登记，保留现场等待核对。')
        self.require_build_space()
        candidate = job / 'package'
        import psutil
        memory = psutil.virtual_memory()
        # 本项目构建任务的最低空闲预算；不是上游对所有配置的内存上限保证。
        minimum_memory = 4 * 1024**3
        atomic_json(job / 'preflight.json', {'memory_total_bytes': memory.total,
            'memory_available_bytes': memory.available, 'minimum_available_memory_bytes': minimum_memory,
            'disk_free_bytes': shutil.disk_usage(self.local).free,
            'fields': {'memory_total_bytes': '物理内存总字节', 'memory_available_bytes': '构建前可用物理内存字节',
                       'minimum_available_memory_bytes': '项目构建最低空闲内存预算，不代表所有模型的峰值上限',
                       'disk_free_bytes': '本机资源盘可用字节'}})
        if memory.available < minimum_memory:
            raise ValueError('新建 AnifLive 引擎需要至少 4 GiB（吉比字节）空闲物理内存；请释放占用后重试，原可用模型保持不变。')
        build = self.presets['build']
        emit('导出计算图并构建九个推理引擎')
        self.run(self.command('export', 'model', 'convert', '--gpt', values['gpt'], '--sovits', values['sovits'],
                    '--reference-audio', values['reference'], '--reference-text-file', values['reference_text'],
                    '--reference-language', values['reference_language'], '--model-id', model_id,
                    '--output', candidate, '--shared-dir', shared_root,
                    '--source-dir', self.runtime / 'source/minimal_inference', '--max-len', build['max_len'],
                    '--stream-overlap-frames', build['stream_overlap_frames'], '--workspace-mib', build['workspace_mib'],
                    '--optimization-level', build['optimization_level']), job / 'conversion.log')
        emit('执行完整音频及流式合成验证')
        verified = self.worker('inference', 'validate', job, package=candidate)
        if verified.get('validation', {}).get('enqueue_verified') is not True or verified.get('streaming_verified') is not True:
            raise RuntimeError('模型没有完成真实合成与流式验证。')
        from .aniflive_build_diagnostics import classify_build_logs
        diagnostics = classify_build_logs(candidate, verified)
        atomic_json(job / 'build-diagnostics.json', diagnostics)
        if diagnostics['capture_probe_fallbacks']:
            emit('已验证图捕获回退为普通显卡执行；保留完整构建诊断')
        atomic_json(candidate / 'application-receipt.json', {'schema': 1, 'identity': identity, **verified,
                    'build_diagnostics': diagnostics,
                    'files': [{'path': p.relative_to(candidate).as_posix(), 'size': p.stat().st_size,
                               'sha256': digest(p)} for p in sorted(candidate.rglob('*'))
                              if p.is_file() and p.name not in {'checksums.json', 'application-receipt.json'}],
                    'fields': {'schema': '应用模型收据格式', 'identity': '完整输入及构建身份',
                               'validation': '实际引擎加载及完整合成验证', 'streaming_verified': '流式实际执行通过',
                               'streaming': '流式音频结果', 'files': '实际生成文件、大小及摘要'}})
        # 新收据也是模型包的一部分，由固定上游工具重新生成完整摘要清单。
        checksums = {p.relative_to(candidate).as_posix(): digest(p) for p in candidate.rglob('*')
                     if p.is_file() and p.name != 'checksums.json'}
        atomic_json(candidate / 'checksums.json', checksums)
        self.worker('inference', 'package-check', job, package=candidate)
        if (any(digest(self.program / path) != expected for path, expected in identity['adapter'].items())
                or digest(self.lock_path) != identity['dependency_lock']
                or digest(self.program / 'config/runtime/aniflive-shared-assets.lock.json') != identity['shared_lock']):
            raise RuntimeError('构建期间桥接源码或依赖锁发生变化，候选保留且不登记。')
        emit('启用已验证模型版本')
        preset = next((r for r in self.presets['models'] if r['id'] == model_id), {})
        from ..resource_distribution import ResourceManager, ResourceSpec, ResourceFile
        manager = ResourceManager(self.program, self.local)
        file_rows = tuple(ResourceFile(p.relative_to(candidate).as_posix(), p.stat().st_size, digest(p), False)
                          for p in sorted(candidate.rglob('*')) if p.is_file())
        spec = ResourceSpec('tts-aniflive-' + model_id, 'model', name or values.get('name') or preset.get('name', model_id),
                            f'aniflive-tts/packages/{model_id}/{revision}', revision, {}, file_rows, required=False)
        registration = registry.register(model_id, revision, spec.title, identity, persist=False)
        manager._commit_candidate(spec, candidate, job.name,
            model_registration=json.dumps(registration, ensure_ascii=False, indent=2).encode('utf-8'))
        registry.resolve(model_id, revision)
        return {'model_id': model_id, 'revision': revision, 'reused': False}

    def registered_inputs(self, model_id):
        """复用已登记导入模型的本机原始输入；不借用另一模型的预设下载来源。"""
        from .aniflive_models import AnifLiveModelRegistry, validate_model_id
        validate_model_id(model_id)
        if any(row['id'] == model_id for row in self.presets['models']):
            return self.prepare_inputs(model_id)
        entry = AnifLiveModelRegistry(self.local).read()['models'].get(model_id)
        if not isinstance(entry, dict):
            raise ValueError('导入模型尚无受管登记。')
        identity = entry['versions'][entry['current_revision']]['identity']
        root = contained(self.local, self.local / 'cache/aniflive-tts/inputs/imports')
        for path in root.glob('*/input.json'):
            contained(root, path)
            values = json.loads(path.read_text(encoding='utf-8'))
            if values.get('model_id') != model_id:
                continue
            if all(digest(contained(self.local, Path(values[key]))) == identity[key] for key in ('gpt', 'sovits', 'reference')):
                return path
        raise ValueError('已登记导入模型的原始输入缺失或内容已变化，请重新导入配对权重。')

    def import_inputs(self, spec_path: Path):
        """显式选择的原始模型输入复制后转换；不执行输入中携带的代码。"""
        spec_path = Path(spec_path).absolute()
        if spec_path.stat().st_size > 1024 * 1024 or any(path.is_symlink() or path.is_junction() for path in (spec_path, *spec_path.parents)):
            raise ValueError('模型导入描述必须为不超过 1 MiB（兆二进制字节）的普通本机文件。')
        values = json.loads(spec_path.read_text(encoding='utf-8'))
        required = {'model_id', 'name', 'gpt', 'sovits', 'reference', 'reference_text', 'reference_language'}
        if set(values) != required or values['reference_language'] not in {'zh', 'yue', 'en', 'ja', 'ko'}:
            raise ValueError('模型导入需要模型编号、名称、成对权重、录音、准确转写和语言。')
        from .aniflive_models import validate_model_id
        model_id = validate_model_id(values['model_id'])
        if not isinstance(values['reference_text'], str) or not 1 <= len(values['reference_text'].strip()) <= 2000:
            raise ValueError('模型参考转写必须为 1–2000 个字符。')
        if not isinstance(values['name'], str) or not 1 <= len(values['name'].strip()) <= 80:
            raise ValueError('导入模型名称必须为 1–80 个字符。')
        folder = self.local / 'cache/aniflive-tts/inputs/imports' / uuid4().hex
        folder.mkdir(parents=True)
        result = dict(values)
        for key, extension in [('gpt', '.ckpt'), ('sovits', '.pth'), ('reference', '.wav')]:
            source = Path(values[key])
            source = source.absolute() if source.is_absolute() else contained(spec_path.parent, spec_path.parent / source)
            if any(parent.is_symlink() or parent.is_junction() for parent in (source, *source.parents)) or not source.is_file():
                raise ValueError('模型导入源必须是普通本机文件。')
            if source.stat().st_size > 4 * 1024**3:
                raise ValueError('模型输入超过允许大小。')
            destination = folder / (key + extension)
            shutil.copyfile(source, destination)
            if digest(source) != digest(destination):
                raise ValueError('模型复制期间源内容发生变化。')
            result[key] = str(destination)
        transcript = folder / 'reference.txt'
        transcript.write_text(values['reference_text'].strip() + '\n', encoding='utf-8')
        result['reference_text'] = str(transcript)
        if digest(Path(result['sovits'])) == 'd42a22bbbf65fb2bbdd45ad6a66841156977db45c7aabe0a6992ff378d9c7d3b':
            result['metadata_recipe'] = 'known-v2proplus-base-v1'
        atomic_json(folder / 'input.json', result)
        return folder / 'input.json', values['name']
