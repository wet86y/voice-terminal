"""严格声纹准入：真实匹配及聚合，隔离外部音频、通信和持久结算。"""
from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.turn_workspace import TurnWorkspace
from voice_terminal.core.state import RuntimeState
from voice_terminal.session.ambient import AmbientTranscriptStore
from voice_terminal.speaker.contracts import SpeakerObservation, SpeakerState
from voice_terminal.speaker.router import SpeakerRouter


TEXT = "这段输入只有身份确认后才能提交。"  # TEXT（测试转写正文）。


def make_controller(score, *, mode="filter", finished=True, connected=True):
    """score（确定性声纹向量相似度），不将模型替身当作真人识别验收。"""
    profile = SimpleNamespace(available=True, vectors=lambda **_: [("centroid", (1.0, 0.0))])
    router = SpeakerRouter(profile, min_turn_speech_ms=2000)
    ws = TurnWorkspace("input", generation=3, speaker_mode=mode)
    router.reset(turn_id=ws.turn_id, generation=ws.generation)
    if score is not None:
        router.observe(SpeakerObservation(
            turn_id=ws.turn_id, generation=ws.generation,
            window_started_sample=0, window_ended_sample=32000,
            speech_ms=2000, turn_speech_ms=2000, quality="ready",
            embedding=(score, math.sqrt(1 - score * score)),
        ))
    ws.segments = [{"text": TEXT}]
    ws.final_text = ws.paraformer_final = TEXT
    controller = object.__new__(EpisodeController)
    controller.workspace = ws
    controller.speaker_mode = mode
    controller.speaker_router = router
    controller.speaker_client = SimpleNamespace(
        connected=connected, degraded=False, finish_turn=AsyncMock(return_value=finished),
    )
    controller._speaker_snapshot = None
    controller._speaker_turn_start_sample = 0
    controller._speaker_vetoed_turns = set()
    controller.state = RuntimeState()
    controller.episode = SimpleNamespace(
        observe=AsyncMock(), discard_speaker_vetoed_input=Mock(), episode_id="conversation",
    )
    controller.episode_history = {}
    controller.ambient_store = AmbientTranscriptStore()
    controller._publish = AsyncMock()
    controller._cancel_tts_prewarm = AsyncMock()
    controller._retire_committed_input = AsyncMock()
    controller._commit_input_workspace = AsyncMock()
    controller._state_event = AsyncMock()
    return controller, ws


@pytest.mark.parametrize("score", [0.8, 0.55, 0.2, None])
def test_filter_never_dispatches_provisional_text_before_final_identity(score):
    async def scenario():
        controller, _ws = make_controller(score)
        await controller._detail("transcript.segment", changed_ids=["sentence"])
        controller.episode.observe.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("score,finished,connected,allowed", [
    (0.8, True, True, True),
    (0.55, True, True, False),
    (0.2, True, True, False),
    (None, True, True, False),
    (0.8, False, True, False),
    (0.2, False, True, False),
    (0.8, True, False, False),
])
def test_filter_commits_only_confirmed_owner_and_keeps_other_text_ambient(score, finished, connected, allowed):
    async def scenario():
        controller, ws = make_controller(score, finished=finished, connected=connected)
        await controller._input_committed(ws, ws.token)
        assert ws.speaker_routing_finalized
        if allowed:
            controller.episode.observe.assert_awaited_once()
            assert controller.episode.observe.await_args.args[0] == "USER_INPUT_FINALIZED"
            assert not controller.ambient_store.records
            assert not ws.speaker_blocked
        else:
            controller.episode.observe.assert_not_awaited()
            assert controller.ambient_store.records[0]["text"] == TEXT
            assert controller.ambient_store.records[0]["route"] == "ambient"
            assert ws.speaker_blocked
            controller._cancel_tts_prewarm.assert_awaited_once_with(ws.turn_id)
    asyncio.run(scenario())


def test_confirmed_non_owner_survives_final_acknowledgement_timeout():
    async def scenario():
        controller, ws = make_controller(0.2, finished=False)
        decision = await controller._speaker_decision(ws)
        assert decision.state == SpeakerState.NON_OWNER_CONFIRMED
        assert decision.route == "ambient"
        assert decision.reason == "speaker_timeout"
    asyncio.run(scenario())


def test_filter_without_router_cannot_bypass_identity_gate():
    async def scenario():
        controller, ws = make_controller(None)
        controller.speaker_router = None
        await controller._input_committed(ws, ws.token)
        controller.episode.observe.assert_not_awaited()
        assert controller.ambient_store.records[0]["speaker_state"] == "unavailable"
    asyncio.run(scenario())


def test_observe_mode_still_allows_unknown_and_provisional_text():
    async def scenario():
        controller, ws = make_controller(None, mode="observe")
        await controller._detail("transcript.segment")
        await controller._input_committed(ws, ws.token)
        assert controller.episode.observe.await_count == 2
        assert not controller.ambient_store.records
    asyncio.run(scenario())


def test_late_final_identity_cannot_commit_a_resumed_input_generation():
    async def scenario():
        controller, ws = make_controller(0.8)
        token = ws.token
        started, release = asyncio.Event(), asyncio.Event()
        async def finish(*_args, **_kwargs):
            started.set()
            await release.wait()
            return True
        controller.speaker_client.finish_turn = finish
        task = asyncio.create_task(controller._input_committed(ws, token))
        await started.wait()
        ws.resume()
        controller.speaker_router.reset(turn_id=ws.turn_id, generation=ws.generation)
        release.set()
        await task
        controller.episode.observe.assert_not_awaited()
        controller._retire_committed_input.assert_not_awaited()
        assert not ws.speaker_routing_finalized and not ws.speaker_decision
    asyncio.run(scenario())


def test_pending_identity_keeps_agent_input_without_dispatch_or_veto():
    async def scenario():
        agent = object.__new__(AgentEpisodeRuntime)
        agent.scheduler = SimpleNamespace(
            window_index=0, final_pending=False, interval=0.4,
            heartbeat=lambda _at: None, secondary_due=lambda _previous: False,
            normal_due=lambda _meaningful, *, at_heartbeat: at_heartbeat,
        )
        agent._immediate_system_check = None
        agent.active_request = None
        agent.pending = {"USER_SEGMENT_UPDATED"}
        agent.inputs = {"input": {"text": TEXT}}
        agent.last_turn_id = "input"
        agent._pending_transcript_turn_id = None
        agent._pending_since = 1.0
        agent.admit_user_turn = lambda _turn: True
        agent.defer_user_turn = lambda _turn: True
        agent._meaningful_pending = lambda: set(agent.pending)
        agent._dispatch_allowed = lambda: True
        agent.log = AsyncMock()
        agent._start_dispatch = AsyncMock(return_value=True)
        await agent._on_heartbeat(10.0)
        assert agent.inputs["input"]["text"] == TEXT
        assert agent.pending == {"USER_SEGMENT_UPDATED"}
        agent._start_dispatch.assert_not_awaited()
    asyncio.run(scenario())
