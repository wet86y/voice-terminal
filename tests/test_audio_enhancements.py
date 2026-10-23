"""音频增强契约：背景不唤醒、请求快照确认和多片段精修归属。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from functools import wraps

def async_test(function):
    @wraps(function)
    def run():
        return asyncio.run(function())
    return run

from voice_terminal.audio_enhancements import AcousticMailbox, AudioEnhancements
from voice_terminal.controller.enhancements import AudioEnhancementsMixin
from voice_terminal.controller.turn_workspace import AudioFrame, TurnWorkspace
from voice_terminal.asr.worker import AsrWorkerEngine
from voice_terminal.config import AppConfig
from unittest.mock import patch


def test_migration_and_independent_flags():
    config = AppConfig.from_mapping({"smart_turn": {"sensevoice": False},
                                    "compute": {"devices": {"sensevoice": "cuda"}}}, base_dir=Path.cwd())
    assert config.audio_enhancements == AudioEnhancements(False, False, False)
    assert config.compute.devices["emotion"] == "cuda"
    assert config.smart_turn.sensevoice is False
    assert AppConfig.from_mapping({}, base_dir=Path.cwd()).audio_enhancements == AudioEnhancements()
    assert AudioEnhancements.from_mapping({"emotion": False}).snapshot() == {
        "dual_pass": True, "emotion": False, "acoustic_events": True}
    with pytest.raises(ValueError):
        AudioEnhancements.from_mapping({"emotion": 1})


def test_mailbox_continuity_silence_and_snapshot_growth():
    mailbox = AcousticMailbox()
    mailbox.append(["Dog"], 0, 15600)
    frozen = mailbox.snapshot()
    mailbox.append(["Dog"], 7680, 23280)
    mailbox.append(["Bell"], 15360, 30960)
    mailbox.consume(frozen)
    assert [(row["labels"], row["start_sample"], row["end_sample"]) for row in mailbox.snapshot()] == [
        (["Dog"], 0, 23280), (["Bell"], 15360, 30960)]
    mailbox.append([], 23040, 38640)
    mailbox.append(["Bell"], 30720, 46320)
    assert len(mailbox.snapshot()) == 3
    before_reset = mailbox.snapshot()
    mailbox.clear()
    assert mailbox.valid(before_reset) == []


class Controller(AudioEnhancementsMixin):
    def __init__(self):
        self._init_enhancements(AudioEnhancements())
        self.speaker_mode = "off"
        self.analysis = SimpleNamespace(analyze=AsyncMock(return_value={"result": {"label": "平静"}}))
        self._refinement_completed = AsyncMock()
        self._queue_asr = lambda row: self.queued.append(row)
        self.queued = []


@async_test
async def test_refinement_out_of_order_accumulates_only_offline_segments():
    controller = Controller()
    ws = TurnWorkspace("first")
    controller._enhancement_streams["first/asr/1"] = {
        "workspace": ws, "text": "甲乙尾", "epoch": 0, "sequence": 1,
        "segments": {"a": {"start_sample": 0, "end_sample": 16000, "text": None},
                     "b": {"start_sample": 16000, "end_sample": 32000, "text": None}}}
    controller.workspace = TurnWorkspace("second")
    async def result(segment, start, end, text, first, last):
        await controller._enhancement_result({"type": "asr.refined", "turn_id": "first/asr/1",
            "segment_id": segment, "start_sample": start, "end_sample": end, "text": text,
            "start_char": first, "end_char": last})
        await asyncio.gather(*tuple(controller._refinement_tasks))
    await result("b", 16000, 32000, "二", 1, 2)
    assert controller._refinement_completed.await_args.args == (ws, {
        "text": "二", "source": "paraformer_offline", "source_revision": 1})
    await result("a", 0, 16000, "一", 0, 1)
    assert controller._refinement_completed.await_args.args[1]["text"] == "一二"
    await result("a", 0, 16000, "重复", 0, 1)
    assert controller._refinement_completed.await_count == 2
    await controller._stop_enhancements()
    await result("b", 16000, 32000, "失效", 1, 2)
    assert controller._refinement_completed.await_count == 2


@async_test
async def test_emotion_latest_only_and_old_speaker_result_is_discarded():
    controller = Controller()
    gate = asyncio.Event()
    async def infer(*args, **kwargs):
        await gate.wait()
        return {"result": {"label": "快乐"}}
    controller.analysis.analyze.side_effect = infer
    controller._submit_perception("emotion", b"\0\0" * 16000, 0)
    await asyncio.sleep(0)
    for start in range(1, 20):
        controller._submit_perception("emotion", b"\0\0" * 16000, start)
    assert len(controller._perception_pending) == 1
    controller.perception["speaker_scope"] += 1
    gate.set()
    await controller._perception_tasks["emotion"]
    assert controller.analysis.analyze.await_count == 2
    assert controller.perception["voice_emotion"] is None
    assert not controller.queued


@async_test
async def test_vad_segment_closure_is_separate_from_business_final():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("input")
    ws.audio.append(AudioFrame(b"\0\0" * 32000, 0, 0))
    controller._sub_id, controller._sub_sequence = "input/asr/1", 1
    controller._register_enhancement_stream()
    controller._enhancement_start, controller._speech_end_sample = 1000, 30000
    controller._close_acoustic_segment(ws)
    assert len(controller.queued) == 1
    segment = controller.queued[0]
    assert not segment.is_final and segment.to_dict()["type"] == "audio.segment"
    assert len(segment.pcm) == 58000
    assert ws.phase == "CAPTURING"
    controller._close_acoustic_segment(ws)
    assert len(controller.queued) == 1


@async_test
async def test_streaming_cache_is_retained_until_business_final():
    engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")
    from voice_terminal.asr.streaming_model import StreamingParaformer
    import numpy as np
    model = object.__new__(StreamingParaformer)
    model._np = np
    model._cache_by_turn = {}
    model.chunk_size = engine.chunk_size
    model.encoder_chunk_look_back = engine.encoder_chunk_look_back
    model.decoder_chunk_look_back = engine.decoder_chunk_look_back
    model.model = SimpleNamespace(generate=lambda **kwargs: [{"text": "尾" if kwargs["is_final"] else "字"}])
    engine._infer_sync = model.infer
    try:
        await engine.infer("turn", b"\0" * engine.chunk_stride_bytes, False)
        assert engine._transcript_by_turn["turn"] == "字"
        assert "turn" in model._cache_by_turn
        await engine.infer("turn", b"\0" * engine.chunk_stride_bytes, False)
        assert engine._transcript_by_turn["turn"] == "字字"
        assert "turn" in engine._transcript_by_turn
        result = await engine.infer("turn", b"", True)
        assert result.text == "字字尾"
        assert "turn" not in engine._transcript_by_turn
        assert "turn" not in model._cache_by_turn
    finally:
        engine.close()


@async_test
async def test_acoustic_mailbox_is_consumed_without_shadow_and_emotion_never_wakes():
    from tests.test_secondary_mailbox import CapturingProvider, request_snapshot, wait_for_calls
    from tests.test_scheduler_staircase import _speaking_runtime, _finish_blocking_request
    provider = CapturingProvider()
    runtime = _speaking_runtime(provider)
    mailbox = AcousticMailbox()
    runtime.perception = {"voice_emotion": "平静", "acoustic": mailbox, "scope": 0}
    runtime.ledger.set_shadow_runtime_state(available=False, active=False)
    runtime.scheduler.start_heartbeat(0.0)
    runtime.scheduler.arm(0.0, turn_id="turn-a")
    await runtime._on_heartbeat(0.4)
    assert provider.calls == 0
    mailbox.append(["Dog"], 0, 15600)
    await runtime._on_heartbeat(4.0)
    await wait_for_calls(provider, 1)
    state = request_snapshot(provider.messages[-1])
    assert state["runtime"]["voice_emotion"] == "平静"
    assert state["runtime"]["acoustic_events"] == [{"labels": ["Dog"], "start_ms": 0, "end_ms": 975}]
    assert "progress" not in state.get("shadow", {})
    assert mailbox.snapshot() == []
    await _finish_blocking_request(runtime, provider)


@async_test
async def test_acoustic_audit_failure_and_retry_snapshot_preserve_new_observations():
    from tests.test_scheduler_staircase import _speaking_runtime, _BlockingProvider
    runtime = _speaking_runtime(_BlockingProvider())
    mailbox = AcousticMailbox()
    runtime.perception = {"voice_emotion": None, "acoustic": mailbox, "scope": 0}
    mailbox.append(["Dog"], 0, 15600)
    with patch("voice_terminal.agent.episode.capture_context_snapshot", side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError, match="audit failed"):
            await runtime._step((runtime.episode_id, "request"), {"USER_INPUT_FINALIZED"})
    captured = runtime._secondary_snapshot()
    assert mailbox.snapshot()
    mailbox.append(["Bell"], 7680, 23280)
    runtime._retry_secondary_by_turn["turn-a"] = captured
    retry = runtime._request_secondary_context("turn-a", {"LLM_RETRY"})
    assert runtime._acoustic_context(retry)[0]["labels"] == ["Dog"]
    runtime._consume_secondary(retry, prompt_kind="normal")
    assert [row["labels"] for row in mailbox.snapshot()] == [["Bell"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "load", "save", "rollback"])
async def test_dual_switch_keeps_primary_running_and_saves_only_verified_state(tmp_path, monkeypatch, failure):
    from dataclasses import replace
    from voice_terminal.server.settings_runtime import ProductSettingsRuntimeMixin
    monkeypatch.setattr("voice_terminal.application.resource_readiness.inspect_model_resources", lambda *_: {})
    calls = []
    class Runtime(ProductSettingsRuntimeMixin):
        async def start_listening(self):
            calls.append("resume")
            self.state.listening = True
        def snapshot(self):
            return {}
    runtime = Runtime()
    runtime._project_root = tmp_path
    runtime.config = replace(AppConfig(), audio_enhancements=AudioEnhancements(False, False, False))
    runtime._session_lock = asyncio.Lock()
    runtime.state = SimpleNamespace(listening=True)
    async def stop():
        calls.append("stop")
        runtime.state.listening = False
    async def configure(enabled):
        assert not runtime._enhancement_switching  # 纯双路切换不能挂起新的监听请求。
        calls.append(enabled)
        if failure in {"load", "rollback"} and enabled or failure == "rollback":
            raise RuntimeError("加载失败")
        return {"device": "cpu", "offline": {"device": "cpu", "status": "READY" if enabled else "DISABLED"}}
    def save(value):
        calls.append("save")
        if failure == "save":
            raise OSError("保存失败")
    runtime.controller = SimpleNamespace(stop_listening=stop, perception={"acoustic": AcousticMailbox()},
                                         _invalidate_dual_refinements=AsyncMock())
    runtime.asr = SimpleNamespace(configure_enhancements=configure)
    runtime.bus = SimpleNamespace(publish=AsyncMock())
    runtime.product_settings = SimpleNamespace(save_audio_enhancements=save)
    if failure:
        with pytest.raises((RuntimeError, OSError)):
            await runtime.switch_audio_enhancements({"dual_pass": True})
        assert runtime.config.audio_enhancements.dual_pass is False
        assert runtime.state.listening
    else:
        await runtime.switch_audio_enhancements({"dual_pass": True})
        assert runtime.config.audio_enhancements.dual_pass is True
        assert runtime.state.listening
        assert calls == [True, "save"]
    assert calls[0] is True
    assert "stop" not in calls and "resume" not in calls


@pytest.mark.asyncio
async def test_user_stop_during_switch_revokes_resume(tmp_path, monkeypatch):
    from dataclasses import replace
    from voice_terminal.server.settings_runtime import ProductSettingsRuntimeMixin
    monkeypatch.setattr("voice_terminal.application.resource_readiness.inspect_model_resources", lambda *_: {})
    class Runtime(ProductSettingsRuntimeMixin):
        start_listening = AsyncMock()
        def snapshot(self):
            return {}
    runtime = Runtime()
    runtime._project_root = tmp_path
    runtime.config = replace(AppConfig(), audio_enhancements=AudioEnhancements(False, False, False))
    runtime._session_lock = asyncio.Lock()
    runtime.state = SimpleNamespace(listening=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def configure(enabled):
        entered.set()
        await release.wait()
        return {"device": "cpu", "offline": {"device": "cpu", "status": "READY"}}
    runtime.asr = SimpleNamespace(configure_enhancements=configure)
    runtime.controller = SimpleNamespace(stop_listening=AsyncMock(), perception={"acoustic": AcousticMailbox()},
                                         _invalidate_dual_refinements=AsyncMock())
    runtime.bus = SimpleNamespace(publish=AsyncMock())
    runtime.product_settings = None
    task = asyncio.create_task(runtime.switch_audio_enhancements({"dual_pass": True}))
    await entered.wait()
    runtime._enhancement_resume_requested = False
    release.set()
    await task
    runtime.start_listening.assert_not_called()


def test_retirement_removes_only_verified_managed_files(tmp_path, monkeypatch):
    import hashlib
    import json
    from scripts import retire_sensevoice as migration
    from voice_terminal.distribution import manifest
    models = tmp_path / "models"
    old = models / "analysis/sensevoice"
    old.mkdir(parents=True)
    weight = old / "model.pt"
    weight.write_bytes(b"old managed weight")
    (models / "manifest.json").write_text(json.dumps(manifest(models, kind="models", build="test")), "utf-8")
    unknown = old / "user-note.txt"
    unknown.write_text("保留未受管文件", "utf-8")
    monkeypatch.setattr(migration, "OLD_WEIGHT_SHA256", hashlib.sha256(weight.read_bytes()).hexdigest())
    monkeypatch.setattr(migration, "verify_snapshot", lambda *_: {})
    preview = migration.retire(models)
    assert weight.exists() and unknown.exists()
    assert len(preview["retained"]) == 1
    result = migration.retire(models, apply=True)
    assert not weight.exists() and unknown.exists()
    assert result["removed"] == ["analysis/sensevoice/model.pt"]


@pytest.mark.asyncio
async def test_segment_admission_does_not_wait_for_unrelated_pending_tail():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("voice", speaker_mode="filter")
    controller.speaker_policy = SimpleNamespace(pieces=[
        SimpleNamespace(turn_id="voice", start=0, end=16000, revision=1, status="admitted", text="甲"),
        SimpleNamespace(turn_id="voice", start=16000, end=32000, revision=2, status="pending", text="乙"),
    ], fully_admitted=lambda _: False)
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "text": "甲乙", "sequence": 1,
        "epoch": 0, "segments": {"a": {"start_sample": 0, "end_sample": 16000, "text": "一", "start_char": 0, "end_char": 1}}}
    await controller._publish_segment_refinements(ws)
    payload = controller._refinement_completed.await_args.args[1]
    assert payload["text"] == "一" and payload["admission_checked"]
    controller.speaker_policy.pieces[0].status = "rejected"
    controller._refinement_completed.reset_mock()
    await controller._publish_segment_refinements(ws)
    controller._refinement_completed.assert_not_called()


@pytest.mark.asyncio
async def test_refinement_requires_admission_covering_the_entire_audio_segment():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("voice", speaker_mode="filter")
    controller.speaker_policy = SimpleNamespace(pieces=[
        SimpleNamespace(turn_id="voice", start=0, end=8000, revision=1, status="admitted", text="已知"),
    ])
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "sequence": 1, "epoch": 0,
        "segments": {"a": {"start_sample": 0, "end_sample": 16000, "text": "完整离线正文"}}}
    await controller._publish_segment_refinements(ws)
    controller._refinement_completed.assert_not_called()
    controller.speaker_policy.pieces.append(SimpleNamespace(turn_id="voice", start=8000, end=16000,
                                                            revision=2, status="admitted", text="后半段"))
    await controller._publish_segment_refinements(ws)
    assert controller._refinement_completed.await_args.args[1]["text"] == "完整离线正文"


@pytest.mark.asyncio
async def test_slow_punctuation_rechecks_continuation_and_latest_segment_snapshot():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("voice")
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "sequence": 1, "epoch": 0,
        "segments": {"a": {"start_sample": 0, "end_sample": 16000, "text": "前段"}}}
    entered, release = asyncio.Event(), asyncio.Event()
    async def punctuate(text, *, final):
        entered.set()
        await release.wait()
        return {"text": text + ("。" if final else ""), "punctuation_status": "READY"}
    controller.asr = SimpleNamespace(punctuate_text=punctuate)
    ws._refinement_input_finished = True
    task = asyncio.create_task(controller._publish_segment_refinements(ws))
    await entered.wait()
    ws._refinement_input_finished = False
    controller._enhancement_streams["voice/asr/1"]["segments"]["b"] = {
        "start_sample": 16000, "end_sample": 32000, "text": "后段"}
    release.set()
    await task
    await asyncio.gather(*tuple(controller._refinement_tasks))
    assert controller._refinement_completed.await_count == 1
    assert controller._refinement_completed.await_args.args[1]["text"] == "前段后段"


@pytest.mark.asyncio
async def test_slow_punctuation_includes_new_continuation_substream():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("voice")
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "sequence": 1, "epoch": 0,
        "segments": {"a": {"start_sample": 0, "end_sample": 16000, "text": "前段"}}}
    entered, release = asyncio.Event(), asyncio.Event()
    async def punctuate(text, *, final):
        entered.set()
        await release.wait()
        return {"text": text, "punctuation_status": "READY"}
    controller.asr = SimpleNamespace(punctuate_text=punctuate)
    task = asyncio.create_task(controller._publish_segment_refinements(ws))
    await entered.wait()
    controller._enhancement_streams["voice/asr/2"] = {"workspace": ws, "sequence": 2, "epoch": 0,
        "segments": {"b": {"start_sample": 16000, "end_sample": 32000, "text": "续说"}}}
    release.set()
    await task
    await asyncio.gather(*tuple(controller._refinement_tasks))
    assert controller._refinement_completed.await_count == 1
    assert controller._refinement_completed.await_args.args[1]["text"] == "前段续说"


def test_retirement_preserves_pending_admission_from_previous_substream():
    controller = Controller()
    ws = TurnWorkspace("voice", speaker_mode="filter")
    piece = SimpleNamespace(turn_id="voice", start=0, end=16000, status="pending")
    ws._refinement_speaker_pieces = [piece]
    controller.speaker_policy = SimpleNamespace(pieces=[])
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "epoch": 0,
        "segments": {"a": {"text": "前段"}}}
    controller._retire_enhancement_streams(ws)
    assert "voice/asr/1" in controller._enhancement_streams
    piece.status = "admitted"
    controller._retire_enhancement_streams(ws)
    assert not controller._enhancement_streams


@pytest.mark.asyncio
async def test_stale_segment_revision_cannot_replace_or_fail_current_segment():
    controller = Controller()
    ws = controller.workspace = TurnWorkspace("voice")
    segment = {"start_sample": 0, "end_sample": 16000, "revision": 2, "text": None}
    controller._enhancement_streams["voice/asr/1"] = {"workspace": ws, "epoch": 0, "segments": {"a": segment}}
    base = {"turn_id": "voice/asr/1", "segment_id": "a", "revision": 1,
            "start_sample": 0, "end_sample": 16000, "text": "旧回包"}
    await controller._enhancement_result({**base, "type": "asr.refined"})
    await controller._enhancement_result({**base, "type": "asr.refinement_failed"})
    assert segment["text"] is None and not segment.get("failed")


@pytest.mark.asyncio
async def test_business_commit_keeps_previously_admitted_refinement():
    from voice_terminal.controller.episode_controller import EpisodeController
    controller = object.__new__(EpisodeController)
    ws = controller.workspace = TurnWorkspace("voice", speaker_mode="filter")
    ws.final_text, ws.refined_text, ws.refinement_available = "甲乙", "一", True
    ws._published_refinement = ("paraformer_offline", 1, "一")
    ws.transcript_updates = [{"kind": "asr_final", "text": "甲乙"},
                             {"kind": "refinement", "text": "一"}]
    controller.speaker_policy = SimpleNamespace(pieces=[
        SimpleNamespace(turn_id="voice", status="admitted"),
        SimpleNamespace(turn_id="voice", status="rejected")],
        admitted_text=lambda _: "甲", fully_admitted=lambda _: False)
    controller._speaker_decision = AsyncMock(return_value={})
    controller._flush_speaker_admissions = AsyncMock()
    controller.hotwords = None
    controller.state = SimpleNamespace(current={})
    controller.episode = SimpleNamespace(observe=AsyncMock(), episode_id="session")
    controller.episode_history = {}
    controller._commit_input_workspace = AsyncMock()
    controller._retire_committed_input = AsyncMock()
    assert await controller._input_committed(ws, ws.token)
    assert ws.final_text == "甲" and ws.refined_text == "一" and ws.refinement_available
    assert [row["text"] for row in ws.transcript_updates if row["kind"] == "refinement"] == ["一"]
    assert all(row["text"] != "甲乙" for row in ws.transcript_updates)


def test_ended_session_releases_workspace_even_with_pending_native_result():
    controller = Controller()
    ws = TurnWorkspace("expired")
    controller._enhancement_streams["expired/asr/1"] = {"workspace": ws, "epoch": 0,
        "session_scope": 0, "segments": {"pending": {"text": None}}}
    controller.perception["scope"] += 1
    controller._retire_enhancement_streams(ws)
    assert not controller._enhancement_streams


@pytest.mark.asyncio
async def test_normal_request_consumes_before_four_seconds_and_preserves_new_audio():
    from tests.test_secondary_mailbox import CapturingProvider, request_snapshot, wait_for_calls
    from tests.test_scheduler_staircase import _speaking_runtime, _finish_blocking_request
    provider = CapturingProvider()
    runtime = _speaking_runtime(provider)
    mailbox = AcousticMailbox()
    runtime.perception = {"voice_emotion": None, "acoustic": mailbox, "scope": 0}
    mailbox.append(["Dog"], 0, 15600)
    assert provider.calls == 0
    assert await runtime._start_dispatch({"USER_INPUT_FINALIZED"})
    await wait_for_calls(provider, 1)
    assert request_snapshot(provider.messages[-1])["runtime"]["acoustic_events"][0]["labels"] == ["Dog"]
    assert mailbox.snapshot() == []
    mailbox.append(["Bell"], 7680, 23280)
    await _finish_blocking_request(runtime, provider)
    assert [row["labels"] for row in mailbox.snapshot()] == [["Bell"]]
