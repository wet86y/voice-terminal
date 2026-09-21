from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from voice_terminal.dsh.bridge import DshMappingStore, DshShadowAgentSession
from voice_terminal.shadow_agent.contracts import (
    ShadowContextFrame,
    ShadowContextSnapshot,
    ShadowContextUpdate,
)
from voice_terminal.shadow_agent.provider import ShadowProviderResponse


class FakeContext:
    shadow_context_epoch = 1
    shadow_input_revision = 2

    def shadow_context_snapshot(self) -> ShadowContextSnapshot:
        return ShadowContextSnapshot(
            epoch=1,
            revision=2,
            frames=(ShadowContextFrame(2, "user", "transcript", "do the work"),),
        )

    def shadow_context_update(self, since_revision: int, *, epoch: int):
        if epoch != 1:
            return self.shadow_context_snapshot()
        return ShadowContextUpdate(
            epoch=1,
            from_revision=since_revision,
            to_revision=2,
            frames=() if since_revision >= 2 else self.shadow_context_snapshot().frames,
        )


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, payload))
        return {"protocol_version": "1.0", "result": "accepted"}


class RecordingStatusProvider:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> ShadowProviderResponse:
        self.requests.append(dict(kwargs))
        return ShadowProviderResponse(output=({
            "type": "message",
            "content": [{"type": "output_text", "text": "已提交一个模型步骤，当前没有待处理工具。"}],
        },), usage={"output_tokens": 18})


class ObserverClient(FakeClient):
    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, payload))
        if operation == "observer.snapshot":
            return {
                "protocol_version": "1.0",
                "result": "accepted",
                "mode": "snapshot",
                "trace_revision": 3,
                "agent_state": "running",
                "committed_model_steps": 1,
                "pending_tools": [],
                "pending_subagents": [],
                "records": [{"kind": "turn/start"}],
            }
        return {"protocol_version": "1.0", "result": "accepted"}


class DshBridgeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.events = []
        self.session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "mappings.json"),
            context_source=FakeContext(),
            voice_session_id="voice-session",
            workspace=Path(self.temp.name),
            production_sink=self._events,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _events(self, values) -> None:
        self.events.extend(values)

    async def test_wake_opens_injects_snapshot_then_sends_content_free_activation(self) -> None:
        self.assertEqual(await self.session.request_wake(), "started")
        self.assertEqual([name for name, _ in self.client.calls], [
            "session.open", "context.snapshot", "wake",
        ])
        wake = self.client.calls[-1][1]
        self.assertNotIn("text", wake)
        self.assertNotIn("frames", wake)
        self.assertEqual(wake["context_revision"], 2)

    async def test_event_requires_exact_binding_epoch_revision_and_activation(self) -> None:
        await self.session.request_wake()
        payload = {
            "protocol_version": "1.0",
            "event_id": "event-0001",
            "voice_session_id": "voice-session",
            "dsh_session_id": self.session.dsh_session_id,
            "epoch": 1,
            "context_revision": 2,
            "activation_id": self.session.activation_id,
            "kind": "result",
            "text": "done",
            "payload": {},
        }
        self.assertEqual(await self.session.accept_event(payload), "accepted")
        self.assertEqual(await self.session.accept_event(payload), "duplicate")
        self.assertEqual(len(self.events), 1)
        stale = {**payload, "event_id": "event-0002", "epoch": 0}
        self.assertEqual(await self.session.accept_event(stale), "stale")

    async def test_no_action_is_terminal_without_semantic_mailbox_event(self) -> None:
        await self.session.request_wake()
        payload = {
            "protocol_version": "1.0", "event_id": "event-no-action",
            "voice_session_id": "voice-session", "dsh_session_id": self.session.dsh_session_id,
            "epoch": 1, "context_revision": 2, "activation_id": self.session.activation_id,
            "kind": "no_action", "reason": "nothing pending",
        }
        self.assertEqual(await self.session.accept_event(payload), "accepted")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.session.activation_id)

    async def test_reset_retires_old_dsh_session_and_rotates_identity(self) -> None:
        await self.session.request_wake()
        old = self.session.dsh_session_id
        await self.session.reset()
        self.assertNotEqual(old, self.session.dsh_session_id)
        self.assertIn("session.retire", [name for name, _ in self.client.calls])
        self.assertEqual(self.session.last_applied_revision, -1)

    async def test_status_observer_uses_dsh_provider_adapter(self) -> None:
        provider = RecordingStatusProvider()
        session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "status-mappings.json"),
            context_source=FakeContext(),
            voice_session_id="status-session",
            workspace=Path(self.temp.name),
            status_provider=provider,  # type: ignore[arg-type]
        )

        text = await session._observer.summarize(  # noqa: SLF001 - adapter contract test
            [{"role": "user", "content": "committed observer snapshot"}],
            session._status_provider_complete,  # noqa: SLF001
            forced=True,
        )

        self.assertIn("已提交", text)
        request = provider.requests[0]
        self.assertTrue(request["instructions"])
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["reasoning_effort"], "none")
        self.assertNotIn("timeout_ms", request)
        self.assertNotIn("retry", request)

    async def test_status_observer_projects_summary_as_valid_ui_activity(self) -> None:
        provider = RecordingStatusProvider()
        activities: list[dict[str, Any]] = []
        events = []

        async def activity_sink(values: list[dict[str, Any]]) -> None:
            activities.extend(values)

        async def event_sink(values) -> None:
            events.extend(values)

        session = DshShadowAgentSession(
            client=ObserverClient(),  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "observer-mappings.json"),
            context_source=FakeContext(),
            voice_session_id="observer-session",
            workspace=Path(self.temp.name),
            status_provider=provider,  # type: ignore[arg-type]
            activity_sink=activity_sink,
            production_sink=event_sink,
        )
        session.activation_id = "activation-1"
        session._observer_force = True  # noqa: SLF001 - observer scheduling contract

        await session._run_observer()  # noqa: SLF001

        status = activities[-1]
        self.assertEqual(status["state"], "observer_status")
        self.assertEqual(status["stream"], "thinking")
        self.assertEqual(status["phase"], "delta")
        self.assertEqual(status["agent_state"], "running")
        self.assertIsInstance(status["timestamp"], str)
        self.assertTrue(status["timestamp"])
        self.assertIn("已提交", status["text"])
        self.assertEqual(session.snapshot()["trace_revision"], 3)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "status")
        self.assertEqual(events[0].wake_policy, "normal")
        self.assertEqual(events[0].payload["trace_revision"], 3)


if __name__ == "__main__":
    unittest.main()
