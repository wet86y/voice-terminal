"""ASR（流式语音识别）本机回放：真实权重、20 毫秒发送、独立收包与推理。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True  # 不向待测只读程序或随包运行环境写入编译缓存。


def statistics_ms(values: list[float]) -> dict:
    """统计毫秒耗时；95% 分位使用最邻近秩。"""
    if not values:
        return {"样本数": 0}
    ordered = sorted(values)
    return {"样本数": len(ordered), "中位毫秒": round(statistics.median(ordered), 2),
            "95%分位毫秒": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
            "最大毫秒": round(ordered[-1], 2)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program-root", type=Path, default=ROOT, help="待测源码／正式程序目录")
    parser.add_argument("--model-dir", type=Path, required=True, help="Paraformer（流式识别）本地模型目录")
    parser.add_argument("--audio", type=Path, default=ROOT / "reports/asr-finalization-sample-20261002.wav", help="16 千赫兹单声道录音")
    parser.add_argument("--device", choices=("cpu", "cuda"), required=True, help="实际请求的计算设备")
    parser.add_argument("--iterations", type=int, default=6, help="顺序回放次数")
    parser.add_argument("--output", type=Path, required=True, help="原始结果与统计文件")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("回放次数必须大于零")
    sys.path.insert(0, str(args.program_root.resolve()))
    logging.disable(logging.CRITICAL)
    import soundfile
    import torch
    import websockets
    from voice_terminal.asr.client import AsrWorkerClient
    from voice_terminal.asr.protocol import AudioChunk
    from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection

    audio, rate = soundfile.read(args.audio, dtype="int16")
    if rate != 16000 or audio.ndim != 1:
        raise ValueError("录音必须是 16 千赫兹单声道")
    pcm = audio.tobytes()
    engine = AsrWorkerEngine(model_dir=args.model_dir, device=args.device)
    windows: list[dict] = []
    original = engine._infer_window
    async def measured(turn_id, window, final):
        started = time.perf_counter()
        text = await original(turn_id, window, final)
        windows.append({"最终刷新": final, "音频字节": len(window),
                        "耗时毫秒": (time.perf_counter() - started) * 1000})
        return text
    engine._infer_window = measured
    started = time.perf_counter()
    try:
        # 旧版本没有 prepare（专用线程准备）；仍可用同一工具获得可比结果。
        if hasattr(engine, "prepare"):
            await engine.prepare()
        else:
            await asyncio.to_thread(engine.load)
        ready_ms = (time.perf_counter() - started) * 1000
        if engine.device != args.device:
            raise RuntimeError(f"设备回退：请求 {args.device}，实际 {engine.device}")
        print(json.dumps({"阶段":"已就绪", "实际设备":engine.device, "准备毫秒":round(ready_ms)}, ensure_ascii=False), flush=True)
        turns = []
        async def handler(socket):
            await handle_asr_connection(socket, engine)
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
            for index in range(args.iterations):
                received = []
                final = asyncio.get_running_loop().create_future()
                started = time.perf_counter()
                async def on_result(message):
                    received.append({"相对毫秒":(time.perf_counter() - started) * 1000, "结果":message})
                    if message.get("type") == "asr.final" and not final.done():
                        final.set_result(message)
                    elif message.get("type") == "error" and not final.done():
                        final.set_exception(RuntimeError(message))
                client = AsrWorkerClient(url, on_result=on_result)
                try:
                    await client.connect()
                    started = time.perf_counter()
                    for offset in range(0, len(pcm), 640):
                        await asyncio.sleep(max(0.0, started + offset / 32000 - time.perf_counter()))
                        await client.send_audio(AudioChunk(f"replay-{index}", pcm[offset:offset + 640]))
                    await asyncio.sleep(max(0.0, started + len(pcm) / 32000 - time.perf_counter()))
                    final_sent = time.perf_counter()
                    await client.send_audio(AudioChunk(f"replay-{index}", b"", is_final=True))
                    result = await asyncio.wait_for(final, 15)
                    final_wait_ms = (time.perf_counter() - final_sent) * 1000
                    partials = [row["相对毫秒"] for row in received if row["结果"].get("type") == "asr.partial"]
                    turns.append({"最终等待毫秒":final_wait_ms, "首次文字毫秒":partials[0] if partials else None,
                                  "文本":result["text"], "结果原文":received})
                    await client.send_finish(f"replay-{index}")
                    print(json.dumps({"阶段":"回放完成", "次数":index+1, "最终等待毫秒":round(final_wait_ms, 2)}, ensure_ascii=False), flush=True)
                finally:
                    await client.close()
        result = {"说明":"同一录音以20毫秒音频帧按实际时钟发送；本机套接字保持收包与原生推理独立，不开启麦克风或调用在线模型。",
                  "源码目录":str(args.program_root.resolve()), "解释器":sys.executable, "实际设备":engine.device,
                  "推理框架版本":torch.__version__, "数值计算线程":torch.get_num_threads(), "加载及预热毫秒":ready_ms,
                  "录音摘要":hashlib.sha256(args.audio.read_bytes()).hexdigest(), "录音秒数":len(pcm)/32000,
                  "最终等待":statistics_ms([row["最终等待毫秒"] for row in turns]),
                  "模型窗":statistics_ms([row["耗时毫秒"] for row in windows if not row["最终刷新"]]),
                  "轮次":turns, "模型窗原文":windows}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps({"最终等待":result["最终等待"], "模型窗":result["模型窗"], "输出":str(args.output)}, ensure_ascii=False), flush=True)
    finally:
        if hasattr(engine, "close"):
            engine.close()


if __name__ == "__main__":
    asyncio.run(main())
