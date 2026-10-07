"""跨进程会话恢复：模型语义、聊天顺序与后台绑定共用重置边界。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.config import AppConfig, DshConfig, PersonaConfig
from voice_terminal.dsh.bridge import DshMappingStore, DshShadowAgentSession
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.conversation_log import ASSISTANT_SPEECH, USER_TURN
from voice_terminal.session.ledger import SessionLedger


def resume_ledger(root: Path) -> SessionLedger:
    ledger = SessionLedger()
    ledger.attach_storage(root, cache_bytes=256, resume=True)
    return ledger


def make_runtime(root: Path) -> SessionRuntime:
    config = AppConfig(persona=PersonaConfig(directory=root / "config/persona"),
                       dsh=replace(DshConfig(), home_dir=root / ".runtime/dsh/home"))
    runtime = SessionRuntime(config, runtime_data_dir=root / ".runtime")
    runtime._restore_or_persist_voice_identity()
    runtime.state.wake_always_on = False
    return runtime


class PersistedBridgeClient:
    """测试宿主：持久会话与进程内绑定分开，重复创建旧身份会被拒绝。"""

    def __init__(self):
        self.persisted = set()
        self.bindings = {}
        self.calls = []

    async def call(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        identity = payload["dsh_session_id"]
        if operation in {"session.open", "session.resume"}:
            if operation == "session.open":
                assert identity not in self.persisted, "已有会话必须恢复，不能重新创建"
                self.persisted.add(identity)
            else:
                assert identity in self.persisted
            self.bindings[identity] = {"state": "active", "context_revision": 0, "activation_id": ""}
        elif operation == "session.state":
            return {"result": "accepted", **self.bindings[identity]} if identity in self.bindings else {"result": "not_found"}
        elif operation == "session.cancel":
            self.bindings[identity]["activation_id"] = ""
        elif operation == "session.retire":
            self.bindings[identity]["state"] = "retired"
        elif operation in {"context.snapshot", "context.update"}:
            self.bindings[identity]["context_revision"] = payload["context_revision"]
        elif operation == "wake":
            self.bindings[identity]["activation_id"] = payload["activation_id"]
        return {"protocol_version": "2.0", "result": "accepted"}


def shadow_session(runtime, client, root):
    return DshShadowAgentSession(client=client, mapping_store=DshMappingStore(root / "mapping.json"),
                                context_source=runtime.shadow_context,
                                voice_session_id=runtime.conversation.conversation_id, workspace=root)


def test_restart_restores_model_history_heard_speech_refinement_and_chat_order(tmp_path):
    first = make_runtime(tmp_path)
    identity = first.conversation.conversation_id
    first.history.commit_input("turn-a", "明天开会")
    first.history.append_record({"turn_id": "turn-a", "transcript": "明天开会"})
    first.history.update_input_refinement("turn-a", "明天下午开会")
    delivery = DeliveryReport(status="partial", termination="interrupted", heard_text="已记下",
                              unheard_text="这部分尚未播放")
    first.history.commit_delivery("speech-a", "已记下。这部分尚未播放", delivery)
    first.history.conversation.append(USER_TURN, conversation_id=identity, turn_id="turn-a", text="明天开会")
    first.history.conversation.append(ASSISTANT_SPEECH, conversation_id=identity, speech_id="speech-a", text="已记下")
    expected_context = first.history.context_messages("")
    expected_chat = first.history.conversation.snapshot()
    expected_shadow = first.shadow_context.shadow_context_snapshot()
    expected_calendar = first.conversation.calendar_baseline
    assert first.history.close_storage()

    second = make_runtime(tmp_path)
    try:
        assert second.conversation.conversation_id == identity
        assert second.conversation.calendar_baseline == expected_calendar
        assert second.history.context_messages("") == expected_context
        assert second.history.conversation.snapshot() == expected_chat
        assert second.shadow_context.shadow_context_snapshot() == expected_shadow
        assert second.history.records[0].details["refined_text"] == "明天下午开会"
        assert not second.history.commit_delivery("speech-a", "重复交付", delivery)
        second.history.conversation.append(USER_TURN, conversation_id=identity, turn_id="turn-b", text="继续")
        assert second.history.conversation.latest_seq == 3
        assert second.history.journal.cache_size <= second._history_cache_bytes
    finally:
        assert second.history.close_storage()


def test_another_python_process_recovers_full_history_and_bounded_chat(tmp_path):
    ledger = resume_ledger(tmp_path)
    for index in range(205):
        ledger.commit_input(f"turn-{index}", f"消息{index}")
        ledger.conversation.append(USER_TURN, conversation_id="conversation-test", turn_id=f"turn-{index}", text=f"消息{index}")
    assert ledger.close_storage()
    script = """
