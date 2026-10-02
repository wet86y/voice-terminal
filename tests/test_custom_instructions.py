from __future__ import annotations

import asyncio

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.custom_instructions import shadow_projection, validate_entries, voice_projection
from voice_terminal.server.product import ProductSettingsError, ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.session.context_frames import ContextLane
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


VOICE = {"id": "wait", "trigger": "等一下", "target": "voice",
         "instruction": "保持等待，不抢答。", "enabled": True}
SHADOW = {"id": "search", "trigger": "帮我查查", "target": "shadow",
          "instruction": "查询用户所问的资料并核实。", "enabled": True}


def test_validation_and_model_projections():
    entries = validate_entries([VOICE, SHADOW, {**SHADOW, "id": "off", "enabled": False}])
    voice = voice_projection(entries)
    assert "等一下" in voice and "保持等待" in voice
    assert "帮我查查" in voice and "影子代理" in voice
    assert "查询用户所问的资料" not in voice
    assert shadow_projection(entries) == [{
        "id": "search", "触发语义": "帮我查查", "执行目标": "查询用户所问的资料并核实。",
    }]
    with pytest.raises(ValueError):
        validate_entries([SHADOW, SHADOW])
    with pytest.raises(ValueError):
        validate_entries([{**VOICE, "target": "system"}])
    with pytest.raises(ValueError):
        validate_entries([{**VOICE, "target": []}])


def test_session_freezes_entries_until_reset(tmp_path):
    config = AppConfig(model_dir=tmp_path / "models",
                       persona=PersonaConfig(directory=tmp_path / "config" / "persona"))
    store = ProductSettingsStore(path=tmp_path / "config" / "product-settings.json", config=config)
    store.save_custom_instructions([VOICE])
    runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / ".runtime")
    from voice_terminal.handoff import OrganizedHandoff

    async def organize(archive, previous, entries):
        return OrganizedHandoff(previous, [], [], archive)

    runtime._organize_archive = organize
    assert runtime._instruction_snapshot == [VOICE]
    store.save_custom_instructions([SHADOW])
    assert runtime._instruction_snapshot == [VOICE]
    assert "custom_instructions" not in runtime.shadow_context.shadow_context_snapshot().frames[0].content
    asyncio.run(runtime.reset_session())
    assert runtime._instruction_snapshot == [SHADOW]
    environment = runtime.shadow_context.shadow_context_snapshot().frames[0].content
    assert environment["custom_instructions"] == shadow_projection([SHADOW])
    with pytest.raises(ProductSettingsError):
        store.save_persona({"personality": "", "user": "", "shadow": "越权"})


def test_voice_static_context_does_not_expose_shadow_goal(tmp_path):
    class Registry:
        def action_definitions(self):
            return []

        def begin_turn(self, _episode_id):
            return None

    class Output:
        status = "IDLE"
        speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

    async def emit(*_args):
        return None

    async def scenario():
        runtime = AgentEpisodeRuntime(
            None, Registry(), Output(), emit,
            conversation=ConversationRuntime(), ledger=SessionLedger(),
            persona_config=PersonaConfig(directory=tmp_path),
            custom_instructions_provider=lambda: [VOICE, SHADOW],
        )
        await runtime.ensure_episode(turn_id="turn", text="帮我查查")
        static = "\n".join(
            item["content"] for item in runtime.messages({"USER_INPUT_FINALIZED"})
            if item["context_lane"] == ContextLane.STATIC_INSTRUCTIONS
        )
        assert "帮我查查" in static and "保持等待" in static
        assert "查询用户所问的资料并核实" not in static
        if runtime._tokenizer_prewarm_task is not None:
            await runtime._tokenizer_prewarm_task

    asyncio.run(scenario())
