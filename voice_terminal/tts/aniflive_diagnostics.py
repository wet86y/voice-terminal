"""显式半精度步骤引擎诊断；不改变正式模型登记或语义分块设置。"""
from __future__ import annotations
import json
from pathlib import Path

from .aniflive_native import contained, digest


def install_step_candidate(streaming, upstream, receipt_path: Path, package: Path, local: Path):
    """候选必须绑定完整正式构建身份、原图、原引擎及输入输出契约。"""
    receipt_path = contained(local, receipt_path.absolute())
    values = json.loads(receipt_path.read_text(encoding='utf-8'))
    baseline = json.loads((package / 'application-receipt.json').read_text(encoding='utf-8'))
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    fingerprint = manifest['active_engine_fingerprint']
    engines = contained(package, package / 'engines' / fingerprint)
    metadata = json.loads((engines / 'engine-manifest.json').read_text(encoding='utf-8'))
    if (values.get('schema') != 1 or values.get('kind') != 'aniflive-fp16-step-diagnostic'
            or values.get('baseline_identity') != baseline['identity']
            or values.get('baseline_engine_sha256') != digest(engines / 'gpt_step.engine')
            or values.get('source_sha256') != digest(package / 'onnx/gpt_step.onnx')
            or values.get('io_tensors') != metadata['build_results']['gpt_step']['io_tensors']
            or values.get('precision') != 'fp16'):
        raise ValueError('半精度诊断候选与当前完整构建身份或输入输出契约不匹配。')
    engine = contained(receipt_path.parent, receipt_path.parent / values['engine_file'])
    if digest(engine) != values.get('sha256'):
        raise ValueError('半精度诊断候选内容摘要不符。')
    expected = baseline['identity']['hardware']
    original = streaming.TensorRTFixedReferenceStreamer

    class CandidateStreamer(original):
        def __init__(self, runtime, *args, **kwargs):
            import torch
            import tensorrt
            if (tensorrt.__version__ != expected['tensorrt']
                    or torch.cuda.get_device_name(runtime.device) != expected['gpu']
                    or list(torch.cuda.get_device_capability(runtime.device)) != expected['compute_capability']):
                raise ValueError('诊断候选与当前显卡或推理引擎版本不匹配。')
            from run_trt_inference import TRTModule
            runtime.model_gpt_step = TRTModule(engine, device=runtime.device, stream=runtime.stream)
            super().__init__(runtime, *args, **kwargs)

    streaming.TensorRTFixedReferenceStreamer = CandidateStreamer
    original_health = upstream.SERVICE.health
    def health():
        result = original_health()
        result['diagnostic_candidate'] = {'sha256': values['sha256'], 'baseline_fingerprint': fingerprint,
            'precision': 'mixed-fp16', 'fields': {'sha256': '诊断步骤引擎实际摘要',
                'baseline_fingerprint': '完整基底构建指纹', 'precision': '仅步骤引擎的混合半精度诊断'}}
        return result
    upstream.SERVICE.health = health
