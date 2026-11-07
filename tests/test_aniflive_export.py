"""独立阶段导出的内存回退、错误分类及完整产物门禁。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_terminal.tts import aniflive_export as export


def test_parallel_requires_physical_and_commit_headroom():
    assert export.permits_parallel(['ssl'], 'gpt', (24*export.GIB, 32*export.GIB))
    assert not export.permits_parallel(['ssl'], 'gpt', (4*export.GIB, 32*export.GIB))
    assert not export.permits_parallel(['ssl'], 'gpt', (24*export.GIB, 8*export.GIB))
    assert not export.permits_parallel(['ssl', 'gpt'], 'sv', (64*export.GIB, 64*export.GIB))


@pytest.mark.parametrize('error', [MemoryError(), RuntimeError('Could not allocate bytes object!'),
                                    RuntimeError('DefaultCPUAllocator: not enough memory')])
def test_known_allocation_failure_can_fall_back(error):
    assert export.memory_failure(error)


@pytest.mark.parametrize('message', ['GPT encoder PyTorch to ONNX validation failed',
                                     'CUDA out of memory', 'shape mismatch'])
def test_other_failures_never_trigger_cpu_memory_retry(message):
    assert not export.memory_failure(RuntimeError(message))


def test_unknown_upstream_structure_is_rejected():
    with pytest.raises(ValueError, match='结构不匹配'):
        export.stage_export_source('def export_onnx(args): pass')


def test_windows_memory_exit_codes_exclude_cancel_and_unknown_crash(monkeypatch):
    monkeypatch.setattr(export, 'os', SimpleNamespace(name='nt'))
    assert export.process_memory_failure(0xc0000017)
    assert export.process_memory_failure(0xc000012d-2**32)
    assert not export.process_memory_failure(1)
    assert not export.process_memory_failure(0xc000013a)
    assert not export.process_memory_failure(0xc0000005)


def test_insufficient_memory_wait_has_a_bound_and_does_not_start_process(tmp_path, monkeypatch):
    local = tmp_path/'local';local.mkdir()
    ticks = iter((0, 181))
    monkeypatch.setattr(export, 'memory_available', lambda: (export.GIB, export.GIB))
    monkeypatch.setattr(export.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(export.time, 'sleep', lambda _: None)
    monkeypatch.setattr(export.subprocess, 'Popen', lambda *a, **k: pytest.fail('内存不足不得启动阶段'))
    with pytest.raises(RuntimeError, match='等待内存超过 180 秒'):
        export.export_stages(local, tmp_path, tmp_path/'export_onnx.py', ['--output_dir', str(local/'output')])
    assert not (local/'output').exists()


def make_stage(folder, stage):
    folder.mkdir(exist_ok=True)
    names = {'ssl': ['ssl'], 'bert': ['bert'], 'gpt': ['gpt_encoder', 'gpt_step'],
             'sovits': ['vq_encoder', 'sovits', 'sovits_stream', 'spectrogram'], 'sv': ['sv_embedding']}
    for name in names[stage]:
        (folder/f'{name}.onnx').write_bytes(b'fixture')
    if stage == 'sovits':
        (folder/'config.json').write_text('{}')
    models = sorted(export.MODELS[stage])
    rows = [{'passed':True, 'model_name':models[i % len(models)]} for i in range(export.COUNTS[stage])]
    (folder/'validation_report.json').write_text(json.dumps(rows))


def test_merge_refuses_missing_or_failed_validation_before_output(tmp_path):
    for stage in export.STAGES:
        make_stage(tmp_path/stage, stage)
    report = tmp_path/'gpt/validation_report.json'
    original = report.read_text()
    rows = json.loads(original); rows.pop()
    report.write_text(json.dumps(rows))
    with pytest.raises(RuntimeError, match='校验未全部通过'):
        export.merge_stages(tmp_path, tmp_path/'output', True)
    assert not (tmp_path/'output').exists()
    report.write_text(original)
    export.merge_stages(tmp_path, tmp_path/'output', True)
    assert len(json.loads((tmp_path/'output/validation_report.json').read_text())) == 105


@pytest.mark.parametrize('failure_stage', ['bert', 'gpt'])
@pytest.mark.parametrize('failure', ['cpu_memory', 'validation', 'repeat_memory'])
def test_scheduler_retries_only_memory_and_keeps_completed_stages(tmp_path, monkeypatch, failure, failure_stage):
    local = tmp_path/'local';local.mkdir()
    starts, stops = [], []
    attempts = {}
    class Process:
        def __init__(self, command, **kwargs):
            self.stage = command[command.index('--export-stage')+1]
            self.folder = Path(command[command.index('--output_dir')+1])
            starts.append(self.stage)
            attempts[self.stage] = attempts.get(self.stage, 0)+1
            # 第二个并行阶段失败，迫使终止仍活动的第一阶段；其后仅串行执行。
            self.code = None
            self.fail = self.stage == failure_stage and (attempts[self.stage] == 1 or failure == 'repeat_memory')
        def poll(self):
            if self.code is not None:
                return self.code
            if self.stage == 'ssl' and len(starts) == 1:
                return None
            if self.stage == 'ssl' and failure_stage == 'bert' and attempts.get('bert') == 1 and attempts.get('ssl') == 1:
                return None
            if self.fail:
                if failure != 'validation':
                    (self.folder/'stage-result.json').write_text(json.dumps({'kind':'cpu_memory','stage':self.stage}))
                self.code = 1
            else:
                make_stage(self.folder, self.stage); self.code = 0
            return self.code
        def terminate(self):
            stops.append(self.stage);self.code = -1
        def wait(self):
            return self.code
    monkeypatch.setattr(export.subprocess, 'Popen', Process)
    monkeypatch.setattr(export, 'memory_available', lambda: (64*export.GIB, 64*export.GIB))
    monkeypatch.setattr(export.time, 'sleep', lambda _: None)
    if failure == 'cpu_memory':
        export.export_stages(local, tmp_path, tmp_path/'export_onnx.py', ['--output_dir', str(local/'output'), '--validate'])
        assert attempts[failure_stage] == 2
        if failure_stage == 'bert':
            assert stops == ['ssl']
            assert all(attempts[s] == 1 for s in ('gpt', 'sovits', 'sv'))
        else:
            assert attempts['ssl'] == attempts['bert'] == 1
    else:
        with pytest.raises(RuntimeError, match='串行重试后仍内存不足' if failure == 'repeat_memory' else '失败，退出码'):
            export.export_stages(local, tmp_path, tmp_path/'export_onnx.py', ['--output_dir', str(local/'output'), '--validate'])
        assert attempts[failure_stage] == (2 if failure == 'repeat_memory' else 1)
        assert not (local/'output').exists()
