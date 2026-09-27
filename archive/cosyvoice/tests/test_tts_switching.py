from __future__ import annotations

import asyncio
import base64
import json
import math
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from fastapi.testclient import TestClient

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.controller.episode_output import EpisodeSpeechOutput
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.tts.cosyvoice_runtime import (
    CosyVoiceRuntimeError, CosyVoiceTtsClient, CosyVoiceTtsSession,
)
from voice_terminal.tts.cosyvoice_worker import resample_24k_to_32k
from voice_terminal.tts import cosyvoice_worker
from voice_terminal.tts.router import TtsBackendRouter


class FakeRemote:
    transport = "websocket"

    async def close(self):
        return

    async def discard_prewarm(self, *, turn_id=None):
        return


class TtsSwitchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = AppConfig(model_dir=self.root / "models",
                           persona=PersonaConfig(directory=self.root / "config" / "persona"))
        self.store = ProductSettingsStore(path=self.root / "config" / "product-settings.json", config=config)
        self.runtime = SessionRuntime(config, product_settings=self.store)
        self.remote = FakeRemote()
        self.runtime.tts = TtsBackendRouter(
            remote=self.remote, worker=self.runtime.cosyvoice_worker, backend="minimax",
        )
        self.runtime.player = SimpleNamespace(sample_rate=32000)
        self.app = create_app(self.runtime, product_settings=self.store)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_product_switch_route_is_removed_and_status_is_locked_to_api(self):
        self.runtime.cosyvoice_worker.ensure_ready = AsyncMock()
        status = await self.request("GET", "/api/v1/tts/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["backend"], "minimax")
        self.assertEqual(status.json()["worker"]["state"], "stopped")

        response = await self.request(
            "PUT", "/api/v1/product/settings/tts", json={"backend": "cosyvoice"},
        )
        self.assertEqual(response.status_code, 404)
        self.runtime.cosyvoice_worker.ensure_ready.assert_not_awaited()
        self.assertEqual(self.runtime.tts.backend, "minimax")
        self.assertEqual(self.store.settings_only()["tts"]["backend"], "minimax")

    async def test_legacy_local_settings_and_assignment_restore_are_forced_to_api(self):
        loaded = AppConfig.from_mapping({
            "tts": {"backend": "cosyvoice", "provider": "cosyvoice"},
        })
        self.assertEqual(loaded.tts.backend, "minimax")
        self.assertEqual(loaded.tts.provider, "minimax")
        local_config = replace(
            self.store.config,
            tts=replace(self.store.config.tts, backend="cosyvoice"),
        )
        settings_path = self.root / "legacy-product-settings.json"
        settings_path.write_text(json.dumps({
            "schema_version": 5, "tts": {"backend": "cosyvoice"},
        }), encoding="utf-8")
        settings = ProductSettingsStore(path=settings_path, config=local_config)
        runtime = SessionRuntime(local_config, product_settings=settings)
        self.assertEqual(settings.snapshot()["tts"]["backend"], "minimax")
        self.assertEqual(settings.effective_config().tts.backend, "minimax")
        self.assertEqual(runtime.config.tts.backend, "minimax")
        self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["tts"]["backend"], "minimax")
        self.assertEqual(runtime.cosyvoice_worker.state, "stopped")

        runtime.config = replace(runtime.config, tts=replace(runtime.config.tts, backend="cosyvoice"))
        payload = {
            "voice": {"model_id": "voice-model", "provider": {"protocol": "deepseek", "base_url": "https://llm.example"}},
            "shadow": {"model_id": "shadow-model", "provider": {"protocol": "deepseek", "base_url": "https://llm.example"}},
            "status": {"model_id": "status-model", "provider": {"protocol": "deepseek", "base_url": "https://llm.example"}},
            "tts": {
                "model_id": "speech-2.8-hd",
                "provider": {"protocol": "minimax", "base_url": "https://api.minimaxi.com"},
                "profile": {"voice_id": "female-shaonv", "output_format": "pcm"},
            },
        }
        restored = await runtime.restore_model_services(payload)
        self.assertTrue(restored["ok"])
        self.assertEqual(runtime.config.tts.backend, "minimax")

    async def test_speech_handle_keeps_provider_chosen_at_creation(self):
        async def ready():
            self.runtime.cosyvoice_worker.state = "ready"
        self.runtime.cosyvoice_worker.ensure_ready = ready
        controller = SimpleNamespace(tts=self.runtime.tts, player=None, options=None,
                                     bridge_options={}, _turn_timings={})
        output = EpisodeSpeechOutput(controller)
        output.runtime = SimpleNamespace(gate=lambda _identity: None, user_speaking=True,
                                         emit=AsyncMock())
        first = await output.begin(("conversation", "request"), "turn-1")
        await self.runtime.tts.switch("cosyvoice")
        second = await output.begin(("conversation", "request"), "turn-2")
        self.assertEqual(first.tts_backend, "minimax")
        self.assertIs(first.tts_client, self.remote)
        self.assertEqual(second.tts_backend, "cosyvoice")
        self.assertIs(second.tts_client, self.runtime.tts.local)
        await output.close()

    async def test_invalid_backend_and_settings_reload(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text(json.dumps({
            "schema_version": 5, "tts": {"backend": "cosyvoice"},
        }), encoding="utf-8")
        reloaded = ProductSettingsStore(path=self.store.path, config=self.store.config)
        self.assertEqual(reloaded.snapshot()["tts"]["backend"], "minimax")
        self.assertEqual(reloaded.effective_config().tts.backend, "minimax")
        self.assertEqual(json.loads(self.store.path.read_text(encoding="utf-8"))["tts"]["backend"], "minimax")
        self.store.path.write_text(json.dumps({
            "schema_version": 4,
            "silence": {"seconds": 30},
            "speaker": {"mode": "filter", "confirm_threshold": 0.71,
                        "continuity_threshold": 0.61},
        }), encoding="utf-8")
        legacy = ProductSettingsStore(path=self.store.path, config=self.store.config)
        self.assertEqual(legacy.snapshot()["tts"]["backend"], "minimax")
        self.assertEqual(legacy.snapshot()["speaker"]["confirm_threshold"], 0.71)


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.total = 0
        self.index = 0

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "text":
            pcm = (b"\x00\x01" * (5 + self.index))
            self.total += len(pcm)
            await self.incoming.put({"type": "audio", "pcm": base64.b64encode(pcm).decode()})
            await self.incoming.put({"type": "done", "index": self.index,
                                     "text_chars": len(message["text"]), "end_received_bytes": self.total})
            self.index += 1
        elif message["type"] in {"finish", "cancel"}:
            await self.incoming.put({"type": "finished"})

    async def close(self):
        await self.incoming.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)


class CosyVoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_order_receipts_and_finish(self):
        socket = FakeSocket()
        manager = SimpleNamespace(ensure_ready=AsyncMock(), websocket_url="ws://local/tts")
        client = CosyVoiceTtsClient(manager)
        session = CosyVoiceTtsSession(client, emotion="happy")
        with patch("voice_terminal.tts.cosyvoice_runtime.websockets.connect", new=AsyncMock(return_value=socket)):
            await session.start()
            await session.send_text("第一句。")
            first = await session.wait_for_segment(0)
            await session.send_text("第二句。")
            second = await session.wait_for_segment(1)
            await session.finish()
            audio = [chunk async for chunk in session.audio_chunks()]
            await session.close()
        self.assertEqual([len(part) for part in audio], [10, 12])
        self.assertEqual((first.index, first.text_chars, first.end_received_bytes), (0, 4, 10))
        self.assertEqual((second.index, second.text_chars, second.end_received_bytes), (1, 4, 22))
        self.assertEqual([item["emotion"] for item in socket.sent if item["type"] == "text"], ["happy", "happy"])

    async def test_cancel_does_not_send_another_text_block(self):
        socket = FakeSocket()
        manager = SimpleNamespace(ensure_ready=AsyncMock(), websocket_url="ws://local/tts", stop=AsyncMock())
        session = CosyVoiceTtsSession(CosyVoiceTtsClient(manager), emotion="calm")
        with patch("voice_terminal.tts.cosyvoice_runtime.websockets.connect", new=AsyncMock(return_value=socket)):
            await session.start()
            await session.send_text("中断。")
            await session.cancel(timeout_seconds=1)
        self.assertEqual([item["type"] for item in socket.sent], ["text", "cancel"])
        manager.stop.assert_not_awaited()

    async def test_local_synthesis_error_is_reported_without_remote_fallback(self):
        class ErrorSocket(FakeSocket):
            async def send(self, raw):
                message = json.loads(raw)
                self.sent.append(message)
                if message["type"] == "text":
                    await self.incoming.put({"type": "error", "message": "模型合成失败"})

        socket = ErrorSocket()
        manager = SimpleNamespace(ensure_ready=AsyncMock(), websocket_url="ws://local/tts",
                                  state="ready", last_error=None)
        remote = SimpleNamespace(session=unittest.mock.Mock(side_effect=AssertionError("远程回退被调用")))
        router = TtsBackendRouter(remote=remote, worker=manager, backend="cosyvoice")
        backend, local = router.bind()
        session = local.session(emotion="calm")
        with patch("voice_terminal.tts.cosyvoice_runtime.websockets.connect", new=AsyncMock(return_value=socket)):
            await session.start()
            await session.send_text("测试。")
            with self.assertRaisesRegex(CosyVoiceRuntimeError, "模型合成失败"):
                await session.wait_for_segment(0)
            await session.close()
        self.assertEqual((backend, router.backend), ("cosyvoice", "cosyvoice"))
        remote.session.assert_not_called()
        self.assertEqual(manager.state, "error")
        router.release(backend)


