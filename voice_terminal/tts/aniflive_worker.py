"""隔离子进程中的输入准备与真实合成验收。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import wave

from .aniflive_native import contained, digest, nominal_vram_supported


def tensor_digest(values):
    import torch
    result = hashlib.sha256()
    for key, value in sorted(values.items()):
        if isinstance(value, torch.Tensor):
            result.update(key.encode())
            result.update(str((str(value.dtype), list(value.shape))).encode())
            result.update(value.detach().cpu().contiguous().view(torch.uint8).numpy().tobytes())
    return result.hexdigest()


def prepare_base(input_path):
    from aniflive_tts.inspector import _load, inspect_pair
    import torch
    values = json.loads(input_path.read_text(encoding='utf-8'))
    if values.get('metadata_recipe'):
        path = Path(values['sovits'])
        if values['metadata_recipe'] != 'known-v2proplus-base-v1' or digest(path) != 'd42a22bbbf65fb2bbdd45ad6a66841156977db45c7aabe0a6992ff378d9c7d3b':
            raise ValueError('底模元数据修正只接受精确匹配的官方权重。')
        payload = _load(path, allow_unsafe_pickle=False)
        state = payload.get('weight') or payload.get('state_dict')
        if not isinstance(state, dict):
            raise ValueError('底模缺少可核对的权重集合。')
        before = tensor_digest(state)
        config = payload['config']
        train = config['train'] if isinstance(config, dict) else config.train
        if not isinstance(train, dict):
            raise ValueError('底模训练元数据格式不符合固定兼容配方。')
        train['pretrained_s2G'] = 'GPT-SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth'
        output = path.with_name('s2Gv2ProPlus-aniflive.pth')
        torch.save(payload, output)
        restored = _load(output, allow_unsafe_pickle=False)
        after = tensor_digest(restored.get('weight') or restored.get('state_dict'))
        if before != after:
            raise ValueError('元数据处理改变了模型张量，拒绝转换。')
        values['sovits'] = str(output)
        values['metadata_provenance'] = {'source_sha256': digest(path), 'derived_sha256': digest(output),
                                          'tensor_sha256_before': before, 'tensor_sha256_after': after}
    inspection = inspect_pair(Path(values['gpt']), Path(values['sovits']), allow_unsafe_pickle=False)
    import soundfile
    reference = soundfile.info(values['reference'])
    if not 3 <= reference.duration <= 10 or reference.channels not in {1, 2}:
        raise ValueError('参考录音必须为 3–10 秒、单声道或双声道的可解码音频。')
    transcript = Path(values['reference_text']).read_text(encoding='utf-8').strip()
    if not 1 <= len(transcript) <= 2000:
        raise ValueError('参考录音必须具有 1–2000 字的准确转写。')
    values['inspection'] = inspection
    input_path.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'inspection': inspection, 'imports': import_report()}


def shared(local):
    from .aniflive_shared import specification, activate
    program = Path(__file__).resolve().parents[2]
    lock, _ = specification(program, local)
    return activate(program, local, [(row, local / 'models/aniflive-tts/shared' / row['path']) for row in lock['files']])


def import_report():
    import torch, transformers, tokenizers, huggingface_hub
    return {module.__name__: {'version': module.__version__, 'path': module.__file__}
            for module in (torch, transformers, tokenizers, huggingface_hub)}


def probe():
    from ..resources import model_thread_budget
    import torch
    import tensorrt
    import transformers
    import tokenizers
    import huggingface_hub
    if not torch.cuda.is_available():
        raise RuntimeError('AnifLive 需要可用的 NVIDIA（英伟达）显卡，不支持处理器回退。')
    properties = torch.cuda.get_device_properties(0)
    free, total = torch.cuda.mem_get_info(0)
    if not nominal_vram_supported(properties.total_memory, windows=os.name == 'nt') or (properties.major, properties.minor) < (7, 5):
        raise RuntimeError('显卡不满足 AnifLive 的计算能力或显存要求。')
    if free < 4 * 1024**3:
        raise RuntimeError('可用显存不足 4 GiB（吉比字节），请释放冲突占用后重试；未改变现用模型。')
    return {'torch': torch.__version__, 'tensorrt': tensorrt.__version__, 'cuda': torch.version.cuda,
            'export_thread_budget': model_thread_budget(),
            'gpu': properties.name, 'compute_capability': [properties.major, properties.minor],
            'vram_bytes': properties.total_memory, 'free_vram_bytes': free,
            'imports': {**import_report(), 'tensorrt': {'version': tensorrt.__version__, 'path': tensorrt.__file__}}}


def validate(package, local):
    from aniflive_tts.validate import validate_model_package
    source = local / 'runtimes/aniflive-tts/source/minimal_inference'
    shared_root = local / 'models/aniflive-tts/shared'
    report = validate_model_package(package, enqueue=True, shared_dir=shared_root, source_dir=source,
                                    text='你好，今天我们验证本地语音模型是否能够正常工作。', language='zh')
    # 完整语音和流式解码均需验证，静态反序列化不等于实际执行通过。
    import aniflive_tts.streaming as streaming
    original = streaming.TensorRTFixedReferenceStreamer
    class ValidatedStreamer(original):
        def streaming_chunk_length(self):
            return min(getattr(self, "_validation_tokens", 48), super().streaming_chunk_length())
    streaming.TensorRTFixedReferenceStreamer = ValidatedStreamer
    from aniflive_tts.service import SERVICE, SynthesisOptions
    options = SynthesisOptions(text='你好，这是中文长句与流式输出的验证。我们需要连续生成音频，并确认输出完整、采样率正确。',
        text_language='zh', top_k=15, top_p=1.0, temperature=1.0, speed=1.0,
        pause_length=.3, noise_scale=.5, cut_punc='。！？.!?、，；：', seed=1234)
    SERVICE.load()
    try:
        results = {}
        for tokens in (48, 96):
            SERVICE._service._streamer._validation_tokens = tokens
            chunks, pcm, first = 0, bytearray(), None
            start = time.perf_counter()
            for chunk in SERVICE.stream_pcm(options):
                if chunk:
                    first = first if first is not None else time.perf_counter() - start
                    chunks += 1
                    pcm.extend(chunk)
                    if len(pcm) > 32000 * 2 * 120:
                        raise RuntimeError('验证音频超出最大时长。')
            if len(pcm) < 6400 or len(pcm) % 2:
                raise RuntimeError('流式验证音频为空、过短或采样不完整。')
            output = contained(local, package.parent / f'streaming-{tokens}.wav')
            with wave.open(str(output), 'wb') as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(32000)
                stream.writeframes(pcm)
            results[str(tokens)] = {'chunks': chunks, 'first_audio_seconds': first,
                'elapsed_seconds': time.perf_counter() - start, 'audio_bytes': len(pcm),
                'sha256': hashlib.sha256(pcm).hexdigest(), 'audio_file': output.name}
        return {'validation': report, 'streaming_verified': True, 'streaming': results,
                'imports': import_report()}
    finally:
        SERVICE.unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare-base', 'shared', 'probe', 'validate', 'package-check'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--package', type=Path)
    parser.add_argument('--result', type=Path, required=True)
    args = parser.parse_args()
    local = args.root.absolute()
    result = contained(local, args.result.absolute())
    if args.operation == 'prepare-base':
        value = prepare_base(contained(local, args.input.absolute()))
    elif args.operation == 'shared':
        value = shared(local)
    elif args.operation == 'probe':
        value = probe()
    elif args.operation == 'package-check':
        from aniflive_tts.model_package import validate_checksums
        value = {'files_verified': len(validate_checksums(contained(local, args.package.absolute())))}
    else:
        value = validate(contained(local, args.package.absolute()), local)
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
