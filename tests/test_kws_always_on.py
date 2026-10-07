from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_controller import InputController
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.config import load_config
from voice_terminal.core.event_bus import EventBus
from voice_terminal.core.state import RuntimeState, TerminalState
from voice_terminal.agent.scheduler import AgentDecisionScheduler


class _IdleKws:
    running = True
    error = None

    def __init__(self) -> None:
        self.frames = []

    def submit(self, frame) -> bool:
        self.frames.append(frame)
        return True

    def poll(self):
        return None


class _ForbiddenVad:
    def feed(self, _pcm):
        raise AssertionError("VAD must not run while KWS owns input")


def test_wake_phrases_and_detector_knobs_come_from_config(tmp_path):
    """The measured operating point and the multi-phrase list are config-owned."""

    default_path = Path(__file__).parents[1] / "config" / "default.yaml"
    config = load_config(default_path)
    assert config.wake.keywords_score == 1.5
    assert config.wake.num_trailing_blanks == 3
    assert config.wake.max_active_paths == 8
    assert config.wake.keywords == ("hei claw",)
    assert config.wake.keyword_labels == ("HEI_CLAW",)

    override = tmp_path / "local.yaml"
    override.write_text(
        "wake:\n"
        "  keywords: [\"七三\", \"你好七三\", \"七三同学\"]\n"
        "  max_active_paths: 12\n",
        encoding="utf-8",
    )
    multi = load_config(default_path, override)
    assert multi.wake.keywords == ("七三", "你好七三", "七三同学")
    assert multi.wake.keyword == "七三"
    assert multi.wake.keyword_label == "七三"
    assert multi.wake.keyword_labels == ("七三", "你好七三", "七三同学")
    assert multi.wake.max_active_paths == 12

    # A file that only carries the legacy single keyword still yields one phrase.
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("wake:\n  keyword: \"hei claw\"\n  keyword_label: \"HEI_CLAW\"\n", encoding="utf-8")
    single = load_config(default_path, legacy)
    assert single.wake.keywords == ("hei claw",)
    assert single.wake.keyword_label == "HEI_CLAW"


def test_enabling_wake_always_enters_standby(monkeypatch):
    default_path = Path(__file__).parents[1] / "config" / "default.yaml"
    # load_config intentionally loads .env.local, so make the two cases
    # explicit instead of depending on the developer machine's environment.
    monkeypatch.setenv("VOICE_WAKE_ENABLED", "false")
    assert load_config(default_path).wake.always_on is False
    monkeypatch.setenv("VOICE_WAKE_ENABLED", "true")
    config = load_config(default_path)
    assert config.wake.enabled is True
    assert config.wake.always_on is True


def test_always_on_can_start_protected_kws_while_asr_is_unavailable():
    state = RuntimeState(state=TerminalState.IDLE, wake_always_on=True)
    state.services.update(
        audio="READY", kws="READY", asr="ERROR", llm="READY", tts="READY",
    )
    assert state.can_start is True


def test_always_on_start_does_not_open_unready_asr_input():
    async def scenario(asr_status):
        class Controller(InputController):
            async def _on_asr_result(self, _message):
                return None

            async def _listen_loop(self):
                await asyncio.Event().wait()

            async def _cancel_turn(self, **_kwargs):
                return None

        class Capture:
            started = False

            async def start(self):
                self.started = True

            async def stop(self):
                self.started = False

        class Asr:
            connected = False
            connect_calls = 0
            on_result = None

            async def connect(self):
                self.connect_calls += 1
                raise AssertionError("ASR must not be opened before KWS activation")

            async def close(self):
                return None

        class Vad:
            def reset(self):
                return None

        state = RuntimeState(state=TerminalState.IDLE)
        state.services.update(
            audio="READY", kws="READY", asr=asr_status, llm="READY", tts="READY",
        )
        capture, asr = Capture(), Asr()
        controller = Controller(
            state=state, bus=EventBus(), history=SimpleNamespace(records=[]),
            capture=capture, kws=_IdleKws(), vad=Vad(), asr=asr,
            llm=object(), tts=object(), player=None,
            wake_enabled=True, wake_always_on=True, wake_keyword="hei claw",
            wake_preroll_ms=0, vad_min_speech_ms=250, vad_end_silence_ms=300,
            max_turn_ms=15000, bridge_min_chars=1, bridge_max_chars=10,
            bridge_max_wait_ms=10, bridge_breaks="，。",
        )

        await controller.start_listening()
        assert state.listening is True
        assert state.input_owner == "kws"
        assert state.services["asr"] == asr_status
        assert asr.connect_calls == 0
        await controller.stop_listening()
        # ASR（语音识别）会话正常关闭后，连续重启 KWS（关键词唤醒）监听
        # 仍应保持就绪；真实 ASR 故障已在上面的 ERROR（错误）分支保留。
        for _ in range(2):
            await controller.start_listening()
            assert state.listening is True
            assert state.input_owner == "kws"
            assert state.services["asr"] == "READY"
            assert asr.connect_calls == 0
            await controller.stop_listening()

    for asr_status in ("ERROR", "READY"):
        asyncio.run(scenario(asr_status))


