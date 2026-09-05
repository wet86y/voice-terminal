from pathlib import Path


class SmartTurn:
    def __init__(self, model_dir: Path) -> None:
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_dir / "smart-turn-v3.2-cpu.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])
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
