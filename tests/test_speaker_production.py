"""Production speaker routing, timing, and barge-in safety regressions."""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.controller.turn_controller import InputController
from voice_terminal.speaker.rolling import RollingSpeakerIdentity, RollingSpeakerSnapshot
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.core.state import RuntimeState
from voice_terminal.speaker.client import SpeakerWorkerError
from voice_terminal.server.websocket import SessionRuntime


def _snapshot(identity, *, start=0, end=32_000, completed_at=None):
    return RollingSpeakerSnapshot(
        analysis_start_sample=max(start, end - 32_000),
        analysis_end_sample=end,
        candidate_start_sample=start,
        candidate_end_sample=end,
        analyzed_at=time.monotonic(),
        completed_at=time.monotonic() if completed_at is None else completed_at,
        quality="ready",
        identity=identity,
        score=0.8 if identity == RollingSpeakerIdentity.OWNER else 0.2,
        reason="confirmed",
    )


def test_six_switch_combinations_keep_barge_independent():
    async def embed(_pcm):
        return (1.0,)

    client = SimpleNamespace(embed_pcm=embed)
    router = SimpleNamespace(
        score_embedding=lambda _value: 0.8,
        state_config=SimpleNamespace(confirm_threshold=0.6, continuity_threshold=0.5),
    )
    for mode in ("off", "observe", "filter"):
        for owner_only in (False, True):
            controller = object.__new__(RealtimeController)
            controller.speaker_mode = mode
            controller.owner_only_barge_in = owner_only
            controller.speaker_client = client
            controller.speaker_router = router
            controller.speaker_window_ms = 2000
            controller.speaker_min_speech_ms = 700
            controller.speaker_min_turn_speech_ms = 2000
            controller.speaker_min_window_speech_ratio = 0.4
            controller.speaker_sensor = None
            controller._speaker_snapshot = None
            controller.configure_speaker_runtime()
            assert (controller.speaker_sensor is not None) == (mode != "off" or owner_only)


def test_hot_threshold_change_invalidates_prior_owner_proof():
    async def embed(_pcm):
        return (1.0,)

    controller = object.__new__(RealtimeController)
    controller.speaker_mode = "off"
    controller.owner_only_barge_in = True
    controller.speaker_client = SimpleNamespace(embed_pcm=embed)
    thresholds = SimpleNamespace(confirm_threshold=0.6, continuity_threshold=0.5)
    controller.speaker_router = SimpleNamespace(
        score_embedding=lambda _value: 0.8, state_config=thresholds,
    )
    controller.speaker_window_ms = 2000
    controller.speaker_min_speech_ms = 700
    controller.speaker_min_turn_speech_ms = 2000
    controller.speaker_min_window_speech_ratio = 0.4
    controller.speaker_sensor = None
    controller._speaker_snapshot = None
    controller.configure_speaker_runtime()
    sensor = controller.speaker_sensor
    controller._speaker_snapshot = _snapshot(RollingSpeakerIdentity.OWNER)
    controller._speaker_snapshot_turn_id = "old_turn"
    controller._speaker_snapshot_generation = 2
    controller._speaker_snapshot_barge_id = "old_candidate"
    thresholds.confirm_threshold = 0.85
    controller.configure_speaker_runtime()
    assert controller.speaker_sensor is sensor
    assert controller.speaker_sensor.confirm_threshold == 0.85
    assert controller._speaker_snapshot is None
    assert controller._speaker_snapshot_turn_id is None
    assert controller._speaker_snapshot_generation is None
    assert controller._speaker_snapshot_barge_id is None


def test_off_plus_owner_only_preserves_selection_but_hides_marks_without_profile():
    async def scenario():
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = SessionRuntime(AppConfig(
                model_dir=root / "models",
                persona=PersonaConfig(directory=root / "persona"),
            ), runtime_data_dir=root / ".runtime")
            applied = await runtime.set_speaker_settings({
                "mode": "off", "owner_only_barge_in": True,
                "confirm_threshold": 0.6, "continuity_threshold": 0.5,
            })
            assert applied["owner_only_barge_in_selected"] is True
            assert applied["owner_only_barge_in_effective"] is False
            assert runtime.speaker_status()["rolling_state"]["identity"] == "unknown"
    asyncio.run(scenario())