def test_kws_owner_short_circuits_before_vad_workspace_and_asr():
    async def scenario():
        controller = object.__new__(RealtimeController)
        controller.audio_processor = None
        controller.wake_always_on = True
        controller.state = SimpleNamespace(input_owner="kws")
        controller.kws = _IdleKws()
        controller.vad = _ForbiddenVad()
        controller.workspace = None
        controller._last_frame = None
        controller._asr_queue_overflow = False

        await controller.handle_frame(AudioFrame(b"\x00\x00" * 320, 0, 1.0))

        assert len(controller.kws.frames) == 1
        submitted = controller.kws.frames[0]
        assert submitted.start_sample == 0
        assert submitted.end_sample == 320
        assert controller.workspace is None
        assert controller._last_frame is None

    asyncio.run(scenario())


def test_final_settlement_waits_for_user_input_and_rejects_stale_generation():
    async def scenario():
        controller = object.__new__(EpisodeController)
        controller.state = SimpleNamespace(listening=True, input_owner="asr")
        controller._activation_generation = 4
        controller.workspace = SimpleNamespace(turn_id="turn-new")
        controller.episode = SimpleNamespace(
            user_speaking=True,
            scheduler=SimpleNamespace(generation=8),
        )
        controller._background = set()
        returned = []

        async def return_to_kws(generation):
            returned.append(generation)
            return True

        controller._return_to_kws = return_to_kws
        await controller._final_system_check_settled({"generation": 8})
        await asyncio.sleep(0.06)
        assert returned == []

        # A finalized/refined user input owns a new ladder generation.  The
        # old final callback must never switch that generation back to KWS.
        controller.episode.scheduler.generation = 9
        controller.workspace = None
        controller.episode.user_speaking = False
        await asyncio.sleep(0.06)
        assert returned == []

    asyncio.run(scenario())


def test_final_settlement_waits_for_output_transaction_to_become_idle():
    async def scenario():
        controller = object.__new__(EpisodeController)
        controller.state = SimpleNamespace(listening=True, input_owner="asr")
        controller._activation_generation = 4
        controller.workspace = None
        controller.output = SimpleNamespace(active=False, busy=True)
        controller.episode = SimpleNamespace(
            user_speaking=False,
            scheduler=SimpleNamespace(generation=8),
        )
        controller._background = set()
        returned = []

        async def return_to_kws(generation):
            returned.append(generation)
            return True

        controller._return_to_kws = return_to_kws
        await controller._final_system_check_settled({"generation": 8})
        await asyncio.sleep(0.06)
        assert returned == []

        controller.output.busy = False
        await asyncio.sleep(0.06)
        assert returned == [4]

    asyncio.run(scenario())


