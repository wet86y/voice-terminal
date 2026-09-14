from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import struct

from .keywords import validate_keywords_file


class KwsError(RuntimeError):
    pass


@dataclass(frozen=True)
class KwsModelPaths:
    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path
    keywords: Path


def _find_one(directory: Path, pattern: str, description: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if not matches:
        raise KwsError(f"KWS 模型缺少 {description}：{directory}")
    return matches[0]


def discover_model_paths(model_dir: Path, keywords_file: Path, keyword_label: str) -> KwsModelPaths:
    paths = KwsModelPaths(
        tokens=_find_one(model_dir, "tokens.txt", "tokens.txt"),
        encoder=_find_one(model_dir, "encoder*.onnx", "encoder onnx"),
        decoder=_find_one(model_dir, "decoder*.onnx", "decoder onnx"),
        joiner=_find_one(model_dir, "joiner*.onnx", "joiner onnx"),
        keywords=keywords_file,
    )
    validate_keywords_file(paths.keywords, keyword_label)
    return paths


class KwsDetector:
    def __init__(
        self,
        *,
        model_dir: Path,
        keywords_file: Path,
        keyword_label: str,
        threshold: float = 0.25,
        keywords_score: float = 1.0,
        num_trailing_blanks: int = 1,
        provider: str = "cpu",
        sample_rate: int = 16000,
    ) -> None:
        self.sample_rate = sample_rate
        self.keyword_label = keyword_label.upper()
        self.paths = discover_model_paths(model_dir, keywords_file, self.keyword_label)
        try:
            import numpy as np  # type: ignore
            import sherpa_onnx  # type: ignore
        except ImportError as exc:
            raise KwsError("缺少 numpy/sherpa-onnx，请先运行 scripts\\setup.ps1。") from exc
        self._np = np

        try:
            self._spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(self.paths.tokens),
                encoder=str(self.paths.encoder),
                decoder=str(self.paths.decoder),
                joiner=str(self.paths.joiner),
                num_threads=1,
                max_active_paths=4,
                keywords_file=str(self.paths.keywords),
                keywords_score=keywords_score,
                keywords_threshold=threshold,
                num_trailing_blanks=num_trailing_blanks,
                provider=provider,
            )
            self._stream = self._spotter.create_stream()
        except Exception as exc:
            raise KwsError(f"KWS 模型初始化失败：{exc}") from exc

    def _pcm_to_float32(self, pcm: bytes):
        if len(pcm) % 2:
            pcm = pcm[:-1]
        values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        return self._np.asarray(values, dtype=self._np.float32) / 32768.0

    def accept_pcm(self, pcm: bytes) -> str | None:
        self._stream.accept_waveform(self.sample_rate, self._pcm_to_float32(pcm))
        result: str | None = None
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            detected = self._spotter.get_result(self._stream)
            if detected:
                result = str(detected)
                self._spotter.reset_stream(self._stream)
                break
        if result and self.keyword_label.casefold() == result.casefold():
            return self.keyword_label
        # A keyword file may contain diagnostic or future alternate labels.
        # This detector represents one configured wake word, so never turn a
        # different result into a truthy wake signal for its caller.
        return None

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)
