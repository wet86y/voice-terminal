"""AnifLive（本地语音）独立导出阶段与内存感知调度。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

STAGES = ('ssl', 'bert', 'gpt', 'sovits', 'sv')  # STAGES（导出阶段）。
GIB = 1024**3
# 工作预算（字节）用于限制并发，不代表操作系统内存预留或硬上限。
BUDGETS = {'ssl': 4*GIB, 'bert': 6*GIB, 'gpt': 4*GIB, 'sovits': 6*GIB, 'sv': 3*GIB}
MODELS = {'ssl': {'SSL'}, 'bert': {'BERT'}, 'gpt': {'GPTEncoder', 'GPTStep'},
          'sovits': {'VQEncoder', 'SoVITS', 'SoVITSStreaming', 'Spectrogram'}, 'sv': {'SVEmbedding'}}
COUNTS = {'ssl': 1, 'bert': 1, 'gpt': 96, 'sovits': 6, 'sv': 1}  # COUNTS（官方输出校验项数）。


def stage_export_source(text: str) -> str:
    """给固定官方导出函数添加阶段边界，计算与校验语句保持原样。"""
    spans = [
        ('    # SSL\n', '    # BERT\n', 'ssl'),
        ('    # BERT\n', '    # GPT\n', 'bert'),
        ('    # GPT\n', '    # SoVITS\n', 'gpt'),
        ('    # SoVITS\n', '    # SV Model (for speaker embedding)\n', 'sovits'),
        ('    # SV Model (for speaker embedding)\n', '    output_dir = args.output_dir\n', 'sv'),
        ('    print("Exporting SSL...")\n', '    print("Exporting BERT...")\n', 'ssl'),
        ('    print("Exporting BERT...")\n', '    print("Exporting VQEncoder...")\n', 'bert'),
        ('    print("Exporting VQEncoder...")\n', '    print("Exporting GPT Encoder...")\n', 'sovits'),
        ('    print("Exporting GPT Encoder...")\n', '    print("Exporting SoVITS...")\n', 'gpt'),
        ('    print("Exporting SoVITS...")\n', '    # Export SVEmbeddingWrapper\n', 'sovits'),
        ('    # Export SVEmbeddingWrapper\n', '    # Publish only inference data. Training configs may contain private paths.\n', 'sv'),
        ('    # Publish only inference data. Training configs may contain private paths.\n', '    # 校验摘要\n', 'sovits'),
    ]
    replacements = []
    for begin, end, stage in spans:
        if text.count(begin) != 1 or text.count(end) != 1:
            raise ValueError('上游导出阶段结构不匹配，拒绝应用补丁。')
        start, stop = text.index(begin), text.index(end)
        if stop <= start:
            raise ValueError('上游导出阶段顺序不匹配。')
        block = text[start:stop]
        replacements.append((start, stop, f'    if args.export_stage in ("all", "{stage}"):\n' +
                             ''.join('    '+line if line.strip() else line for line in block.splitlines(True))))
    for start, stop, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[stop:]
    anchor = '    parser.add_argument("--validate", action="store_true", help="Enable ONNX export accuracy validation")'
    if text.count(anchor) != 1:
        raise ValueError('上游导出参数结构不匹配。')
    text = text.replace(anchor, anchor + '\n    parser.add_argument("--export-stage", dest="export_stage", '
                        'choices=("all", "ssl", "bert", "gpt", "sovits", "sv"), default="all", '
                        'help="export-stage（独立导出阶段）")')
    compile(text, 'export_onnx.py', 'exec')
    return text


def memory_available() -> tuple[int, int]:
    """返回可用物理内存、提交额度（字节）；读取失败不得乐观并行。"""
    import psutil
    physical = psutil.virtual_memory().available
    if os.name != 'nt':
        return physical, physical
    import ctypes
    class Status(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ('physical', 'available', 'commit', 'commit_available',
                                                  'virtual', 'virtual_available', 'extended')]
    status = Status(); status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('无法读取 Windows（操作系统）内存状态。')
    return physical, int(status.commit_available)


def memory_failure(error: BaseException) -> bool:
    """仅识别明确分配失败，不把数值／计算图／显存错误混为内存不足。"""
    return isinstance(error, MemoryError) or any(
        marker in str(error).lower() for marker in (
            'could not allocate bytes object', 'defaultcpuallocator: not enough memory',
            'bad allocation', 'std::bad_alloc', 'bfcarena::allocaterawinternal failed to allocate memory'))


def permits_parallel(running: list[str], stage: str, available: tuple[int, int]) -> bool:
    physical, commit = available
    required = sum(BUDGETS[name] for name in [*running, stage])
    return len(running) < 2 and physical >= required + 2*GIB and commit >= required + 4*GIB


def process_memory_failure(code: int) -> bool:
    """Windows（操作系统）无内存／提交上限退出，连错误收据也无法写入时识别。"""
    return os.name == 'nt' and code & 0xffffffff in {0xc0000017, 0xc000012d}


def merge_stages(work: Path, output: Path, validate: bool):
    """全部阶段通过后才汇总；任何漏项、额外图或校验失败均拒绝发布。"""
    expected = {'ssl.onnx', 'bert.onnx', 'gpt_encoder.onnx', 'gpt_step.onnx', 'vq_encoder.onnx',
                'sovits.onnx', 'sovits_stream.onnx', 'spectrogram.onnx', 'sv_embedding.onnx'}
    graphs, rows, names = set(), [], set()
    for stage in STAGES:
        folder = work / stage
        if validate:
            values = json.loads((folder/'validation_report.json').read_text(encoding='utf-8'))
            if (not isinstance(values, list) or len(values) != COUNTS[stage]
                    or any(not isinstance(r, dict) or r.get('passed') is not True for r in values)
                    or {r.get('model_name') for r in values} != MODELS[stage]):
                raise RuntimeError(f'{stage}（导出阶段）数值校验未全部通过。')
            rows.extend(values)
        for p in folder.rglob('*'):
            if not p.is_file() or p.name in {'validation_report.json', 'stage-result.json'}:
                continue
            name = p.relative_to(folder).as_posix()
            if name in names:
                raise RuntimeError('阶段产物文件名冲突。')
            names.add(name)
            if p.suffix == '.onnx':
                graphs.add(name)
    if graphs != expected or 'config.json' not in names:
        raise RuntimeError('独立导出阶段缺少完整九图或配置。')
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('导出候选目录必须为空，保留已有诊断。')
    for stage in STAGES:
        for p in (work/stage).rglob('*'):
            if p.is_file() and p.name not in {'validation_report.json', 'stage-result.json'}:
                target = output / p.relative_to(work/stage)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
    if validate:
        (output/'validation_report.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')


def export_stages(local: Path, program: Path, script: Path, arguments: list[str]):
    """资源允许时最多两个阶段并行；明确内存失败后仅串行重试未完成阶段。"""
    from .aniflive_native import contained
    output = contained(local, Path(arguments[arguments.index('--output_dir')+1]).absolute())
    work = Path(tempfile.mkdtemp(prefix='.export-stages-', dir=output.parent))
    pending, running, done = list(STAGES), {}, []
    serial, retried = False, set()
    history = []
    def record(event, **values):
        history.append({'event':event, **values})
        (work/'schedule.json').write_text(json.dumps({'events':history,
            'fields':{'events':'阶段调度记录','event':'事件','stage':'导出阶段','memory':'可用物理内存与提交额度（字节）'}},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'[AnifLive 分阶段导出] {event} {values}', flush=True)
    def stop():
        for process, stream, _ in running.values():
            if process.poll() is None:
                process.terminate()
        for process, stream, _ in running.values():
            process.wait(); stream.close()
        running.clear()
    waiting_since = None
    try:
        while pending or running:
            for stage in list(running):
                process, stream, folder = running[stage]
                code = process.poll()
                if code is None:
                    continue
                stream.close(); del running[stage]
                if code == 0:
                    done.append(stage); record('阶段完成', stage=stage)
                    continue
                result = folder/'stage-result.json'
                failure = json.loads(result.read_text(encoding='utf-8')) if result.is_file() else {}
                if not ((failure.get('kind') == 'cpu_memory' and failure.get('stage') == stage)
                        or process_memory_failure(code)):
                    raise RuntimeError(f'{stage}（导出阶段）失败，退出码 {code}；见 {work/stage}.log')
                if stage in retried:
                    raise RuntimeError(f'{stage}（导出阶段）串行重试后仍内存不足；见 {work/stage}.log')
                retried.add(stage); serial = True
                interrupted = list(running)
                stop()
                pending = [stage, *interrupted, *pending]
                record('内存不足，回退串行新进程', stage=stage)
                break
            if pending and (not running or not serial):
                stage = pending[0]
                available = memory_available()
                if not running:
                    # 单阶段允许分页运行，但保留系统余量；不足时等待，而非立即占满。
                    # 转换父进程也会占用提交额度；串行允许系统分页文件按需增长，
                    # 不要求空闲额度已覆盖保守的整阶段峰值估计。
                    ready = available[0] >= 2*GIB and available[1] >= 4*GIB
                else:
                    ready = permits_parallel(list(running), stage, available)
                if ready:
                    waiting_since = None; pending.pop(0)
                    folder = work/stage
                    # 每次重试保留旧现场，绝不在同一个半成品目录继续。
                    if folder.exists():
                        folder.rename(work/f'{stage}.failed-{time.time_ns()}')
                    folder.mkdir()
                    values = list(arguments); values[values.index('--output_dir')+1] = str(folder)
                    command = [sys.executable, '-S', '-B', '-X', 'utf8', str(program/'scripts/run-aniflive.py'),
                               '--root', str(local), '--role', 'export', '--script', str(script), '--',
                               *values, '--export-stage', stage]
                    stream = (work/f'{stage}.log').open('ab')
                    try:
                        process = subprocess.Popen(command, cwd=program, stdout=stream, stderr=subprocess.STDOUT,
                                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    except BaseException:
                        stream.close(); raise
                    running[stage] = process, stream, folder
                    record('启动独立阶段', stage=stage, memory=available)
                elif not running:
                    if waiting_since is None:
                        waiting_since = time.monotonic(); record('等待内存释放', stage=stage, memory=available)
                    if time.monotonic()-waiting_since > 180:
                        raise RuntimeError(f'{stage}（导出阶段）等待内存超过 180 秒；保留诊断，请释放资源后重试。')
            time.sleep(.2)
        merge_stages(work, output, '--validate' in arguments)
        record('全部阶段通过并汇总', completed=done)
    finally:
        stop()