def test_owner_barge_status_separates_selected_from_current_aec_availability():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        runtime = SessionRuntime(AppConfig(
            model_dir=root / "models",
            persona=PersonaConfig(directory=root / "persona"),
        ), runtime_data_dir=root / ".runtime")
        runtime.config = replace(
            runtime.config,
            speaker=replace(runtime.config.speaker, owner_only_barge_in=True),
        )
        runtime.speaker_profile = SimpleNamespace(snapshot=lambda: {"available": True})
        runtime.speaker = SimpleNamespace(connected=True, last_error=None)
        runtime.state.set_service("speaker", "READY")
        runtime.audio_processor = SimpleNamespace(enabled=True)
        output = SimpleNamespace(active=False)
        runtime.controller = SimpleNamespace(
            speaker_sensor=object(), output=output,
            _barge_in_protected_available=lambda: False,
            _speaker_snapshot=None,
        )
        status = runtime.speaker_status()
        assert status["owner_only_barge_in_selected"] is True
        assert status["owner_only_barge_in_effective"] is False
        assert "等待播放" in status["owner_only_barge_in_reason"]
        output.active = True
        status = runtime.speaker_status()
        assert status["owner_only_barge_in_effective"] is False
        assert "回声保护不可用" in status["owner_only_barge_in_reason"]
        runtime.state.set_service("speaker", "DEGRADED")
        status = runtime.speaker_status()
        assert status["owner_only_barge_in_selected"] is True
        assert status["owner_only_barge_in_effective"] is False
        assert "工作进程" in status["owner_only_barge_in_reason"]


def test_worker_reconnect_rebinds_live_sensor_and_invalidates_old_owner_proof():
    """从本地组件重试入口重连时，实时采样器也必须使用新客户端。"""
    async def scenario():
        old_client = SimpleNamespace(embed_pcm=AsyncMock(return_value=(1.0,)))
        new_client = SimpleNamespace(
            embed_pcm=AsyncMock(return_value=(0.0, 1.0)), connect=AsyncMock(),
            health=AsyncMock(), fingerprint=AsyncMock(return_value={}),
        )
        controller = object.__new__(RealtimeController)
        controller.speaker_mode = "observe"
        controller.owner_only_barge_in = False
        controller.speaker_client = old_client
        controller.speaker_router = SimpleNamespace(
            score_embedding=lambda _embedding: 0.8,
            state_config=SimpleNamespace(confirm_threshold=0.6, continuity_threshold=0.5),
        )
        controller.speaker_window_ms = 2000
        controller.speaker_min_speech_ms = 700
        controller.speaker_min_turn_speech_ms = 2000
        controller.speaker_min_window_speech_ratio = 0.4
        controller.speaker_sensor = None
        controller.configure_speaker_runtime()
        old_sensor = controller.speaker_sensor
        controller._speaker_snapshot = _snapshot(RollingSpeakerIdentity.OWNER)
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.config = AppConfig()
        runtime.state = RuntimeState()
        runtime.controller = controller
        runtime.speaker_profile = SimpleNamespace(stale=False, set_runtime_fingerprint=Mock())
        runtime.startup_errors = ["speaker: 连接已断开"]
        with patch("voice_terminal.server.speaker_runtime.SpeakerWorkerClient", return_value=new_client):
            ready, error = await runtime._connect_speaker_worker()
        assert ready and error is None
        assert controller.speaker_client is new_client
        assert controller.speaker_sensor is not old_sensor
        assert await controller.speaker_sensor.embed_pcm(b"pcm") == (0.0, 1.0)
        old_client.embed_pcm.assert_not_awaited()
        assert controller._speaker_snapshot is None
        assert not runtime.startup_errors
    asyncio.run(scenario())


