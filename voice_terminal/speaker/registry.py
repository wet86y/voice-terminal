from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from ..core.events import utc_now_iso

try:  # numpy is already a runtime dependency of the audio stack.
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - optional sidecar remains unavailable
    np = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SpeakerTemplate:
    template_id: str
    label: str
    quality: dict[str, Any]
    created_at: str
    include_in_centroid: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "label": self.label,
            "quality": dict(self.quality),
            "created_at": self.created_at,
            "include_in_centroid": self.include_in_centroid,
        }


class SpeakerProfileStore:
    """Local, atomic single-user profile store; raw enrollment audio is never saved."""

    SCHEMA_VERSION = 1

    def __init__(self, root: Path, *, model_id: str = "wespeaker-cnceleb-resnet34") -> None:
        self.root = root.resolve()
        self.path = self.root / "registry.json"
        self.templates_dir = self.root / "templates"
        self.model_id = model_id
        self.model_version = model_id
        self.user_id = "owner"
        self.user_name = "主人"
        self.templates: dict[str, SpeakerTemplate] = {}
        self.centroid: tuple[float, ...] = ()
        self.vector_dim = 0
        self.warning: str | None = None
        self.load()

    @property
    def available(self) -> bool:
        # Metadata alone is not a usable profile: at least one referenced
        # vector must still be readable and compatible with the stored
        # dimension.  This also makes a partially deleted/corrupt template
        # fail open as "unregistered" instead of advertising a stale centre.
        if not self.centroid or not self.templates:
            return False
        if np is not None:
            try:
                centre = np.asarray(self.centroid, dtype=np.float32)
                if not centre.size or not np.isfinite(centre).all() or float(np.linalg.norm(centre)) <= 1e-8:
                    return False
            except Exception:
                return False
        return bool(self.vectors(include_centroid=False))

    def load(self) -> None:
        self.templates = {}
        self.centroid = ()
        self.vector_dim = 0
        self.warning = None
        self.user_id = "owner"
        self.user_name = "主人"
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("schema_version") != self.SCHEMA_VERSION:
                raise ValueError("speaker profile schema mismatch")
            stored_model_id = str(raw.get("model_id") or self.model_id)
            if stored_model_id != self.model_id:
                raise ValueError("speaker profile model mismatch")
            user = raw.get("user") if isinstance(raw.get("user"), Mapping) else {}
            stored_user_id = str(user.get("user_id") or "owner")
            if stored_user_id != "owner":
                raise ValueError("speaker profile only supports the fixed owner user")
            self.user_id = "owner"
            self.user_name = str(user.get("display_name") or "主人")[:80]
            for item in raw.get("templates", []):
                if not isinstance(item, Mapping):
                    continue
                template_id = str(item.get("template_id") or "")
                if not self._valid_template_id(template_id):
                    continue
                vector_path = self.templates_dir / f"{template_id}.npy"
                if not template_id or not vector_path.is_file():
                    continue
                template = SpeakerTemplate(
                    template_id=template_id,
                    label=str(item.get("label") or "自定义")[:80],
                    quality=dict(item.get("quality") or {}) if isinstance(item.get("quality"), Mapping) else {},
                    created_at=str(item.get("created_at") or ""),
                    include_in_centroid=item.get("include_in_centroid") is not False,
                )
                self.templates[template_id] = template
            raw_dim = raw.get("vector_dim")
            if isinstance(raw_dim, int) and raw_dim > 0:
                self.vector_dim = raw_dim
            # The centre is derived from the template files every time.  A
            # crash between vector/registry replacement must never leave a
            # stale centre being used for a later identity decision.
            self.rebuild_centroid(persist=False)
        except Exception as exc:
            self.warning = f"声纹资料无法读取，已按未注册处理：{type(exc).__name__}"
            self.templates = {}
            self.centroid = ()

    @staticmethod
    def _valid_template_id(value: str) -> bool:
        # IDs are generated UUID4 hex strings.  Reject path separators and
        # arbitrary registry input before it is ever joined to templates_dir.
        return len(value) == 32 and all(char in "0123456789abcdef" for char in value)

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "schema_version": self.SCHEMA_VERSION,
            "user": {"user_id": self.user_id, "display_name": self.user_name},
            "model_id": self.model_id,
            "model_version": self.model_version,
            "vector_dim": self.vector_dim,
            "template_count": len(self.templates),
            "templates": [item.to_payload() for item in self.templates.values()],
            "warning": self.warning,
        }

    def vectors(self, *, include_centroid: bool = True) -> list[tuple[str, tuple[float, ...]]]:
        result: list[tuple[str, tuple[float, ...]]] = []
        if include_centroid and self.centroid:
            result.append(("centroid", self.centroid))
        if np is None:
            return result
        for template_id, template in self.templates.items():
            try:
                vector = np.load(self.templates_dir / f"{template_id}.npy", allow_pickle=False)
                flat = tuple(float(x) for x in vector.reshape(-1))
                if (flat and all(np.isfinite(item) for item in flat)
                        and float(np.linalg.norm(np.asarray(flat, dtype=np.float32))) > 1e-8
                        and (not self.vector_dim or len(flat) == self.vector_dim)):
                    result.append((template_id, flat))
            except Exception:
                continue
        return result

    @staticmethod
    def _normalize(vector: Any) -> Any:
        if np is None:
            return vector
        value = np.asarray(vector, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(value))
        return value / norm if norm > 1e-8 else value

    @classmethod
    def _validated_normalize(cls, vector: Any) -> Any:
        """Normalize one embedding and reject empty, non-finite, or zero data.

        A zero vector is technically non-empty and JSON/NumPy can persist it,
        but it can never participate in cosine matching.  Rejecting it here
        keeps a successful enrollment from advertising an unusable profile.
        """

        if np is None:
            raise RuntimeError("numpy unavailable")
        normalized = cls._normalize(vector)
        value = np.asarray(normalized, dtype=np.float32).reshape(-1)
        if not value.size or not np.isfinite(value).all():
            raise ValueError("声纹向量为空或包含无效数值")
        if float(np.linalg.norm(value)) <= 1e-8:
            raise ValueError("声纹向量不能是零向量")
        return value

    def _write_json(self, payload: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".speaker-registry-", suffix=".json", dir=str(self.root))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "user": {"user_id": self.user_id, "display_name": self.user_name},
            "model_id": self.model_id,
            "model_version": self.model_version,
            "vector_dim": self.vector_dim,
            "centroid": list(self.centroid),
            "templates": [item.to_payload() for item in self.templates.values()],
        }

    def _write_vector(self, template_id: str, vector: Any) -> None:
        if np is None:
            raise RuntimeError("numpy unavailable")
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{template_id}-", suffix=".npy", dir=str(self.templates_dir))
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            np.save(temporary, self._normalize(vector), allow_pickle=False)
            os.replace(temporary, self.templates_dir / f"{template_id}.npy")
        finally:
            temporary.unlink(missing_ok=True)

    def rebuild_centroid(self, *, persist: bool = True) -> tuple[float, ...]:
        if np is None:
            self.centroid = ()
        else:
            vectors = []
            expected_dim = self.vector_dim or None
            for template_id, template in self.templates.items():
                if not template.include_in_centroid:
                    continue
                try:
                    vector = np.asarray(
                        np.load(self.templates_dir / f"{template_id}.npy", allow_pickle=False),
                        dtype=np.float32,
                    ).reshape(-1)
                    if not vector.size:
                        continue
                    if not np.isfinite(vector).all() or float(np.linalg.norm(vector)) <= 1e-8:
                        continue
                    if expected_dim is None:
                        expected_dim = int(vector.size)
                    if int(vector.size) != expected_dim:
                        continue
                    vectors.append(vector)
                except Exception:
                    continue
            if vectors:
                mean = np.mean(np.stack(vectors), axis=0).astype(np.float32)
                norm = float(np.linalg.norm(mean))
                if norm <= 1e-8 or not np.isfinite(norm):
                    self.centroid = ()
                    self.vector_dim = 0
                else:
                    self.centroid = tuple(float(x) for x in (mean / norm))
                    self.vector_dim = len(self.centroid)
            else:
                self.centroid = ()
                self.vector_dim = 0
        if persist:
            self._write_json(self._payload())
        return self.centroid

    def replace_templates(self, samples: list[tuple[str, Any, Mapping[str, Any]]], *, display_name: str = "主人") -> dict[str, Any]:
        if not samples:
            raise ValueError("至少需要一段有效声纹样本")
        if len(samples) > 8:
            raise ValueError("单用户声纹模板最多 8 组")
        old_ids = set(self.templates)
        old_templates = self.templates
        old_centroid = self.centroid
        old_vector_dim = self.vector_dim
        old_user_name = self.user_name
        next_templates: dict[str, SpeakerTemplate] = {}
        self.user_name = str(display_name or "主人")[:80]
        written: set[str] = set()
        new_dimension: int | None = None
        try:
            for label, vector, quality in samples:
                template_id = uuid4().hex
                normalized = self._validated_normalize(vector)
                dimension = int(np.asarray(normalized).reshape(-1).size)
                if new_dimension is None:
                    new_dimension = dimension
                if dimension != new_dimension or (self.vector_dim and dimension != self.vector_dim):
                    raise ValueError("声纹向量维度与现有资料不兼容")
                self._write_vector(template_id, normalized)
                written.add(template_id)
                next_templates[template_id] = SpeakerTemplate(
                    template_id=template_id,
                    label=str(label or "自定义")[:80],
                    quality=dict(quality),
                    created_at=utc_now_iso(),
                )
            self.templates = next_templates
            self.vector_dim = int(new_dimension or 0)
            self.rebuild_centroid(persist=False)
            if not self.centroid:
                raise ValueError("无法从这些模板重建主声纹中心")
            self._write_json(self._payload())
            self.warning = None
        except Exception:
            self.templates = old_templates
            self.centroid = old_centroid
            self.vector_dim = old_vector_dim
            self.user_name = old_user_name
            for template_id in written:
                (self.templates_dir / f"{template_id}.npy").unlink(missing_ok=True)
            raise
        for stale in old_ids - set(next_templates):
            try:
                (self.templates_dir / f"{stale}.npy").unlink(missing_ok=True)
            except OSError:
                # The new registry is already committed; an orphaned old
                # vector is harmless because it is not referenced by JSON.
                pass
        return self.snapshot()

    def add_template(self, label: str, vector: Any, quality: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if len(self.templates) >= 8:
            raise ValueError("单用户声纹模板最多 8 组")
        template_id = uuid4().hex
        old_templates = self.templates
        old_centroid = self.centroid
        old_vector_dim = self.vector_dim
        old_warning = self.warning
        normalized = self._validated_normalize(vector)
        dimension = int(np.asarray(normalized).reshape(-1).size)
        if not dimension or (self.vector_dim and dimension != self.vector_dim):
            raise ValueError("声纹向量维度与现有资料不兼容")
        if not np.isfinite(np.asarray(normalized, dtype=np.float32)).all():
            raise ValueError("声纹向量包含无效数值")
        self._write_vector(template_id, normalized)
        try:
            self.templates = dict(self.templates)
            self.templates[template_id] = SpeakerTemplate(
                template_id=template_id, label=str(label or "自定义")[:80],
                quality=dict(quality or {}), created_at=utc_now_iso(),
            )
            self.vector_dim = dimension
            self.rebuild_centroid(persist=False)
            if not self.centroid:
                raise ValueError("无法重建主声纹中心")
            self._write_json(self._payload())
            self.warning = None
        except Exception:
            self.templates = old_templates
            self.centroid = old_centroid
            self.vector_dim = old_vector_dim
            self.warning = old_warning
            (self.templates_dir / f"{template_id}.npy").unlink(missing_ok=True)
            raise
        return self.snapshot()

    def delete_template(self, template_id: str) -> dict[str, Any]:
        if template_id not in self.templates:
            raise ValueError("声纹模板不存在")
        old_templates = self.templates
        old_centroid = self.centroid
        old_vector_dim = self.vector_dim
        vector_path = self.templates_dir / f"{template_id}.npy"
        backup_path: Path | None = None
        try:
            # Move the vector aside while the new registry is committed.  A
            # failed JSON write can therefore restore both halves of the
            # profile without exposing a missing referenced file.
            if vector_path.is_file():
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{template_id}-delete-", suffix=".npy", dir=str(self.templates_dir),
                )
                os.close(descriptor)
                backup_path = Path(temporary_name)
                os.replace(vector_path, backup_path)
            self.templates = dict(self.templates)
            self.templates.pop(template_id)
            self.rebuild_centroid(persist=False)
            self._write_json(self._payload())
            if backup_path is not None:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.warning = None
            return self.snapshot()
        except Exception:
            self.templates = old_templates
            self.centroid = old_centroid
            self.vector_dim = old_vector_dim
            if backup_path is not None and backup_path.exists() and not vector_path.exists():
                os.replace(backup_path, vector_path)
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            raise

    def set_template_centroid(self, template_id: str, include: bool) -> dict[str, Any]:
        """Toggle whether one retained template contributes to the centre."""

        template = self.templates.get(template_id)
        if template is None:
            raise ValueError("声纹模板不存在")
        if template.include_in_centroid is bool(include):
            return self.snapshot()
        old_templates = self.templates
        old_centroid = self.centroid
        old_vector_dim = self.vector_dim
        self.templates = dict(self.templates)
        self.templates[template_id] = SpeakerTemplate(
            template_id=template.template_id,
            label=template.label,
            quality=dict(template.quality),
            created_at=template.created_at,
            include_in_centroid=bool(include),
        )
        try:
            if not any(item.include_in_centroid for item in self.templates.values()):
                raise ValueError("至少需要一个模板纳入主声纹中心")
            self.rebuild_centroid(persist=False)
            if not self.centroid:
                raise ValueError("无法重建主声纹中心")
            self._write_json(self._payload())
            self.warning = None
            return self.snapshot()
        except Exception:
            self.templates = old_templates
            self.centroid = old_centroid
            self.vector_dim = old_vector_dim
            raise
