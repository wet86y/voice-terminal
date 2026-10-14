"""Product-facing settings and safe projections.

The voice runtime deliberately owns audio, scheduling, provider and playback
lifecycles.  This module is the small sidecar boundary used by the product Web
UI: it stores non-secret presentation settings and projects only user-readable
Shadow state.  Nothing in here is allowed to expose provider credentials or
the internal Agent/Shadow trace.
"""

from __future__ import annotations

from ..screen_capture import ScreenCaptureConfig

import asyncio
import json
import os
import tempfile
from collections import deque
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ..speaker.settings import TUNING_DEFAULTS, validate_tuning
from ..config import AppConfig, COMPUTE_DEVICE_MODES, COMPUTE_MODEL_IDS, ComputeConfig, validate_tts_inference_steps
from ..custom_instructions import validate_entries
from ..asr.hotwords import DEFAULT_HOTWORDS, validate_hotwords
from ..quiet_hours import QuietHoursConfig
from ..core.events import utc_now_iso
from ..tts.voices import safe_voice_diagnostic
from ..kws.keywords import MAX_WAKE_KEYWORDS
if TYPE_CHECKING:
    # ShadowEvent（代理事件）只用于类型标注；配置读取不加载整个代理运行环境。
    from ..shadow_agent.contracts import ShadowEvent


PRODUCT_SETTINGS_SCHEMA_VERSION = 14
MIN_SILENCE_SECONDS = 5
MAX_SILENCE_SECONDS = 1800  # MAX_SILENCE_SECONDS（最长静默秒数）：30 分钟。
MAX_PERSONA_DOCUMENT_BYTES = 64 * 1024
MIN_KWS_SENSITIVITY = 1
MAX_KWS_SENSITIVITY = 99
MIN_SPEAKER_THRESHOLD = 0.0
MAX_SPEAKER_THRESHOLD = 1.0

PERSONA_FILES = {
    "personality": "personality.md",
    "user": "user.md",
}


class ProductSettingsError(ValueError):
    """Invalid or unsafe product setting."""


def _threshold_to_sensitivity(threshold: float) -> int:
    """Expose sherpa's inverse threshold as an intuitive UI percentage."""

    return max(
        MIN_KWS_SENSITIVITY,
        min(MAX_KWS_SENSITIVITY, round((1.0 - float(threshold)) * 100)),
    )


def _sensitivity_to_threshold(sensitivity: int) -> float:
    """Higher sensitivity means a lower/easier sherpa match threshold."""

    return (100 - sensitivity) / 100


