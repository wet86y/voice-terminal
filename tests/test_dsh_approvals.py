"""DSH（代理执行宿主）授权的请求归属、语音语义及临时窗口边界。"""
import asyncio
import json

import pytest

from tests.test_dsh_bridge import FakeClient, FakeContext
from voice_terminal.dsh.bridge import DshBridgeError, DshMappingStore, DshShadowAgentSession
from voice_terminal.shadow_agent.provider import ShadowProviderResponse


def setup_session(tmp_path, *, voice=False, continuous=False):
    events = []

    async def sink(values):
        events.extend(values)

    client = FakeClient()
    session = DshShadowAgentSession(client=client, mapping_store=DshMappingStore(tmp_path / "map.json"),
                                    context_source=FakeContext(), voice_session_id="voice-a", workspace=tmp_path,
                                    production_sink=sink)
    session.activation_id = "activation-a"
    session.last_applied_revision = 2
    session._run_seq = 1
    state = {"window": ("voice-a", 1), "input": ("turn-a", "执行这项操作"), "dialogue": (1, "先执行这项操作")}
    session.approvals.window_provider = lambda: state["window"]
    session.approvals.input_provider = lambda: state["input"]
    session.approvals.dialogue_provider = lambda _turn_id=None: state["dialogue"]
    session.approvals.configure(voice, continuous)
    return session, client, state, events


async def pending(session, approval_id, seq):
    return await session.accept_activity({
        **session._envelope(), "protocol_version": "2.0", "context_revision": session.last_applied_revision,
        "activation_id": session.activation_id, "action_seq": seq,
        "activity": {"stream": "approval", "phase": "started", "item_id": f"approval-{approval_id}",
                     "approval_id": approval_id, "tool_name": "bash", "text": "创建用户指定的目录"},
    })


class DecisionProvider:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return ShadowProviderResponse(output=({"type": "message", "content": [{"type": "output_text",
            "text": json.dumps({"decision": self.decision})}]},), usage={})


def test_continuous_button_maps_each_original_request_and_expires_at_window_boundary(tmp_path):
    async def run():
        session, client, state, _ = setup_session(tmp_path)
        assert await pending(session, "first", 1) == "accepted"
        await session.decide_approval("first", "allowed-until-silence", 1)
        assert session.snapshot()["continuous_approval"] is True
        assert client.calls[-1][1]["decision"] == "allowed-once"
        assert await pending(session, "second", 2) == "accepted"
        await session._approval_auto_task
        decisions = [body for name, body in client.calls if name == "approval.decide"]
        assert [body["approval_id"] for body in decisions] == ["first", "second"]
        assert all(body["activation_id"] == "activation-a" for body in decisions)
        state["window"] = None  # 模拟真实静默或手动停止录音。
        assert session.snapshot()["continuous_approval"] is False
        state["window"] = ("voice-a", 2)
        assert await pending(session, "third", 3) == "accepted"
        assert "third" in session.approvals.pending
        assert len([name for name, _ in client.calls if name == "approval.decide"]) == 2
        await session.close()
    asyncio.run(run())


def test_continuous_button_requires_active_recording_and_only_grants_after_acceptance(tmp_path):
    async def run():
        session, client, state, _ = setup_session(tmp_path)
        await pending(session, "first", 1)
        state["window"] = None
        with pytest.raises(DshBridgeError, match="录音"):
            await session.decide_approval("first", "allowed-until-silence", 1)
        assert client.calls == []
        assert not session.approvals.continuous_active
    asyncio.run(run())


@pytest.mark.parametrize("decision", ["approve", "reject", "unrelated"])
def test_voice_requires_new_input_and_explicit_semantic_verdict(tmp_path, decision):
    async def run():
        session, client, state, events = setup_session(tmp_path, voice=True, continuous=True)
        provider = DecisionProvider(decision)
        session.gate_provider = provider
        await pending(session, "first", 1)
        assert events[-1].kind == "information"
        assert "等待用户明确同意或拒绝" in events[-1].text
        assert not await session.respond_to_voice_approval(*state["input"])
        assert provider.calls == []
        assert not session.approvals.continuous_active
        state["input"] = ("turn-b", "可以继续")
        state["dialogue"] = (2, "需要授权创建你指定的目录，同意吗？")
        handled = await session.respond_to_voice_approval(*state["input"])
        assert handled is (decision != "unrelated")
        assert session.approvals.continuous_active is (decision == "approve")
        calls = [body for name, body in client.calls if name == "approval.decide"]
        assert len(calls) == (0 if decision == "unrelated" else 1)
        if calls:
            assert calls[0]["approval_id"] == "first"
            assert calls[0]["decision"] == ("allowed-once" if decision == "approve" else "rejected")
        assert provider.calls[0]["tools"] == []
        assert json.loads(provider.calls[0]["input_items"][0]["content"])["最新实际传达助手文本"] == state["dialogue"][1]
        if handled:
            assert not await session.respond_to_voice_approval(*state["input"])
        else:
            assert "first" in session.approvals.pending
        await session.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["stop", "new_input", "request_settled", "setting_off"])
def test_voice_confirmation_discards_verdict_if_scope_changes_during_inference(tmp_path, change):
    async def run():
        session, client, state, _ = setup_session(tmp_path, voice=True, continuous=True)
        entered, release = asyncio.Event(), asyncio.Event()

        class BlockingProvider(DecisionProvider):
            async def complete(self, **kwargs):
                entered.set()
                await release.wait()
                return await super().complete(**kwargs)

        session.gate_provider = BlockingProvider("approve")
        await pending(session, "first", 1)
        state["input"] = ("turn-b", "同意")
        task = asyncio.create_task(session.respond_to_voice_approval(*state["input"]))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        if change == "stop": state["window"] = None
        if change == "new_input": state["input"] = ("turn-c", "别执行了")
        if change == "request_settled": session.approvals.pending.clear()
        if change == "setting_off": session.approvals.configure(False, True)
        release.set()
        assert not await task
        assert client.calls == []
        assert not session.approvals.continuous_active
        await session.close()
    asyncio.run(run())
