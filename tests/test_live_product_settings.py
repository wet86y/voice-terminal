"""Live settings（即时设置）：真实保存接口无需开启或重置语音会话。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.persona import PersonaDocuments
from voice_terminal.agent.scheduler import AgentDecisionScheduler
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.dsh.bridge import DshShadowAgentSession
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app


@pytest.fixture
def runtime(tmp_path):
    config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"))
    store = ProductSettingsStore(path=tmp_path / "config" / "product-settings.json", config=config)
    return SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / ".local")


async def request(runtime, section, payload):
    app = create_app(runtime, product_settings=runtime.product_settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.put(f"/api/v1/product/settings/{section}", json=payload)


def live_controller(runtime):
    """保留真实控制器的参数应用方法，替代音频与模型硬件边界。"""
    controller = object.__new__(EpisodeController)
    controller.state = runtime.state
    controller.agent_options = runtime.config.agent
    controller.kws = runtime.kws
    controller.wake_enabled = runtime.config.wake.enabled
    controller.wake_always_on = runtime.config.wake.enabled
    controller.workspace = None
    controller.output = SimpleNamespace(active=False)
    controller._activation_generation = 0
    controller._reset_owner_boundary = AsyncMock()
    controller._return_to_kws = AsyncMock(return_value=True)
    controller._ensure_asr_ready = AsyncMock(return_value=True)
    controller.episode = SimpleNamespace(
        options=runtime.config.agent, scheduler=AgentDecisionScheduler(runtime.config.agent),
        episode_id="same-episode", user_speaking=False, defer_silence_until_output=False,
    )
    runtime.controller = controller
    return controller


def test_dsh_approval_requires_host_auth_and_rejects_stale_requests(runtime, monkeypatch):
    async def run():
        monkeypatch.setenv("VOICE_APP_INTERNAL_TOKEN", "approval-test-token")
        session = object.__new__(DshShadowAgentSession)
        session.decide_approval = AsyncMock(return_value={"result": "accepted"})
        runtime.shadow_session = session
        app = create_app(runtime, product_settings=runtime.product_settings)
        payload = {"approval_id": "request-demo", "run_seq": 2, "decision": "rejected"}
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            endpoint = "/internal/application/dsh-approval"
            response = await client.post(endpoint, json=payload)
            assert response.status_code == 403
            session.decide_approval.assert_not_awaited()
            headers = {"Authorization": "Bearer approval-test-token"}
            response = await client.post(endpoint, json={**payload, "run_seq": True}, headers=headers)
            assert response.status_code == 422
            response = await client.post(endpoint, json=payload, headers=headers)
            assert response.status_code == 200
            session.decide_approval.assert_awaited_once_with("request-demo", "rejected", 2)
            session.decide_approval.return_value = {"result": "stale"}
            response = await client.post(endpoint, json=payload, headers=headers)
            assert response.status_code == 409
    asyncio.run(run())


def test_voice_approval_preferences_apply_without_rotating_the_episode(runtime, tmp_path):
    from tests.test_dsh_bridge import FakeClient, FakeContext
    from voice_terminal.dsh.bridge import DshMappingStore

    async def run():
        session = DshShadowAgentSession(client=FakeClient(), mapping_store=DshMappingStore(tmp_path / "map.json"),
                                        context_source=FakeContext(), voice_session_id="voice-a", workspace=tmp_path)
        runtime.shadow_session = session
        original_conversation = runtime.conversation.conversation_id
        preferences = {"voice_confirmation_enabled": True, "continuous_enabled": True}
        response = await request(runtime, "approvals", preferences)
        assert response.status_code == 200
        assert response.json()["settings"]["approvals"] == preferences
        assert session.approvals.voice_confirmation and session.approvals.continuous_voice
        assert not session.approvals.continuous_active
        assert runtime.conversation.conversation_id == original_conversation
        invalid = await request(runtime, "approvals", {**preferences, "continuous_enabled": "true"})
        assert invalid.status_code == 422
        assert runtime.product_settings.settings_only()["approvals"] == preferences
        await session.close()
    asyncio.run(run())


def test_voice_approval_context_must_precede_user_answer_in_actual_conversation(runtime):
    entries = runtime.history.conversation.entries
    entries.extend([
        {"seq": 1, "kind": "user_turn", "turn_id": "task", "text": "创建目录"},
        {"seq": 2, "kind": "assistant_speech", "turn_id": "task", "text": "同意创建目录吗？", "delivery": {"status": "full"}},
        {"seq": 3, "kind": "user_turn", "turn_id": "reply", "text": "可以"},
        {"seq": 4, "kind": "assistant_speech", "turn_id": "reply", "text": "同意提权吗？", "delivery": {"status": "full"}},
    ])
    assert runtime._approval_spoken_context("reply") == (2, "同意创建目录吗？")
    assert runtime._approval_spoken_context("unknown") == (0, "")
    assert runtime._approval_spoken_context() == (4, "同意提权吗？")


def test_wake_save_replaces_worker_while_listening_without_a_new_session(runtime):
    async def run():
        runtime.state.listening = True
        runtime.state.input_owner = "asr"
        controller = live_controller(runtime)
        session_id = runtime.conversation.conversation_id
        candidate = Mock(running=True, error=None)
        with patch.object(runtime.product_settings, "_compile_keyword"), \
             patch.object(runtime, "_build_kws_worker", return_value=candidate) as build:
            response = await request(runtime, "wake", {"enabled": True, "keywords": ["你好助手"], "sensitivity": 90})
        assert response.status_code == 200
        assert response.json()["effective_from"] == "immediate"
        assert response.json()["pending"] is False
        assert runtime.config.wake.keywords == ("你好助手",)
        assert controller.kws is runtime.kws is candidate
        assert controller.wake_enabled is controller.wake_always_on is True
        assert controller.episode.episode_id == "same-episode"
        assert runtime.conversation.conversation_id == session_id and runtime.state.listening
        build.assert_called_once()
        assert build.call_args.args[0].wake.keywords == ("你好助手",)
    asyncio.run(run())


def test_failed_wake_replacement_keeps_old_worker_running_and_reports_failure(runtime):
    async def run():
        runtime.config = replace(runtime.config, wake=replace(runtime.config.wake, enabled=True, always_on=True))
        original = runtime.config
        old_worker = Mock(running=True, error=None)
        runtime.kws = old_worker
        runtime.state.listening = True
        runtime.state.input_owner = "kws"
        controller = live_controller(runtime)
        with patch.object(runtime.product_settings, "_compile_keyword"), \
             patch.object(runtime, "_build_kws_worker", side_effect=RuntimeError("模型加载失败")):
            response = await request(runtime, "wake", {"enabled": True, "keywords": ["新唤醒词"], "sensitivity": 90})
        assert response.json()["effective_from"] == "retry_required"
        assert response.json()["pending"] is True
        assert runtime.config is original
        assert runtime.kws is controller.kws is old_worker
        assert runtime.state.listening and runtime.state.input_owner == "kws"
        old_worker.close.assert_not_called()
    asyncio.run(run())


def test_changing_wake_words_does_not_end_an_active_asr_window(runtime):
    async def run():
        runtime.config = replace(runtime.config, wake=replace(runtime.config.wake, enabled=True, always_on=True))
        old_worker, new_worker = Mock(), Mock()
        runtime.kws = old_worker
        runtime.state.listening = True
        runtime.state.input_owner = "asr"
        controller = live_controller(runtime)
        with patch.object(runtime.product_settings, "_compile_keyword"), \
             patch.object(runtime, "_build_kws_worker", return_value=new_worker):
            response = await request(runtime, "wake", {"enabled": True, "keywords": ["新的唤醒词"], "sensitivity": 90})
        assert response.json()["pending"] is False
        assert runtime.state.input_owner == "asr" and runtime.state.listening
        assert runtime.kws is controller.kws is new_worker
        controller._return_to_kws.assert_not_awaited()
        old_worker.close.assert_called_once()
    asyncio.run(run())


def test_disabling_wake_switches_standby_to_asr_without_stopping_audio(runtime):
    async def run():
        runtime.config = replace(runtime.config, wake=replace(runtime.config.wake, enabled=True, always_on=True))
        old_worker = Mock(running=True, error=None)
        runtime.kws = old_worker
        runtime.state.listening = True
        runtime.state.input_owner = "kws"
        controller = live_controller(runtime)
        response = await request(runtime, "wake", {"enabled": False, "keywords": [runtime.config.wake.keyword], "sensitivity": 75})
        assert response.json()["pending"] is False
        assert runtime.state.listening and runtime.state.input_owner == "asr"
        assert runtime.kws is controller.kws is None
        assert controller.wake_enabled is controller.wake_always_on is False
        assert controller.episode.defer_silence_until_output is False
        controller._ensure_asr_ready.assert_awaited_once_with(preserve_listening=True)
        old_worker.close.assert_called_once()
    asyncio.run(run())


def test_silence_save_changes_live_scheduler_and_preserves_session(runtime):
    async def run():
        runtime.state.listening = True
        controller = live_controller(runtime)
        scheduler = controller.episode.scheduler
        scheduler.arm(now=asyncio.get_running_loop().time() - 15)
        anchor, generation = scheduler.anchor_monotonic, scheduler.generation
        scheduler.start_heartbeat()
        heartbeat = scheduler.next_heartbeat
        response = await request(runtime, "silence", {"seconds": 10})
        assert response.json()["pending"] is False
        assert runtime.config.agent.final_silence_ms == 10_000
        assert controller.episode.scheduler is scheduler
        assert scheduler.FINAL_WINDOW == 25 and scheduler.final_pending
        assert scheduler.anchor_monotonic == anchor and scheduler.generation == generation
        assert scheduler.next_heartbeat == heartbeat and runtime.state.listening
        response = await request(runtime, "silence", {"seconds": 60})
        assert response.json()["pending"] is False
        assert scheduler.FINAL_WINDOW == 150 and not scheduler.final_pending
        assert scheduler.anchor_monotonic == anchor
    asyncio.run(run())


def test_persona_and_instruction_save_refresh_same_episode_and_shadow_context(runtime):
    async def run():
        registry = SimpleNamespace(action_definitions=lambda: [], begin_turn=lambda _: None)
        output = SimpleNamespace(status="IDLE", speak_snapshot={"queue_status": "empty", "text_only_queue": True})
        episode = AgentEpisodeRuntime(None, registry, output, AsyncMock(), conversation=runtime.conversation,
            ledger=runtime.history, persona_config=runtime.config.persona,
            custom_instructions_provider=lambda: runtime._instruction_snapshot)
        runtime.controller = SimpleNamespace(episode=episode)
        await episode.ensure_episode(turn_id="same-turn", text="继续")
        episode.persona_documents = PersonaDocuments(personality="旧人格", user="旧资料")
        old_messages = episode.messages({"USER_INPUT_FINALIZED"})
        old_digest = episode.static_context_digest
        original_id = episode.episode_id
        saved = await request(runtime, "persona", {"personality": "新人格：简洁回答", "user": "新资料：用中文"})
        assert saved.json()["effective_from"] == "next_request" and not saved.json()["pending"]
        instruction = {"id": "rule", "trigger": "查询资料", "target": "shadow", "instruction": "核实来源", "enabled": True}
        saved = await request(runtime, "custom-instructions", {"custom_instructions": [instruction]})
        assert saved.json()["pending"] is False
        new_messages = episode.messages({"USER_INPUT_FINALIZED"})
        assert episode.episode_id == original_id
        assert "新人格" in str(new_messages) and "旧人格" not in str(new_messages)
        assert "旧人格" in str(old_messages) and "新人格" not in str(old_messages)
        assert "查询资料" in str(new_messages)
        assert episode.static_context_digest != old_digest
        environment = runtime.shadow_context.environment_snapshot()
        assert environment["user_profile"] == "新资料：用中文"
        assert environment["custom_instructions"][0]["执行目标"] == "核实来源"
        assert runtime._instruction_snapshot == [instruction]
        revision = runtime.history.shadow_input_revision
        update = runtime.shadow_context.shadow_context_update(revision - 1, epoch=runtime.history.shadow_context_epoch)
        assert update.frames[-1].kind == "product_settings"
        # 清空也要更新当前两个模型的快照，不能遗留已删除规则。
        await request(runtime, "custom-instructions", {"custom_instructions": []})
        assert episode.custom_instructions == []
        assert "custom_instructions" not in runtime.shadow_context.environment_snapshot()
        if episode._tokenizer_prewarm_task is not None:
            await episode._tokenizer_prewarm_task
    asyncio.run(run())


def test_loading_settings_apply_at_session_initialization_without_listening(runtime):
    async def run():
        runtime._application_phased_startup = True
        with patch.object(runtime, "_build_kws_worker", return_value=Mock()) as build:
            saved = await request(runtime, "silence", {"seconds": 45})
            assert saved.json()["effective_from"] == "components_ready"
            assert saved.json()["pending"] is True
            saved_speaker = await request(runtime, "speaker", {"mode": "off", "owner_only_barge_in": False,
                "confirm_threshold": .65, "continuity_threshold": .35})
            assert saved_speaker.json()["effective_from"] == "components_ready"
            build.assert_not_called()
            # 实际 initialize_session（会话调度器初始化）完成路径必须补应用设置。
            with patch("voice_terminal.server.websocket.AsrWorkerClient", return_value=SimpleNamespace(connect=AsyncMock(), health=AsyncMock())), \
                 patch("voice_terminal.server.websocket.PcmPlayer", return_value=Mock()), \
                 patch("voice_terminal.server.websocket.EpisodeController", return_value=Mock(initialize=AsyncMock())), \
                 patch.object(runtime, "_api_key", return_value=""):
                await runtime.initialize_session()
            assert runtime._application_session_ready
            assert runtime._pending_product_settings is None
            assert runtime.config.agent.final_silence_ms == 45_000
            assert runtime.config.speaker.confirm_threshold == .65
            assert runtime.speaker_router.state_config.continuity_threshold == .45
            assert not runtime.state.listening
            if runtime._quiet_clock_task is not None:
                runtime._quiet_clock_task.cancel()
                await asyncio.gather(runtime._quiet_clock_task, return_exceptions=True)
    asyncio.run(run())


def test_other_settings_do_not_apply_pending_compute_choices_before_restart(runtime):
    async def run():
        original = runtime.config.compute
        runtime.product_settings._settings["compute"]["devices"]["campplus"] = "cuda"
        response = await request(runtime, "silence", {"seconds": 90})
        assert response.json()["pending"] is False
        assert runtime.config.compute is original
        assert runtime.config.compute.devices["campplus"] == "cpu"
    asyncio.run(run())