def test_worker_retry_validates_cached_readiness_before_accepting_it():
    async def scenario():
        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime._speaker_worker_lock = asyncio.Lock()
        runtime._speaker_worker_process = SimpleNamespace(returncode=None)
        runtime.state = RuntimeState()
        runtime.state.set_service("speaker", "READY")
        stale = SimpleNamespace(connected=True, degraded=False, close=AsyncMock(),
            health=AsyncMock(side_effect=SpeakerWorkerError("声纹健康检查超时")))
        runtime.speaker = stale
        runtime._connect_speaker_worker = AsyncMock(return_value=(True, None))
        assert await runtime.ensure_speaker_worker()
        stale.health.assert_awaited_once()
        stale.close.assert_awaited_once()
        runtime._connect_speaker_worker.assert_awaited_once()
    asyncio.run(scenario())


def test_request_veto_requires_matching_fresh_candidate_and_filter_mode():
    controller = object.__new__(RealtimeController)
    controller.speaker_mode = "filter"
    controller.workspace = SimpleNamespace(turn_id="t", generation=3)
    controller._speaker_turn_start_sample = 100
    controller._speaker_snapshot = _snapshot(RollingSpeakerIdentity.NON_OWNER, start=100, end=32_100)
    controller._speaker_snapshot_turn_id = "t"
    controller._speaker_snapshot_generation = 3
    assert controller.speaker_vetoes_turn("t")
    controller.speaker_mode = "observe"
    assert not controller.speaker_vetoes_turn("t")
    controller.speaker_mode = "filter"
    controller._speaker_turn_start_sample = 101
    assert not controller.speaker_vetoes_turn("t")
    controller._speaker_turn_start_sample = 100
    controller._speaker_snapshot = _snapshot(
        RollingSpeakerIdentity.NON_OWNER, start=100, end=32_100,
        completed_at=time.monotonic() - 2,
    )
    assert not controller.speaker_vetoes_turn("t")


def test_heartbeat_schedules_one_shot_at_plus_300ms():
    calls = []

    class FakeHandle:
        def cancel(self):
            pass

        def cancelled(self):
            return False

    loop = SimpleNamespace(
        time=lambda: 10.0,
        call_at=lambda when, callback, *args: (calls.append((when, callback, args)), FakeHandle())[1],
    )
    controller = object.__new__(EpisodeController)
    controller.speaker_sensor = object()
    controller.episode = SimpleNamespace(scheduler=SimpleNamespace(interval=0.4))
    controller._speaker_timer_handle = None
    with patch("voice_terminal.controller.episode_controller.asyncio.get_running_loop", return_value=loop):
        controller._speaker_heartbeat_pulse(SimpleNamespace(scheduled_at=10.0, episode_generation=1, sequence=7))
    assert len(calls) == 1
    assert abs(calls[0][0] - 10.3) < 1e-9
    assert calls[0][2] == (1, 7)


def test_inflight_speaker_result_does_not_mark_replaced_input_turn():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def analyze(**_kwargs):
            entered.set()
            await release.wait()
            return _snapshot(RollingSpeakerIdentity.NON_OWNER)

        controller = object.__new__(EpisodeController)
        controller.speaker_sensor = SimpleNamespace(latest_sample=32_000, analyze=analyze)
        controller.workspace = SimpleNamespace(turn_id="old", generation=1)
        controller._speaker_turn_start_sample = 0
        controller._owner_barge_start_sample = None
        controller._owner_barge_candidate_id = None
        controller._speaker_snapshot = None
        controller.speaker_mode = "off"
        task = asyncio.create_task(controller._analyze_speaker_snapshot(1))
        await entered.wait()
        controller.workspace = SimpleNamespace(turn_id="new", generation=1)
        release.set()
        await task
        assert controller._speaker_snapshot is None

    asyncio.run(scenario())


def test_agent_veto_consumes_pending_user_input_before_dispatch():
    async def scenario():
        agent = object.__new__(AgentEpisodeRuntime)
        agent.scheduler = SimpleNamespace(
            window_index=0, final_pending=False, interval=0.4,
            heartbeat=lambda _at: None,
            secondary_due=lambda _previous_window: False,
            normal_due=lambda _meaningful, *, at_heartbeat: at_heartbeat,
        )
        agent._immediate_system_check = None
        agent.active_request = None
        agent.pending = {"USER_SEGMENT_UPDATED"}
        agent.inputs = {"t": {"text": "non-owner partial"}}
        agent.last_turn_id = "t"
        agent._pending_transcript_turn_id = None
        agent._pending_since = 1.0
        agent.admit_user_turn = lambda _turn: False
        agent._meaningful_pending = lambda: set(agent.pending)
        agent._dispatch_allowed = lambda: True
        async def log(_kind, **_kwargs):
            return None
        agent.log = log
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("speaker-vetoed text was dispatched")
        agent._start_dispatch = forbidden
        await agent._on_heartbeat(10.0)
        assert not agent.pending
        assert "t" not in agent.inputs
    asyncio.run(scenario())


