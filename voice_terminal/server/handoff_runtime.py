"""会话交接事务（handoff transaction）与被动静默运行策略。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import logging
import time
from pathlib import Path
from uuid import uuid4

from ..handoff import (ContinuityOrganizer, OrganizedHandoff, PairedArchive,
                       PairedArchiveStore, PendingTransferStore, atomic_json, stable_id)
from ..quiet_hours import QuietHoursConfig, QuietHoursPolicy
from ..shadow_agent.provider import DeepSeekResponsesShadowProvider
from ..core.events import event
from ..dsh import DshShadowAgentSession

logger = logging.getLogger(__name__)


class HandoffRuntimeMixin:
    def _on_passive_playback_blocked(self, playback_key: str) -> None:
        """设备线程仅通知；由事件循环停止对应语音事务，保留实际播放前缀。"""
        output = getattr(self.controller, "output", None)
        loop = getattr(output, "loop", None)
        if loop is None or loop.is_closed():
            return
        def suppress():
            for handle in list(output._handles_by_token.values()):
                if handle.workspace.playback_key == playback_key:
                    output.cancel(handle.speech_id, "passive_quiet")
        loop.call_soon_threadsafe(suppress)

    async def _handoff_failed(self, exc: Exception) -> None:
        await self.bus.publish(event("error", code="HANDOFF_ORGANIZER_FAILED",
            message=f"高强度交接整理失败（{type(exc).__name__}），原会话、归档及待传达材料已保留。",
            recoverable=True))

    def _init_handoff(self) -> None:
        root = self._project_root / ".runtime" / "continuity"
        self.pending_transfer = PendingTransferStore(root / "pending-transfer.json")
        self.pending_transfer.recover_captures()
        self.paired_archives = PairedArchiveStore(root / "archives")
        self._archive_backlog_path = root / "archive-backlog.json"
        try:
            catalog = json.loads(self._archive_backlog_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            catalog = {"pending": [], "consumed": []}
        if isinstance(catalog, list):  # 兼容首次开发版本的待整理归档列表。
            catalog = {"pending": catalog, "consumed": []}
        self._archive_backlog = list(catalog["pending"])
        self._archive_consumed = list(catalog["consumed"])
        # 文件夹提交后、目录清单写入前发生重启：扫描完整归档，补回待整理材料。
        for directory in self.paired_archives.root.glob("*"):
            if (not directory.name.startswith(".") and (directory / "manifest.json").is_file()
                    and directory.name not in self._archive_consumed
                    and directory.name not in self._archive_backlog):
                self._archive_backlog.append(directory.name)
        atomic_json(self._archive_backlog_path,
                    {"pending": self._archive_backlog, "consumed": self._archive_consumed})
        self.quiet_policy = QuietHoursPolicy(self.config.quiet_hours)
        self._quiet_clock_task = None
        self._capture_retries: dict[str, dict] = {}
        self._append_retries: dict[str, dict] = {}

    def _capture_deferred(self, source_id: str, text: str, *, heard_text: str = "") -> str:
        voice_id = self.conversation.conversation_id
        entry_id = stable_id(voice_id, source_id)
        arguments = {"voice_session_id": voice_id, "source_ref": source_id, "heard_text": heard_text}
        try:
            self.pending_transfer.capture(entry_id, text, **arguments)
        except OSError:
            self._capture_retries[entry_id] = {"text": text, **arguments}
            raise
        return entry_id

    def _append_deferred(self, entry_id: str, text: str, unorganized: bool) -> None:
        item = {"text": text, "voice_session_id": self.conversation.conversation_id,
                "source_ref": entry_id, "unorganized": unorganized}
        try:
            self.pending_transfer.append(entry_id, **item)
        except OSError:
            self._append_retries[entry_id] = item

    def _flush_deferred_storage(self) -> None:
        for entry_id, item in list(self._capture_retries.items()):
            self.pending_transfer.capture(entry_id, **item)
            self.pending_transfer.append(entry_id, item["text"], voice_session_id=item["voice_session_id"],
                                         source_ref=item["source_ref"], unorganized=True)
            del self._capture_retries[entry_id]
        for entry_id, item in list(self._append_retries.items()):
            self.pending_transfer.append(entry_id, **item)
            del self._append_retries[entry_id]

    async def apply_quiet_hours(self, settings: dict) -> None:
        quiet = QuietHoursConfig(**settings)
        if quiet.enabled and not self.config.wake.enabled:
            raise ValueError("开启被动静默时间需要先开启 KWS（关键词唤醒）。")
        self.config = replace(self.config, quiet_hours=quiet)
        self.quiet_policy.config = quiet
        await self._refresh_quiet_policy()

    async def _refresh_quiet_policy(self) -> None:
        if not self.state.listening and self.quiet_policy.user_interaction:
            episode = getattr(self.controller, "episode", None)
            output = getattr(self.controller, "output", None)
            if not getattr(episode, "active_request", None) and not getattr(output, "busy", False):
                self.quiet_policy.user_interaction = False
        snapshot = self.quiet_policy.snapshot()
        previous = self.state.current.get("quiet_hours")
        if snapshot != previous:
            self.state.current["quiet_hours"] = snapshot
            if snapshot["passive_quiet"]:
                output = getattr(self.controller, "output", None)
                if output is not None:
                    output.cancel_all("passive_quiet")
            await self.bus.publish(self.snapshot())

    async def _quiet_clock(self) -> None:
        while True:
            try:
                await self._refresh_quiet_policy()
                self._flush_deferred_storage()
            except OSError:
                logger.warning("handoff.storage_retry_pending captures=%d entries=%d",
                               len(self._capture_retries), len(self._append_retries))
            await asyncio.sleep(.25)

    async def _export_paired_archive(self, snapshot: list[dict]) -> PairedArchive:
        self._flush_deferred_storage()
        # 已取消的主模型可能尚未来得及整理队列；归档前将已落盘原文明确纳入槽位快照。
        self.pending_transfer.recover_captures()
        await self._prepare_archive_session()
        session = self.shadow_session
        dsh_id, rows = "", []
        cwd = str(getattr(session, "workspace", self._project_root))
        if session is not None and getattr(session, "_opened", False):
            if not self._cleanup_in_progress:
                state = await session.client.call("session.state", session._envelope(request_id=str(uuid4())))
                if state.get("result") != "accepted" or state.get("ready_for_handoff") is not True:
                    raise RuntimeError("DSH（后台代理）尚未共同静默，保留原材料等待重新整理。")
            dsh_id = session.dsh_session_id
            offset, total = 0, None
            export_id = str(uuid4())
            while True:
                response = await session.client.call("session.export", session._envelope(
                    request_id=str(uuid4()), export_id=export_id, offset=offset, limit=100, total=total))
                if response.get("result") != "accepted" or not isinstance(response.get("records"), list):
                    raise RuntimeError("DSH（后台代理）完整会话导出失败，保留原会话。")
                rows.extend(response["records"])
                received_total = response.get("total")
                if (type(received_total) is not int or received_total < len(rows)
                        or (total is not None and total != received_total)):
                    raise RuntimeError("DSH（后台代理）归档记录范围不一致。")
                total = received_total
                next_offset = response.get("next_offset")
                if next_offset is None:
                    if len(rows) != total:
                        raise RuntimeError("DSH（后台代理）归档记录不完整。")
                    break
                if not isinstance(next_offset, int) or next_offset <= offset:
                    raise RuntimeError("DSH（后台代理）归档分页没有推进。")
                offset = next_offset
        elif session is not None and not hasattr(session, "_opened"):
            # Native shadow（内置后台代理）保留全部已提交记录。
            runtime = getattr(session, "runtime", None)
            if runtime is not None:
                rows = [{"type": "assistant/message", "data": item} for item in
                        getattr(runtime, "archival_trace", [])]
        records = snapshot + [{"kind": entry.kind, "content": entry.content,
                               "metadata": entry.metadata} for entry in self.history.entries]
        records.extend(self.history.delivery_records)
        archive = self.paired_archives.save(
            voice_session_id=self.conversation.conversation_id, dsh_session_id=dsh_id, cwd=cwd,
            voice_records=records, dsh_records=rows,
            captures=self.pending_transfer.captures(self.conversation.conversation_id))
        if archive.archive_id not in self._archive_backlog:
            backlog = [*self._archive_backlog, archive.archive_id]
            atomic_json(self._archive_backlog_path, {"pending": backlog, "consumed": self._archive_consumed})
            self._archive_backlog = backlog
        return archive

    async def _prepare_archive_session(self) -> None:
        """恢复既有 DSH 记录用于导出，不创建空会话或启动旧任务。"""
        session = self.shadow_session
        if not isinstance(session, DshShadowAgentSession) or session._opened:
            return
        mapping = await session.mapping_store.load(self.conversation.conversation_id)
        if mapping is None or mapping.state == "retired":
            return
        envelope = {"voice_session_id": mapping.voice_session_id, "dsh_session_id": mapping.dsh_session_id,
                    "epoch": mapping.epoch, "request_id": str(uuid4())}
        response = await session.client.call("session.state", envelope)
        if response.get("result") == "not_found":
            response = await session.client.call("session.resume", {**envelope, "request_id": str(uuid4()),
                "cwd": str(session.workspace), "archive_only": True})
        if response.get("result") not in {"accepted", "duplicate"}:
            raise RuntimeError("DSH（后台代理）既有会话恢复失败，不能将其视为空会话。")
        self.dsh.bridge_hub.unregister(session)
        session.dsh_session_id, session.epoch = mapping.dsh_session_id, mapping.epoch
        session._opened, session._mapping_checked = True, True
        self.history._shadow_context_epoch = mapping.epoch
        self.dsh.bridge_hub.register(session)

    async def _organize_archive(self, archive: PairedArchive, previous: str, entries: list[dict]) -> OrganizedHandoff:
        cfg = self.config.continuity
        provider_cfg = cfg.provider or self.config.shadow_agent.provider
        key = self._api_key(provider_cfg.api_key_env)
        if not key:
            raise RuntimeError(f"未配置交接整理模型凭据 {provider_cfg.api_key_env}")
        provider = DeepSeekResponsesShadowProvider(
            base_url=provider_cfg.base_url, model=provider_cfg.model, api_key=key,
            timeout_ms=provider_cfg.timeout_ms)
        archive_ids = tuple(self._archive_backlog)
        voice, dsh = list(archive.voice_records), list(archive.dsh_records)
        for archive_id in archive_ids:
            if archive_id == archive.archive_id:
                continue
            directory = self.paired_archives.root / archive_id
            self.paired_archives.require_complete(directory)
            raw = json.loads((directory / "source.json").read_text(encoding="utf-8"))
            reference = {"archive_id": archive_id, "manifest_path": str(directory / "manifest.json"),
                         "voice_path": str(directory / "voice.md"), "dsh_path": str(directory / "dsh.md")}
            voice.extend({"archive": reference, "record": item} for item in raw["voice"])
            dsh.extend({"archive": reference, "record": item}
                       for item in self.paired_archives.filter_dsh(raw["dsh"]))
        material = PairedArchive(archive.archive_id, archive.directory, voice, dsh)
        try:
            result = await ContinuityOrganizer(
                provider, max_document_chars=cfg.max_document_chars,
                max_output_tokens=cfg.max_output_tokens,
                max_context_tokens=cfg.max_context_tokens).organize(material, previous, entries)
            return replace(result, archive=archive, related_archive_ids=archive_ids)
        finally:
            await provider.close()

    async def _summarize_continuity(self, snapshot: list[dict], previous_document: str) -> OrganizedHandoff:
        if self._handoff_marker.exists():
            marker = json.loads(self._handoff_marker.read_text(encoding="utf-8"))
            if marker.get("schema_version") == 2:
                if marker.get("phase") not in {"prepared", "document_saved", "consumed"}:
                    raise RuntimeError("交接恢复阶段无效，保留事务。")
                directory = Path(marker["archive"]["manifest_path"]).parent
                raw = json.loads((directory / "source.json").read_text(encoding="utf-8"))
                archive = PairedArchive(marker["archive"]["archive_id"], directory,
                    raw["voice"], self.paired_archives.filter_dsh(raw["dsh"]))
                return OrganizedHandoff(marker["document"], marker["extracted_items"],
                    marker["consumed_entry_ids"], archive, tuple(marker.get("related_archive_ids", [])))
        archive = await self._export_paired_archive(snapshot)
        entries = self.pending_transfer.snapshot()
        return await self._organize_archive(archive, previous_document, entries)

    async def _commit_continuity_cleanup(self, result: OrganizedHandoff, generation: int) -> bool:
        async with self._session_lock:
            if (generation != self.continuity._generation or self._dsh_busy
                    or self._voice_silence_anchor is None):
                return False
            episode = getattr(self.controller, "episode", None)
            output = getattr(self.controller, "output", None)
            if getattr(episode, "active_request", None) or getattr(output, "busy", False):
                self.continuity.arm(time.time(), self._continuity_snapshot)
                return False
            session = self.shadow_session
            if isinstance(session, DshShadowAgentSession) and session._opened and not self._handoff_marker.exists():
                state = await session.client.call("session.state", session._envelope(request_id=str(uuid4())))
                manifest = json.loads((result.archive.directory / "manifest.json").read_text(encoding="utf-8"))
                if (state.get("result") != "accepted" or state.get("ready_for_handoff") is not True
                        or state.get("record_count") != manifest["dsh_record_count"]):
                    self.continuity.arm(time.time(), self._continuity_snapshot)
                    return False
                if generation != self.continuity._generation:
                    return False
            was_listening = bool(self.state.listening)
            try:
                if self.controller is not None:
                    await self.controller.stop_listening()
                if generation != self.continuity._generation:
                    return False
                if self._handoff_marker.exists():
                    await self._resume_handoff()
                else:
                    await self._commit_handoff(result)
                self._voice_silence_anchor = None
                return True
            finally:
                if was_listening and self.controller is not None and not self._handoff_marker.exists():
                    await self.controller.start_listening()

    async def _commit_handoff(self, result: OrganizedHandoff) -> None:
        marker = {"schema_version": 2, "phase": "prepared",
                  "target_voice_session_id": f"conversation-{uuid4().hex}",
                  "target_dsh_session_id": f"voice-shadow-{uuid4()}",
                  "voice_session_id": self.conversation.conversation_id,
                  "dsh_session_id": (getattr(self.shadow_session, "dsh_session_id", "")
                                     if getattr(self.shadow_session, "_opened", False) else ""),
                  "dsh_epoch": getattr(self.shadow_session, "epoch", 1),
                  "archive": result.archive.reference(), "document": result.document,
                  "extracted_items": result.extracted_items,
                  "related_archive_ids": list(result.related_archive_ids),
                  "consumed_entry_ids": result.consumed_entry_ids}
        atomic_json(self._handoff_marker, marker)
        await self._resume_handoff()

    async def _resume_handoff(self) -> None:
        marker = json.loads(self._handoff_marker.read_text(encoding="utf-8"))
        self._cleanup_in_progress = True
        try:
            if marker.get("schema_version") == 2:
                if marker.get("phase") not in {"prepared", "document_saved", "consumed"}:
                    raise RuntimeError("交接恢复阶段无效，保留事务。")
                for path in ("voice_path", "dsh_path", "manifest_path"):
                    if not Path(marker["archive"][path]).is_file():
                        raise RuntimeError("交接归档文件缺失，保留事务等待恢复。")
                self.paired_archives.require_complete(Path(marker["archive"]["manifest_path"]).parent)
                for archive_id in marker.get("related_archive_ids", []):
                    self.paired_archives.require_complete(self.paired_archives.root / archive_id)
                new_ids = []
                for index, text in enumerate(marker["extracted_items"]):
                    entry_id = stable_id(marker["archive"]["archive_id"], str(index), text)
                    new_ids.append(entry_id)
                    self.pending_transfer.append(entry_id, text, voice_session_id=marker["voice_session_id"],
                        source_ref=marker["archive"]["manifest_path"])
                if marker["phase"] == "prepared":
                    self.continuity._replace_document(self.continuity._bounded(marker["document"]))
                    marker["phase"] = "document_saved"
                    atomic_json(self._handoff_marker, marker)
                elif (not self.continuity.path.is_file()
                      or self.continuity.document != marker["document"]):
                    self.continuity._replace_document(self.continuity._bounded(marker["document"]))
                self.pending_transfer.consume(marker["consumed_entry_ids"] + new_ids)
                marker["phase"] = "consumed"
                atomic_json(self._handoff_marker, marker)
                remaining = [item for item in self._archive_backlog
                             if item not in marker.get("related_archive_ids", [])]
                completed = list(dict.fromkeys([*self._archive_consumed, *marker.get("related_archive_ids", [])]))
                atomic_json(self._archive_backlog_path, {"pending": remaining, "consumed": completed})
                self._archive_backlog = remaining
                self._archive_consumed = completed
            if self.conversation.conversation_id == marker["voice_session_id"]:
                session = self.shadow_session
                if isinstance(session, DshShadowAgentSession) and not session._opened and marker.get("dsh_session_id"):
                    # 归档已提交：即使进程重启丢失内存绑定，也按原标识幂等退役旧会话。
                    session.dsh_session_id, session.epoch = marker["dsh_session_id"], marker.get("dsh_epoch", 1)
                    session._opened = True
                await self._rotate_session(_lock_held=True, _strict_shadow=True,
                    target_voice_session_id=marker.get("target_voice_session_id"),
                    target_dsh_session_id=marker.get("target_dsh_session_id"))
            elif marker.get("schema_version") == 2:
                if self.conversation.conversation_id != marker["target_voice_session_id"]:
                    raise RuntimeError("交接恢复标识与当前会话不匹配，保留事务。")
                self._persist_voice_identity()
                session = self.shadow_session
                if session is not None and hasattr(session, "rotate_voice_session_id"):
                    if not session._opened:
                        session.dsh_session_id = marker["target_dsh_session_id"]
                    self.dsh.bridge_hub.unregister(session)
                    session.rotate_voice_session_id(self.conversation.conversation_id)
                    self.dsh.bridge_hub.register(session)
                if self.llm is not None:
                    self.llm.set_session_id(self.conversation.conversation_id)
                provider = getattr(getattr(self, "shadow_component", None), "provider", None)
                set_session = getattr(provider, "set_session_id", None)
                if callable(set_session):
                    set_session(self.conversation.conversation_id
                                if self.config.llm.provider == "opencode_go" else None)
                if self.controller is not None:
                    self.controller.set_reasoning_mode(self.reasoning_mode)
            self._handoff_marker.unlink(missing_ok=True)
        finally:
            self._cleanup_in_progress = False

    async def reset_session(self, *, _lock_held: bool = False, _strict_shadow: bool = False) -> None:
        # Legacy embedding（旧嵌入调用）不创建新的私有存储。
        if not hasattr(self, "pending_transfer"):
            return await self._rotate_session(_lock_held=_lock_held, _strict_shadow=_strict_shadow)
        if not _lock_held:
            async with self._session_lock:
                return await self.reset_session(_lock_held=True, _strict_shadow=True)
        if self._handoff_marker.exists():
            return await self._resume_handoff()
        self.continuity.user_started()
        self._cleanup_in_progress = True
        was_listening = bool(self.state.listening)
        try:
            if self.controller is not None:
                await self.controller.stop_listening()
                await self.controller.reset_session()
            await self._prepare_archive_session()
            if self.shadow_session is not None:
                cancel = getattr(self.shadow_session, "cancel", None)
                if cancel is not None:
                    await cancel(strict=True)
                else:
                    runtime = self.shadow_session.runtime
                    tasks = [task for task in (runtime.runner, runtime.observer_task) if task is not None]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    runtime.active = False
            self.history.commit_semantic_event("manual_reset_boundary", {
                "text": "手动重置已结束本轮运行；已取消的工作不自动重跑，未完成事项需核对当前用户意图。"})
            self.quiet_policy.user_interaction = False
            result = await self._summarize_continuity(self._continuity_snapshot(), self.continuity.document)
            await self._commit_handoff(result)
        finally:
            self._cleanup_in_progress = False
            if was_listening and self.controller is not None and not self._handoff_marker.exists():
                self.controller.set_reasoning_mode(self.reasoning_mode)
                await self.controller.start_listening()
