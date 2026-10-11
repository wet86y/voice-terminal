"""Local startup resource checks（启动前本地资源检查）：never resolve or download models."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..resource_distribution import ResourceCatalog, ResourceError
from ..model_installation import inspect_installed_model, model_update_available


def required_model_resources(config: Any) -> dict[str, list[str]]:
    """Return selected resource ids（已选资源编号）for each model-loading role."""
    requirements: dict[str, list[str]] = {
        "asr": ["paraformer-streaming"],
        # Voice（语音服务）的活动检测器始终初始化，KWS（关键词唤醒）按配置选择。
        "voice": ["fsmn-vad"],
    }
    if config.wake.enabled:
        requirements["voice"].append("kws-sherpa-onnx")
    analysis: list[str] = []
    if config.smart_turn.smart_turn:
        analysis.append("smart-turn")
    if config.smart_turn.sensevoice:
        analysis.extend(("sensevoice-small", "fsmn-vad"))
    if analysis:
        requirements["analysis"] = analysis
    if config.speaker.mode != "off" or config.speaker.owner_only_barge_in:
        # model_id（声纹模型标识）：可选模型也必须检查自身资源，不能固定检查默认权重。
        requirements["speaker"] = [getattr(config.speaker, "model_id", "wespeaker-cnceleb-resnet34")]
    return requirements


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _check_model_file(model_root: Path, destination: str, relative: str, expected_size: int) -> str | None:
    base = model_root.resolve(strict=False)
    cursor = model_root
    parts = (*PurePosixPath(destination).parts, *PurePosixPath(relative).parts)
    if _is_reparse(cursor):
        return f"模型目录不能是链接：{relative}。"
    for part in parts:
        cursor = cursor / part
        if _is_reparse(cursor):
            return f"模型文件路径包含链接：{relative}。"
    try:
        if not cursor.resolve(strict=False).is_relative_to(base):
            return f"模型文件路径越出本地模型目录：{relative}。"
    except (OSError, RuntimeError):
        return f"无法核验模型文件路径：{relative}。"
    if not cursor.is_file():
        return f"缺少必需文件：{relative}。"
    try:
        actual_size = cursor.stat().st_size
    except OSError:
        return f"无法读取文件：{relative}。"
    if actual_size != expected_size:
        return f"文件大小不符：{relative}（预期 {expected_size} 字节，实际 {actual_size} 字节）。"
    return None


def inspect_model_resources(
    program_root: Path,
    model_root: Path,
    requirements: Mapping[str, list[str]],
) -> dict[str, str]:
    """Check current resources（核验当前资源）：待更新组件阻止所有模型角色启动。"""
    if not requirements:
        return {}
    try:
        catalog = ResourceCatalog(program_root)
    except ResourceError:
        issue = "程序内固定模型资源清单缺失或无效，请修复应用安装后再启动模型组件。"
        return {role: issue for role in requirements}

    # Pending updates（待更新组件）对整体语音服务生效，包括本次配置未启用的模型。
    pending_updates: dict[str, str] = {}
    for spec in catalog.specs.values():
        if spec.kind == "model" and model_update_available(
                spec, inspect_installed_model(model_root, spec.destination, spec.resource_id)):
            pending_updates[spec.resource_id] = spec.title

    runtime_issue = None
    runtime_path = model_root.parent / "runtimes/python/runtime.json"
    if runtime_path.is_file():
        try:
            import hashlib
            runtime_record = json.loads(runtime_path.read_text("utf-8-sig"))
            digest = hashlib.sha256((program_root / "config/runtime/voice-runtime.lock.json").read_bytes()).hexdigest()
            groups = {"voice-core", "torch-cpu", "onnx-cpu", "sherpa-cpu", "modelscope-download", "onnx-vad"}
            if (not isinstance(runtime_record, dict) or runtime_record.get("schema") != 2
                    or runtime_record.get("lock_sha256") != digest
                    or not isinstance(runtime_record.get("groups"), list)
                    or not groups.issubset(runtime_record["groups"])):
                runtime_issue = "语音运行环境需要更新，请在组件管理完成全部当前运行依赖的配置。"
        except (OSError, UnicodeError, ValueError, TypeError):
            runtime_issue = "语音运行环境登记缺失或无效，请在组件管理完成当前运行依赖的配置。"

    issues: dict[str, str] = {}
    for role, resource_ids in requirements.items():
        details = [f"{title}：组件有更新，请先更新或导入当前版本后再启动整体语音服务。"
                   for title in pending_updates.values()]
        if runtime_issue:
            details.append(runtime_issue)
        for resource_id in resource_ids:
            try:
                spec = catalog.get(resource_id)
            except ResourceError:
                details.append(f"固定资源 {resource_id} 不在程序资源清单中。")
                continue
            if spec.kind != "model":
                details.append(f"固定资源 {resource_id} 不是模型资源。")
                continue
            if resource_id in pending_updates:
                continue
            for item in spec.required_files:
                problem = _check_model_file(model_root, spec.destination, item.path, item.size_bytes)
                if problem:
                    details.append(f"{spec.title}：{problem}")
        if details:
            labels = "；".join(details)
            issues[role] = f"请下载或导入所需本地模型资源后再启动。{labels}"
    return issues


def inspect_dsh_runtime(
    program_root: Path,
    runtime_root: Path,
    *,
    expected_version: str,
    expected_commit: str,
    expected_bridge: str,
) -> str | None:
    """Check local DSH entry points（本地代理入口）before launching its parser."""
    if _is_reparse(runtime_root) or not runtime_root.is_dir():
        return "DSH（代理执行宿主）运行组件缺失，请下载或导入官方 DSH 资源后再启动。"
    metadata_path = runtime_root / "runtime.json"
    manifest_path = runtime_root / "manifest.json"
    if any(_is_reparse(path) or not path.is_file() for path in (metadata_path, manifest_path)):
        return "DSH（代理执行宿主）缺少 runtime.json（运行元数据）或 manifest.json（文件清单），请下载或导入官方 DSH 资源。"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "DSH（代理执行宿主）runtime.json（运行元数据）无法读取，请下载或导入官方 DSH 资源。"
    if not isinstance(metadata, dict):
        return "DSH（代理执行宿主）runtime.json（运行元数据）格式无效，请下载或导入官方 DSH 资源。"

    try:
        lock_path = program_root / "config/dsh/runtime.lock.json"
        if lock_path.is_file():
            lock = json.loads(lock_path.read_text(encoding="utf-8-sig"))
            if lock.get("distribution", {}).get("source_kind") == "npm_package" and metadata.get("source_kind") != "npm_package":
                return "旧 DSH（代理宿主）需要迁移，请在组件管理页点击更新。"
        if metadata.get("source_kind") == "npm_package":
            from ..dsh.distribution import validate_runtime
            validate_runtime(program_root, runtime_root)
            return None
        if metadata.get("source_kind") == "official_desktop":
            from ..dsh.official import load_official_artifact, validate_official_runtime
            artifact = load_official_artifact(program_root)
            if artifact.version != expected_version or artifact.compatibility_commit != expected_commit:
                raise ValueError("应用与运行组件兼容版本不一致")
            validate_official_runtime(runtime_root, artifact, verify_required=False)
            return None

        if (metadata.get("schema") != 1
                or metadata.get("version") != expected_version
                or metadata.get("commit") != expected_commit
                or metadata.get("bridge_protocol") != expected_bridge
                or metadata.get("source_kind") not in (None, "physical_closure")):
            raise ValueError("运行组件版本或桥接协议不匹配")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if (not isinstance(manifest, dict) or manifest.get("schema") != 1
                or manifest.get("kind") != "dsh" or not isinstance(manifest.get("files"), list)):
            raise ValueError("运行组件文件清单无效")
        from ..distribution import safe_path
        required = {
            "runtime.json",
            "package.json",
            str(metadata.get("node") or ""),
            str(metadata.get("entry") or ""),
            str(metadata.get("schedule") or ""),
            "node_modules/@deepseek-ai/dsh/lib/bin.js",
        }
        required.discard("")
        records = {
            item.get("path") for item in manifest["files"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        if not required.issubset(records):
            raise ValueError("运行组件清单缺少必要入口")
        for relative in required:
            path = safe_path(runtime_root, relative)
            if _is_reparse(path) or not path.is_file():
                raise ValueError(f"必要入口缺失：{Path(relative).name}")
        modules = metadata.get("modules")
        if not isinstance(modules, str):
            raise ValueError("运行组件元数据缺少依赖目录")
        modules_path = safe_path(runtime_root, modules)
        if _is_reparse(modules_path) or not modules_path.is_dir():
            raise ValueError("运行组件依赖目录缺失")
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        if metadata.get("source_kind") == "npm_package":
            return f"DSH（代理宿主）尚未就绪：{exc}"
        return "DSH（代理执行宿主）运行入口或兼容元数据不完整，请下载或导入官方 DSH 资源后再启动。"


def inspect_startup_resources(
    program_root: Path,
    model_root: Path,
    requirements: Mapping[str, list[str]],
    *,
    dsh_enabled: bool,
    dsh_runtime: Path,
    dsh_version: str,
    dsh_commit: str,
    dsh_bridge: str,
) -> dict[str, str]:
    """Return role-specific local startup issues（各组件的本地资源缺失说明）。"""
    from ..startup_logging import startup_stage
    with startup_stage("核验本地模型资源"):
        issues = inspect_model_resources(program_root, model_root, requirements)
    if dsh_enabled:
        with startup_stage("核验代理执行宿主组件"):
            issue = inspect_dsh_runtime(
                program_root,
                dsh_runtime,
                expected_version=dsh_version,
                expected_commit=dsh_commit,
                expected_bridge=dsh_bridge,
            )
        if issue:
            issues["dsh"] = issue
    return issues