def _clean_text(value: Any, name: str, *, max_length: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductSettingsError(f"{name}不能为空。")
    result = " ".join(value.strip().split())
    if len(result) > max_length:
        raise ProductSettingsError(f"{name}过长。")
    return result


def _safe_display_text(value: Any, *, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:limit]


def _default_settings(config: AppConfig) -> dict[str, Any]:
    final_silence_ms = int(getattr(config.agent, "final_silence_ms", 30_000))
    paraformer_mode = config.asr.device if config.asr.device in COMPUTE_DEVICE_MODES else "auto"
    compute_devices = {name: "cpu" for name in COMPUTE_MODEL_IDS}
    compute_devices["paraformer"] = paraformer_mode
    return {
        "schema_version": PRODUCT_SETTINGS_SCHEMA_VERSION,
        "screen_capture": config.screen_capture.snapshot(),
        "quiet_hours": config.quiet_hours.snapshot(),
        "wake": {
            "enabled": bool(config.wake.enabled),
            "keyword": str(config.wake.keyword),
            "keyword_label": str(config.wake.keyword_label),
            "keywords": list(config.wake.keywords or (config.wake.keyword,)),
            "sensitivity": _threshold_to_sensitivity(config.wake.threshold),
        },
        "silence": {
            "seconds": max(MIN_SILENCE_SECONDS, min(MAX_SILENCE_SECONDS, round(final_silence_ms / 1000))),
            "smart_turn_hard_silence_ms": int(getattr(config.smart_turn, "hard_silence_ms", 1400)),
        },
        "shadow": {
            "experimental_context_gate_enabled": bool(
                config.shadow_agent.experimental_context_gate_enabled,
            ),
        },
        "custom_instructions": [],
        "hotwords": deepcopy(DEFAULT_HOTWORDS),  # hotwords（识别后热词设置）。
        "approvals": {"voice_confirmation_enabled": False, "continuous_enabled": False},  # approvals（审批确认偏好）。
        # TTS（语音合成）固定走在线 API（程序接口）；backend（运行后端）不再是产品设置。
        "tts": {"backend": config.tts.backend, "local_model": config.tts.local_model,
                "local_optimization": True, "acoustic_continuation": config.tts.acoustic_continuation,
                "inference_steps_by_model": dict(config.tts.inference_steps_by_model),
                "active_voice_by_model": {"voxcpm1": None, "voxcpm2": None}},
        "speaker": {
            **{key: getattr(config.speaker, key, value) for key, value in TUNING_DEFAULTS.items()},
            "mode": str(getattr(config.speaker, "mode", "off")),
            "confirm_threshold": float(getattr(config.speaker, "confirm_threshold", 0.50)),
            "continuity_threshold": float(getattr(config.speaker, "continuity_threshold", 0.40)),
            "continuity_relaxation": round(min(0.20, max(0.0, config.speaker.confirm_threshold - config.speaker.continuity_threshold)), 4),  # 连续主人放宽量。
            "owner_only_barge_in": bool(getattr(config.speaker, "owner_only_barge_in", False)),
        },
        # compute（计算设备）首版沿用旧运行方式：Paraformer 自动选择，其余模型保持 CPU。
        "compute": {
            "devices": compute_devices,
            "fallback_devices": {name: "cpu" for name in COMPUTE_MODEL_IDS},
        },
    }


def _legacy_speaker_scale(value: Mapping[str, Any]) -> bool:
    """True when the stored file carries speaker thresholds from the old scale.

    旧分数尺度只产生重新校准提示；CAM++迁移保留用户阈值与运行模式偏好。
    没有声纹设置的文件不产生迁移提示。
    """
    marker = value.get("schema_version")
    if not isinstance(marker, int) or isinstance(marker, bool):
        return False
    # Version 4 already migrated the speaker scale.  The TTS field added in
    # version 5 must not reset a user's version 4 thresholds a second time.
    if marker >= 4:
        return False
    speaker = value.get("speaker")
    if not isinstance(speaker, Mapping):
        return False
    return "confirm_threshold" in speaker or "continuity_threshold" in speaker


def _merge_settings(defaults: dict[str, Any], value: Any, *,
                    legacy_speaker_scale: bool = False) -> dict[str, Any]:
    from ..kws.keywords import (
        DEFAULT_WAKE_KEYWORD,
        canonical_labels,
        normalize_keyword_list,
    )

    result = deepcopy(defaults)
    if not isinstance(value, Mapping):
        return result
    if "hotwords" in value:
        try:
            result["hotwords"] = validate_hotwords(value["hotwords"])
        except ValueError:
            result["hotwords"] = deepcopy(DEFAULT_HOTWORDS)
    for section in ("wake", "silence", "shadow", "speaker", "tts", "quiet_hours", "approvals"):
        raw = value.get(section)
        if isinstance(raw, Mapping):
            result[section].update({key: raw[key] for key in result[section] if key in raw})
    for field in ("local_optimization", "acoustic_continuation"):
        if not isinstance(result["tts"].get(field), bool):
            result["tts"][field] = defaults["tts"][field]
    try:
        result["tts"]["inference_steps_by_model"] = validate_tts_inference_steps(
            result["tts"]["inference_steps_by_model"], defaults["tts"]["inference_steps_by_model"],
        )
    except ValueError:
        result["tts"]["inference_steps_by_model"] = dict(defaults["tts"]["inference_steps_by_model"])
    if (not isinstance(result["tts"].get("local_model"), str)
            or result["tts"].get("local_model") not in {"voxcpm1", "voxcpm2"}):
        result["tts"]["local_model"] = defaults["tts"]["local_model"]
    active_voices = result["tts"].get("active_voice_by_model")
    normalized_active: dict[str, dict[str, Any] | None] = {"voxcpm1": None, "voxcpm2": None}
    if isinstance(active_voices, Mapping):
        for model in normalized_active:
            selected = active_voices.get(model)
            if (isinstance(selected, Mapping)
                    and isinstance(selected.get("voice_id"), str)
                    and selected.get("voice_id")
                    and isinstance(selected.get("revision"), int)
                    and not isinstance(selected.get("revision"), bool)
                    and selected.get("revision") > 0):
                normalized_active[model] = {
                    "voice_id": selected["voice_id"], "revision": selected["revision"],
                }
    result["tts"]["active_voice_by_model"] = normalized_active
    try:
        result["custom_instructions"] = validate_entries(value.get("custom_instructions", []))
    except ValueError:
        result["custom_instructions"] = []
    # Product settings are intentionally versioned independently from the
    # model registry.  Unknown fields are discarded rather than echoed to the
    # browser, which keeps this file a narrow, non-secret state boundary.
    try:
        result["screen_capture"] = ScreenCaptureConfig(**value.get("screen_capture", defaults["screen_capture"])).snapshot()
    except (ValueError, TypeError):
        result["screen_capture"] = defaults["screen_capture"]
    result["schema_version"] = PRODUCT_SETTINGS_SCHEMA_VERSION
    raw_compute = value.get("compute")
    if isinstance(raw_compute, Mapping):
        for field, allowed in (("devices", COMPUTE_DEVICE_MODES), ("fallback_devices", {"cpu", "cuda"})):
            entries = raw_compute.get(field)
            if isinstance(entries, Mapping):
                for name in COMPUTE_MODEL_IDS:
                    mode = entries.get(name, entries.get("wespeaker") if name == "campplus" else None)
                    if isinstance(mode, str) and mode in allowed:
                        result["compute"][field][name] = mode
    wake = result["wake"]
    wake["enabled"] = wake["enabled"] if type(wake.get("enabled")) is bool else defaults["wake"]["enabled"]
    wake["keyword"] = wake["keyword"] if isinstance(wake.get("keyword"), str) and wake["keyword"].strip() else defaults["wake"]["keyword"]
    wake["keyword_label"] = wake["keyword_label"] if isinstance(wake.get("keyword_label"), str) else defaults["wake"]["keyword_label"]
    # ``keywords`` is additive: a legacy file that only has ``keyword`` yields a
    # one-item list, and whatever survives is mirrored back into
    # ``keyword``/``keyword_label`` so both shapes always agree.
    raw_keywords = wake.get("keywords")
    if isinstance(raw_keywords, (list, tuple)) and raw_keywords:
        candidates = [item for item in raw_keywords if isinstance(item, str)]
    else:
        candidates = [wake["keyword"]]
    try:
        keywords = normalize_keyword_list(candidates or [wake["keyword"]])
    except ValueError:
        # The stored list is unusable; the default phrase is validated at import
        # time, so this fallback cannot fail in practice.
        try:
            keywords = normalize_keyword_list(defaults["wake"]["keywords"])
        except ValueError:
            keywords = normalize_keyword_list([DEFAULT_WAKE_KEYWORD])
    wake["keywords"] = list(keywords)
    wake["keyword"] = keywords[0]
    try:
        wake["keyword_label"] = canonical_labels(keywords)[0]
    except ValueError:
        wake["keyword_label"] = defaults["wake"]["keyword_label"]
    try:
        sensitivity = int(wake.get("sensitivity"))
    except (TypeError, ValueError):
        sensitivity = int(defaults["wake"]["sensitivity"])
    wake["sensitivity"] = max(MIN_KWS_SENSITIVITY, min(MAX_KWS_SENSITIVITY, sensitivity))
    try:
        result["quiet_hours"] = QuietHoursConfig(**result["quiet_hours"]).snapshot()
    except ValueError:
        result["quiet_hours"] = defaults["quiet_hours"]
    if not wake["enabled"]:
        result["quiet_hours"]["enabled"] = False
    silence = result["silence"]
    try:
        seconds = int(silence.get("seconds"))
    except (TypeError, ValueError):
        seconds = int(defaults["silence"]["seconds"])
    silence["seconds"] = max(MIN_SILENCE_SECONDS, min(MAX_SILENCE_SECONDS, seconds))
    silence["smart_turn_hard_silence_ms"] = int(defaults["silence"]["smart_turn_hard_silence_ms"])
    shadow = result["shadow"]
    for field in ("voice_confirmation_enabled", "continuous_enabled"):
        if type(result["approvals"].get(field)) is not bool:
            result["approvals"][field] = False
    if type(shadow.get("experimental_context_gate_enabled")) is not bool:
        shadow["experimental_context_gate_enabled"] = defaults["shadow"]["experimental_context_gate_enabled"]
    # 旧设置可能记录了 cosyvoice；合并后统一锁定 MiniMax API（在线语音接口）。
    if result["tts"].get("backend") not in {"minimax", "voxcpm1"}:
        result["tts"]["backend"] = defaults["tts"]["backend"]
    result["tts"]["local_optimization"] = True
    speaker = result["speaker"]
    raw_speaker = value.get("speaker", {})
    if isinstance(raw_speaker, Mapping) and "continuity_relaxation" not in raw_speaker:
        try:
            speaker["continuity_relaxation"] = min(0.20, max(0.0, float(speaker["confirm_threshold"]) - float(speaker["continuity_threshold"])))
        except (TypeError, ValueError):
            speaker["continuity_relaxation"] = 0.10
    if speaker.get("mode") not in {"off", "observe", "filter"}:
        speaker["mode"] = defaults["speaker"]["mode"]
    if type(speaker.get("owner_only_barge_in")) is not bool:
        speaker["owner_only_barge_in"] = False
    try:
        confirm = float(speaker.get("confirm_threshold"))
        relaxation = float(speaker.get("continuity_relaxation", 0.10))
    except (TypeError, ValueError):
        confirm = defaults["speaker"]["confirm_threshold"]
        relaxation = defaults["speaker"]["continuity_relaxation"]
    if not MIN_SPEAKER_THRESHOLD <= confirm <= MAX_SPEAKER_THRESHOLD or not 0 <= relaxation <= 0.20:
        confirm = defaults["speaker"]["confirm_threshold"]
        relaxation = defaults["speaker"]["continuity_relaxation"]
    speaker["confirm_threshold"] = round(confirm, 4)
    speaker["continuity_relaxation"] = round(relaxation, 4)
    speaker["continuity_threshold"] = round(max(0.0, confirm - relaxation), 4)
    try:
        speaker.update(validate_tuning(speaker, continuity=speaker["continuity_threshold"]))
    except ValueError:
        speaker.update(TUNING_DEFAULTS)
        speaker["non_owner_threshold"] = min(0.35, speaker["continuity_threshold"] - 0.01)
    return result


def _settings_validation_warning(value: Mapping[str, Any]) -> str | None:
    """Report malformed persisted fields without exposing their contents."""

    invalid = False
    if "hotwords" in value:
        try:
            validate_hotwords(value["hotwords"])
        except ValueError:
            invalid = True
    quiet = value.get("quiet_hours")
    if quiet is not None:
        try:
            if not isinstance(quiet, Mapping):
                raise ValueError("被动静默设置格式无效。")
            QuietHoursConfig(**quiet)
        except (TypeError, ValueError):
            invalid = True
    schema_version = value.get("schema_version")
    if schema_version is not None and (
        type(schema_version) is not int
        or schema_version not in set(range(1, PRODUCT_SETTINGS_SCHEMA_VERSION + 1))
    ):
        invalid = True
    for section_name, fields in (
        ("wake", ("enabled", "keyword", "keyword_label", "keywords", "sensitivity")),
        ("silence", ("seconds",)),
        ("shadow", ("experimental_context_gate_enabled",)),
        ("approvals", ("voice_confirmation_enabled", "continuous_enabled")),
        ("tts", ("backend", "local_model", "local_optimization", "acoustic_continuation", "inference_steps_by_model", "active_voice_by_model")),
        ("speaker", ("mode", "confirm_threshold", "continuity_threshold", "continuity_relaxation", "owner_only_barge_in")),
    ):
        section = value.get(section_name)
        if section is None:
            continue
        if not isinstance(section, Mapping):
            invalid = True
            continue
        for field in fields:
            if field not in section:
                continue
            candidate = section[field]
            if section_name == "approvals":
                invalid = invalid or type(candidate) is not bool
            elif section_name == "wake" and field == "enabled":
                invalid = invalid or type(candidate) is not bool
            elif section_name == "wake" and field == "keyword":
                invalid = invalid or not isinstance(candidate, str) or not candidate.strip()
            elif section_name == "wake" and field == "keyword_label":
                invalid = invalid or not isinstance(candidate, str)
            elif section_name == "wake" and field == "keywords":
                invalid = invalid or (
                    not isinstance(candidate, (list, tuple))
                    or not 1 <= len(candidate) <= MAX_WAKE_KEYWORDS
                    or any(not isinstance(item, str) or not item.strip() for item in candidate)
                )
            elif section_name == "wake" and field == "sensitivity":
                invalid = invalid or type(candidate) is not int or not MIN_KWS_SENSITIVITY <= candidate <= MAX_KWS_SENSITIVITY
            elif section_name == "tts" and field == "local_model":
                invalid = invalid or not isinstance(candidate, str) or candidate not in {"voxcpm1", "voxcpm2"}
            elif section_name == "tts" and field == "inference_steps_by_model":
                try:
                    validate_tts_inference_steps(candidate)
                except ValueError:
                    invalid = True
            elif section_name == "silence" and field == "seconds":
                invalid = invalid or type(candidate) is not int or not MIN_SILENCE_SECONDS <= candidate <= MAX_SILENCE_SECONDS
            elif section_name == "shadow" and field == "experimental_context_gate_enabled":
                invalid = invalid or type(candidate) is not bool
            elif section_name == "tts" and field == "backend":
                invalid = invalid or not isinstance(candidate, str) or candidate not in {"minimax", "voxcpm1", "cosyvoice"}
            elif section_name == "tts" and field == "active_voice_by_model":
                invalid = invalid or not isinstance(candidate, Mapping) or any(
                    model not in {"voxcpm1", "voxcpm2"}
                    or selected is not None and (
                        not isinstance(selected, Mapping)
                        or not isinstance(selected.get("voice_id"), str)
                        or not selected.get("voice_id")
                        or type(selected.get("revision")) is not int
                        or selected.get("revision", 0) < 1
                    )
                    for model, selected in candidate.items()
                )
            elif section_name == "speaker" and field == "mode":
                invalid = invalid or candidate not in {"off", "observe", "filter"}
            elif section_name == "speaker" and field == "owner_only_barge_in":
                invalid = invalid or type(candidate) is not bool
            elif section_name == "speaker" and field in {"confirm_threshold", "continuity_threshold"}:
                invalid = invalid or not isinstance(candidate, (int, float)) or isinstance(candidate, bool) or not 0 <= float(candidate) <= 1
            elif section_name == "speaker" and field == "continuity_relaxation":
                invalid = invalid or type(candidate) not in {int, float} or not 0 <= float(candidate) <= 0.20
    speaker_section = value.get("speaker")
    if isinstance(speaker_section, Mapping):
        try:
            confirm_value = float(speaker_section.get("confirm_threshold"))
            continuity_value = float(speaker_section.get("continuity_threshold"))
            invalid = invalid or not 0 <= continuity_value <= confirm_value <= 1
        except (TypeError, ValueError):
            # The field-level checks above already mark non-numeric values;
            # keep this branch quiet and let the normal merge fallback apply.
            pass
    compute = value.get("compute")
    if "custom_instructions" in value:
        try:
            validate_entries(value["custom_instructions"])
        except ValueError:
            invalid = True
    if compute is not None:
        if not isinstance(compute, Mapping):
            invalid = True
        else:
            for field, allowed in (("devices", COMPUTE_DEVICE_MODES), ("fallback_devices", {"cpu", "cuda"})):
                entries = compute.get(field)
                if entries is not None:
                    if not isinstance(entries, Mapping):
                        invalid = True
                    else:
                        invalid = invalid or any(
                            key not in (*COMPUTE_MODEL_IDS, "wespeaker")
                            or not isinstance(candidate, str)
                            or candidate not in allowed
                            for key, candidate in entries.items()
                        )
    return "product_settings: 部分字段无效，已回退为安全值。" if invalid else None


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".product-settings-", suffix=".json", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=path.suffix or ".tmp", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def apply_settings_to_config(config: AppConfig, settings: Mapping[str, Any]) -> AppConfig:
    """Return a config overlay without changing the YAML or secret source."""

    from ..kws.keywords import canonical_labels, normalize_keyword_list

    wake = settings.get("wake") if isinstance(settings.get("wake"), Mapping) else {}
    silence = settings.get("silence") if isinstance(settings.get("silence"), Mapping) else {}
    shadow = settings.get("shadow") if isinstance(settings.get("shadow"), Mapping) else {}
    speaker = settings.get("speaker") if isinstance(settings.get("speaker"), Mapping) else {}
    compute = settings.get("compute") if isinstance(settings.get("compute"), Mapping) else {}
    try:
        seconds = int(silence.get("seconds", getattr(config.agent, "final_silence_ms", 30_000) // 1000))
    except (TypeError, ValueError):
        seconds = 30
    seconds = max(MIN_SILENCE_SECONDS, min(MAX_SILENCE_SECONDS, seconds))
    try:
        sensitivity = int(wake.get("sensitivity", _threshold_to_sensitivity(config.wake.threshold)))
    except (TypeError, ValueError):
        sensitivity = _threshold_to_sensitivity(config.wake.threshold)
    sensitivity = max(MIN_KWS_SENSITIVITY, min(MAX_KWS_SENSITIVITY, sensitivity))
    enabled = bool(wake.get("enabled", config.wake.enabled))
    # ``keywords`` is the authoritative phrase list; a settings file that still
    # carries only the legacy single ``keyword`` falls back to a one-item list.
    try:
        keywords = normalize_keyword_list(
            wake.get("keywords") if isinstance(wake.get("keywords"), (list, tuple))
            else [wake.get("keyword") or config.wake.keyword]
        )
    except ValueError:
        keywords = config.wake.keywords or (config.wake.keyword,)
    labels = canonical_labels(keywords)
    next_wake = replace(
        config.wake,
        enabled=enabled,
        # KWS has one product mode: enabled means the microphone stays in
        # wake-word standby until the user stops the session.  Keep the
        # legacy dataclass field synchronized for older controller fixtures,
        # but do not expose or read a second UI setting.
        always_on=enabled,
        keyword=keywords[0],
        keyword_label=labels[0],
        keywords=keywords,
        threshold=_sensitivity_to_threshold(sensitivity),
    )
    next_agent = replace(
        config.agent,
        final_silence_ms=seconds * 1000,
    )
    next_shadow = replace(
        config.shadow_agent,
        experimental_context_gate_enabled=bool(
            shadow.get(
                "experimental_context_gate_enabled",
                config.shadow_agent.experimental_context_gate_enabled,
            )
        ),
    )
    mode = str(speaker.get("mode", getattr(config.speaker, "mode", "off")))
    try:
        confirm_threshold = float(speaker.get("confirm_threshold", getattr(config.speaker, "confirm_threshold", 0.50)))
        continuity_threshold = float(speaker.get("continuity_threshold", getattr(config.speaker, "continuity_threshold", 0.40)))
        relaxation = float(speaker.get("continuity_relaxation", min(0.20, max(0.0, confirm_threshold - continuity_threshold))))
    except (TypeError, ValueError):
        confirm_threshold = getattr(config.speaker, "confirm_threshold", 0.50)
        continuity_threshold = getattr(config.speaker, "continuity_threshold", 0.40)
        relaxation = 0.10
    if mode not in {"off", "observe", "filter"} or not 0 <= confirm_threshold <= 1 or not 0 <= relaxation <= 0.20:
        mode = "off"
        confirm_threshold = 0.50
        continuity_threshold = 0.40
        relaxation = 0.10
    next_speaker = replace(
        config.speaker,
        mode=mode,
        owner_only_barge_in=bool(speaker.get("owner_only_barge_in", getattr(config.speaker, "owner_only_barge_in", False))),
        confirm_threshold=confirm_threshold,
        continuity_threshold=continuity_threshold,
        continuity_relaxation=relaxation,
        **validate_tuning(speaker, continuity=max(0.0, confirm_threshold-relaxation)),
    )
    tts = settings.get("tts") if isinstance(settings.get("tts"), Mapping) else {}
    backend = tts.get("backend", config.tts.backend)
    if not isinstance(backend, str) or backend not in {"minimax", "voxcpm1"}:
        backend = config.tts.backend
    local_model = tts.get("local_model", config.tts.local_model)
    if not isinstance(local_model, str) or local_model not in {"voxcpm1", "voxcpm2"}:
        local_model = config.tts.local_model
    next_tts = replace(
        config.tts,
        backend=backend,
        sample_rate=16000 if backend == "voxcpm1" else config.tts.sample_rate,
        provider="minimax",
        local_model=str(local_model),
        local_optimization=tts.get("local_optimization", config.tts.local_optimization),
        acoustic_continuation=tts.get("acoustic_continuation", config.tts.acoustic_continuation),
        inference_steps_by_model=tts.get("inference_steps_by_model", config.tts.inference_steps_by_model),

    )
    next_compute = ComputeConfig(
        devices={**ComputeConfig().devices,
                 **(compute.get("devices") if isinstance(compute.get("devices"), Mapping) else {})},
        fallback_devices={**ComputeConfig().fallback_devices,
                          **(compute.get("fallback_devices") if isinstance(compute.get("fallback_devices"), Mapping) else {})},
    )
    quiet = QuietHoursConfig(**dict(settings.get("quiet_hours", config.quiet_hours.snapshot())))
    if quiet.enabled and not next_wake.enabled:
        raise ProductSettingsError("被动静默要求开启 KWS（关键词唤醒）。")
    return replace(config, screen_capture=ScreenCaptureConfig(**settings.get("screen_capture", config.screen_capture.snapshot())), quiet_hours=quiet, wake=next_wake, agent=next_agent, shadow_agent=next_shadow,
                   speaker=next_speaker, tts=next_tts, compute=next_compute)


class ProductSettingsStore:
    """Atomic, non-secret product settings plus allowlisted Persona writes."""

    def __init__(self, *, path: Path, config: AppConfig) -> None:
        self.path = path
        self.config = config
        self.persona_directory = (config.paths.persona if config.paths and config.paths.separated
                                  else path.resolve().parent / "persona")
        self.warnings: list[str] = []
        # Keep the legacy observable version for a damaged schema marker.
        # A normal load and every successful save use schema version 6;
        # malformed legacy markers still expose the safe recovery version 2.
        self._malformed_schema_marker = False
        self._settings = self._load()

    def _load(self) -> dict[str, Any]:
        defaults = _default_settings(self.config)
        if not self.path.is_file():
            return defaults
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.warnings = [f"product_settings: 无法读取设置文件（{type(exc).__name__}），已使用安全默认值。"]
            return defaults
        if not isinstance(value, Mapping):
            self.warnings = ["product_settings: 设置文件格式无效，已使用安全默认值。"]
            return defaults
        warning = _settings_validation_warning(value)
        if warning:
            self.warnings = [warning]
            marker = value.get("schema_version")
            self._malformed_schema_marker = not isinstance(marker, int)
        legacy = _legacy_speaker_scale(value)
        if legacy:
            self.warnings.append(
                "product_settings: 声纹特征管线已更换，旧阈值是在另一套分数尺度上标定的，"
                "已保留设置偏好；缺少CAM++注册档案时运行于观察状态，请重新注册后校准。"
            )
        merged = _merge_settings(defaults, value, legacy_speaker_scale=legacy)
        stored_tts = value.get("tts")
        if isinstance(stored_tts, Mapping) and stored_tts.get("backend") == "cosyvoice":
            # Migrate only the obsolete selection value; preserve all other
            # persisted fields and any diagnostics recorded above.
            migrated = deepcopy(dict(value))
            migrated_tts = deepcopy(dict(stored_tts))
            migrated_tts["backend"] = "minimax"
            migrated["tts"] = migrated_tts
            try:
                _atomic_write_json(self.path, migrated)
            except OSError:
                self.warnings.append(
                    "product_settings: 已在运行时锁定 MiniMax API，但旧 TTS 设置未能写回文件。"
                )
        return merged

    def snapshot(self) -> dict[str, Any]:
        value = self.settings_only()
        if self._malformed_schema_marker:
            value["schema_version"] = 2
        value["persona"] = self.read_persona()
        value["warnings"] = list(self.warnings)
        return value

    def settings_only(self) -> dict[str, Any]:
        result = deepcopy(self._settings)
        selections = getattr(self, "_tts_voice_selections", None)
        if selections:
            result["tts"]["active_voice_by_model"] = selections()
        return result

    def save_hotwords(self, payload: dict) -> dict:
        """save_hotwords（保存热词）：写入失败不改变内存配置。"""
        try:
            value = validate_hotwords(payload)
        except ValueError as exc:
            raise ProductSettingsError(str(exc)) from exc
        previous = self._settings
        self._settings = {**previous, "hotwords": value}
        try:
            self._save_settings()
        except Exception:
            self._settings = previous
            raise
        return deepcopy(value)

    def save_tts_local_model(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """在运行切换成功后保存 backend（语音后端）及 local_model（本地模型）。"""
        if not payload or set(payload) - {"backend", "local_model", "local_optimization", "acoustic_continuation", "inference_steps_by_model"}:
            raise ProductSettingsError(
                "TTS 设置仅接受 backend（语音后端）和 local_model（本地语音合成模型）。"
            )
        model = payload.get("local_model", self._settings["tts"]["local_model"])
        if not isinstance(model, str) or model not in {"voxcpm1", "voxcpm2"}:
            raise ProductSettingsError(
                "local_model（本地语音合成模型）必须为 voxcpm1 或 voxcpm2。"
            )
        for field in ("local_optimization", "acoustic_continuation"):
            if field in payload and not isinstance(payload[field], bool):
                raise ProductSettingsError(f"{field}（本地优化开关）必须为布尔值。")
        previous = deepcopy(self._settings["tts"])
        try:
            steps = validate_tts_inference_steps(
                payload.get("inference_steps_by_model", {}), previous["inference_steps_by_model"],
            )
        except ValueError as exc:
            raise ProductSettingsError(str(exc)) from exc
        backend = payload.get("backend", previous["backend"])
        if not isinstance(backend, str) or backend not in {"minimax", "voxcpm1"}:
            raise ProductSettingsError("backend（语音后端）必须为 minimax 或 voxcpm1。")
        previous_version = self._settings["schema_version"]
        self._settings["tts"] = {**previous, **dict(payload), "local_optimization": True, "local_model": model, "backend": backend,
                                 "inference_steps_by_model": steps}
        self._settings["schema_version"] = PRODUCT_SETTINGS_SCHEMA_VERSION
        try:
            self._save_settings()
        except Exception:
            self._settings["tts"] = previous
            self._settings["schema_version"] = previous_version
            raise
        return deepcopy(self._settings["tts"])

    def save_tts_voice_selection(self, model: str, selection: Mapping[str, Any]) -> dict[str, Any]:
        """原子保存 model（本地模型）的 voice_id（音色标识）和 revision（音色版本）。"""
        if model not in {"voxcpm1", "voxcpm2"}:
            raise ProductSettingsError("model（本地模型）必须为 voxcpm1 或 voxcpm2。")
        voice_id, revision = selection.get("voice_id"), selection.get("revision")
        if (not isinstance(voice_id, str) or not voice_id or len(voice_id) > 80
                or type(revision) is not int or revision < 1):
            raise ProductSettingsError("音色选择必须包含有效的 voice_id（音色标识）和 revision（音色版本）。")
        previous = deepcopy(self._settings["tts"])
        previous_version = self._settings["schema_version"]
        active = deepcopy(previous.get("active_voice_by_model") or {"voxcpm1": None, "voxcpm2": None})
        active[model] = {"voice_id": voice_id, "revision": revision}
        self._settings["tts"] = {**previous, "active_voice_by_model": active}
        self._settings["schema_version"] = PRODUCT_SETTINGS_SCHEMA_VERSION
        try:
            self._save_settings()
        except Exception:
            self._settings["tts"] = previous
            self._settings["schema_version"] = previous_version
            raise
        return deepcopy(active)

    def read_persona(self) -> dict[str, str]:
        directory = self.persona_directory.resolve()
        result: dict[str, str] = {}
        for key, filename in PERSONA_FILES.items():
            path = (directory / filename).resolve()
            try:
                path.relative_to(directory)
                result[key] = path.read_text(encoding="utf-8") if path.is_file() else ""
            except (OSError, UnicodeError, ValueError):
                result[key] = ""
        return result

    def save_custom_instructions(self, value: Any) -> dict[str, Any]:
        try:
            entries = validate_entries(value)
        except ValueError as exc:
            raise ProductSettingsError(str(exc)) from exc
        self._settings["custom_instructions"] = entries
        self._settings["schema_version"] = PRODUCT_SETTINGS_SCHEMA_VERSION
        self._save_settings()
        return self.snapshot()

    def effective_config(self) -> AppConfig:
        return apply_settings_to_config(self.config, self._settings)

    def _save_settings(self) -> None:
        saved = deepcopy(self._settings)
        if getattr(self, "_tts_voice_selections", None):
            saved["tts"].pop("active_voice_by_model", None)
        _atomic_write_json(self.path, saved)
        # A successful explicit save repairs a previously unreadable settings
        # artifact.  Keep the warning until that point, but do not continue
        # advertising a stale recovery warning after the file is valid.
        self.warnings.clear()
        self._malformed_schema_marker = False

    @staticmethod
    def _atomic_replace_documents(documents: Mapping[Path, str]) -> None:
        """Replace the four allowlisted Persona files as one small batch.

        Each document is prepared and fsynced in a staging directory first.
        If a later ``os.replace`` fails, already-replaced files are restored
        from their backups (or removed when they did not exist before), so a
        failed save cannot leave the main and Shadow prompts out of sync.
        """

        destinations = list(documents)
        if not destinations:
            return
        common_parent = destinations[0].parent.parent
        common_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".product-persona-", dir=str(common_parent)) as temporary_name:
            temporary = Path(temporary_name)
            staged: dict[Path, Path] = {}
            backups: dict[Path, Path | None] = {}
            for index, destination in enumerate(destinations):
                staged_path = temporary / f"document-{index}.md"
                _atomic_write_text(staged_path, documents[destination])
                staged[destination] = staged_path
                backup = None
                if destination.exists():
                    if not destination.is_file():
                        raise ProductSettingsError(
                            f"Persona 目标不是普通文件：{destination.name}。"
                        )
                    try:
                        backup = temporary / f"backup-{index}.md"
                        backup.write_bytes(destination.read_bytes())
                    except OSError as exc:
                        raise ProductSettingsError(
                            f"Persona 旧文档无法读取：{destination.name}。"
                        ) from exc
                backups[destination] = backup
                destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                for destination in destinations:
                    os.replace(staged[destination], destination)
            except Exception as exc:
                for destination in destinations:
                    try:
                        backup = backups[destination]
                        if backup is not None and backup.exists():
                            os.replace(backup, destination)
                        elif destination.exists():
                            destination.unlink()
                    except OSError:
                        pass
                raise ProductSettingsError(
                    f"Persona 文档原子替换失败：{type(exc).__name__}。"
                ) from exc

    async def save_wake(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if payload.get("enabled") is False and self._settings["quiet_hours"]["enabled"]:
            raise ProductSettingsError("请先关闭被动静默时间，再禁用 KWS（关键词唤醒）。")
        if type(payload.get("enabled")) is not bool:
            raise ProductSettingsError("enabled 必须是布尔值。")
        raw_sensitivity = payload.get("sensitivity")
        if type(raw_sensitivity) is not int:
            raise ProductSettingsError("sensitivity 必须是整数。")
        sensitivity = raw_sensitivity
        if not MIN_KWS_SENSITIVITY <= sensitivity <= MAX_KWS_SENSITIVITY:
            raise ProductSettingsError(f"sensitivity 必须在 {MIN_KWS_SENSITIVITY} 至 {MAX_KWS_SENSITIVITY} 之间。")
        from ..kws.keywords import (
            MAX_WAKE_KEYWORDS,
            canonical_labels,
            normalize_keyword_list,
            validate_keywords_file,
        )

        # ``keywords`` (a list) is the current contract; ``keyword`` (a string)
        # stays accepted so an older client or a stored payload keeps working.
        raw_keywords = payload.get("keywords")
        if raw_keywords is None:
            raw_keywords = [payload.get("keyword")]
        if isinstance(raw_keywords, (str, bytes)) or not isinstance(raw_keywords, (list, tuple)):
            raise ProductSettingsError("keywords 必须是唤醒词列表。")
        if not raw_keywords:
            raise ProductSettingsError("请至少填写 1 个唤醒词。")
        cleaned: list[str] = []
        for item in raw_keywords:
            if not isinstance(item, str):
                raise ProductSettingsError("唤醒词必须是字符串。")
            if item.strip():
                cleaned.append(_clean_text(item, "唤醒词", max_length=120))
        try:
            keywords = normalize_keyword_list(cleaned)
            labels = canonical_labels(keywords)
        except (TypeError, ValueError) as exc:
            raise ProductSettingsError(f"唤醒词无法生成有效标签：{str(exc)[:200]}") from exc
        if len(keywords) > MAX_WAKE_KEYWORDS:  # defensive: normalize already caps
            raise ProductSettingsError(f"最多支持 {MAX_WAKE_KEYWORDS} 个唤醒词。")
        previous = self._settings["wake"]
        try:
            previous_keywords = normalize_keyword_list(
                previous.get("keywords") if isinstance(previous.get("keywords"), (list, tuple))
                else [previous.get("keyword") or ""]
            )
        except ValueError:
            previous_keywords = ()
        compile_required = keywords != previous_keywords
        if payload["enabled"] and not compile_required:
            # 首次启用、迁移或文件损坏时，即使唤醒词未改动也需重新生成。
            try:
                validate_keywords_file(self.config.kws_keywords_file, labels)
            except (OSError, UnicodeError, ValueError):
                compile_required = True
        if compile_required:
            await asyncio.to_thread(self._compile_keyword, tuple(zip(keywords, labels)))
        self._settings["wake"] = {
            "enabled": payload["enabled"],
            "keyword": keywords[0],
            "keyword_label": labels[0],
            "keywords": list(keywords),
            "sensitivity": sensitivity,
        }
        self._save_settings()
        return self.snapshot()

    def _compile_keyword(self, entries: Sequence[tuple[str, str]]) -> None:
        """Compile every wake phrase and atomically replace both keyword files."""

        from ..kws.detector import discover_model_paths
        from ..kws.keywords import generate_keywords_for, validate_keywords_file

        if not entries:
            raise ProductSettingsError("请至少填写 1 个唤醒词。")
        labels = tuple(label for _keyword, label in entries)
        model_dir = self.config.kws_dir.resolve()
        target = self.config.kws_keywords_file.resolve()
        paths = self.config.paths
        # KWS（关键词唤醒）生成文件随用户配置迁移，唯一受管目录由 AppPaths（路径契约）决定。
        allowed_root = (paths.keywords.parent if paths and paths.separated else model_dir).resolve()
        try:
            if paths and paths.separated:
                allowed_root.relative_to(paths.data_root.resolve())
            target.relative_to(allowed_root)
        except ValueError as exc:
            raise ProductSettingsError("KWS（关键词唤醒）文件必须位于受管生成资源目录内。") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_target = target.parent / "keywords_raw.txt"
        with tempfile.TemporaryDirectory(prefix=".product-kws-", dir=str(target.parent)) as temporary_name:
            temporary = Path(temporary_name)
            raw_path = temporary / "keywords_raw.txt"
            compiled_path = temporary / "keywords.txt"
            try:
                generate_keywords_for(
                    model_dir=model_dir,
                    raw_path=raw_path,
                    output_path=compiled_path,
                    entries=tuple(entries),
                )
                validate_keywords_file(compiled_path, labels)
                # Force path discovery/label validation once more before any
                # live worker can observe the new files.
                discover_model_paths(model_dir, compiled_path, labels)
            except Exception as exc:
                if isinstance(exc, ProductSettingsError):
                    raise
                raise ProductSettingsError(f"KWS 唤醒词生成失败：{str(exc)[:300]}") from exc

            backups: list[tuple[Path, Path | None]] = []
            for destination in (raw_target, target):
                backup = None
                if destination.exists():
                    if not destination.is_file():
                        raise ProductSettingsError(
                            f"KWS 目标不是普通文件：{destination.name}。"
                        )
                    try:
                        backup = temporary / f"backup-{destination.name}"
                        backup.write_bytes(destination.read_bytes())
                    except OSError as exc:
                        raise ProductSettingsError(
                            f"KWS 旧关键词文件无法读取：{destination.name}。"
                        ) from exc
                backups.append((destination, backup))
            try:
                os.replace(raw_path, raw_target)
                os.replace(compiled_path, target)
            except Exception as exc:
                for destination, backup in backups:
                    try:
                        if backup is not None and backup.exists():
                            os.replace(backup, destination)
                        elif destination.exists():
                            # The destination may have been replaced
                            # successfully before the second file failed.
                            # Remove that new file when there was no previous
                            # file to restore, otherwise a raw/compiled
                            # keyword pair could be left half-new.
                            destination.unlink()
                    except OSError:
                        pass
                raise ProductSettingsError(f"KWS 关键词文件替换失败：{type(exc).__name__}。") from exc

    def save_quiet_hours(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != {"enabled", "start", "end"}:
            raise ProductSettingsError("被动静默字段必须为 enabled（启用）、start（开始时间）、end（结束时间）。")
        try:
            quiet = QuietHoursConfig(**dict(payload))
        except ValueError as exc:
            raise ProductSettingsError(str(exc)) from exc
        if quiet.enabled and not self._settings["wake"]["enabled"]:
            raise ProductSettingsError("开启被动静默时间需要先开启 KWS（关键词唤醒）。")
        previous = self._settings["quiet_hours"]
        self._settings["quiet_hours"] = quiet.snapshot()
        try:
            self._save_settings()
        except BaseException:
            self._settings["quiet_hours"] = previous
            raise
        return self.snapshot()

    async def save_silence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_seconds = payload.get("seconds")
        if type(raw_seconds) is not int:
            raise ProductSettingsError("seconds（静默秒数）必须是整数。")
        seconds = raw_seconds
        if not MIN_SILENCE_SECONDS <= seconds <= MAX_SILENCE_SECONDS:
            raise ProductSettingsError(f"seconds（静默秒数）必须在 {MIN_SILENCE_SECONDS} 至 {MAX_SILENCE_SECONDS} 之间。")
        previous = self._settings["silence"]
        self._settings["silence"] = {
            "seconds": seconds,
            "smart_turn_hard_silence_ms": int(getattr(self.config.smart_turn, "hard_silence_ms", 1400)),
        }
        try:
            self._save_settings()
        except BaseException:
            self._settings["silence"] = previous
            raise
        return self.snapshot()

    def save_approvals(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """save_approvals（保存审批确认偏好）：不保存已授予的连续授权。"""
        fields = {"voice_confirmation_enabled", "continuous_enabled"}
        if set(payload) != fields or any(type(payload[key]) is not bool for key in fields):
            raise ProductSettingsError("审批设置需包含 voice_confirmation_enabled（语音确认开关）和 continuous_enabled（首次确认后连续提权开关），均须为布尔值。")
        previous = self._settings["approvals"]
        self._settings["approvals"] = dict(payload)
        try:
            self._save_settings()
        except BaseException:
            self._settings["approvals"] = previous
            raise
        return deepcopy(self._settings["approvals"])

    def save_shadow(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != {"experimental_context_gate_enabled"}:
            raise ProductSettingsError(
                "Shadow 设置字段必须仅包含 experimental_context_gate_enabled。"
            )
        enabled = payload.get("experimental_context_gate_enabled")
        if type(enabled) is not bool:
            raise ProductSettingsError("experimental_context_gate_enabled 必须是布尔值。")
        self._settings["shadow"] = {
            "experimental_context_gate_enabled": enabled,
        }
        self._save_settings()
        return deepcopy(self._settings["shadow"])

    def save_speaker(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """原子保存声纹策略；兼容旧四字段请求，不清除新增参数。"""
        base = {"mode", "confirm_threshold", "continuity_threshold", "continuity_relaxation", "owner_only_barge_in"}
        if set(payload) - (base | set(TUNING_DEFAULTS)) or not {"mode", "confirm_threshold", "owner_only_barge_in"} <= set(payload):
            raise ProductSettingsError("声纹设置字段无效或缺少模式、主人阈值、主人打断开关")
        candidate = {**self._settings["speaker"], **payload}
        if candidate["mode"] not in {"off", "observe", "filter"} or type(candidate["owner_only_barge_in"]) is not bool:
            raise ProductSettingsError("声纹模式或主人打断开关无效")
        try:
            confirm = candidate["confirm_threshold"]
            if "continuity_relaxation" in payload:
                relaxation = payload["continuity_relaxation"]
            elif "continuity_threshold" in payload:
                continuity_legacy = payload["continuity_threshold"]
                if type(continuity_legacy) not in {int, float} or not 0 <= continuity_legacy <= confirm:
                    raise ValueError("旧连续阈值必须在0与主人阈值之间")
                relaxation = min(0.20, confirm - continuity_legacy)
            else:
                relaxation = candidate.get("continuity_relaxation", 0.10)
            import math
            if type(confirm) not in {int, float} or type(relaxation) not in {int, float} or not math.isfinite(confirm) or not math.isfinite(relaxation) or not 0 <= confirm <= 1 or not 0 <= relaxation <= 0.20:
                raise ValueError("主人阈值必须在0至1之间，连续放宽量必须在0至0.20之间")
            continuity = max(0.0, confirm - relaxation)
            # 旧客户端未传反证参数时，将新增默认值约束到旧阈值下方。
            if "non_owner_threshold" not in payload and candidate["non_owner_threshold"] >= continuity:
                candidate["non_owner_threshold"] = continuity - 0.01
            tuning = validate_tuning(candidate, continuity=continuity)
        except (TypeError, ValueError) as exc:
            raise ProductSettingsError(str(exc)) from exc
        saved = {"mode": candidate["mode"], "owner_only_barge_in": candidate["owner_only_barge_in"],
                 "confirm_threshold": round(confirm, 4), "continuity_relaxation": round(relaxation, 4),
                 "continuity_threshold": round(continuity, 4), **tuning}
        self._settings["speaker"] = saved
        self._save_settings()
        return deepcopy(saved)

    def save_compute(self, payload: Mapping[str, Any], *,
                     active_devices: Mapping[str, str] | None = None) -> dict[str, Any]:
        """保存本地模型设备选择，并记录每项最近成功设备作为启动回退。"""
        if set(payload) != {"devices"} or not isinstance(payload.get("devices"), Mapping):
            raise ProductSettingsError("计算设备设置字段必须仅包含 devices（模型设备选项）。")
        submitted = payload["devices"]
        if set(submitted) != set(COMPUTE_MODEL_IDS):
            raise ProductSettingsError("计算设备设置必须包含全部六种非 TTS 本地模型。")
        devices: dict[str, str] = {}
        for name in COMPUTE_MODEL_IDS:
            mode = submitted.get(name)
            if not isinstance(mode, str) or mode not in COMPUTE_DEVICE_MODES:
                raise ProductSettingsError(f"{name} 的设备必须是 auto、cpu 或 cuda。")
            devices[name] = mode
        previous = self._settings.get("compute", {})
        old_fallbacks = previous.get("fallback_devices", {}) if isinstance(previous, Mapping) else {}
        fallbacks: dict[str, str] = {}
        for name in COMPUTE_MODEL_IDS:
            active = (active_devices or {}).get(name)
            if active in {"cpu", "cuda", "cuda:0"}:
                fallbacks[name] = "cuda" if active.startswith("cuda") else "cpu"
            else:
                old = old_fallbacks.get(name) if isinstance(old_fallbacks, Mapping) else None
                fallbacks[name] = old if old in {"cpu", "cuda"} else "cpu"
        self._settings["compute"] = {"devices": devices, "fallback_devices": fallbacks}
        self._save_settings()
        return deepcopy(self._settings["compute"])

    def revert_failed_compute_devices(self, model_status: Mapping[str, Any]) -> dict[str, str]:
        """把显式 CUDA 启动失败的偏好恢复为上次成功设备。"""
        compute = self._settings.get("compute", {})
        if not isinstance(compute, Mapping):
            return {}
        devices = dict(compute.get("devices", {})) if isinstance(compute.get("devices"), Mapping) else {}
        fallbacks = dict(compute.get("fallback_devices", {})) if isinstance(compute.get("fallback_devices"), Mapping) else {}
        actual: dict[str, str] = {}
        restored: dict[str, str] = {}
        for name in COMPUTE_MODEL_IDS:
            item = model_status.get(name)
            if not isinstance(item, Mapping):
                continue
            device = item.get("device")
            if device in {"cpu", "cuda", "cuda:0"}:
                actual[name] = "cuda" if str(device).startswith("cuda") else "cpu"
            if devices.get(name) == "cuda" and actual.get(name) == "cpu" and item.get("load_error"):
                devices[name] = fallbacks.get(name, "cpu") if fallbacks.get(name) in {"cpu", "cuda"} else "cpu"
                restored[name] = devices[name]
        if devices != compute.get("devices", {}):
            self._settings["compute"] = {
                "devices": devices,
                "fallback_devices": {name: actual.get(name, fallbacks.get(name, "cpu")) for name in COMPUTE_MODEL_IDS},
            }
            self._save_settings()
        return restored

    def save_persona(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != set(PERSONA_FILES):
            raise ProductSettingsError("Persona 文档字段必须是 personality、user。")
        values: dict[str, str] = {}
        for key in PERSONA_FILES:
            value = payload.get(key)
            if not isinstance(value, str) or "\x00" in value:
                raise ProductSettingsError(f"Persona 文档 {key} 内容无效。")
            if len(value.encode("utf-8")) > MAX_PERSONA_DOCUMENT_BYTES:
                raise ProductSettingsError(f"Persona 文档 {key} 不得超过 64 KB。")
            values[key] = value

        # Product writes are deliberately pinned to the repository config
        # layout.  The client never supplies a path, and a YAML override must
        # not turn this API into an arbitrary filesystem writer.
        persona_dir = self.persona_directory.resolve()
        paths = {key: persona_dir / filename for key, filename in PERSONA_FILES.items()}
        for path in paths.values():
            try:
                path.resolve().relative_to(persona_dir)
            except ValueError as exc:
                raise ProductSettingsError("Persona 路径不在允许目录内。") from exc

        self._atomic_replace_documents({path: values[key] for key, path in paths.items()})
        return self.snapshot()


class ShadowProductProjection:
    """Bounded, user-readable projection of Shadow status/progress events."""

    _KIND_LABELS = {
        "progress": "后台处理中",
        "information": "Voice 信息",
        "input_required": "等待你的输入",
        "result": "完成",
        "no_action": "无需动作",
        "failed": "需要注意",
    }

    def __init__(self) -> None:
        self.available = False
        self.active = False
        self.user_input_required = False
        self.phase = "unavailable"
        self.focus = ""
        self.summary = ""
        self.latest_event: dict[str, str] | None = None  # latest_event（实际投递语音模型的最新事件正文）。
        self.timeline: deque[dict[str, Any]] = deque(maxlen=5)
        self.activities: deque[dict[str, Any]] = deque(maxlen=96)
        self._activity_run_seq: int | None = None
        self.last_activity_at = ""

    def reset(self, *, available: bool | None = None) -> None:
        if available is not None:
            self.available = bool(available)
        self.active = False
        self.user_input_required = False
        self.phase = "idle" if self.available else "unavailable"
        self.focus = ""
        self.summary = ""
        self.latest_event = None
        self.timeline.clear()
        self.activities.clear()
        self._activity_run_seq = None
        self.last_activity_at = ""

    def update_runtime(self, value: Mapping[str, Any] | None) -> None:
        if not isinstance(value, Mapping):
            return
        pending_ids = value.get("pending_approval_ids")
        if isinstance(pending_ids, list) and all(isinstance(item, str) for item in pending_ids):
            # 重连和终态快照也清理已失效的审批入口，不能从历史卡片恢复授权。
            for item in self.activities:
                if (item.get("stream") == "approval" and item.get("phase") == "started"
                        and item.get("approval_id") not in pending_ids):
                    item["phase"] = "failed"
                    item["text"] = (str(item.get("text") or "") + "\n\n权限请求已结束")[-12000:]
        self.available = bool(value.get("available", self.available))
        pending_jobs = any(item.get("run_seq") == self._activity_run_seq
                           and item.get("stream") == "output"
                           and str(item.get("item_id", "")).startswith("job-")
                           and item.get("phase") in {"started", "delta"} for item in self.activities)
        self.active = bool(value.get("active", self.active)) or pending_jobs
        self.user_input_required = bool(value.get("user_input_required", self.user_input_required))
        # status（状态观察器摘要）仅供模型使用，禁止从运行快照重新映射到前台。
        if not self.available:
            self.phase = "unavailable"
        elif self.user_input_required:
            self.phase = "waiting"
        elif self.active:
            self.phase = "working"
        elif self.phase not in {"completed", "error"}:
            self.phase = "idle"

    def update_events(self, events: list[ShadowEvent]) -> None:
        for item in events:
            if item.source == "observer":
                continue  # observer（观察器）摘要仅供模型二等信箱使用，不映射到产品界面。
            kind = str(item.kind)
            if kind not in self._KIND_LABELS:
                continue
            text = _safe_display_text(item.text, limit=320)
            self.latest_event = {"text": _safe_display_text(item.text, limit=8000), "event_kind": kind,
                                 "timestamp": getattr(item, "timestamp", "") or utc_now_iso()}
            if kind in {"progress", "information"} and text:
                self.focus = text
                self.summary = text
            if kind == "input_required":
                self.user_input_required = True
                self.active = False
                self.phase = "waiting"
            elif kind in {"result", "no_action"}:
                self.user_input_required = False
                self.active = False
                self.phase = "completed"
            elif kind == "failed":
                self.user_input_required = False
                self.active = False
                self.phase = "error"
            elif kind in {"progress", "information"}:
                self.active = True
                self.phase = "working"
            self.timeline.append({
                "state": "active" if self.active and kind in {"progress", "information"} else "done",
                "label": self._KIND_LABELS[kind],
                "text": text,
                "timestamp": getattr(item, "timestamp", "") or utc_now_iso(),
            })

    def update_activities(self, activities: list[Mapping[str, Any]]) -> None:
        """Merge ordered UI-only activities without changing Shadow state."""
        valid_streams = {"lifecycle", "thinking", "tool", "event", "output", "approval"}
        valid_phases = {"started", "delta", "completed", "failed"}
        valid_event_kinds = {"progress", "information", "input_required", "result", "no_action", "failed"}
        for raw in activities:
            if not isinstance(raw, Mapping):
                continue
            run_seq = raw.get("run_seq")
            if isinstance(run_seq, bool) or not isinstance(run_seq, int) or run_seq < 0:
                continue
            item_id = raw.get("item_id")
            stream = raw.get("stream")
            phase = raw.get("phase")
            if not isinstance(item_id, str) or not item_id.strip() or stream not in valid_streams or phase not in valid_phases:
                continue
            if self._activity_run_seq is not None and run_seq < self._activity_run_seq:
                continue
            if self._activity_run_seq is None or run_seq > self._activity_run_seq:
                # Keep a short completed tail for reconnect/history while
                # discarding unfinished items from the previous run.
                completed_tail = [
                    dict(item) for item in self.activities
                    if item.get("phase") in {"completed", "failed"}
                ][-32:]
                self.activities = deque(completed_tail, maxlen=96)
                self._activity_run_seq = run_seq
            if stream == "lifecycle" and raw.get("state") in {"cancelled", "failed"}:
                # 取消和失败关闭所属作业，避免迟到回传被拒后仍显示永久运行中。
                for existing in self.activities:
                    if (existing.get("run_seq") == run_seq and existing.get("stream") == "output"
                            and str(existing.get("item_id", "")).startswith("job-")
                            and existing.get("phase") in {"started", "delta"}):
                        existing["phase"] = "failed"
                        existing["tool_status"] = "failed"
            timestamp = raw.get("timestamp")
            item: dict[str, Any] = {
                "run_seq": run_seq,
                "item_id": item_id[:160],
                "stream": stream,
                "phase": phase,
                "timestamp": timestamp if isinstance(timestamp, str) and timestamp else utc_now_iso(),
            }
            for key, limit in (("text", 12000), ("delta", 2000), ("tool_name", 160), ("tool_status", 120), ("approval_id", 160)):
                value = raw.get(key)
                if isinstance(value, str):
                    item[key] = value.replace("\x00", "")[:limit] if key in {"text", "delta"} else _safe_display_text(value, limit=limit)
            event_kind = raw.get("event_kind")
            if event_kind in valid_event_kinds:
                item["event_kind"] = event_kind

            current_index = next(
                (index for index, existing in enumerate(self.activities)
                 if existing.get("run_seq") == run_seq and existing.get("item_id") == item_id),
                None,
            )
            if current_index is not None:
                existing_phase = self.activities[current_index].get("phase")
                if existing_phase in {"completed", "failed"} and phase != existing_phase:
                    # A terminal item is authoritative even if a delayed
                    # provider delta arrives after the relay has completed.
                    continue
            current = dict(self.activities[current_index]) if current_index is not None else {}
            merged = {**current, **item}
            if phase == "delta":
                delta = item.get("delta")
                if isinstance(delta, str) and delta:
                    merged["text"] = f"{current.get('text', '')}{delta}"[-12000:]
                merged.pop("delta", None)
            elif "text" not in item and "text" in current:
                merged["text"] = current["text"]
            if current_index is not None:
                values = list(self.activities)
                values[current_index] = merged
                self.activities = deque(values, maxlen=96)
            else:
                if len(self.activities) == self.activities.maxlen:
                    # 待决权限不能被后续输出挤出，否则页面会失去答复入口。
                    evict = next((index for index, value in enumerate(self.activities)
                                  if value.get("stream") != "approval" or value.get("phase") != "started"), None)
                    if evict is not None:
                        del self.activities[evict]
                self.activities.append(merged)
            self._apply_activity_state(merged)
            self.last_activity_at = str(merged.get("timestamp") or self.last_activity_at)

    def _apply_activity_state(self, activity: Mapping[str, Any]) -> None:
        """Reflect the live activity lifecycle in the summary status card."""
        stream = activity.get("stream")
        phase = activity.get("phase")
        event_kind = activity.get("event_kind")
        item_id = str(activity.get("item_id") or "")
        if stream == "event":
            if event_kind == "input_required":
                self.user_input_required = True
                self.active = False
                self.phase = "waiting"
            elif event_kind in {"result", "no_action"}:
                self.user_input_required = False
                self.active = False
                self.phase = "completed"
            elif event_kind == "failed" or phase == "failed":
                self.user_input_required = False
                self.active = False
                self.phase = "error"
            elif event_kind in {"progress", "information"} and phase in {"started", "delta"}:
                self.user_input_required = False
                self.active = True
                self.phase = "working"
            return
        if stream == "approval":
            self.user_input_required = phase == "started"
            self.active = phase != "started"
            self.phase = "waiting" if phase == "started" else "working"
            return
        if stream in {"thinking", "tool", "output"} and phase in {"started", "delta"}:
            waiting = any(item.get("stream") == "approval" and item.get("phase") == "started"
                          for item in self.activities)
            self.user_input_required = waiting
            self.active = not waiting
            self.phase = "waiting" if waiting else "working"
            return
        if stream != "lifecycle" or not item_id.startswith("run-"):
            return
        if phase == "started":
            self.user_input_required = False
            self.active = True
            self.phase = "working"
        elif phase == "failed":
            self.user_input_required = False
            self.active = False
            self.phase = "error"
        elif phase == "completed":
            self.active = False
            self.phase = "waiting" if self.user_input_required else "completed"

    def snapshot(self, runtime: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.update_runtime(runtime)
        context: dict[str, Any] = {}
        provider: dict[str, Any] = {}
        if isinstance(runtime, Mapping):
            # ShadowRuntime reports a zero estimate before its first real
            # provider/context update.  That is an implementation default,
            # not telemetry; do not render it as a fake 0% resource meter.
            used_tokens = runtime.get("context_token_estimate")
            has_context_telemetry = (
                isinstance(used_tokens, (int, float))
                and not isinstance(used_tokens, bool)
                # ShadowRuntime's empty context estimator returns ``1`` as a
                # sentinel before the first real provider/context sample.
                # Treat that value like zero so the product UI cannot show a
                # fabricated ``1 / limit · 0%`` resource meter.
                and used_tokens > 1
            )
            if has_context_telemetry:
                context_values = {
                    "used_tokens": used_tokens,
                    "limit_tokens": runtime.get("context_limit_tokens"),
                    "usage_percent": runtime.get("context_usage_percent"),
                }
                context = {key: value for key, value in context_values.items() if value is not None}
            provider_values = {
                "input_tokens": runtime.get("provider_input_tokens"),
                "cached_tokens": runtime.get("provider_cached_tokens"),
                "cache_hit_ratio": runtime.get("provider_cache_hit_ratio"),
            }
            provider = {key: value for key, value in provider_values.items() if value is not None}
        return {
            "available": self.available,
            "continuous_approval": bool(runtime.get("continuous_approval")) if isinstance(runtime, Mapping) else False,
            "active": self.active,
            "phase": self.phase,
            "user_input_required": self.user_input_required,
            "focus": self.focus,
            "summary": self.summary,
            "latest_event": dict(self.latest_event) if self.latest_event is not None else None,
            "timeline": [dict(item) for item in self.timeline],
            "activities": [dict(item) for item in self.activities],
            "last_activity_at": self.last_activity_at or None,
            "context": context,
            "provider": provider,
        }


def sanitize_runtime_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build the product bootstrap runtime shape from RuntimeState."""

    current = value.get("current") if isinstance(value.get("current"), Mapping) else {}
    recent = value.get("recent_inputs") if isinstance(value.get("recent_inputs"), list) else []
    safe_recent: list[dict[str, Any]] = []
    for item in recent[:10]:
        if not isinstance(item, Mapping):
            continue
        safe_recent.append({
            "turn_id": item.get("turn_id"),
            "transcript": _safe_display_text(item.get("transcript"), limit=4000),
            "response": _safe_display_text(item.get("response"), limit=4000),
            "completed_at": item.get("completed_at"),
            "status": item.get("status"),
        })
    raw_latency = value.get("latency") if isinstance(value.get("latency"), Mapping) else {}
    raw_latency_values = raw_latency.get("values") if isinstance(raw_latency.get("values"), Mapping) else {}
    complete = raw_latency.get("complete") is True
    has_real_latency = any(
        key != "replacement_count"
        and isinstance(candidate, (int, float))
        and not isinstance(candidate, bool)
        and candidate >= 0
        for key, candidate in raw_latency_values.items()
    )
    safe_latency: dict[str, Any] = {}
    if complete or has_real_latency:
        safe_latency = {
            "complete": complete,
            "values": {
                str(key): candidate
                for key, candidate in raw_latency_values.items()
                if candidate is None or (
                    isinstance(candidate, (int, float))
                    and not isinstance(candidate, bool)
                    and candidate >= 0
                )
            },
        }
    raw_speaker = current.get("speaker_decision") if isinstance(current.get("speaker_decision"), Mapping) else {}
    safe_speaker = {
        key: raw_speaker.get(key)
        for key in (
            "turn_id", "generation", "user_id", "state", "similarity",
            "threshold_used", "speech_ms", "observation_count",
            "continuity_source", "route", "reason", "degraded", "anti_spoofing",
        )
        if key in raw_speaker
    }
    # The bootstrap rail renders the same server-ordered conversation the live
    # session publishes, so an offline page shows the identical history.
    raw_conversation = value.get("conversation") if isinstance(value.get("conversation"), Mapping) else {}
    safe_entries: list[dict[str, Any]] = []
    for item in raw_conversation.get("entries") or []:
        if not isinstance(item, Mapping):
            continue
        delivery = item.get("delivery") if isinstance(item.get("delivery"), Mapping) else {}
        safe_entries.append({
            "seq": item.get("seq"),
            "kind": item.get("kind"),
            "conversation_id": item.get("conversation_id"),
            "turn_id": item.get("turn_id"),
            "speech_id": item.get("speech_id"),
            "text": _safe_display_text(item.get("text"), limit=4000),
            "status": item.get("status"),
            "termination": item.get("termination"),
            "delivery": {str(key): candidate for key, candidate in delivery.items()},
            "at": item.get("at"),
        })
    return {
        "state": value.get("state"),
        "listening": bool(value.get("listening")),
        "can_start": bool(value.get("can_start")),
        "wake_always_on": bool(value.get("wake_always_on")),
        "input_owner": value.get("input_owner"),
        "reasoning_mode": value.get("reasoning_mode", "deep"),  # reasoning_mode（运行模式）：缺省为深度。
        "quiet_hours": {key: bool((value.get("quiet_hours") or {}).get(key))
                        for key in ("passive_quiet", "user_interaction", "within_quiet_hours")},
        "services": dict(value.get("services") or {}),
        "latency": safe_latency,
        "conversation": {
            "recent": safe_recent,
            "entries": safe_entries,
            "latest_seq": raw_conversation.get("latest_seq") or 0,
            "current": {
                "partial_text": _safe_display_text(current.get("partial_text"), limit=4000),
                "final_text": _safe_display_text(current.get("final_text"), limit=4000),
                "assistant_text": _safe_display_text(current.get("assistant_text"), limit=4000),
                "tts_text": _safe_display_text(current.get("tts_text"), limit=4000),
                "tts_status": _safe_display_text(current.get("tts_status"), limit=120),
                "speaker_decision": safe_speaker,
            },
        },
    }
    raw_local_tts = value.get("local_tts") if isinstance(value.get("local_tts"), Mapping) else None
    if raw_local_tts is not None:
        local_model = raw_local_tts.get("local_model")
        loaded_model = raw_local_tts.get("loaded_model")
        models = raw_local_tts.get("models") if isinstance(raw_local_tts.get("models"), list) else []
        active_voices = (raw_local_tts.get("active_voice_by_model")
                         if isinstance(raw_local_tts.get("active_voice_by_model"), Mapping) else {})
        voice_state = raw_local_tts.get("voice_state")
        result["local_tts"] = {
            "backend": raw_local_tts.get("backend") if isinstance(raw_local_tts.get("backend"), str) and raw_local_tts.get("backend") in {"minimax", "voxcpm1"} else "minimax",
            "local_model": local_model if isinstance(local_model, str) and local_model in {"voxcpm1", "voxcpm2"} else "voxcpm1",
            "loaded_model": loaded_model if isinstance(loaded_model, str) and loaded_model in {"voxcpm1", "voxcpm2"} else None,
            "load_state": _safe_display_text(raw_local_tts.get("load_state"), limit=32),
            "pending_selection": ({"backend": pending.get("backend"), "local_model": pending.get("local_model")}
                                  if isinstance(pending := raw_local_tts.get("pending_selection"), Mapping)
                                  and pending.get("backend") in {"minimax", "voxcpm1"}
                                  and pending.get("local_model") in {"voxcpm1", "voxcpm2"} else None),
            "error": (safe_voice_diagnostic(raw_local_tts.get("error"), "")
                      if raw_local_tts.get("error") else None),
            "active_voice": ({
                "voice_id": raw_local_tts.get("active_voice", {}).get("voice_id"),
                "revision": raw_local_tts.get("active_voice", {}).get("revision"),
                "name": _safe_display_text(raw_local_tts.get("active_voice", {}).get("name"), limit=80),
            } if isinstance(raw_local_tts.get("active_voice"), Mapping) else None),
            "voice_state": voice_state if isinstance(voice_state, str) and voice_state in {
                "ready", "unconfigured", "asset_missing", "reference_text_missing", "invalid",
            } else "unconfigured",
            "voice_error": (safe_voice_diagnostic(raw_local_tts.get("voice_error"), "")
                            if raw_local_tts.get("voice_error") else None),
            "active_voice_by_model": {
                model: ({"voice_id": item.get("voice_id"), "revision": item.get("revision")}
                        if isinstance(item, Mapping) and isinstance(item.get("voice_id"), str)
                        and type(item.get("revision")) is int and item.get("revision") > 0 else None)
                for model, item in active_voices.items()
                if model in {"voxcpm1", "voxcpm2"}
            },
            "voice_capabilities": {
                key: raw_local_tts.get("voice_capabilities", {}).get(key) is True
                for key in ("voice_clone", "voice_design", "style_control", "emotion_control",
                            "requires_reference_text")
            } if isinstance(raw_local_tts.get("voice_capabilities"), Mapping) else {},
            "emotion_diagnostic": _safe_display_text(raw_local_tts.get("emotion_diagnostic"), limit=240),
            "operation": ({
                "operation_id": raw_local_tts.get("operation", {}).get("operation_id"),
                "kind": raw_local_tts.get("operation", {}).get("kind"),
                "state": raw_local_tts.get("operation", {}).get("state"),
                "recorded_ms": raw_local_tts.get("operation", {}).get("recorded_ms"),
            } if isinstance(raw_local_tts.get("operation"), Mapping) else None),
            "models": [
                {
                    "id": item.get("id") if isinstance(item.get("id"), str) and item.get("id") in {"voxcpm1", "voxcpm2"} else "voxcpm1",
                    "name": _safe_display_text(item.get("name"), limit=80),
                    "native_sample_rate": item.get("native_sample_rate")
                    if isinstance(item.get("native_sample_rate"), int) and not isinstance(item.get("native_sample_rate"), bool)
                    else 16000,
                    "available": item.get("available") is True,
                    "loaded": item.get("loaded") is True,
                }
                for item in models if isinstance(item, Mapping)
            ],
        }
    return result
