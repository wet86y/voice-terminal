from __future__ import annotations

import asyncio
import json
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
        return {"protocol_version": "2.0", "result": "accepted"}


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
                "protocol_version": "2.0", "result": "accepted",
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
        return {"protocol_version": "2.0", "result": "accepted"}


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
                "protocol_version": "2.0",
                "result": "accepted",
                "mode": "snapshot",
                "trace_revision": 3,
                "agent_state": "running",
                "committed_model_steps": 1,
                "pending_tools": [],
                "pending_subagents": [],
                "records": [{"kind": "turn/start"}],
            }
        return {"protocol_version": "2.0", "result": "accepted"}


class GateClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.activation_id = ""
        self.synced_revision = 0
        self.queued_revision = 0
        self.deferred_revision = 0

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, payload))
        if operation in {"context.snapshot", "context.update"}:
            self.synced_revision = int(payload["context_revision"])
            if payload.get("guidance_delivery") != "hold":
                self.queued_revision = self.synced_revision
        elif operation == "wake":
            self.activation_id = str(payload["activation_id"])
            if payload.get("guidance_delivery") == "deferred_next_turn":
                self.queued_revision = self.synced_revision
                self.deferred_revision = 0
        elif operation == "context.guidance":
            if payload["timing"] == "defer_once":
                self.deferred_revision = int(payload["context_revision"])
            else:
                self.queued_revision = int(payload["context_revision"])
                self.deferred_revision = 0
        elif operation == "observer.snapshot":
            return {"result": "accepted", "records": [
                {"type": "voice/activation", "data": {"activation_id": self.activation_id}},
                {"type": "step/end", "data": {"committed": True}},
            ]}
        elif operation == "session.state":
            return {"result": "accepted", "context_revision": self.synced_revision,
                    "activation_id": self.activation_id,
                    "queued_revision": self.queued_revision,
                    "deferred_revision": self.deferred_revision,
                    "ready_for_handoff": not self.activation_id}
        return {"protocol_version": "2.0", "result": "accepted"}


class LostGuidanceResponseClient(GateClient):
    def __init__(self) -> None:
        super().__init__()
        self.lost_response = False

    async def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await super().call(operation, payload)
        if operation == "context.guidance" and not self.lost_response:
            self.lost_response = True
            raise DshBridgeError(
                "DSH Bridge transport failed: TimeoutError",
                operation=operation, request_id=str(payload.get("request_id")),
                transport=True,
            )
        return response


class GateProvider:
    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = verdicts
        self.calls = 0

    async def complete(self, **kwargs: Any) -> ShadowProviderResponse:
        self.calls += 1
        timing = self.verdicts.pop(0)
        relation = "irrelevant" if timing == "defer_once" else "change"
        return ShadowProviderResponse(output=({"type": "message", "content": [{
            "type": "output_text",
            "text": json.dumps({"timing": timing, "relation": relation, "reason": "fixture"}),
        }]},), usage={})


class DshBridgeSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.events = []
        self.mapping_store = DshMappingStore(Path(self.temp.name) / "mappings.json")
        self.session = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=self.mapping_store,
            context_source=FakeContext(),
            voice_session_id="voice-session",
            workspace=Path(self.temp.name),
            production_sink=self._events,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _events(self, values) -> None:
        self.events.extend(values)

    async def test_plugin_telemetry_keeps_latest_watermark_and_rejects_foreign_session(self) -> None:
        await self.session.request_wake()
        payload = {"protocol_version": "2.0", "voice_session_id": "voice-session",
                   "dsh_session_id": self.session.dsh_session_id, "epoch": self.session.epoch,
                   "context_revision": self.session.last_applied_revision,
                   "activation_id": self.session.activation_id, "trace_revision": 8,
                   "state": "running", "telemetry": {"context_token_estimate": 12000,
                   "context_limit_tokens": 100000, "context_usage_percent": 12,
                   "provider_input_tokens": 10000, "provider_cached_tokens": 8000,
                   "provider_cache_hit_ratio": 0.8, "api_key": "do-not-export"}}
        self.assertEqual(await self.session.accept_activity(payload), "accepted")
        self.assertEqual(self.session.snapshot()["provider_cache_hit_ratio"], 0.8)
        self.assertNotIn("api_key", self.session.snapshot())
        self.session._update_telemetry({"trace_revision": 7, "telemetry": {"provider_cache_hit_ratio": 0}})
        self.assertEqual(self.session.snapshot()["provider_cache_hit_ratio"], 0.8)
        await self.session.accept_activity({**payload, "dsh_session_id": "foreign", "trace_revision": 9, "telemetry": {"provider_cache_hit_ratio": 0}})
        self.assertEqual(self.session.snapshot()["provider_cache_hit_ratio"], 0.8)
        # 终态后相同会话的最终用量仍可接收，但不恢复已结束的激活。
        self.session.activation_id = None
        await self.session.accept_activity({**payload, "activation_id": "", "trace_revision": 10,
                                           "telemetry": {"provider_cache_hit_ratio": 0.9}})
        self.assertEqual(self.session.snapshot()["provider_cache_hit_ratio"], 0.9)
        self.assertIsNone(self.session.activation_id)

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

    async def test_early_silence_requires_current_activation_and_is_idempotent(self) -> None:
        self.assertEqual(await self.session.request_wake(), "started")
        payload = {
            "protocol_version": "2.0", "request_id": "early-request-1",
            "voice_session_id": self.session.voice_session_id,
            "dsh_session_id": self.session.dsh_session_id,
            "epoch": self.session.epoch,
            "context_revision": self.session.last_applied_revision,
            "activation_id": self.session.activation_id,
        }
        calls = []

        async def request() -> str:
            calls.append(True)
            return "scheduled"

        self.assertEqual(await self.session.accept_early_silence(payload, request),
                         ("accepted", "scheduled"))
        self.assertEqual(await self.session.accept_early_silence(payload, request),
                         ("duplicate", "scheduled"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(await self.session.accept_early_silence(
            {**payload, "activation_id": "old-activation", "request_id": "early-request-2"}, request,
        ), ("stale", "stale_binding"))
        self.assertEqual(await self.session.accept_early_silence(
            {**payload, "request_id": "early-request-3", "target": "asr"}, request,
        ), ("conflict", "invalid_request"))
        self.assertEqual(len(calls), 1)

        for invalid in (None, "", 1):
            self.assertEqual(await self.session.accept_early_silence(
                {**payload, "activation_id": invalid}, request,
            ), ("conflict", "invalid_request"))
        self.session.activation_id = None
        self.assertEqual(await self.session.accept_early_silence(payload, request),
                         ("stale", "stale_binding"))
        self.assertEqual(len(calls), 1)

    async def test_dsh_gate_can_defer_repeatedly_then_handoff_and_guide_new_answer(self) -> None:
        context = MutableContext()
        client = GateClient()
        provider = GateProvider(["defer_once", "defer_once", "inject_now"])
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "gate.json"),
            context_source=context, voice_session_id="gate-session",
            workspace=Path(self.temp.name), gate_provider=provider, status_provider=None,
        )
        session.set_context_gate_enabled(True)
        self.assertEqual(await session.request_wake(), "started")
        first_activation = session.activation_id
        self.assertEqual(provider.calls, 0)
        context.advance("irrelevant while working")
        with self.assertLogs("voice_terminal.dsh.bridge", level="INFO") as gate_logs:
            self.assertEqual(await session.request_wake(), "coalesced")
        self.assertIn("dsh.context_gate requested", "\n".join(gate_logs.output))
        self.assertIn("timing=defer_once", "\n".join(gate_logs.output))
        self.assertEqual(await session.request_wake(), "coalesced")
        self.assertEqual(provider.calls, 2)
        self.assertEqual([payload["timing"] for name, payload in client.calls
                          if name == "context.guidance"], ["defer_once", "defer_once"])
        self.assertEqual([payload.get("guidance_delivery") for name, payload in client.calls
                          if name == "context.update"], ["hold"])
        terminal = {
            "protocol_version": "2.0", "request_id": "request-gate-terminal",
            "event_id": "event-gate-terminal", "voice_session_id": "gate-session",
            "dsh_session_id": session.dsh_session_id, "epoch": 1,
            "context_revision": 3, "activation_id": first_activation,
            "event": {"type": "input_required", "question": "Need an answer?", "options": []},
        }
        self.assertEqual(await session.accept_event(terminal), "accepted")
        client.activation_id = ""
        for _ in range(50):
            if session.activation_id is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(session.activation_id)
        self.assertNotEqual(session.activation_id, first_activation)
        second_wake = [payload for name, payload in client.calls if name == "wake"][-1]
        self.assertEqual(second_wake.get("guidance_delivery"), "deferred_next_turn")
        self.assertEqual(await session.accept_event(terminal), "duplicate")
        self.assertEqual([name for name, _ in client.calls].count("wake"), 2)
        self.assertEqual(await session.accept_activity({
            "protocol_version": "2.0", "voice_session_id": "gate-session",
            "dsh_session_id": session.dsh_session_id, "epoch": 1,
            "context_revision": 3, "activation_id": session.activation_id,
            "claimed_revision": 3, "state": "running",
        }), "accepted")
        self.assertEqual(session.last_claimed_revision, 3)
        context.advance("user answer changes the task")
        self.assertEqual(await session.request_wake(), "coalesced")
        self.assertEqual(provider.calls, 3)
        self.assertEqual([payload["timing"] for name, payload in client.calls
                          if name == "context.guidance"][-1], "inject_now")
        self.assertEqual(session.last_guided_revision, 4)
        await session.close()

    async def test_disabling_dsh_gate_flushes_pending_context_then_stays_direct(self) -> None:
        context = MutableContext()
        client = GateClient()
        provider = GateProvider(["defer_once"])
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "gate-disable.json"),
            context_source=context, voice_session_id="gate-disable-session",
            workspace=Path(self.temp.name), gate_provider=provider,
        )
        session.set_context_gate_enabled(True)
        await session.request_wake()
        context.advance("defer then disable")
        await session.request_wake()
        session.set_context_gate_enabled(False)
        await session.flush_context_gate()
        self.assertEqual(session.last_guided_revision, 3)
        self.assertEqual([payload["timing"] for name, payload in client.calls
                          if name == "context.guidance"], ["defer_once", "inject_now"])
        context.advance("direct again")
        await session.request_wake()
        self.assertEqual(provider.calls, 1)
        self.assertNotIn("guidance_delivery", [payload for name, payload in client.calls
                                               if name == "context.update"][-1])
        await session.close()

    async def test_lost_guidance_response_uses_state_without_replaying_decision(self) -> None:
        context = MutableContext()
        client = LostGuidanceResponseClient()
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "gate-lost-response.json"),
            context_source=context, voice_session_id="gate-lost-response",
            workspace=Path(self.temp.name), gate_provider=GateProvider(["defer_once"]),
        )
        session.set_context_gate_enabled(True)
        await session.request_wake()
        context.advance("delay despite lost response")
        self.assertEqual(await session.request_wake(), "coalesced")
        self.assertTrue(client.lost_response)
        self.assertEqual(client.deferred_revision, 3)
        self.assertEqual(session.snapshot()["context_gate"]["deferred_revision"], 3)
        self.assertEqual([name for name, _ in client.calls].count("context.guidance"), 1)
        await session.close()

    async def test_deferred_context_handoff_works_for_other_terminal_kinds(self) -> None:
        terminal_bodies = [
            {"type": "result", "text": "Done"},
            {"type": "no_action", "reason": "No action"},
            {"type": "failed", "message": "Infrastructure error", "code": "fixture", "infrastructure": True},
        ]
        for index, body in enumerate(terminal_bodies):
            context = MutableContext()
            client = GateClient()
            session = DshShadowAgentSession(
                client=client,  # type: ignore[arg-type]
                mapping_store=DshMappingStore(Path(self.temp.name) / f"gate-terminal-{index}.json"),
                context_source=context, voice_session_id=f"gate-terminal-{index}",
                workspace=Path(self.temp.name), gate_provider=GateProvider(["defer_once"]),
            )
            session.set_context_gate_enabled(True)
            await session.request_wake()
            first = session.activation_id
            context.advance("delayed")
            await session.request_wake()
            event = {
                "protocol_version": "2.0", "request_id": f"request-kind-{index}",
                "event_id": f"event-kind-{index}", "voice_session_id": f"gate-terminal-{index}",
                "dsh_session_id": session.dsh_session_id, "epoch": 1,
                "context_revision": 3, "activation_id": first, "event": body,
            }
            self.assertEqual(await session.accept_event(event), "accepted")
            client.activation_id = ""
            for _ in range(50):
                if session.activation_id is not None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(session.activation_id)
            self.assertNotEqual(session.activation_id, first)
            self.assertEqual([name for name, _ in client.calls].count("wake"), 2)
            await session.close()

    async def test_deferred_gate_state_restores_as_new_activation(self) -> None:
        context = MutableContext()
        store = DshMappingStore(Path(self.temp.name) / "gate-restore.json")
        first_client = GateClient()
        first = DshShadowAgentSession(
            client=first_client,  # type: ignore[arg-type]
            mapping_store=store, context_source=context,
            voice_session_id="gate-restore-session", workspace=Path(self.temp.name),
            gate_provider=GateProvider(["defer_once"]),
        )
        first.set_context_gate_enabled(True)
        await first.request_wake()
        old_activation = first.activation_id
        context.advance("recover after restart")
        await first.request_wake()
        await first.close()
        second_client = GateClient()
        second = DshShadowAgentSession(
            client=second_client,  # type: ignore[arg-type]
            mapping_store=store, context_source=context,
            voice_session_id="gate-restore-session", workspace=Path(self.temp.name),
        )
        second.set_context_gate_enabled(True)
        await second.resume_pending_gate()
        for _ in range(50):
            if second.activation_id is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(second.activation_id)
        self.assertNotEqual(second.activation_id, old_activation)
        wake = [payload for name, payload in second_client.calls if name == "wake"][-1]
        self.assertEqual(wake.get("guidance_delivery"), "deferred_next_turn")
        self.assertEqual(second.last_guided_revision, 3)
        await second.close()

    async def test_disabled_dsh_gate_uses_original_update_path(self) -> None:
        context = MutableContext()
        client = GateClient()
        provider = GateProvider(["defer_once"])
        session = DshShadowAgentSession(
            client=client,  # type: ignore[arg-type]
            mapping_store=DshMappingStore(Path(self.temp.name) / "gate-off.json"),
            context_source=context, voice_session_id="gate-off-session",
            workspace=Path(self.temp.name), gate_provider=provider,
        )
        await session.request_wake()
        context.advance("straight through")
        await session.request_wake()
        self.assertEqual(provider.calls, 0)
        self.assertEqual([name for name, _ in client.calls].count("context.guidance"), 0)
        update = next(payload for name, payload in client.calls if name == "context.update")
        self.assertNotIn("guidance_delivery", update)
        await session.close()

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
            "protocol_version": "2.0", "request_id": "request-forward-revision",
            "event_id": "event-forward-revision",
            "voice_session_id": "callback-reconcile-session",
            "dsh_session_id": session.dsh_session_id,
            "epoch": 1, "context_revision": 3, "activation_id": activation,
            "event": {"type": "result", "text": "done"},
        }

        self.assertEqual(await session.accept_event(payload), "accepted")
        self.assertEqual(session.last_applied_revision, 3)
        self.assertEqual(len(events), 1)

    async def test_event_requires_exact_binding_epoch_revision_and_activation(self) -> None:
        await self.session.request_wake()
        payload = {
            "protocol_version": "2.0",
            "request_id": "request-0001",
            "event_id": "event-0001",
            "voice_session_id": "voice-session",
            "dsh_session_id": self.session.dsh_session_id,
            "epoch": 1,
            "context_revision": 2,
            "activation_id": self.session.activation_id,
            "event": {"type": "result", "text": "done"},
        }
        self.assertEqual(await self.session.accept_event(payload), "accepted")
        self.assertEqual(await self.session.accept_event(payload), "duplicate")
        self.assertEqual(len(self.events), 1)
        stale = {**payload, "event_id": "event-0002", "epoch": 0}
        self.assertEqual(await self.session.accept_event(stale), "stale")

    async def test_no_action_is_terminal_without_semantic_mailbox_event(self) -> None:
        await self.session.request_wake()
        payload = {
            "protocol_version": "2.0", "request_id": "request-no-action",
            "event_id": "event-no-action",
            "voice_session_id": "voice-session", "dsh_session_id": self.session.dsh_session_id,
            "epoch": 1, "context_revision": 2, "activation_id": self.session.activation_id,
            "event": {"type": "no_action", "reason": "nothing pending"},
        }
        self.assertEqual(await self.session.accept_event(payload), "accepted")
        self.assertEqual([item.kind for item in self.events], ["no_action"])
        self.assertIsNone(self.session.activation_id)

    async def test_concurrent_terminal_events_have_exactly_one_winner_and_persist_receipt(self) -> None:
        await self.session.request_wake()
        activation = self.session.activation_id

        def terminal(event_id: str, text: str) -> dict[str, Any]:
            return {
                "protocol_version": "2.0", "request_id": f"request-{event_id}",
                "event_id": event_id, "voice_session_id": "voice-session",
                "dsh_session_id": self.session.dsh_session_id, "epoch": 1,
                "context_revision": 2, "activation_id": activation,
                "event": {"type": "result", "text": text},
            }

        first = terminal("event-terminal-a", "result A")
        second = terminal("event-terminal-b", "result B")
        outcomes = await asyncio.gather(
            self.session.accept_event(first), self.session.accept_event(second),
        )

        self.assertCountEqual(outcomes, ["accepted", "stale"])
        accepted_payload = first if outcomes[0] == "accepted" else second
        self.assertEqual(await self.session.accept_event(accepted_payload), "duplicate")
        conflicting = {
            **accepted_payload,
            "event": {"type": "result", "text": "different body"},
        }
        self.assertEqual(await self.session.accept_event(conflicting), "conflict")
        mapping = json.loads(
            (Path(self.temp.name) / "mappings.json").read_text(encoding="utf-8"),
        )
        row = mapping["sessions"]["voice-session"]
        self.assertEqual(mapping["schema_version"], 2)
        self.assertEqual(row["state"], "idle")
        self.assertIsNone(row["last_activation_id"])
        self.assertEqual(row["last_terminal_activation_id"], activation)
        self.assertEqual(row["last_terminal_event_id"], accepted_payload["event_id"])
        self.assertTrue(row["last_terminal_event_digest"])

    async def test_terminal_replay_receipt_survives_new_dsh_binding_and_process_restart(self) -> None:
        await self.session.request_wake()
        payload = {
            "protocol_version": "2.0", "request_id": "request-persisted-terminal",
            "event_id": "event-persisted-terminal", "voice_session_id": "voice-session",
            "dsh_session_id": self.session.dsh_session_id, "epoch": 1,
            "context_revision": 2, "activation_id": self.session.activation_id,
            "event": {"type": "result", "text": "self-contained result"},
        }
        self.assertEqual(await self.session.accept_event(payload), "accepted")

        # A new process never resumes the old DSH session, but can acknowledge
        # its one persisted terminal receipt before the new binding opens.
        second = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=self.mapping_store,
            context_source=FakeContext(),
            voice_session_id="voice-session",
            workspace=Path(self.temp.name),
        )
        old_dsh_session = payload["dsh_session_id"]
        self.assertNotEqual(second.dsh_session_id, old_dsh_session)
        await second.prepare_callback_binding(payload)
        self.assertTrue(second.is_terminal_replay(payload))
        self.assertEqual(await second.accept_event(payload), "duplicate")
        conflicting_replay = {
            **payload, "event": {"type": "result", "text": "different result"},
        }
        self.assertTrue(second.matches_last_terminal_identity(conflicting_replay))
        self.assertEqual(await second.accept_event(conflicting_replay), "conflict")

        # Opening a fresh DSH session preserves the receipt for another restart.
        await second.request_wake()
        third = DshShadowAgentSession(
            client=self.client,  # type: ignore[arg-type]
            mapping_store=self.mapping_store,
            context_source=FakeContext(),
            voice_session_id="voice-session",
            workspace=Path(self.temp.name),
        )
        await third.prepare_callback_binding(payload)
        self.assertTrue(third.is_terminal_replay(payload))
        self.assertEqual(await third.accept_event(payload), "duplicate")

    async def test_information_is_nonterminal_and_late_information_is_stale(self) -> None:
        await self.session.request_wake()
        activation = self.session.activation_id
        base = {
            "protocol_version": "2.0", "voice_session_id": "voice-session",
            "dsh_session_id": self.session.dsh_session_id, "epoch": 1,
            "context_revision": 2, "activation_id": activation,
        }
        information = {
            **base, "request_id": "request-information", "event_id": "event-information",
            "event": {"type": "information", "text": " newest fact "},
        }
        self.assertEqual(await self.session.accept_event(information), "accepted")
        self.assertEqual(self.events[-1].text, "newest fact")
        self.assertEqual(self.events[-1].source, "agent")
        terminal = {
            **base, "request_id": "request-result", "event_id": "event-result",
            "event": {"type": "result", "text": "self-contained result"},
        }
        self.assertEqual(await self.session.accept_event(terminal), "accepted")
        late = {
            **base, "request_id": "request-late", "event_id": "event-late",
            "event": {"type": "information", "text": "too late"},
        }
        self.assertEqual(await self.session.accept_event(late), "stale")

    async def test_strict_event_union_rejects_unknown_and_inapplicable_fields(self) -> None:
        await self.session.request_wake()
        base = {
            "protocol_version": "2.0", "request_id": "request-invalid",
            "event_id": "event-invalid", "voice_session_id": "voice-session",
            "dsh_session_id": self.session.dsh_session_id, "epoch": 1,
            "context_revision": 2, "activation_id": self.session.activation_id,
        }
        invalid = {**base, "event": {"type": "progress", "text": "ok", "options": []}}
        self.assertEqual(await self.session.accept_event(invalid), "conflict")
        blank = {**base, "event_id": "event-blank", "event": {"type": "information", "text": "  "}}
        self.assertEqual(await self.session.accept_event(blank), "conflict")
        legacy = {**base, "event_id": "event-legacy", "kind": "result", "text": "legacy"}
        self.assertEqual(await self.session.accept_event(legacy), "conflict")
        invalid_envelope = {**base, "event_id": "event-bool-epoch", "epoch": True,
                            "event": {"type": "progress", "text": "ok"}}
        self.assertEqual(await self.session.accept_event(invalid_envelope), "conflict")

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
        self.assertEqual(events[0].kind, "information")
        self.assertEqual(events[0].source, "observer")
        self.assertEqual(events[0].activation_id, "activation-1")


if __name__ == "__main__":
    unittest.main()
