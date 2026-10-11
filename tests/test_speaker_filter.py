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
from voice_terminal.session.ambient import AmbientTranscriptStore
from voice_terminal.speaker.contracts import SpeakerObservation, SpeakerState
from voice_terminal.speaker.router import SpeakerRouter
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


def test_recent_owner_continuity_uses_the_relaxed_threshold_for_filtering():
    async def scenario():
        controller, ws = make_controller(0.55)
        controller.speaker_router.machine._last_owner_confirmed_at = time.monotonic()
        decision = await controller._speaker_decision(ws)
        assert decision.state == SpeakerState.OWNER_CONTINUITY
        assert decision.route == "main"
    asyncio.run(scenario())


def _observe_embedding(router, ws, *, score, start, turn_speech_ms):
    router.observe(SpeakerObservation(
        turn_id=ws.turn_id, generation=ws.generation,
        window_started_sample=start, window_ended_sample=start + 32000,
        speech_ms=2000, turn_speech_ms=turn_speech_ms, quality="ready",
        embedding=(score, math.sqrt(1 - score * score)),
    ))


def _attach_agent_runtime(controller, ws):
    """创建真实 AgentEpisodeRuntime，仅替身外部调度和模型派发边界。"""
    agent = AgentEpisodeRuntime(
        None, None, None, None,
        conversation=SimpleNamespace(), ledger=SimpleNamespace(),
    )
    agent.episode_id = "conversation"
    agent.last_turn_id = ws.turn_id
    agent._pending_transcript_turn_id = None
    agent.inputs = {ws.turn_id: {"text": TEXT, "final": False}}
    agent.pending = {"USER_SEGMENT_UPDATED"}
    agent._pending_since = 1.0
    agent.scheduler = SimpleNamespace(
        window_index=0, final_pending=False, interval=0.4, generation=0,
        anchor_turn_id=None, anchor_wall_time=1.0,
        NON_SEMANTIC_EVENTS=set(),
        heartbeat=lambda _at: None,
        secondary_due=lambda _previous: False,
        normal_due=lambda _meaningful, *, at_heartbeat: at_heartbeat,
        pending_checkpoint_windows=lambda: [],
        accepts=lambda _kind: True,
    )

    def arm(*, now=None, wall_time=None, turn_id=None, anchor_source=None):
        agent.scheduler.generation += 1
        agent.scheduler.anchor_turn_id = turn_id
        agent.scheduler.anchor_wall_time = wall_time or 1.0
        return agent.scheduler.generation

    agent.scheduler.arm = arm
    agent._supersede_recovery_without_request_cancel = Mock()
    agent._supersede_system_output_without_request_cancel = Mock()
    agent.log = AsyncMock()

    def wake(kind, *, turn_id=None):
        agent.pending.add(kind)
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"} and turn_id:
            agent._pending_transcript_turn_id = turn_id

    agent._wake = wake
    # The actual heartbeat and speaker admission callbacks run; only provider
    # dispatch is stubbed so this regression never contacts an external model.
    agent._dispatch_allowed = lambda **_kwargs: True
    agent._start_dispatch = AsyncMock(return_value=True)
    agent.admit_user_turn = controller._speaker_admit_turn
    agent.defer_user_turn = controller._speaker_defer_turn
    controller.episode = agent
    return agent


def test_early_non_owner_snapshot_does_not_override_final_owner_aggregate():
    async def scenario():
        controller, ws = make_controller(0.2)
        assert controller.speaker_router.machine.state == SpeakerState.NON_OWNER_CONFIRMED
        assert not controller._speaker_admit_turn(ws.turn_id)

        # Three current-generation owner windows move the real router aggregate
        # above the owner threshold even though its first window was non-owner.
        for index, turn_speech_ms in enumerate((4000, 6000, 8000), start=1):
            _observe_embedding(
                controller.speaker_router, ws, score=0.9,
                start=index * 8000, turn_speech_ms=turn_speech_ms,
            )
        assert controller.speaker_router.machine.state == SpeakerState.OWNER_CONFIRMED

        # A still-fresh rolling snapshot is useful as provisional evidence, but
        # the final current-generation aggregate remains the commit authority.
        now = time.monotonic()
        controller._speaker_snapshot = RollingSpeakerSnapshot(
            analysis_start_sample=0, analysis_end_sample=32000,
            candidate_start_sample=controller._speaker_turn_start_sample,
            candidate_end_sample=32000, analyzed_at=now, completed_at=now,
            quality="ready", identity=RollingSpeakerIdentity.NON_OWNER,
            score=0.2, reason="non_owner_confirmed",
        )
        controller._speaker_snapshot_turn_id = ws.turn_id
        controller._speaker_snapshot_generation = ws.generation
        assert controller.speaker_vetoes_turn(ws.turn_id)

        await controller._input_committed(ws, ws.token)

        assert ws.speaker_routing_finalized
        assert ws.speaker_decision["state"] == "owner_confirmed"
        assert ws.speaker_decision["route"] == "main"
        assert not ws.speaker_blocked
        assert not controller.ambient_store.records
        controller.episode.observe.assert_awaited_once()
        assert controller._speaker_admit_turn(ws.turn_id)

    asyncio.run(scenario())


