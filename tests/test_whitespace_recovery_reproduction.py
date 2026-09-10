"""Regression coverage for the 2026-09-09 whitespace recovery storm.

The provider script mirrors the observed server behavior: an HTTP-successful
stream emits only literal space characters and then a normal terminal event.
Responses may make one immediate correction, then must return to the ordinary
event-driven scheduler without waking itself again.
"""

from __future__ import annotations

import asyncio

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_whitespace_only_completed_stream_stops_after_one_immediate_recovery():
    async def scenario() -> None:
        events: list[tuple[str, dict]] = []

        class Provider:
            transport = "responses"
            protocol_recovery_limit = 1

            def __init__(self) -> None:
                self.calls = 0
                self.requests = []
                self.next_event_completed = asyncio.Event()

            def stream_events(self, _messages, *, json_mode=False):
                assert json_mode is True
                self.requests.append(list(_messages))

                async def stream():
                    self.calls += 1
                    if self.calls == 3:
                        yield {"type": "content", "text": '{"speak":{"state":"wait","emotion":"calm","text":""},"actions":[]}'}
                        yield {"type": "finish", "reason": "completed"}
                        self.next_event_completed.set()
                        return
                    # Seven non-empty *space* deltas exactly exercise the live
                    # provider branch: ``if isinstance(text, str) and text``.
                    for _ in range(7):
                        yield {"type": "content", "text": " "}
                    yield {"type": "finish", "reason": "stop"}

                return stream()

        class Registry:
            def begin_turn(self, _episode_id):
                return None

            def cancel_turn(self, _episode_id):
                return None

            def action_definitions(self):
                return []

        class Handle:
            speech_id = "reproduction-speech"

            async def feed(self, _text):
                return None

            async def finish(self):
                return None

        class Output:
            active = False
            status = "IDLE"
            speak_snapshot = {"queue_status": "empty", "text_only_queue": True}

            async def begin(self, *_args, **_kwargs):
                return Handle()

            def cancel_all(self, _reason):
                return None

            async def close(self):
                return None

        async def emit(kind, payload):
            events.append((kind, payload))
            if kind == "error" and payload.get("code") == "LLM_PROTOCOL_RECOVERY_EXHAUSTED":
                exhausted.set()

        provider = Provider()
        exhausted = asyncio.Event()
        runtime = AgentEpisodeRuntime(provider, Registry(), Output(), emit,
                                      conversation=ConversationRuntime(), ledger=SessionLedger())
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="last-night-turn", text="在吗为什么不回复我")
        await asyncio.wait_for(exhausted.wait(), timeout=1.0)
        await asyncio.sleep(0.05)  # Prove exhaustion did not wake another request.

        traces = [payload for kind, payload in events if kind == "agent.trace"]
        kinds = [trace.get("kind") for trace in traces]
        dispatched = [trace for trace in traces if trace.get("kind") == "LLM_DISPATCHED"]

        # One ordinary request plus one immediate recovery.  Exhaustion is
        # reported locally and does not create a 400 ms self-retry loop.
        assert provider.calls == 2
        assert kinds.count("LLM_EMPTY_OUTPUT") == 2
        assert kinds.count("LLM_RECOVERY_QUEUED") == 1
        assert kinds.count("LLM_RECOVERY_STARTED") == 1
        assert kinds.count("LLM_RECOVERY_EXHAUSTED") == 1
        assert len(dispatched) == 2
        assert dispatched[0].get("mode") == "normal"
        assert dispatched[1].get("mode") == "exception_recovery"
        recovery_prompt = "\n".join(message.get("content", "") for message in provider.requests[1])
        assert "LLM_EMPTY_OUTPUT" in recovery_prompt
        assert "上一次输出为空白" in recovery_prompt
        assert "唯一一次即时自动纠正" in recovery_prompt
        assert any(kind == "agent.turn_settled" and payload.get("outcome") == "protocol_recovery_exhausted"
                   for kind, payload in events)

        # A real new event re-enters the ordinary 400 ms lane and receives the
        # deferred problem feedback exactly once.
        await runtime.observe("USER_INPUT_FINALIZED", turn_id="next-turn", text="继续")
        await asyncio.wait_for(provider.next_event_completed.wait(), timeout=1.0)
        assert provider.calls == 3
        assert any("上一轮恢复问题" in message.get("content", "")
                   for message in provider.requests[2])
        assert not runtime.deferred_recovery_feedback

        await runtime.stop()

    asyncio.run(scenario())
