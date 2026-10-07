"""Speaker client（声纹客户端）：真实套接字验证故障记录与普通音频拒绝。"""
from __future__ import annotations

import asyncio
import json
import unittest

import numpy as np
import websockets

from voice_terminal.speaker.client import SpeakerWorkerClient, SpeakerWorkerError
from voice_terminal.speaker.quality import base_window_quality
from voice_terminal.speaker.rolling import RollingSpeakerSensor


class SpeakerClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ignore_health = False
        self.embed_response = {"quality": "ready", "embedding": [1.0, 0.0]}
        self.peer = None

        async def handler(socket, path=None):
            self.peer = socket
            async for raw in socket:
                message = json.loads(raw)
                if message["type"] == "health":
                    if self.ignore_health:
                        continue
                    response = {"type": "health", "status": "READY", "model_loaded": True}
                elif message["type"] == "embed":
                    response = {"type": "speaker.embed_result", **self.embed_response}
                else:
                    continue
                await socket.send(json.dumps({**response, "request_id": message["request_id"]}))

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.client = SpeakerWorkerClient(f"ws://127.0.0.1:{port}/speaker")
        await self.client.connect()

    async def asyncTearDown(self):
        await self.client.close()
        self.server.close()
        await self.server.wait_closed()

    async def test_peer_close_records_a_reason_and_fails_pending_requests(self):
        """对端正常关闭也必须降级并记录原因，而不是空错误的红色状态。"""
        self.ignore_health = True
        pending = asyncio.create_task(self.client.health(timeout_s=1))
        await asyncio.sleep(0)
        await self.peer.close()
        with self.assertRaises(SpeakerWorkerError) as caught:
            await pending
        self.assertFalse(self.client.connected)
        self.assertTrue(self.client.degraded)
        self.assertIn("断开", str(caught.exception))
        self.assertTrue(self.client.last_error)
        self.assertFalse(self.client.pending_health)

    async def test_intentional_close_is_not_reported_as_a_failure(self):
        await self.client.health()
        await self.client.close()
        self.assertFalse(self.client.degraded)
        self.assertIsNone(self.client.last_error)

    async def test_health_timeout_has_a_visible_reason(self):
        self.ignore_health = True
        with self.assertRaises(SpeakerWorkerError) as caught:
            await self.client.health(timeout_s=0.05)
        self.assertIn("健康检查超时", str(caught.exception))
        self.assertTrue(self.client.degraded)
        self.assertIn("健康检查超时", self.client.last_error)
        self.assertFalse(self.client.pending_health)

    async def test_model_health_does_not_erase_an_inference_error(self):
        self.embed_response = {"type": "error", "code": "SPEAKER_WORKER_ERROR", "message": "模拟模型推理异常"}
        with self.assertRaises(SpeakerWorkerError):
            await self.client.embed_pcm(b"\x00\x00" * 32000)
        self.assertEqual((await self.client.health())["status"], "READY")
        self.assertTrue(self.client.degraded)
        self.assertEqual(self.client.last_error, "模拟模型推理异常")

    async def test_invalid_embedding_cannot_keep_the_component_healthy(self):
        for embedding in ([], [float("nan")], ["invalid"]):
            with self.subTest(embedding=embedding):
                self.embed_response = {"quality": "ready", "embedding": embedding}
                with self.assertRaises(SpeakerWorkerError):
                    await self.client.embed_pcm(b"\x00\x00" * 32000)
                self.assertTrue(self.client.degraded)
                self.assertTrue(self.client.last_error)

    async def test_quality_rejection_is_an_unknown_identity_with_a_healthy_worker(self):
        """复现已被语音活动检测接受的低电平窗口被工作进程能量门槛拒绝。"""
        time_axis = np.arange(32000) / 16000
        pcm = (np.sin(2 * np.pi * 220 * time_axis) * 220).astype("<i2").tobytes()
        quality, _ = base_window_quality(pcm, sample_rate=16000, min_speech_ms=700)
        self.assertEqual(quality, "insufficient_audio")
        self.embed_response = {"quality": quality, "embedding": []}
        sensor = RollingSpeakerSensor(self.client.embed_pcm, lambda _embedding: 0.8)
        sensor.submit_frame(pcm, start_sample=0, speech=True)
        snapshot = await sensor.analyze()
        self.assertEqual(snapshot.reason, "audio_quality_rejected")
        self.assertEqual(snapshot.quality, quality)
        self.assertFalse(self.client.degraded)
        self.assertIsNone(self.client.last_error)
        self.assertEqual((await self.client.health())["status"], "READY")
