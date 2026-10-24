"""播放前输入交接：保留真实播放器许可与 PCM（原始音频采样）队列。"""
from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
import time

import pytest

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.episode_output import EpisodeSpeechOutput, SpeechHandle
from voice_terminal.controller.options import InputOptions
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace
from voice_terminal.core.state import RuntimeState
from voice_terminal.session.runtime import ConversationRuntime
from voice_terminal.session.ledger import SessionLedger


async def ignore_event(*_args, **_kwargs):
    """ignore_event（隔离外部事件发送），不替代播放器或输入创建逻辑。"""


def make_held_output(mode, *, controller_type=RealtimeController):
    """make_held_output（构造尚未播放的有效输出与新输入控制器）。"""
    controller = object.__new__(controller_type)
    controller.player = PcmPlayer(sample_rate=16000, channels=1, output_device=None)
    controller.tts = None
    controller.options = InputOptions()
    controller.bridge_options = {}
    controller.state = RuntimeState()
    controller.turns = SimpleNamespace(begin=lambda: "new-input")
    controller.conversation = ConversationRuntime()
    controller.speaker_mode = mode
    controller.speaker_router = None
    controller._speech_start_sample = None
    controller._turn_timings = {}
    controller._begin_tool_scope = lambda _: None
    controller._recent = deque()
    controller.wake_preroll_ms = 0
    controller.barge_in_gate = SimpleNamespace(reset=lambda: None)
    controller._sub_parts = {}
    controller._sub_finals = {}
    controller._new_sub = lambda _: None
    controller.analysis_health = {}
    controller._detail = ignore_event
    controller._state_event = ignore_event
    output = controller.output = EpisodeSpeechOutput(controller)
    output.runtime = SimpleNamespace(closed=False)
    handle = SpeechHandle(output, ("conversation", "request"), "reply", 0, "calm")
    handle.task = asyncio.get_running_loop().create_future()
    handle.state = "READY"
    output.handle = handle
    controller.player.allow(handle.workspace.playback_key)
    return controller, output, handle


@pytest.mark.parametrize("state", ["unknown", "non_owner_confirmed", "unavailable"])
def test_unadmitted_continuous_activity_does_not_hold_or_drop_a_valid_reply(state):
    async def scenario():
        controller, output, handle = make_held_output("filter", controller_type=EpisodeController)
        episode = controller.episode = AgentEpisodeRuntime(
            None, SimpleNamespace(), output, ignore_event,
            conversation=controller.conversation, ledger=SessionLedger(),
        )
        output.runtime = episode
        controller.state.listening = True
        controller.workspace = None
        activity = SimpleNamespace(is_speech=True)
        pcm = b"\x01\x00" * 320
        controller.player.enqueue(handle.workspace.playback_key, pcm)
        # 新候选尚未创建时也不能用原始声音检测暂停输出。
        frame = AudioFrame(b"\x00\x00" * 320, 0, time.monotonic())
        assert await controller._observe_activity(activity, frame) is activity
        await controller._new_turn(frame)
        controller.workspace.speaker_state = state
        controller.workspace.speaker_decision = {"state": state, "route": "blocked"}
        # 连续三秒环境声：原始检测进入识别，输出事务仍可实际消费音频。
        for index in range(150):
            frame = AudioFrame(b"\x00\x00" * 320, index * 320, frame.captured_at + .02)
            assert await controller._observe_activity(activity, frame) is activity
        assert output.playback_hold_active is False
        assert episode.user_speaking is False
        assert episode._user_speech_epoch == 0
        assert handle.reason is None
        rendered = bytearray(640)
        controller.player._callback(rendered, 320, None, None)
        assert rendered == pcm
        assert controller.player.has_started(handle.workspace.playback_key)
        handle.task.cancel()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["off", "observe", "filter"])
def test_new_input_preserves_unheld_output_transaction(mode):
    async def scenario():
        controller, output, handle = make_held_output(mode)
        pcm = b"\x01\x00" * 320
        controller.player.enqueue(handle.workspace.playback_key, pcm)
        assert output.playback_hold_active is False
        await controller._new_turn(AudioFrame(b"\x00\x00" * 320, 0, time.monotonic()))
        rendered = bytearray(640)
        controller.player._callback(rendered, 320, None, None)
        assert rendered == pcm
        assert handle.reason is None
        handle.task.cancel()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_passive_activity_is_preserved_outside_filter_mode(mode):
    controller = object.__new__(EpisodeController)
    controller.speaker_mode = mode
    controller.workspace = TurnWorkspace("input", speaker_mode=mode)
    assert controller._admitted_passive_speech(SimpleNamespace(is_speech=True)) is True
    assert controller._admitted_passive_speech(SimpleNamespace(is_speech=False)) is False


