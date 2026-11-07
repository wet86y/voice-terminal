"""构建日志分类：仅接受有明确无图回退证据且通过独立执行验收的捕获探测错误。"""
from __future__ import annotations
from pathlib import Path
from .aniflive_native import contained, digest


def classify_build_logs(package: Path, verified: dict) -> dict:
    validation = verified.get('validation', {})
    if (validation.get('enqueue_verified') is not True or validation.get('engine_count') != 9
            or validation.get('smoke_inference', {}).get('pytorch_fallback') is not False
            or verified.get('streaming_verified') is not True):
        raise ValueError('构建日志分类要求九引擎及独立完整、流式实际执行验收。')
    accepted = []
    for log in sorted(package.glob('engines/*/logs/*.log')):
        contained(package, log)
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
        for index, line in enumerate(lines):
            if '[TensorRT:ERROR]' not in line and '[TensorRT:INTERNAL_ERROR]' not in line:
                continue
            context = '\n'.join(lines[index:index + 6])
            if ('IExecutionContext::enqueueV3: Error Code 1:' not in line
                    or 'operation not permitted when stream is capturing' not in context
                    or 'The engine is not graph capturable. Falling back to not using CUDA graphs.' not in context):
                raise RuntimeError(f'构建存在未分类错误：{log.name} 第 {index + 1} 行；保留候选，不登记为可用。')
            accepted.append({'stage': log.stem, 'log': log.relative_to(package).as_posix(),
                'line': index + 1, 'log_sha256': digest(log), 'evidence': context,
                'classification': 'capture_probe_non_graph_fallback'})
    return {'capture_probe_fallbacks': accepted, 'execution_verified': True,
        'description': 'TensorRT 明确记录图捕获失败后回退普通无图执行；独立进程的九引擎、完整和流式合成均通过。',
        'fields': {'capture_probe_fallbacks': '已解释的捕获探测回退记录', 'execution_verified': '独立执行验证通过',
            'stage': '引擎阶段', 'log': '完整构建日志相对路径', 'line': '错误行号',
            'log_sha256': '完整原始日志摘要', 'evidence': '错误与明确回退上下文', 'classification': '错误分类'}}
