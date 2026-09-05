import asyncio
import unittest

try:
    from fastapi.testclient import TestClient  # type: ignore
    from voice_terminal.config import AppConfig
    from voice_terminal.server.websocket import VoiceRuntime, create_app
except ImportError:  # pragma: no cover - dependency is installed by setup.ps1
    TestClient = None  # type: ignore[assignment]


@unittest.skipUnless(TestClient is not None, "fastapi is not installed")
class ServerProtocolTests(unittest.TestCase):
    def test_vad_startup_failure_keeps_public_asr_status_but_preserves_vad_diagnostic(self) -> None:
        runtime = VoiceRuntime(AppConfig())

        runtime._record_error("vad", "model unavailable", status_service="asr")

        self.assertEqual(runtime.state.services["asr"], "ERROR")
        self.assertEqual(runtime.startup_errors, ["vad: model unavailable"])

    def test_websocket_rejects_command_without_request_id(self) -> None:
        runtime = VoiceRuntime(AppConfig())
        app = create_app(runtime)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as socket:
                socket.send_json({
                    "type": "command",
                    "command": "subscribe",
                    "payload": {},
                })
                error = socket.receive_json()
                self.assertEqual(error["type"], "error")
                self.assertEqual(error["payload"]["code"], "INVALID_COMMAND")

    def test_websocket_subscribe_returns_runtime_snapshot(self) -> None:
        runtime = VoiceRuntime(AppConfig())
        app = create_app(runtime)
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as socket:
                socket.send_json(
                    {
                        "type": "command",
                        "request_id": "req_000001",
                        "command": "subscribe",
                        "payload": {},
                    }
                )
                message = socket.receive_json()
                self.assertEqual(message["type"], "runtime.snapshot")
                self.assertEqual(message["seq"], 1)
                self.assertFalse(message["payload"]["can_start"])

                socket.send_json(
                    {
                        "type": "command",
                        "request_id": "req_000002",
                        "command": "session.start",
                        "payload": {},
                    }
                )
                error = socket.receive_json()
                self.assertEqual(error["type"], "error")
                self.assertEqual(error["payload"]["code"], "RUNTIME_NOT_INITIALIZED")


@unittest.skipUnless(TestClient is not None, "fastapi is not installed")
class RuntimeConcurrencyTests(unittest.TestCase):
    def test_start_and_stop_commands_are_serialized(self) -> None:
        async def exercise() -> tuple[list[str], int]:
            runtime = VoiceRuntime(AppConfig())
            started = asyncio.Event()
            release = asyncio.Event()
            active = 0
            max_active = 0
            order: list[str] = []

            class Controller:
                async def start_listening(self) -> None:
                    nonlocal active, max_active
                    active += 1
                    max_active = max(max_active, active)
                    order.append("start.begin")
                    started.set()
                    await release.wait()
                    order.append("start.end")
                    active -= 1

                async def stop_listening(self) -> None:
                    nonlocal active, max_active
                    active += 1
                    max_active = max(max_active, active)
                    order.append("stop.begin")
                    order.append("stop.end")
                    active -= 1

            runtime.controller = Controller()
            start_task = asyncio.create_task(runtime.start_listening())
            await started.wait()
            stop_task = asyncio.create_task(runtime.stop_listening())
            await asyncio.sleep(0)
            self.assertFalse(stop_task.done())
            release.set()
            await asyncio.gather(start_task, stop_task)
            return order, max_active

        order, max_active = asyncio.run(exercise())
        self.assertEqual(order, ["start.begin", "start.end", "stop.begin", "stop.end"])
        self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
