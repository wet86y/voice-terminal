"""FSMN ONNX adapter（轻量流式检测适配器）：只读已导出资产，不触发下载或导出。"""
from pathlib import Path
import hashlib
import json


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OnnxStreamingModel:
    def __init__(self, model_dir: Path, *, threads: int = 4):
        exported = model_dir / "onnx"
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        from ..local_layout import project_root
        from ..resource_distribution import ResourceCatalog
        spec = ResourceCatalog(project_root()).get("fsmn-vad")
        expected = {record.path: record for record in spec.required_files}
        if manifest.get("source_sha256") != expected["model.pt"].sha256 or manifest.get("quantized") is not False:
            raise ValueError("轻量检测资产与当前未量化权重记录不匹配，请校验完整性")
        for name in ("model.onnx", "config.yaml", "am.mvn"):
            record = expected[f"onnx/{name}"]
            path = exported / name
            if manifest.get("files", {}).get(name) != record.sha256 or not path.is_file() or path.stat().st_size != record.size_bytes:
                raise ValueError(f"轻量检测必要资产不匹配：{name}，请校验完整性")
        import numpy as np
        from funasr_onnx import Fsmn_vad_online
        from funasr_onnx.utils.frontend import WavFrontendOnline
        from funasr_onnx.utils.e2e_vad import E2EVadModel
        self.np, self.frontend_type, self.scorer_type = np, WavFrontendOnline, E2EVadModel
        self.model = Fsmn_vad_online(str(exported), quantize=False, device_id="-1", intra_op_num_threads=threads)

    def generate(self, *, input, cache, is_final=False, chunk_size=200,
                 max_end_silence_time=None, **_):
        np, model = self.np, self.model
        if not input.size:
            if is_final:
                cache.clear()  # 空最终刷新沿用原事件接口；FsmnVadDetector（检测器）关闭活动端点。
            return [{"value": []}]
        state = cache.get("onnx_state")
        if state is None:
            state = {"frontend": self.frontend_type(cmvn_file=model.cmvn_file, **model.config["frontend_conf"]),
                     "scorer": self.scorer_type(model.config["model_conf"]),
                     "model_cache": model.prepare_cache([]), "pending": np.empty(0, dtype=np.float32)}
            cache["onnx_state"] = state
        audio = np.concatenate((state["pending"], input))
        stride = chunk_size * model.config["frontend_conf"]["fs"] // 1000
        count = len(audio) // stride + int(is_final)
        segments = []
        for index in range(count):
            final = is_final and index == count - 1
            chunk = audio[index * stride:(index + 1) * stride]
            frontend = state["frontend"]
            # 上游封装在零特征时引用未初始化的 scorer（端点判定器）；显式管理实例。
            if final and len(chunk) < frontend.frame_sample_length and frontend.input_cache is None:
                continue
            feats, _ = model.extract_feat(frontend, np.expand_dims(chunk, 0), final)
            if feats.size:
                scores, caches = model.infer([feats, *state["model_cache"]])
                state["model_cache"] = caches
                found = state["scorer"](scores, frontend.get_waveforms(), is_final=final,
                    max_end_sil=max_end_silence_time if max_end_silence_time is not None else model.max_end_sil,
                    online=True)
                if found:
                    for values in found:
                        segments.extend(values)
        state["pending"] = audio[(len(audio) // stride) * stride:] if not is_final else np.empty(0, dtype=np.float32)
        if is_final:
            cache.clear()
        return [{"value": segments}]