def test_previous_generation_speaker_evidence_cannot_veto_current_owner():
    async def scenario():
        controller, ws = make_controller(0.2)
        old_generation = ws.generation
        assert not controller._speaker_admit_turn(ws.turn_id)

        ws.resume()
        ws.final_text = ws.paraformer_final = TEXT
        controller.speaker_router.reset(turn_id=ws.turn_id, generation=ws.generation)
        # This is the only evidence accepted by the new generation's router.
        for index, turn_speech_ms in enumerate((2000, 4000, 6000), start=1):
            _observe_embedding(
                controller.speaker_router, ws, score=0.9,
                start=index * 8000, turn_speech_ms=turn_speech_ms,
            )

        now = time.monotonic()
        controller._speaker_snapshot = RollingSpeakerSnapshot(
            analysis_start_sample=0, analysis_end_sample=32000,
            candidate_start_sample=controller._speaker_turn_start_sample,
            candidate_end_sample=32000, analyzed_at=now, completed_at=now,
            quality="ready", identity=RollingSpeakerIdentity.NON_OWNER,
            score=0.2, reason="non_owner_confirmed",
        )
        controller._speaker_snapshot_turn_id = ws.turn_id
        controller._speaker_snapshot_generation = old_generation

        assert not controller.speaker_vetoes_turn(ws.turn_id)
        assert controller._speaker_admit_turn(ws.turn_id)
        await controller._input_committed(ws, ws.token)

        assert ws.speaker_routing_finalized
        assert ws.generation == old_generation + 1
        assert ws.speaker_decision["generation"] == ws.generation
        assert ws.speaker_decision["state"] == "owner_confirmed"
        assert not ws.speaker_blocked
        controller.episode.observe.assert_awaited_once()

    asyncio.run(scenario())


def test_early_non_owner_heartbeat_defers_until_final_owner_then_dispatches_once():
    async def scenario():
        controller, ws = make_controller(0.2)
        agent = _attach_agent_runtime(controller, ws)
        now = time.monotonic()
        controller._speaker_snapshot = RollingSpeakerSnapshot(
            analysis_start_sample=0, analysis_end_sample=32000,
            candidate_start_sample=controller._speaker_turn_start_sample,
            candidate_end_sample=32000, analyzed_at=now, completed_at=now,
            quality="ready", identity=RollingSpeakerIdentity.NON_OWNER,
            score=0.2, reason="non_owner_confirmed",
        )
        controller._speaker_snapshot_turn_id = ws.turn_id
        controller._speaker_snapshot_generation = ws.generation
        assert controller.speaker_vetoes_turn(ws.turn_id)

        # A pre-final temporary veto must retain the pending text for review.
        await agent._on_heartbeat(10.0)
        assert agent.inputs[ws.turn_id]["text"] == TEXT
        assert agent.pending == {"USER_SEGMENT_UPDATED"}
        agent._start_dispatch.assert_not_awaited()

        # The real router aggregates later current-generation owner windows.
        for index, turn_speech_ms in enumerate((4000, 6000, 8000), start=1):
            _observe_embedding(
                controller.speaker_router, ws, score=0.9,
                start=index * 8000, turn_speech_ms=turn_speech_ms,
            )
        assert controller.speaker_router.machine.state == SpeakerState.OWNER_CONFIRMED
        await controller._input_committed(ws, ws.token)

        assert ws.speaker_routing_finalized
        assert ws.speaker_decision["state"] == "owner_confirmed"
        assert agent.inputs[ws.turn_id]["final"]
        await agent._on_heartbeat(11.0)
        assert agent._start_dispatch.await_count == 1
        dispatched_kinds = agent._start_dispatch.await_args.args[0]
        assert "USER_INPUT_FINALIZED" in dispatched_kinds

        # Cleared pending events cannot cause a duplicate dispatch at the next heartbeat.
        await agent._on_heartbeat(12.0)
        assert agent._start_dispatch.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("score,finished", [
    (0.2, True),   # 最终非主人。
    (None, True),  # 最终身份未知。
    (0.8, False),  # 最终回执超时，即使分数像主人也降级拒绝。
])
def test_final_filter_veto_removes_pending_agent_input(score, finished):
    async def scenario():
        controller, ws = make_controller(score, finished=finished)
        agent = _attach_agent_runtime(controller, ws)
        await controller._input_committed(ws, ws.token)

        assert ws.speaker_routing_finalized
        assert ws.speaker_blocked
        assert ws.turn_id not in agent.inputs
        assert not agent.pending.intersection({
            "USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED", "USER_INPUT_REFINED",
        })

    asyncio.run(scenario())


def test_filter_without_router_cannot_bypass_identity_gate():
    async def scenario():
        controller, ws = make_controller(None)
        controller.speaker_router = None
        await controller._input_committed(ws, ws.token)
        controller.episode.observe.assert_not_awaited()
        assert controller.ambient_store.records[0]["speaker_state"] == "non_owner_confirmed"
        assert controller.ambient_store.records[0]["reason"] == "speaker_router_unavailable"
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
