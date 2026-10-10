"""交接输入构建与请求调度回归，完整原始归档保持不变。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from voice_terminal.config import AppConfig, ContinuityConfig, DshConfig, PersonaConfig
from voice_terminal.handoff import ContinuityOrganizer, PairedArchiveStore
from voice_terminal.server.websocket import SessionRuntime


def response(document="事项仍待用户确认", *, consumed=()):
    return SimpleNamespace(output=[{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
        "document": document, "extracted_items": [], "consumed_entry_ids": list(consumed),
    }, ensure_ascii=False)}]}])


def archive_store(root):
    return PairedArchiveStore(root / "archives", streaming=False)


def test_real_dsh_call_blocks_and_text_results_filter_noise_but_preserve_failures():
    rows = [
        {"type": "assistant/message", "data": {"message": {"content": [
            {"type": "tool-call", "name": "get_latest_context", "id": "context"},
            {"type": "text", "text": "任务仍需用户确认"},
        ]}}},
        {"type": "tool/call", "data": {"name": "get_latest_context", "callId": "context"}},
        {"type": "tool/result", "data": {"message": {
            "source": {"kind": "tool", "callId": "context"},
            "content": [{"type": "text", "text": "重复完整语音上下文"}],
        }}},
        {"type": "tool/result", "data": {"message": {
            "isError": True, "source": {"kind": "tool", "callId": "context"},
            "content": [{"type": "text", "text": "读取失败"}],
        }}},
        {"type": "agent/inbox/spliced", "data": {"inserted": [
            {"source": {"kind": "plugin:voice-terminal-shadow-bridge"}, "content": "重复注入"},
            {"source": {"kind": "schedule"}, "content": "用户授权的定时任务"},
        ]}},
    ]
    original = json.dumps(rows, ensure_ascii=False)
    result = PairedArchiveStore.filter_dsh(rows)
    assert [row["source_record_index"] for row in result] == [0, 3, 4]
    assert result[0]["data"]["message"]["content"] == [{"type": "text", "text": "任务仍需用户确认"}]
    assert len(result[-1]["data"]["inserted"]) == 1
    assert json.dumps(rows, ensure_ascii=False) == original


def test_14k_semantic_context_uses_one_request_despite_large_execution_trace(tmp_path):
    async def scenario():
        text = "用户事实" * 3500  # text（约 14k 字符的有效输入）。
        voice = [
            {"role": "user", "context_lane": "history", "content": text},
            {"kind": "input", "content": text, "metadata": {"turn_id": "input"}},
        ]
        dsh = [
            {"type": "assistant/message", "data": {"usage": {"output": 80000}, "message": {
                "source": {"kind": "model", "replayState": {"signature": "回放" * 20000}},
                "content": [
                    {"type": "reasoning", "text": "内部思考" * 80000},
                    {"type": "tool-call", "name": "read", "id": "read-file", "arguments": {"path": "任务.txt"}},
                ],
            }}},
            {"type": "tool/call", "data": {"name": "read", "callId": "read-file", "arguments": {"path": "任务.txt"}}},
            {"type": "tool/result", "data": {"message": {"source": {"callId": "read-file"},
                "content": [{"type": "text", "text": "任务结果完整保留"}]}}},
        ]
        archive = archive_store(tmp_path).save(
            voice_session_id="voice", dsh_session_id="dsh", cwd=str(tmp_path),
            voice_records=voice, dsh_records=dsh, captures=[],
        )
        source = archive.directory / "source.json"
        original = source.read_bytes()
        provider = SimpleNamespace(complete=AsyncMock(return_value=response()))
        await ContinuityOrganizer(provider).organize(archive, "旧事项", [])
        provider.complete.assert_awaited_once()
        payload = json.loads(provider.complete.await_args.kwargs["input_items"][0]["content"])
        encoded = json.dumps(payload, ensure_ascii=False)
        assert "segment_notes" not in payload
        assert text in encoded and encoded.count(text) == 1
        assert "任务结果完整保留" in encoded and "read-file" in encoded
        assert "内部思考" not in encoded and "replayState" not in encoded
        assert source.read_bytes() == original
    asyncio.run(scenario())


def test_projection_keeps_legacy_history_when_authoritative_ledger_is_absent(tmp_path):
    archive = archive_store(tmp_path).save(
        voice_session_id="voice", dsh_session_id="", cwd=str(tmp_path),
        voice_records=[{"context_lane": "history", "role": "user", "content": "旧会话唯一记录"}],
        dsh_records=[], captures=[],
    )
    assert PairedArchiveStore.organizer_records(archive)[0]["record"]["content"] == "旧会话唯一记录"


def test_independent_segments_run_in_bounded_parallel_and_keep_source_order(tmp_path):
    async def scenario():
        active, maximum, payloads = 0, 0, []
        async def complete(**kwargs):
            nonlocal active, maximum
            payload = json.loads(kwargs["input_items"][0]["content"])
            payloads.append(payload)
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.005)
                return response("仍需确认")
            finally:
                active -= 1
        archive = archive_store(tmp_path).save(
            voice_session_id="voice", dsh_session_id="", cwd=str(tmp_path),
            voice_records=[{"index": i, "content": "材料" * 160} for i in range(30)],
            dsh_records=[], captures=[],
        )
        await ContinuityOrganizer(SimpleNamespace(complete=complete), max_context_tokens=4096,
                                  max_output_tokens=128).organize(archive, "", [])
        assert maximum == 3
        indexes = [row["record"]["index"] for payload in payloads for row in payload.get("records", [])]
        assert indexes == list(range(30))
        assert "segment_notes" in payloads[-1]
        assert active == 0
    asyncio.run(scenario())


def test_failed_parallel_request_cancels_and_collects_other_requests():
    async def scenario():
        pending, started = set(), asyncio.Event()
        async def complete(**kwargs):
            number = json.loads(kwargs["input_items"][0]["content"])["number"]
            pending.add(number)
            try:
                if number == 0:
                    await started.wait()
                    raise RuntimeError("分段请求失败")
                started.set()
                await asyncio.Event().wait()
            finally:
                pending.remove(number)
        organizer = ContinuityOrganizer(SimpleNamespace(complete=complete))
        with pytest.raises(RuntimeError, match="分段请求失败"):
            await organizer._request_many([{"number": i} for i in range(6)], phase="extract")
        assert not pending
    asyncio.run(scenario())


def test_reset_deadline_releases_lock_and_preserves_archive_document_and_pending(tmp_path):
    async def scenario():
        cfg = AppConfig(
            persona=PersonaConfig(directory=tmp_path / "persona"),
            dsh=replace(DshConfig(), home_dir=tmp_path / "dsh"),
            continuity=ContinuityConfig(organize_timeout_ms=10),
        )
        runtime = SessionRuntime(cfg, runtime_data_dir=tmp_path / "data")
        runtime._restore_or_persist_voice_identity()
        identity = runtime.conversation.conversation_id
        runtime.continuity._replace_document("原事项仍需确认")
        runtime.pending_transfer.append("pending", "未传达结果", voice_session_id=identity, source_ref="result")
        runtime.history.commit_input("input", "用户有效输入")
        cancelled = asyncio.Event()
        async def complete(**_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        provider = SimpleNamespace(complete=complete, close=AsyncMock())
        runtime._api_key = lambda _: "isolated-test-key"
        with patch("voice_terminal.server.handoff_runtime.DeepSeekResponsesShadowProvider", return_value=provider):
            with pytest.raises(RuntimeError, match="超过等待上限"):
                await runtime.reset_session()
        assert cancelled.is_set()
        provider.close.assert_awaited_once()
        assert runtime._session_operation is None and not runtime._session_lock.locked()
        assert runtime.conversation.conversation_id == identity
        assert runtime.continuity.document == "原事项仍需确认"
        assert runtime.pending_transfer.snapshot()[0]["id"] == "pending"
        assert runtime._archive_backlog and not runtime._handoff_marker.exists()
        for archive_id in runtime._archive_backlog:
            PairedArchiveStore.require_complete(runtime.paired_archives.root / archive_id)
    asyncio.run(scenario())