def test_protected_gate_uses_output_generation_after_input_retirement():
    class Player:
        running = True

        def has_started(self, key):
            assert key == "input_1:4"
            return True

    controller = object.__new__(InputController)
    controller.player = Player()
    controller.workspace = None
    controller.output = SimpleNamespace(
        handle=SimpleNamespace(workspace=SimpleNamespace(playback_key="input_1:4", playback_started=True)),
    )
    controller.audio_processor = SimpleNamespace(enabled=True, protected_interruption_available=False)
    assert not controller._barge_in_protected_available()


def test_owner_barge_requires_current_candidate_owner_before_stop():
    async def scenario():
        calls = []
        controller = object.__new__(EpisodeController)
        controller._owner_barge_candidate_id = None
        controller._owner_barge_start_sample = None
        controller._owner_barge_started_at = None
        controller._owner_barge_output_key = None
        controller._owner_barge_expired = False
        controller._speaker_snapshot = None
        controller._speaker_snapshot_barge_id = None
        controller._speech_start_sample = 0
        controller._recent = deque([AudioFrame(b"\x01\x00" * 16_000, 0, 1.0),
                                    AudioFrame(b"\x01\x00" * 16_000, 16_000, 2.0)])
        controller.barge_in_gate = SimpleNamespace(candidate_started_at=0.1, reset=lambda: calls.append("reset"))
        controller.turns = SimpleNamespace(invalidate=lambda: calls.append("invalidate"))
        controller.player = object()
        controller._barge_in_protected_available = lambda: True
        controller._stop_playback_snapshot = lambda _key: (calls.append("stop"), object())[1]
        async def state_event(*_args, **_kwargs):
            calls.append("state")
        async def speech_activity(*_args, **_kwargs):
            calls.append("speech")
        async def cancel_turn(*_args, **_kwargs):
            calls.append("cancel")
        async def new_turn(*_args, **_kwargs):
            calls.append("new_turn")
        async def log(*_args, **_kwargs):
            return None
        controller._state_event = state_event
        controller._cancel_turn = cancel_turn
        controller._new_turn = new_turn
        controller.episode = SimpleNamespace(speech_activity=speech_activity, log=log)
        handle = SimpleNamespace(
            workspace=SimpleNamespace(playback_key="input_1:4"), speech_id="speech_1", turn_id="input_1",
        )
        activity = SimpleNamespace(is_speech=True)
        decision = SimpleNamespace(candidate=True, confirmed=True, duration_ms=2000)
        progress = {"output_estimated_ms": 4000}
        await controller._observe_owner_barge(
            activity, SimpleNamespace(start_sample=0, end_sample=16_000, captured_at=1.0),
            handle, progress, decision,
        )
        assert "stop" not in calls
        controller._speaker_snapshot = _snapshot(RollingSpeakerIdentity.NON_OWNER)
        controller._speaker_snapshot_barge_id = controller._owner_barge_candidate_id
        await controller._observe_owner_barge(
            activity, SimpleNamespace(start_sample=16_000, end_sample=32_000, captured_at=2.0),
            handle, progress, decision,
        )
        assert "stop" not in calls
        assert controller._owner_barge_expired
        # A new candidate can begin after silence; only fresh owner evidence
        # from that candidate may authorize the next interruption.
        controller._owner_barge_expired = False
        controller._speaker_snapshot = None
        await controller._observe_owner_barge(
            activity, SimpleNamespace(start_sample=0, end_sample=16_000, captured_at=1.0),
            handle, progress, decision,
        )
        candidate = controller._owner_barge_candidate_id
        controller._speaker_snapshot = _snapshot(RollingSpeakerIdentity.OWNER)
        controller._speaker_snapshot_barge_id = candidate
        await controller._observe_owner_barge(
            activity, SimpleNamespace(start_sample=16_000, end_sample=32_000, captured_at=2.0),
            handle, progress, decision,
        )
        assert calls.index("stop") < calls.index("speech")
        assert calls[-1] == "new_turn"
    asyncio.run(scenario())


