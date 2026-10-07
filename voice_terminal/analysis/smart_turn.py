from pathlib import Path
import time


class SmartTurn:
    def __init__(self, model_dir: Path, *, device: str = "cpu",
                 fallback_device: str = "cpu") -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        self.requested_device = device
        self.device = "cpu"
        self.warmed = False  # warmed（模型已在服务推理线程完成实际预热）。
        self.last_latency: dict[str, float] = {}  # last_latency（最近一次调用的分阶段耗时）。
        self.load_error: str | None = None
        self.extractor = WhisperFeatureExtractor.from_pretrained(str(model_dir), local_files_only=True)
        from ..compute import device_candidates
        last_error: Exception | None = None
        for candidate in device_candidates(device, "smart_turn", fallback_device):
            session = None
            try:
                model_file = "smart-turn-v3.2-gpu.onnx" if candidate == "cuda" else "smart-turn-v3.2-cpu.onnx"
                model_path = model_dir / model_file
                if not model_path.is_file():
                    raise FileNotFoundError(f"SmartTurn（语音结束判断）模型文件缺失：{model_file}")
                if candidate == "cuda":
                    if "CUDAExecutionProvider" not in ort.get_available_providers():
                        raise RuntimeError("ONNX Runtime（模型推理运行时）未提供 CUDA（显卡）后端")
                    # preload_dlls（预加载动态库）优先使用同一运行包内的 PyTorch（张量框架）库。
                    # 不要求客户机另行安装 CUDA Toolkit（显卡开发工具包）。
                    ort.preload_dlls()
                options = ort.SessionOptions()
                options.intra_op_num_threads = 1 if candidate == "cuda" else 2
                options.inter_op_num_threads = 1
                # allow_spinning（空闲线程自旋）关闭后不持续占用 CPU（中央处理器）与其他语音模型争抢。
                options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                providers = [
                    ("CUDAExecutionProvider", {
                        "use_tf32": "0",  # use_tf32（降低矩阵精度的加速）关闭，保留完整单精度计算。
                        "cudnn_conv_algo_search": "HEURISTIC",  # 卷积算法启发式选择，减少初始化搜索。
                        "cudnn_conv_use_max_workspace": "0",  # 限制卷积临时显存，避免预热扩大分配。
                    }),
                    "CPUExecutionProvider",
                ] if candidate == "cuda" else ["CPUExecutionProvider"]
                session = ort.InferenceSession(
                    str(model_path),
                    sess_options=options,
                    providers=providers,
                )
                active = session.get_providers()
                if candidate == "cuda" and "CUDAExecutionProvider" not in active:
                    raise RuntimeError("CUDA（显卡）提供程序初始化失败，运行时自动回退到了 CPU（中央处理器）")
                session.disable_fallback()  # 禁止推理失败后静默重建 CPU（中央处理器）会话。
                self.session = session
                self.input_name = session.get_inputs()[0].name  # input_name（模型输入名称）只读取一次。
                self.model_file = model_file  # model_file（实际使用的模型文件）。
                self.providers = active  # providers（实际启用的执行后端）。
                self.runtime = "ONNX Runtime（推理运行时）"  # runtime（实际加载引擎）。
                self.device = candidate
                # 固定八秒输入在接收业务请求前完成分配与真实计算；失败也走设备回退。
                self.warmup()
                self.load_error = (
                    f"CUDA（图形处理器）加载失败，已回退到 {self.device}：{type(last_error).__name__}: {last_error}"
                    if last_error is not None else None
                )
                break
            except Exception as exc:
                self.session = None  # 释放失败会话后，重新加载 CPU（中央处理器）量化模型。
                session = None
                self.warmed = False
                last_error = exc
        else:
            raise RuntimeError(f"SmartTurn ONNX Runtime（ONNX 推理运行时）初始化失败：{last_error}") from last_error

    def warmup(self) -> None:
        """warmup（预热）：模型构造与运行均由分析通道的同一个线程执行。"""
        if not self.warmed:
            self(b"\0\0" * 128000)
            self(b"\0\0" * 128000)
            self.warmed = True

    @staticmethod
    def new_run_options():
        import onnxruntime as ort
        return ort.RunOptions()

    def __call__(self, pcm: bytes, run_options=None) -> dict:
        import numpy as np
        started = time.perf_counter()
        if run_options is not None and run_options.terminate:
            raise RuntimeError("analysis cancelled")
        from ..audio.allocation import pcm_samples
        audio = pcm_samples(pcm, amplitude="int16")[-128000:] / 32768
        audio = np.pad(audio, (128000 - len(audio), 0))
        features = self.extractor(audio, sampling_rate=16000, return_tensors="np",
                                  padding="max_length", max_length=128000,
                                  truncation=True, do_normalize=True).input_features.astype(np.float32)
        prepared = time.perf_counter()
        if run_options is not None and run_options.terminate:
            raise RuntimeError("analysis cancelled")
        probability = float(self.session.run(
            None, {self.input_name: features}, run_options,
        )[0].reshape(-1)[0])
        finished = time.perf_counter()
        if not 0 <= probability <= 1:
            raise ValueError("invalid endpoint probability")
        self.last_latency = {
            "preprocess_ms": (prepared - started) * 1000,  # preprocess_ms（音频预处理毫秒）。
            "model_ms": (finished - prepared) * 1000,  # model_ms（模型计算及必要数据传输毫秒）。
        }
        return {"probability": probability}
