"""模型归属接口、双库启用项与设置单一来源回归。"""
import asyncio
import json
import httpx
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from tests.test_tts_voices import _wav_bytes
from voice_terminal.core.state import TerminalState
from voice_terminal.tts.router import TtsBackendRouter


def test_scoped_voice_api_and_settings_projection(tmp_path):
    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        for library in runtime.voice_library.libraries.values():
            library.register_default(_wav_bytes(), "默认素材准确转写。")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
            one = (await client.get("/api/v1/tts/voices/voxcpm1")).json()
            two = (await client.get("/api/v1/tts/voices/voxcpm2")).json()
            assert one["active_voice"] != two["active_voice"]
            default = one["active_voice"]["voice_id"]
            assert (await client.delete(f"/api/v1/tts/voices/voxcpm1/{default}")).status_code == 409
            asset = (await client.post("/api/v1/tts/assets/voxcpm1/import?filename=test.wav", content=_wav_bytes())).json()["asset"]
            payload = {"name": "自定义", "source": "import", "reference_asset_id": asset["asset_id"], "reference_text": "准确转写。"}
            foreign = await client.post("/api/v1/tts/voices/voxcpm2", json={**payload, "model": "voxcpm1"})
            assert foreign.status_code == 422
            created = (await client.post("/api/v1/tts/voices/voxcpm1", json=payload)).json()["voice"]
            selected = {"voice_id": created["id"], "revision": 1}
            assert (await client.put("/api/v1/tts/voices/voxcpm2/selection", json={"selection": selected})).status_code == 422
            assert (await client.put("/api/v1/tts/voices/voxcpm1/selection", json={"selection": selected})).status_code == 200
            path = "/api/v1/tts/voices/voxcpm1/" + created["id"]
            assert (await client.put(path, json={**payload, "name": "新版", "expected_revision": 1})).status_code == 200
            assert (await client.put(path, json={**payload, "expected_revision": 1})).status_code == 409
            assert store.settings_only()["tts"]["active_voice_by_model"]["voxcpm1"]["revision"] == 2
            assert (await client.delete(path)).status_code == 200
            assert store.settings_only()["tts"]["active_voice_by_model"]["voxcpm1"]["voice_id"] == default
            result = await client.put("/api/v1/product/settings/tts", json={"inference_steps_by_model": {"voxcpm1": 12, "voxcpm2": 22}})
            assert result.status_code == 200
            persisted = json.loads(store.path.read_text(encoding="utf-8"))
            assert persisted["schema_version"] == 14
            assert "active_voice_by_model" not in persisted["tts"]
            target = runtime.voice_library.for_model("voxcpm2").put_import(_wav_bytes(), "target.wav")
            stale = {"name": "过期设计", "source": "design", "reference_asset_id": target["asset_id"],
                     "reference_text": "生成正文。", "design_text": "生成正文。", "design_description": "温柔女声", "inference_steps": 20}
            runtime.voice_library.for_model("voxcpm2").mark_design(target["asset_id"], stale)
            assert (await client.post("/api/v1/tts/voices/voxcpm2", json=stale)).status_code == 422
    asyncio.run(scenario())


def test_queued_selection_keeps_last_target_and_respects_explicit_stop(tmp_path):
    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        local = SimpleNamespace(server=SimpleNamespace(model_available=lambda model: True), close=AsyncMock())
        runtime.tts = TtsBackendRouter(remote=SimpleNamespace(transport="websocket", close=AsyncMock()), local=local)
        runtime.state.set_state(TerminalState.LISTENING, listening=True)
        runtime.state.set_service("tts", "READY")
        async def stop():
            runtime.state.set_state(TerminalState.IDLE, listening=False)
        runtime.controller = SimpleNamespace(_active_input=True, stop_listening=stop, start_listening=AsyncMock())
        switched = []
        async def switch(backend, model):
            switched.append((backend, model))
            runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, backend=backend, local_model=model))
            runtime.tts.backend = backend
        runtime.switch_tts_selection = switch
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
                first = await client.put("/api/v1/product/settings/tts", json={"backend": "voxcpm1", "local_model": "voxcpm1"})
                second = await client.put("/api/v1/product/settings/tts", json={"backend": "voxcpm1", "local_model": "voxcpm2"})
                assert first.json()["pending"] and second.json()["pending"]
                assert runtime.config.tts.backend == "minimax"
                await runtime.stop_listening()
                runtime.controller._active_input = False
                await asyncio.wait_for(runtime._tts_update_task, 3)
                assert switched == [("voxcpm1", "voxcpm2")]
                assert not runtime.state.listening
                runtime.controller.start_listening.assert_not_awaited()
                assert store.settings_only()["tts"]["local_model"] == "voxcpm2"
        finally:
            if getattr(runtime, "_tts_update_task", None) and not runtime._tts_update_task.done():
                runtime._tts_update_task.cancel()
                await asyncio.gather(runtime._tts_update_task, return_exceptions=True)
    asyncio.run(scenario())


def test_rate_change_propagates_lock_ownership_and_closes_old_runtime(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        runtime.tts = SimpleNamespace()
        closed = []
        async def close(**kwargs):
            closed.append(kwargs)
        async def initialize(*, _session_lock_held=False):
            assert _session_lock_held and runtime._session_lock.locked()
            runtime.state.set_service("tts", "READY")
        runtime.close = close
        runtime.initialize = initialize
        async with runtime._session_lock:
            await asyncio.wait_for(runtime.switch_tts_selection("voxcpm1", "voxcpm2"), 1)
        assert closed == [{"preserve_tts_updates": True}]
        assert runtime.config.tts.sample_rate == 16000
        async with runtime._session_lock:
            await runtime.switch_tts_selection("minimax", "voxcpm2")
        assert runtime.config.tts.sample_rate == 32000
    asyncio.run(scenario())


def test_runtime_cleanup_continues_after_controller_or_tts_failure(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        runtime.controller = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("在线关闭失败")))
        local_close = AsyncMock()
        remote = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("连接关闭失败")))
        runtime.tts = TtsBackendRouter(remote=remote, local=SimpleNamespace(close=local_close))
        llm_close = AsyncMock()
        runtime.llm = SimpleNamespace(close=llm_close)
        processor_close = AsyncMock()
        runtime.audio_processor = SimpleNamespace(close=processor_close)
        await runtime.close()
        local_close.assert_awaited_once()
        llm_close.assert_awaited_once()
        processor_close.assert_awaited_once()
        assert runtime.controller is None and runtime.tts is None and runtime.player is None
    asyncio.run(scenario())