def test_owner_barge_aec_failure_never_falls_back_to_unprotected_stop():
    async def scenario():
        calls = []
        controller = object.__new__(EpisodeController)
        controller.owner_only_barge_in = True
        controller.state = SimpleNamespace(listening=True)
        async def log(*_args, **_kwargs):
            calls.append("suppressed")
        controller.episode = SimpleNamespace(closed=False, log=log)
        handle = SimpleNamespace(workspace=SimpleNamespace(playback_started=True))
        controller.output = SimpleNamespace(active=True, handle=handle)
        controller.player = object()
        controller._playback_progress = lambda _handle: {"remaining_ms": 1000}
        controller._barge_in_protected_available = lambda: False
        controller.barge_in_gate = SimpleNamespace(reset=lambda: calls.append("gate_reset"))
        controller._owner_barge_candidate_id = "candidate"
        controller._owner_barge_start_sample = 0
        controller._owner_barge_started_at = 1.0
        controller._owner_barge_output_key = "output"
        async def forbidden(_handle):
            raise AssertionError("AEC failure stopped owner-only playback")
        controller._handle_aec_unavailable = forbidden
        result = await controller._observe_activity(
            SimpleNamespace(is_speech=True), SimpleNamespace(captured_at=1.2),
        )
        assert result is None
        assert calls == ["gate_reset", "suppressed"]
        assert controller._owner_barge_candidate_id is None
    asyncio.run(scenario())


def test_playback_finishing_before_owner_confirmation_returns_to_normal_input():
    async def scenario():
        calls = []
        controller = object.__new__(EpisodeController)
        controller.state = SimpleNamespace(listening=True)
        controller._owner_barge_candidate_id = "pending"
        controller._owner_barge_start_sample = 0
        controller._owner_barge_started_at = 1.0
        controller._owner_barge_output_key = "output"
        controller._owner_barge_expired = True
        controller._barge_trace_state = "pending"
        handle = SimpleNamespace(workspace=SimpleNamespace(playback_started=True))
        controller.output = SimpleNamespace(active=True, handle=handle)
        controller.player = object()
        controller._playback_progress = lambda _handle: {"remaining_ms": 0}
        controller.barge_in_gate = SimpleNamespace(reset=lambda: calls.append("gate_reset"))

        async def speech_activity(value):
            calls.append(("ordinary_speech", value))

        controller.episode = SimpleNamespace(closed=False, speech_activity=speech_activity)
        result = await controller._observe_activity(
            SimpleNamespace(is_speech=True), SimpleNamespace(captured_at=1.2),
        )
        assert result.is_speech
        assert controller._owner_barge_candidate_id is None
        assert controller._owner_barge_expired is False
        assert calls == ["gate_reset", ("ordinary_speech", True)]

    asyncio.run(scenario())


def test_owner_barge_deadline_expires_without_stop():
    async def scenario():
        controller = object.__new__(EpisodeController)
        controller._owner_barge_candidate_id = "candidate"
        controller._owner_barge_start_sample = 0
        controller._owner_barge_started_at = 1.0
        controller._owner_barge_output_key = "output"
        controller._owner_barge_expired = False
        controller.barge_in_gate = SimpleNamespace(reset=lambda: None)
        result = await controller._observe_owner_barge(
            SimpleNamespace(is_speech=True),
            SimpleNamespace(start_sample=40_000, end_sample=40_320, captured_at=3.6),
            SimpleNamespace(workspace=SimpleNamespace(playback_key="output"), speech_id="speech"),
            {"output_estimated_ms": 1000},
            SimpleNamespace(candidate=True, confirmed=True, duration_ms=2500),
        )
        assert result is None
        assert controller._owner_barge_expired
        assert controller._owner_barge_candidate_id is None
    asyncio.run(scenario())