def _ownership_controller(*, generation=8, owner="asr", output_busy=False):
    """EpisodeController double that only instruments the handover decision."""

    returned: list = []
    tasks: set = set()

    async def publish(*_args, **_kwargs):
        return None

    async def episode_log(*_args, **_kwargs):
        return None

    async def settle_committed_turn(*_args, **_kwargs):
        return None

    controller = object.__new__(EpisodeController)
    controller.wake_always_on = True
    controller.state = SimpleNamespace(
        listening=True, input_owner=owner, agent={}, latency={},
    )
    controller._activation_generation = 4
    controller.workspace = None
    controller.output = SimpleNamespace(active=False, busy=output_busy)
    controller._track = tasks.add
    controller._settled_turns = set()
    controller._committed_workspaces = {}
    controller.history = SimpleNamespace(
        commit_delivery=lambda *_a, **_kw: False,
        has_speech=lambda *_a, **_kw: False,
        conversation=SimpleNamespace(append=lambda *_a, **_kw: None),
    )
    controller.episode = SimpleNamespace(
        user_speaking=False, last_turn_id=None,
        scheduler=SimpleNamespace(generation=generation), log=episode_log,
    )
    controller.on_silence_anchor = None
    controller._publish = publish
    controller._settle_committed_turn = settle_committed_turn

    async def return_to_kws(activation_generation):
        returned.append(activation_generation)
        return True

    controller._return_to_kws = return_to_kws
    return controller, returned


def test_speech_completed_never_hands_the_microphone_back_to_kws():
    """Regression (78dc97c): a settled speech must not cut the ASR window short."""

    async def scenario():
        controller, returned = _ownership_controller()
        await controller._agent_event("speech.completed", {
            "speech_id": "speech-wake", "turn_id": "wake_1",
            "conversation_id": "conversation_a", "owner_scope": "system_check",
            "text": "我在，请说。", "status": "completed",
            "delivery": {"status": "full", "termination": "completed"},
        })
        await asyncio.sleep(0.15)
        assert returned == [], (
            "语音结算不得改变输入所有权；交接只能由 final_silence 系统检查触发"
        )

    asyncio.run(scenario())


def test_final_settlement_without_a_ladder_generation_is_ignored():
    """A callback that cannot name its ladder must not rearm KWS."""

    async def scenario():
        controller, returned = _ownership_controller()
        await controller._final_system_check_settled({})
        await asyncio.sleep(0.15)
        assert returned == [], "缺少 generation 的回调无法证明静默窗口已到期"

        await controller._final_system_check_settled({"generation": 8})
        await asyncio.sleep(0.15)
        assert returned == [4], "正常结算仍必须在输入空闲后交还 KWS"

    asyncio.run(scenario())


def test_model_initiated_speech_arms_the_same_silence_window():
    """A KWS→ASR rotation without a wake word still needs its timer."""

    async def scenario():
        armed: list = []
        controller = object.__new__(EpisodeController)
        controller.on_user_activity = None

        async def arm_silence_window(*_args, **_kwargs):
            armed.append(True)
            return 1

        controller.episode = SimpleNamespace(arm_silence_window=arm_silence_window)

        async def rotated():
            return True

        async def already_asr():
            return False

        controller.activate_asr_for_model_speech = rotated
        await controller._on_model_speech_accepted()
        assert armed == [True], "KWS→ASR 轮换后必须挂上静默梯子"

        controller.activate_asr_for_model_speech = already_asr
        await controller._on_model_speech_accepted()
        assert armed == [True], "没有发生所有权轮换时不得重复挂梯子"

    asyncio.run(scenario())


def test_early_silence_marks_immediately_and_survives_new_ladder():
    async def scenario():
        scheduler = AgentDecisionScheduler(SimpleNamespace(
            scheduler_tick_ms=400, final_silence_ms=90_000,
        ))
        scheduler.arm()
        dispatched = []
        episode = SimpleNamespace(
            closed=False, user_speaking=True, active_request=object(),
            scheduler=scheduler, _ensure_runner=lambda: dispatched.append(scheduler.generation),
        )

        async def log(*_args, **_kwargs):
            return None

        async def arm_silence_window(*_args, **_kwargs):
            scheduler.arm()

        episode.log = log
        episode.arm_silence_window = arm_silence_window
        controller = object.__new__(EpisodeController)
        controller.wake_always_on = True
        controller.kws = _IdleKws()
        controller.state = SimpleNamespace(listening=True, input_owner="asr")
        controller._activation_generation = 3
        controller._early_silence_task = None
        controller._background = set()
        controller.workspace = SimpleNamespace(turn_id="speaking")
        controller.output = SimpleNamespace(active=False, busy=True)
        controller.episode = episode

        assert await controller.request_early_silence() == "scheduled"
        assert scheduler.final_pending
        assert dispatched == [1]
        assert await controller.request_early_silence() == "already_pending"

        # New input defers/replaces the ladder; the early request survives and
        # marks the newly activated generation without waiting for idle gates.
        scheduler.defer()
        await asyncio.sleep(0.07)
        assert not scheduler.final_pending
        scheduler.activate_deferred()
        await asyncio.sleep(0.07)
        assert scheduler.final_pending
        assert dispatched == [1, 2]

        controller.state.input_owner = "kws"
        await asyncio.sleep(0.07)
        assert controller._early_silence_task is None

    asyncio.run(scenario())


