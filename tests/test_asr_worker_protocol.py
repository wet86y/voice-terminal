from __future__ import annotations

import asyncio
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from voice_terminal.asr.client import AsrWorkerClient, AsrWorkerError
from voice_terminal.asr.probe import probe
from voice_terminal.asr.protocol import AudioChunk
from voice_terminal.asr.worker import AsrInferenceResult, handle_asr_connection
from voice_terminal.config import load_config


class FakeEngine:
    def __init__(self) -> None:
        self.model_loaded = False
        self.turns: set[str] = set()

    def load(self) -> None:
        self.model_loaded = True

    async def infer(self, turn_id: str, pcm: bytes, is_final: bool) -> AsrInferenceResult:
        self.turns.add(turn_id)
        return AsrInferenceResult("你好" if pcm else "", bool(pcm))

    def _clear_turn(self, turn_id: str) -> None:
        self.turns.discard(turn_id)


class FailingEngine(FakeEngine):
    def load(self) -> None:
        raise RuntimeError("model load failed")


class AsrWorkerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError as exc:  # pragma: no cover - setup dependency
            self.skipTest(str(exc))
        self.engine = FakeEngine()

        async def handler(socket):
            await handle_asr_connection(socket, self.engine)

        self.server = await websockets.serve(handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/asr"

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_health_waits_for_worker_response_and_never_reaches_callback(self) -> None:
        received: list[dict] = []

        async def on_result(message):
            received.append(message)

        client = AsrWorkerClient(self.url, on_result=on_result)
        await client.connect()
        health = await client.health(timeout_s=1)
        self.assertEqual(health["status"], "READY")
        self.assertTrue(health["model_loaded"])
        self.assertEqual(received, [])

        await client.send_audio(AudioChunk("turn-1", b"\x00\x00"))
        await client.send_audio(AudioChunk("turn-1", b"", is_final=True))
        await asyncio.sleep(0.05)
        self.assertEqual([item["type"] for item in received], ["asr.partial", "asr.final"])
        await client.close()

    async def test_probe_uses_matching_request_id(self) -> None:
        ready, payload = await probe(self.url, timeout_s=1)
        self.assertTrue(ready)
        self.assertEqual(payload["status"], "READY")
        self.assertTrue(payload["model_loaded"])

    async def test_worker_not_ready_response_is_classified(self) -> None:
        import websockets  # type: ignore

        engine = FailingEngine()

        async def handler(socket):
            await handle_asr_connection(socket, engine)

        server = await websockets.serve(handler, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
        client = AsrWorkerClient(url)
        await client.connect()
        with self.assertRaises(AsrWorkerError) as raised:
            await client.health(timeout_s=1)
        self.assertEqual(raised.exception.code, "ASR_WORKER_NOT_READY")
        await client.close()
        server.close()
        await server.wait_closed()

    async def test_health_timeout_is_not_reported_as_ready(self) -> None:
        import websockets  # type: ignore

        async def silent(socket):
            await socket.wait_closed()

        server = await websockets.serve(silent, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
        client = AsrWorkerClient(url)
        await client.connect()
        with self.assertRaises(AsrWorkerError) as raised:
            await client.health(timeout_s=0.05)
        self.assertEqual(raised.exception.code, "ASR_WORKER_TIMEOUT")
        await client.close()
        server.close()
        await server.wait_closed()

    async def test_health_remote_disconnect_is_classified_and_waiter_cleaned(self) -> None:
        import websockets  # type: ignore

        async def disconnect(socket):
            await socket.recv()
            await socket.close()

        server = await websockets.serve(disconnect, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
        client = AsrWorkerClient(url)
        await client.connect()
        with self.assertRaises(AsrWorkerError) as raised:
            await client.health(timeout_s=1)
        self.assertEqual(raised.exception.code, "ASR_WORKER_DISCONNECTED")
        self.assertEqual(client._health_waiters, {})
        self.assertFalse(client.connected)
        await client.close()
        server.close()
        await server.wait_closed()

    async def test_mismatched_health_response_fails_protocol(self) -> None:
        import websockets  # type: ignore

        async def wrong_health(socket):
            raw = await socket.recv()
            request = json.loads(raw)
            await socket.send(json.dumps({
                "type": "health",
                "request_id": uuid.uuid4().hex,
                "status": "READY",
                "model_loaded": True,
            }))
            await asyncio.sleep(0.2)

        server = await websockets.serve(wrong_health, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
        client = AsrWorkerClient(url)
        await client.connect()
        with self.assertRaises(AsrWorkerError) as raised:
            await client.health(timeout_s=0.05)
        self.assertEqual(raised.exception.code, "ASR_WORKER_PROTOCOL_ERROR")
        await client.close()
        server.close()
        await server.wait_closed()

    async def test_mismatched_error_response_fails_health_immediately(self) -> None:
        import websockets  # type: ignore

        async def wrong_error(socket):
            await socket.recv()
            await socket.send(json.dumps({
                "type": "error",
                "request_id": uuid.uuid4().hex,
                "code": "ASR_WORKER_NOT_READY",
                "message": "wrong request",
            }))
            await asyncio.sleep(0.2)

        server = await websockets.serve(wrong_error, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/asr"
        client = AsrWorkerClient(url)
        await client.connect()
        started = asyncio.get_running_loop().time()
        with self.assertRaises(AsrWorkerError) as raised:
            await client.health(timeout_s=1)
        self.assertEqual(raised.exception.code, "ASR_WORKER_PROTOCOL_ERROR")
        self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
        await client.close()
        server.close()
        await server.wait_closed()

    async def test_final_empty_text_is_explicit_ack(self) -> None:
        messages: list[dict] = []
        async def on_result(message):
            messages.append(message)

        client = AsrWorkerClient(self.url, on_result=on_result)
        await client.connect()
        await client.send_audio(AudioChunk("turn-empty", b"", is_final=True))
        await asyncio.sleep(0.05)
        self.assertEqual(messages, [{"type": "asr.final", "turn_id": "turn-empty", "text": ""}])
        await client.close()

    async def test_non_object_message_returns_protocol_error(self) -> None:
        import websockets  # type: ignore

        async with websockets.connect(self.url) as socket:
            await socket.send("[]")
            response = json.loads(await socket.recv())
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "ASR_PROTOCOL_ERROR")


class AsrWorkerConfigurationTests(unittest.TestCase):
    def test_worker_url_can_be_overridden_without_hardcoded_probe_endpoint(self) -> None:
        with patch.dict(os.environ, {"ASR_WORKER_URL": "ws://127.0.0.1:9123/custom"}, clear=False):
            config = load_config(Path("config/default.yaml"))
        self.assertEqual(config.asr.worker_url, "ws://127.0.0.1:9123/custom")


if __name__ == "__main__":
    unittest.main()
