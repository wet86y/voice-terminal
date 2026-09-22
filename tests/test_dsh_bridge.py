from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from voice_terminal.dsh.bridge import DshBridgeError, DshMappingStore, DshShadowAgentSession
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


class MutableContext:
    shadow_context_epoch = 1

    def __init__(self) -> None:
        self.frames = [ShadowContextFrame(2, "user", "transcript", "do the work")]
        self.shadow_input_revision = 2

    def advance(self, content: str) -> None:
        self.shadow_input_revision += 1
        self.frames.append(ShadowContextFrame(
            self.shadow_input_revision, "user", "transcript", content,
        ))

    def shadow_context_snapshot(self) -> ShadowContextSnapshot:
        return ShadowContextSnapshot(
            epoch=1, revision=self.shadow_input_revision, frames=tuple(self.frames),
        )

    def shadow_context_update(self, since_revision: int, *, epoch: int):
        if epoch != 1:
            return self.shadow_context_snapshot()
        return ShadowContextUpdate(
            epoch=1, from_revision=since_revision,
            to_revision=self.shadow_input_revision,
            frames=tuple(frame for frame in self.frames if frame.revision > since_revision),
        )


class ReconciliationClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.state_revision = 0
        self.activation_id = ""
        self.fail_after_apply: set[str] = set()
        self.fail_without_apply: set[str] = set()

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, payload))
        if operation == "session.state":
            if operation in self.fail_without_apply:
                raise DshBridgeError(
                    "DSH Bridge transport failed: TimeoutError",
                    operation=operation, request_id=str(payload.get("request_id")),
                    transport=True,
                )
            return {
                "protocol_version": "1.0", "result": "accepted",
                "context_revision": self.state_revision,
                "activation_id": self.activation_id,
                "terminal": False, "agent_state": "running" if self.activation_id else "idle",
            }
        if operation in {"context.snapshot", "context.update"}:
            if operation not in self.fail_without_apply:
                self.state_revision = int(payload["context_revision"])
        if operation == "wake" and operation not in self.fail_without_apply:
            self.activation_id = str(payload["activation_id"])
        if operation in self.fail_after_apply or operation in self.fail_without_apply:
            raise DshBridgeError(
                "DSH Bridge transport failed: TimeoutError",
                operation=operation, request_id=str(payload.get("request_id")),
                transport=True,
            )
        return {"protocol_version": "1.0", "result": "accepted"}


