import unittest

try:
    from fastapi.testclient import TestClient  # type: ignore
    from voice_terminal.config import AppConfig
    from voice_terminal.server.websocket import VoiceRuntime, create_app
except ImportError:  # pragma: no cover - dependency is installed by setup.ps1
    TestClient = None  # type: ignore[assignment]


@unittest.skipUnless(TestClient is not None, "fastapi is not installed")
class ServerProtocolTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
