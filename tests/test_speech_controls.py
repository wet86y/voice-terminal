"""语音表现协议、实际音频时钟与任务滚动的回归。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from voice_terminal.agent.decision_envelope import SpeechDecisionEnvelopeStream
from voice_terminal.agent.decision_schema import decision_schema
from voice_terminal.audio.speech_controls import PcmControlProcessor
from voice_terminal.tts.controls import DEFAULT_CONTROLS, SpeechControls, SpeechControlError, merge_controls, profile_for
from voice_terminal.tts.controlled_session import ControlledTtsSession
from voice_terminal.tts.scheduling import TtsSegmentReceipt


@pytest.mark.parametrize("model,kind,native", [
    ("speech-2.8-hd", "enum", True), ("speech-2.8-turbo", "enum", True),
    ("voxcpm2", "description", False), ("voxcpm1", "none", False),
])
def test_registered_models_generate_only_executable_parameters(model, kind, native):
    client = SimpleNamespace(model=model) if native else SimpleNamespace(local_model=model)
    profile = profile_for(client, local_tempo=True)
    assert profile.guidance_kind == kind and profile.native_speed == native
    assert ("guidance" in profile.names) == (kind != "none")
    if model == "voxcpm1":
        assert "guidance" not in profile.protocol() and "风格引导" not in profile.protocol()
    assert "长文本" in profile.protocol() and "无调整需求时不要改变音量" in profile.protocol()
    disabled = profile_for(client, enabled=False, local_tempo=True)
    assert disabled.names == () and disabled.normalize(SpeechControls("happy", 1.2, 1.4)).is_default


def test_missing_tempo_hides_only_unavailable_speed():
    profile = profile_for(SimpleNamespace(local_model="voxcpm2"), local_tempo=False)
    assert profile.names == ("guidance", "volume")


def test_patch_inheritance_partial_reset_and_atomic_validation():
    profile = profile_for(SimpleNamespace(model="speech-2.8-hd"))
    first = merge_controls(profile, DEFAULT_CONTROLS, [
        {"name": "guidance", "value": "happy"}, {"name": "speed", "value": "0.8"},
    ])
    assert merge_controls(profile, first, []) == first
    assert merge_controls(profile, first, [{"name": "volume", "value": "1.4"}]) == SpeechControls("happy", .8, 1.4)
    reset = merge_controls(profile, first, [{"name": "guidance", "value": "default"}])
    assert reset == SpeechControls(None, .8, 1)
    for patch in (
        [{"name": "speed", "value": "nan"}], [{"name": "volume", "value": "2"}],
        [{"name": "guidance", "value": "whisper"}],
        [{"name": "speed", "value": "1.2"}, {"name": "speed", "value": ".8"}],
        [{"name": "guidance", "value": "happy"}, {"name": "volume", "value": "bad"}],
    ):
        with pytest.raises(SpeechControlError):
            merge_controls(profile, first, patch)
        assert first == SpeechControls("happy", .8, 1)


def test_strict_schema_is_stable_and_controls_name_is_not_a_dynamic_enum():
    speak = decision_schema([])["properties"]["speak"]
    assert speak["required"] == ["state", "controls", "text"]
    assert "emotion" not in speak["properties"]
    assert speak["properties"]["controls"]["items"]["properties"]["name"] == {"type": "string"}


def test_stream_waits_for_complete_controls_even_when_text_arrives_first():
    parser = SpeechDecisionEnvelopeStream(require_controls=True)
    assert parser.feed('{"speak":{"state":"speak","text":"前面的正文。",') == ""
    assert parser.feed('"controls":[{"name":"speed","value":"0.8"}') == ""
    assert parser.feed(']},"actions":[]}') == "前面的正文。"
    assert parser.speak_prefix().controls == ({"name": "speed", "value": "0.8"},)
    assert parser.finish().speak.text == "前面的正文。"


def test_stream_control_descriptions_cannot_spoof_text_or_state_members():
    payload = {"speak": {"state": "speak", "controls": [
        {"name": "guidance", "value": '温柔，描述中有"text":"错误正文"和"state":"cancel"'},
    ], "text": "真实正文。"}, "actions": []}
    parser = SpeechDecisionEnvelopeStream(require_controls=True)
    spoken = "".join(parser.feed(char) for char in json.dumps(payload, ensure_ascii=False))
    assert spoken == "真实正文。"
    assert parser.finish().speak.state == "speak"


def test_legacy_emotion_still_parses_without_becoming_new_default_patch():
    parser = SpeechDecisionEnvelopeStream()
    parser.feed('{"speak":{"state":"speak","emotion":"happy","text":"旧格式。"},"actions":[]}')
    assert parser.finish().speak.emotion == "happy"
    assert parser.finish().speak.controls is None
    new = SpeechDecisionEnvelopeStream(require_controls=True)
    new.feed('{"speak":{"state":"speak","controls":[],"text":"新格式。"},"actions":[]}')
    assert new.finish().speak.controls == ()


def test_gain_uses_real_output_frames_and_saturates_without_integer_wrap():
    source = np.array([-30000, -1000, 0, 1000, 30000], dtype="<i2").tobytes()
    processor = PcmControlProcessor(16000, volume=1.4)
    actual = processor.push(source[:4]) + processor.push(source[4:]) + processor.finish()
    assert np.frombuffer(actual, dtype="<i2").tolist() == [-32768, -1400, 0, 1400, 32767]
    assert processor.span().input_end == processor.span().output_end == 5
    neutral = PcmControlProcessor(16000)
    assert neutral.push(source) + neutral.finish() == source


def test_buffered_tempo_flush_is_included_in_exact_output_boundary():
    class BufferedTempo:
        def __init__(self, *args, **kwargs):
            self.pending = b""
        def push(self, pcm):
            self.pending += pcm
            return b""
        def finish(self):
            return self.pending + self.pending[:4]
        def close(self):
            pass
    processor = PcmControlProcessor(16000, speed=.8, tempo_factory=BufferedTempo)
    assert processor.push(b"\x01\0" * 4) == b""
    assert processor.push(b"\x02\0" * 4) == b""
    assert len(processor.finish()) == 20
    assert processor.span(input_start=100, output_start=200).output_end == 210
    assert processor.span().input_end == 8


def test_online_control_rollover_preserves_pcm_receipts_and_subtitle_origins():
    class Session:
        bidi = False
        reusable = False
        def __init__(self, values):
            self.controls = values
            self.received_bytes = self.completed_segments = self.completed_sentences = 0
            self.alignment_events = []
            self.queue = asyncio.Queue()
            self.receipts = []
        async def start(self):
            pass
        async def send_text(self, text):
            self.received_bytes += 3200
            self.receipts.append(TtsSegmentReceipt(self.completed_segments, len(text), self.received_bytes))
            self.completed_segments += 1
            self.completed_sentences += 1
            self.alignment_events.append({"audio_base_ms": 0, "sentence_index": 0,
                                          "value": {"text": text, "start_ms": 0, "end_ms": 100}})
            await self.queue.put(b"\x01\0" * 1600)
        async def wait_for_segment(self, index):
            return self.receipts[index]
        async def finish(self):
            await self.queue.put(None)
        async def close(self):
            pass
        async def audio_chunks(self):
            while (item := await self.queue.get()) is not None:
                yield item

    async def scenario():
        values = DEFAULT_CONTROLS
        sessions = []
        def factory(control, fresh):
            raw = Session(control)
            sessions.append(raw)
            return raw
        client = SimpleNamespace(sample_rate=16000, prepare_speech_controls=AsyncMock())
        async def start(raw):
            await raw.start()
            return raw
        client.start_session_with_retry = start
        group = ControlledTtsSession(client, factory(values, False), lambda: values, factory)
        async def collect():
            return b"".join([pcm async for pcm in group.audio_chunks()])
        reader = asyncio.create_task(collect())
        await group.start()
        await group.send_text("甲")
        assert (await group.wait_for_segment(0)).end_received_bytes == 3200
        values = SpeechControls("happy", 1.2, .6)
        await group.send_text("乙")
        assert (await group.wait_for_segment(1)).end_received_bytes == 6400
        await group.finish()
        assert len(await reader) == group.received_bytes == 6400
        assert [event["audio_base_ms"] for event in group.alignment_events] == [0, 100]
        assert [event["sentence_index"] for event in group.alignment_events] == [0, 1]
        assert sessions[1].controls == values
        await group.close()
    asyncio.run(scenario())


def test_online_alignment_cursor_survives_bounded_provider_history():
    from collections import deque
    raw = SimpleNamespace(controls=DEFAULT_CONTROLS, alignment_events=deque(maxlen=512), alignment_event_count=0)
    group = ControlledTtsSession(SimpleNamespace(sample_rate=16000), raw, lambda: DEFAULT_CONTROLS, None)
    for index in range(600):
        raw.alignment_events.append({"sentence_index": index, "audio_base_ms": 0})
        raw.alignment_event_count += 1
    assert group.alignment_events[-1]["sentence_index"] == 599
    raw.alignment_events.append({"sentence_index": 600, "audio_base_ms": 0})
    raw.alignment_event_count += 1
    assert len(group.alignment_events) == 513
    assert group.alignment_events[-1]["sentence_index"] == 600


@pytest.mark.parametrize("invalid", [None, 12, True, "invalid", {"name": "speed", "value": "1.2"}])
def test_invalid_optional_controls_keep_valid_text_and_cancel_parseable(invalid):
    for state, text in (("speak", "合法正文"), ("cancel", "")):
        parser = SpeechDecisionEnvelopeStream(require_controls=True)
        parser.feed(json.dumps({"speak": {"state": state, "controls": invalid, "text": text}, "actions": []}))
        decision = parser.finish().speak
        assert decision.state == state and decision.text == text
        with pytest.raises(SpeechControlError):
            merge_controls(profile_for(SimpleNamespace(model="speech-2.8-hd")), DEFAULT_CONTROLS, decision.controls)


def test_own_kws_resume_refresh_accepts_current_patch_without_reviving_stale_patch():
    from tests.test_speech_continuation import ContinuationHarness
    async def scenario():
        for stale in (False, True):
            harness = ContinuationHarness()
            harness.tts.model = "speech-2.8-hd"
            output = harness.output
            epoch = output.speech_control_epoch
            if stale:
                output.refresh_speech_controls("settings_changed")
            async def resume():
                previous = output.speech_control_epoch
                output.refresh_speech_controls("listening_resumed")
                return previous, output.speech_control_epoch
            output.controller._on_model_speech_accepted = resume
            try:
                handle = await output.begin(harness.identity, harness.turn_id, controls=({"name": "speed", "value": "0.8"},),
                                            control_epoch=epoch, control_profile=output.speech_control_profile.identity)
                assert handle.speech_controls.speed == (1 if stale else .8)
            finally:
                await harness.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_mode", ["agent", "off"])
def test_minimax_explicit_controls_own_emotion_injection(legacy_mode):
    from voice_terminal.providers.minimax_tts import MiniMaxTtsClient
    async def scenario():
        client = MiniMaxTtsClient(base_url="https://api.minimaxi.com", websocket_path="/ws/v1/t2a_v2",
                                 model="speech-2.8-hd", api_key="test-only", voice_id="test-voice",
                                 emotion_mode=legacy_mode)
        await client.prepare_speech_controls(DEFAULT_CONTROLS)
        assert "emotion" not in client.start_payload(controls=DEFAULT_CONTROLS)["voice_setting"]
        values = SpeechControls("happy", .8, .6)
        await client.prepare_speech_controls(values)
        setting = client.start_payload(controls=values)["voice_setting"]
        assert (setting["emotion"], setting["speed"], setting["vol"]) == ("happy", .8, .6)
        await client.close()
    asyncio.run(scenario())


def test_local_formal_guidance_and_gain_are_frozen_per_new_synthesis():
    from tests.test_audio_cpp_tts import _audio_event, _event, _fake_client, _response
    async def scenario():
        values = [SpeechControls("轻柔、安抚", volume=.6)]
        requests = []
        source = np.full(1600, 1000, dtype="<i2").tobytes()
        def handler(request):
            requests.append(json.loads(request.content))
            # 模拟已提交请求进行期间收到下一次LLM（大语言模型）参数校准。
            values[0] = SpeechControls("明快、带笑意", volume=1.4)
            return _response(_audio_event(source), _event("speech.audio.done"), "data: [DONE]\n\n")
        client = _fake_client(handler, native_sample_rate=48000)
        session = client.session()
        session.speech_control_reader = lambda: values[0]
        await session.start()
        await session.send_text("第一句。")
        first = await session.wait_for_segment(0)
        await session.send_text("第二句。")
        second = await session.wait_for_segment(1)
        await session.finish()
        pcm = b"".join([chunk async for chunk in session.audio_chunks()])
        assert requests[0]["input"] == "(轻柔、安抚)第一句。"
        assert requests[1]["input"] == "(明快、带笑意)第二句。"
        assert (first.text_chars, second.text_chars) == (4, 4)
        assert first.end_received_bytes == len(source)
        assert second.end_received_bytes == session.received_bytes == len(pcm)
        assert np.frombuffer(pcm[:len(source)], dtype="<i2").tolist() == [600] * 1600
        assert np.frombuffer(pcm[len(source):], dtype="<i2").tolist() == [1400] * 1600
        assert session.audio_transform_spans[-1].output_end * 2 == len(pcm)
        assert session.voice_snapshot.style_description == ""
        await session.close()
        await client.close()
    asyncio.run(scenario())


def test_product_switch_defaults_persists_false_and_rejects_non_boolean(tmp_path):
    import httpx
    from voice_terminal.config import AppConfig
    from voice_terminal.server.http_api import create_app
    from voice_terminal.server.product import ProductSettingsStore
    from voice_terminal.server.websocket import SessionRuntime
    async def scenario():
        path = tmp_path / "data/config/product-settings.json"
        config = AppConfig()
        store = ProductSettingsStore(path=path, config=config)
        assert store.settings_only()["tts"]["speech_control_enabled"] is True
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)),
                                     base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/tts", json={"speech_control_enabled": False})
            assert response.status_code == 200, response.text
            assert response.json()["effective_from"] == "next_synthesis"
            assert runtime.config.tts.speech_control_enabled is False
            assert ProductSettingsStore(path=path, config=config).settings_only()["tts"]["speech_control_enabled"] is False
            assert (await client.put("/api/v1/product/settings/tts", json={"speech_control_enabled": "false"})).status_code == 422
    asyncio.run(scenario())


def test_queue_patch_continuation_summary_and_refresh_share_the_same_lifecycle():
    from tests.test_speech_continuation import ContinuationHarness, SUFFIX_TEXT, _agent
    from tests.test_speech_continuation_window import complete
    async def scenario():
        harness = ContinuationHarness()
        harness.tts.model = "speech-2.8-hd"
        runtime = _agent(harness)
        try:
            first = await harness.first()
            output = harness.output
            output._set_handle_controls(first, [{"name": "speed", "value": "0.8"}], base=DEFAULT_CONTROLS)
            await complete(harness, first)
            assert "当前播报调节：语速=0.8" in output.speech_control_context()
            snapshot = dict(output.speak_snapshot)
            changed = await runtime._apply_revision_tail(harness.identity, harness.turn_id, snapshot,
                                                         SUFFIX_TEXT, emotion="calm",
                                                         controls=({"name": "volume", "value": "0.6"},))
            assert changed is first
            successor = await harness.wait_for_drain()
            assert successor.speech_controls == SpeechControls(None, .8, .6)
            old_epoch = output.speech_control_epoch
            output.refresh_speech_controls("kws_standby")
            assert output.speech_control_epoch == old_epoch + 1
            assert successor.speech_controls.is_default
            assert "当前播报调节" not in output.speech_control_context()
            output._set_handle_controls(successor, [{"name": "volume", "value": "1.4"}], expected_epoch=old_epoch)
            assert successor.speech_controls.is_default
            harness.tts.speech_control_enabled = False
            output.refresh_speech_controls("settings_changed")
            assert "guidance" not in output.speech_control_context()
            assert "speed" not in output.speech_control_context()
        finally:
            await harness.close()
    asyncio.run(scenario())
