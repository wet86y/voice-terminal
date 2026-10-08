from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.config import AppConfig, DshConfig, PersonaConfig, WakeConfig
from voice_terminal.continuity import ContinuityManager
from voice_terminal.audio.player import PcmPlayer
from voice_terminal.dsh.bridge import (DshBridgeClient, DshBridgeError, DshShadowAgentSession,
                                       DshMappingStore, BRIDGE_PROTOCOL_VERSION)
from voice_terminal.handoff import (ContinuityOrganizer, OrganizedHandoff, PairedArchiveStore,
                                    PendingTransferStore)
from voice_terminal.quiet_hours import QuietHoursConfig, QuietHoursPolicy, PassiveQuietSuppressed
from voice_terminal.server.product import ProductSettingsStore, ProductSettingsError, sanitize_runtime_snapshot
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def make_runtime(root: Path) -> SessionRuntime:
    cfg = AppConfig(persona=PersonaConfig(directory=root / "config" / "persona"),
                    dsh=replace(DshConfig(), home_dir=root / ".local" / "dsh" / "home"))
    runtime = SessionRuntime(cfg, runtime_data_dir=root / ".local")
    runtime._restore_or_persist_voice_identity()  # 模拟 initialize（初始化）的身份恢复，不启动设备。
    return runtime


def make_archive(root: Path, records: list[dict] | None = None):
    return PairedArchiveStore(root / "archives").save(voice_session_id="voice-a", dsh_session_id="",
        cwd=str(root), voice_records=records or [{"role": "user", "content": "等待确认发布。"}],
        dsh_records=[], captures=[])


def response(value: dict):
    return SimpleNamespace(output=[{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value, ensure_ascii=False)}]}])


@pytest.mark.parametrize("hour,minute,expected", [(22,59,False),(23,0,True),(0,0,True),(6,59,True),(7,0,False)])
def test_quiet_hours_boundaries_and_user_window(hour, minute, expected):
    policy = QuietHoursPolicy(QuietHoursConfig(True))
    now = datetime(2026,10,2,hour,minute)
    assert policy.passive(now) is expected
    policy.user_interaction = True
    assert not policy.passive(now)
    policy.user_interaction = False
    assert policy.passive(now) is expected
    policy.config = QuietHoursConfig(False)
    assert not policy.passive(now)


def test_quiet_hours_daytime_and_invalid_settings(tmp_path):
    policy = QuietHoursPolicy(QuietHoursConfig(True, "09:00", "17:00"))
    assert policy.passive(datetime(2026,10,2,9))
    assert not policy.passive(datetime(2026,10,2,17))
    for args in [(True,"23:00","23:00"),(True,"24:00","07:00"),(1,"23:00","07:00")]:
        with pytest.raises(ValueError):
            QuietHoursConfig(*args)
    config = AppConfig(persona=PersonaConfig(directory=tmp_path / "persona"))
    store = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
    with pytest.raises(ProductSettingsError, match="KWS"):
        store.save_quiet_hours({"enabled":True,"start":"23:00","end":"07:00"})
    store._settings["wake"]["enabled"] = True
    assert store.save_quiet_hours({"enabled":True,"start":"23:00","end":"07:00"})["quiet_hours"]["enabled"]
    with pytest.raises(ProductSettingsError, match="先关闭"):
        asyncio.run(store.save_wake({"enabled":False}))
    restored = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
    assert restored.settings_only()["quiet_hours"]["enabled"]


def test_slot_accumulates_idempotently_and_survives_concurrent_append(tmp_path):
    path = tmp_path / "pending.json"
    store = PendingTransferStore(path)
    for entry_id in ["a","b","a"]:
        store.append(entry_id, f"等待答复{entry_id}", voice_session_id="voice-a", source_ref=entry_id)
    frozen = store.snapshot()
    store.append("c", "整理期间的新信息", voice_session_id="voice-a", source_ref="c")
    store.consume([item["id"] for item in frozen])
    store = PendingTransferStore(path)
    assert [item["id"] for item in store.snapshot()] == ["c"]
    assert not store.append("a", "重试不能复活已消费条目", voice_session_id="voice-a", source_ref="a")
    store.capture("d", "原文", voice_session_id="voice-a", source_ref="d", heard_text="已播放前缀")
    store = PendingTransferStore(path)
    store.recover_captures()
    assert store.snapshot()[-1]["unorganized"]
    assert store.captures("voice-a")[0]["heard_text"] == "已播放前缀"


def test_slot_write_failure_keeps_memory_and_disk(tmp_path):
    store = PendingTransferStore(tmp_path / "pending.json")
    store.append("a", "事实", voice_session_id="v", source_ref="s")
    with patch("voice_terminal.handoff.os.replace", side_effect=OSError("disk")):
        with pytest.raises(OSError):
            store.consume(["a"])
    assert store.snapshot()[0]["id"] == "a"
    assert PendingTransferStore(store.path).snapshot() == store.snapshot()


