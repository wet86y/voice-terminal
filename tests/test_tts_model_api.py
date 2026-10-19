"""模型归属接口、双库启用项与设置单一来源回归。"""
import asyncio
import json
import httpx
import pytest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server import http_api
from voice_terminal.server import tts_voice_runtime
from voice_terminal.server.websocket import SessionRuntime, create_app
from tests.test_tts_voices import _wav_bytes
from voice_terminal.core.state import TerminalState
from voice_terminal.tts.router import TtsBackendRouter
from voice_terminal.audio.player import PcmPlayer


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
            audio_url = f"/api/v1/tts/assets/voxcpm1/{asset['asset_id']}/audio"
            audio = await client.get(audio_url)
            assert audio.status_code == 200
            assert audio.headers["content-type"] == "audio/wav"
            assert audio.content == runtime.voice_library.for_model("voxcpm1").asset_bytes(asset["asset_id"])
            missing = await client.get("/api/v1/tts/assets/voxcpm1/nonexistent/audio")
            assert missing.status_code == 404
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
            assert persisted["schema_version"] == 15
            assert "active_voice_by_model" not in persisted["tts"]
            target = runtime.voice_library.for_model("voxcpm2").put_import(_wav_bytes(), "target.wav")
            stale = {"name": "过期设计", "source": "design", "reference_asset_id": target["asset_id"],
                     "reference_text": "生成正文。", "design_text": "生成正文。", "design_description": "温柔女声", "inference_steps": 20}
            runtime.voice_library.for_model("voxcpm2").mark_design(target["asset_id"], stale)
            assert (await client.post("/api/v1/tts/voices/voxcpm2", json=stale)).status_code == 422
            # 样音独立使用30步，调整正式步数仍可保存并单独校对目标转写。
            candidate = {**stale, "name": "独立样音", "inference_steps": 30,
                         "generation_recipe": "voxcpm2-full-decode-v2"}
            runtime.voice_library.for_model("voxcpm2").mark_design(target["asset_id"], candidate)
            assert (await client.put("/api/v1/product/settings/tts", json={"inference_steps_by_model": {"voxcpm2": 13}})).status_code == 200
            created = await client.post("/api/v1/tts/voices/voxcpm2", json=candidate)
            assert created.status_code == 200, created.text
            saved = created.json()["voice"]
            correction = {**saved, "reference_text": "生成时实际说出的正文。", "expected_revision": 1}
            updated = await client.put("/api/v1/tts/voices/voxcpm2/" + saved["id"], json=correction)
            assert updated.status_code == 200, updated.text
            assert updated.json()["voice"]["reference_asset_id"] == target["asset_id"]
            assert updated.json()["voice"]["reference_text"] == "生成时实际说出的正文。"
            assert runtime.config.tts.inference_steps_by_model["voxcpm2"] == 13
    asyncio.run(scenario())


def test_minimax_voice_lookup_accepts_future_voice_categories(monkeypatch):
    from voice_terminal.server.http_api import _minimax_voice_ids, _validate_minimax_voice_id

    payload = {
        "system_voice": [{"voice_id": "female-shaonv"}],
        "voice_cloning_voice": [{"voice_id": "my-clone"}],
        "voice_generation_voice": [{"voice_id": "my-generated"}],
        "future_official_group": {"voices": [{"voice_id": "future-clone-01"}]},
    }
    assert _minimax_voice_ids(payload) == {
        "female-shaonv", "my-clone", "my-generated", "future-clone-01",
    }

    seen = {}
    client_type = httpx.AsyncClient
    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    runtime = SimpleNamespace(config=AppConfig(), _api_key=lambda _: "test-secret")

    asyncio.run(_validate_minimax_voice_id(runtime, "future-clone-01"))
    assert seen["url"] == "https://api.minimaxi.com/v1/get_voice"
    assert seen["headers"]["authorization"] == "Bearer test-secret"
    assert seen["body"] == {"voice_type": "all"}


