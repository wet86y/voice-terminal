"""音色精修识别与候选保存回归：保持当前分析实例、设备和纯文本。"""
import asyncio
from types import SimpleNamespace
import pytest
from tests.test_tts_voices import _wav_bytes
from voice_terminal.tts.voices import ModelVoiceLibraries
from voice_terminal.server.tts_voice_runtime import TtsVoiceOperationManager
from unittest.mock import AsyncMock


def test_preview_cleanup_keeps_missing_default_unregistered(tmp_path):
    library = ModelVoiceLibraries(tmp_path / 'tts').for_model('test-model')
    library.register_default(_wav_bytes(), '验收录音', registered=False)
    client = SimpleNamespace(voice_library=library, prepare_reference=AsyncMock(), _prepared_expression_snapshot=('temporary',))
    manager = TtsVoiceOperationManager(SimpleNamespace())
    asyncio.run(manager._restore_formal_reference(client))
    client.prepare_reference.assert_not_awaited()
    assert client._prepared_expression_snapshot is None


def test_temporary_preview_restores_formal_engine_after_failure(tmp_path, monkeypatch):
    import voice_terminal.providers.aniflive_server as server_module
    import voice_terminal.providers.aniflive_tts as client_module
    events = []
    class Server:
        def __init__(self, *args, **kwargs):
            events.append(('temporary_model', kwargs['local_model']))
    class Client:
        def __init__(self, **kwargs):
            pass
        async def close(self):
            events.append('temporary_closed')
    monkeypatch.setattr(server_module, 'AnifLiveTtsServer', Server)
    monkeypatch.setattr(client_module, 'AnifLiveTtsClient', Client)
    async def mark(name):
        events.append(name)
    formal = SimpleNamespace(close=lambda: mark('formal_closed'), ensure_ready=lambda: mark('formal_ready'),
                             prepare_reference=lambda: mark('formal_reference'))
    config = SimpleNamespace(backend='aniflive', local_model='formal', aniflive_language='zh',
                             aniflive_chunk_optimization=True)
    runtime = SimpleNamespace(tts=SimpleNamespace(local=formal), config=SimpleNamespace(tts=config),
        paths=SimpleNamespace(program_root=tmp_path), _runtime_local_root=tmp_path, _tts_voice_root=tmp_path / 'tts')
    manager = TtsVoiceOperationManager(runtime)
    async def run():
        with pytest.raises(RuntimeError, match='试听失败'):
            async with manager._preview_client('other', SimpleNamespace(root=tmp_path)):
                raise RuntimeError('试听失败')
        assert events == [('temporary_model', 'other'), 'formal_closed', 'temporary_closed', 'formal_ready', 'formal_reference']
        assert config.backend == 'aniflive' and config.local_model == 'formal'
    asyncio.run(run())


@pytest.mark.parametrize("description,has_source", [
    ("空灵、特殊的声音", False), ("温和清晰的女声", True), ("", True),
])
@pytest.mark.parametrize("transcript", ["", "  源音频的准确转写。\n "])
def test_preview_modes_can_save_and_reload_with_optional_source_and_transcript(tmp_path, monkeypatch, description, has_source, transcript):
    """description（设计描述）、has_source（含源音频）、transcript（源转写）覆盖三种创建方式。"""
    async def scenario():
        owner = ModelVoiceLibraries(tmp_path)
        library = owner.for_model("aniflive")
        source = library.put_import(_wav_bytes(), "reference.wav") if has_source else None
        snapshots = []  # snapshots（合成时使用的音色快照）。

        async def ready():
            pass

        client = SimpleNamespace(ensure_ready=ready, inference_steps_by_model={"voxcpm2": 20})
        runtime = SimpleNamespace(voice_library=owner, tts=SimpleNamespace(local=client),
            config=SimpleNamespace(tts=SimpleNamespace(backend="aniflive", local_model="tangyuge-v2pp-d78cf598")))
        manager = TtsVoiceOperationManager(runtime)

        async def synthesize(operation, local_client, snapshot, text):
            snapshots.append(snapshot)
            assert local_client is client and text == "目标样音正文。"
            # 返回 (16 kHz 编码器素材, 原生率高保真副本或 None)。
            return b"\x00\x01" * (16000 * 4), None

        async def play(operation):
            return {"asset_id": operation.request["asset_id"]}

        monkeypatch.setattr(manager, "_collect_speech", synthesize)
        monkeypatch.setattr(manager, "_play", play)
        operation = manager.start_preview("aniflive", {
            "name": "参考音色", "design_description": description,
            "reference_asset_id": source["asset_id"] if source else None,
            "reference_text": transcript,
        }, "目标样音正文。")
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        if description or not has_source or not transcript.strip():
            assert result["state"] == "failed" and not snapshots
            assert not library.list_voices()
            return
        assert result["state"] == "succeeded", result["error"]
        saved = library.create_voice(result["result"]["voice"])
        restored = ModelVoiceLibraries(tmp_path).for_model("aniflive")
        snapshot = restored.resolve("aniflive", {"voice_id": saved["id"], "revision": 1})
        assert snapshot.reference_text == transcript.strip()
        assert snapshot.reference_path == restored.asset_path(source["asset_id"])
        assert not manager.busy
    asyncio.run(scenario())