import json, sys
from pathlib import Path
from voice_terminal.session.ledger import SessionLedger
ledger = SessionLedger()
ledger.attach_storage(Path(sys.argv[1]), resume=True)
print(json.dumps({'history_count': len(ledger.context_messages('')), 'chat': ledger.conversation.snapshot()}, ensure_ascii=False))
assert ledger.close_storage()
"""
    child = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)],
                           capture_output=True, text=True, encoding="utf-8", check=True)
    restored = json.loads(child.stdout)
    assert restored["history_count"] == 205
    assert restored["chat"]["latest_seq"] == 205
    assert len(restored["chat"]["entries"]) == 200
    assert restored["chat"]["entries"][0]["text"] == "消息5"


def test_abrupt_process_exit_keeps_flushed_history_without_shutdown_export(tmp_path):
    script = """
import os, sys
from pathlib import Path
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.conversation_log import USER_TURN
ledger = SessionLedger()
ledger.attach_storage(Path(sys.argv[1]), resume=True)
ledger.commit_input('turn-crash', '直接退出前的消息')
ledger.conversation.append(USER_TURN, conversation_id='conversation-crash', turn_id='turn-crash', text='直接退出前的消息')
assert ledger.journal.flush()
os._exit(0)
"""
    subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)], check=True)
    assert not (tmp_path / "active-session.md").exists()
    restored = resume_ledger(tmp_path)
    try:
        assert restored.has_input("turn-crash")
        assert restored.conversation.snapshot()["entries"][0]["text"] == "直接退出前的消息"
    finally:
        assert restored.close_storage()


def test_checkpoint_failure_preserves_previous_state_and_retries_all_dependencies(tmp_path):
    ledger = resume_ledger(tmp_path)
    assert ledger.journal.flush()
    path = ledger.journal.path
    try:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TRIGGER fail_input BEFORE INSERT ON records "
                               "WHEN NEW.bucket LIKE 'entries-%' BEGIN SELECT RAISE(FAIL, 'disk failure'); END;")
        ledger.commit_input("pending", "失败后必须保留的消息")
        ledger.conversation.append(USER_TURN, conversation_id="conversation-test", turn_id="pending", text="失败后必须保留的消息")
        assert not ledger.journal.flush()
        with sqlite3.connect(path) as connection:
            state = json.loads(connection.execute("SELECT body FROM records WHERE bucket='session-state'").fetchone()[0])
            assert state["sequences"]["entries"]["count"] == 0
            assert state["conversation"]["latest_seq"] == 0
            connection.execute("DROP TRIGGER fail_input")
        assert ledger.has_input("pending")
        assert ledger.journal.flush()
    finally:
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TRIGGER IF EXISTS fail_input")
        assert ledger.close_storage()
    restored = resume_ledger(tmp_path)
    try:
        assert restored.has_input("pending")
        assert restored.conversation.latest_seq == 1
    finally:
        assert restored.close_storage()


def test_reset_namespace_and_epoch_survive_restart_without_resurrecting_old_history(tmp_path):
    ledger = resume_ledger(tmp_path)
    ledger.commit_input("old", "旧会话")
    ledger.reset()
    ledger.commit_input("new", "新会话")
    expected = ledger.context_messages("")
    assert ledger.close_storage()
    restored = resume_ledger(tmp_path)
    try:
        assert restored.context_messages("") == expected
        assert restored.shadow_context_epoch == 2
        assert not restored.has_input("old")
        assert restored.has_input("new")
    finally:
        assert restored.close_storage()


def test_invalid_restore_namespace_preserves_database_and_refuses_empty_history(tmp_path):
    ledger = resume_ledger(tmp_path)
    ledger.commit_input("turn", "不能丢失")
    path = ledger.journal.path
    assert ledger.close_storage()
    with sqlite3.connect(path) as connection:
        state = json.loads(connection.execute("SELECT body FROM records WHERE bucket='session-state'").fetchone()[0])
        del state["sequences"]["entries"]
        connection.execute("UPDATE records SET body=? WHERE bucket='session-state'", (json.dumps(state),))
    with pytest.raises(RuntimeError, match="命名空间无效"):
        resume_ledger(tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM records WHERE kind='input'").fetchone()[0] == 1


def test_failed_chat_restore_leaves_ledger_reusable_after_checkpoint_repair(tmp_path):
    first = resume_ledger(tmp_path)
    first.commit_input("turn", "修复后仍然能恢复")
    first.conversation.append(USER_TURN, conversation_id="conversation-test", turn_id="turn", text="修复后仍然能恢复")
    path = first.journal.path
    assert first.close_storage()
    with sqlite3.connect(path) as connection:
        valid_body = connection.execute("SELECT body FROM records WHERE bucket='session-state'").fetchone()[0]
        invalid = json.loads(valid_body)
        invalid["conversation"]["latest_seq"] = 0
        connection.execute("UPDATE records SET body=? WHERE bucket='session-state'", (json.dumps(invalid),))
    restored = SessionLedger()
    with pytest.raises(ValueError, match="恢复顺序无效"):
        restored.attach_storage(tmp_path, resume=True)
    assert restored.journal is None
    assert restored.entries == []
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE records SET body=? WHERE bucket='session-state'", (valid_body,))
    restored.attach_storage(tmp_path, resume=True)
    try:
        assert restored.has_input("turn")
        assert restored.conversation.latest_seq == 1
    finally:
        assert restored.close_storage()


def test_stop_start_keeps_voice_and_dsh_identity_and_never_reopens(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        client = PersistedBridgeClient()
        session = shadow_session(runtime, client, tmp_path)
        runtime.shadow_session = session
        runtime.controller = SimpleNamespace(start_listening=AsyncMock(), stop_listening=AsyncMock())
        try:
            await session.reconnect()
            identity = (runtime.conversation.conversation_id, session.dsh_session_id)
            await runtime.stop_listening()
            await runtime.start_listening()
            assert (runtime.conversation.conversation_id, session.dsh_session_id) == identity
            assert [name for name, _ in client.calls].count("session.open") == 1
            assert not any(name == "session.retire" for name, _ in client.calls)
        finally:
            await session.close()
            assert runtime.history.close_storage()
    asyncio.run(scenario())


@pytest.mark.parametrize("host_restarted", [False, True])
def test_reinitialize_or_host_restart_reuses_existing_binding_without_replaying_wake(tmp_path, host_restarted):
    async def scenario():
        first = make_runtime(tmp_path)
        client = PersistedBridgeClient()
        old = shadow_session(first, client, tmp_path)
        await old.reconnect()
        identity = old.dsh_session_id
        await old.close()
        assert (await old.mapping_store.load(first.conversation.conversation_id)).state == "idle"
        assert first.history.close_storage()
        if host_restarted:
            client.bindings.clear()
        second = make_runtime(tmp_path)
        current = shadow_session(second, client, tmp_path)
        try:
            await current.reconnect()
            assert current.dsh_session_id == identity
            operations = [name for name, _ in client.calls]
            assert operations.count("session.open") == 1
            assert operations.count("session.resume") == int(host_restarted)
            assert "session.retire" not in operations
            assert "wake" not in operations
            await current.request_wake()
            assert [name for name, _ in client.calls].count("wake") == 1
        finally:
            await current.close()
            assert second.history.close_storage()
    asyncio.run(scenario())


@pytest.mark.parametrize("was_active", [False, True])
def test_component_reconnect_uses_resume_for_same_identity_after_host_restart(tmp_path, was_active):
    async def scenario():
        runtime = make_runtime(tmp_path)
        client = PersistedBridgeClient()
        session = shadow_session(runtime, client, tmp_path)
        try:
            await session.reconnect()
            identity = session.dsh_session_id
            if was_active:
                await session.request_wake()
            client.bindings.clear()
            await session.reconnect()
            assert session.dsh_session_id == identity
            assert session.activation_id is None
            assert [name for name, _ in client.calls].count("session.open") == 1
            assert [name for name, _ in client.calls].count("session.resume") == 1
            assert [name for name, _ in client.calls].count("wake") == int(was_active)
            await session.request_wake()
            assert [name for name, _ in client.calls].count("wake") == int(was_active) + 1
        finally:
            await session.close()
            assert runtime.history.close_storage()
    asyncio.run(scenario())