def test_minimax_voice_lookup_rejects_unknown_id_and_api_errors(monkeypatch):
    from fastapi import HTTPException
    from voice_terminal.server.http_api import _validate_minimax_voice_id

    client_type = httpx.AsyncClient
    payload = {"future_voice_category": [{"voice_id": "valid-id"}]}
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)), **kwargs,
    ))
    runtime = SimpleNamespace(config=AppConfig(), _api_key=lambda _: "test-secret")
    with pytest.raises(HTTPException) as missing:
        asyncio.run(_validate_minimax_voice_id(runtime, "unknown-id"))
    assert missing.value.status_code == 422

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"message": "unavailable"})), **kwargs,
    ))
    with pytest.raises(HTTPException) as unavailable:
        asyncio.run(_validate_minimax_voice_id(runtime, "valid-id"))
    assert unavailable.value.status_code == 502

    no_key_runtime = SimpleNamespace(config=AppConfig(), _api_key=lambda _: "")
    with pytest.raises(HTTPException) as no_key:
        asyncio.run(_validate_minimax_voice_id(no_key_runtime, "valid-id"))
    assert no_key.value.status_code == 422


def test_minimax_preview_uses_selected_model_voice_and_default_sample(monkeypatch):
    async def scenario():
        observed = {}

        class FakeSession:
            async def send_text(self, text):
                observed["text"] = text

            async def finish(self):
                return None

            async def audio_chunks(self):
                yield b"\0\0" * 160

            async def close(self):
                observed["closed"] = True

        class FakeClient:
            def __init__(self, **kwargs):
                observed["client"] = kwargs
                self.test_session = FakeSession()

            def session(self, *, fresh=False):
                observed["fresh"] = fresh
                return self.test_session

            async def start_session_with_retry(self, session):
                return session

        class FakePlayer:
            sample_rate = 32000
            channels = 1

            def allow(self, operation_id):
                observed["allow"] = operation_id

            def enqueue(self, operation_id, pcm):
                observed["pcm"] = (operation_id, pcm)

            async def wait_until_output_complete(self, *, timeout_seconds):
                observed["timeout"] = timeout_seconds

            def has_started(self, operation_id):
                return True

            def stop_and_snapshot(self, operation_id):
                observed["stopped"] = operation_id

        monkeypatch.setattr(tts_voice_runtime, "MiniMaxTtsClient", FakeClient)
        runtime = SimpleNamespace(config=AppConfig(), _api_key=lambda _: "test-secret", player=FakePlayer(), voice_library=None)
        manager = tts_voice_runtime.TtsVoiceOperationManager(runtime)
        payload = manager.start_minimax_preview("speech-2.8-turbo", "  future-clone-01  ")
        operation = manager._items[payload["operation_id"]]
        await operation.task
        finished = manager.get(payload["operation_id"])
        assert finished["state"] == "succeeded"
        assert finished["result"]["voice_id"] == "future-clone-01"
        assert observed["client"]["model"] == "speech-2.8-turbo"
        assert observed["client"]["voice_id"] == "future-clone-01"
        assert observed["client"]["api_key"] == "test-secret"
        assert observed["text"] == tts_voice_runtime.DESIGN_SAMPLE_TEXT
        assert observed["pcm"][1] == b"\0\0" * 160
        assert observed["closed"] is True

    asyncio.run(scenario())


def test_online_tts_settings_validate_apply_and_persist(tmp_path, monkeypatch):
    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        runtime.state.set_state(TerminalState.IDLE, listening=False)
        remote = SimpleNamespace(transport="websocket", close=AsyncMock())
        local = SimpleNamespace(server=SimpleNamespace(refresh_status=AsyncMock()), close=AsyncMock())
        runtime.tts = TtsBackendRouter(remote=remote, local=local)
        runtime.state.set_service("tts", "READY")
        switched = []

        async def switch(backend, local_model):
            switched.append((backend, local_model, runtime.config.tts.model, runtime.config.tts.voice_id))
            runtime.tts.backend = backend
            runtime.config = replace(runtime.config, tts=replace(
                runtime.config.tts, backend=backend, local_model=local_model,
            ))

        runtime.switch_tts_selection = switch
        validate = AsyncMock()
        monkeypatch.setattr(http_api, "_validate_minimax_voice_id", validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/tts", json={
                "online_model": "speech-2.8-turbo", "online_voice_id": "  my-clone  ",
            })
        assert response.status_code == 200, response.text
        assert validate.await_args.args[1] == "my-clone"
        assert switched == [("minimax", "voxcpm2", "speech-2.8-turbo", "my-clone")]
        assert runtime.config.tts.model == "speech-2.8-turbo"
        assert runtime.config.tts.voice_id == "my-clone"
        assert store.settings_only()["tts"]["online_model"] == "speech-2.8-turbo"
        assert store.settings_only()["tts"]["online_voice_id"] == "my-clone"
        restored = ProductSettingsStore(path=store.path, config=config).effective_config()
        assert restored.tts.model == "speech-2.8-turbo"
        assert restored.tts.voice_id == "my-clone"

    asyncio.run(scenario())


