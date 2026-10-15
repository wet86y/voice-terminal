"""音色精修识别与候选保存回归：保持当前分析实例、设备和纯文本。"""
import asyncio
from types import SimpleNamespace
import pytest
from tests.test_tts_voices import _wav_bytes
from voice_terminal.tts.voices import ModelVoiceLibraries
from voice_terminal.server.tts_voice_runtime import TtsVoiceOperationManager


@pytest.mark.parametrize("description,has_source", [
    ("空灵、特殊的声音", False), ("温和清晰的女声", True), ("", True),
])
@pytest.mark.parametrize("transcript", ["", "  源音频的准确转写。\n "])
def test_preview_modes_can_save_and_reload_with_optional_source_and_transcript(tmp_path, monkeypatch, description, has_source, transcript):
    """description（设计描述）、has_source（含源音频）、transcript（源转写）覆盖三种创建方式。"""
    async def scenario():
        owner = ModelVoiceLibraries(tmp_path)
        library = owner.for_model("voxcpm2")
        source = library.put_import(_wav_bytes(), "reference.wav") if has_source else None
        snapshots = []  # snapshots（合成时使用的音色快照）。

        async def ready():
            pass

        client = SimpleNamespace(ensure_ready=ready, inference_steps_by_model={"voxcpm2": 20})
        runtime = SimpleNamespace(voice_library=owner, tts=SimpleNamespace(local=client),
            config=SimpleNamespace(tts=SimpleNamespace(backend="voxcpm1", local_model="voxcpm2")))
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
        operation = manager.start_preview("voxcpm2", {
            "name": "三模式音色", "design_description": description,
            "source_reference_asset_id": source["asset_id"] if source else None,
            "source_reference_text": transcript,
        }, " \n目标样音正文。  ")
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        assert result["state"] == "succeeded", result["error"]
        candidate = result["result"]["voice"]  # candidate（试音后可保存的音色候选）。
        saved = library.create_voice(candidate)
        restored = ModelVoiceLibraries(tmp_path).for_model("voxcpm2")
        voice = restored.get_voice(saved["id"])
        snapshot = restored.resolve("voxcpm2", {"voice_id": saved["id"], "revision": 1})
        assert snapshots[0].reference_path == (library.asset_path(source["asset_id"]) if source else None)
        assert snapshots[0].style_description == description
        assert voice["source"] == ("design" if description else "import")
        assert voice["source_reference_text"] == transcript.strip()
        assert voice["reference_text"] == ("目标样音正文。" if description else transcript.strip())
        assert snapshot.reference_path == restored.asset_path(voice["reference_asset_id"])
        if description:
            assert voice["reference_asset_id"] == result["result"]["asset_id"]
            assert voice.get("source_reference_asset_id") == (source["asset_id"] if source else None)
        else:
            assert voice["reference_asset_id"] == source["asset_id"]
        assert not manager.busy
    asyncio.run(scenario())


def test_reference_transcription_reuses_current_refine_lane_without_rewriting(tmp_path, monkeypatch):
    calls = []
    class Analysis:
        def __init__(self, url):
            calls.append(("url", url))
        async def health(self):
            return {"refine": {"status": "READY", "device": "cuda"}}
        async def analyze(self, operation, pcm, turn, generation, timeout):
            calls.append((operation, len(pcm), turn, generation, timeout))
            return {"result": {"complete": True, "text": "  嘿小爪 API原始文本  "}}
    monkeypatch.setattr("voice_terminal.analysis.client.AnalysisClient", Analysis)
    async def scenario():
        library = ModelVoiceLibraries(tmp_path)
        asset = library.for_model("voxcpm2").put_import(_wav_bytes(), "reference.wav")
        runtime = SimpleNamespace(voice_library=library, config=SimpleNamespace(smart_turn=SimpleNamespace(analysis_url="ws://current-loaded-analysis")), player=None)
        manager = TtsVoiceOperationManager(runtime)
        operation = manager.start_transcribe("voxcpm2", asset["asset_id"])
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        assert result["state"] == "succeeded"
        assert result["result"]["text"] == "嘿小爪 API原始文本"
        assert calls[0] == ("url", "ws://current-loaded-analysis")
        assert calls[1][0] == "refine" and calls[1][-1] == 30000
        assert calls[1][2].startswith("voice-ref-") and calls[1][3] == 0
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
        operation = manager._new("transcribe", {"model": "voxcpm2"}, late)
        await entered.wait()
        manager.cancel(operation["operation_id"])
        await manager._items[operation["operation_id"]].task
        result = manager.get(operation["operation_id"])
        assert result["state"] == "cancelled" and result["result"] is None
        assert not manager.busy
    asyncio.run(scenario())
