"""兼容性组件的业务资料处理器：修复、归档故障来源、恢复当前默认值。

仅由持有工作区互斥锁的安装器调用；不启动业务、不恢复归档资料。
设置沿用正式程序的合并和校验入口，避免维护另一套字段定义。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import threading
import tempfile
import wave
from uuid import uuid4

from .local_files import reparse
from .paths import atomic_json


def record_settings_validation(path: Path, schema: int) -> None:
    """记录权威设置入口刚完成校验的文件身份；外部修改不能沿用成功结论。"""
    if path.name != "product-settings.json" or path.parent.name != "config":
        return
    data = path.parent.parent
    receipt = _safe(data, "state/settings-validation.json")
    atomic_json(receipt, {"schema": 1, "settings_schema": schema,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "fields": {"settings_schema": "权威入口校验过的设置格式", "sha256": "实际落盘配置内容摘要"}})


def _safe(root: Path, relative: str) -> Path:
    """逐级核对本机数据路径，归档不穿透链接。"""
    parts = Path(relative)
    if parts.is_absolute() or ".." in parts.parts or ":" in relative:
        raise ValueError("兼容处理路径越出数据目录")
    current = root
    for part in ("", *parts.parts):
        current = current / part
        if os.path.lexists(current) and reparse(current):
            raise ValueError("兼容处理路径不能包含目录联接或链接")
    return current


class Archive:
    """Archive（故障归档）：同卷移走并逐项记账，中断后不会重新参与启动。"""

    def __init__(self, data: Path):
        self.data = data
        self.relative = f"archives/compatibility/{uuid4().hex}"
        self.rows: list[dict] = []

    def move(self, relative: str, reason: str) -> bool:
        source = _safe(self.data, relative)
        if not source.exists():
            return False
        # 目录内部也先全量检查；不移动含外部链接的资料树。
        if source.is_dir():
            for entry in source.rglob("*"):
                if reparse(entry):
                    raise ValueError("故障资料目录包含链接，不能穿透归档")
        target = _safe(self.data, f"{self.relative}/{relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.rows.append({"path": relative, "reason": reason, "state": "prepared"})
        self._save()  # 先写意图；故障中断也可定位原资料。
        source.rename(target)
        self.rows[-1]["state"] = "archived"
        self._save()
        return True

    def _save(self):
        atomic_json(_safe(self.data, f"{self.relative}/manifest.json"), {
            "schema": 1, "items": self.rows,
            "fields": {"items": "已隔离的启动故障来源", "path": "原数据相对路径",
                       "reason": "归档原因，不含原始配置值", "state": "准备或已归档"},
        })


def _valid_settings(defaults: dict, raw: object) -> dict:
    from .server.product import _merge_settings, _settings_validation_warning
    from .speaker.settings import validate_tuning
    from .screen_capture import ScreenCaptureConfig
    from .quiet_hours import QuietHoursConfig
    from .audio_enhancements import AudioEnhancements
    candidate = _merge_settings(defaults, raw)
    # 合并入口有兼容回退；落盘前还须严格检查，不能只相信“没有抛错”。
    if _settings_validation_warning(candidate):
        raise ValueError("合并后的产品设置未通过校验")
    validate_tuning(candidate["speaker"], continuity=candidate["speaker"]["continuity_threshold"])
    ScreenCaptureConfig(**candidate["screen_capture"])
    QuietHoursConfig(**candidate["quiet_hours"])
    AudioEnhancements.from_mapping(candidate["audio_enhancements"])
    return candidate


def _repair_settings(program: Path, data: Path, archive: Archive, *, reset_devices: bool) -> dict:
    import yaml
    from .config import load_config
    from .server.product import PRODUCT_SETTINGS_SCHEMA_VERSION, _default_settings, apply_settings_to_config
    default_path = program / "config/default.yaml"
    local = _safe(data, "config/local.yaml")
    if local.is_file() and local.stat().st_size <= 4 * 1024 * 1024:
        try:
            legacy = yaml.safe_load(local.read_text(encoding="utf-8-sig"))
            asr = legacy.get("asr") if isinstance(legacy, dict) else None
            if isinstance(asr, dict) and "punctuation_enabled" in asr:
                asr.pop("punctuation_enabled")
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=local.parent, prefix=".punctuation-", suffix=".yaml", delete=False) as stream:
                    stream.write(yaml.safe_dump(legacy, allow_unicode=True, sort_keys=False))
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary = Path(stream.name)
                try:
                    archive.move("config/local.yaml", "标点升级为必需组件，移除旧标点开关并保留其余配置")
                    temporary.replace(local)
                finally:
                    temporary.unlink(missing_ok=True)
        except (UnicodeError, yaml.YAMLError, RecursionError):
            pass  # 下方权威加载入口统一归档损坏配置。
    try:
        if local.is_file() and local.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("本机覆盖配置超过兼容处理大小限制")
        config = load_config(default_path, local if local.is_file() else None)
        defaults = _default_settings(config)
        _valid_settings(defaults, defaults)
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError, OverflowError, RecursionError, yaml.YAMLError):
        archive.move("config/local.yaml", "本机覆盖配置无法加载，回退新版默认配置")
        config = load_config(default_path)
        defaults = _default_settings(config)
    path = _safe(data, "config/product-settings.json")
    raw = None
    try:
        if path.is_file() and path.stat().st_size <= 4 * 1024 * 1024:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        pass
    supported = (isinstance(raw, dict) and type(raw.get("schema_version", 1)) is int
                 and 1 <= raw.get("schema_version", 1) <= PRODUCT_SETTINGS_SCHEMA_VERSION)
    try:
        candidate = _valid_settings(defaults, raw if supported else {})
        apply_settings_to_config(config, candidate)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        candidate = _valid_settings(defaults, {})
        apply_settings_to_config(config, candidate)
    if reset_devices:
        # 环境被强制重建时先恢复中央处理器路径，不能沿用失效显卡能力登记。
        candidate["compute"] = deepcopy(_default_settings(load_config(default_path))["compute"])
        for field in candidate["compute"].values():
            field.update({name: "cpu" for name in field})
        archive.move("config/gpu-environment.json", "运行环境重建，原显卡能力登记已失效")
    if raw != candidate:
        archive.move("config/product-settings.json", "迁移或默认回退前的原产品设置")
        atomic_json(path, candidate)
    return candidate


def _repair_registry(program: Path, data: Path, archive: Archive) -> None:
    from .config import load_config
    from .model_registry.service import ModelRegistryService, ModelRegistryError
    from .model_registry.vault import WindowsCredentialVault
    path = _safe(data, "config/model-registry.json")
    if not path.is_file():
        return
    service = ModelRegistryService(config=load_config(program / "config/default.yaml"),
                                   registry_path=path, vault=WindowsCredentialVault())
    try:
        service._load()  # 复用注册表格式迁移；此入口不读取或删除系统密钥。
    except (ModelRegistryError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        archive.move("config/model-registry.json", "模型注册配置无法迁移，恢复新版空配置；系统凭据保持原位")
        atomic_json(path, service._empty())
        service._load()


def _repair_speaker(data: Path, archive: Archive) -> None:
    from .speaker.registry import SpeakerProfileStore
    root = _safe(data, "speaker")
    if not root.is_dir():
        return
    archive.move("speaker/wespeaker-cnceleb-resnet34", "已退役声纹模型的注册资料，不再参与新版识别")
    for directory in list(root.iterdir()):
        if not directory.is_dir() or not (directory / "registry.json").exists():
            continue
        relative = directory.relative_to(data).as_posix()
        _safe(data, relative)
        for entry in directory.rglob("*"):
            _safe(data, entry.relative_to(data).as_posix())
        store = SpeakerProfileStore(directory, model_id=directory.name)
        raw = json.loads(store.path.read_text(encoding="utf-8")) if not store.warning else {}
        invalid = bool(store.warning or store.stale)
        invalid |= len(store.templates) != len(raw.get("templates", []))
        if not invalid and store.templates:
            import numpy as np
            for identifier in store.templates:
                try:
                    vector = np.load(_safe(data, f"{relative}/templates/{identifier}.npy"), allow_pickle=False)
                    invalid |= vector.size != store.vector_dim or not bool(np.isfinite(vector).all())
                except (ValueError, OSError):
                    invalid = True
        if invalid:
            archive.move(relative, "声纹注册资料损坏、模板缺失或特征格式不兼容，恢复未注册状态")


def _repair_voices(data: Path, archive: Archive, selections: dict) -> set[str]:
    from .tts.voices import VoiceLibrary, VoiceLibraryError
    affected = set()
    for model in ("voxcpm1", "voxcpm2"):
        relative = f"tts/{model}/voices"
        root = _safe(data, relative)
        index = _safe(data, f"{relative}/index.json")
        if not index.is_file():
            if selections.get(model):
                affected.add(model)  # 归档后中断的重试也必须清除悬空选择。
            continue
        for entry in root.rglob("*"):
            _safe(data, entry.relative_to(data).as_posix())
        try:
            # 只读索引；构造 VoiceLibrary 会执行回收，因此不能用于故障探测。
            library = VoiceLibrary.__new__(VoiceLibrary)
            library.root, library.assets_dir, library.index_path, library.model = root, root / "assets", index, model
            library._lock = threading.RLock()
            value = library._read_index()
            for identifier, asset in value["assets"].items():
                if not isinstance(asset, dict):
                    raise VoiceLibraryError("音频素材登记格式无效")
            library._index = value
            for identifier in value["assets"]:
                with wave.open(str(library.asset_path(identifier)), "rb") as audio:
                    if audio.getnframes() <= 0:
                        raise VoiceLibraryError("音色参考音频为空")
            library.list_voices()  # 全部条目都会参与音色页投影，也需要排除故障来源。
            selection = library.active_selection
            if selection:
                library.resolve(model, selection)
            if selections.get(model):
                library.resolve(model, selections[model])
        except (VoiceLibraryError, ValueError, TypeError, KeyError, AttributeError, OSError, wave.Error, EOFError):
            archive.move(relative, "音色索引、已选音色版本或引用素材不兼容，恢复默认音色")
            affected.add(model)
    return affected


def _repair_policy(program: Path, data: Path, archive: Archive) -> None:
    from .resources import ResourcePolicy
    path = _safe(data, "config/runtime-resources.json")
    try:
        ResourcePolicy.load(path)
    except (ValueError, TypeError, UnicodeError, AttributeError):
        archive.move("config/runtime-resources.json", "运行资源策略无效，恢复新版线程及检测后端默认值")
        value = asdict(ResourcePolicy.load(program / "config/runtime-resources.json"))
        value["policy_schema"] = 2
        atomic_json(path, value)
        ResourcePolicy.load(path)


def _restore_default_voices(program: Path, data: Path, candidate: dict) -> None:
    """本地合成必须有实际可解析音色；使用随新版程序校验过的种子重建。"""
    if candidate["tts"]["backend"] != "voxcpm1":
        return
    from .tts.voices import VoiceLibrary
    seed = program / "config/resources/assets/tts"
    audio, text = seed / "default.wav", seed / "default.json"
    for model in ("voxcpm1", "voxcpm2"):
        library = VoiceLibrary(_safe(data, f"tts/{model}/voices"), model=model)
        selection = library.active_selection
        if not selection:
            if not audio.is_file() or not text.is_file():
                raise ValueError("本地语音默认音色缺失，当前程序包需要包含有效默认音色种子")
            transcript = json.loads(text.read_text(encoding="utf-8"))["reference_text"]
            library.register_default(audio.read_bytes(), transcript)
            selection = library.active_selection
        library.resolve(model, selection)


def _archive_session(data: Path, archive: Archive) -> None:
    pending = _safe(data, "state/compatibility-session-archive.json")
    if pending.exists():
        try:
            value = json.loads(pending.read_text(encoding="utf-8"))
            relative = value.get("archive", "")
            token = relative.removeprefix("archives/compatibility/")
            if not relative.startswith("archives/compatibility/") or len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
                raise ValueError("会话归档恢复记录无效")
            manifest = _safe(data, f"{relative}/manifest.json")
            rows = json.loads(manifest.read_text(encoding="utf-8"))["items"] if manifest.exists() else []
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("会话归档清单无效")
            archive.relative, archive.rows = relative, rows
        except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
            archive.move("state/compatibility-session-archive.json", "会话归档派生记录损坏，保留原件并重新归档现存文件")
    elif not any(_safe(data, f"history/active-session.sqlite3{suffix}").exists() for suffix in ("", "-wal", "-shm")):
        return
    atomic_json(pending, {"schema": 1, "archive": archive.relative,
                          "fields": {"archive": "同组会话主库和日志的归档目录，中断后继续"}})
    # 主库与日志始终使用同一个可重放归档组，主库已移走也不能跳过剩余日志。
    for suffix in ("", "-wal", "-shm"):
        archive.move(f"history/active-session.sqlite3{suffix}", "升级后重新建立活动会话，旧会话保留供恢复查阅")
    pending.unlink()


def repair(program: Path, data: Path, *, reset_devices: bool = False) -> dict:
    """repair（强制修复）：执行后只返回已验证配置，不返回无法确认。"""
    _safe(data, "")
    archive = Archive(data)
    _archive_session(data, archive)
    environment = _safe(data, ".env.local")
    if environment.is_file():
        try:
            environment.read_text(encoding="utf-8")
        except UnicodeError:
            archive.move(".env.local", "环境配置编码损坏，退出当前加载；原件保密归档，不写入日志")
    _repair_policy(program, data, archive)
    candidate = _repair_settings(program, data, archive, reset_devices=reset_devices)
    _repair_registry(program, data, archive)
    _repair_speaker(data, archive)
    affected = _repair_voices(data, archive, candidate["tts"]["active_voice_by_model"])
    for model in affected:
        candidate["tts"]["active_voice_by_model"][model] = None
    if affected:
        # 音色索引是选择的依据；不能保留指向已归档音色的产品设置引用。
        atomic_json(_safe(data, "config/product-settings.json"), candidate)
    _restore_default_voices(program, data, candidate)
    record_settings_validation(_safe(data, "config/product-settings.json"), candidate["schema_version"])
    return {"ready": True, "settings_schema": candidate["schema_version"],
            "archived_items": len(archive.rows), "archive": archive.relative if archive.rows else None,
            "fields": {"ready": "配置及启动资料已处理", "settings_schema": "修复后的设置格式",
                       "archived_items": "隔离项目数量", "archive": "故障资料归档位置"}}
