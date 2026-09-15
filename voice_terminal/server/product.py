"""Product-facing settings and safe projections.

The voice runtime deliberately owns audio, scheduling, provider and playback
lifecycles.  This module is the small sidecar boundary used by the product Web
UI: it stores non-secret presentation settings and projects only user-readable
Shadow state.  Nothing in here is allowed to expose provider credentials or
the internal Agent/Shadow trace.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from ..config import AppConfig
from ..core.events import utc_now_iso
from ..shadow_agent import ShadowEvent


PRODUCT_SETTINGS_SCHEMA_VERSION = 1
MIN_SILENCE_SECONDS = 5
MAX_SILENCE_SECONDS = 90
MAX_PERSONA_DOCUMENT_BYTES = 64 * 1024
MIN_KWS_SENSITIVITY = 1
MAX_KWS_SENSITIVITY = 99

PERSONA_FILES = {
    "instructions": "instructions.md",
    "personality": "personality.md",
    "user": "user.md",
}
SHADOW_PERSONA_FILE = "instructions.md"


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
    return {
        "schema_version": PRODUCT_SETTINGS_SCHEMA_VERSION,
        "wake": {
            "enabled": bool(config.wake.enabled),
            "keyword": str(config.wake.keyword),
            "keyword_label": str(config.wake.keyword_label),
            "sensitivity": _threshold_to_sensitivity(config.wake.threshold),
        },
        "silence": {
            "seconds": max(MIN_SILENCE_SECONDS, min(MAX_SILENCE_SECONDS, round(final_silence_ms / 1000))),
            "smart_turn_hard_silence_ms": int(getattr(config.smart_turn, "hard_silence_ms", 1400)),
        },
    }


def _merge_settings(defaults: dict[str, Any], value: Any) -> dict[str, Any]:
    result = deepcopy(defaults)
    if not isinstance(value, Mapping):
        return result
    for section in ("wake", "silence"):
        raw = value.get(section)
        if isinstance(raw, Mapping):
            result[section].update({key: raw[key] for key in result[section] if key in raw})
    # Product settings are intentionally versioned independently from the
    # model registry.  Unknown fields are discarded rather than echoed to the
    # browser, which keeps this file a narrow, non-secret state boundary.
    result["schema_version"] = PRODUCT_SETTINGS_SCHEMA_VERSION
    wake = result["wake"]
    wake["enabled"] = wake["enabled"] if type(wake.get("enabled")) is bool else defaults["wake"]["enabled"]
    wake["keyword"] = wake["keyword"] if isinstance(wake.get("keyword"), str) and wake["keyword"].strip() else defaults["wake"]["keyword"]
    wake["keyword_label"] = wake["keyword_label"] if isinstance(wake.get("keyword_label"), str) else defaults["wake"]["keyword_label"]
    try:
        sensitivity = int(wake.get("sensitivity"))
    except (TypeError, ValueError):
        sensitivity = int(defaults["wake"]["sensitivity"])
    wake["sensitivity"] = max(MIN_KWS_SENSITIVITY, min(MAX_KWS_SENSITIVITY, sensitivity))
    silence = result["silence"]
    try:
        seconds = int(silence.get("seconds"))
    except (TypeError, ValueError):
        seconds = int(defaults["silence"]["seconds"])
    silence["seconds"] = max(MIN_SILENCE_SECONDS, min(MAX_SILENCE_SECONDS, seconds))
    silence["smart_turn_hard_silence_ms"] = int(defaults["silence"]["smart_turn_hard_silence_ms"])
    return result


def _settings_validation_warning(value: Mapping[str, Any]) -> str | None:
    """Report malformed persisted fields without exposing their contents."""

    invalid = False
    schema_version = value.get("schema_version")
    if schema_version is not None and schema_version != PRODUCT_SETTINGS_SCHEMA_VERSION:
        invalid = True
    for section_name, fields in (
        ("wake", ("enabled", "keyword", "keyword_label", "sensitivity")),
        ("silence", ("seconds",)),
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
            if section_name == "wake" and field == "enabled":
                invalid = invalid or type(candidate) is not bool
            elif section_name == "wake" and field == "keyword":
                invalid = invalid or not isinstance(candidate, str) or not candidate.strip()
            elif section_name == "wake" and field == "keyword_label":
                invalid = invalid or not isinstance(candidate, str)
            elif section_name == "wake" and field == "sensitivity":
                invalid = invalid or type(candidate) is not int or not MIN_KWS_SENSITIVITY <= candidate <= MAX_KWS_SENSITIVITY
            elif section_name == "silence" and field == "seconds":
                invalid = invalid or type(candidate) is not int or not MIN_SILENCE_SECONDS <= candidate <= MAX_SILENCE_SECONDS
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

    wake = settings.get("wake") if isinstance(settings.get("wake"), Mapping) else {}
    silence = settings.get("silence") if isinstance(settings.get("silence"), Mapping) else {}
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
    next_wake = replace(
        config.wake,
        enabled=enabled,
        # KWS has one product mode: enabled means the microphone stays in
        # wake-word standby until the user stops the session.  Keep the
        # legacy dataclass field synchronized for older controller fixtures,
        # but do not expose or read a second UI setting.
        always_on=enabled,
        keyword=str(wake.get("keyword") or config.wake.keyword),
        keyword_label=str(wake.get("keyword_label") or config.wake.keyword_label),
        threshold=_sensitivity_to_threshold(sensitivity),
    )
    next_agent = replace(
        config.agent,
        final_silence_ms=seconds * 1000,
    )
    return replace(config, wake=next_wake, agent=next_agent)


class ProductSettingsStore:
    """Atomic, non-secret product settings plus allowlisted Persona writes."""

    def __init__(self, *, path: Path, config: AppConfig) -> None:
        self.path = path
        self.config = config
        self.warnings: list[str] = []
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
        return _merge_settings(defaults, value)

    def snapshot(self) -> dict[str, Any]:
        value = deepcopy(self._settings)
        value["persona"] = self.read_persona()
        value["warnings"] = list(self.warnings)
        return value

    def settings_only(self) -> dict[str, Any]:
        return deepcopy(self._settings)

    def read_persona(self) -> dict[str, str]:
        directory = (self.path.resolve().parent / "persona").resolve()
        result: dict[str, str] = {}
        for key, filename in PERSONA_FILES.items():
            path = (directory / filename).resolve()
            try:
                path.relative_to(directory)
                result[key] = path.read_text(encoding="utf-8") if path.is_file() else ""
            except (OSError, UnicodeError, ValueError):
                result[key] = ""
        shadow_path = (directory.parent / "shadow_agent" / SHADOW_PERSONA_FILE).resolve()
        try:
            shadow_path.relative_to(directory.parent.resolve())
            result["shadow"] = shadow_path.read_text(encoding="utf-8") if shadow_path.is_file() else ""
        except (OSError, UnicodeError, ValueError):
            result["shadow"] = ""
        return result

    def effective_config(self) -> AppConfig:
        return apply_settings_to_config(self.config, self._settings)

    def _save_settings(self) -> None:
        _atomic_write_json(self.path, self._settings)
        # A successful explicit save repairs a previously unreadable settings
        # artifact.  Keep the warning until that point, but do not continue
        # advertising a stale recovery warning after the file is valid.
        self.warnings.clear()

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
        if type(payload.get("enabled")) is not bool:
            raise ProductSettingsError("enabled 必须是布尔值。")
        keyword = _clean_text(payload.get("keyword"), "唤醒词", max_length=120)
        raw_sensitivity = payload.get("sensitivity")
        if type(raw_sensitivity) is not int:
            raise ProductSettingsError("sensitivity 必须是整数。")
        sensitivity = raw_sensitivity
        if not MIN_KWS_SENSITIVITY <= sensitivity <= MAX_KWS_SENSITIVITY:
            raise ProductSettingsError(f"sensitivity 必须在 {MIN_KWS_SENSITIVITY} 至 {MAX_KWS_SENSITIVITY} 之间。")
        from ..kws.keywords import keyword_label, normalize_keyword

        try:
            normalized = normalize_keyword(keyword)
            label = keyword_label(normalized)
        except (TypeError, ValueError) as exc:
            raise ProductSettingsError(f"唤醒词无法生成有效标签：{str(exc)[:200]}") from exc
        previous = self._settings["wake"]
        changed_keyword = (
            normalized != str(previous.get("keyword") or "")
            or label.upper() != str(previous.get("keyword_label") or "").upper()
        )
        if changed_keyword:
            await asyncio.to_thread(self._compile_keyword, normalized, label)
        self._settings["wake"] = {
            "enabled": payload["enabled"],
            "keyword": normalized,
            "keyword_label": label,
            "sensitivity": sensitivity,
        }
        self._save_settings()
        return self.snapshot()

    def _compile_keyword(self, keyword: str, label: str) -> None:
        """Compile a KWS keyword and atomically replace both keyword files."""

        from ..kws.detector import discover_model_paths
        from ..kws.keywords import generate_keywords, validate_keywords_file

        model_dir = self.config.kws_dir.resolve()
        target = self.config.kws_keywords_file.resolve()
        try:
            target.relative_to(model_dir)
        except ValueError as exc:
            raise ProductSettingsError("KWS 关键词文件必须位于受管 KWS 模型目录内。") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        raw_target = target.parent / "keywords_raw.txt"
        with tempfile.TemporaryDirectory(prefix=".product-kws-", dir=str(target.parent)) as temporary_name:
            temporary = Path(temporary_name)
            raw_path = temporary / "keywords_raw.txt"
            compiled_path = temporary / "keywords.txt"
            try:
                generate_keywords(
                    model_dir=model_dir,
                    raw_path=raw_path,
                    output_path=compiled_path,
                    keyword=keyword,
                    label=label,
                )
                validate_keywords_file(compiled_path, label)
                # Force path discovery/label validation once more before any
                # live worker can observe the new files.
                discover_model_paths(model_dir, compiled_path, label)
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

    async def save_silence(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw_seconds = payload.get("seconds")
        if type(raw_seconds) is not int:
            raise ProductSettingsError("seconds 必须是整数。")
        seconds = raw_seconds
        if not MIN_SILENCE_SECONDS <= seconds <= MAX_SILENCE_SECONDS:
            raise ProductSettingsError(f"seconds 必须在 {MIN_SILENCE_SECONDS} 至 {MAX_SILENCE_SECONDS} 之间。")
        self._settings["silence"] = {
            "seconds": seconds,
            "smart_turn_hard_silence_ms": int(getattr(self.config.smart_turn, "hard_silence_ms", 1400)),
        }
        self._save_settings()
        return self.snapshot()

    def save_persona(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != set(PERSONA_FILES) | {"shadow"}:
            raise ProductSettingsError("Persona 文档字段必须是 instructions、personality、user、shadow。")
        values: dict[str, str] = {}
        for key in (*PERSONA_FILES, "shadow"):
            value = payload.get(key)
            if not isinstance(value, str) or "\x00" in value:
                raise ProductSettingsError(f"Persona 文档 {key} 内容无效。")
            if len(value.encode("utf-8")) > MAX_PERSONA_DOCUMENT_BYTES:
                raise ProductSettingsError(f"Persona 文档 {key} 不得超过 64 KB。")
            values[key] = value

        # Product writes are deliberately pinned to the repository config
        # layout.  The client never supplies a path, and a YAML override must
        # not turn this API into an arbitrary filesystem writer.
        persona_dir = (self.path.resolve().parent / "persona").resolve()
        shadow_dir = (persona_dir.parent / "shadow_agent").resolve()
        paths = {key: persona_dir / filename for key, filename in PERSONA_FILES.items()}
        paths["shadow"] = shadow_dir / SHADOW_PERSONA_FILE
        for path in paths.values():
            try:
                path.resolve().relative_to(persona_dir if path.parent == persona_dir else shadow_dir)
            except ValueError as exc:
                raise ProductSettingsError("Persona 路径不在允许目录内。") from exc

        self._atomic_replace_documents({path: values[key] for key, path in paths.items()})
        return self.snapshot()


class ShadowProductProjection:
    """Bounded, user-readable projection of Shadow status/progress events."""

    _KIND_LABELS = {
        "status": "状态汇报",
        "progress": "后台处理中",
        "observation": "发布观察",
        "user_input_required": "等待你的输入",
        "result": "完成",
        "failed": "需要注意",
    }

    def __init__(self) -> None:
        self.available = False
        self.active = False
        self.user_input_required = False
        self.phase = "unavailable"
        self.focus = ""
        self.summary = ""
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
        self.timeline.clear()
        self.activities.clear()
        self._activity_run_seq = None
        self.last_activity_at = ""

    def update_runtime(self, value: Mapping[str, Any] | None) -> None:
        if not isinstance(value, Mapping):
            return
        self.available = bool(value.get("available", self.available))
        self.active = bool(value.get("active", self.active))
        self.user_input_required = bool(value.get("user_input_required", self.user_input_required))
        status = _safe_display_text(value.get("status"), limit=320)
        if status:
            self.summary = status
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
            kind = str(item.kind)
            if kind not in self._KIND_LABELS:
                continue
            text = _safe_display_text(item.text, limit=320)
            if kind in {"status", "progress", "observation"} and text:
                self.focus = text
                self.summary = text
            if kind == "user_input_required":
                self.user_input_required = True
                self.active = False
                self.phase = "waiting"
            elif kind == "result":
                self.user_input_required = False
                self.active = False
                self.phase = "completed"
            elif kind == "failed":
                self.user_input_required = False
                self.active = False
                self.phase = "error"
            elif kind in {"status", "progress", "observation"}:
                self.active = True
                self.phase = "working"
            self.timeline.append({
                "state": "active" if self.active and kind in {"status", "progress", "observation"} else "done",
                "label": self._KIND_LABELS[kind],
                "text": text,
                "timestamp": getattr(item, "timestamp", "") or utc_now_iso(),
            })

    def update_activities(self, activities: list[Mapping[str, Any]]) -> None:
        """Merge ordered UI-only activities without changing Shadow state."""
        valid_streams = {"lifecycle", "thinking", "tool", "event"}
        valid_phases = {"started", "delta", "completed", "failed"}
        valid_event_kinds = {"progress", "observation", "user_input_required", "result", "failed"}
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
            timestamp = raw.get("timestamp")
            item: dict[str, Any] = {
                "run_seq": run_seq,
                "item_id": item_id[:160],
                "stream": stream,
                "phase": phase,
                "timestamp": timestamp if isinstance(timestamp, str) and timestamp else utc_now_iso(),
            }
            for key, limit in (("text", 4000), ("delta", 2000), ("tool_name", 160), ("tool_status", 120)):
                value = raw.get(key)
                if isinstance(value, str):
                    item[key] = _safe_display_text(value, limit=limit) if key != "delta" else value[:limit]
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
                    merged["text"] = f"{current.get('text', '')}{delta}"[:4000]
                merged.pop("delta", None)
            elif "text" not in item and "text" in current:
                merged["text"] = current["text"]
            if current_index is not None:
                values = list(self.activities)
                values[current_index] = merged
                self.activities = deque(values, maxlen=96)
            else:
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
            if event_kind == "user_input_required":
                self.user_input_required = True
                self.active = False
                self.phase = "waiting"
            elif event_kind == "result":
                self.user_input_required = False
                self.active = False
                self.phase = "completed"
            elif event_kind == "failed" or phase == "failed":
                self.user_input_required = False
                self.active = False
                self.phase = "error"
            elif event_kind in {"progress", "observation"} and phase in {"started", "delta"}:
                self.user_input_required = False
                self.active = True
                self.phase = "working"
            return
        if stream in {"thinking", "tool"} and phase in {"started", "delta"}:
            self.user_input_required = False
            self.active = True
            self.phase = "working"
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
            "active": self.active,
            "phase": self.phase,
            "user_input_required": self.user_input_required,
            "focus": self.focus,
            "summary": self.summary,
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
    return {
        "state": value.get("state"),
        "listening": bool(value.get("listening")),
        "can_start": bool(value.get("can_start")),
        "wake_always_on": bool(value.get("wake_always_on")),
        "input_owner": value.get("input_owner"),
        "reasoning_mode": value.get("reasoning_mode", "balanced"),
        "services": dict(value.get("services") or {}),
        "latency": safe_latency,
        "conversation": {
            "recent": safe_recent,
            "current": {
                "partial_text": _safe_display_text(current.get("partial_text"), limit=4000),
                "final_text": _safe_display_text(current.get("final_text"), limit=4000),
                "assistant_text": _safe_display_text(current.get("assistant_text"), limit=4000),
                "tts_text": _safe_display_text(current.get("tts_text"), limit=4000),
                "tts_status": _safe_display_text(current.get("tts_status"), limit=120),
            },
        },
    }
