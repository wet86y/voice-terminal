from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
from uuid import NAMESPACE_URL, uuid5

from ..config import AppConfig
from .vault import CredentialVault, CredentialVaultUnavailable

ModelApplyCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

MODEL_CAPABILITIES = {
    "llm", "tts", "local_kws", "local_asr", "local_vad",
    "local_analysis_endpoint", "local_analysis_refine",
}
LOCAL_CAPABILITIES = MODEL_CAPABILITIES - {"llm", "tts"}
REGISTRY_SCHEMA_VERSION = 1


class ModelRegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(kind: str, identity: str) -> str:
    return f"{kind}_{uuid5(NAMESPACE_URL, f'voice-terminal/{kind}/{identity}').hex}"


def _clean_text(value: Any, name: str, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelRegistryError(f"{name}不能为空。")
    result = value.strip()
    if len(result) > max_length:
        raise ModelRegistryError(f"{name}过长。")
    return result


def _safe_url(value: Any) -> str:
    result = _clean_text(value, "服务地址")
    if not result.startswith(("https://", "http://")):
        raise ModelRegistryError("服务地址必须以 http:// 或 https:// 开始。")
    return result.rstrip("/")


class ModelRegistryService:
    """Secret-safe registry persistence, scanning and provider verification.

    ``registry_path`` stores only non-secret metadata.  An injected vault lets
    unit tests exercise atomic behavior without ever installing keyring.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        registry_path: Path,
        vault: CredentialVault,
        apply_callback: ModelApplyCallback | None = None,
    ) -> None:
        self.config = config
        self.registry_path = registry_path
        self.vault = vault
        self.apply_callback = apply_callback
        self._lock = asyncio.Lock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "providers": [], "models": [], "tts_profiles": [],
            "assignments": {
                "voice_llm_model_id": None, "shadow_llm_model_id": None,
                "status_llm_model_id": None, "tts_profile_id": None,
                "role_settings": {
                    "voice": {"reasoning_effort": "low", "max_context_tokens": 128000},
                    "shadow": {"reasoning_effort": "high", "max_context_tokens": 256000},
                    "status": {"reasoning_effort": "none", "max_context_tokens": 32768},
                },
            },
            "hidden_model_ids": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return self._empty()
        try:
            value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRegistryError(f"模型注册表无法读取：{type(exc).__name__}。") from exc
        if not isinstance(value, dict) or value.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise ModelRegistryError("模型注册表版本不受支持。")
        template = self._empty()
        for key, default in template.items():
            if key not in value or not isinstance(value[key], type(default)):
                value[key] = default
        # Schema v1 predates the third independent LLM role.  It is safe to
        # upgrade shape in memory because no secret or model entry changes.
        if not isinstance(value["assignments"], dict):
            value["assignments"] = deepcopy(template["assignments"])
        else:
            for key, default in template["assignments"].items():
                if key not in value["assignments"]:
                    value["assignments"][key] = deepcopy(default)
            role_settings = value["assignments"].get("role_settings")
            if not isinstance(role_settings, dict):
                value["assignments"]["role_settings"] = deepcopy(template["assignments"]["role_settings"])
            else:
                for role, defaults in template["assignments"]["role_settings"].items():
                    if not isinstance(role_settings.get(role), dict):
                        role_settings[role] = deepcopy(defaults)
                    else:
                        for key, default in defaults.items():
                            role_settings[role].setdefault(key, default)
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".model-registry-", suffix=".json", dir=str(self.registry_path.parent))
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.registry_path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _snapshot(self, registry: dict[str, Any]) -> dict[str, Any]:
        hidden = set(registry["hidden_model_ids"])
        profiles = [deepcopy(item) for item in registry["providers"]]
        # provider records are intentionally constructed without a secret, but
        # this guard makes a future accidental field addition non-exportable.
        for profile in profiles:
            profile.pop("api_key", None)
            profile.pop("secret", None)
        models = []
        for item in registry["models"]:
            public = deepcopy(item)
            public["hidden"] = public["id"] in hidden
            models.append(public)
        return {
            "credential_vault": {"available": self.vault.available, "diagnostic": self.vault.diagnostic},
            "providers": profiles,
            "models": models,
            "tts_profiles": deepcopy(registry["tts_profiles"]),
            "assignments": deepcopy(registry["assignments"]),
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return self._snapshot(self._load())

    def _runtime_imports(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        llm = self.config.llm
        shadow = self.config.shadow_agent.provider
        status = self.config.shadow_agent.status_observer.provider or shadow
        tts = self.config.tts
        providers = [
            {"id": _stable_id("provider", f"runtime/llm/{llm.base_url}"), "name": "主 LLM（现有运行时配置）", "origin": "runtime_config", "protocol": llm.provider, "base_url": llm.base_url, "capabilities": ["llm"], "discovery_capability": "models_endpoint", "credential_ref": None, "credential_state": "externally_managed" if bool(self.config.api_key(llm.api_key_env)) else "missing", "last_validation": None, "last_validated_at": None},
            {"id": _stable_id("provider", f"runtime/tts/{tts.base_url}"), "name": "TTS（现有运行时配置）", "origin": "runtime_config", "protocol": tts.provider, "base_url": tts.base_url, "capabilities": ["tts"], "discovery_capability": "models_endpoint", "credential_ref": None, "credential_state": "externally_managed" if bool(self.config.api_key(tts.api_key_env)) else "missing", "last_validation": None, "last_validated_at": None},
        ]
        llm_provider_id = providers[0]["id"]
        tts_provider_id = providers[1]["id"]
        shadow_provider_id = llm_provider_id
        if (shadow.base_url, shadow.api_key_env) != (llm.base_url, llm.api_key_env):
            shadow_provider_id = _stable_id("provider", f"runtime/shadow/{shadow.base_url}")
            providers.append({"id": shadow_provider_id, "name": "Shadow（现有运行时配置）", "origin": "runtime_config", "protocol": llm.provider, "base_url": shadow.base_url, "capabilities": ["llm"], "discovery_capability": "inherited_or_models_endpoint", "credential_ref": None, "credential_state": "externally_managed" if bool(self.config.api_key(shadow.api_key_env)) else "missing", "last_validation": None, "last_validated_at": None})
        status_provider_id = shadow_provider_id
        if (status.base_url, status.api_key_env) != (shadow.base_url, shadow.api_key_env):
            status_provider_id = _stable_id("provider", f"runtime/status/{status.base_url}")
            providers.append({"id": status_provider_id, "name": "状态汇报（现有运行时配置）", "origin": "runtime_config", "protocol": llm.provider, "base_url": status.base_url, "capabilities": ["llm"], "discovery_capability": "inherited_or_models_endpoint", "credential_ref": None, "credential_state": "externally_managed" if bool(self.config.api_key(status.api_key_env)) else "missing", "last_validation": None, "last_validated_at": None})
        models = [
            {"id": _stable_id("model", f"runtime/voice/{llm.base_url}/{llm.model}"), "name": llm.model, "model_id": llm.model, "provider_id": llm_provider_id, "capability": "llm", "origin": "runtime_config", "verification": "inherited", "health": "unknown", "local_path": None, "fingerprint": None},
            {"id": _stable_id("model", f"runtime/shadow/{shadow.base_url}/{shadow.model}"), "name": f"Shadow · {shadow.model}", "model_id": shadow.model, "provider_id": shadow_provider_id, "capability": "llm", "origin": "runtime_config", "verification": "inherited", "health": "unknown", "local_path": None, "fingerprint": None},
            {"id": _stable_id("model", f"runtime/status/{status.base_url}/{status.model}"), "name": f"状态汇报 · {status.model}", "model_id": status.model, "provider_id": status_provider_id, "capability": "llm", "origin": "runtime_config", "verification": "inherited", "health": "unknown", "local_path": None, "fingerprint": None},
            {"id": _stable_id("model", f"runtime/tts/{tts.base_url}/{tts.model}"), "name": tts.model, "model_id": tts.model, "provider_id": tts_provider_id, "capability": "tts", "origin": "runtime_config", "verification": "inherited", "health": "unknown", "local_path": None, "fingerprint": None},
        ]
        tts_profiles = [{"id": _stable_id("tts-profile", f"runtime/{tts.base_url}/{tts.model}/{tts.voice_id}"), "name": "当前 TTS 配置", "model_id": models[3]["id"], "origin": "runtime_config", "voice_id": tts.voice_id, "emotion_mode": tts.emotion_mode, "default_emotion": tts.default_emotion, "output_format": tts.output_format, "sample_rate": tts.sample_rate, "transport": tts.transport, "scheduling": tts.scheduling, "subtitle_enable": tts.subtitle_enable, "subtitle_type": tts.subtitle_type, "verification": "inherited"}]
        return providers, models, tts_profiles

    def _local_entries(self) -> list[dict[str, Any]]:
        root = self.config.model_dir.resolve()
        known = [
            ("local_kws", "KWS", self.config.kws_dir),
            ("local_asr", "ASR", self.config.asr_model_dir),
            ("local_vad", "VAD", self.config.vad_model_dir),
            ("local_analysis_endpoint", "Smart Turn", root / "analysis" / "smart-turn"),
            ("local_analysis_refine", "SenseVoice", root / "analysis" / "sensevoice"),
        ]
        provider_id = _stable_id("provider", f"local/{root}")
        entries: list[dict[str, Any]] = []
        for capability, label, path in known:
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
            except ValueError:
                continue
            exists = resolved.is_dir()
            fingerprint = None
            if exists:
                digest = hashlib.sha256()
                for item in sorted(resolved.rglob("*")):
                    if item.is_file():
                        stat = item.stat()
                        digest.update(str(item.relative_to(root)).encode("utf-8"))
                        digest.update(str(stat.st_size).encode("ascii"))
                        digest.update(str(stat.st_mtime_ns).encode("ascii"))
                fingerprint = digest.hexdigest()[:24]
            identity = str(resolved).lower()
            entries.append({"id": _stable_id("model", f"local/{capability}/{identity}"), "name": label, "model_id": resolved.name, "provider_id": provider_id, "capability": capability, "origin": "local_scan", "verification": "not_applicable", "health": "healthy" if exists else "missing", "local_path": str(resolved), "fingerprint": fingerprint})
        manifest_path = root / "manifests" / "model-manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_entries = manifest.get("entries", []) if isinstance(manifest, dict) else []
        except (OSError, json.JSONDecodeError):
            manifest_entries = []
        kind_map = {"kws": "local_kws", "asr": "local_asr", "vad": "local_vad"}
        known_paths = {entry["local_path"] for entry in entries}
        for item in manifest_entries:
            if not isinstance(item, dict) or item.get("kind") not in kind_map:
                continue
            candidate = Path(str(item.get("path") or ""))
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
            except ValueError:
                continue
            if str(resolved) in known_paths:
                continue
            capability = kind_map[str(item["kind"])]
            entries.append({"id": _stable_id("model", f"local/{capability}/{str(resolved).lower()}"), "name": str(item.get("name") or resolved.name), "model_id": resolved.name, "provider_id": provider_id, "capability": capability, "origin": "local_scan", "verification": "not_applicable", "health": "healthy" if resolved.is_dir() else "missing", "local_path": str(resolved), "fingerprint": None})
        return entries

    async def scan(self) -> dict[str, Any]:
        async with self._lock:
            registry = self._load()
            providers, runtime_models, tts_profiles = self._runtime_imports()
            root = self.config.model_dir.resolve()
            providers.append({"id": _stable_id("provider", f"local/{root}"), "name": "本地受管模型目录", "origin": "local_scan", "protocol": "managed_directory", "base_url": None, "capabilities": sorted(LOCAL_CAPABILITIES), "discovery_capability": "managed_directory_only", "credential_ref": None, "credential_state": "not_required", "last_validation": None, "last_validated_at": None})
            managed = [item for item in registry["providers"] if item.get("origin") == "product_managed"]
            managed_models = [item for item in registry["models"] if item.get("origin") == "product_managed"]
            managed_tts = [item for item in registry["tts_profiles"] if item.get("origin") == "product_managed"]
            registry["providers"] = providers + managed
            registry["models"] = runtime_models + self._local_entries() + managed_models
            registry["tts_profiles"] = tts_profiles + managed_tts
            self._write(registry)
            return self._snapshot(registry)

    async def hide(self, model_id: str) -> dict[str, Any]:
        async with self._lock:
            registry = self._load()
            model = next((entry for entry in registry["models"] if entry["id"] == model_id), None)
            if model is None:
                raise ModelRegistryError("未找到模型条目。")
            assignments = registry["assignments"]
            tts_profiles = registry["tts_profiles"]
            assigned_ids = {assignments.get("voice_llm_model_id"), assignments.get("shadow_llm_model_id"), assignments.get("status_llm_model_id")}
            assigned_ids.update(profile.get("model_id") for profile in tts_profiles if profile.get("id") == assignments.get("tts_profile_id"))
            if model_id in assigned_ids:
                raise ModelRegistryError("该模型正在被分配使用；请先改用其他条目或解除分配。")
            if model_id not in registry["hidden_model_ids"]:
                registry["hidden_model_ids"].append(model_id)
                self._write(registry)
            return self._snapshot(registry)

    async def restore(self, model_id: str) -> dict[str, Any]:
        async with self._lock:
            registry = self._load()
            registry["hidden_model_ids"] = [item for item in registry["hidden_model_ids"] if item != model_id]
            self._write(registry)
            return self._snapshot(registry)

    async def validate_connection(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Probe only.  Does not retain the secret, provider or models."""
        return await self._probe(request)

    async def _probe(self, request: Mapping[str, Any]) -> dict[str, Any]:
        name = _clean_text(request.get("name"), "连接名称")
        base_url = _safe_url(request.get("base_url"))
        protocol = _clean_text(request.get("protocol"), "协议", max_length=80).lower().replace("-", "_").replace(" ", "_")
        if protocol not in {"openai_compatible", "deepseek", "opencode_go", "responses", "minimax", "manual", "custom"}:
            raise ModelRegistryError("不支持的供应商协议。")
        capability = _clean_text(request.get("capability"), "能力类型", max_length=80)
        if capability not in {"llm", "tts"}:
            raise ModelRegistryError("连接能力类型必须为 llm 或 tts。")
        api_key = _clean_text(request.get("api_key"), "API Key", max_length=4096)
        custom_model_id = str(request.get("model_id") or "").strip()
        discovered = await self._discover_models(base_url, protocol, api_key)
        if not discovered and not custom_model_id:
            return {"ok": False, "name": name, "discovery": "unsupported", "models": [], "requires_custom_model_id": True, "message": "服务未提供可用的模型列表，请输入模型 ID 后执行最小能力测试。"}
        if discovered and not custom_model_id:
            # Discovery is intentionally free of inference.  Do not issue a
            # potentially billable probe for every remote model merely because
            # a provider returned a long list; the product UI asks the user to
            # choose the entry that should enter the verified pool.
            return {"ok": False, "name": name, "discovery": "supported", "models": [], "discovered_models": discovered[:100], "requires_model_id": True, "message": "已发现模型；请选择一个模型后执行最小能力测试。"}
        candidate_ids = [custom_model_id]
        verified: list[str] = []
        failures: list[str] = []
        for model_id in candidate_ids[:20]:
            try:
                if capability == "llm":
                    await self._probe_llm(base_url, protocol, api_key, model_id)
                else:
                    await self._probe_tts(base_url, protocol, api_key, model_id, request)
                verified.append(model_id)
            except ModelRegistryError as exc:
                failures.append(str(exc))
        return {"ok": bool(verified), "name": name, "discovery": "supported" if discovered else "custom", "models": verified, "requires_custom_model_id": not bool(discovered), "message": "验证成功。" if verified else (failures[0] if failures else "没有可验证的模型。")}

    async def _discover_models(self, base_url: str, protocol: str, api_key: str) -> list[str]:
        if protocol in {"manual", "custom"}:
            return []
        suffix = "models" if protocol != "minimax" or base_url.endswith("/v1") else "v1/models"
        url = f"{base_url}/{suffix}"
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        except Exception as exc:
            raise ModelRegistryError(f"无法连接模型列表接口：{type(exc).__name__}。") from exc
        if response.status_code in {404, 405, 501}:
            return []
        if response.status_code >= 400:
            raise ModelRegistryError(f"模型列表接口返回 HTTP {response.status_code}。")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelRegistryError("模型列表接口没有返回 JSON。") from exc
        items = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
        return [str(item.get("id") or item.get("name")).strip() for item in items if isinstance(item, dict) and str(item.get("id") or item.get("name") or "").strip()]

    async def _probe_llm(self, base_url: str, protocol: str, api_key: str, model_id: str) -> None:
        endpoint = "responses" if protocol in {"deepseek", "responses"} else "chat/completions"
        payload: dict[str, Any] = {"model": model_id, "stream": False}
        if endpoint == "responses":
            payload.update({"input": "Reply only OK.", "max_output_tokens": 16})
        else:
            payload.update({"messages": [{"role": "user", "content": "Reply only OK."}], "max_tokens": 16})
        try:
            import httpx  # type: ignore
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"{base_url}/{endpoint}", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, json=payload)
        except Exception as exc:
            raise ModelRegistryError(f"LLM 最小能力测试无法连接：{type(exc).__name__}。") from exc
        if response.status_code >= 400:
            raise ModelRegistryError(f"LLM 最小能力测试返回 HTTP {response.status_code}。")

    async def _probe_tts(self, base_url: str, protocol: str, api_key: str, model_id: str, request: Mapping[str, Any]) -> None:
        if protocol != "minimax":
            raise ModelRegistryError("当前仅支持 MiniMax TTS 的真实最小合成探测；其他 TTS 协议需要适配器。")
        from ..providers.minimax_tts import MiniMaxTtsClient
        tts = MiniMaxTtsClient(base_url=base_url, websocket_path=str(request.get("websocket_path") or "/ws/v1/t2a_v2"), model=model_id, api_key=api_key, voice_id=str(request.get("voice_id") or "female-shaonv"), output_format=str(request.get("output_format") or "pcm"), sample_rate=int(request.get("sample_rate") or 32000), transport=str(request.get("transport") or "websocket_bidi"), scheduling=str(request.get("scheduling") or "direct"), subtitle_enable=False)
        session = tts.session(fresh=True)
        try:
            await session.start()
            await session.send_text("测试。")
            await session.flush()
            async for chunk in session.audio_chunks():
                if chunk:
                    return
            raise ModelRegistryError("TTS 最小合成未返回音频。")
        except ModelRegistryError:
            raise
        except Exception as exc:
            raise ModelRegistryError(f"TTS 最小合成测试失败：{type(exc).__name__}。") from exc
        finally:
            await session.close()
            await tts.close()

    async def create_verified_provider(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not self.vault.available:
            raise CredentialVaultUnavailable(self.vault.diagnostic or "Windows Credential Manager 不可用。")
        probe = await self._probe(request)
        if not probe["ok"]:
            raise ModelRegistryError(str(probe["message"]))
        name = _clean_text(request.get("name"), "连接名称")
        base_url = _safe_url(request.get("base_url"))
        protocol = _clean_text(request.get("protocol"), "协议", max_length=80).lower().replace("-", "_").replace(" ", "_")
        capability = _clean_text(request.get("capability"), "能力类型", max_length=80)
        api_key = _clean_text(request.get("api_key"), "API Key", max_length=4096)
        provider_id = _stable_id("provider", f"managed/{capability}/{name}/{base_url}")
        credential_ref = _stable_id("credential", provider_id)
        async with self._lock:
            registry = self._load()
            self.vault.put(credential_ref, api_key)
            try:
                profile = {"id": provider_id, "name": name, "origin": "product_managed", "protocol": protocol, "base_url": base_url, "capabilities": [capability], "discovery_capability": str(probe["discovery"]), "credential_ref": credential_ref, "credential_state": "stored", "last_validation": "success", "last_validated_at": _now()}
                registry["providers"] = [item for item in registry["providers"] if item.get("id") != provider_id] + [profile]
                entries = [{"id": _stable_id("model", f"managed/{provider_id}/{model_id}/{capability}"), "name": model_id, "model_id": model_id, "provider_id": provider_id, "capability": capability, "origin": "product_managed", "verification": "verified", "health": "healthy", "local_path": None, "fingerprint": None} for model_id in probe["models"]]
                registry["models"] = [item for item in registry["models"] if item.get("provider_id") != provider_id] + entries
                if capability == "tts":
                    settings = request.get("tts_profile") if isinstance(request.get("tts_profile"), Mapping) else {}
                    profile_id = _stable_id("tts-profile", f"managed/{provider_id}/{entries[0]['id']}/{settings.get('voice_id') or 'default'}")
                    registry["tts_profiles"] = [item for item in registry["tts_profiles"] if item.get("id") != profile_id] + [{"id": profile_id, "name": f"{name} 默认 TTS 配置", "model_id": entries[0]["id"], "origin": "product_managed", "voice_id": str(settings.get("voice_id") or request.get("voice_id") or "female-shaonv"), "emotion_mode": str(settings.get("emotion_mode") or "agent"), "default_emotion": str(settings.get("default_emotion") or "calm"), "output_format": str(settings.get("output_format") or request.get("output_format") or "pcm"), "sample_rate": int(settings.get("sample_rate") or request.get("sample_rate") or 32000), "transport": str(settings.get("transport") or request.get("transport") or "websocket_bidi"), "scheduling": str(settings.get("scheduling") or request.get("scheduling") or "direct"), "subtitle_enable": bool(settings.get("subtitle_enable", False)), "subtitle_type": str(settings.get("subtitle_type") or "word_streaming"), "verification": "verified"}]
                self._write(registry)
            except Exception:
                self.vault.delete(credential_ref)
                raise
            return self._snapshot(registry)

    async def apply_assignments(self, assignments: Mapping[str, Any], *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            return {"requires_confirmation": True, "impact": ["当前语音会话", "活动 TTS 播报", "主 LLM", "Shadow Agent", "状态汇报模型", "TTS"], "message": "应用模型分配会中止当前聆听或播报，并立即重启受影响服务。"}
        async with self._lock:
            registry = self._load()
            target = {key: assignments.get(key) for key in ("voice_llm_model_id", "shadow_llm_model_id", "status_llm_model_id", "tts_profile_id")}
            models = {item["id"]: item for item in registry["models"]}
            profiles = {item["id"]: item for item in registry["tts_profiles"]}
            hidden = set(registry["hidden_model_ids"])
            for key in ("voice_llm_model_id", "shadow_llm_model_id", "status_llm_model_id"):
                entry = models.get(target[key])
                if entry is None or entry["capability"] != "llm" or entry["id"] in hidden or entry["verification"] not in {"verified", "inherited"}:
                    raise ModelRegistryError(f"{key} 必须选择未隐藏、已验证的 LLM 条目。")
            tts_profile = profiles.get(target["tts_profile_id"])
            tts_model = models.get(tts_profile.get("model_id")) if tts_profile else None
            if tts_profile is None or tts_model is None or tts_model["capability"] != "tts" or tts_model["id"] in hidden or tts_profile["verification"] not in {"verified", "inherited"}:
                raise ModelRegistryError("tts_profile_id 必须选择未隐藏、已验证的 TTS 配置。")
            if self.apply_callback is None:
                raise ModelRegistryError("当前运行时尚未启用模型热重启适配器。")
            raw_settings = assignments.get("role_settings")
            raw_settings = raw_settings if isinstance(raw_settings, Mapping) else {}
            defaults = self._empty()["assignments"]["role_settings"]
            role_settings: dict[str, dict[str, Any]] = {}
            for role in ("voice", "shadow", "status"):
                item = raw_settings.get(role)
                item = item if isinstance(item, Mapping) else {}
                try:
                    context = int(item.get("max_context_tokens", defaults[role]["max_context_tokens"]))
                except (TypeError, ValueError) as exc:
                    raise ModelRegistryError(f"{role} 上下文上限无效。") from exc
                if not 1024 <= context <= 524288:
                    raise ModelRegistryError(f"{role} 上下文上限必须在 1024 至 524288 token 之间。")
                effort = str(item.get("reasoning_effort", defaults[role]["reasoning_effort"])).lower()
                if role == "status":
                    effort = "none"
                elif effort not in {"low", "high", "max"}:
                    raise ModelRegistryError(f"{role} 思考等级无效。")
                role_settings[role] = {"reasoning_effort": effort, "max_context_tokens": context}
            providers = {item["id"]: item for item in registry["providers"]}

            def runtime_entry(entry: dict[str, Any]) -> dict[str, Any]:
                provider = providers.get(entry["provider_id"])
                if provider is None:
                    raise ModelRegistryError("模型条目缺少供应商配置。")
                value = {"model_id": entry["model_id"], "provider": deepcopy(provider)}
                reference = provider.get("credential_ref")
                if reference:
                    secret = self.vault.get(str(reference))
                    if not secret:
                        raise ModelRegistryError("供应商凭据不可用，请重新验证连接。")
                    value["api_key"] = secret
                return value

            runtime_payload = {
                "voice": {**runtime_entry(models[target["voice_llm_model_id"]]), "settings": role_settings["voice"]},
                "shadow": {**runtime_entry(models[target["shadow_llm_model_id"]]), "settings": role_settings["shadow"]},
                "status": {**runtime_entry(models[target["status_llm_model_id"]]), "settings": role_settings["status"]},
                "tts": {**runtime_entry(tts_model), "profile": deepcopy(tts_profile)},
            }
            result = await self.apply_callback(runtime_payload)
            if not result.get("ok"):
                return {"requires_confirmation": False, "applied": False, "message": result.get("message", "模型服务重启失败，已保留上一份有效分配。"), "assignments": deepcopy(registry["assignments"])}
            target["role_settings"] = role_settings
            registry["assignments"] = target
            self._write(registry)
            return {"requires_confirmation": False, "applied": True, "message": "模型服务已重启，新分配已生效。", "assignments": deepcopy(target), "runtime": result}
