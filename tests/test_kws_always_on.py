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
    async def scenario():
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
            audio="READY", kws="READY", asr="ERROR", llm="READY", tts="READY",
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
        assert state.services["asr"] == "ERROR"
        assert asr.connect_calls == 0
        await controller.stop_listening()

    asyncio.run(scenario())


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
