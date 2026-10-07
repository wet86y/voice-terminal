"""ASR（语音识别）启动对照：独立进程、真实权重和固定录音，不访问麦克风或在线服务。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True, help="本地流式识别模型目录")
    parser.add_argument("--audio", type=Path, default=ROOT / "reports/asr-finalization-sample-20261002.wav", help="固定的单声道录音")
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True, help="请求计算设备")
    parser.add_argument("--import-mode", choices=("full", "selective"), required=True, help="完整导入或按需导入")
    parser.add_argument("--threads", type=int, default=4, help="两种导入路径使用相同的计算线程预算")
    parser.add_argument("--output", type=Path, required=True, help="诊断结果文件")
    args = parser.parse_args()
    if not 1 <= args.threads <= 256:
        parser.error("计算线程预算须为 1—256")
    os.environ.update(VOICE_ASR_IMPORT_MODE=args.import_mode, VOICE_RESOURCE_ROLE="asr", VOICE_MODEL_THREADS=str(args.threads))
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = str(args.threads)
    logging.disable(logging.CRITICAL)
    import psutil
    process = psutil.Process()
    peaks = {"常驻字节": 0, "私有提交字节": 0}
    stop = threading.Event()
    def sample():
        while not stop.is_set():
            memory = process.memory_info()
            peaks["常驻字节"] = max(peaks["常驻字节"], memory.rss)
            peaks["私有提交字节"] = max(peaks["私有提交字节"], getattr(memory, "private", 0))
            stop.wait(.05)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    from voice_terminal.asr.worker import AsrWorkerEngine
    engine = AsrWorkerEngine(model_dir=args.model_dir.resolve(), device=args.device)
    try:
        await engine.prepare()
        ready_seconds = time.perf_counter() - started
        if not engine.model_loaded or not engine.model_warmed:
            raise RuntimeError("真实模型或预热未就绪")
        import soundfile
        audio, rate = soundfile.read(args.audio, dtype="int16")
        if rate != 16000 or audio.ndim != 1:
            raise ValueError("录音必须是 16 千赫兹单声道")
        pcm = audio.tobytes()
        partials = []
        for offset in range(0, len(pcm), 640):
            result = await engine.infer("startup-replay", pcm[offset:offset + 640], False)
            if result.emitted:
                partials.append(result.text)
        final = await engine.infer("startup-replay", b"", True)
        import torch
        from funasr.register import tables
        from voice_terminal.asr.model_loader import _COMPONENTS
        result = {"导入方式": args.import_mode, "请求设备": args.device, "实际设备": engine.device,
                  "设备回退说明": engine.load_error, "模型已加载": engine.model_loaded, "模型已预热": engine.model_warmed,
                  "准备耗时秒": round(ready_seconds, 4), "阶段耗时毫秒": engine.startup_timings_ms,
                  "框架模块数": sum(name == "funasr" or name.startswith("funasr.") for name in sys.modules),
                  "文本模型库已加载": "transformers" in sys.modules, "聚类库已加载": "sklearn" in sys.modules,
                  "实际计算线程数": torch.get_num_threads(), "录音摘要": hashlib.sha256(args.audio.read_bytes()).hexdigest(),
                  "模型组件来源": {key: getattr(tables, registry)[name].__module__
                                   for key, (name, registry, _) in _COMPONENTS.items()},
                  "张量显存分配峰值字节": torch.cuda.max_memory_allocated() if engine.device == "cuda" else None,
                  "最终文本": final.text, "累计部分文本": partials, "空最终包已处理": True,
                  "内存峰值": dict(peaks), "解释器": sys.executable,
                  "测量条件": "独立新进程；不清理系统文件缓存；同一线程预算；含真实加载和预热；录音按顺序快速回放"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        stop.set()
        sampler.join(timeout=1)
        engine.close()


if __name__ == "__main__":
    asyncio.run(main())