def test_online_tts_client_switch_failure_rolls_back_runtime_and_settings(tmp_path, monkeypatch):
    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        runtime.state.set_state(TerminalState.IDLE, listening=False)
        runtime.tts = TtsBackendRouter(
            remote=SimpleNamespace(transport="websocket", close=AsyncMock()),
            local=SimpleNamespace(server=SimpleNamespace(refresh_status=AsyncMock()), close=AsyncMock()),
        )
        runtime.state.set_service("tts", "READY")
        switch_calls = []

        async def switch(backend, local_model):
            switch_calls.append((backend, local_model))
            if len(switch_calls) == 1:
                raise RuntimeError("模拟客户端重建失败")
            runtime.tts.backend = backend
            runtime.config = replace(runtime.config, tts=replace(
                runtime.config.tts, backend=backend, local_model=local_model,
            ))

        runtime.switch_tts_selection = switch
        validate = AsyncMock()
        monkeypatch.setattr(http_api, "_validate_minimax_voice_id", validate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/tts", json={
                "online_model": "speech-2.8-turbo", "online_voice_id": "candidate-clone",
            })
        assert response.status_code == 503
        assert switch_calls == [("minimax", "voxcpm2"), ("minimax", "voxcpm2")]
        assert runtime.config.tts.model == config.tts.model
        assert runtime.config.tts.voice_id == config.tts.voice_id
        assert store.settings_only()["tts"]["online_model"] == "speech-2.8-hd"
        assert store.settings_only()["tts"]["online_voice_id"] == "female-shaonv"

    asyncio.run(scenario())


def test_online_tts_voice_validation_failure_does_not_save_or_apply(tmp_path, monkeypatch):
    from fastapi import HTTPException

    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        validate = AsyncMock(side_effect=HTTPException(status_code=422, detail="音色 ID 未找到。"))
        monkeypatch.setattr(http_api, "_validate_minimax_voice_id", validate)
        before = dict(store.settings_only()["tts"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/tts", json={
                "online_model": "speech-2.8-turbo", "online_voice_id": "missing-clone",
            })
        assert response.status_code == 422
        assert store.settings_only()["tts"] == before
        assert runtime.config.tts.model == config.tts.model
        assert runtime.config.tts.voice_id == config.tts.voice_id

    asyncio.run(scenario())


def test_asset_file_missing_returns_not_found(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        library = runtime.voice_library.for_model("voxcpm2")
        asset = library.put_import(_wav_bytes(), "reference.wav")
        library.asset_path(asset["asset_id"]).unlink()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://test") as client:
            response = await client.get(f"/api/v1/tts/assets/voxcpm2/{asset['asset_id']}/audio")
            assert response.status_code == 404
    asyncio.run(scenario())


def test_legacy_empty_transcript_can_be_corrected_without_replacing_clone_audio(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        library = runtime.voice_library.for_model("voxcpm2")
        library.register_default(_wav_bytes(), "默认参考转写。")
        asset = library.put_import(_wav_bytes(), "legacy.wav")
        legacy = library.create_voice({"name": "旧音色", "source": "import", "reference_asset_id": asset["asset_id"], "reference_text": ""})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://test") as client:
            path = "/api/v1/tts/voices/voxcpm2/" + legacy["id"]
            renamed = await client.put(path, json={**legacy, "name": "旧音色改名", "expected_revision": 1})
            assert renamed.status_code == 200
            selection = {"voice_id": legacy["id"], "revision": 2}
            assert (await client.put("/api/v1/tts/voices/voxcpm2/selection", json={"selection": selection})).status_code == 422
            corrected = await client.put(path, json={**renamed.json()["voice"], "reference_text": "人工校对后的准确转写。", "expected_revision": 2})
            assert corrected.status_code == 200
            voice = corrected.json()["voice"]
            assert voice["reference_asset_id"] == asset["asset_id"]
            selection["revision"] = 3
            assert (await client.put("/api/v1/tts/voices/voxcpm2/selection", json={"selection": selection})).status_code == 200
            assert library.resolve("voxcpm2", selection).reference_text == "人工校对后的准确转写。"
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


def test_queued_online_tts_settings_apply_after_current_transaction(tmp_path, monkeypatch):
    async def scenario():
        config = AppConfig()
        store = ProductSettingsStore(path=tmp_path / "data/config/product-settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
        local = SimpleNamespace(server=SimpleNamespace(model_available=lambda _: True), close=AsyncMock())
        remote = SimpleNamespace(transport="websocket", close=AsyncMock())
        runtime.tts = TtsBackendRouter(remote=remote, local=local)
        runtime.state.set_state(TerminalState.LISTENING, listening=True)
        runtime.state.set_service("tts", "READY")

        async def stop():
            runtime.state.set_state(TerminalState.IDLE, listening=False)

        runtime.controller = SimpleNamespace(
            _active_input=True, stop_listening=stop, start_listening=AsyncMock(),
        )
        switched = []

        async def switch(backend, local_model):
            switched.append((backend, local_model, runtime.config.tts.model, runtime.config.tts.voice_id))
            runtime.tts.backend = backend

        runtime.switch_tts_selection = switch
        validate = AsyncMock()
        monkeypatch.setattr(http_api, "_validate_minimax_voice_id", validate)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime, product_settings=store)), base_url="http://test") as client:
                response = await client.put("/api/v1/product/settings/tts", json={
                    "online_model": "speech-2.8-turbo", "online_voice_id": "clone-queued",
                })
                assert response.status_code == 200, response.text
                assert response.json()["pending"] is True
                assert runtime.config.tts.model == "speech-2.8-hd"
                assert store.settings_only()["tts"]["online_voice_id"] == "clone-queued"
                await runtime.stop_listening()
                runtime.controller._active_input = False
                await asyncio.wait_for(runtime._tts_update_task, 3)
            assert switched == [("minimax", "voxcpm2", "speech-2.8-turbo", "clone-queued")]
            assert runtime.config.tts.model == "speech-2.8-turbo"
            assert runtime.config.tts.voice_id == "clone-queued"
            runtime.controller.start_listening.assert_not_awaited()
        finally:
            if getattr(runtime, "_tts_update_task", None) and not runtime._tts_update_task.done():
                runtime._tts_update_task.cancel()
                await asyncio.gather(runtime._tts_update_task, return_exceptions=True)

    asyncio.run(scenario())