def test_reference_transcription_reuses_paraformer_without_rewriting(tmp_path, monkeypatch):
    calls = []
    class Asr:
        def __init__(self, url):
            calls.append(("url", url))
        async def transcribe_reference(self, pcm):
            calls.append(("reference", len(pcm)))
            return {"complete": True, "text": "  嘿小爪 API原始文本  "}
        async def close(self):
            calls.append(("closed",))
    monkeypatch.setattr("voice_terminal.asr.client.AsrWorkerClient", Asr)
    async def scenario():
        library = ModelVoiceLibraries(tmp_path)
        asset = library.for_model("aniflive").put_import(_wav_bytes(), "reference.wav")
        runtime = SimpleNamespace(voice_library=library, config=SimpleNamespace(asr=SimpleNamespace(worker_url="ws://current-loaded-asr")), player=None)
        manager = TtsVoiceOperationManager(runtime)
        operation = manager.start_transcribe("aniflive", asset["asset_id"])
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        assert result["state"] == "succeeded"
        assert result["result"]["text"] == "嘿小爪 API原始文本"
        assert calls[0] == ("url", "ws://current-loaded-asr")
        assert calls[1][0] == "reference" and calls[1][1] > 0
        assert calls[2] == ("closed",)
        assert not manager.busy
    asyncio.run(scenario())


def test_cancelled_voice_operation_cannot_publish_late_result(tmp_path):
    async def scenario():
        manager = TtsVoiceOperationManager(SimpleNamespace(voice_library=ModelVoiceLibraries(tmp_path), player=None))
        entered = asyncio.Event()
        async def late(item):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": "迟到文本"}
        operation = manager._new("transcribe", {"model": "aniflive"}, late)
        await entered.wait()
        manager.cancel(operation["operation_id"])
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        assert result["state"] == "cancelled" and result["result"] is None
        assert not manager.busy
    asyncio.run(scenario())


def test_sample_collection_fixes_30_steps_without_changing_formal_settings(tmp_path):
    async def scenario():
        library = ModelVoiceLibraries(tmp_path)
        voice_library = library.for_model("aniflive")
        asset = voice_library.put_import(_wav_bytes(), "reference.wav")
        snapshot = voice_library.resolve_draft("aniflive", {"name": "试听", "source": "import",
            "reference_asset_id": asset["asset_id"], "reference_text": "准确的参考正文。"})
        captured = []
        class Session:
            start = AsyncMock()
            send_text = AsyncMock()
            finish = AsyncMock()
            close = AsyncMock()
            cancel = AsyncMock()
            async def audio_chunks(self):
                yield b"\x01\x00" * 16000
        def session(**kwargs):
            captured.append(kwargs)
            return Session()
        client = SimpleNamespace(session=session, sample_rate=16000, prepare_reference=AsyncMock(),
                                 inference_steps_by_model={"voxcpm2": 17})
        manager = TtsVoiceOperationManager(SimpleNamespace(voice_library=library))
        pcm, _ = await manager._collect_speech(SimpleNamespace(id="sample-test", session=None), client, snapshot, "测试正文。")
        assert pcm and "inference_steps" not in captured[0] and captured[0]["preview"] is True
        client.prepare_reference.assert_awaited_once()
        assert client.inference_steps_by_model["voxcpm2"] == 17
    asyncio.run(scenario())
