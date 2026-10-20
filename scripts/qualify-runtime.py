#!/usr/bin/env python3
"""Qualification runner（运行时验收器）：只在独立测试工作区验证动态端口与重连。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
import uuid

sys.dont_write_bytecode = True  # 不把验收脚本的缓存写回源码或其他工作区。

try:
    import psutil
except ImportError:  # noqa: BLE001 - 输出仅报告依赖缺失，不输出异常内容。
    psutil = None


TEST_ROOT = Path(r"D:\项目开发\测试")  # TEST_ROOT（测试根目录）：禁止使用工作区外路径。
DEFAULT_WORKSPACE = TEST_ROOT / "资源验收" / "工作区 中文和空格"
ENDPOINT_PATHS = {
    "host": "", "web": "", "voice": "", "asr": "/asr",
    "analysis": "/analysis", "speaker": "/speaker", "dsh": "",
}
COMPONENTS = ("host", "web", "voice", "asr", "analysis", "speaker", "dsh")
REQUIRED_COMPONENTS = {"host", "web", "voice", "asr"}
REPORT_FIELDS = {
    "schema": "验收结果格式版本",
    "created_at": "验收结果生成时间（UTC）",
    "ok": "动态端口与重连验收是否通过",
    "error_code": "不包含凭据或异常正文的错误代码",
    "blocked_preferred_ports": "由验收进程独占的首选端口（端口占用）",
    "endpoints": "宿主端点表逐项校验结果",
    "retries": "经受控会话调用现有重试接口后的结果",
    "full_restart": "可选的整程序重启校验结果",
    "offline_bundle_imported": "是否已将测试目录中的模型包离线导入到测试工作区",
    "generation_present": "端点行包含受管服务代次",
    "old_port": "重试前实际端口",
    "new_port": "重试后实际端口",
    "previous_instance_ports_not_reused": "整程序重启后没有复用上一实例端口",
    "endpoint_count": "整程序重启后已校验的端点数量",
    "asr": "ASR（语音识别）的模型健康状态",
    "voice_asr_session": "Voice（语音服务）当前实际持有的 ASR（语音识别）会话状态",
    "analysis": "Analysis（分析）的各识别通道健康状态",
    "smart_turn": "Smart Turn（智能轮次检测）模型状态",
    "emotion": "emotion2vec+ base（情绪背景）模型状态",
    "acoustic_events": "YAMNet（声学事件）通道状态",
    "dual_pass": "Paraformer（语音识别）双路精修通道状态",
    "all_models_enabled": "是否启用 KWS（关键词唤醒）与 CAM++（声纹）验收",
    "all_model_health": "KWS（关键词唤醒）、CAM++（声纹）与基础模型健康状态",
    "test_configuration_restored": "本轮全部模型验收配置是否已在受管服务停止后恢复",
    "paraformer": "Paraformer（语音识别）模型健康状态",
    "voice_vad": "Voice VAD（语音活动检测）模型健康状态",
    "kws": "KWS（关键词唤醒）模型健康状态",
    "campplus": "CAM++（声纹）模型健康状态",
    "service": "模型服务健康状态",
    "verified": "要求的模型健康状态是否全部通过",
    "component": "端点所属组件：host（应用宿主）、web（界面）、voice（语音服务）、asr（识别）、analysis（分析）、speaker（声纹）、dsh（代理）",
    "preferred_port": "配置中的首选端口；0 表示系统分配",
    "actual_port": "端点表公布的实际绑定端口",
    "pid": "端点所有者进程编号",
    "pid_owned": "进程属于本次受管 Python 进程树",
    "created_at_matches": "操作系统进程创建时间与端点登记匹配",
    "loopback": "监听地址是本机回环地址",
    "listener_owned": "操作系统监听套接字属于登记进程",
    "fallback_used": "实际端口与被占用的首选端口不同",
    "endpoint_revision_before": "重试前端点版本",
    "endpoint_revision_after": "重试后端点版本",
    "endpoint_changed": "实际端点地址发生变化",
    "dependency_health": "Voice（语音服务）通过新端点重连后的模型健康状态",
    "restart_complete": "现有组件重试接口已完成处理",
    "fields": "本报告其他字段的中文说明",
}


class QualificationError(RuntimeError):
    """Safe failure（安全错误）：message 不包含子进程输出、令牌或配置内容。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _has_reparse_point(path: Path) -> bool:
    """Check Windows reparse points（链接/目录联接），防止写出测试目录。"""
    if os.name != "nt":
        return False
    try:
        return bool(path.stat().st_file_attributes & 0x400)
    except FileNotFoundError:
        return False


def _assert_no_reparse(path: Path, stop: Path) -> None:
    cursor = path
    stop = stop.resolve()
    while True:
        if _has_reparse_point(cursor):
            raise QualificationError("workspace_path_is_link")
        if cursor.resolve() == stop:
            return
        if cursor.parent == cursor:
            raise QualificationError("workspace_path_outside_test_root")
        cursor = cursor.parent


def _safe_workspace(value: Path) -> Path:
    workspace = value.resolve()
    test_root = TEST_ROOT.resolve()
    if workspace == test_root or not _inside(workspace, test_root):
        raise QualificationError("workspace_must_be_independent_test_subdirectory")
    _assert_no_reparse(workspace, test_root)
    return workspace


