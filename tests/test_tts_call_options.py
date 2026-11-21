"""TTS hook（后端钩子）兼容旧签名，且不重试真实内部异常。"""
import asyncio
import gc
from types import SimpleNamespace
import weakref

import pytest

from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.tts.call_options import call_tts_hook


@pytest.mark.parametrize("fields", [(), ("fresh",), ("fresh", "turn_id")])
def test_legacy_session_declarations_receive_only_supported_fields(fields):
    calls = []
    namespace = {"calls": calls}
    parameters = ", ".join(f"{name}=None" for name in fields)
    exec(f"def session({parameters}):\n    calls.append(locals())\n    return 'session'", namespace)
    assert call_tts_hook(namespace["session"], fresh=True, turn_id="turn", speech_id="speech") == "session"
    assert len(calls) == 1
    assert set(calls[0]) == set(fields)


def test_output_session_factory_preserves_internal_type_error_and_calls_once():
    class Provider:
        calls = 0
        def session(self, *, fresh=False, turn_id=None, speech_id=None):
            self.calls += 1
            raise TypeError("内部状态转换失败")
    provider = Provider()
    output = object.__new__(EpisodeSpeechOutput)
    output.tts = provider
    output.handle = None
    output._handles_by_token = {}
    ws = SimpleNamespace(turn_id="turn", token=("turn", 1))
    with pytest.raises(TypeError, match="内部状态转换失败"):
        output._new_tts_session(ws)
    assert provider.calls == 1


def test_required_remote_playback_identity_and_open_keyword_contract_are_preserved():
    def session(*, playback_key, **options):
        return playback_key, options
    assert call_tts_hook(session, playback_key="device-key", turn_id="turn", speech_id="speech") == (
        "device-key", {"turn_id": "turn", "speech_id": "speech"})


@pytest.mark.parametrize("field", ["playback_key", "voice_snapshot", "inference_steps", "controls"])
def test_backend_cannot_silently_drop_required_routing_voice_or_control_fields(field):
    calls = []
    def legacy_session():
        calls.append(1)
    with pytest.raises(TypeError, match=field):
        call_tts_hook(legacy_session, **{field: "required"})
    assert not calls


def test_async_hook_internal_error_is_not_reexecuted():
    calls = []
    async def prewarm():
        calls.append(1)
        raise TypeError("预热内部错误")
    with pytest.raises(TypeError, match="预热内部错误"):
        asyncio.run(call_tts_hook(prewarm, turn_id="turn"))
    assert calls == [1]


def test_adapting_a_dynamic_backend_does_not_retain_its_closed_over_resources():
    def create_backend():
        audio = bytearray(1024)
        class Backend:
            def session(self):
                return len(audio)
        return Backend()
    backend = create_backend()
    implementation = weakref.ref(backend.session.__func__)
    assert call_tts_hook(backend.session, fresh=True) == 1024
    del backend
    gc.collect()
    assert implementation() is None
