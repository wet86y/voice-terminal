"""CAM++ 工作进程往返测速；合成音频不代表真实身份准确率。"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def asr_load(ready: Path) -> None:
    """Paraformer（流式识别模型）持续计算，用于声纹并发预算验证。"""
    import numpy as np
    from voice_terminal.resources import bootstrap_resources
    bootstrap_resources("asr")
    from voice_terminal.asr.worker import AsrWorkerEngine
    engine = AsrWorkerEngine(model_dir=ROOT / ".local/models/funasr/paraformer-zh-streaming", device="cpu")
    engine.load()
    ready.write_text("READY",encoding="utf-8")
    pcm = (4000*np.sin(2*np.pi*180*np.arange(9600)/16000)).astype("<i2").tobytes()
    index = 0
    while True:
        await engine.infer(f"stress-{index}",pcm,True)
        index += 1


async def measure(stress_asr: bool = False) -> None:
    import numpy as np
    from voice_terminal.speaker.client import SpeakerWorkerClient

    output = ROOT / ".local/cache/campplus-validation"
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = output / "worker.yaml"
    config.write_text(f"speaker:\n  model_id: campplus-zh-cn-common\nspeaker_worker:\n  port: {port}\ncompute:\n  devices:\n    campplus: cpu\n", encoding="utf-8")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    command = [sys.executable, "-B", "-X", "utf8", "-c",
               "import sys;sys.path.insert(0,sys.argv[1]);from voice_terminal.server.product import ProductSettingsStore;ProductSettingsStore.effective_config=lambda self:self.config;from voice_terminal.speaker.worker import main;sys.argv=['speaker','--config',sys.argv[2]];main()",
               str(ROOT), str(config)]
    client = SpeakerWorkerClient(f"ws://127.0.0.1:{port}/speaker")
    stress_process = None
    stress_log = None
    with (output / "worker.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"声纹工作进程启动失败，日志：{output / 'worker.log'}")
                try:
                    await client.connect()
                    health = await client.health()
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            else:
                raise TimeoutError("声纹工作进程预热未完成")
            rows = []
            for duration_ms in (45, 400, 800, 3200):
                count = duration_ms * 16
                t = np.arange(count) / 16000
                pcm = (4000 * (np.sin(2*np.pi*180*t) + .3*np.sin(2*np.pi*370*t))).astype("<i2").tobytes()
                timings = []
                for _ in range(35):
                    started = time.perf_counter()
                    vector = await client.embed_pcm(pcm)
                    timings.append((time.perf_counter()-started)*1000)
                    assert len(vector) == 192 and np.isfinite(vector).all()
                    assert abs(np.linalg.norm(vector)-1) < 1e-5
                rows.append({"audio_ms": duration_ms, "iterations": len(timings),
                             "p50_ms": float(np.percentile(timings,50)),
                             "p95_ms": float(np.percentile(timings,95)),
                             "max_ms": max(timings), "over_budget": sum(x>100 for x in timings)})
            stress_rows = []
            if stress_asr:
                ready = output / f"asr-ready-{os.getpid()}.txt"
                stress_log = (output / "asr-load.log").open("wb")
                stress_process = subprocess.Popen([sys.executable,"-B","-X","utf8",str(Path(__file__).resolve()),"--asr-load",str(ready)],
                    cwd=ROOT,env=environment,stdout=stress_log,stderr=stress_log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                deadline = time.monotonic()+120
                while not ready.is_file():
                    if stress_process.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError("Paraformer并发模型未就绪，见asr-load.log（并发模型日志）")
                    await asyncio.sleep(.5)
                ready.unlink()
                for duration_ms in (400,3200):
                    t=np.arange(duration_ms*16)/16000
                    pcm=(4000*np.sin(2*np.pi*180*t)).astype("<i2").tobytes()
                    timings=[]
                    for _ in range(35):
                        started=time.perf_counter()
                        await client.embed_pcm(pcm)
                        timings.append((time.perf_counter()-started)*1000)
                    stress_rows.append({"audio_ms":duration_ms,"iterations":35,"p50_ms":float(np.percentile(timings,50)),
                        "p95_ms":float(np.percentile(timings,95)),"max_ms":max(timings),"over_budget":sum(x>100 for x in timings)})
            result = {"说明": "真实隔离进程、协议传输、特征提取与推理总耗时；合成音频仅验证计算与性能。",
                      "health": {k: health.get(k) for k in ("model_id","runtime","device","embedding_dim","model_version")},
                      "baseline": rows,"concurrent_paraformer_cpu":stress_rows}
            (output / "result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            if stress_process is not None:
                if stress_process.poll() is None: stress_process.terminate()
                await asyncio.to_thread(stress_process.wait,timeout=10)
            if stress_log is not None: stress_log.close()
            await client.close()
            if process.poll() is None:
                process.terminate()  # 只结束本次验证创建的进程。
            await asyncio.to_thread(process.wait, timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stress-asr",action="store_true",help="加入Paraformer持续原生推理负载")
    parser.add_argument("--asr-load",type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    asyncio.run(asr_load(args.asr_load) if args.asr_load else measure(args.stress_asr))
