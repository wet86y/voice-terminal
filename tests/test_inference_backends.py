"""主语音双后端：请求边界、共享凭据、动作事务及图片传输回归。"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from voice_terminal.config import AppConfig
from voice_terminal.inference import InferenceRuntimeMixin, inference_config
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.decision_envelope import Action, ActionContractError, DecisionEnvelope, SpeechDecision
from voice_terminal.agent.shadow_registry import VoiceShadowRegistry
from voice_terminal.agent.context_builder import AgentContextMixin
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


class RuntimeHarness(InferenceRuntimeMixin):
    """RuntimeHarness（切换测试台）：仅替换设备及网络，不重建会话对象。"""
    def __init__(self, root):
        self.config = replace(AppConfig(model_dir=root / "models"), llm=inference_config(AppConfig(model_dir=root).llm, "deepseek"))
        self.product_settings = ProductSettingsStore(path=root / "settings.json", config=self.config)
        self.reasoning_mode = "fast"
        self._session_lock = asyncio.Lock()
        self._credential_overrides = {"DEEPSEEK_API_KEY": "deep-test", "MINIMAX_API_KEY": "mini-test"}
        self.conversation = SimpleNamespace(conversation_id="same-conversation")
        self.llm = DeepSeekClient(base_url="https://api.deepseek.com", model="deepseek-flash", api_key="deep-test")
        self.episode = SimpleNamespace(active_request=None, recovery_queue=deque(), pending=set(), queue=asyncio.Queue(), closed=False)
        self.episode.provider = self.llm
        self.controller = SimpleNamespace(episode=self.episode, registry=SimpleNamespace(supports_images=True), set_reasoning_mode=Mock(), llm=self.llm)
        self.bus = SimpleNamespace(publish=AsyncMock())
        self.state = SimpleNamespace(set_service=Mock())
        self._clear_service_error = Mock()

    def _api_key(self, name):
        return self._credential_overrides.get(name, "")

    def snapshot(self):
        return self.inference_snapshot()


def test_settings_18_upgrade_to_current_schema_defaults_and_selection_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"schema_version":18,"silence":{"seconds":25}}', encoding="utf-8")
    config = AppConfig(model_dir=tmp_path)
    store = ProductSettingsStore(path=path, config=config)
    assert store.settings_only()["schema_version"] == 20
    assert store.settings_only()["inference"] == {"backend": "deepseek"}
    legacy_path = tmp_path / "legacy-m3.json"
    legacy_path.write_text('{"schema_version":18}', encoding="utf-8")
    legacy = ProductSettingsStore(path=legacy_path, config=replace(config, llm=inference_config(config.llm, "minimax")))
    assert legacy.effective_config().llm.model == "deepseek-flash"
    store.save_inference({"backend": "minimax"})
    restored = ProductSettingsStore(path=path, config=config)
    assert restored.effective_config().llm.model == "MiniMax-M3.1-Flash"
    assert restored.effective_config().llm.api_key_env == "MINIMAX_API_KEY"
    assert restored.settings_only()["silence"]["seconds"] == 25
    for invalid in ({}, {"backend": "other"}, {"backend": []}, {"backend": "deepseek", "model": "other"}):
        with pytest.raises(ValueError):
            store.save_inference(invalid)


def test_switch_waits_for_preparation_active_recovery_and_retry_and_preserves_state(tmp_path):
    async def scenario():
        runtime = RuntimeHarness(tmp_path)
        episode, controller, conversation = runtime.episode, runtime.controller, runtime.conversation
        episode._dispatch_starting = True
        task = asyncio.create_task(runtime.switch_inference("minimax"))
        await asyncio.sleep(.04)
        assert not task.done() and episode.inference_switch_pending
        assert runtime.inference_snapshot()["backend"] == "deepseek"
        assert runtime.inference_snapshot()["pending_backend"] == "minimax"
        episode.active_request = object(); episode._dispatch_starting = False
        episode.pending.add("USER_INPUT_FINALIZED")
        await asyncio.sleep(.04)
        assert not task.done()
        episode.recovery_queue.append(object()); episode.active_request = None
        await asyncio.sleep(.04)
        assert not task.done()
        episode.pending.add("LLM_RETRY"); episode.recovery_queue.clear()
        await asyncio.sleep(.04)
        assert not task.done()
        episode.pending.discard("LLM_RETRY")
        runtime._credential_overrides["MINIMAX_API_KEY"] = "rotated-during-wait"
        await asyncio.wait_for(task, 1)
        assert runtime.controller is controller and runtime.conversation is conversation and controller.episode is episode
        assert episode.pending == {"USER_INPUT_FINALIZED"}
        assert episode.provider is runtime.llm and runtime.llm.session_id == "same-conversation"
        assert runtime.llm.api_key == "rotated-during-wait"
        assert runtime.inference_snapshot()["reasoning_mode_selectable"] is False
        assert runtime.reasoning_mode == "deep"
        await runtime.switch_inference("deepseek")
        assert runtime.reasoning_mode == "fast" and runtime.inference_snapshot()["reasoning_mode_selectable"]
        assert episode.pending == {"USER_INPUT_FINALIZED"}
        await runtime.llm.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["construct", "save"])
def test_switch_failure_retains_effective_client_and_saved_selection(tmp_path, failure):
    async def scenario():
        runtime = RuntimeHarness(tmp_path)
        old = runtime.llm
        target = "voice_terminal.providers.deepseek.DeepSeekClient" if failure == "construct" else None
        context = patch(target, side_effect=ValueError("构造失败")) if target else patch.object(runtime.product_settings, "save_inference", side_effect=OSError("保存失败"))
        with context, pytest.raises((ValueError, OSError)):
            await runtime.switch_inference("minimax")
        assert runtime.llm is old and runtime.episode.provider is old
        assert runtime.product_settings.settings_only()["inference"]["backend"] == "deepseek"
        assert runtime.inference_snapshot()["pending_backend"] is None
        assert not runtime.episode.inference_switch_pending and runtime.reasoning_mode == "fast"
    asyncio.run(scenario())


def test_minimax_all_provider_entry_paths_force_low_and_failure_does_not_fallback():
    async def scenario():
        client = DeepSeekClient(provider="minimax", base_url="https://api.minimaxi.com/v1", model="MiniMax-M3.1-Flash", api_key="test", transport="responses", allowed_reasoning=("low",))
        seen = []
        async def stream(_messages, **kwargs):
            seen.append(kwargs["reasoning_effort"])
            raise RuntimeError("MiniMax 请求失败")
            yield  # 异步生成器形状，与真实适配器一致。
        client._stream_responses = stream
        for effort in (None, "none", "off", "low"):
            with pytest.raises(RuntimeError, match="MiniMax"):
                async for _ in client.stream_events([], schema={}, reasoning_effort=effort):
                    pass
        assert seen == ["low"] * 4 and client.provider == "minimax"
    asyncio.run(scenario())


def test_multi_tool_actions_validate_whole_list_before_effects():
    async def scenario():
        registry = VoiceShadowRegistry(available=True, supports_images=True)
        runtime = AgentEpisodeRuntime(None, registry, SimpleNamespace(status="IDLE"), AsyncMock(), conversation=ConversationRuntime(), ledger=SessionLedger())
        runtime.tools_enabled = True
        effects = []
        runtime._submit_shadow_actions = AsyncMock(side_effect=lambda *_: effects.append("shadow"))
        runtime._capture_screen_action = AsyncMock(side_effect=lambda *_: effects.append("screen"))
        runtime.listening.request = Mock(return_value={"accepted": 1})
        actions = (Action("request_shadow", {"request": True}), Action("capture_screen", {"request": True}), Action("continue_listening", {"count": 1}))
        for size in (2, 3):
            await runtime._dispatch_actions_v06(("c", "r"), "turn", DecisionEnvelope(SpeechDecision("wait", "calm", ""), actions[:size]))
            await asyncio.sleep(0)
        assert effects.count("shadow") == effects.count("screen") == 2
        assert runtime.listening.request.call_count == 1
        before = list(effects)
        for invalid in (actions + (actions[0],), actions[:2] + (Action("continue_listening", {"count": True}),), (actions[0], Action("capture_screen", {"request": False})), (actions[0], Action("capture_screen", {"request": True, "count": 1}))):
            with pytest.raises(ActionContractError):
                await runtime._dispatch_actions_v06(("c", "r"), "turn", DecisionEnvelope(SpeechDecision("wait", "calm", ""), invalid))
            await asyncio.sleep(0)
            assert effects == before and runtime.listening.request.call_count == 1
    asyncio.run(scenario())


def test_merged_filler_cleanup_is_only_an_explicit_legacy_entry():
    registry = VoiceShadowRegistry(available=True, supports_images=True)
    runtime = SimpleNamespace(_session_action_definitions=registry.action_definitions)
    envelope = DecisionEnvelope(SpeechDecision("wait", "calm", ""), (Action("request_shadow", {"request": True, "count": 1, "unknown": True}),))
    assert AgentContextMixin._normalize_schema_envelope(runtime, envelope) == envelope
    runtime.legacy_merged_schema = True
    legacy = AgentContextMixin._normalize_schema_envelope(runtime, envelope)
    assert legacy.actions[0].arguments == {"request": True, "unknown": True}


def test_request_credentials_freeze_until_request_finishes_and_new_request_uses_new_key():
    client = DeepSeekClient(base_url="https://api.deepseek.com", model="deepseek-flash", api_key="old-key")
    with client.credential_scope():
        old_scope = client.image_transport_snapshot()["scope"]
        client.api_key = "new-key"
        assert client._request_headers()["Authorization"] == "Bearer old-key"
        assert client.image_transport_snapshot()["scope"] == old_scope
    assert client._request_headers()["Authorization"] == "Bearer new-key"
    assert client.image_transport_snapshot()["scope"] != old_scope


def test_two_neutral_images_survive_provider_file_transport_and_switch():
    from tests.test_screen_capture import packet
    from voice_terminal.agent.decision_schema import decision_schema
    async def scenario():
        ledger = SessionLedger()
        capture = ledger.store_screen_capture(packet(), "conversation", "turn")
        ledger.commit_screen_capture(capture)
        original = ledger.context_messages("")
        deepseek = DeepSeekClient(base_url="https://api.deepseek.com", model="deepseek-flash", api_key="deep")
        response = Mock()
        response.raise_for_status = Mock()
        response.json.side_effect = [{"id": "file-api-global"}, {"id": "file-api-crop"}]
        deepseek._http_client = AsyncMock(return_value=SimpleNamespace(post=AsyncMock(return_value=response)))
        prepared = await deepseek.prepare_messages(original)
        assert sum("file_id" in part for frame in prepared for part in frame["content"] if isinstance(frame["content"], list)) == 2
        assert ledger.context_messages("") == original
        mini = DeepSeekClient(provider="minimax", base_url="https://api.minimaxi.com/v1", model="MiniMax-M3.1-Flash", api_key="mini")
        mini.restore_image_transport(deepseek.image_transport_snapshot())
        inline = await mini.prepare_messages(ledger.context_messages(""))
        wire = mini._responses_payload(inline, model=mini.model, schema=decision_schema([]), schema_name="test", reasoning_effort="low", max_output_tokens=393216)
        images = [part for frame in wire["input"] for part in frame["content"] if part["type"] == "input_image"]
        assert len(images) == 2 and all(part["image_url"].startswith("data:image/") and "file_id" not in part for part in images)
        assert ledger.context_messages("") == original
        ledger.screen_capture.close()
    asyncio.run(scenario())


def test_dsh_bootstrap_marker_written_only_after_ack_then_key_only(tmp_path):
    async def scenario():
        runtime = RuntimeHarness(tmp_path)
        runtime.dsh = SimpleNamespace(paths=SimpleNamespace(config=tmp_path), bridge=SimpleNamespace(sync_minimax=AsyncMock(side_effect=RuntimeError("暂时不可用"))), _closing=False)
        marker = tmp_path / "dsh-minimax-bootstrap.json"
        await runtime.sync_minimax_dsh()
        assert runtime.inference_snapshot()["dsh_sync_pending"] is True and not marker.exists()
        assert not runtime.dsh._credential_sync_task.done()
        runtime.dsh.bridge.sync_minimax.side_effect = None
        await runtime.sync_minimax_dsh()
        assert marker.exists() and runtime.inference_snapshot()["dsh_sync_pending"] is False
        runtime.dsh.bridge.sync_minimax.assert_awaited_with("mini-test", initialize=True)
        runtime._credential_overrides["MINIMAX_API_KEY"] = "rotated-test"
        await runtime.sync_minimax_dsh()
        runtime.dsh.bridge.sync_minimax.assert_awaited_with("rotated-test", initialize=False)
        runtime.dsh._credential_sync_task.cancel()
        await asyncio.gather(runtime.dsh._credential_sync_task, return_exceptions=True)
    asyncio.run(scenario())


def test_tts_key_rotation_keeps_active_generation_and_drops_idle_connections():
    from voice_terminal.providers.minimax_tts import MiniMaxTtsClient
    async def scenario():
        client = MiniMaxTtsClient(base_url="https://api.minimaxi.com", websocket_path="/ws/v1/t2a_v2", model="speech-2.8-hd", api_key="old")
        active = SimpleNamespace(generation_active=True, close=AsyncMock())
        client._bidi_session = active
        client.update_api_key("new")
        assert client.api_key == "new" and client._bidi_session is None
        active.close.assert_not_awaited()
        await client.close()
    asyncio.run(scenario())


def test_switch_waiting_retry_retains_original_turn_when_new_input_arrives():
    from tests.test_json_schema_shadow import _runtime
    async def scenario():
        runtime = _runtime(SimpleNamespace())
        runtime.inference_switch_pending = True
        runtime._transport_retry_source_turn_id = "original-turn"
        runtime.last_turn_id = "new-turn"
        runtime.inputs["original-turn"] = {"text": "原请求", "final": True}
        runtime.inputs["new-turn"] = {"text": "切换期间的新输入", "final": True}
        runtime._step = AsyncMock(return_value="completed")
        assert await runtime._start_dispatch({"LLM_RETRY", "USER_INPUT_FINALIZED"})
        assert runtime.active_request_source_turn_id == "original-turn"
        await runtime.active_request
        await runtime.end("test")
    asyncio.run(scenario())


def test_invalid_action_recovers_with_typed_field_feedback_and_bounded_budget():
    from tests.test_json_schema_shadow import _runtime
    async def scenario():
        class Provider:
            transport = "responses"
            supports_decision_schema = True
            protocol_recovery_limit = 1
            async def stream_events(self, _messages, **_kwargs):
                yield {"type": "content", "text": '{"speak":{"state":"wait","controls":[],"text":""},"actions":[{"name":"request_shadow","arguments":{"request":false}}]}'}
                yield {"type": "finish", "finish_reason": "stop"}
        runtime = _runtime(Provider())
        runtime.registry = VoiceShadowRegistry(available=True)
        runtime.inputs["turn-a"] = {"text": "合成动作请求", "final": True}
        first = (runtime.episode_id, "request-invalid")
        runtime._identity_turns[first] = "turn-a"
        assert await runtime._step(first, {"USER_INPUT_FINALIZED"}) == "recovery_queued"
        signal = runtime.recovery_queue.popleft()
        assert signal.contract_feedback == {"type": "action_contract", "code": "CONST_MISMATCH", "path": "$.actions[0].arguments.request"}
        assert not runtime._background_tools
        second = (runtime.episode_id, "request-recovery")
        runtime._identity_turns[second] = "turn-a"
        assert await runtime._step(second, {"LLM_RECOVERY"}, recovery=True, recovery_signal=signal) == "recovery_exhausted"
        assert not runtime.recovery_queue and not runtime._background_tools
        await runtime.end("test")
    asyncio.run(scenario())


def test_dsh_sync_retry_uses_all_five_delays_when_failures_remain(tmp_path):
    async def scenario():
        runtime = RuntimeHarness(tmp_path)
        runtime.dsh = SimpleNamespace(_closing=False)
        runtime._minimax_dsh_sync_pending = True
        runtime.sync_minimax_dsh = AsyncMock()  # 保持待处理，模拟每次同步仍失败。
        with patch("voice_terminal.inference.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await runtime._retry_minimax_sync()
            assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 5, 15, 30]
        assert runtime.sync_minimax_dsh.await_count == 5
        assert runtime.bus.publish.await_count == 5
        assert runtime.inference_snapshot()["dsh_sync_pending"] is True
    asyncio.run(scenario())