def test_backend_switch_restores_online_clock_and_preserves_player_instance(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        old = runtime.tts = SimpleNamespace(shutdown=AsyncMock())
        player = runtime.player = PcmPlayer(sample_rate=32000, channels=1, output_device=None)
        player.start = lambda: None
        vad = runtime.vad = object()
        runtime.close = AsyncMock()
        runtime.initialize = AsyncMock()
        local = SimpleNamespace(ensure_ready=AsyncMock(), prewarm=AsyncMock(return_value={"status": "READY"}), close=AsyncMock())
        remote = SimpleNamespace(ensure_ready=AsyncMock(), prewarm=AsyncMock(return_value={"status": "READY"}), close=AsyncMock())
        configured_rates = []
        def build_tts_clients():
            configured_rates.append(runtime.config.tts.sample_rate)
            return remote, local
        runtime._build_tts_clients = build_tts_clients
        runtime._active_voice_snapshot = lambda model: object()
        async with runtime._session_lock:
            await asyncio.wait_for(runtime.switch_tts_selection("voxcpm1", "voxcpm2"), 1)
        old.shutdown.assert_awaited_once()
        assert runtime.config.tts.sample_rate == player.sample_rate == 48000
        assert runtime.player is player and runtime.vad is vad
        async with runtime._session_lock:
            await runtime.switch_tts_selection("minimax", "voxcpm2")
        assert runtime.config.tts.sample_rate == player.sample_rate == 32000
        assert configured_rates == [48000, 32000]
        runtime.close.assert_not_awaited()
        runtime.initialize.assert_not_awaited()
    asyncio.run(scenario())


def test_switch_barrier_includes_remote_output_task_and_queued_audio(tmp_path):
    async def scenario():
        runtime = SessionRuntime(AppConfig(), runtime_data_dir=tmp_path / "data")
        runtime.tts = TtsBackendRouter(remote=SimpleNamespace())
        runtime.player = SimpleNamespace(buffered_ms=0)
        task = asyncio.create_task(asyncio.Event().wait())
        runtime.controller = SimpleNamespace(output=SimpleNamespace(handle=SimpleNamespace(task=task)))
        assert runtime.tts_switch_busy()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not runtime.tts_switch_busy()
        runtime.controller.output.continuation_active = True
        assert runtime.tts_switch_busy()
        runtime.controller.output.continuation_active = False
        runtime.player.buffered_ms = 100
        assert runtime.tts_switch_busy()
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
