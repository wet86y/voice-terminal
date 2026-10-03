from pathlib import Path
import re
import math

TAG = re.compile(r"<\|([^|]+)\|>")
EMOTIONS = {"HAPPY", "SAD", "ANGRY", "NEUTRAL", "FEARFUL", "DISGUSTED", "SURPRISED"}
LANGUAGES = {"zh", "en", "yue", "ja", "ko", "nospeech"}
EVENTS = {"Speech", "BGM", "Applause", "Laughter", "Cry", "Sneeze", "Breath", "Cough"}


def parse_transcript(raw: str) -> dict:
    tags = TAG.findall(raw)
    return {"text": TAG.sub("", raw).strip(), "language": next((t for t in tags if t in LANGUAGES), "unknown"),
            "emotion": next((t for t in tags if t in EMOTIONS), "unknown"),
            "events": [t for t in tags if t in EVENTS], "raw_tags": tags}


def bounded_segments(segments: list, duration_ms: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for start, end in segments:
        start, end = max(0, int(start)), min(duration_ms, int(end))
        if end <= start or (result and start < result[-1][1]):
            raise ValueError("invalid VAD segment ordering")
        if result and end - result[-1][0] <= 15000:
            start = result.pop()[0]
        # FSMN may overshoot max_single_segment_time by a frame. Splitting
        # 30010 ms into 30000+10 creates an unusable final clip; balance instead.
        count = max(1, math.ceil((end - start) / 30000))
        width = math.ceil((end - start) / count)
        while start < end:
            boundary = min(end, start + width)
            result.append((start, boundary))
            start = boundary
    return result


class SenseVoice:
    def __init__(self, model_dir: Path, vad_dir: Path, *,
                 device: str = "cpu", fallback_device: str = "cpu") -> None:
        from funasr import AutoModel
        from funasr.tokenizer.sentencepiece_tokenizer import SentencepiecesTokenizer
        from funasr.register import tables
        import sentencepiece
        # SentencePiece's native Windows filename loader does not handle some
        # Unicode paths. Python reads the pinned file, then passes bytes.
        class UnicodeSentencepiece(SentencepiecesTokenizer):
            def _build_sentence_piece_processor(self):
                if self.sp is None:
                    self.sp = sentencepiece.SentencePieceProcessor(model_proto=Path(self.bpemodel).read_bytes())
        tables.tokenizer_classes["VoiceTerminalSentencepiece"] = UnicodeSentencepiece
        self.requested_device = device
        self.device = "cpu"
        self.load_error: str | None = None
        self.internal_vad_device = "cpu"
        from ..compute import device_candidates
        last_error: Exception | None = None
        for candidate in device_candidates(device, "sensevoice", fallback_device):
            model = vad = None
            try:
                model = AutoModel(
                    model=str(model_dir), tokenizer="VoiceTerminalSentencepiece", device=candidate,
                    disable_update=True, disable_pbar=True, ncpu=2,
                )
                vad = AutoModel(
                    model=str(vad_dir), device=candidate, disable_update=True, disable_pbar=True,
                    max_single_segment_time=30000,
                )
                self.model, self.vad = model, vad
                self.device = self.internal_vad_device = candidate
                self.load_error = (
                    f"CUDA（图形处理器）加载失败，已回退到 {candidate}：{type(last_error).__name__}: {last_error}"
                    if last_error is not None else None
                )
                break
            except Exception as exc:
                last_error = exc
                model = vad = None
                if candidate == "cuda":
                    try:
                        import torch  # type: ignore
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
        else:
            raise RuntimeError(f"SenseVoiceSmall 或内部 FSMN-VAD 加载失败：{last_error}") from last_error

    def __call__(self, pcm: bytes) -> dict:
        import numpy as np
        from ..audio.allocation import pcm_samples
        audio = pcm_samples(pcm)
        duration = len(audio) // 16
        detected = self.vad.generate(input=audio, cache={}, is_final=True)
        if not detected or not isinstance(detected[0].get("value"), list):
            raise ValueError("missing VAD result")
        ranges = bounded_segments(detected[0]["value"], duration)
        if not ranges:
            return {"text": "", "segments": [], "no_speech": True, "complete": True}
        parts = []
        for start, end in ranges:
            result = self.model.generate(input=audio[start * 16:end * 16], cache={}, language="auto", use_itn=True)
            if not result or not isinstance(result[0].get("text"), str):
                raise ValueError("incomplete SenseVoice result")
            part = parse_transcript(result[0]["text"])
            if not part["text"] and (part["language"] != "nospeech" and (not part["events"] or "Speech" in part["events"])):
                raise ValueError("empty speech segment")
            parts.append({"start_ms": start, "end_ms": end, **part})
        text = "".join(p["text"] for p in parts)
        return {"text": text, "segments": parts, "no_speech": not bool(text), "complete": True}