def test_early_silence_rejects_non_asr_and_cancels_on_reset_boundary():
    async def scenario():
        controller = object.__new__(EpisodeController)
        controller.wake_always_on = True
        controller.kws = _IdleKws()
        controller.state = SimpleNamespace(listening=True, input_owner="kws")
        scheduler = AgentDecisionScheduler(SimpleNamespace(
            scheduler_tick_ms=400, final_silence_ms=90_000,
        ))
        scheduler.arm()
        scheduler.mark_final_pending()
        controller.episode = SimpleNamespace(closed=False, scheduler=scheduler)
        assert await controller.request_early_silence() == "not_asr"
        controller.state.input_owner = "asr"
        controller._early_silence_task = asyncio.create_task(asyncio.sleep(60))
        await controller._cancel_early_silence()
        assert controller._early_silence_task is None
        assert not scheduler.final_pending
        assert not scheduler.armed

    asyncio.run(scenario())


def test_early_silence_runs_final_check_before_kws_handover():
    from tests.test_scheduler_staircase import (
        _BlockingProvider, _speaking_runtime, _finish_blocking_request,
    )

    async def scenario():
        provider = _BlockingProvider()
        runtime = _speaking_runtime(provider)
        runtime._ensure_runner = lambda: None
        runtime.scheduler.arm()
        anchor = runtime.scheduler.anchor_monotonic
        runtime.scheduler.start_heartbeat(anchor)
        controller = object.__new__(EpisodeController)
        controller.wake_always_on = True
        controller.kws = _IdleKws()
        controller.state = SimpleNamespace(listening=True, input_owner="asr")
        controller._activation_generation = 1
        controller._early_silence_task = None
        controller._background = set()
        controller.workspace = SimpleNamespace(turn_id="current-input")
        controller.output = runtime.output
        runtime.output.active = True
        runtime.output.busy = True
        controller.episode = runtime
        returned = []

        async def return_to_kws(generation):
            returned.append(generation)
            controller.state.input_owner = "kws"
            controller._activation_generation += 1
            return True

        controller._return_to_kws = return_to_kws
        runtime.on_final_system_check_settled = controller._final_system_check_settled
        runtime.can_dispatch_final_silence = controller._final_silence_dispatch_ready
        assert await controller.request_early_silence() == "scheduled"
        assert runtime.scheduler.final_pending
        assert runtime.output.active and runtime.output.busy, "排队请求不能取消当前播报"
        assert returned == []
        await runtime._on_heartbeat(anchor + 0.4)
        await asyncio.sleep(0)
        assert provider.calls == 0, "活动输入和播报时不能派发最终静默轮"
        assert runtime.scheduler.final_pending

        controller.workspace = None
        runtime.user_speaking = False
        runtime.output.active = False
        runtime.output.busy = False
        # A cancelled queue entry is stale bookkeeping, not active playback.
        runtime.output._pending = [SimpleNamespace(
            reason="superseded", task=SimpleNamespace(done=lambda: True),
        )]
        await runtime._on_heartbeat(anchor + 0.8)
        await asyncio.sleep(0)
        assert runtime.active_dispatch_context["prompt_kind"] == "final_silence"
        assert runtime.active_dispatch_context["silence_elapsed_ms"] < 1000
        assert provider.calls == 1
        assert returned == []
        await _finish_blocking_request(runtime, provider)
        await asyncio.sleep(0.06)
        assert returned == [1]
        assert controller.state.input_owner == "kws"
        assert provider.calls == 1
        assert controller._early_silence_task is None

    asyncio.run(scenario())
