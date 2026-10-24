"""严格声纹准入：真实匹配及聚合，隔离外部音频、通信和持久结算。"""
from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.turn_workspace import TurnWorkspace
from voice_terminal.core.state import RuntimeState
from voice_terminal.speaker.contracts import SpeakerObservation, SpeakerState
from voice_terminal.speaker.router import SpeakerRouter
from voice_terminal.config import SpeakerConfig
from voice_terminal.speaker.session import SessionSpeakerPolicy
from voice_terminal.speaker.rolling import RollingSpeakerIdentity, RollingSpeakerSnapshot


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
    controller.speaker_config = SpeakerConfig()
    controller.speaker_policy = SessionSpeakerPolicy(controller.speaker_config, lambda _: score)
    controller._speaker_published = {}
    controller._speaker_pending_audio = {}
    controller._settle_committed_turn = AsyncMock()
    if score is not None:
        for i in range(3 if score < 0.35 else 1):
            controller.speaker_policy.observe((score, math.sqrt(1-score*score)),
                turn_id=ws.turn_id,generation=ws.generation,start=0,end=6400*(i+1),speech_ms=400)
    controller.speaker_policy.ingest(turn_id=ws.turn_id,generation=ws.generation,revision=1,
                                    start=0,end=6400,text=TEXT,bypass=mode != "filter")

    controller.speaker_mode = mode
    controller.speaker_router = router
    controller.speaker_client = SimpleNamespace(
        connected=connected, degraded=False, finish_turn=AsyncMock(return_value=finished),
    )
    controller._speaker_snapshot = None
    controller._speaker_turn_start_sample = 0
    controller.state = RuntimeState()
    controller.episode = SimpleNamespace(
        observe=AsyncMock(), discard_speaker_vetoed_input=Mock(), episode_id="conversation",
    )
    controller.episode_history = {}
    controller._publish = AsyncMock()
    controller._cancel_tts_prewarm = AsyncMock()
    controller._retire_committed_input = AsyncMock()
    controller._commit_input_workspace = AsyncMock()
    controller._state_event = AsyncMock()
    return controller, ws


@pytest.mark.parametrize("score,allowed", [(0.8,True),(0.55,False),(0.2,False),(None,False)])
def test_partial_dispatches_only_after_streaming_admission(score,allowed):
    async def run():
        c,ws=make_controller(score)
        await c._detail("transcript.segment",changed_ids=["sentence"])
        assert bool(c.episode.observe.await_count)==allowed
        if not allowed: assert c.speaker_policy.pieces
    asyncio.run(run())


@pytest.mark.parametrize("finished,connected", [(True,True),(False,True),(True,False)])
def test_final_timeout_does_not_revoke_already_admitted_text(finished,connected):
    async def run():
        c,ws=make_controller(0.8,finished=finished,connected=connected)
        await c._input_committed(ws,ws.token)
        assert ws.final_text==TEXT and not ws.speaker_blocked
        c.episode.discard_speaker_vetoed_input.assert_not_called()
        c.speaker_client.finish_turn.assert_not_awaited()
        finalized=[call for call in c.episode.observe.await_args_list if call.args[0]=="USER_INPUT_FINALIZED"]
        assert finalized[0].kwargs["text"]==TEXT
    asyncio.run(run())


def test_unknown_final_stays_pending_without_entering_context():
    async def run():
        c,ws=make_controller(None)
        await c._input_committed(ws,ws.token)
        assert ws.final_text=="" and c.speaker_policy.pieces[0].status=="pending"
        c.episode.observe.assert_not_awaited()
        c.episode.discard_speaker_vetoed_input.assert_not_called()
    asyncio.run(run())


def test_refinement_cannot_bypass_unadmitted_input():
    async def run():
        c,ws=make_controller(None)
        await c._refinement_completed(ws,{"text":"尚未放行的精修"})
        c.episode.observe.assert_not_awaited()
    asyncio.run(run())


def test_blocked_refinement_stays_out_of_conversation_without_environment_store():
    async def run():
        c, ws = make_controller(.2)
        ws.speaker_blocked = True
        ws.speaker_summary = {"admitted": False}
        await c._refinement_completed(ws, {"text": "被拦截轮次的迟到精修"})
        assert ws.raw_refined_text == "被拦截轮次的迟到精修"
        assert not ws.refinement_available
        assert not ws.transcript_updates
        c._publish.assert_not_awaited()
        c.episode.observe.assert_not_awaited()
    asyncio.run(run())


def test_owner_continuity_has_same_admission_as_owner():
    async def run():
        c,ws=make_controller(None)
        import time
        c.speaker_policy.last_owner_at=time.monotonic()
        c.speaker_policy.scorer=lambda _:0.55
        c.speaker_policy.observe((1.,0.),turn_id=ws.turn_id,generation=3,start=0,end=6400,speech_ms=400)
        assert c.speaker_policy.state(ws.turn_id,3)==SpeakerState.OWNER_CONTINUITY
        await c._input_committed(ws,ws.token)
        assert ws.final_text==TEXT
    asyncio.run(run())


def test_observe_mode_keeps_unknown_text_available():
    async def run():
        c,ws=make_controller(None,mode="observe")
        await c._input_committed(ws,ws.token)
        assert ws.final_text==TEXT
    asyncio.run(run())


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


@pytest.mark.parametrize("score,expected",[(None,""),(.8,TEXT)])
def test_cancel_context_helper_reads_only_admitted_text(score,expected):
    async def run():
        c,ws=make_controller(score)
        ws.episode_id=None
        ws.raw_partial="原始未放行正文"
        c.episode.ensure_episode=AsyncMock()
        await c._ensure_workspace_episode(ws)
        if expected:
            assert c.episode.ensure_episode.await_args.kwargs['text']==expected
        else:
            c.episode.ensure_episode.assert_not_awaited()
    asyncio.run(run())


def test_late_pending_embedding_is_consumed_next_tick_without_reinference():
    from unittest.mock import patch
    async def run():
        c,ws=make_controller(None)
        c.speaker_policy.scorer=lambda _: .9
        clock=[1.0]
        async def embed(_):
            clock[0]+=.2
            return (1.,0.)
        c.speaker_client.embed_pcm=AsyncMock(side_effect=embed)
        c._speaker_pending_audio[(ws.turn_id,ws.generation)]=(b'\x00\x10'*6400,0,6400,1.,'')
        c._detail=AsyncMock()
        with patch('voice_terminal.controller.episode_controller.time.monotonic',side_effect=lambda:clock[0]):
            await c._analyze_pending_speaker_audio(1)
            assert c.speaker_policy.admitted_text(ws.turn_id)==''
            await c._analyze_pending_speaker_audio(2)
            assert c.speaker_policy.admitted_text(ws.turn_id)==TEXT
            c.speaker_client.embed_pcm.assert_awaited_once()
    asyncio.run(run())
