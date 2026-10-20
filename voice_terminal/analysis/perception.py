"""Perception models（背景感知模型）：纯波形输入，不生成文件。"""
from __future__ import annotations

import csv
from pathlib import Path


class Emotion2vec:
    def __init__(self, model_dir: Path, *, device="cpu", fallback_device="cpu"):
        from .snapshot import verify_snapshot
        self.identity = verify_snapshot(model_dir, "emotion2vec-plus-base")
        from ..asr.model_loader import auto_model_class
        from ..compute import device_candidates, loaded_funasr_device
        self.requested_device = device
        self.model_file = str(model_dir / "model.pt")
        self.runtime = "PyTorch（推理框架）"
        self.load_error = None
        last = None
        for candidate in device_candidates(device, "emotion", fallback_device):
            try:
                self.model = auto_model_class(model_dir)(model=str(model_dir), device=candidate,
                    disable_update=True, disable_pbar=True, ncpu=4)
                self.device = loaded_funasr_device(self.model, candidate)
                self.load_error = str(last) if last else None
                break
            except Exception as exc:
                last = exc
        else:
            raise RuntimeError(f"emotion2vec+ base（基础版情绪模型）加载失败：{last}")
        self.warmed = False

    def warmup(self):
        self(b"\0\0" * 32000)
        self.warmed = True

    def __call__(self, pcm):
        from ..audio.allocation import pcm_samples
        rows = self.model.generate(input=pcm_samples(pcm), granularity="utterance",
                                   extract_embedding=False, batch_size=1)
        if not rows:
            return {"label": ""}
        row = rows[0]
        labels, scores = row.get("labels", []), row.get("scores", [])
        if not labels or len(labels) != len(scores):
            raise ValueError("情绪分类标签与分数不匹配")
        index = max(range(len(scores)), key=scores.__getitem__)
        label = str(labels[index]).split("/")[0]
        if label in {"<unk>", "unknown", "未知"}:
            label = ""
        return {"label": label, "score": float(scores[index])}


class Yamnet:
    def __init__(self, model_dir: Path):
        from .snapshot import verify_snapshot
        self.identity = verify_snapshot(model_dir, "yamnet")
        import onnxruntime as ort
        self.model_file = str(model_dir / "yamnet.onnx")
        self.requested_device = self.device = "cpu"
        self.runtime = "ONNX Runtime（模型推理库）"
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.session = ort.InferenceSession(self.model_file, options, providers=["CPUExecutionProvider"])
        self.providers = self.session.get_providers()
        with (model_dir / "yamnet_class_map.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.labels = [row["display_name"] for row in csv.DictReader(stream)]
        if len(self.labels) != 521:
            raise ValueError("YAMNet（声学事件）类别表必须包含 521 类")
        self.warmed = False

    def warmup(self):
        self(b"\0\0" * 15600)
        self.warmed = True

    def __call__(self, pcm):
        from ..audio.allocation import pcm_samples
        audio = pcm_samples(pcm)
        # 输出包含补零尾帧；只投影收齐 975 毫秒真实音频的行。
        count = max(0, 1 + (len(audio) - 15600) // 7680)
        if not count:
            return {"frames": []}
        scores = self.session.run(["output_0"], {"waveform": audio})[0]
        return {"frames": [{"start_sample": index * 7680,
                            "end_sample": index * 7680 + 15600,
                            "label_ids": [i for i, score in enumerate(row)
                                          if float(score) >= 0.5 and self.labels[i] not in {"Speech", "Silence"}],
                            "labels": [self.labels[i] for i, score in enumerate(row)
                                       if float(score) >= 0.5 and self.labels[i] not in {"Speech", "Silence"}]}
                           for index, row in enumerate(scores[:count])]}