class CosyVoiceResampleTests(unittest.TestCase):
    def test_stateful_24_to_32_khz_pcm16_duration(self):
        samples = (np.sin(np.arange(24000) * 2 * math.pi * 440 / 24000) * 16000).astype("<i2").tobytes()
        state = None
        parts = []
        for start in range(0, len(samples), 2400):
            result, state = resample_24k_to_32k(samples[start:start + 2400], state)
            parts.append(result)
        self.assertLessEqual(abs(len(b"".join(parts)) - 64000), 4)
        self.assertTrue(all(len(part) % 2 == 0 for part in parts))

    def test_worker_streams_pcm_then_exact_block_receipt(self):
        class FakeSpeech:
            def detach(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return np.zeros((1, 2400), dtype=np.float32)

        class FakeModel:
            def inference_zero_shot(self, text, prompt_text, prompt_wav, *, stream):
                self.seen = (text, prompt_text, prompt_wav, stream)
                yield {"tts_speech": FakeSpeech()}

        fake_model = FakeModel()
        with patch.object(cosyvoice_worker, "model", fake_model), \
             patch.object(cosyvoice_worker, "prompt_wav", Path("prompt.wav")), \
             patch.dict("os.environ", {"VOICE_COSY_TOKEN": "test-token"}):
            with TestClient(cosyvoice_worker.app) as client:
                with client.websocket_connect("/tts?token=test-token") as socket:
                    socket.send_json({"type": "text", "text": "你好。", "emotion": "calm"})
                    audio = socket.receive_json()
                    receipt = socket.receive_json()
                    socket.send_json({"type": "finish"})
                    finished = socket.receive_json()
        self.assertEqual(audio["type"], "audio")
        pcm = base64.b64decode(audio["pcm"])
        self.assertLessEqual(abs(len(pcm) - 6400), 4)
        self.assertEqual(receipt, {"type": "done", "index": 0, "text_chars": 3,
                                   "end_received_bytes": len(pcm)})
        self.assertEqual(finished, {"type": "finished"})
        self.assertEqual(fake_model.seen[0], "你好。")

    def test_worker_acknowledges_cancel_before_slow_model_yields(self):
        started = threading.Event()
        release = threading.Event()

        class SlowModel:
            def inference_zero_shot(self, *_args, **_kwargs):
                started.set()
                release.wait(5)
                return iter(())

        with patch.object(cosyvoice_worker, "model", SlowModel()), \
             patch.object(cosyvoice_worker, "prompt_wav", Path("prompt.wav")), \
             patch.dict("os.environ", {"VOICE_COSY_TOKEN": "test-token"}):
            try:
                with TestClient(cosyvoice_worker.app) as client:
                    with client.websocket_connect("/tts?token=test-token") as socket:
                        socket.send_json({"type": "text", "text": "取消测试。", "emotion": "calm"})
                        self.assertTrue(started.wait(2))
                        start = time.monotonic()
                        socket.send_json({"type": "cancel"})
                        self.assertEqual(socket.receive_json()["type"], "finished")
                        self.assertLess(time.monotonic() - start, 2.0)
            finally:
                release.set()
