"""ModelProcess（按需模型进程）：退出回收权重、运行库及原生缓存。

管道字段 pcm（编码音频）、result（推理结果）、error（错误原因）仅在父子进程间传递。
resource_status（最近一次原生调用的资源快照）和 tensor_cuda_build（显卡张量构建已加载）归属模型子进程。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading


_PREFIX = "VOICE_MODEL_RESULT="


class ModelProcess:
    def __init__(self, kind: str, model_dir: Path, *, device="cpu", fallback_device="cpu",
                 require_cuda: bool = False,  # require_cuda（严格使用图形处理器）。
                 ignore_host_fallback: bool = False,  # ignore_host_fallback（忽略宿主强制中央处理器回退）。
                 model_options: dict | None = None):  # model_options（模型专用参数）。
        self._lock = threading.RLock()
        self._responses = queue.Queue()
        self._process = None
        self._reader = None
        self.resource_status = None
        self.tensor_cuda_build = False
        self.requested_device = device
        self.fallback_device = fallback_device
        self.model_options = model_options or {}
        host_fallback = bool(os.environ.get("VOICE_GPU_FALLBACK")) and not ignore_host_fallback
        attempts = (("cpu",) if host_fallback else
                    (device, "cpu") if kind == "emotion" and device != "cpu" else (device,))
        previous_error = "宿主已切换到独立 CPU（中央处理器）依赖" if host_fallback and device != "cpu" else None
        for selected in attempts:
            try:
                metadata = self._start(kind, model_dir, selected, fallback_device,
                                       ignore_host_fallback, self.model_options)
                if selected != "cpu" and metadata.get("device") == "cpu" and len(attempts) > 1:
                    previous_error = metadata.get("load_error") or "显卡模型已回退到独立处理器进程"
                    self.close()
                    continue
                self.__dict__.update(metadata)
                self.requested_device = device
                self.fallback_device = fallback_device
                if previous_error:
                    self.load_error = str(previous_error)
                if require_cuda and str(self.device).startswith("cpu"):
                    error = self.load_error or "显式请求 cuda（图形处理器），但模型实际运行于 cpu（中央处理器）。"
                    self.close()
                    raise RuntimeError(error)
                return
            except BaseException as error:
                self.close()
                if selected == attempts[-1] or not isinstance(error, Exception):
                    raise
                previous_error = error

    def _start(self, kind, model_dir, device, fallback_device, ignore_host_fallback=False,
               model_options=None):
        from .paths import python_command

        self._responses = queue.Queue()
        environment = {**os.environ, "VOICE_TENSOR_DEVICE": device, "PYTHONDONTWRITEBYTECODE": "1"}
        cpu_python = environment.get("VOICE_CPU_PYTHON")
        executable = sys.executable
        if cpu_python:
            from .paths import AppPaths
            from .gpu_environment import worker_python
            paths = AppPaths.resolve()
            model_id = {"streaming": "paraformer", "offline": "paraformer", "emotion": "emotion",
                        "acoustic": "smart_turn"}.get(kind)
            role = {"streaming": "asr", "offline": "asr", "emotion": "analysis",
                    "acoustic": "analysis"}.get(kind)
            if model_id is not None:
                devices = {model_id: device}
                if role == "analysis":
                    # The nested process only owns this model; another model's
                    # auto preference must not pull a CPU child into the GPU runtime.
                    devices.update({name: "cpu" for name in ("emotion", "smart_turn") if name != model_id})
                executable = worker_python(paths.install_root, paths.data_root or paths.install_root / "data",
                                           role, cpu_python, devices)
        if ignore_host_fallback:
            environment.pop("VOICE_GPU_FALLBACK", None)
        environment["VOICE_RESOURCE_ROLE"] = {"streaming": "asr", "offline": "asr", "emotion": "analysis",
                                               "acoustic": "analysis"}.get(kind, environment.get("VOICE_RESOURCE_ROLE", "analysis"))
        command = python_command(executable, "voice_terminal.model_process", kind, str(model_dir),
                                 "--device", device, "--fallback-device", fallback_device,
                                 "--model-options", json.dumps(model_options or {}, separators=(",", ":")))
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, encoding="utf-8", bufsize=1, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._reader = threading.Thread(target=self._read, name="model-results", daemon=True)
        self._reader.start()
        return self._receive(240)

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def alive(self):
        """alive（进程存活）：异常退出不能继续报告模型就绪。"""
        return self._process is not None and self._process.poll() is None

    @property
    def error(self):
        if self._process is None:
            return "模型工作进程已关闭"
        code = self._process.poll()
        return f"模型工作进程已退出（退出码 {code}）" if code is not None else None

    def _read(self):
        try:
            for line in self._process.stdout:
                if line.startswith(_PREFIX):
                    self._responses.put(json.loads(line[len(_PREFIX):]))
        except (OSError, ValueError) as error:
            self._responses.put({"error": f"模型进程通信失败：{error}"})
        finally:
            self._responses.put({"error": "模型工作进程已退出"})

    def _receive(self, timeout):
        try:
            response = self._responses.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("模型工作进程响应超时") from None
        self.resource_status = response.get("resource_status", self.resource_status)
        self.tensor_cuda_build = response.get("tensor_cuda_build", self.tensor_cuda_build)
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    def _request(self, payload, timeout=120):
        with self._lock:
            if not self.alive:
                raise RuntimeError(self.error)
            try:
                self._process.stdin.write(json.dumps(payload) + "\n")
                self._process.stdin.flush()
                return self._receive(timeout)
            except (OSError, TimeoutError):
                self.close()
                raise

    def __call__(self, pcm):
        return self._request({"pcm": base64.b64encode(pcm).decode("ascii")})

    def infer_stream(self, turn_id: str, pcm: bytes, is_final: bool):
        """infer_stream（流式识别）：turn_id（发言轮次编号）隔离模型缓存。"""
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id（发言轮次编号）必须是非空字符串")
        return self._request({"operation": "infer", "turn_id": turn_id,
                              "pcm": base64.b64encode(pcm).decode("ascii"), "is_final": bool(is_final)})

    def clear_turn(self, turn_id: str):
        if not isinstance(turn_id, str) or not turn_id:
            return None
        return self._request({"operation": "clear_turn", "turn_id": turn_id}, timeout=10)

    def warmup(self):
        return None

    def close(self):
        with self._lock:
            process = self._process
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if self._reader is not None:
                self._reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            self._process = None
            self.resource_status = None


def _create_model(kind, model_dir, device, fallback_device, model_options=None):
    if kind == "offline":
        from .asr.dual_pass import OfflineParaformer
        return OfflineParaformer(model_dir, device)
    if kind == "emotion":
        from .analysis.perception import Emotion2vec
        return Emotion2vec(model_dir, device=device, fallback_device=fallback_device)
    if kind == "acoustic":
        from .analysis.perception import Yamnet
        return Yamnet(model_dir)
    if kind == "streaming":
        from .asr.streaming_model import StreamingParaformer
        options = model_options or {}
        return StreamingParaformer(model_dir, device=device, fallback_device=fallback_device,
                                   **options)
    raise ValueError("未知的按需模型")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("offline", "streaming", "emotion", "acoustic"), help="按需模型类型")
    parser.add_argument("model_dir", type=Path, help="受管本地模型目录")
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda", "auto"), help="请求计算设备")
    parser.add_argument("--fallback-device", default="cpu", choices=("cpu", "cuda"), help="失败时的回退设备")
    parser.add_argument("--model-options", default="{}", help="模型专用配置")
    arguments = parser.parse_args()

    def emit(result=None, error=None):
        from .resources import process_resource_status
        from .tensor_runtime import cuda_tensor_loaded
        payload = {"error": error} if error else {"result": result}
        payload.update(resource_status=process_resource_status(), tensor_cuda_build=cuda_tensor_loaded())
        print("\n" + _PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)

    model = None
    try:
        try:
            model_options = json.loads(arguments.model_options)
        except (TypeError, ValueError) as exc:
            raise ValueError("模型专用配置无效") from exc
        if not isinstance(model_options, dict):
            raise ValueError("模型专用配置必须是对象")
        from .resources import model_load_footprint, bootstrap_resources
        bootstrap_resources(os.environ.get("VOICE_RESOURCE_ROLE", "analysis"))
        with model_load_footprint() as footprint:
            model = _create_model(arguments.kind, arguments.model_dir, arguments.device,
                                  arguments.fallback_device, model_options)
            model.warmup()
        names = ("identity", "device", "requested_device", "runtime", "model_file", "warmed", "load_error",
                 "providers", "internal_vad_device")
        metadata = {name: getattr(model, name, None) for name in names}
        metadata["resource_footprint"] = footprint
        emit(metadata)
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if arguments.kind == "streaming" and request.get("operation") == "clear_turn":
                    model.clear_turn(request.get("turn_id"))
                    emit(True)
                else:
                    pcm = base64.b64decode(request["pcm"], validate=True)
                    if len(pcm) % 2 or len(pcm) > 16000 * 2 * 62:
                        raise ValueError("模型输入音频无效")
                    if arguments.kind == "streaming":
                        result = model.infer(request.get("turn_id"), pcm, request.get("is_final") is True)
                    else:
                        if not pcm:
                            raise ValueError("模型输入音频无效")
                        result = model(pcm)
                    emit(result)
            except Exception as error:
                emit(error=f"{type(error).__name__}: {error}")
            finally:
                line = request = pcm = None
    except Exception as error:
        emit(error=f"{type(error).__name__}: {error}")
    finally:
        close = getattr(model, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
