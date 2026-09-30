from __future__ import annotations

import asyncio
from types import SimpleNamespace

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.server.websocket import SessionRuntime
from voice_terminal.shadow_agent.contracts import DurableVoiceEvent
from voice_terminal.shadow_agent.durable_mailbox import DurableVoiceMailbox


def test_mailbox_survives_restart_and_deduplicates(tmp_path):
    async def scenario():
        path = tmp_path / "voice-mailbox.json"
        event = DurableVoiceEvent(
            event_id="event-1", text="请向用户汇报",
            source_session_id="dsh", payload={"source": "dsh"},
        )
        first = DurableVoiceMailbox(path)
        assert await first.publish(event) == "accepted"
        assert await first.publish(event) == "duplicate"
        restored = DurableVoiceMailbox(path)
        assert await restored.pending() == [event]
        await restored.acknowledge(event.event_id)
        assert await DurableVoiceMailbox(path).pending() == []
        assert await restored.publish(event) == "duplicate"

    asyncio.run(scenario())


def test_dsh_event_waits_for_manual_voice_start(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(
            model_dir=tmp_path / "models",
            persona=PersonaConfig(directory=tmp_path / "persona"),
        ))
        runtime._voice_mailbox = DurableVoiceMailbox(tmp_path / "mailbox.json")
        runtime.shadow_session = SimpleNamespace(
            dsh_session_id="voice-shadow-current", last_applied_revision=0,
        )
        accepted = []

        class Controller:
            episode = SimpleNamespace(closed=False)

            async def accept_durable_voice_event(self, event):
                accepted.append(event)

        runtime.controller = Controller()
        runtime.state.listening = False
        assert await runtime.publish_voice_event({
            "event_id": "event-42", "source_session_id": "voice-shadow-current",
            "text": "定时汇报内容",
        }) == "accepted"
        assert accepted == []
        runtime._voice_mailbox = DurableVoiceMailbox(tmp_path / "mailbox.json")
        runtime.state.listening = True
        await runtime._deliver_voice_mailbox()
        assert [item.text for item in accepted] == ["定时汇报内容"]
        assert await runtime._voice_mailbox.pending() == []

    asyncio.run(scenario())
