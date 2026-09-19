"""Reproduction of the KWS wake-window defect.

These are *characterization* tests for the behaviour diagnosed in
``docs/known-issues/kws-wake-recall-diagnosis.md``.  They pin down, with the
real controller code, why a user who repeats the wake word hears nothing:

1. while the microphone belongs to ASR (the "continuous conversation" window
   that follows every answer), no captured frame is handed to the keyword
   spotter at all, so the wake word cannot be detected no matter how it is
   pronounced;
2. a turn whose transcript is exactly the wake word is stripped to an empty
   string and settled as ``empty_input`` — no Agent dispatch, no speech, no
   acknowledgement of any kind.

Both are deliberately asserted as the current contract.  When the fix lands
(keep feeding KWS during ASR ownership, or answer a wake-only transcript with
the existing ``kws_wake`` acknowledgement), these assertions must be updated to
the new behaviour.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

from voice_terminal.controller.episode_controller import EpisodeController
from voice_terminal.controller.realtime_controller import RealtimeController
from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.kws.keywords import strip_wake_prefix

TOKEN = ("sub-1", 1)


class _RecordingKws:
    running = True
    error = None

    def __init__(self) -> None:
        self.frames = []

    def submit(self, frame) -> bool:
        self.frames.append(frame)
        return True

    def poll(self):
        return None


class _Activity:
    speech_started = False
    speech_end_sample = None
    is_speech = False


class _Vad:
    def feed(self, _pcm):
        return _Activity()

    def reset(self):
        return None


def _frame_controller(owner: str) -> RealtimeController:
    controller = object.__new__(RealtimeController)
    controller.audio_processor = None
    controller.wake_enabled = True
    controller.wake_always_on = True
    controller.wake_preroll_ms = 0
    controller.kws = _RecordingKws()
    controller.vad = _Vad()
    controller.state = SimpleNamespace(input_owner=owner, listening=True)
    controller.workspace = None
    controller._last_frame = None
    controller._recent = deque()
    controller._asr_queue_overflow = False
    controller._reset_vad_pending = False
    controller._vad_origin = None
    controller._vad_generation = 0
    controller._speech_start_sample = None
    controller._speech_end_sample = None
    controller._speech_samples = 0
    controller._active_input = False

    async def observe(activity, _frame):
        return activity

    controller._observe_activity = observe
    return controller


def test_wake_word_cannot_reach_the_spotter_while_asr_owns_input():
    """In the post-answer window the wake word is invisible to the detector."""

    async def scenario():
        standby = _frame_controller("kws")
        await standby.handle_frame(AudioFrame(b"\x00\x00" * 320, 0, 1.0))
        await standby.handle_frame(AudioFrame(b"\x00\x00" * 320, 320, 1.02))
        assert len(standby.kws.frames) == 2, "待唤醒状态必须持续把 PCM 交给 KWS"

        # Same audio, same controller, only the input owner differs: this is the
        # state the microphone sits in for `silence.seconds` after every reply.
        talking = _frame_controller("asr")
        await talking.handle_frame(AudioFrame(b"\x00\x00" * 320, 0, 1.0))
        await talking.handle_frame(AudioFrame(b"\x00\x00" * 320, 320, 1.02))
        assert talking.kws.frames == [], (
            "ASR 所有权下 KWS 收不到任何帧 —— 这正是唤醒词在对话窗口内不可能被识别的原因"
        )

    asyncio.run(scenario())


def _committed_controller():
    """An EpisodeController with only the commit boundary instrumented."""

    controller = object.__new__(EpisodeController)
    controller.state = SimpleNamespace(listening=True, current=SimpleNamespace(update=lambda **_kw: None))
    controller.ambient_store = None
    controller.episode_history = {}
    controller.wake_enabled = True
    controller.wake_keyword = "七三"
    controller.wake_keywords = ("七三", "你好七三", "七三同学")
    calls = {"observe": [], "settled": [], "retired": [], "committed": []}

    async def speaker_decision(_ws):
        return None

    async def commit_input_workspace(ws):
        calls["committed"].append(ws.turn_id)

    async def settle(turn_id, *, outcome, latency=None):
        calls["settled"].append((turn_id, outcome))

    async def retire(ws):
        calls["retired"].append(ws.turn_id)

    async def observe(kind, **_kw):
        calls["observe"].append(kind)

    async def detail(*_a, **_kw):
        return None

    controller._speaker_decision = speaker_decision
    controller._commit_input_workspace = commit_input_workspace
    controller._settle_committed_turn = settle
    controller._retire_committed_input = retire
    controller._detail = detail
    controller.episode = SimpleNamespace(episode_id="conversation_x", observe=observe)
    return controller, calls


def test_wake_word_only_utterance_is_settled_silently_as_empty_input():
    """A bare wake word produces no Agent dispatch and no spoken reply."""

    async def scenario():
        controller, calls = _committed_controller()
        raw_text = "七三"                       # what ASR hears in the window
        stripped = strip_wake_prefix(raw_text, controller.wake_keywords)
        assert stripped == "", "裸唤醒词会被完整剥离成空串"

        workspace = SimpleNamespace(
            turn_id="input_1", final_text=stripped, paraformer_final=raw_text,
            refined_text="", audio_cues=[], analysis={}, segments=[],
            speaker_decision={}, transcript_updates=[], speaker_blocked=False,
            generation=1, timings={}, episode_id=None, refinement_available=False,
        )
        assert await controller._input_committed(workspace, TOKEN) is True

        assert calls["observe"] == [], "不应派发任何 Agent/LLM 回合"
        assert calls["settled"] == [("input_1", "empty_input")]
        assert calls["retired"] == ["input_1"]
        # The user-visible result is the frontend label behind empty_input.
        assert calls["settled"][0][1] == "empty_input"  # -> “没有听到内容”

        # Every configured spelling behaves the same way, so the "silent"
        # window is not an artifact of one particular phrase.
        for phrase in controller.wake_keywords:
            assert strip_wake_prefix(phrase, controller.wake_keywords) == ""

    asyncio.run(scenario())


def test_wake_word_with_a_command_is_still_delivered():
    """Control case: the same window works when a command follows the wake word."""

    async def scenario():
        controller, calls = _committed_controller()
        stripped = strip_wake_prefix("七三，帮我查下天气", controller.wake_keywords)
        assert stripped == "帮我查下天气"
        # Longest-first stripping keeps the leftover command intact.
        assert strip_wake_prefix(
            "七三同学，帮我查下天气", controller.wake_keywords,
        ) == "帮我查下天气"

        workspace = SimpleNamespace(
            turn_id="input_2", final_text=stripped, paraformer_final="七三，帮我查下天气",
            refined_text="", audio_cues=[], analysis={}, segments=[],
            speaker_decision={}, transcript_updates=[], speaker_blocked=False,
            generation=1, timings={}, episode_id=None, refinement_available=False,
        )
        assert await controller._input_committed(workspace, TOKEN) is True

        assert calls["observe"] == ["USER_INPUT_FINALIZED"]
        assert calls["settled"] == []

    asyncio.run(scenario())