def _read_portable(workspace: Path) -> tuple[dict, Path, Path]:
    """Load portable.json（便携配置）；其 paths 必须解析到本测试工作区内。"""
    portable_path = workspace / "portable.json"
    try:
        portable = json.loads(portable_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 不透出路径、配置或异常文本。
        raise QualificationError("portable_manifest_unavailable") from exc
    if not isinstance(portable, dict) or portable.get("schema") != 2:
        raise QualificationError("portable_manifest_invalid")
    app_relative = portable.get("app")
    python_relative = portable.get("python")
    if not isinstance(app_relative, str) or not isinstance(python_relative, str):
        raise QualificationError("portable_paths_invalid")
    app_root = (workspace / app_relative).resolve()
    python = (workspace / python_relative).resolve()
    if not _inside(app_root, workspace) or not _inside(python, workspace):
        raise QualificationError("portable_paths_outside_workspace")
    _assert_no_reparse(app_root, workspace)
    _assert_no_reparse(python, workspace)
    if not (app_root / "config/default.yaml").is_file():
        raise QualificationError("application_package_unavailable")
    if not (app_root / "scripts/backend-entry.py").is_file():
        raise QualificationError("managed_backend_entry_unavailable")
    if not python.is_file():
        raise QualificationError("managed_python_unavailable")
    if portable.get("data_dir") != "data":
        raise QualificationError("workspace_data_path_invalid")
    workspace_id = portable.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise QualificationError("workspace_id_unavailable")
    return portable, app_root, python


def _qualification_marker(workspace: Path, app_root: Path) -> bool:
    """Accept only this workspace's explicit prepare/resource qualification marker（验收标记）。"""
    roots = [workspace, *workspace.parents]
    if TEST_ROOT not in roots:
        roots.append(TEST_ROOT)
    for root in roots:
        if not _inside(root, TEST_ROOT):
            continue
        prepare = root / "prepare-result.json"
        if prepare.is_file() and not _has_reparse_point(prepare):
            try:
                record = json.loads(prepare.read_text(encoding="utf-8-sig"))
                declared_root = Path(record.get("root", "")).resolve()
                if (record.get("channel") == "distribution" and record.get("ready") is True
                        and declared_root == root.resolve() and workspace != root and _inside(workspace, root)):
                    return True
            except Exception:  # noqa: BLE001 - 验收标记只作布尔判定，不回显其内容。
                pass
        report = root / "resource-qualification.json"
        if report.is_file() and not _has_reparse_point(report):
            try:
                record = json.loads(report.read_text(encoding="utf-8-sig"))
                summary = record.get("summary")
                if (record.get("schema_version") == 1
                        and Path(record.get("workspace_root", "")).resolve() == workspace.resolve()
                        and Path(record.get("program_root", "")).resolve() == app_root.resolve()
                        and isinstance(summary, dict) and summary.get("failed") == 0
                        and isinstance(summary.get("passed"), int) and summary["passed"] > 0):
                    return True
            except Exception:  # noqa: BLE001 - 验收标记只作布尔判定，不回显其内容。
                pass
    return False


def _default_test_documents(app_root: Path, workspace: Path) -> tuple[dict, dict, dict, dict]:
    """Load immutable defaults and the automatic registry seed（程序默认值与自动注册表种子），without reading credentials."""
    override_prefixes = ("VOICE_", "ASR_WORKER_")
    override_names = {name for name in os.environ
                      if name.startswith(override_prefixes) or name == "MINIMAX_API_HOST"}
    saved_environment = {name: os.environ[name] for name in override_names}
    for name in override_names:
        os.environ.pop(name, None)
    old_path = list(sys.path)
    try:
        import asyncio
        os.environ.update({
            "VOICE_PROGRAM_ROOT": str(app_root),
            "VOICE_INSTALL_ROOT": str(workspace),
            "VOICE_LAUNCH_CHANNEL": "distribution",
            "VOICE_DATA_DIR": str(workspace / "data"),
            "VOICE_MODEL_DIR": str(workspace / "models"),
        })
        sys.path.insert(0, str(app_root))
        import yaml
        from voice_terminal.config import AppConfig
        from voice_terminal.model_registry.service import ModelRegistryService
        from voice_terminal.server.product import _default_settings

        default_config_path = app_root / "config/default.yaml"
        default_config = yaml.safe_load(default_config_path.read_text(encoding="utf-8-sig"))
        config = AppConfig.from_mapping(default_config, base_dir=app_root)
        product_defaults = _default_settings(config)

        class _RegistrySeedProbe(ModelRegistryService):
            """Capture scan output in memory（内存捕获扫描结果），without writing a registry file."""

            seeded_registry: dict | None = None

            def _write(self, value):
                self.seeded_registry = json.loads(json.dumps(value))

        class _NoCredentialVault:
            """Report no vault（无凭据库）without reading or storing secrets."""
            available = False
            diagnostic = "not used by qualification seed"

        async def build_registry_seed() -> dict:
            probe = _RegistrySeedProbe(
                config=config,
                registry_path=workspace / "data/config" / f".qualification-seed-{uuid.uuid4().hex}.json",
                vault=_NoCredentialVault(),
            )
            await probe.scan()
            if not isinstance(probe.seeded_registry, dict):
                raise RuntimeError("default registry scan did not produce a seed")
            return probe.seeded_registry

        # The generated registry contains runtime and local-scan projections,
        # not just ModelRegistryService._empty()（空注册表）。 Patch the API-key（接口密钥）
        # accessor for this computation so external credentials are never read.
        from unittest.mock import patch
        with patch.object(AppConfig, "api_key", return_value=""):
            registry_defaults = asyncio.run(build_registry_seed())
        resource_policy = json.loads((app_root / "config/runtime-resources.json").read_text(encoding="utf-8-sig"))
        return default_config, product_defaults, registry_defaults, resource_policy
    except Exception as exc:  # noqa: BLE001 - 不向报告输出本机环境或文件内容。
        raise QualificationError("qa_default_state_unavailable") from exc
    finally:
        sys.path[:] = old_path
        for name in override_prefixes:
            for key in tuple(os.environ):
                if key.startswith(name):
                    os.environ.pop(key, None)
        os.environ.pop("MINIMAX_API_HOST", None)
        os.environ.update(saved_environment)


def _atomic_test_file(path: Path, data: bytes, workspace: Path) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not _inside(parent.resolve(), workspace):
        raise QualificationError("workspace_write_path_invalid")
    _assert_no_reparse(parent, workspace)
    if path.exists() and _has_reparse_point(path):
        raise QualificationError("workspace_path_is_link")
    fd, temporary_name = tempfile.mkstemp(prefix=".qualification-override-", suffix=".tmp", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _restore_test_files(workspace: Path, backups: dict[Path, bytes | None]) -> None:
    failed = False
    for path, original in backups.items():
        try:
            if not _inside(path.resolve(), workspace):
                raise QualificationError("workspace_write_path_invalid")
            if path.exists() and _has_reparse_point(path):
                raise QualificationError("workspace_path_is_link")
            if original is None:
                if path.exists():
                    path.unlink()
            else:
                _atomic_test_file(path, original, workspace)
        except Exception:  # noqa: BLE001 - continue restoring the remaining file without exposing its contents.
            failed = True
    if failed:
        raise QualificationError("test_configuration_restore_failed")


def _apply_all_model_overrides(workspace: Path, app_root: Path,
                               backups: dict[Path, bytes | None] | None = None) -> dict[Path, bytes | None]:
    """仅在验收工作区临时启用 KWS（关键词唤醒）与 CAM++（声纹）。"""
    config = workspace / "data/config"
    if not _inside(config.resolve(), workspace):
        raise QualificationError("workspace_write_path_invalid")
    local_path = config / "local.yaml"
    settings_path = config / "product-settings.json"
    backups = backups if backups is not None else {}
    try:
        local_bytes = local_path.read_bytes() if local_path.is_file() else None
        backups[local_path] = local_bytes
        old_path = list(sys.path)
        try:
            sys.path.insert(0, str(app_root))
            import yaml
            local = yaml.safe_load(local_bytes.decode("utf-8-sig")) if local_bytes is not None else {}
        finally:
            sys.path[:] = old_path
        local = local or {}
        if not isinstance(local, dict):
            raise QualificationError("workspace_contains_user_configuration")
        wake = local.get("wake", {})
        speaker = local.get("speaker", {})
        if not isinstance(wake, dict) or not isinstance(speaker, dict):
            raise QualificationError("workspace_contains_user_configuration")
        local["wake"] = {**wake, "enabled": True}
        local["speaker"] = {**speaker, "mode": "observe"}
        try:
            sys.path.insert(0, str(app_root))
            import yaml
            local_data = yaml.safe_dump(local, allow_unicode=True, sort_keys=False).encode("utf-8")
        finally:
            sys.path[:] = old_path
        _atomic_test_file(local_path, local_data, workspace)

        if settings_path.is_file():
            settings_bytes = settings_path.read_bytes()
            backups[settings_path] = settings_bytes
            settings = json.loads(settings_bytes.decode("utf-8-sig"))
            if not isinstance(settings, dict):
                raise QualificationError("workspace_contains_user_configuration")
            settings_wake = settings.get("wake")
            settings_speaker = settings.get("speaker")
            if not isinstance(settings_wake, dict) or not isinstance(settings_speaker, dict):
                raise QualificationError("workspace_contains_user_configuration")
            settings_wake["enabled"] = True
            settings_speaker["mode"] = "observe"
            settings_data = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _atomic_test_file(settings_path, settings_data, workspace)
        return backups
    except Exception:
        _restore_test_files(workspace, backups)
        raise


def _has_files(directory: Path, workspace: Path) -> bool:
    if not directory.exists():
        return False
    if not directory.is_dir():
        return True
    _assert_no_reparse(directory, workspace)
    for current, folders, files in os.walk(directory, followlinks=False):
        current_path = Path(current)
        for name in [*folders, *files]:
            entry = current_path / name
            if _has_reparse_point(entry):
                raise QualificationError("workspace_path_is_link")
        if files:
            return True
    return False


def _matches_registry_seed(value: object, registry_defaults: dict) -> bool:
    """Accept only the automatic registry scan（自动注册表扫描）with optional external-key status（外部密钥状态）."""
    if value == registry_defaults:
        return True
    if not isinstance(value, dict):
        return False
    normalized = json.loads(json.dumps(value))
    providers = normalized.get("providers")
    if not isinstance(providers, list):
        return False
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("origin") != "runtime_config":
            continue
        # credential_state（凭据状态）只允许 missing（未配置）或 externally_managed（外部管理）。
        if provider.get("credential_state") not in {"missing", "externally_managed"}:
            return False
        # This field reveals presence only（只表示是否配置），not the credential value.
        provider["credential_state"] = "missing"
    return normalized == registry_defaults


def _reject_existing_user_state(workspace: Path, app_root: Path) -> None:
    """Allow only marker-bound empty/default QA files（验收默认文件），reject personal or credential state."""
    data = workspace / "data"
    if data.exists() and not data.is_dir():
        raise QualificationError("workspace_data_path_invalid")
    if not data.exists():
        return
    _assert_no_reparse(data, workspace)
    config = data / "config"
    credential_paths = (
        data / ".env", data / ".env.local", data / "credentials.json",
        config / ".env", config / ".env.local", config / "credentials.json", config / "secrets.json",
    )
    if any(path.exists() for path in credential_paths):
        raise QualificationError("workspace_contains_user_configuration")

    default_paths = {
        config / "local.yaml", config / "product-settings.json",
        config / "model-registry.json", config / "runtime-resources.json",
    }
    if config.exists():
        if not config.is_dir():
            raise QualificationError("workspace_configuration_path_invalid")
        _assert_no_reparse(config, workspace)
        allowed_names = {path.name for path in default_paths}
        for entry in config.iterdir():
            if _has_reparse_point(entry):
                raise QualificationError("workspace_path_is_link")
            if entry.name == "kws" and entry.is_dir():
                # AppPaths.prepare（应用路径准备）creates this empty folder for the
                # default keyword file. Accept it only in a marked QA workspace.
                if not _qualification_marker(workspace, app_root) or any(entry.iterdir()):
                    raise QualificationError("workspace_contains_user_configuration")
                continue
            if entry.name not in allowed_names or entry.is_dir():
                raise QualificationError("workspace_contains_user_configuration")
    persona = data / "persona"
    if persona.exists() and (_has_reparse_point(persona) or not persona.is_dir()):
        raise QualificationError("workspace_contains_user_configuration")
    user_state_present = any(path.exists() for path in default_paths) or persona.exists()
    if user_state_present and not _qualification_marker(workspace, app_root):
        raise QualificationError("workspace_contains_user_configuration")

    if user_state_present:
        default_config, product_defaults, registry_defaults, resource_policy = _default_test_documents(app_root, workspace)
        try:
            import yaml
            if (config / "local.yaml").is_file():
                local = yaml.safe_load((config / "local.yaml").read_text(encoding="utf-8-sig")) or {}
                if not isinstance(local, dict) or local not in ({}, default_config):
                    raise QualificationError("workspace_contains_user_configuration")
            if (config / "product-settings.json").is_file():
                value = json.loads((config / "product-settings.json").read_text(encoding="utf-8-sig"))
                if not isinstance(value, dict) or value not in ({}, product_defaults):
                    raise QualificationError("workspace_contains_user_configuration")
            if (config / "model-registry.json").is_file():
                value = json.loads((config / "model-registry.json").read_text(encoding="utf-8-sig"))
                if not _matches_registry_seed(value, registry_defaults):
                    raise QualificationError("workspace_contains_user_configuration")
            if (config / "runtime-resources.json").is_file():
                value = json.loads((config / "runtime-resources.json").read_text(encoding="utf-8-sig"))
                if value != resource_policy:
                    raise QualificationError("workspace_contains_user_configuration")
            if persona.is_dir():
                source = app_root / "config/templates/persona"
                if not source.is_dir():
                    source = app_root / "config/persona"
                expected_names = {"personality.md", "user.md", "instructions.md"}
                for path in persona.rglob("*"):
                    if _has_reparse_point(path):
                        raise QualificationError("workspace_path_is_link")
                    if path.is_dir():
                        continue
                    template = source / path.name
                    if (path.name not in expected_names or not template.is_file()
                            or path.read_bytes() != template.read_bytes()):
                        raise QualificationError("workspace_contains_user_configuration")
        except QualificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不向报告输出配置或解析诊断。
            raise QualificationError("workspace_contains_user_configuration") from exc

    if _has_files(data / "speaker", workspace) or _has_files(data / "dsh", workspace):
        raise QualificationError("workspace_contains_user_configuration")

    history = data / "history"
    if history.exists():
        if not history.is_dir():
            raise QualificationError("workspace_contains_user_configuration")
        _assert_no_reparse(history, workspace)
        pending_path = history / "pending-transfer.json"
        backlog_path = history / "archive-backlog.json"
        archives_path = history / "archives"
        for entry in history.iterdir():
            if _has_reparse_point(entry):
                raise QualificationError("workspace_path_is_link")
            if entry == archives_path:
                if not entry.is_dir() or _has_files(entry, workspace):
                    raise QualificationError("workspace_contains_user_configuration")
            elif entry not in {pending_path, backlog_path} or not entry.is_file():
                raise QualificationError("workspace_contains_user_configuration")
        if pending_path.exists() or backlog_path.exists():
            if not _qualification_marker(workspace, app_root):
                raise QualificationError("workspace_contains_user_configuration")
            try:
                if pending_path.exists() and json.loads(pending_path.read_text(encoding="utf-8")) != {
                        "schema_version": 1, "entries": {}, "seen": [], "captures": {}}:
                    raise QualificationError("workspace_contains_user_configuration")
                if backlog_path.exists() and json.loads(backlog_path.read_text(encoding="utf-8")) != {
                        "pending": [], "consumed": []}:
                    raise QualificationError("workspace_contains_user_configuration")
            except QualificationError:
                raise
            except Exception as exc:  # noqa: BLE001 - never expose history data.
                raise QualificationError("workspace_contains_user_configuration") from exc


def _minimal_environment(workspace: Path, app_root: Path, python: Path, workspace_id: str) -> dict[str, str]:
    """Create a credential-free child environment（不继承凭据的子进程环境）。"""
    names = (
        "PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "OS",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
        "CUDA_PATH", "CUDA_PATH_V12_8", "NVIDIA_VISIBLE_DEVICES",
    )
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    runtime_bin = str(python.parent)
    current_path = env.get("PATH", "")
    env["PATH"] = runtime_bin + (os.pathsep + current_path if current_path else "")
    cache = workspace / "cache"
    data = workspace / "data"
    temporary = workspace / "tmp/process"
    for folder in (cache, data, temporary):
        folder.mkdir(parents=True, exist_ok=True)
        if not _inside(folder.resolve(), workspace):
            raise QualificationError("workspace_write_path_invalid")
    env.update({
        "VOICE_PROGRAM_ROOT": str(app_root),
        "VOICE_INSTALL_ROOT": str(workspace),
        "VOICE_DATA_DIR": str(data),
        "VOICE_LAUNCH_CHANNEL": "distribution",
        "VOICE_WORKSPACE_ID": workspace_id,
        "VOICE_RESOURCE_POLICY": str(data / "config/runtime-resources.json"),
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(cache / "python"),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "MODELSCOPE_OFFLINE": "1",
        "HF_HOME": str(cache / "huggingface"),
        "MODELSCOPE_CACHE": str(cache / "modelscope"),
        "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache),
        "TEMP": str(temporary),
        "TMP": str(temporary),
    })
    return env


def _import_offline_bundle(workspace: Path, app_root: Path, python: Path,
                           env: dict[str, str], bundle: Path | None) -> bool:
    """Use only import（离线导入）; this runner has no download path."""
    if bundle is None:
        return False
    source = bundle.resolve()
    if not _inside(source, TEST_ROOT.resolve()) or not source.exists():
        raise QualificationError("offline_bundle_must_be_inside_test_root")
    _assert_no_reparse(source, TEST_ROOT.resolve())
    manager = app_root / "scripts/manage-resources.py"
    if not manager.is_file():
        raise QualificationError("offline_import_tool_unavailable")
    command = [str(python), "-B", "-X", "utf8", str(manager),
               "--root", str(workspace), "--program", str(app_root), "import",
               # task-id（任务编号）保持短值，避免验收目录前缀把 DSH（代理宿主）暂存文件推过 Windows 路径上限。
               "--source", str(source), "--task-id", "q-" + uuid.uuid4().hex[:12]]
    try:
        result = subprocess.run(command, cwd=app_root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=3600, check=False)
    except Exception as exc:  # noqa: BLE001 - CLI output may contain sensitive paths.
        raise QualificationError("offline_import_failed") from exc
    if result.returncode != 0:
        raise QualificationError("offline_import_failed")
    try:
        response = json.loads(result.stdout.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - do not persist raw CLI output.
        raise QualificationError("offline_import_result_invalid") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise QualificationError("offline_import_failed")
    return True


def _yaml_section(text: str, name: str) -> list[str]:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if re.match(rf"^{re.escape(name)}:\s*(?:#.*)?$", line)), None)
    if start is None:
        raise QualificationError("default_port_configuration_invalid")
    result = []
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
            break
        result.append(line)
    return result


def _yaml_integer(section: list[str], field: str, fallback: int) -> int:
    match = next((re.match(rf"^\s+{re.escape(field)}:\s*(\d+)\b", line)
                  for line in section if re.match(rf"^\s+{re.escape(field)}:", line)), None)
    if match is None:
        return fallback
    return int(match.group(1))


def _preferred_ports(app_root: Path) -> dict[str, int]:
    """Read only packaged defaults（程序默认配置）；never read data/config/local.yaml."""
    try:
        text = (app_root / "config/default.yaml").read_text(encoding="utf-8-sig")
        server = _yaml_integer(_yaml_section(text, "server"), "port", 8765)
        asr = _yaml_integer(_yaml_section(text, "asr_worker"), "port", 8766)
        speaker = _yaml_integer(_yaml_section(text, "speaker_worker"), "port", 8768)
        dsh = _yaml_integer(_yaml_section(text, "dsh"), "port", 0)
        smart = _yaml_section(text, "smart_turn")
        line = next((item for item in smart if re.match(r"^\s+analysis_url:", item)), "")
        match = re.search(r":(\d+)(?:/|$)", line)
        analysis = int(match.group(1)) if match else 8767
    except QualificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - suppress package/config details.
        raise QualificationError("default_port_configuration_invalid") from exc
    result = {"host": 5173, "voice": server, "asr": asr, "analysis": analysis,
              "speaker": speaker, "dsh": dsh}
    if any(type(port) is not int or not 0 <= port <= 65535 for port in result.values()):
        raise QualificationError("default_port_configuration_invalid")
    bound = [port for port in result.values() if port > 0]
    if len(bound) != len(set(bound)):
        raise QualificationError("preferred_ports_are_not_unique")
    return result


def _reserve_port(port: int) -> socket.socket:
    """Hold a loopback TCP port（独占本机回环端口）until test cleanup."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(socket.SOMAXCONN)
        return listener
    except OSError as exc:
        listener.close()
        raise QualificationError("preferred_port_not_available_for_controlled_test") from exc


def _origin_parts(origin: str) -> tuple[str, int]:
    parsed = urlsplit(origin)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise QualificationError("private_pipe_origin_invalid")
    return parsed.netloc, parsed.port


class ManagedApplication:
    """One private-pipe child（单实例私有管道子进程）；secrets stay in memory."""

    def __init__(self, command: list[str], app_root: Path, env: dict[str, str],
                 startup_timeout: float) -> None:
        self.command = command
        self.app_root = app_root
        self.env = env
        self.startup_timeout = startup_timeout
        self.process: subprocess.Popen | None = None
        self.messages = {"origin": "", "launch_token": "", "instance_id": "",
                         "endpoints": {}, "endpoint_revision": -1, "startup_result": None,
                         "protocol_error": False}
        self.lock = threading.Lock()
        self.message_event = threading.Event()
        self.reader: threading.Thread | None = None
        self.cookie = ""
        self.cookie_name = ""
        self.origin = ""
        self.host_pid = 0

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        while True:
            line = self.process.stdout.readline(16385)
            if not line:
                return
            if len(line) > 16384:
                while line and not line.endswith(b"\n"):
                    line = self.process.stdout.readline(16385)
                with self.lock:
                    self.messages["protocol_error"] = True
                    self.message_event.set()
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(message, dict) or message.get("protocol") != 1:
                continue
            kind = message.get("kind")
            with self.lock:
                if kind == "ready":
                    self.messages.update({
                        "origin": message.get("origin", ""),
                        "launch_token": message.get("launch_token", ""),
                        "instance_id": message.get("instance_id", ""),
                    })
                elif kind == "endpoints":
                    endpoints = message.get("endpoints")
                    revision = message.get("endpoint_revision")
                    if isinstance(endpoints, dict) and type(revision) is int:
                        self.messages["endpoints"] = endpoints
                        self.messages["endpoint_revision"] = revision
                elif kind == "startup_result":
                    self.messages["startup_result"] = {
                        "state": message.get("state"),
                        "core_available": message.get("core_available") is True,
                    }
                self.message_event.set()

    def _send(self, kind: str, **values) -> None:
        if self.process is None or self.process.stdin is None:
            raise QualificationError("private_pipe_unavailable")
        message = {"protocol": 1, "kind": kind, **values}
        self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def start(self) -> None:
        if os.name != "nt":
            raise QualificationError("windows_runtime_required")
        if psutil is None:
            raise QualificationError("psutil_required")
        try:
            self.process = subprocess.Popen(
                self.command, cwd=self.app_root, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                bufsize=0,
            )
        except Exception as exc:  # noqa: BLE001 - suppress executable and environment details.
            raise QualificationError("managed_python_start_failed") from exc
        self.host_pid = self.process.pid
        self.reader = threading.Thread(target=self._read_stdout, name="qualification-private-pipe", daemon=True)
        self.reader.start()
        deadline = time.monotonic() + min(self.startup_timeout, 60)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise QualificationError("managed_python_exited_before_ready")
            with self.lock:
                origin = self.messages["origin"]
                token = self.messages["launch_token"]
                if self.messages["protocol_error"]:
                    raise QualificationError("private_pipe_message_invalid")
            if origin and token:
                self.origin = origin
                self._establish_cookie(token)
                self._send("start")
                return
            self.message_event.wait(.2)
            self.message_event.clear()
        raise QualificationError("managed_host_ready_timeout")

    def _establish_cookie(self, launch_token: str) -> None:
        netloc, _ = _origin_parts(self.origin)
        host, port = netloc.rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port), timeout=5)
        try:
            connection.request("GET", "/desktop-entry?token=" + quote(launch_token, safe=""),
                               headers={"Host": netloc, "Cache-Control": "no-store"})
            response = connection.getresponse()
            response.read(1024 * 1024)
            if response.status != 200:
                raise QualificationError("controlled_cookie_session_failed")
            set_cookie = response.getheader("Set-Cookie", "")
            first = set_cookie.split(";", 1)[0]
            name, separator, value = first.partition("=")
            if not separator or not name or not value:
                raise QualificationError("controlled_cookie_session_failed")
            self.cookie_name = name
            self.cookie = first
        except QualificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - response details may include the one-time URL.
            raise QualificationError("controlled_cookie_session_failed") from exc
        finally:
            connection.close()

    def request_json(self, method: str, path: str, *, body: dict | None = None) -> tuple[int, dict]:
        netloc, _ = _origin_parts(self.origin)
        host, port_text = netloc.rsplit(":", 1)
        connection = http.client.HTTPConnection(host, int(port_text), timeout=15)
        headers = {"Host": netloc, "Cookie": self.cookie, "Cache-Control": "no-store"}
        data = None
        if method == "POST":
            headers.update({"Origin": self.origin, "Content-Type": "application/json"})
            data = json.dumps(body or {}, separators=(",", ":")).encode("utf-8")
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024)
            result = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(result, dict):
                raise QualificationError("host_response_invalid")
            return response.status, result
        except QualificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - no response body or token is persisted.
            raise QualificationError("host_request_failed") from exc
        finally:
            connection.close()

    def snapshot(self) -> dict:
        status, value = self.request_json("GET", "/api/v1/application/snapshot")
        if status != 200:
            raise QualificationError("application_snapshot_unavailable")
        return value

    def wait_pipe_revision(self, revision: int, timeout: float = 10) -> tuple[dict, int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                table = self.messages["endpoints"]
                current = self.messages["endpoint_revision"]
                protocol_error = self.messages["protocol_error"]
            if protocol_error:
                raise QualificationError("private_pipe_message_invalid")
            if current >= revision:
                return table, current
            if self.process is not None and self.process.poll() is not None:
                raise QualificationError("managed_python_exited")
            self.message_event.wait(.2)
            self.message_event.clear()
        raise QualificationError("private_endpoint_table_timeout")

    def wait_startup(self) -> dict:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise QualificationError("managed_python_exited_during_startup")
            snapshot = self.snapshot()
            if snapshot.get("startup_complete") is True:
                return snapshot
            time.sleep(.5)
        raise QualificationError("startup_snapshot_timeout")

    def wait_component(self, component: str, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise QualificationError("managed_python_exited_during_retry")
            snapshot = self.snapshot()
            row = next((item for item in snapshot.get("components", [])
                        if isinstance(item, dict) and item.get("id") == component), None)
            endpoint = snapshot.get("endpoints", {}).get(component, {})
            if isinstance(row, dict) and not row.get("retrying"):
                state = row.get("state")
                if state in {"failed", "blocked"}:
                    raise QualificationError(component + "_retry_failed")
                if state in {"disabled", "stopped"}:
                    raise QualificationError(component + "_retry_not_ready")
                if (state in {"ready", "external"}
                        and isinstance(endpoint, dict) and endpoint.get("url") and endpoint.get("pid")):
                    return snapshot
            time.sleep(.5)
        raise QualificationError("component_retry_timeout")

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self._send("shutdown", budget_ms=14000)
                self.process.wait(timeout=20)
            except Exception:  # noqa: BLE001 - cleanup must not reveal child output.
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    self.process.kill()
                    try:
                        self.process.wait(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


def _component_rows(snapshot: dict) -> dict[str, dict]:
    rows = snapshot.get("components")
    if not isinstance(rows, list):
        raise QualificationError("component_snapshot_invalid")
    return {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}


def _actual_ports(table: dict, preferred: dict[str, int], host_pid: int) -> list[dict]:
    """Validate loopback, PID ancestry, process creation time and OS listener ownership."""
    results = []
    for component in COMPONENTS:
        row = table.get(component)
        if not isinstance(row, dict) or not row.get("url"):
            continue
        url = row.get("url")
        try:
            parsed = urlsplit(url)
            address_host, port = parsed.hostname, parsed.port
            expected_scheme = "http" if component in {"host", "web", "voice", "dsh"} else "ws"
            expected_path = ENDPOINT_PATHS[component]
            if (parsed.scheme != expected_scheme or address_host != "127.0.0.1" or port is None
                    or not 1 <= port <= 65535 or parsed.path.rstrip("/") != expected_path):
                raise QualificationError("endpoint_url_invalid")
            if row.get("host") != address_host or row.get("port") != port or row.get("scheme") != parsed.scheme:
                raise QualificationError("endpoint_metadata_mismatch")
            pid = row.get("pid")
            created_at = row.get("created_at")
            if type(pid) is not int or pid <= 0 or not isinstance(created_at, (int, float)):
                raise QualificationError("endpoint_process_identity_missing")
            process = psutil.Process(pid)
            actual_created_at = float(process.create_time())
            if abs(actual_created_at - float(created_at)) > 2.0:
                raise QualificationError("endpoint_process_creation_time_mismatch")
            if component in {"host", "web"}:
                pid_owned = pid == host_pid
            else:
                current = process
                pid_owned = False
                for _ in range(64):
                    parent = current.parent()
                    if parent is None:
                        break
                    if parent.pid == host_pid:
                        pid_owned = True
                        break
                    current = parent
            if not pid_owned:
                raise QualificationError("endpoint_pid_not_owned")
            listener_owned = any(
                connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and (connection.laddr.ip if hasattr(connection.laddr, "ip") else connection.laddr[0]) == "127.0.0.1"
                and (connection.laddr.port if hasattr(connection.laddr, "port") else connection.laddr[1]) == port
                for connection in process.net_connections(kind="tcp")
            )
            if not listener_owned:
                raise QualificationError("endpoint_listener_not_owned")
            preferred_port = preferred.get(component, 0)
            fallback_used = not preferred_port or port != preferred_port
            if preferred_port and not fallback_used:
                raise QualificationError("preferred_port_did_not_fallback")
            generation_present = isinstance(row.get("generation"), str) and bool(row.get("generation"))
            if not generation_present:
                raise QualificationError("endpoint_generation_missing")
            results.append({
                "component": component,
                "preferred_port": preferred_port,
                "actual_port": port,
                "pid": pid,
                "pid_owned": pid_owned,
                "created_at_matches": True,
                "loopback": True,
                "listener_owned": listener_owned,
                "fallback_used": fallback_used,
                "generation_present": generation_present,
            })
        except QualificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - suppress URLs, PIDs and OS error details.
            raise QualificationError("endpoint_identity_validation_failed") from exc
    if not any(item["component"] == "host" for item in results):
        raise QualificationError("host_endpoint_missing")
    if not any(item["component"] == "web" for item in results):
        raise QualificationError("web_endpoint_missing")
    if not any(item["component"] == "voice" for item in results):
        raise QualificationError("voice_endpoint_missing")
    if not any(item["component"] == "asr" for item in results):
        raise QualificationError("asr_endpoint_missing")
    return results


def _assert_pipe_matches(table: dict, pipe_table: dict, revision: int, pipe_revision: int) -> None:
    if pipe_revision < revision:
        raise QualificationError("private_endpoint_table_stale")
    for component, row in table.items():
        if not isinstance(row, dict) or not row.get("url"):
            continue
        other = pipe_table.get(component)
        if not isinstance(other, dict):
            raise QualificationError("private_endpoint_table_missing_component")
        for field in ("url", "preferred_url", "host", "port", "scheme", "path", "generation", "pid"):
            if row.get(field) != other.get(field):
                raise QualificationError("private_endpoint_table_mismatch")


def _http_json_at(url: str, path: str, timeout: float) -> tuple[int, dict]:
    parsed = urlsplit(url.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise QualificationError("voice_api_endpoint_unavailable")
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request("GET", path, headers={
            "Host": parsed.netloc, "Cache-Control": "no-store"})
        response = connection.getresponse()
        value = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
        if not isinstance(value, dict):
            raise QualificationError("voice_api_response_invalid")
        return response.status, value
    except QualificationError:
        raise
    except Exception as exc:  # noqa: BLE001 - never preserve response text.
        raise QualificationError("voice_api_request_failed") from exc
    finally:
        connection.close()


def _voice_asr_session_state(base: str) -> str:
    """Read the live Voice session status（读取 Voice 实际 ASR 会话状态），not a direct Worker probe."""
    status, health = _http_json_at(base, "/health", timeout=5)
    services = health.get("services") if status == 200 else None
    if not isinstance(services, dict) or "asr" not in services:
        raise QualificationError("voice_health_unavailable")
    state = services.get("asr")
    return state if isinstance(state, str) else "UNKNOWN"


def _wait_voice_asr_session(base: str, timeout: float = 30) -> str:
    """Wait for Voice's own ASR session（等待 Voice 自身 ASR 会话），never accept a temporary Worker probe alone."""
    deadline = time.monotonic() + timeout
    state = "UNKNOWN"
    while time.monotonic() < deadline:
        state = _voice_asr_session_state(base)
        if state == "READY":
            return state
        if state in {"ERROR", "DISABLED", "STOPPED"}:
            raise QualificationError("voice_asr_session_not_ready_after_reconnect")
        time.sleep(.5)
    raise QualificationError("voice_asr_session_timeout_after_reconnect")


def _verify_dependencies(snapshot: dict, analysis_enabled: bool, timeout: float = 30) -> dict:
    voice = snapshot.get("voice_api_url")
    if not isinstance(voice, str) or not voice.startswith("http://127.0.0.1:"):
        raise QualificationError("voice_api_endpoint_unavailable")
    base = voice.rstrip("/")
    # 等 Voice（语音服务）自身的 ASR 会话完成重连；compute-status（计算状态）在会话断开时会用临时连接探测 Worker，不能单独证明 Voice 已连接。
    voice_asr_session = _wait_voice_asr_session(base, timeout)

    # Compute status（计算状态）verifies the loaded models at Voice's refreshed endpoints.
    status, compute = _http_json_at(base, "/api/v1/product/compute-status", timeout=120)
    if status != 200 or not isinstance(compute, dict) or not isinstance(compute.get("models"), dict):
        raise QualificationError("dependency_health_unavailable")
    models = compute["models"]
    asr_state = models.get("paraformer", {}).get("service")
    analysis_states = {
        "smart_turn": models.get("smart_turn", {}).get("service"),
    }
    if asr_state != "READY":
        raise QualificationError("asr_dependency_not_ready_after_reconnect")
    if analysis_enabled and any(state != "READY" for state in analysis_states.values()):
        raise QualificationError("analysis_dependency_not_ready_after_reconnect")
    # Recheck after compute-status so a temporary direct Worker probe cannot hide a lost Voice session.
    voice_asr_session = _wait_voice_asr_session(base, timeout=5)
    return {"voice_asr_session": "READY", "asr": asr_state, "analysis": analysis_states}


def _verify_all_model_health(snapshot: dict, timeout: float = 300) -> dict:
    """Wait for all packaged model services（模型服务）without recording diagnostics."""
    voice = snapshot.get("voice_api_url")
    if not isinstance(voice, str) or not voice.startswith("http://127.0.0.1:"):
        raise QualificationError("voice_api_endpoint_unavailable")
    base = voice.rstrip("/")
    model_names = ("paraformer", "voice_vad", "kws", "smart_turn", "campplus")
    deadline = time.monotonic() + timeout
    states: dict[str, str | None] = {}
    while time.monotonic() < deadline:
        _wait_voice_asr_session(base, timeout=min(30, max(1, deadline - time.monotonic())))
        status, compute = _http_json_at(base, "/api/v1/product/compute-status", timeout=120)
        models = compute.get("models") if status == 200 else None
        if isinstance(models, dict):
            states = {
                name: models.get(name, {}).get("service") if isinstance(models.get(name), dict) else None
                for name in model_names
            }
            # 三项增强是可选能力，停用允许；已启用能力仍必须完成加载与预热。
            states["emotion"] = models.get("emotion", {}).get("service")
            auxiliary = compute.get("auxiliary", {})
            for name in ("dual_pass", "acoustic_events"):
                states[name] = auxiliary.get(name, {}).get("service")
            if (all(states.get(name) == "READY" for name in model_names)
                    and all(states.get(name) in {"READY", "DISABLED"}
                            for name in ("emotion", "dual_pass", "acoustic_events"))):
                _wait_voice_asr_session(base, timeout=5)
                return {"verified": True, "voice_asr_session": "READY", **states}
        time.sleep(.5)
    return {"verified": False, **states}


def _retry_component(app: ManagedApplication, component: str, expected_endpoint: bool,
                     timeout: float, preferred: dict[str, int], blockers: list[socket.socket],
                     already: list[dict]) -> dict | None:
    snapshot = app.snapshot()
    rows = _component_rows(snapshot)
    row = rows.get(component, {})
    endpoint = snapshot.get("endpoints", {}).get(component, {})
    if not expected_endpoint or not isinstance(endpoint, dict) or not endpoint.get("url"):
        return None
    pid = endpoint.get("pid")
    old_port = endpoint.get("port")
    if type(pid) is not int or type(old_port) is not int or pid == app.host_pid:
        raise QualificationError("component_retry_target_invalid")
    if not any(result.get("component") == component for result in already):
        raise QualificationError("component_retry_target_unverified")
    process = psutil.Process(pid)
    if abs(float(process.create_time()) - float(endpoint.get("created_at", 0))) > 2.0:
        raise QualificationError("component_retry_pid_mismatch")
    # Ensure the retry target is a descendant before stopping only this managed component process.
    parent = process
    owned = False
    for _ in range(64):
        current_parent = parent.parent()
        if current_parent is None:
            break
        if current_parent.pid == app.host_pid:
            owned = True
            break
        parent = current_parent
    if not owned:
        raise QualificationError("component_retry_pid_not_owned")
    try:
        process.terminate()
        process.wait(timeout=10)
    except psutil.NoSuchProcess:
        pass
    except Exception as exc:  # noqa: BLE001 - don't expose OS process details.
        raise QualificationError("component_retry_stop_failed") from exc
    # Re-occupy the old ephemeral port before retry; the replacement must publish a different actual address.
    blockers.append(_reserve_port(old_port))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = app.snapshot()
        current_row = _component_rows(current).get(component, {})
        if current_row.get("retry_allowed") is True:
            break
        if current_row.get("state") in {"failed", "blocked"} and current.get("startup_complete") is True:
            break
        time.sleep(.5)
    else:
        raise QualificationError("component_retry_not_allowed")
    before_revision = int(current.get("endpoint_revision", -1))
    status, response = app.request_json("POST", f"/api/v1/application/components/{component}/retry", body={})
    if status != 202 or response.get("ok") is not True:
        raise QualificationError("component_retry_request_rejected")
    new_snapshot = app.wait_component(component, timeout)
    new_row = new_snapshot.get("endpoints", {}).get(component, {})
    if not isinstance(new_row, dict) or not new_row.get("url"):
        raise QualificationError("component_retry_endpoint_missing")
    new_revision = int(new_snapshot.get("endpoint_revision", -1))
    if new_revision <= before_revision:
        raise QualificationError("endpoint_revision_did_not_advance")
    if new_row.get("port") == old_port or new_row.get("url") == endpoint.get("url"):
        raise QualificationError("component_retry_endpoint_did_not_change")
    pipe_table, pipe_revision = app.wait_pipe_revision(new_revision)
    _assert_pipe_matches(new_snapshot.get("endpoints", {}), pipe_table, new_revision, pipe_revision)
    retry_endpoint_results = _actual_ports(new_snapshot.get("endpoints", {}), preferred, app.host_pid)
    if not any(item.get("component") == component and item.get("actual_port") == new_row.get("port")
               for item in retry_endpoint_results):
        raise QualificationError("component_retry_endpoint_identity_invalid")
    dependencies = _verify_dependencies(new_snapshot, analysis_enabled=component == "analysis")
    return {
        "component": component,
            "old_port": old_port,
            "new_port": new_row.get("port"),
        "preferred_port": preferred.get(component, 0),
        "endpoint_revision_before": before_revision,
        "endpoint_revision_after": new_revision,
        "endpoint_changed": True,
        "restart_complete": True,
        "dependency_health": dependencies,
    }


def _write_report(path: Path, value: dict) -> None:
    if not _inside(path, TEST_ROOT.resolve()):
        raise QualificationError("report_path_outside_test_root")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _inside(path.parent.resolve(), TEST_ROOT.resolve()):
        raise QualificationError("report_path_outside_test_root")
    fd, temporary = tempfile.mkstemp(prefix=".qualification-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qualification runner（运行时验收器）：占用首选端口，启动分发版受管 Python，并校验端点表与重连。")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="测试工作区（必定位于 D:\\项目开发\\测试 的独立子目录；含 app/（程序）、runtimes/python（运行环境）、data/（数据））。")
    parser.add_argument("--offline-model-bundle", type=Path,
                        help="可选离线模型资源包（只执行 import（导入），无下载路径；省略时复用已完整导入的 QA（质量验收）工作区）。文件须位于 D:\\项目开发\\测试 内。")
    parser.add_argument("--report", type=Path,
                        help="验收结果 JSON（结构化数据）路径，必须位于 --workspace（测试工作区）内。")
    parser.add_argument("--startup-timeout", type=int, default=2400,
                        help="启动快照超时秒数（默认 2400 秒）。")
    parser.add_argument("--retry-timeout", type=int, default=900,
                        help="单组件重试超时秒数（默认 900 秒）。")
    parser.add_argument("--full-restart", action="store_true",
                        help="另执行一次整程序关闭/重启（完整进程重启）校验。")
    parser.add_argument("--all-models", action="store_true",
                        help="在隔离工作区临时启用 KWS（关键词唤醒）与 CAM++（声纹），验收全部本地模型。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace = None
    report_path = None
    blockers: list[socket.socket] = []
    app = None
    test_file_backups: dict[Path, bytes | None] | None = None
    result = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "error_code": "not_started",
        "blocked_preferred_ports": [],
        "endpoints": [],
        "retries": [],
        "full_restart": None,
        "all_models_enabled": bool(args.all_models),
        "all_model_health": None,
        "test_configuration_restored": None if not args.all_models else False,
        "fields": REPORT_FIELDS,
    }
    try:
        workspace = _safe_workspace(args.workspace)
        report_path = (args.report or (workspace / "qualification/runtime-port-check.json")).resolve()
        if not _inside(report_path, workspace):
            raise QualificationError("report_path_outside_workspace")
        if args.startup_timeout < 30 or args.retry_timeout < 30:
            raise QualificationError("timeout_value_invalid")
        portable, app_root, python = _read_portable(workspace)
        _reject_existing_user_state(workspace, app_root)
        env = _minimal_environment(workspace, app_root, python, portable["workspace_id"])
        imported = _import_offline_bundle(workspace, app_root, python, env, args.offline_model_bundle)
        result["offline_bundle_imported"] = imported
        if args.all_models:
            test_file_backups = {}
            _apply_all_model_overrides(workspace, app_root, test_file_backups)
        preferred = _preferred_ports(app_root)
        for component, port in preferred.items():
            if port > 0:
                blockers.append(_reserve_port(port))
                result["blocked_preferred_ports"].append({"component": component, "port": port})
        command = [str(python), "-B", "-X", "utf8", "-X",
                   "pycache_prefix=" + str(workspace / "cache/python"),
                   str(app_root / "scripts/backend-entry.py"), "voice_terminal.application.launch",
                   "--root", str(app_root), "--python", str(python), "--data-dir", str(workspace / "data"),
                   "--desktop-managed"]
        app = ManagedApplication(command, app_root, env, args.startup_timeout)
        app.start()
        snapshot = app.wait_startup()
        revision = int(snapshot.get("endpoint_revision", -1))
        pipe_table, pipe_revision = app.wait_pipe_revision(revision)
        endpoints = snapshot.get("endpoints")
        if not isinstance(endpoints, dict):
            raise QualificationError("endpoint_table_invalid")
        _assert_pipe_matches(endpoints, pipe_table, revision, pipe_revision)
        result["endpoints"] = _actual_ports(endpoints, preferred, app.host_pid)
        if args.all_models and not any(item["component"] == "speaker" for item in result["endpoints"]):
            raise QualificationError("speaker_endpoint_missing")
        rows = _component_rows(snapshot)
        if rows.get("analysis", {}).get("enabled") is True and not any(
                item["component"] == "analysis" for item in result["endpoints"]):
            raise QualificationError("analysis_endpoint_missing")
        # Retry active model workers using the authenticated, same-origin component API.
        for component in ("asr", "analysis"):
            row = rows.get(component, {})
            endpoint_exists = isinstance(endpoints.get(component), dict) and bool(endpoints[component].get("url"))
            if not endpoint_exists:
                if component in REQUIRED_COMPONENTS or row.get("enabled") is True:
                    raise QualificationError(component + "_endpoint_missing")
                continue
            retry_result = _retry_component(app, component, True, args.retry_timeout,
                                            preferred, blockers, result["endpoints"])
            if retry_result:
                result["retries"].append(retry_result)
        if not result["retries"]:
            raise QualificationError("no_component_retry_was_verified")
        if args.all_models:
            health = _verify_all_model_health(app.snapshot())
            result["all_model_health"] = health
            if health.get("verified") is not True:
                raise QualificationError("all_model_health_not_ready")
        if args.full_restart:
            # Full restart is an optional second phase; previous listeners remain reserved so new origins differ.
            previous_snapshot = app.snapshot()
            previous = {
                key: row.get("port") for key, row in previous_snapshot.get("endpoints", {}).items()
                if isinstance(row, dict) and type(row.get("port")) is int
            }
            app.stop()
            app = None
            for port in set(previous.values()):
                if port not in preferred.values():
                    blockers.append(_reserve_port(port))
            second = ManagedApplication(command, app_root, env, args.startup_timeout)
            app = second
            app.start()
            restarted = app.wait_startup()
            second_revision = int(restarted.get("endpoint_revision", -1))
            second_pipe, second_pipe_revision = app.wait_pipe_revision(second_revision)
            _assert_pipe_matches(restarted.get("endpoints", {}), second_pipe, second_revision, second_pipe_revision)
            second_endpoints = _actual_ports(restarted.get("endpoints", {}), preferred, app.host_pid)
            if any(previous.get(item["component"]) == item["actual_port"] for item in second_endpoints):
                raise QualificationError("full_restart_reused_previous_port")
            result["full_restart"] = {
                "restart_complete": True,
                "previous_instance_ports_not_reused": True,
                "endpoint_revision": second_revision,
                "endpoint_count": len(second_endpoints),
            }
            if args.all_models:
                restart_health = _verify_all_model_health(restarted)
                result["full_restart"]["all_model_health"] = restart_health
                if restart_health.get("verified") is not True:
                    raise QualificationError("all_model_health_not_ready_after_full_restart")
        result["ok"] = True
        result["error_code"] = ""
    except QualificationError as exc:
        result["error_code"] = exc.code
    except Exception as exc:  # noqa: BLE001 - the raw exception can contain secrets or user configuration.
        result["error_code"] = "qualification_internal_error_" + type(exc).__name__
    finally:
        if app is not None:
            try:
                app.stop()
            except Exception:  # noqa: BLE001 - ManagedApplication already attempts graceful shutdown and bounded cleanup.
                pass
            process = app.process
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001 - do not expose process diagnostics.
                    pass
            if process is not None and process.poll() is None:
                result["ok"] = False
                result["error_code"] = "managed_process_stop_failed"
        if workspace is not None and test_file_backups is not None:
            try:
                _restore_test_files(workspace, test_file_backups)
                result["test_configuration_restored"] = True
            except Exception:  # noqa: BLE001 - never disclose configuration bytes or paths.
                result["ok"] = False
                result["error_code"] = "test_configuration_restore_failed"
        for listener in blockers:
            try:
                listener.close()
            except OSError:
                pass
        if workspace is not None and report_path is not None:
            try:
                _write_report(report_path, result)
            except Exception:  # noqa: BLE001 - never print a path outside the allowed test tree.
                result["ok"] = False
                result["error_code"] = "qualification_report_write_failed"
    summary = {"ok": result["ok"], "error_code": result["error_code"],
               "report": str(report_path) if report_path else "", "fields": REPORT_FIELDS}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
