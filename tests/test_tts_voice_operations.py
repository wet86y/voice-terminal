"""音色精修识别与候选保存回归：保持当前分析实例、设备和纯文本。"""
import asyncio
from types import SimpleNamespace
from tests.test_tts_voices import _wav_bytes
from voice_terminal.tts.voices import ModelVoiceLibraries
from voice_terminal.server.tts_voice_runtime import TtsVoiceOperationManager


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