class RetryOnceClient(ReconciliationClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once_operation: str | None = None
        self._failed_once = False

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation == self.fail_once_operation and not self._failed_once:
            self._failed_once = True
            self.calls.append((operation, payload))
            raise DshBridgeError(
                "DSH Bridge transport failed: TimeoutError",
                operation=operation, request_id=str(payload.get("request_id")),
                transport=True,
            )
        return await super().call(operation, payload)


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
        opened = self.client.calls[0][1]
        self.assertEqual(opened["runtime_reminder_interval_ms"], 20000)
        wake = self.client.calls[-1][1]
        self.assertNotIn("text", wake)
        self.assertNotIn("frames", wake)
        self.assertEqual(wake["context_revision"], 2)

    async def test_active_duplicate_wake_does_not_send_context_or_second_activation(self) -> None:
        self.assertEqual(await self.session.request_wake(), "started")
        self.assertEqual(await self.session.request_wake(), "coalesced")
        names = [name for name, _ in self.client.calls]
        self.assertEqual(names.count("context.snapshot"), 1)
        self.assertEqual(names.count("context.update"), 0)
        self.assertEqual(names.count("wake"), 1)

    async def test_custom_runtime_reminder_interval_is_sent_when_opening(self) -> None:
        session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "reminder-mappings.json"),
            context_source=FakeContext(),
            voice_session_id="reminder-session",
            workspace=Path(self.temp.name),
            runtime_reminder_interval_ms=12345,
        )

        await session.request_wake()

        opened = next(payload for name, payload in self.client.calls if name == "session.open")
        self.assertEqual(opened["runtime_reminder_interval_ms"], 12345)

    async def test_active_wake_syncs_new_revision_without_second_activation(self) -> None:
        context = MutableContext()
        session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "mutable-mappings.json"),
            context_source=context,
            voice_session_id="mutable-session",
            workspace=Path(self.temp.name),
        )
        self.assertEqual(await session.request_wake(), "started")
        context.advance("new guidance")
        self.assertEqual(await session.request_wake(), "coalesced")
        names = [name for name, _ in self.client.calls]
        self.assertEqual(names.count("wake"), 1)
        self.assertEqual(names.count("context.update"), 1)
        update = next(payload for name, payload in self.client.calls if name == "context.update")
        self.assertEqual(update["from_revision"], 2)
        self.assertEqual(update["context_revision"], 3)

    async def test_concurrent_active_wakes_collapse_to_latest_revision(self) -> None:
        context = MutableContext()
        session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "concurrent-mappings.json"),
            context_source=context,
            voice_session_id="concurrent-session",
            workspace=Path(self.temp.name),
        )
        await session.request_wake()
        context.advance("revision three")
        context.advance("revision four")

        results = await asyncio.gather(*(
            session.request_wake() for _ in range(3)
        ))

        self.assertEqual(results, ["coalesced", "coalesced", "coalesced"])
        updates = [payload for name, payload in self.client.calls if name == "context.update"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["from_revision"], 2)
        self.assertEqual(updates[0]["context_revision"], 4)
        self.assertEqual([frame["revision"] for frame in updates[0]["frames"]], [3, 4])
        self.assertEqual([name for name, _ in self.client.calls].count("wake"), 1)

    async def test_context_response_loss_reconciles_from_session_state(self) -> None:
        context = MutableContext()
        client = ReconciliationClient()
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "context-timeout.json"),
            context_source=context,
            voice_session_id="context-timeout-session",
            workspace=Path(self.temp.name),
        )
        self.assertEqual(await session.request_wake(), "started")
        context.advance("arrived while running")
        client.fail_after_apply.add("context.update")

        self.assertEqual(await session.request_wake(), "coalesced")
        self.assertEqual(session.last_applied_revision, 3)
        names = [name for name, _ in client.calls]
        self.assertEqual(names.count("context.update"), 1)
        self.assertEqual(names.count("session.state"), 1)

    async def test_transport_retry_reuses_the_original_request_id(self) -> None:
        context = MutableContext()
        client = RetryOnceClient()
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "stable-request-id.json"),
            context_source=context,
            voice_session_id="stable-request-id-session",
            workspace=Path(self.temp.name),
        )
        await session.request_wake()
        context.advance("retry this exact update")
        client.fail_once_operation = "context.update"

        self.assertEqual(await session.request_wake(), "coalesced")
        updates = [payload for name, payload in client.calls if name == "context.update"]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0]["request_id"], updates[1]["request_id"])
        self.assertEqual(session.last_applied_revision, 3)

    async def test_wake_response_loss_reconciles_matching_activation(self) -> None:
        client = ReconciliationClient()
        client.fail_after_apply.add("wake")
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "wake-timeout.json"),
            context_source=MutableContext(),
            voice_session_id="wake-timeout-session",
            workspace=Path(self.temp.name),
        )

        self.assertEqual(await session.request_wake(), "started")
        self.assertEqual(session.activation_id, client.activation_id)
        self.assertEqual([name for name, _ in client.calls].count("wake"), 1)

    async def test_active_unconfirmed_transport_loss_is_coalesced_not_failed(self) -> None:
        context = MutableContext()
        client = ReconciliationClient()
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "deferred-timeout.json"),
            context_source=context,
            voice_session_id="deferred-timeout-session",
            workspace=Path(self.temp.name),
            production_sink=self._events,
        )
        self.assertEqual(await session.request_wake(), "started")
        context.advance("latest guidance")
        client.fail_without_apply.update({"context.update", "session.state"})

        self.assertEqual(await session.request_wake(), "coalesced")
        self.assertEqual(self.events, [])
        self.assertIsNotNone(session.activation_id)
        await session.cancel()

    async def test_forward_revision_callback_reconciles_before_accepting_result(self) -> None:
        context = MutableContext()
        client = ReconciliationClient()
        events = []

        async def sink(values) -> None:
            events.extend(values)

        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "callback-reconcile.json"),
            context_source=context,
            voice_session_id="callback-reconcile-session",
            workspace=Path(self.temp.name),
            production_sink=sink,
        )
        await session.request_wake()
        activation = session.activation_id
        context.advance("accepted by plugin but response was lost")
        client.state_revision = 3
        payload = {
            "protocol_version": "1.0", "event_id": "event-forward-revision",
            "voice_session_id": "callback-reconcile-session",
            "dsh_session_id": session.dsh_session_id,
            "epoch": 1, "context_revision": 3, "activation_id": activation,
            "kind": "result", "text": "done", "payload": {},
        }

        self.assertEqual(await session.accept_event(payload), "accepted")
        self.assertEqual(session.last_applied_revision, 3)
        self.assertEqual(len(events), 1)

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
