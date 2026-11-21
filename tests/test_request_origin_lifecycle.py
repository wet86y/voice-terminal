"""Request origin（请求来源）只由活动模型调用、输出租约和恢复队列保留。"""
import asyncio
from types import SimpleNamespace

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def test_completed_control_only_requests_do_not_accumulate_origin_records():
    async def scenario():
        async def emit(*args, **kwargs):
            pass
        output = SimpleNamespace(status="idle", active=False, busy=False,
                                 retains_request=lambda identity: False)
        runtime = AgentEpisodeRuntime(None, SimpleNamespace(), output, emit,
                                      conversation=ConversationRuntime(), ledger=SessionLedger())
        runtime.episode_id = runtime.conversation.conversation_id
        for index in range(200):
            identity = (runtime.episode_id, f"request-{index}")
            runtime.agent_step_id = identity[1]
            runtime._identity_turns[identity] = f"turn-{index}"
            runtime.active_request_identity = identity
            runtime.active_request_source_turn_id = f"turn-{index}"
            request = asyncio.get_running_loop().create_future()
            request.set_result("completed")
            runtime.active_request = request
            await runtime._finish_active_request(request)
            assert not runtime._identity_turns
    asyncio.run(scenario())
