from pathlib import Path


class SmartTurn:
    def __init__(self, model_dir: Path, *, device: str = "cpu",
                 fallback_device: str = "cpu") -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        self.requested_device = device
        self.device = "cpu"
        self.load_error: str | None = None
        from ..compute import device_candidates
        last_error: Exception | None = None
        for candidate in device_candidates(device, "smart_turn", fallback_device):
            try:
                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if candidate == "cuda" else ["CPUExecutionProvider"]
                session = ort.InferenceSession(
                    str(model_dir / "smart-turn-v3.2-cpu.onnx"),
                    sess_options=options,
                    providers=providers,
                )
                active = session.get_providers()
                self.session = session
                self.device = "cuda" if "CUDAExecutionProvider" in active else "cpu"
                self.load_error = (
                    f"CUDA（图形处理器）加载失败，已回退到 {self.device}：{type(last_error).__name__}: {last_error}"
                    if last_error is not None else None
                )
                break
            except Exception as exc:
                last_error = exc
        else:
            raise RuntimeError(f"SmartTurn ONNX Runtime（ONNX 推理运行时）初始化失败：{last_error}") from last_error
        self.extractor = WhisperFeatureExtractor.from_pretrained(str(model_dir), local_files_only=True)

    def __call__(self, pcm: bytes) -> dict:
        import numpy as np
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32)[-128000:] / 32768
        audio = np.pad(audio, (128000 - len(audio), 0))
        features = self.extractor(audio, sampling_rate=16000, return_tensors="np",
                                  padding="max_length", max_length=128000,
                                  truncation=True, do_normalize=True).input_features.astype(np.float32)
        probability = float(self.session.run(None, {self.session.get_inputs()[0].name: features})[0].reshape(-1)[0])
        if not 0 <= probability <= 1:
            raise ValueError("invalid endpoint probability")
        return {"probability": probability}
