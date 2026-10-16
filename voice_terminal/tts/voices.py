"""Versioned local TTS voice library (本地语音音色库)."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
import io
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Mapping
from uuid import uuid4


MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_SECONDS = 15.0
DESIGN_INFERENCE_STEPS = 30  # DESIGN_INFERENCE_STEPS（样音制作步数）：独立于正式播报设置。
DESIGN_GENERATION_RECIPE = "voxcpm2-full-decode-v2"  # DESIGN_GENERATION_RECIPE（样音生成配方版本）：混合创作不叠加续写提示。
# MIN_REFERENCE_SECONDS（参考音频下限）：官方「5 秒规则」要求至少 5 秒，
# 并给出 5–30 秒的实用区间；低于 5 秒的音色质量官方不背书。
# 上限 15 秒不是模型限制：audio.cpp 的 AudioVAE 编码器预留容量
# `encoder_sample_capacity = 240000`（16 kHz × 15 秒）是硬上限，超出即抛错；
# 可用会话选项 `voxcpm2.audiovae_encoder_sample_capacity` 提高（实测 960000
# 可处理 30 秒参考音频），但会增大编码器图与显存占用，故默认保持 15 秒。
MIN_REFERENCE_SECONDS = 5.0
ALLOWED_IMPORT_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg"}
VOICE_MODELS = {"voxcpm1", "voxcpm2"}
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_LOCAL_PATH_RE = re.compile(r"(?:(?:[A-Za-z]:[\\/]|\\\\)[^\s,;\"'<>|]+|(?<![\w.])/(?:[^\s,;\"'<>|]+))")


def safe_voice_diagnostic(value: Any, fallback: str = "本地音色操作失败，请检查音频和模型状态。") -> str:
    """移除错误信息中的本机绝对路径后再送回界面。"""
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    text = _LOCAL_PATH_RE.sub("本机资源路径", text)
    return text[:500] or fallback


class VoiceLibraryError(ValueError):
    """音色条目、版本或素材无效。"""


class VoiceRevisionConflict(VoiceLibraryError):
    """音色编辑基于过期版本。"""


class VoiceDeleteConflict(VoiceLibraryError):
    """音色仍被一个或多个本地模型启用。"""


@dataclass(frozen=True)
class ResolvedLocalTtsVoice:
    """ResolvedLocalTtsVoice（已解析的逐请求音色快照）。"""

    model: str
    native_model_id: str
    voice_id: str | None
    revision: int | None
    reference_path: Path | None
    reference_text: str
    style_description: str
    emotion: str | None = None
    reference_mode: str = "voice"  # reference_mode（参考模式）：voice 或 design。
    is_preview: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _collapse_description(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise VoiceLibraryError(f"{field}（描述）必须为文字。")
    result = " ".join(value.strip().split())
    if len(result) > limit:
        raise VoiceLibraryError(f"{field}（描述）最多为 {limit} 个字符。")
    if any(char in result for char in "()（）"):
        raise VoiceLibraryError(f"{field}（描述）不能包含圆括号（半角 () 或全角（））。")
    return result


def _safe_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise VoiceLibraryError(f"{field}（文字）必须为字符串。")
    result = value.strip()
    if len(result) > limit:
        raise VoiceLibraryError(f"{field}（文字）最多为 {limit} 个字符。")
    if required and not result:
        raise VoiceLibraryError(f"{field}（文字）不能为空。")
    return result


class VoiceLibrary:
    """本地音频素材和不可变音色版本的原子存储。"""

    def __init__(self, root: Path, *, model: str | None = None) -> None:
        self.model = model  # model（库所属模型）：生产库必须绑定模型。
        self._leases: dict[str, int] = {}  # leases（在途素材引用计数）。
        self.root = Path(root).resolve()
        self.assets_dir = self.root / "assets"
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index = self._read_index()
        self.collect_garbage()

    @staticmethod
    def _empty_index() -> dict[str, Any]:
        return {"schema_version": 1, "assets": {}, "voices": {}, "migrations": {}}

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_index()
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VoiceLibraryError("音色库索引损坏；原文件已保留，请修复后再操作。") from exc
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or not isinstance(value.get("assets"), dict)
                or not isinstance(value.get("voices"), dict)
                or not isinstance(value.get("migrations"), dict)):
            raise VoiceLibraryError("音色库索引格式无效；原文件已保留，请修复后再操作。")
        return value

    def _save_index(self, value: dict[str, Any]) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".index-", suffix=".json.tmp", dir=str(self.root),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index_path)
            self._index = value
        finally:
            temporary.unlink(missing_ok=True)

    def _asset_path(self, asset_id: str) -> Path:
        if not isinstance(asset_id, str) or not _ID_RE.fullmatch(asset_id):
            raise VoiceLibraryError("asset_id（音频素材标识）无效。")
        path = (self.assets_dir / f"{asset_id}.wav").resolve()
        if not path.is_relative_to(self.assets_dir):
            raise VoiceLibraryError("音频素材标识无效。")
        return path

    @staticmethod
    def _normalize_audio(data: bytes, filename: str) -> tuple[bytes, int]:
        extension = Path(filename or "").suffix.lower()
        if extension not in ALLOWED_IMPORT_EXTENSIONS:
            raise VoiceLibraryError("仅支持 WAV、MP3、FLAC 或 OGG 音频。")
        if not data:
            raise VoiceLibraryError("音频文件为空。")
        if len(data) > MAX_IMPORT_BYTES:
            raise OverflowError("音频文件超过 20 MiB。")
        try:
            import numpy as np
            import soundfile as sf
            from scipy.signal import resample_poly

            samples, source_rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        except Exception as exc:
            raise VoiceLibraryError(f"音频无法严格解码：{type(exc).__name__}。") from exc
        if source_rate <= 0 or samples.size == 0 or not np.isfinite(samples).all():
            raise VoiceLibraryError("音频采样数据无效。")
        duration = samples.shape[0] / float(source_rate)
        if not MIN_REFERENCE_SECONDS <= duration <= MAX_REFERENCE_SECONDS:
            raise VoiceLibraryError("导入音频长度必须为 5–15 秒（官方建议至少 5 秒）。")
        mono = samples.mean(axis=1, dtype=np.float32)
        if source_rate != 16000:
            ratio = Fraction(16000, int(source_rate))
            mono = resample_poly(mono, ratio.numerator, ratio.denominator).astype(np.float32, copy=False)
        mono = np.clip(mono, -1.0, 1.0)
        rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))
        clipped = float(np.mean(np.abs(mono) >= 0.999))
        if rms < 0.003:
            raise VoiceLibraryError("音频接近静音，请选择更清晰的录音。")
        if clipped > 0.05:
            raise VoiceLibraryError("音频存在明显削波，请降低录音音量后重试。")
        output = io.BytesIO()
        sf.write(output, mono, 16000, format="WAV", subtype="PCM_16")
        return output.getvalue(), round(len(mono) * 1000 / 16000)

    @staticmethod
    def _validate_pcm(pcm: bytes, sample_rate: int = 16000) -> tuple[bytes, int]:
        if not isinstance(pcm, bytes) or len(pcm) < 2 or len(pcm) % 2:
            raise VoiceLibraryError("PCM（脉冲编码音频）素材无效。")
        duration = len(pcm) / 2 / sample_rate
        # 生成候选不受官方"参考音频至少 5 秒"约束：试音正文本身就短，
        # 样音只要求非空且不超过上限。它随后作为正式克隆的参考素材使用，
        # 而编码器对短参考按零填充处理，长度不足不影响可用性。
        if not 0.0 < duration <= MAX_REFERENCE_SECONDS:
            raise VoiceLibraryError("候选音频长度必须大于 0 且不超过 15 秒。")
        try:
            import numpy as np

            values = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(np.square(values), dtype=np.float64)))
            clipped = float(np.mean(np.abs(values) >= 0.999))
        except Exception as exc:
            raise VoiceLibraryError("PCM 音频格式无效。") from exc
        if rms < 0.003:
            raise VoiceLibraryError("音频接近静音，请重新录制或生成。")
        if clipped > 0.05:
            raise VoiceLibraryError("音频存在明显削波，请降低录音音量后重试。")
        return pcm, round(duration * 1000)

    def put_import(self, data: bytes, filename: str) -> dict[str, Any]:
        """严格解码用户上传并保存标准化素材。"""
        normalized, duration_ms = self._normalize_audio(data, filename)
        return self._put_wav(normalized, duration_ms)

    def put_pcm(self, pcm: bytes, *, sample_rate: int = 16000) -> dict[str, Any]:
        """保存已经由音频适配器重采样的单声道 16-bit PCM。"""
        if sample_rate != 16000:
            raise VoiceLibraryError("本地音色素材必须为 16 kHz。")
        pcm, duration_ms = self._validate_pcm(pcm, sample_rate)
        try:
            import numpy as np
            import soundfile as sf

            samples = np.frombuffer(pcm, dtype="<i2")
            output = io.BytesIO()
            sf.write(output, samples, sample_rate, format="WAV", subtype="PCM_16")
        except Exception as exc:
            raise VoiceLibraryError("无法写入标准 WAV 音频素材。") from exc
        return self._put_wav(output.getvalue(), duration_ms)

    def _put_wav(self, data: bytes, duration_ms: int) -> dict[str, Any]:
        asset_id = uuid4().hex
        destination = self._asset_path(asset_id)
        temporary = self.assets_dir / f".{asset_id}.wav.tmp"
        with self._lock:
            try:
                with temporary.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                updated = json.loads(json.dumps(self._index))
                updated["assets"][asset_id] = {
                    "created_at": _utc_now(), "duration_ms": duration_ms,
                    "sample_rate": 16000, "channels": 1, "format": "wav",
                }
                self._save_index(updated)
            except Exception:
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        return {"asset_id": asset_id, **updated["assets"][asset_id]}

    def high_fidelity_path(self, asset_id: str) -> Path | None:
        """high_fidelity_path（高保真素材路径）：原生采样率的旁路副本，可能不存在。

        编码器固定按 16 kHz 读取，所以主素材必须是 16 kHz；但 48 kHz 原生样音
        保留了 8 kHz 以上的带宽（VoxCPM2 实测 8–16 kHz 占 1.1%–14.4%），供后续
        音色处理使用。缺失时返回 None，调用方回退到主素材。
        """
        with self._lock:
            metadata = self._index["assets"].get(asset_id)
            if not isinstance(metadata, dict) or not metadata.get("high_fidelity"):
                return None
        path = self._high_fidelity_path(asset_id)
        return path if path.is_file() else None

    def _high_fidelity_path(self, asset_id: str) -> Path:
        return self._asset_path(asset_id).with_suffix(".hifi.wav")

    def put_high_fidelity_pcm(self, asset_id: str, pcm: bytes, *, sample_rate: int,
                              channels: int = 1) -> dict[str, Any]:
        """保存原生采样率旁路副本；只登记元数据，主素材与索引引用不变。"""
        if not isinstance(pcm, bytes) or len(pcm) < 2 or len(pcm) % 2:
            raise VoiceLibraryError("PCM（脉冲编码音频）素材无效。")
        if int(sample_rate) <= 16000 or int(channels) != 1:
            # 16 kHz 及以下与主素材等价，单声道以外的原生样音不做旁路。
            return {}
        try:
            import numpy as np
            import soundfile as sf
            samples = np.frombuffer(pcm, dtype="<i2")
            output = io.BytesIO()
            sf.write(output, samples, int(sample_rate), format="WAV", subtype="PCM_16")
            payload = output.getvalue()
        except Exception as exc:
            raise VoiceLibraryError("无法写入高保真 WAV 音频素材。") from exc
        destination = self._high_fidelity_path(asset_id)
        temporary = self.assets_dir / f".{asset_id}.hifi.wav.tmp"
        with self._lock:
            if asset_id not in self._index["assets"]:
                raise FileNotFoundError("音频素材不存在。")
            try:
                with temporary.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                updated = json.loads(json.dumps(self._index))
                updated["assets"][asset_id]["high_fidelity"] = {
                    "sample_rate": int(sample_rate), "channels": int(channels),
                    "duration_ms": round(len(pcm) / 2 / int(sample_rate) * 1000),
                    "format": "wav",
                }
                self._save_index(updated)
            except Exception:
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        return dict(updated["assets"][asset_id]["high_fidelity"])

    def asset_path(self, asset_id: str) -> Path:
        with self._lock:
            if asset_id not in self._index["assets"]:
                raise FileNotFoundError("音频素材不存在。")
            path = self._asset_path(asset_id)
            if not path.is_file():
                raise FileNotFoundError("音频素材文件缺失。")
            return path

    def asset_bytes(self, asset_id: str) -> bytes:
        return self.asset_path(asset_id).read_bytes()

    def _normalize_voice(self, payload: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
        if self.model and payload.get("model", self.model) != self.model:
            raise VoiceLibraryError("model（音色模型归属）与当前音色库不一致。")
        source = source or payload.get("source")
        if self.model == "voxcpm1" and (source == "design" or payload.get("design_description")):
            raise VoiceLibraryError("VoxCPM1（第一代模型）仅支持参考音频克隆。")
        if source not in {"import", "recording", "design", "legacy"}:
            raise VoiceLibraryError("source（音色来源）无效。")
        reference_asset_id = payload.get("reference_asset_id")
        if not isinstance(reference_asset_id, str) or not _ID_RE.fullmatch(reference_asset_id):
            raise VoiceLibraryError("reference_asset_id（参考音频标识）无效。")
        reference_text = _safe_text(payload.get("reference_text", ""), "reference_text", 2000)
        if self.model == "voxcpm1" and not reference_text:
            raise VoiceLibraryError("VoxCPM1（第一代模型）必须填写准确的参考转写。")
        style = _collapse_description(payload.get("style_description", ""), "style_description", 300)
        normalized: dict[str, Any] = {
            "name": _safe_text(payload.get("name"), "name", 80, required=True),
            "source": source,
            "reference_asset_id": reference_asset_id,
            "reference_text": reference_text,
            "style_description": style,
        }
        source_asset_id = payload.get("source_reference_asset_id")  # 原始克隆素材；reference_asset_id 为确认后的目标样音。
        if source_asset_id is not None:
            if source != "design" or not isinstance(source_asset_id, str) or not _ID_RE.fullmatch(source_asset_id):
                raise VoiceLibraryError("source_reference_asset_id（克隆源音频标识）仅用于可控音色设计。")
            normalized["source_reference_asset_id"] = source_asset_id
        normalized["source_reference_text"] = _safe_text(payload.get("source_reference_text", ""), "source_reference_text", 2000)
        if source == "design":
            description = _collapse_description(payload.get("design_description", ""), "design_description", 500)
            model = payload.get("design_model", "voxcpm2")
            if model != "voxcpm2":
                raise VoiceLibraryError("design_model（音色设计模型）必须为 voxcpm2。")
            design_text = _safe_text(payload.get("design_text", reference_text), "design_text", 2000, required=True)
            if self.model and not reference_text:
                raise VoiceLibraryError("请填写目标样音实际发音的准确转写。")
            steps = payload.get("inference_steps", 20)
            if type(steps) is not int or not 1 <= steps <= 30:
                raise VoiceLibraryError("inference_steps（推理步数）必须为 1–30 的整数。")
            if not description:
                raise VoiceLibraryError("design_description（音色设计描述）不能为空。")
            normalized.update({
                "design_description": description, "design_model": "voxcpm2", "design_text": design_text,
                "inference_steps": payload.get("inference_steps", 20),
            })
            if "generation_recipe" in payload:
                normalized["generation_recipe"] = _safe_text(payload["generation_recipe"], "generation_recipe", 100, required=True)
            if self.model:
                provenance = self._index["assets"].get(reference_asset_id, {}).get("design_provenance")
                expected = self.design_signature(normalized)
                if provenance != expected:
                    raise VoiceLibraryError("设计样音与当前参数不一致，请重新试音后保存。")
        return normalized

    def create_voice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        voice = self._normalize_voice(payload)
        with self._lock:
            if voice["reference_asset_id"] not in self._index["assets"]:
                raise FileNotFoundError("参考音频素材不存在。")
            if voice.get("source_reference_asset_id") and voice["source_reference_asset_id"] not in self._index["assets"]:
                raise FileNotFoundError("克隆源音频素材不存在。")
            voice_id = uuid4().hex
            revision = {**voice, "revision": 1, "created_at": _utc_now()}
            updated = json.loads(json.dumps(self._index))
            updated["voices"][voice_id] = {"latest_revision": 1, "revisions": {"1": revision}}
            self._save_index(updated)
            return {"id": voice_id, **revision}

    def update_voice(self, voice_id: str, expected_revision: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(voice_id, str) or not _ID_RE.fullmatch(voice_id):
            raise FileNotFoundError("音色条目不存在。")
        voice = self._normalize_voice(payload)
        with self._lock:
            current = self._index["voices"].get(voice_id)
            if current is None:
                raise FileNotFoundError("音色条目不存在。")
            if type(expected_revision) is not int or expected_revision != current["latest_revision"]:
                raise VoiceRevisionConflict("音色版本已更新，请刷新条目后再保存。")
            if voice["reference_asset_id"] not in self._index["assets"]:
                raise FileNotFoundError("参考音频素材不存在。")
            if voice.get("source_reference_asset_id") and voice["source_reference_asset_id"] not in self._index["assets"]:
                raise FileNotFoundError("克隆源音频素材不存在。")
            revision_no = int(expected_revision) + 1
            revision = {**voice, "revision": revision_no, "created_at": _utc_now()}
            updated = json.loads(json.dumps(self._index))
            updated["voices"][voice_id]["latest_revision"] = revision_no
            updated["voices"][voice_id]["revisions"][str(revision_no)] = revision
            if (updated.get("active_voice") or {}).get("voice_id") == voice_id:
                updated["active_voice"] = {"voice_id": voice_id, "revision": revision_no}
            if (updated.get("default_voice") or {}).get("voice_id") == voice_id:
                updated["default_voice"] = {"voice_id": voice_id, "revision": revision_no}
            self._save_index(updated)
            return {"id": voice_id, **revision}

    def get_voice(self, voice_id: str, revision: int | None = None) -> dict[str, Any]:
        with self._lock:
            value = self._index["voices"].get(voice_id)
            if value is None:
                raise FileNotFoundError("音色条目不存在。")
            revision_no = value["latest_revision"] if revision is None else revision
            record = value["revisions"].get(str(revision_no))
            if not isinstance(record, dict):
                raise FileNotFoundError("音色版本不存在。")
            return {"id": voice_id, **json.loads(json.dumps(record))}

    def list_voices(self, active_by_model: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        active_by_model = active_by_model or {}
        with self._lock:
            result = []
            for voice_id, value in self._index["voices"].items():
                latest = int(value["latest_revision"])
                record = value["revisions"][str(latest)]
                active: dict[str, Any] = {}
                for model in VOICE_MODELS:
                    selected = active_by_model.get(model)
                    if isinstance(selected, Mapping) and selected.get("voice_id") == voice_id:
                        active[model] = {
                            "revision": selected.get("revision"),
                            "is_latest": selected.get("revision") == latest,
                        }
                asset = self._index["assets"].get(record["reference_asset_id"], {})
                result.append({
                    "id": voice_id, "latest_revision": latest, **json.loads(json.dumps(record)),
                    "model": self.model,
                    "is_default": (self._index.get("default_voice") or {}).get("voice_id") == voice_id,
                    "enabled": (self.active_selection or {}).get("voice_id") == voice_id if self.model else bool(active),
                    "duration_ms": asset.get("duration_ms"), "active_by_model": active,
                    "source_reference_duration_ms": self._index["assets"].get(record.get("source_reference_asset_id"), {}).get("duration_ms"),
                    "compatibility": {
                        "voxcpm1": {"supported": True, "requires_reference_text": True,
                                    "ready": bool(record.get("reference_text"))},
                        "voxcpm2": {"supported": True, "requires_reference_text": True,
                                    "ready": bool(record.get("reference_text", "").strip())},
                    },
                })
            return result

    def resolve(self, model: str, selection: Mapping[str, Any] | None, *, emotion: str | None = None,
                is_preview: bool = False) -> ResolvedLocalTtsVoice:
        if model not in VOICE_MODELS:
            raise VoiceLibraryError("local_model（本地语音模型）无效。")
        if self.model is not None and model != self.model:
            raise VoiceLibraryError("音色库与请求模型不匹配。")
        if not isinstance(selection, Mapping):
            raise VoiceLibraryError("尚未配置可用音色，请先导入或创建音色并启用。")
        voice = self.get_voice(str(selection.get("voice_id") or ""), selection.get("revision"))
        path = self.asset_path(voice["reference_asset_id"])
        if not voice.get("reference_text", "").strip():
            raise VoiceLibraryError("请先填写参考音频的准确转写，再启用或使用克隆音色。")
        native_id = "voxcpm1-stream" if model == "voxcpm1" else "voxcpm2-stream"
        return ResolvedLocalTtsVoice(
            model=model, native_model_id=native_id, voice_id=voice["id"], revision=voice["revision"],
            reference_path=path, reference_text=voice.get("reference_text", ""),
            style_description="", emotion=None,  # 已确认样音承载固定音色与语气，不再逐句注入描述。
            reference_mode="voice", is_preview=is_preview,
        )

    def resolve_design(self, description: str, *, emotion: str | None = None,
                       is_preview: bool = False) -> ResolvedLocalTtsVoice:
        description = _collapse_description(description, "design_description", 500)
        if not description:
            raise VoiceLibraryError("design_description（音色设计描述）不能为空。")
        return ResolvedLocalTtsVoice(
            model="voxcpm2", native_model_id="voxcpm2-stream", voice_id=None, revision=None,
            reference_path=None, reference_text="", style_description=description,
            emotion=emotion, reference_mode="design", is_preview=is_preview,
        )

    def resolve_draft(self, model: str, payload: Mapping[str, Any], *, emotion: str | None = None,
                      is_preview: bool = True) -> ResolvedLocalTtsVoice:
        """解析未保存的克隆草稿，不把其内容写入正式音色或启用选择。"""
        if self.model is not None and model != self.model:
            raise VoiceLibraryError("音色草稿属于另一模型。")
        if payload.get("reference_mode") == "design" or (
            payload.get("source") == "design" and not payload.get("reference_asset_id")
        ):
            if model != "voxcpm2":
                raise VoiceLibraryError("VoxCPM1（第一代模型）不支持音色设计。")
            return self.resolve_design(
                str(payload.get("design_description") or payload.get("style_description") or ""),
                emotion=emotion, is_preview=is_preview,
            )
        voice = self._normalize_voice(payload)
        with self._lock:
            if voice["reference_asset_id"] not in self._index["assets"]:
                raise FileNotFoundError("参考音频素材不存在。")
            path = self.asset_path(voice["reference_asset_id"])
        if model == "voxcpm1" and not voice["reference_text"]:
            raise VoiceLibraryError("VoxCPM1（第一代本地语音模型）需要填写参考音频转写文本。")
        return ResolvedLocalTtsVoice(
            model=model, native_model_id="voxcpm1-stream" if model == "voxcpm1" else "voxcpm2-stream",
            voice_id=None, revision=None, reference_path=path,
            reference_text=voice["reference_text"], style_description="",
            emotion=None, reference_mode="voice", is_preview=is_preview,
        )

    def delete_voice(self, voice_id: str, active_by_model: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            if (self._index.get("default_voice") or {}).get("voice_id") == voice_id:
                raise VoiceDeleteConflict("默认音色用于稳定回退，不能删除。")
            if self.model and (self._index.get("active_voice") or {}).get("voice_id") == voice_id:
                self.activate(None)
            active_by_model = active_by_model or {}
            references = [model for model, item in active_by_model.items()
                          if isinstance(item, Mapping) and item.get("voice_id") == voice_id]
            if references:
                raise VoiceDeleteConflict("该音色仍被以下模型启用，先切换这些模型的音色后才能删除：" + "、".join(references))
            if voice_id not in self._index["voices"]:
                raise FileNotFoundError("音色条目不存在。")
            updated = json.loads(json.dumps(self._index))
            del updated["voices"][voice_id]
            self._save_index(updated)
            self.collect_garbage()

    def migration_record(self) -> dict[str, Any] | None:
        with self._lock:
            value = self._index["migrations"].get("default_voice")
            return dict(value) if isinstance(value, dict) else None

    def set_migration_record(self, voice_id: str, revision: int) -> None:
        with self._lock:
            updated = json.loads(json.dumps(self._index))
            updated["migrations"]["default_voice"] = {
                "voice_id": voice_id, "revision": revision, "completed_at": _utc_now(),
            }
            self._save_index(updated)

    def collect_garbage(self, *, max_age_seconds: float = 24 * 60 * 60) -> None:
        """仅清理超过 24 小时且不被任一版本引用的素材。"""
        now = datetime.now(timezone.utc).timestamp()
        referenced = {
            revision.get(field)
            for voice in self._index.get("voices", {}).values()
            for revision in voice.get("revisions", {}).values()
            for field in ("reference_asset_id", "source_reference_asset_id")
        }
        with self._lock:
            updated = json.loads(json.dumps(self._index))
            changed = False
            for asset_id, metadata in list(updated["assets"].items()):
                created = metadata.get("created_at")
                try:
                    age = now - datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    age = 0
                if asset_id not in referenced and not self._leases.get(asset_id) and age > max_age_seconds:
                    self._asset_path(asset_id).unlink(missing_ok=True)
                    self._high_fidelity_path(asset_id).unlink(missing_ok=True)
                    del updated["assets"][asset_id]
                    changed = True
            if changed:
                self._save_index(updated)
            # 兼容旧版本只移除主素材和索引时留下的无主高保真副本。
            for path in self.assets_dir.glob("*.hifi.wav"):
                asset_id = path.name.removesuffix(".hifi.wav")
                if (_ID_RE.fullmatch(asset_id) and asset_id not in updated["assets"]
                        and not self._leases.get(asset_id)
                        and now - path.stat().st_mtime > max_age_seconds):
                    path.unlink(missing_ok=True)
            for temporary in self.root.glob(".index-*.json.tmp"):
                try:
                    if now - temporary.stat().st_mtime > max_age_seconds:
                        temporary.unlink(missing_ok=True)
                except OSError:
                    continue

    @staticmethod
    def design_signature(payload: Mapping[str, Any]) -> dict[str, Any]:
        """design_signature（候选来源）：按保存规则规范化源转写，再绑定样音输入。"""
        signature = {key: payload.get(key) for key in (
            "design_description", "design_text", "source_reference_asset_id", "source_reference_text", "inference_steps")}
        # source_reference_text（克隆源转写）：试音打标与保存校验统一去除首尾空白。
        signature["source_reference_text"] = _safe_text(payload.get("source_reference_text", ""), "source_reference_text", 2000)
        if "generation_recipe" in payload:
            signature["generation_recipe"] = payload["generation_recipe"]  # generation_recipe（实际生成配方）。
        return signature

    def mark_design(self, asset_id: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.asset_path(asset_id)
            updated = json.loads(json.dumps(self._index))
            updated["assets"][asset_id]["design_provenance"] = self.design_signature(payload)
            self._save_index(updated)

    @property
    def active_selection(self) -> dict[str, Any] | None:
        with self._lock:
            value = self._index.get("active_voice") or self._index.get("default_voice")
            return dict(value) if value else None

    def activate(self, selection: Mapping[str, Any] | None) -> dict[str, Any]:
        with self._lock:
            chosen = dict(selection or self._index.get("default_voice") or {})
            self.resolve(self.model, chosen)
            updated = json.loads(json.dumps(self._index))
            updated["active_voice"] = chosen
            self._save_index(updated)
            return chosen

    def register_default(self, data: bytes, transcript: str) -> dict[str, Any]:
        """register_default（登记默认项）：完整素材就绪后才写入启用选择。"""
        with self._lock:
            if self._index.get("default_voice"):
                voice = self.get_voice(self._index["default_voice"]["voice_id"])
                try:
                    self.asset_path(voice["reference_asset_id"])
                except FileNotFoundError:
                    # 只能原位恢复同一默认素材；用户更换过默认样音时保留原引用并明确报错。
                    normalized, duration_ms = self._normalize_audio(data, "default.wav")
                    seed = self._index["migrations"].get("default_seed", {})
                    matches_seed = (seed.get("asset_id") == voice["reference_asset_id"]
                                    and seed.get("sha256") == hashlib.sha256(normalized).hexdigest()) if seed else (
                                        voice.get("source") == "legacy" and voice.get("revision") == 1)
                    if voice.get("reference_text") != transcript or not matches_seed:
                        raise VoiceLibraryError("默认音色参考音频缺失，请重新导入原参考音频。")
                    asset_id = voice["reference_asset_id"]
                    destination = self._asset_path(asset_id)
                    temporary = destination.with_name(f".{asset_id}-{uuid4().hex}.tmp")
                    try:
                        with temporary.open("wb") as handle:
                            handle.write(normalized)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, destination)
                        updated = json.loads(json.dumps(self._index))
                        updated["assets"].setdefault(asset_id, {
                            "created_at": _utc_now(), "duration_ms": duration_ms,
                            "sample_rate": 16000, "channels": 1, "format": "wav",
                        })
                        self._save_index(updated)
                    finally:
                        temporary.unlink(missing_ok=True)
                return voice
            asset = self.put_import(data, "default.wav")
            voice = self.create_voice({"name": "默认音色", "source": "legacy", "reference_asset_id": asset["asset_id"], "reference_text": transcript})
            updated = json.loads(json.dumps(self._index))
            selection = {"voice_id": voice["id"], "revision": voice["revision"]}
            updated["default_voice"] = selection
            updated.setdefault("active_voice", selection)
            # default_seed（默认种子来源）：asset_id（原素材编号）及 sha256（规范化音频摘要）。
            updated["migrations"]["default_seed"] = {"asset_id": asset["asset_id"],
                "sha256": hashlib.sha256(self.asset_bytes(asset["asset_id"])).hexdigest()}
            self._save_index(updated)
            return voice

    def pin(self, snapshot: ResolvedLocalTtsVoice):
        """pin（在途引用）：删除条目后也不回收正在使用的参考素材。"""
        asset_id = snapshot.reference_path.stem if snapshot.reference_path else None
        if asset_id:
            with self._lock:
                self.asset_path(asset_id)  # 素材已回收时拒绝建立失效租约。
                self._leases[asset_id] = self._leases.get(asset_id, 0) + 1
        def release():
            if asset_id:
                with self._lock:
                    self._leases[asset_id] = max(0, self._leases.get(asset_id, 0) - 1)
        return release

    @contextmanager
    def snapshot_guard(self):
        """snapshot_guard（快照保护）：解析参考素材与建立会话租约必须原子完成。"""
        with self._lock:
            yield


class ModelVoiceLibraries:
    """ModelVoiceLibraries（双库路由）：索引与音频均按模型物理隔离。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.libraries = {model: VoiceLibrary(self.root / model / "voices", model=model) for model in sorted(VOICE_MODELS)}

    def for_model(self, model: str) -> VoiceLibrary:
        if model not in self.libraries:
            raise VoiceLibraryError("model（音色所属模型）无效。")
        return self.libraries[model]

    def resolve(self, model, selection, **kwargs):
        return self.for_model(model).resolve(model, selection, **kwargs)

    def resolve_draft(self, model, payload, **kwargs):
        return self.for_model(model).resolve_draft(model, payload, **kwargs)

    def selections(self):
        return {model: library.active_selection for model, library in self.libraries.items()}

    def pin(self, snapshot):
        return self.for_model(snapshot.model).pin(snapshot)