def test_full_paired_archive_immutable_and_long_tool_attachment(tmp_path):
    store = PairedArchiveStore(tmp_path / "archives")
    rows = [{"seq":i,"type":"assistant/message","data":{"text":str(i)}} for i in range(400)]
    rows += [{"seq":400,"type":"user/message","data":{"source":{"kind":"plugin:voice-terminal-shadow-bridge"},"text":"重复注入"}},
             {"seq":401,"type":"tool/result","data":{"content":"工具结果" * 20000}}]
    kwargs = dict(voice_session_id="v", dsh_session_id="d", cwd=str(tmp_path),
                  voice_records=[{"role":"user","content":"原话与修正"}], dsh_records=rows,
                  captures=[{"text":"尚未播放正文","heard_text":"已经播放前缀"}])
    archive = store.save(**kwargs)
    manifest = json.loads((archive.directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dsh_record_count"] == 402
    assert "重复注入" not in (archive.directory / "dsh.md").read_text(encoding="utf-8")
    assert len(list(archive.directory.glob("dsh-*.txt"))) == 1
    assert json.loads((archive.directory / "source.json").read_text(encoding="utf-8"))["dsh"] == rows
    before = {path.name:path.read_bytes() for path in archive.directory.iterdir()}
    assert store.save(**kwargs).archive_id == archive.archive_id
    assert {path.name:path.read_bytes() for path in archive.directory.iterdir()} == before
    assert not make_archive(tmp_path / "no-dsh").dsh_records


def test_archive_filters_mechanical_control_blocks_but_preserves_errors():
    rows = [
        {"type":"assistant/message","data":{"message":{"content":[{"type":"tool_call","name":"get_latest_context","id":"c"}]}}},
        {"type":"tool/call","data":{"name":"get_latest_context","callId":"c"}},
        {"type":"tool/result","data":{"message":{"content":[{"type":"tool_result","toolCallId":"c","content":[]}]}}},
        {"type":"tool/result","data":{"message":{"content":[{"type":"tool_result","toolCallId":"c","isError":True,"content":[{"type":"text","text":"上下文读取失败"}]}]}}},
    ]
    original = json.dumps(rows)
    filtered = PairedArchiveStore.filter_dsh(rows)
    assert len(filtered) == 1 and filtered[0]["source_record_index"] == 3
    assert json.dumps(rows) == original


def test_organizer_high_snapshot_validation_and_compress_retry(tmp_path):
    async def scenario():
        provider = SimpleNamespace(complete=AsyncMock(side_effect=[
            response({"document":"长"*30,"extracted_items":[],"consumed_entry_ids":["a"]}),
            response({"document":"等待用户确认","extracted_items":["待确认"],"consumed_entry_ids":["a"]})]))
        result = await ContinuityOrganizer(provider,max_document_chars=20).organize(
            make_archive(tmp_path), "旧事项仍未解决", [{"id":"a","text":"待确认"}])
        assert result.document == "等待用户确认"
        assert provider.complete.await_count == 2
        assert all(call.kwargs["reasoning_effort"] == "high" and call.kwargs["max_output_tokens"] == 16384
                   for call in provider.complete.await_args_list)
        provider.complete = AsyncMock(return_value=response({"document":"内容","extracted_items":[],"consumed_entry_ids":["not-in-snapshot"]}))
        with pytest.raises(ValueError, match="快照"):
            await ContinuityOrganizer(provider).organize(make_archive(tmp_path), "", [])
        provider.complete = AsyncMock(side_effect=RuntimeError("high unsupported"))
        with pytest.raises(RuntimeError, match="unsupported"):
            await ContinuityOrganizer(provider).organize(make_archive(tmp_path), "", [])
    asyncio.run(scenario())


def test_consumed_slot_remains_in_previous_document_across_later_handoffs(tmp_path):
    async def scenario():
        documents = [
            "报告已生成，结果尚未向用户传达。发布任务尚未执行，等待用户确认。",
            "报告结果已实际传达。发布任务尚未执行，等待用户确认。",
            "发布任务尚未执行，等待用户确认。",
        ]
        payloads = []
        async def complete(**kwargs):
            payload = json.loads(kwargs["input_items"][0]["content"])
            payloads.append(payload)
            return response({"document": documents[len(payloads) - 1], "extracted_items": [],
                             "consumed_entry_ids": [item["id"] for item in payload["pending_entries"]]})
        organizer = ContinuityOrganizer(SimpleNamespace(complete=complete))
        runtime = make_runtime(tmp_path)
        runtime.pending_transfer.append("a", documents[0], voice_session_id="v", source_ref="s")
        histories = [
            [{"role": "assistant", "content": "报告已生成；静默期间未播放。"}],
            [{"role": "assistant", "content": "报告已生成。", "actually_heard": True}],
            [{"role": "user", "content": "稍后再确认发布。"}],
        ]
        for index, records in enumerate(histories):
            # 模拟每轮重新加载服务；槽位已消费也必须从持久文档承接旧事项。
            runtime = make_runtime(tmp_path)
            runtime._rotate_session = AsyncMock()
            result = await organizer.organize(make_archive(tmp_path, records),
                                              runtime.continuity.document, runtime.pending_transfer.snapshot())
            await runtime._commit_handoff(result)
            assert runtime.continuity.document == documents[index]
            assert not runtime.pending_transfer.snapshot()
        assert [payload["previous_document"] for payload in payloads] == ["", *documents[:2]]
        assert [len(payload["pending_entries"]) for payload in payloads] == [1, 0, 0]
        assert payloads[1]["records"][0]["record"]["actually_heard"] is True
        assert payloads[2]["records"][0]["record"]["content"] == "稍后再确认发布。"
    asyncio.run(scenario())


def test_pcm_exit_suppresses_queue_without_losing_heard_prefix():
    permitted = [True]
    blocked = []
    player = PcmPlayer(sample_rate=1000, channels=1, output_device=None,
                       playback_permission=lambda: permitted[0], on_policy_blocked=blocked.append)
    player.allow("old")
    player.enqueue("old", b"\x01\x00" * 1000)
    player._callback(bytearray(400), 200, None, None)
    permitted[0] = False
    out = bytearray(400)
    player._callback(out, 200, None, None)
    assert not any(out)
    assert player.progress("old")["played_ms"] == 200
    assert player.progress("old")["buffered_ms"] == 800
    assert blocked == ["old"]
    # 用户唤醒后，旧事务仍须结算，不能顺势重新播放。
    permitted[0] = True
    player._callback(out, 200, None, None)
    assert not any(out)
    frozen = player.stop_and_snapshot("old")
    assert frozen.played_ms == 200
    player.allow("new")
    player.enqueue("new", b"\x02\x00" * 200)
    player._callback(out, 200, None, None)
    assert any(out)
    assert player.progress("new")["played_ms"] == 200


def test_tts_gate_blocks_before_provider_submission():
    from tests.test_speech_admission import _output_harness
    async def scenario():
        output, handle, tts, player = _output_harness("playback_window", 500)
        output.runtime.should_defer_speech = lambda: True
        async def deltas():
            yield "静默内容"
        with pytest.raises(PassiveQuietSuppressed):
            await output._play_stream(handle.workspace, handle.workspace.token, deltas(),
                                     speech_id=handle.speech_id, handle=handle)
        assert not tts.session_instance.sent
        assert not player.enqueued
    asyncio.run(scenario())


@pytest.mark.parametrize("validated", [False, True])
def test_held_stream_only_captures_complete_validated_output(validated):
    from tests.test_speech_admission import _output_harness
    async def scenario():
        output, old, tts, player = _output_harness("playback_window", 500)
        output.handle = None
        output.runtime.should_defer_speech = lambda: True
        captured = []
        output.runtime.defer_speech = lambda *args, **kwargs: captured.append((args, kwargs))
        output.runtime.user_speaking = True
        output.controller._on_model_speech_accepted = AsyncMock()
        player.stop_and_snapshot = lambda *_: None
        handle = await output.begin(("episode","request-quiet"), "turn-quiet")
        await handle.feed("完成了，还需要用户确认。")
        await handle.finish()
        handle.envelope_validated = validated
        output.cancel_all("passive_quiet")
        await asyncio.gather(handle.task, return_exceptions=True)
        assert bool(captured) is validated
        if validated:
            assert len(captured) == 1 and captured[0][0][1] == handle.text
        assert output.controller._on_model_speech_accepted.await_count == 0
        assert not tts.session_instance.sent
    asyncio.run(scenario())


def test_quiet_hours_api_requires_effective_kws_and_applies_immediately(tmp_path):
    import httpx
    from voice_terminal.server.websocket import create_app
    async def scenario():
        runtime = make_runtime(tmp_path)
        store = ProductSettingsStore(path=tmp_path / "settings.json", config=runtime.config)
        store._settings["wake"]["enabled"] = True
        runtime.product_settings = store
        runtime.quiet_policy.within_hours = lambda now=None: runtime.quiet_policy.config.enabled
        app = create_app(runtime, product_settings=store)
        body = {"enabled":True,"start":"23:00","end":"07:00"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            assert (await client.put("/api/v1/product/settings/quiet-hours",json=body)).status_code == 422
            assert not store.settings_only()["quiet_hours"]["enabled"]
            runtime.config = replace(runtime.config,wake=replace(runtime.config.wake,enabled=True))
            saved = await client.put("/api/v1/product/settings/quiet-hours",json=body)
            assert saved.status_code == 200 and saved.json()["effective_from"] == "immediate"
            assert runtime.quiet_policy.passive()
            assert runtime.snapshot().payload["quiet_hours"]["passive_quiet"]
            assert (await client.put("/api/v1/product/settings/wake",json={"enabled":False})).status_code == 422
            assert (await client.put("/api/v1/product/settings/quiet-hours",json={**body,"enabled":False})).status_code == 200
            assert not runtime.quiet_policy.passive()
    asyncio.run(scenario())


def test_only_kws_marks_user_interaction_and_silence_expiry_clears_it():
    from tests.test_kws_always_on import _ownership_controller
    async def scenario():
        controller, returned = _ownership_controller()
        policy = QuietHoursPolicy(QuietHoursConfig(True))
        policy.within_hours = lambda now=None: True
        controller.quiet_policy = policy
        controller.on_user_activity = None
        controller.activate_asr_for_model_speech = AsyncMock(return_value=True)
        controller.episode.arm_silence_window = AsyncMock()
        controller.episode.schedule_system_check = AsyncMock()
        await controller._on_model_speech_accepted()
        assert policy.passive()
        await controller._on_kws_activated("wake-a", 4)
        assert policy.user_interaction
        assert not policy.passive()
        await controller._final_system_check_settled({"generation":8})
        await asyncio.sleep(.15)
        assert returned == [4]
        assert policy.passive()
    asyncio.run(scenario())


def test_dsh_full_export_accepts_large_single_record_without_changing_other_limits():
    raw = json.dumps({"protocol_version":BRIDGE_PROTOCOL_VERSION, "result":"accepted",
                      "records":[{"type":"tool/result", "data":{"text":"x"*2200000}}]}).encode()
    client = DshBridgeClient(origin="http://127.0.0.1:3081", token="test-token"*8, timeout_ms=1000)
    with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
        assert len(client._request_sync("POST", "session.export", {})["records"][0]["data"]["text"]) == 2200000
    with patch("urllib.request.urlopen", return_value=io.BytesIO(raw)):
        with pytest.raises(DshBridgeError, match="exceeds"):
            client._request_sync("POST", "session.context", {})


def test_paired_export_pages_all_records_and_refuses_partial_reads(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        rows = [{"type":"assistant/message", "data":{"text":str(i)}} for i in range(401)]
        export_ids = []
        async def call(operation, payload):
            if operation == "session.state":
                return {"result":"accepted", "ready_for_handoff":True}
            export_ids.append(payload["export_id"])
            offset = payload["offset"]
            return {"result":"accepted", "records":rows[offset:offset+100], "total":len(rows),
                    "next_offset":offset+100 if offset+100<len(rows) else None}
        runtime.shadow_session = SimpleNamespace(_opened=True, dsh_session_id="dsh-old", workspace=tmp_path,
            client=SimpleNamespace(call=call), _envelope=lambda **kwargs: kwargs)
        archive = await runtime._export_paired_archive([])
        assert len(archive.dsh_records) == 401
        assert len(export_ids) == 5 and len(set(export_ids)) == 1
        runtime.shadow_session.client.call = AsyncMock(return_value={"result":"not_found"})
        with pytest.raises(RuntimeError, match="保留"):
            await runtime._export_paired_archive([])
        assert len(runtime._archive_backlog) == 1
    asyncio.run(scenario())


def test_dsh_cancellation_releases_event_lock_while_waiting_for_final_records(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        session = None
        async def call(operation, payload):
            # 远端结算期间仍可能回传；若持有事件锁，此探针会超时。
            async def callback():
                async with session._event_lock:
                    assert session.activation_id is None
            await asyncio.wait_for(callback(), .2)
            return {"result":"accepted"}
        session = DshShadowAgentSession(client=SimpleNamespace(call=call),
            mapping_store=DshMappingStore(tmp_path / "mapping.json"),
            context_source=runtime.shadow_context, voice_session_id=runtime.conversation.conversation_id,
            workspace=tmp_path)
        session._opened, session.activation_id = True, "old-activation"
        await session.cancel(strict=True)
        await session.reset(strict=True)
        assert not session._opened
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["prepared", "document_saved", "consumed"])
def test_each_commit_phase_resumes_and_restores_document_before_consumption(tmp_path, phase):
    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.pending_transfer.append("a", "待确认", voice_session_id="v", source_ref="s")
        result = make_archive(tmp_path)
        marker = {"schema_version":2,"phase":phase, "voice_session_id":runtime.conversation.conversation_id,
                  "target_voice_session_id":"conversation-target", "target_dsh_session_id":"dsh-target",
                  "archive":result.reference(), "document":"仍待用户答复，详情见归档。",
                  "extracted_items":["尚未确认问题"], "consumed_entry_ids":["a"], "related_archive_ids":[]}
        runtime._handoff_marker.parent.mkdir(parents=True,exist_ok=True)
        runtime._handoff_marker.write_text(json.dumps(marker),encoding="utf-8")
        runtime._rotate_session = AsyncMock()
        await runtime._resume_handoff()
        assert runtime.continuity.path.is_file()
        assert runtime.continuity.document == marker["document"]
        assert not runtime.pending_transfer.snapshot()
        assert runtime._rotate_session.await_count == 1
        # 身份已经轮换但删除恢复标记失败：只补齐新身份，不能再轮换。
        runtime.conversation.conversation_id = marker["target_voice_session_id"]
        runtime._handoff_marker.write_text(json.dumps(marker),encoding="utf-8")
        runtime.pending_transfer.append("b", "期间新增",voice_session_id="new",source_ref="new")
        await runtime._resume_handoff()
        assert runtime._rotate_session.await_count == 1
        assert [row["id"] for row in runtime.pending_transfer.snapshot()] == ["b"]
    asyncio.run(scenario())


def test_normalizer_failure_keeps_raw_once_and_does_not_replay_actions(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        # 单一主模型运行对象：直接验证恢复路径，不启动音频设备。
        model = AgentEpisodeRuntime(SimpleNamespace(), SimpleNamespace(),SimpleNamespace(),AsyncMock(),
                                    conversation=runtime.conversation,ledger=runtime.history)
        model.deferred_capture, model.deferred_append = runtime._capture_deferred, runtime._append_deferred
        model._wake = lambda *_: None
        async def broken(*_, **__):
            raise RuntimeError("provider unavailable")
            yield
        model._provider_stream = broken
        model.defer_speech("same-output", "尚未播放的事实", heard_text="已播放")
        model.defer_speech("same-output", "尚未播放的事实", heard_text="已播放")
        await model._normalize_deferred_outputs()
        entries = runtime.pending_transfer.snapshot()
        assert len(entries) == 1 and entries[0]["unorganized"]
        assert entries[0]["text"] == "尚未播放的事实"
        assert runtime.pending_transfer.captures(runtime.conversation.conversation_id)[0]["heard_text"] == "已播放"
    asyncio.run(scenario())


def test_organizer_splits_continuous_history_without_tail_cut(tmp_path):
    async def scenario():
        payloads = []
        async def complete(**kwargs):
            payloads.append(json.loads(kwargs["input_items"][0]["content"]))
            return response({"document":"任务仍等待确认","extracted_items":[],"consumed_entry_ids":[]})
        rows = [{"index":i,"content":str(i)+"材料"*160} for i in range(50)]
        await ContinuityOrganizer(SimpleNamespace(complete=complete),max_context_tokens=4096,max_output_tokens=128).organize(
            make_archive(tmp_path,rows), "", [])
        extracted = [item["record"]["index"] for payload in payloads for item in payload.get("records",[])]
        assert extracted == list(range(50))
        assert "segment_notes" in payloads[-1]
    asyncio.run(scenario())


def test_organizer_segments_large_pending_text_and_never_clips_document(tmp_path):
    async def scenario():
        payloads = []
        async def complete(**kwargs):
            payloads.append(json.loads(kwargs["input_items"][0]["content"]))
            return response({"document":"仍待确认", "extracted_items":[], "consumed_entry_ids":["slot-a"]})
        slot = {"id":"slot-a", "text":"long_pending_text"*1000}
        result = await ContinuityOrganizer(SimpleNamespace(complete=complete), max_context_tokens=8192,
                                          max_output_tokens=128).organize(make_archive(tmp_path), "", [slot])
        fragments = [item["record_fragment"] for payload in payloads for item in payload.get("records", [])
                     if item.get("origin") == "pending_transfer"]
        assert json.loads("".join(fragments))["record"] == slot
        assert result.consumed_entry_ids == ["slot-a"]
        assert payloads[-1]["pending_entries"] == [{"id":"slot-a"}]
        provider = SimpleNamespace(complete=AsyncMock(return_value=response(
            {"document":"超长"*100,"extracted_items":[],"consumed_entry_ids":[]})))
        with pytest.raises(ValueError,match="超长"):
            await ContinuityOrganizer(provider,max_document_chars=10).organize(make_archive(tmp_path), "", [])
        assert provider.complete.await_count == 2
    asyncio.run(scenario())


def test_commit_failure_and_recovery_preserve_new_entries(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.pending_transfer.append("a", "待传达", voice_session_id="v", source_ref="r")
        archive = make_archive(tmp_path)
        result = OrganizedHandoff("文档已吸收信息",["新提取的待确认"],["a"],archive)
        runtime._rotate_session = AsyncMock(side_effect=RuntimeError("retire unavailable"))
        with pytest.raises(RuntimeError):
            await runtime._commit_handoff(result)
        assert runtime.continuity.document == result.document
        assert not runtime.pending_transfer.snapshot()
        runtime.pending_transfer.append("b", "事务期间新增", voice_session_id="v", source_ref="new")
        marker = json.loads(runtime._handoff_marker.read_text(encoding="utf-8"))
        recovered = make_runtime(tmp_path)
        assert recovered.conversation.conversation_id == marker["voice_session_id"]
        recovered._rotate_session = AsyncMock()
        await recovered._resume_handoff()
        assert [item["id"] for item in recovered.pending_transfer.snapshot()] == ["b"]
        assert not recovered._handoff_marker.exists()
        assert recovered._rotate_session.await_count == 1
        assert recovered._rotate_session.await_args.kwargs["target_voice_session_id"] == marker["target_voice_session_id"]
    asyncio.run(scenario())


def test_document_disk_failure_does_not_consume_and_memory_stays_old(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.pending_transfer.append("a","等待",voice_session_id="v",source_ref="r")
        runtime.continuity.document = "旧文档"
        result = OrganizedHandoff("新文档", [], ["a"], make_archive(tmp_path))
        runtime._handoff_marker.parent.mkdir(parents=True, exist_ok=True)
        with patch("voice_terminal.continuity.os.replace", side_effect=OSError("disk")):
            # os（系统模块）共享：先写恢复标记再仅模拟文档替换失败。
            runtime._handoff_marker.write_text(json.dumps({"schema_version":2,"phase":"prepared",
                "voice_session_id":runtime.conversation.conversation_id,"archive":result.archive.reference(),
                "document":result.document,"extracted_items":[],"consumed_entry_ids":["a"]}),encoding="utf-8")
            with pytest.raises(OSError):
                await runtime._resume_handoff()
        assert runtime.continuity.document == "旧文档"
        assert runtime.pending_transfer.snapshot()[0]["id"] == "a"
    asyncio.run(scenario())


def test_missing_archive_file_prevents_document_reference_and_consumption(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.pending_transfer.append("a", "未传达",voice_session_id="v",source_ref="r")
        archive = make_archive(tmp_path)
        (archive.directory / "source.json").unlink()
        runtime._rotate_session = AsyncMock()
        with pytest.raises(RuntimeError, match="归档文件缺失"):
            await runtime._commit_handoff(OrganizedHandoff("新文档引用归档", [], ["a"], archive))
        assert not runtime.continuity.document
        assert runtime.pending_transfer.snapshot()[0]["id"] == "a"
        assert runtime._handoff_marker.exists()
        assert not runtime._rotate_session.called
    asyncio.run(scenario())


def test_auto_result_discarded_on_activity_and_manual_failure_preserves_history(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        archive = make_archive(tmp_path)
        assert not await runtime._commit_continuity_cleanup(OrganizedHandoff("过时",[],[],archive), -1)
        runtime.history.commit_input("turn-a", "这个问题暂缓")
        original = runtime._continuity_snapshot()
        runtime._organize_archive = AsyncMock(side_effect=RuntimeError("high unavailable"))
        with pytest.raises(RuntimeError):
            await runtime.reset_session()
        assert runtime._continuity_snapshot()[:len(original)] == original
        assert any("已取消的工作不自动重跑" in row["content"] for row in runtime._continuity_snapshot())
        assert runtime._archive_backlog
        assert not runtime._handoff_marker.exists()
        assert runtime.conversation.conversation_id == json.loads(
            (runtime.paired_archives.root / runtime._archive_backlog[0] / "manifest.json").read_text(encoding="utf-8"))["voice_session_id"]
    asyncio.run(scenario())


def test_private_slot_does_not_enter_voice_or_dsh_projection(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.pending_transfer.append("private", "PRIVATE_SLOT_SENTINEL",voice_session_id="v",source_ref="r")
    assert "PRIVATE_SLOT_SENTINEL" not in json.dumps(runtime._continuity_snapshot())
    assert "PRIVATE_SLOT_SENTINEL" not in str(runtime.shadow_context.shadow_context_snapshot())
    assert "PRIVATE_SLOT_SENTINEL" not in json.dumps(sanitize_runtime_snapshot(runtime.snapshot().payload))


def test_clear_archives_both_sessions_and_context_without_organizer_or_later_revival(tmp_path):
    from voice_terminal.shadow_agent.contracts import DurableVoiceEvent

    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.continuity._replace_document("旧衔接文档")
        runtime.history.commit_input("old-input", "旧会话正文")
        old_voice_id = runtime.conversation.conversation_id
        runtime.pending_transfer.capture("old-slot", "旧待传达材料", voice_session_id=old_voice_id, source_ref="old-output")
        runtime.pending_transfer.recover_captures()
        backlog = await runtime._export_paired_archive(runtime._continuity_snapshot())
        rows = [{"type": "assistant/message", "data": {"text": "完整后台记录"}}]
        operations = []

        async def call(operation, payload):
            operations.append(operation)
            if operation == "session.export":
                return {"result": "accepted", "records": rows, "total": 1, "next_offset": None}
            return {"result": "accepted"}

        session = DshShadowAgentSession(client=SimpleNamespace(call=call),
            mapping_store=DshMappingStore(tmp_path / "mapping.json"), context_source=runtime.shadow_context,
            voice_session_id=old_voice_id, workspace=tmp_path)
        session._opened = True
        old_dsh_id = session.dsh_session_id
        runtime.shadow_session = session
        await runtime._voice_mailbox.publish(DurableVoiceEvent("old-event", "旧后台待投递", old_dsh_id))
        await runtime._voice_mailbox.publish(DurableVoiceEvent("other-event", "其他会话通知", "other-dsh"))
        runtime._organize_archive = AsyncMock(side_effect=AssertionError("清除不能调用模型"))

        await runtime.clear_session()

        runtime._organize_archive.assert_not_called()
        assert "session.export" in operations and "session.retire" in operations
        assert runtime.conversation.conversation_id != old_voice_id
        assert session.dsh_session_id != old_dsh_id
        assert not session._opened
        assert not runtime.history.entries and not runtime.history.conversation.entries
        assert runtime.continuity.document == ""
        assert runtime.pending_transfer.snapshot() == []
        runtime.pending_transfer.recover_captures()
        assert runtime.pending_transfer.snapshot() == []
        assert [item.event_id for item in await runtime._voice_mailbox.pending()] == ["other-event"]
        assert not runtime._archive_backlog
        assert backlog.archive_id in runtime._archive_consumed
        assert runtime.snapshot().payload["session_operation"] is None
        cleared = next(item for item in runtime.paired_archives.root.iterdir()
                       if (item / "source.json").is_file()
                       and "cleared_session_context" in (item / "source.json").read_text(encoding="utf-8"))
        raw = json.loads((cleared / "source.json").read_text(encoding="utf-8"))
        context = next(item for item in raw["voice"] if item.get("kind") == "cleared_session_context")
        assert context["continuity_document"] == "旧衔接文档"
        assert context["pending_transfers"][0]["text"] == "旧待传达材料"
        assert context["voice_events"][0]["event_id"] == "old-event"
        assert raw["dsh"] == rows
        assert await runtime.publish_voice_event({"source_session_id": old_dsh_id,
            "event_id": "late-event", "text": "旧会话的迟到内容"}) == "stale"
        recovered = make_runtime(tmp_path)
        assert recovered.continuity.document == "" and not recovered._archive_backlog
        assert not recovered.pending_transfer.snapshot()
    asyncio.run(scenario())


def test_clear_retirement_failure_resumes_saved_transaction_without_reviving_old_context(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        runtime.continuity._replace_document("旧文档")
        runtime.pending_transfer.append("old-slot", "旧材料", voice_session_id="old", source_ref="old")
        original_rotate = runtime._rotate_session
        runtime._rotate_session = AsyncMock(side_effect=RuntimeError("retire unavailable"))
        runtime._organize_archive = AsyncMock(side_effect=AssertionError("清除不调用整理模型"))
        with pytest.raises(RuntimeError, match="retire unavailable"):
            await runtime.clear_session()
        assert runtime._handoff_marker.exists()
        marker = json.loads(runtime._handoff_marker.read_text(encoding="utf-8"))
        assert marker["document"] == "" and marker["phase"] == "consumed"
        runtime._rotate_session = original_rotate
        # start listening（开始监听）原有的恢复流程即可完成清除事务。
        async with runtime._session_lock:
            await runtime._resume_handoff()
        assert not runtime._handoff_marker.exists()
        assert runtime.conversation.conversation_id == marker["target_voice_session_id"]
        assert runtime.continuity.document == "" and not runtime.pending_transfer.snapshot()
        assert not runtime._archive_backlog
        runtime._organize_archive.assert_not_called()
    asyncio.run(scenario())


def test_clear_mailbox_write_failure_can_retry_without_redelivering_archived_event(tmp_path):
    from voice_terminal.shadow_agent.contracts import DurableVoiceEvent
    from voice_terminal.shadow_agent.durable_mailbox import DurableVoiceMailbox

    async def scenario():
        runtime = make_runtime(tmp_path)
        # 后台会话未打开，也可能持有停止监听前已经接受的持久事件。
        runtime.shadow_session = DshShadowAgentSession(client=SimpleNamespace(),
            mapping_store=DshMappingStore(tmp_path / "mapping.json"), context_source=runtime.shadow_context,
            voice_session_id=runtime.conversation.conversation_id, workspace=tmp_path)
        old_dsh_id = runtime.shadow_session.dsh_session_id
        await runtime._voice_mailbox.publish(DurableVoiceEvent("old-event", "旧待投递材料", old_dsh_id))
        original_save = runtime._voice_mailbox._save
        def failed_save():
            raise OSError("disk")
        runtime._voice_mailbox._save = failed_save
        with pytest.raises(OSError):
            await runtime.clear_session()
        assert runtime._handoff_marker.exists()
        assert [item.event_id for item in await runtime._voice_mailbox.pending()] == ["old-event"]
        runtime._voice_mailbox._save = original_save
        async with runtime._session_lock:
            await runtime._resume_handoff()
        assert not await runtime._voice_mailbox.pending()
        recovered = DurableVoiceMailbox(runtime._voice_mailbox.path)
        assert not await recovered.pending()
    asyncio.run(scenario())


def test_clear_archive_failure_preserves_original_identity_document_and_pending_material(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        original_id = runtime.conversation.conversation_id
        runtime.history.commit_input("old-input", "保留原会话")
        runtime.continuity._replace_document("原衔接文档")
        runtime.pending_transfer.append("old-slot", "待传达", voice_session_id=original_id, source_ref="old")
        runtime._export_paired_archive = AsyncMock(side_effect=OSError("disk unavailable"))
        with pytest.raises(OSError):
            await runtime.clear_session()
        assert runtime.conversation.conversation_id == original_id
        assert runtime.continuity.document == "原衔接文档"
        assert runtime.pending_transfer.snapshot()[0]["id"] == "old-slot"
        assert runtime.history.entries
        assert runtime.snapshot().payload["session_operation"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["reset", "clear"])
def test_manual_operation_snapshot_blocks_start_until_transaction_finishes(tmp_path, operation):
    async def scenario():
        runtime = make_runtime(tmp_path)
        reached, release = asyncio.Event(), asyncio.Event()
        runtime._organize_archive = AsyncMock(side_effect=AssertionError("本测试不调用模型"))

        async def settle():
            reached.set()
            await release.wait()

        runtime._settle_session_for_archive = settle
        task = asyncio.create_task(runtime.reset_session() if operation == "reset" else runtime.clear_session())
        await reached.wait()
        snapshot = runtime.snapshot().payload
        assert snapshot["session_operation"] == operation and snapshot["can_start"] is False
        release.set()
        if operation == "reset":
            with pytest.raises(AssertionError):
                await task
        else:
            await task
        assert runtime.snapshot().payload["session_operation"] is None
    asyncio.run(scenario())


def test_archive_catalog_recovers_commit_gap_and_does_not_revive_consumed_material(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        entry_id = runtime._capture_deferred("quiet-output", "模型取消前已保存的正文")
        # 模拟归档目录已经提交、归档目录清单尚未更新时中断。
        archive = runtime.paired_archives.save(voice_session_id=runtime.conversation.conversation_id,
            dsh_session_id="", cwd=str(tmp_path), voice_records=[], dsh_records=[],
            captures=runtime.pending_transfer.captures(runtime.conversation.conversation_id))
        recovered = make_runtime(tmp_path)
        assert archive.archive_id in recovered._archive_backlog
        assert recovered.pending_transfer.snapshot()[0]["id"] == entry_id
        recovered._rotate_session = AsyncMock()
        await recovered._commit_handoff(OrganizedHandoff("交接已承接该正文", [], [entry_id], archive,
                                                       (archive.archive_id,)))
        restarted = make_runtime(tmp_path)
        assert archive.archive_id not in restarted._archive_backlog
        assert archive.archive_id in restarted._archive_consumed
        assert not restarted.pending_transfer.snapshot()
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["scheduled", "durable"])
def test_main_loop_suppressed_output_normalizes_once_without_replaying_actions(tmp_path, source):
    async def scenario():
        done = asyncio.Event()
        requests = []
        woke = asyncio.Event()
        async def wake_shadow():
            woke.set()
            return "woken"
        waker = AsyncMock(side_effect=wake_shadow)
        store = PendingTransferStore(tmp_path / "pending.json")
        class Provider:
            supports_decision_schema = False
            async def stream_events(self, messages, **kwargs):
                requests.append(messages)
                text = "任务已完成，需要确认发布。" if len(requests) == 1 else "任务完成，等待确认是否发布。"
                actions = [{"name":"request_shadow", "arguments":{"request":True}}] if source == "durable" and len(requests) == 1 else []
                yield {"type":"content","text":json.dumps({"speak":{"state":"speak","emotion":"calm","text":text},"actions":actions},ensure_ascii=False)}
                yield {"type":"finish"}
        class Registry:
            def action_definitions(self): return []
            def begin_turn(self, *_): pass
            def cancel_turn(self, *_): pass
        async def emit(*_): pass
        async def close(): pass
        output = SimpleNamespace(status="IDLE",busy=False,active=False,speak_snapshot={"queue_status":"empty"},
                                 close=close,cancel_all=lambda *_: None)
        runtime = AgentEpisodeRuntime(Provider(), Registry(), output, emit,
                                     conversation=ConversationRuntime(),ledger=SessionLedger(),shadow_waker=waker)
        policy = QuietHoursPolicy(QuietHoursConfig(True))
        policy.within_hours = lambda now=None: True
        runtime.quiet_policy = policy
        def capture(source_id,text,**kwargs):
            store.capture(source_id,text,voice_session_id="v",source_ref=source_id,**kwargs)
            return source_id
        def append(entry_id,text,failed):
            store.append(entry_id,text,voice_session_id="v",source_ref=entry_id,unorganized=failed)
            done.set()
        runtime.deferred_capture, runtime.deferred_append = capture, append
        await runtime.ensure_episode()
        if source == "scheduled":
            assert await runtime.enqueue_scheduled_wake({"task_id":"task-a","occurrence_id":"occurrence-a","text":"后台任务完成了"})
        else:
            from voice_terminal.shadow_agent.contracts import DurableVoiceEvent
            await runtime.accept_durable_voice_event(DurableVoiceEvent(event_id="event-a", text="后台任务完成了", source_session_id="dsh"))
        try:
            await asyncio.wait_for(done.wait(),3)
            if source == "durable":
                await asyncio.wait_for(woke.wait(),1)
        finally:
            await runtime.stop()
        assert len(requests) == 2
        assert len(store.snapshot()) == 1
        assert store.snapshot()[0]["text"] == "任务完成，等待确认是否发布。"
        assert not store.snapshot()[0]["unorganized"]
        assert not any(entry.kind == "speech" for entry in runtime.ledger.entries)
        assert waker.await_count == (1 if source == "durable" else 0)
    asyncio.run(scenario())
