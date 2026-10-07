"""SmartTurn（语音结束判断）真实录音回放：分阶段计时与显卡内核验证。"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import logging
import math
from pathlib import Path
import socket
import statistics
import sys
import threading
import time

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.dont_write_bytecode = True  # 保持待测正式程序与运行环境只读。


def statistics_ms(values):
    ordered = sorted(values)
    return {"样本数": len(ordered), "中位毫秒": round(statistics.median(ordered), 3),
            "95%分位毫秒": round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
            "最大毫秒": round(ordered[-1], 3)}


def instrument(model):
    """为旧版模型补充相同计时，不修改概率计算与音频输入。"""
    extractor, native = model.extractor, model.session.run

    class Extractor:
        def __call__(self, *args, **kwargs):
            started = time.perf_counter()
            result = extractor(*args, **kwargs)
            model.last_latency = {"preprocess_ms": (time.perf_counter() - started) * 1000}
            model.last_features = result.input_features
            return result

    def run(*args, **kwargs):
        started = time.perf_counter()
        result = native(*args, **kwargs)
        model.last_latency["model_ms"] = (time.perf_counter() - started) * 1000
        return result

    model.extractor, model.session.run = Extractor(), run
    return model


def profile_cuda(model, model_dir: Path, output: Path) -> dict:
    import onnxruntime as ort
    import numpy as np
    output.parent.mkdir(parents=True, exist_ok=True)
    options = model.session.get_session_options()
    options.enable_profiling = True
    options.profile_file_prefix = str(output.with_suffix(""))
    providers = [(name, settings) for name, settings in model.session.get_provider_options().items()]
    session = ort.InferenceSession(str(model_dir / model.model_file), sess_options=options, providers=providers)
    session.disable_fallback()
    session.run(None, {session.get_inputs()[0].name:model.last_features.astype(np.float32)})
    trace_file = Path(session.end_profiling())
    trace = json.loads(trace_file.read_text(encoding="utf-8"))
    counts = Counter(item.get("args", {}).get("provider") for item in trace if item.get("args", {}).get("provider"))
    ops = Counter(item["args"].get("op_name") for item in trace
                  if item.get("args", {}).get("provider") == "CUDAExecutionProvider")
    if not ops or not any("MatMul" in op or op in {"Conv", "Gemm", "Attention"} for op in ops):
        raise RuntimeError("未确认 SmartTurn（语音结束判断）的模型计算内核实际执行于显卡")
    output.write_text(json.dumps({"提供程序调用数":dict(counts), "显卡计算节点":dict(ops),
                                  "完整追踪文件":str(trace_file)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"提供程序调用数":dict(counts), "显卡计算节点":dict(ops)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-root", type=Path, default=ROOT, help="待测源码／正式程序目录")
    parser.add_argument("--model-dir", type=Path, required=True, help="SmartTurn（语音结束判断）本地模型目录")
    parser.add_argument("--audio", type=Path, default=ROOT / ".local/cache/evaluation/audio/asr-finalization-sample.wav", help="16千赫兹单声道录音")
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True, help="请求的推理设备；实际回退会使基准失败")
    parser.add_argument("--iterations", type=int, default=10, help="每个录音片段的请求次数")
    parser.add_argument("--torch-threads", type=int, default=2, help="PyTorch（张量框架）预处理线程数，比较时保持一致")
    parser.add_argument("--profile-output", type=Path, help="保存显卡执行内核的诊断摘要与完整追踪")
    parser.add_argument("--output", type=Path, required=True, help="保存统计与逐次结果")
    args = parser.parse_args()
    if args.iterations < 1 or args.torch_threads < 1:
        parser.error("次数和线程数必须大于零")
    sys.path.insert(0, str(args.program_root.resolve()))
    logging.disable(logging.CRITICAL)
    import soundfile
    import torch
    import uvicorn
    import onnxruntime as ort
    from voice_terminal.analysis.smart_turn import SmartTurn
    from voice_terminal.analysis.worker import AnalysisLane, create_analysis_app
    from voice_terminal.analysis.client import AnalysisClient
    torch.set_num_threads(args.torch_threads)
    audio, rate = soundfile.read(args.audio, dtype="int16")
    if rate != 16000 or audio.ndim != 1:
        raise ValueError("录音必须是16千赫兹单声道")
    pcm = audio.tobytes()
    cases = {"两秒语音片段":pcm[:64000], "完整语音":pcm, "语音加300毫秒静音":pcm+b"\0\0"*4800,
             "两秒纯静音":b"\0\0"*32000}
    lane = AnalysisLane(model_name="SmartTurn（语音结束判断）")
    server = None
    thread = None
    bound = socket.socket()
    try:
        started = time.perf_counter()
        if hasattr(lane, "prepare"):
            lane.prepare(lambda:instrument(SmartTurn(args.model_dir, device=args.device)))
        else:
            lane.model = instrument(SmartTurn(args.model_dir, device=args.device))
        ready_ms = (time.perf_counter() - started) * 1000
        model = lane.model
        if model.device != args.device:
            raise RuntimeError(f"设备回退：请求 {args.device}，实际 {model.device}；{model.load_error}")
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_analysis_app({"endpoint":lane}), log_level="critical", lifespan="on"))
        thread = threading.Thread(target=lambda:server.run(sockets=[bound]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("隔离回放服务未能启动")
            await asyncio.sleep(.01)
        client = AnalysisClient(f"ws://127.0.0.1:{port}/analysis")
        health = (await client.health())["endpoint"]
        rows = []
        async def call(name, payload, index):
            started = time.perf_counter()
            response = await client.analyze("endpoint", payload, f"replay-{index}", 0, 300)
            return {"片段":name, "完整请求毫秒":(time.perf_counter()-started)*1000,
                    "服务处理毫秒":response["elapsed_ms"], "结束概率":response["result"]["probability"],
                    "结束判断":response["result"]["probability"] >= .5,
                    "分阶段毫秒":response.get("latency", dict(model.last_latency))}
        first = await call("完整语音", pcm, "first")
        await call("完整语音", pcm, "warmup")
        for index in range(args.iterations):
            for name, payload in cases.items():
                rows.append(await call(name, payload, f"{name}-{index}"))
        profile = profile_cuda(model, args.model_dir, args.profile_output) if args.profile_output and args.device == "cuda" else None
        result = {"说明":"使用本地模型、录音和独立本机服务；概率阈值0.5、判断超时300毫秒。孤立回放不能替代整套应用并发验收。",
                  "源码目录":str(args.program_root.resolve()), "解释器":sys.executable, "推理运行库版本":ort.__version__,
                  "预处理线程数":torch.get_num_threads(), "实际设备":model.device, "健康状态":health,
                  "模型摘要":hashlib.sha256((args.model_dir / getattr(model, "model_file", "smart-turn-v3.2-cpu.onnx")).read_bytes()).hexdigest(),
                  "录音摘要":hashlib.sha256(args.audio.read_bytes()).hexdigest(), "加载及预热毫秒":ready_ms,
                  "服务开放后首次请求":first, "完整请求":statistics_ms([row["完整请求毫秒"] for row in rows]),
                  "服务处理":statistics_ms([row["服务处理毫秒"] for row in rows]),
                  "分阶段":{key:statistics_ms([row["分阶段毫秒"][key] for row in rows if key in row["分阶段毫秒"]])
                            for key in {key for row in rows for key in row["分阶段毫秒"]}},
                  "显卡内核验证":profile, "逐次结果":rows,
                  "字段中文说明":{"executor_wait_ms":"提交任务到专用线程开始的等待毫秒", "native_ms":"线程中完整调用耗时毫秒",
                                  "preprocess_ms":"音频预处理耗时毫秒", "model_ms":"模型计算及必要数据传输毫秒"}}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({key:value for key,value in result.items() if key in {"实际设备", "完整请求", "服务处理", "分阶段", "显卡内核验证"}}, ensure_ascii=False), flush=True)
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(5)
        bound.close()
        lane.close()


if __name__ == "__main__":
    asyncio.run(main())