@pytest.mark.parametrize("mode", ["off", "observe", "filter"])
@pytest.mark.parametrize("audio_already_queued", [False, True])
def test_new_input_preserves_held_reply_until_actual_playback(mode, audio_already_queued):
    async def scenario():
        controller, output, handle = make_held_output(mode)
        player, key = controller.player, handle.workspace.playback_key
        pcm = b"\x01\x00" * 320  # pcm（20 毫秒非零音频，便于验证实际消费）。
        if audio_already_queued:
            player.enqueue(key, pcm)
        assert output.hold_unplayed() == handle.speech_id
        await controller._new_turn(AudioFrame(b"\x00\x00" * 320, 0, time.monotonic()))
        blocked = bytearray(640)
        player._callback(blocked, 320, None, None)
        assert not any(blocked)
        assert player.buffered_ms == (20 if audio_already_queued else 0)

        assert output.release_held() == handle.speech_id
        # 重复结束事件不得再恢复一次，也不得清除有效许可。
        output.release_held()
        if not audio_already_queued:
            player.enqueue(key, pcm)
        rendered = bytearray(640)
        player._callback(rendered, 320, None, None)
        assert rendered == pcm
        assert player.has_started(key)
        assert handle.reason is None
        handle.task.cancel()
    asyncio.run(scenario())


def test_lost_player_permission_cancels_instead_of_reporting_successful_release():
    async def scenario():
        controller, output, handle = make_held_output("filter")
        events = []
        output._schedule_output_event = lambda _h, kind, *_args, **_kwargs: events.append(kind)
        output.hold_unplayed()
        controller.player.block()  # 模拟其他生命周期路径已经撤销播放许可。
        assert output.release_held() is None
        assert handle.reason == "playback_resume_failed"
        assert handle.task.cancelled()
        assert handle.playback_ready.is_set()  # 取消唤醒等待者，身份校验阻止输出。
        assert "playback_hold_released" not in handle.workspace.timings
        assert events == ["TTS_HELD", "TTS_RESUME_FAILED"]
        controller.player.enqueue(handle.workspace.playback_key, b"\x01\x00" * 320)
        assert controller.player.buffered_ms == 0
    asyncio.run(scenario())


def test_cancelled_held_reply_is_not_revived_by_late_input_end():
    async def scenario():
        controller, output, handle = make_held_output("filter")
        output.hold_unplayed()
        output.cancel_all("reset")
        assert output.release_held() is None
        controller.player.enqueue(handle.workspace.playback_key, b"\x01\x00" * 320)
        assert controller.player.buffered_ms == 0
        assert handle.reason == "reset"
    asyncio.run(scenario())


def test_vad_silence_fallback_releases_held_tts_with_timeout_reason():
    async def scenario():
        controller, output, handle = make_held_output("off")
        controller._background = set()
        controller._tts_hold_fallback_task = None
        controller._tts_hold_fallback_generation = 0
        controller.options = InputOptions(tts_hold_fallback_ms=20)
        controller.episode = object()
        controller._track = lambda task: task.add_done_callback(controller._background.discard)
        releases = []
        output._schedule_output_event = lambda _h, kind, _trace, **payload: releases.append((kind, payload))
        output.hold_unplayed()

        controller._start_tts_hold_fallback()
        fallback = controller._tts_hold_fallback_task
        await asyncio.sleep(0)
        assert output.playback_hold_active is True
        await fallback

        released = [(kind, payload) for kind, payload in releases if kind == "TTS_RELEASED"]
        assert len(released) == 1
        assert released[0][1]["reason"] == "vad_silence_timeout"
        assert output.playback_hold_active is False
        handle.task.cancel()

    asyncio.run(scenario())


def test_explicit_tts_release_records_the_smart_turn_reason():
    async def scenario():
        controller, output, handle = make_held_output("off")
        events = []
        output._schedule_output_event = lambda _h, kind, *_args, **payload: events.append((kind, payload))
        output.hold_unplayed()

        assert output.release_held(reason="smart_turn") == handle.speech_id
        released = [(kind, payload) for kind, payload in events if kind == "TTS_RELEASED"]
        assert len(released) == 1
        assert released[0][1]["reason"] == "smart_turn"
        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)

    asyncio.run(scenario())


def test_resumed_speech_cancels_old_fallback_and_new_silence_gets_fresh_deadline():
    async def scenario():
        controller, output, handle = make_held_output("off")
        controller._background = set()
        controller._tts_hold_fallback_task = None
        controller._tts_hold_fallback_generation = 0
        controller.options = InputOptions(tts_hold_fallback_ms=25)
        controller.episode = object()
        controller._track = lambda task: task.add_done_callback(controller._background.discard)
        releases = []
        output._schedule_output_event = lambda _h, kind, _trace, **payload: releases.append((kind, payload))
        output.hold_unplayed()

        controller._start_tts_hold_fallback()
        first = controller._tts_hold_fallback_task
        await asyncio.sleep(0.005)
        controller._cancel_tts_hold_fallback()  # 用户重新开口。
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0.03)
        assert output.playback_hold_active is True
        assert not any(kind == "TTS_RELEASED" for kind, _ in releases)

        controller._start_tts_hold_fallback()  # 下一次 VAD 静默重新计时。
        second = controller._tts_hold_fallback_task
        await second
        released = [(kind, payload) for kind, payload in releases if kind == "TTS_RELEASED"]
        assert len(released) == 1
        assert released[0][1]["reason"] == "vad_silence_timeout"
        handle.task.cancel()

    asyncio.run(scenario())
