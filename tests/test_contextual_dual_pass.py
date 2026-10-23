"""双路隔离、参考转写及原生热词的执行契约。"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.asr.dual_pass import OfflineParaformer
from voice_terminal.asr.hotwords import DEFAULT_HOTWORDS, HotwordService, parse_hotword_text, validate_hotwords
from voice_terminal.asr.protocol import AudioChunk, AudioSegment
from voice_terminal.asr.text import normalize_asr_text, join_asr_segments
from voice_terminal.asr.worker import AsrWorkerEngine, handle_asr_connection


@pytest.mark.asyncio
async def test_loading_lane_preserves_segments_until_model_is_ready(tmp_path, monkeypatch):
    import threading
    from voice_terminal.asr.dual_pass import DualPassLane
    entered, release = threading.Event(), threading.Event()
    class Model:
        resource_footprint = None
        def __init__(self, *args, **kwargs):
            entered.set()
            release.wait(3)
        def __call__(self, pcm, **kwargs):
            return {"text": "保留片段"}
        def close(self):
            pass
    monkeypatch.setattr("voice_terminal.model_process.ModelProcess", Model)
    lane = DualPassLane(tmp_path)
    try:
        switching = asyncio.create_task(lane.configure(True, "cpu"))
        assert await asyncio.to_thread(entered.wait, 1)
        refining = asyncio.create_task(lane.infer(b"\0\0"))
        await asyncio.sleep(0)
        assert not refining.done()
        release.set()
        await switching
        assert (await refining)["text"] == "保留片段"
    finally:
        release.set()
        lane.close()


@pytest.mark.asyncio
async def test_retired_response_does_not_fail_new_punctuation_request():
    from voice_terminal.asr.client import AsrWorkerClient
    client = AsrWorkerClient("ws://localhost/asr")
    client._retire_request("cancelled")
    waiter = asyncio.get_running_loop().create_future()
    client._health_waiters["current"] = waiter
    class Socket:
        async def __aiter__(self):
            yield json.dumps({"type": "health", "request_id": "cancelled", "result": {"text": "旧"}})
            yield json.dumps({"type": "health", "request_id": "current", "result": {"text": "新"}})
    client._socket = Socket()
    await client._receive_loop()
    assert waiter.result()["result"]["text"] == "新"


@pytest.mark.asyncio
async def test_cancelled_settings_save_finishes_and_restores_old_persistent_state(tmp_path, monkeypatch):
    import threading
    from dataclasses import replace
    from voice_terminal.audio_enhancements import AudioEnhancements
    from voice_terminal.config import AppConfig
    from voice_terminal.server.settings_runtime import ProductSettingsRuntimeMixin
    monkeypatch.setattr("voice_terminal.application.resource_readiness.inspect_model_resources", lambda *_: {})
    entered, release = threading.Event(), threading.Event()
    persisted, configured = [], []
    class Runtime(ProductSettingsRuntimeMixin):
        def snapshot(self):
            return {}
    runtime = Runtime()
    runtime._project_root = tmp_path
    runtime.config = replace(AppConfig(), audio_enhancements=AudioEnhancements(False, False, False))
    runtime.state = SimpleNamespace(listening=True)
    runtime._session_lock = asyncio.Lock()
    runtime.controller = SimpleNamespace(_invalidate_dual_refinements=AsyncMock(), stop_listening=AsyncMock(),
                                        perception={})
    runtime.bus = SimpleNamespace(publish=AsyncMock())
    async def configure(enabled):
        configured.append(enabled)
        return {"device": "cpu", "offline": {"device": "cpu", "status": "READY" if enabled else "DISABLED"}}
    def save(value):
        if value["dual_pass"]:
            entered.set()
            release.wait(3)
        persisted.append(value["dual_pass"])
    runtime.asr = SimpleNamespace(configure_enhancements=configure)
    runtime.product_settings = SimpleNamespace(save_audio_enhancements=save)
    task = asyncio.create_task(runtime.switch_audio_enhancements({"dual_pass": True}))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert persisted == [True, False] and configured == [True, False]
        assert runtime.config.audio_enhancements.dual_pass is False
        assert runtime.state.listening
        runtime.controller.stop_listening.assert_not_called()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_vad_segments_do_not_flush_or_finalize_primary():
    traces, texts = [], []
    for enabled in (False, True):
        engine = AsrWorkerEngine(model_dir=Path("."), device="cpu")
        calls = []
        engine._infer_sync = lambda turn, pcm, final: calls.append((pcm, final)) or ("尾" if final else "字")
        offline = engine.offline = SimpleNamespace(enabled=enabled, epoch=0,
            infer=AsyncMock(return_value={"text": "精修"}), close=lambda: None)
        size = engine.chunk_stride_bytes // 2
        messages = [AudioChunk("t/asr/1", b"\0" * size),
                    AudioSegment("t/asr/1", b"\0" * size, "segment", 0, size // 2, hotwords=("魔搭",)),
                    AudioChunk("t/asr/1", b"\0" * size), AudioChunk("t/asr/1", b"", True)]
        class Socket:
            remote_address = None
            def __init__(self):
                self.sent = []
            async def send(self, value):
                self.sent.append(json.loads(value))
            async def __aiter__(self):
                for row in messages:
                    yield json.dumps(row.to_dict())
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
        socket = Socket()
        try:
            await handle_asr_connection(socket, engine)
            traces.append(calls)
            texts.append([(row["type"], row["text"]) for row in socket.sent if row["type"] in {"asr.partial", "asr.final"}])
            assert not any(row.get("segment_closed") for row in socket.sent)
            assert len(calls) == 2 and calls[0][1] is False and calls[1] == (b"", True)
            if enabled:
                offline.infer.assert_awaited_once_with(b"\0" * size, hotwords=["魔搭"], purpose="conversation")
            else:
                offline.infer.assert_not_called()
        finally:
            engine.close()
    assert traces[0] == traces[1] and texts[0] == texts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "offline", "failed"])
async def test_reference_routes_have_no_hotwords_and_one_punctuation_pass(mode):
    engine = AsrWorkerEngine(model_dir=Path("."), device="cpu", dual_pass_enabled=mode != "disabled")
    calls = []
    engine._infer_sync = lambda turn, pcm, final: calls.append((turn, final)) or ("魔搭" if not final else "尾")
    engine.offline = SimpleNamespace(infer=AsyncMock(return_value={"text": "离线原文"}), close=lambda: None)
    if mode == "failed":
        engine.offline.infer.side_effect = RuntimeError("离线故障")
    engine.text_punctuation.close()
    async def punctuate(text, *, final):
        return {"text": text + "。", "punctuation_status": "READY"}
    engine.text_punctuation = SimpleNamespace(process=AsyncMock(side_effect=punctuate), close=lambda: None)
    try:
        result = await engine.transcribe_reference("unique-reference", b"\0" * engine.chunk_stride_bytes)
        assert result["complete"] and result["punctuation_status"] == "READY"
        engine.text_punctuation.process.assert_awaited_once()
        if mode == "offline":
            assert result["route"] == "offline" and not calls
        else:
            assert result["route"] == "streaming" and calls == [("unique-reference", False), ("unique-reference", True)]
        if mode == "disabled":
            engine.offline.infer.assert_not_called()
        else:
            engine.offline.infer.assert_awaited_once_with(b"\0" * engine.chunk_stride_bytes, hotwords=[], purpose="reference")
        if mode == "failed":
            assert result["fallback_reason"] == "离线故障"
    finally:
        engine.close()


def test_native_hotwords_use_phrase_file_and_reference_explicitly_clears(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_terminal.local_layout.local_root", lambda: tmp_path)
    model = object.__new__(OfflineParaformer)
    model.model_dir = tmp_path
    seen, paths = [], []
    def generate(**kwargs):
        filename = kwargs["hotword"]
        seen.append(Path(filename).read_text(encoding="utf-8") if filename else None)
        if filename:
            paths.append(Path(filename))
        return [{"text": "中 文， hello world I/O"}]
    model.model = SimpleNamespace(generate=generate)
    assert model(b"\0\0", hotwords=["open source", "魔搭"])["text"] == "中文， hello world I/O"
    model(b"\0\0", hotwords=["不应注入"], purpose="reference")
    model(b"\0\0", hotwords=[])
    assert seen == ["open source\n魔搭", None, None]
    assert all(not path.exists() for path in paths)


def test_csv_and_chinese_separators_keep_slashes_spaces_and_error_location():
    rows = parse_hotword_text('\ufeff热词,别名,排除词\r\n"open source","开源;开放源码；别名、其他","I/O；TCP/IP"\r\n"open source","别名",""')
    assert rows == [{"word": "open source", "aliases": ["开源", "开放源码", "别名", "其他"], "blacklist": ["I/O", "TCP/IP"]}]
    with pytest.raises(ValueError, match="第 2 行第 2 列"):
        parse_hotword_text('热词,别名,排除词\n词,"' + "；".join(str(index) for index in range(9)) + '",')
    assert normalize_asr_text("这 是 测 试 ， hello world I/O") == "这是测试， hello world I/O"
    assert join_asr_segments(["hello", "world", "中 文"]) == "hello world中文"


@pytest.mark.asyncio
async def test_stream_switch_does_not_disable_native_targets_and_turn_is_frozen():
    service = HotwordService()
    try:
        settings = {**DEFAULT_HOTWORDS, "stream_enabled": False, "entries": [{"word": "魔搭", "aliases": ["么哒"], "blacklist": ["其他"]}]}
        await service.configure(settings)
        assert service.native_targets(("old", 1)) == ("魔搭",)
        assert (await service.correct("么哒", ("old", 1)))["status"] == "disabled"
        await service.configure({**settings, "enabled": False})
        assert service.native_targets(("old", 2)) == ("魔搭",)
        assert service.native_targets(("new", 1)) == ()
        legacy = validate_hotwords({"enabled": False, "threshold": .9, "entries": []})
        assert legacy["enabled"] is False and legacy["stream_enabled"] is True
    finally:
        service.close()
