from __future__ import annotations

import asyncio
import base64
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from unittest.mock import AsyncMock, PropertyMock, patch, ANY

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.config import COMPUTE_MODEL_IDS
from voice_terminal.model_registry.service import ModelRegistryService
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
from voice_terminal.devices.trust import (
    device_request_proof_message, pairing_proof_message, parse_ed25519_public_key,
)
from voice_terminal.session.conversation_log import SYSTEM_SPEECH, USER_TURN


class FakeVault:
    @property
    def available(self) -> bool:
        return True

    @property
    def diagnostic(self) -> str | None:
        return None

    def put(self, _reference: str, _secret: str) -> None:
        pass

    def get(self, _reference: str) -> str | None:
        return None

    def delete(self, _reference: str) -> None:
        pass


class ProductApiTests(unittest.TestCase):
    async def _mock_compute_operation(self, body, *, ticket, result=None, failure=None):
        """模拟已接纳的后台事务，保留真实运行状态及失败记录。"""
        from voice_terminal.server.runtime_operations import runtime_operation
        async with runtime_operation(self.runtime, f"compute_device:{body['model_id']}", "测试设备切换", ticket=ticket):
            if failure:
                raise failure
            return result

    def _compute_request_and_finish(self, payload):
        async def run():
            response = await self._request("PUT", "/api/v1/product/settings/compute", json=payload)
            tasks = tuple(getattr(self.runtime, "_compute_switch_tasks", {}))
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            row = self.runtime._runtime_operations.registry.items[response.json()["operation_id"]]
            return response, row
        return asyncio.run(run())

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = AppConfig(
            model_dir=self.root / "models",
            persona=PersonaConfig(directory=self.root / "config" / "persona"),
        )
        settings = ProductSettingsStore(
            path=self.root / "config" / "product-settings.json",
            config=config,
        )
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=self.root / ".local")
        self.runtime = runtime
        registry = ModelRegistryService(
            config=config,
            registry_path=self.root / "config" / "model-registry.json",
            vault=FakeVault(),
        )
        with patch.dict(os.environ, {"VOICE_WEB_ORIGINS": "http://127.0.0.1:9123"}):
            self.app = create_app(runtime, model_registry=registry, product_settings=settings)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inference_endpoint_validates_shape_switches_and_reports_actual_model(self) -> None:
        async def scenario():
            self.runtime._credential_overrides.update({"MINIMAX_API_KEY": "synthetic-mini", "DEEPSEEK_API_KEY": "synthetic-deep"})
            for invalid in ({}, {"backend": []}, {"backend": "unknown"}, {"backend": "minimax", "model": "custom"}):
                response = await self._request("PUT", "/api/v1/settings/inference", json=invalid)
                assert response.status_code == 422
            response = await self._request("PUT", "/api/v1/settings/inference", json={"backend": "minimax"})
            assert response.status_code == 200
            assert response.json()["model"] == "MiniMax-M3.1-Flash"
            snapshot = (await self._request("GET", "/api/v1/product/snapshot")).json()
            assert snapshot["settings"]["inference"]["backend"] == "minimax"
            assert snapshot["runtime"]["inference"]["backend"] == "minimax"
            assert snapshot["runtime"]["inference"]["reasoning_mode_selectable"] is False
            await self.runtime.llm.close()
        asyncio.run(scenario())

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def _gateway_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:18765") as client:
            return await client.request(method, path, **kwargs)

    @staticmethod
    def _ssh_public_key(private_key: Ed25519PrivateKey, comment: str = "test-device") -> str:
        raw_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )
        algorithm = b"ssh-ed25519"
        public_blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", len(raw_key)) + raw_key
        return f"ssh-ed25519 {base64.b64encode(public_blob).decode('ascii')} {comment}"

    async def _signed_gateway_request(self, private_key, device_id: str, path: str, payload: dict):
        challenge_response = await self._gateway_request(
            "POST", "/v1/challenge",
            json={"protocol_version": 1, "device_id": device_id, "method": "POST", "path": path},
        )
        assert challenge_response.status_code == 200
        challenge = challenge_response.json()
        timestamp_ms = time.time_ns() // 1_000_000
        message = device_request_proof_message(
            method="POST", path=path, device_id=device_id,
            core_session_id=challenge["core_session_id"], challenge=challenge["challenge"],
            timestamp_ms=timestamp_ms, payload=payload,
        )
        headers = {
            "X-Voice-Terminal-Device-ID": device_id,
            "X-Voice-Terminal-Core-Session-ID": challenge["core_session_id"],
            "X-Voice-Terminal-Challenge": challenge["challenge"],
            "X-Voice-Terminal-Timestamp-Ms": str(timestamp_ms),
            "X-Voice-Terminal-Signature": base64.b64encode(private_key.sign(message)).decode("ascii"),
        }
        return await self._gateway_request("POST", path, json=payload, headers=headers), headers

    def test_device_gateway_status_is_versioned_and_contains_no_private_snapshot(self) -> None:
        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            self.runtime.device_trust_store.approve(
                "client-status", self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            unauthorized = await self._gateway_request("POST", "/v1/status", json={"protocol_version": 1})
            assert unauthorized.status_code == 401
            response, signed_headers = await self._signed_gateway_request(
                private_key, "client-status", "/v1/status", {"protocol_version": 1},
            )
            assert response.status_code == 200
            assert response.json()["ready"] is True
            assert response.json()["protocol_version"] == 1
            assert response.json()["core_session_id"] == self.runtime.core_session_id
            assert set(response.json()) == {"protocol_version", "ready", "core_session_id", "fields"}
            replay = await self._gateway_request(
                "POST", "/v1/status", json={"protocol_version": 1}, headers=signed_headers,
            )
            assert replay.status_code == 401
            invalid = await self._gateway_request("POST", "/v1/status", json={"protocol_version": 2})
            assert invalid.status_code == 422
            wrong_port = await self._request("POST", "/v1/status", json={"protocol_version": 1})
            assert wrong_port.status_code == 404
        asyncio.run(scenario())

    def test_device_gateway_snapshot_is_read_only_versioned_and_redacted(self) -> None:
        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            self.runtime.device_trust_store.approve(
                "client-devices", self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            response, _headers = await self._signed_gateway_request(
                private_key, "client-devices", "/v1/devices", {"protocol_version": 1},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["protocol_version"] == 1 and payload["ready"] is True
            assert len(payload["devices"]) == 1
            device = payload["devices"][0]
            assert device["device_id"] == "local"
            assert device["connected"] is True and device["interaction_active"] is True
            assert device["connection_generation"] == device["executor_generation"] == 1
            assert set(device) == {
                "device_id", "name", "connected", "connection_generation", "executor_generation",
                "capabilities", "error", "interaction_active",
            }
            assert not any("key" in key.lower() or "credential" in key.lower() for key in payload)
            assert response.headers["cache-control"] == "no-store"
            assert (await self._gateway_request("POST", "/v1/devices", json={"protocol_version": 1, "device_id": "local"})).status_code == 422
            assert (await self._gateway_request("POST", "/v1/devices", json={"protocol_version": 2})).status_code == 422
        asyncio.run(scenario())

    def test_signed_remote_session_routes_dsh_tasks_and_queries_original_receipt(self) -> None:
        from types import SimpleNamespace

        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "client-remote"
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000)
            opened, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["desktop_control", "file_transfer", "terminal_control"]})
            assert opened.status_code == 200, opened.text
            lease = opened.json()
            assert lease["device_id"] == device_id
            assert lease["core_session_id"] == self.runtime.core_session_id
            assert lease["capabilities"] == ["desktop_control", "file_transfer", "terminal_control"]
            heartbeat_body = {"protocol_version": 1,
                "connection_generation": lease["connection_generation"],
                "executor_generation": lease["executor_generation"]}
            heartbeat, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/heartbeat", heartbeat_body)
            assert heartbeat.status_code == 200 and heartbeat.json()["result"] == "accepted"
            stale_heartbeat, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/heartbeat", {**heartbeat_body,
                    "connection_generation": lease["connection_generation"] + 1})
            assert stale_heartbeat.status_code == 409

            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            self.runtime.dsh.bridge_hub.register(session)
            headers = {"authorization": f"Bearer {self.runtime.dsh.bridge_hub._token}"}
            self.runtime.history.screen_capture = SimpleNamespace(
                config=SimpleNamespace(enabled=True), available_for=lambda _device: False)

            windows_body = {"protocol_version": "2.0", "request_id": "remote-windows-1",
                "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2,
                "context_revision": 1, "activation_id": "active", "device_id": device_id}
            windows_call = asyncio.create_task(self._request("POST", "/api/internal/dsh-bridge/device.windows",
                json=windows_body, headers=headers))
            poll_body = {"protocol_version": 1, "connection_generation": lease["connection_generation"],
                         "executor_generation": lease["executor_generation"], "wait_ms": 1000}
            polled, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/poll", poll_body)
            assert polled.status_code == 200, polled.text
            task = polled.json()["task"]
            assert task["request_id"] == windows_body["request_id"]
            assert task["device_id"] == device_id and task["operation"] == "windows"
            windows_result = [{"windowId": 17, "processId": 29, "title": "Test window"}]
            complete_body = {"protocol_version": 1, "connection_generation": lease["connection_generation"],
                "executor_generation": lease["executor_generation"], "task_id": task["task_id"],
                "sequence": task["sequence"], "core_session_id": task["core_session_id"],
                "succeeded": True, "result": windows_result}
            completed, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/complete", complete_body)
            assert completed.status_code == 200 and completed.json()["result"] == "accepted"
            windows_response = await windows_call
            assert windows_response.status_code == 200, windows_response.text
            assert windows_response.json()["device_id"] == device_id
            assert windows_response.json()["windows"] == windows_result

            action_body = {"protocol_version": "2.0", "request_id": "remote-action-1",
                "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2,
                "context_revision": 1, "activation_id": "active", "device_id": device_id,
                "connection_generation": lease["connection_generation"],
                "executor_generation": lease["executor_generation"], "operation": "click",
                "arguments": {"request": {"windowId": 17, "x": 10, "y": 20, "hardware": False},
                              "focusFallback": False}, "timeout_ms": 30000}
            action_call = asyncio.create_task(self._request("POST", "/api/internal/dsh-bridge/device.action",
                json=action_body, headers=headers))
            polled, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/poll", poll_body)
            assert polled.status_code == 200, polled.text
            action_task = polled.json()["task"]
            assert action_task["task_id"] == "remote-action-1" and action_task["operation"] == "click"
            action_result = {"ok": True, "result": {"completed": True}}
            complete_action = {**complete_body, "task_id": action_task["task_id"],
                "sequence": action_task["sequence"], "core_session_id": action_task["core_session_id"],
                "result": action_result}
            completed, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/complete", complete_action)
            assert completed.status_code == 200
            action_response = await action_call
            assert action_response.status_code == 200, action_response.text
            assert action_response.json()["task"]["status"] == "succeeded"

            query_body = {"protocol_version": "2.0", "request_id": "remote-query-1",
                "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2,
                "context_revision": 1, "activation_id": "active", "device_id": device_id,
                "task_id": "remote-action-1"}
            queried = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                json=query_body, headers=headers)
            assert queried.status_code == 200 and queried.json()["task"]["status"] == "succeeded"

            terminal_body = {"protocol_version": "2.0", "request_id": "remote-terminal-create-1",
                "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2,
                "context_revision": 1, "activation_id": "active", "device_id": device_id,
                "action": "create", "terminal_id": "terminal-session-1"}
            terminal_call = asyncio.create_task(self._request("POST", "/api/internal/dsh-bridge/device.terminal",
                json=terminal_body, headers=headers))
            polled, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/poll", poll_body)
            assert polled.status_code == 200, polled.text
            terminal_task = polled.json()["task"]
            assert terminal_task["request_id"] == "remote-terminal-create-1"
            assert terminal_task["operation"] == "terminal.create"
            assert terminal_task["arguments"] == {"terminal_id": "terminal-session-1"}
            complete_terminal = {**complete_body, "task_id": terminal_task["task_id"],
                "sequence": terminal_task["sequence"], "core_session_id": terminal_task["core_session_id"],
                "result": {"terminal_id": "terminal-session-1", "state": "ready"}}
            completed, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/complete", complete_terminal)
            assert completed.status_code == 200
            terminal_response = await terminal_call
            assert terminal_response.status_code == 200, terminal_response.text
            assert terminal_response.json()["task"]["status"] == "succeeded"

            file_body = {"protocol_version": "2.0", "request_id": "remote-file-list-1",
                "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2,
                "context_revision": 1, "activation_id": "active", "device_id": device_id,
                "action": "list", "path": "D:\\approved\\folder", "limit": 10}
            file_call = asyncio.create_task(self._request("POST", "/api/internal/dsh-bridge/device.file",
                json=file_body, headers=headers))
            polled, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/poll", poll_body)
            assert polled.status_code == 200, polled.text
            file_task = polled.json()["task"]
            assert file_task["request_id"] == "remote-file-list-1"
            assert file_task["operation"] == "file.list"
            assert file_task["arguments"] == {"path": file_body["path"], "limit": 10}
            complete_file = {**complete_body, "task_id": file_task["task_id"],
                "sequence": file_task["sequence"], "core_session_id": file_task["core_session_id"],
                "result": {"path": file_body["path"], "entries": [], "truncated": False}}
            completed, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/complete", complete_file)
            assert completed.status_code == 200
            file_response = await file_call
            assert file_response.status_code == 200, file_response.text
            assert file_response.json()["task"]["status"] == "succeeded"

            invalid_terminal = {**terminal_body, "request_id": "remote-terminal-invalid-1", "script": "whoami"}
            invalid_response = await self._request("POST", "/api/internal/dsh-bridge/device.terminal",
                json=invalid_terminal, headers=headers)
            assert invalid_response.status_code == 422
            invalid_file = {**file_body, "request_id": "remote-file-invalid-1", "action": []}
            invalid_file_response = await self._request("POST", "/api/internal/dsh-bridge/device.file",
                json=invalid_file, headers=headers)
            assert invalid_file_response.status_code == 422

            closed_body = {"protocol_version": 1, "connection_generation": lease["connection_generation"],
                           "executor_generation": lease["executor_generation"]}
            closed, _headers = await self._signed_gateway_request(private_key, device_id,
                "/v1/session/close", closed_body)
            assert closed.status_code == 200 and closed.json()["result"] == "closed"
            assert self.runtime.device_registry.connection(device_id).connected is False
            self.runtime.dsh.bridge_hub.unregister(session)

        asyncio.run(scenario())

    def test_signed_playback_stop_task_crosses_gateway_poll_and_settles_original_transaction(self) -> None:
        from voice_terminal.devices.playback import RemotePlaybackTransactions

        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "client-playback-stop"
            self.runtime.device_trust_store.approve(
                device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            opened, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["audio_stream"]},
            )
            assert opened.status_code == 200, opened.text
            lease = opened.json()
            router = self.runtime.device_router
            assert router is not None
            pending_stop = asyncio.create_task(
                RemotePlaybackTransactions(router, device_id, timeout_seconds=3)
                .stop_and_snapshot_confirmed("speech-playback-1")
            )

            poll_body = {
                "protocol_version": 1,
                "connection_generation": lease["connection_generation"],
                "executor_generation": lease["executor_generation"],
                "wait_ms": 1_000,
            }
            polled, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/poll", poll_body,
            )
            assert polled.status_code == 200, polled.text
            task = polled.json()["task"]
            assert task["device_id"] == device_id
            assert task["core_session_id"] == self.runtime.core_session_id
            assert task["operation"] == "playback.stop_and_snapshot"
            assert task["arguments"] == {"playback_key": "speech-playback-1"}

            snapshot = {
                "playback_key": "speech-playback-1",
                "enqueued_ms": 420,
                "played_ms": 280,
                "buffered_ms": 140,
                "output_estimated_ms": 240,
                "started": True,
                "device_time_us": 9_000_000,
                "precision": "device_time",
            }
            completed, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/complete",
                {
                    "protocol_version": 1,
                    "connection_generation": lease["connection_generation"],
                    "executor_generation": lease["executor_generation"],
                    "task_id": task["task_id"],
                    "sequence": task["sequence"],
                    "core_session_id": task["core_session_id"],
                    "succeeded": True,
                    "result": snapshot,
                },
            )
            assert completed.status_code == 200, completed.text
            assert completed.json()["result"] == "accepted"

            settled = await pending_stop
            assert settled.key == "speech-playback-1"
            assert settled.played_ms == 280
            assert settled.buffered_ms == 140
            assert settled.device_time_us == 9_000_000
            record = await router.query(request_id=task["request_id"], device_id=device_id)
            assert record is not None and record.status == "succeeded"

            closed, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/close",
                {"protocol_version": 1,
                 "connection_generation": lease["connection_generation"],
                 "executor_generation": lease["executor_generation"]},
            )
            assert closed.status_code == 200 and closed.json()["result"] == "closed"

        asyncio.run(scenario())

    def test_dsh_file_transfer_routes_stream_large_reads_and_approved_attachment_writes(self) -> None:
        from types import SimpleNamespace
        from voice_terminal.server.http_api import device_file_subtask_id

        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "file-transfer-client"
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000)
            opened, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["file_transfer"]})
            assert opened.status_code == 200, opened.text
            lease = opened.json()
            session = SimpleNamespace(voice_session_id="file-voice", dsh_session_id="file-dsh", epoch=4,
                activation_id="file-activation", prepare_callback_binding=AsyncMock())
            self.runtime.dsh.bridge_hub.register(session)
            headers = {"authorization": f"Bearer {self.runtime.dsh.bridge_hub._token}"}
            envelope = {"protocol_version":"2.0","voice_session_id":"file-voice","dsh_session_id":"file-dsh",
                "epoch":4,"context_revision":1,"activation_id":"file-activation","device_id":device_id}
            poll_body = {"protocol_version":1,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"wait_ms":1000}

            source = bytes(range(256)) * 1025
            source_sha = __import__("hashlib").sha256(source).hexdigest()
            read_body = {**envelope,"request_id":"remote-large-read-1","action":"read",
                "path":"D:\\AI\\approved\\large.bin","max_bytes":len(source)}
            read_call = asyncio.create_task(self._request("POST", "/api/internal/dsh-bridge/device.file",
                json=read_body, headers=headers))
            stat_poll, _headers = await self._signed_gateway_request(private_key, device_id,"/v1/session/poll",poll_body)
            stat_task = stat_poll.json()["task"]
            assert stat_task["request_id"] == device_file_subtask_id(read_body["request_id"], "stat")
            assert stat_task["operation"] == "file.stat"
            stat_complete = {**poll_body,"protocol_version":1,"task_id":stat_task["task_id"],
                "sequence":stat_task["sequence"],"core_session_id":stat_task["core_session_id"],
                "succeeded":True,"result":{"path":read_body["path"],"name":"large.bin","bytes":len(source)}}
            stat_complete.pop("wait_ms")
            stat_complete_response, _headers = await self._signed_gateway_request(private_key,device_id,
                "/v1/session/complete",stat_complete)
            assert stat_complete_response.status_code == 200

            upload_poll, _headers = await self._signed_gateway_request(private_key,device_id,"/v1/session/poll",poll_body)
            upload_task = upload_poll.json()["task"]
            assert upload_task["operation"] == "file.sftp.upload"
            assert upload_task["arguments"]["local_path"] == read_body["path"]
            with open(upload_task["arguments"]["remote_path"], "xb") as handle:
                handle.write(source)
            upload_receipt = {"path":upload_task["arguments"]["remote_path"],"bytes":len(source),
                "sha256":source_sha,"transfer_id":upload_task["arguments"]["transfer_id"]}
            upload_complete = {"protocol_version":1,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"task_id":upload_task["task_id"],
                "sequence":upload_task["sequence"],"core_session_id":upload_task["core_session_id"],
                "succeeded":True,"result":upload_receipt}
            upload_complete_response, _headers = await self._signed_gateway_request(private_key,device_id,
                "/v1/session/complete",upload_complete)
            assert upload_complete_response.status_code == 200
            read_response = await read_call
            assert read_response.status_code == 200, read_response.text
            read_task = read_response.json()["task"]
            assert read_task["status"] == "succeeded" and read_task["result"]["streamed"] is True
            transfer_id = read_task["result"]["transfer_id"]
            transfer_common = {**envelope,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"parent_request_id":read_body["request_id"],
                "transfer_id":transfer_id}
            chunk_response = await self._request("POST","/api/internal/dsh-bridge/device.file.transfer",
                json={**transfer_common,"request_id":"remote-large-read-chunk-1","action":"read_chunk",
                    "offset":0,"limit":8192},headers=headers)
            assert chunk_response.status_code == 200, chunk_response.text
            chunk = chunk_response.json()["transfer"]
            assert base64.b64decode(chunk["data_base64"],validate=True) == source[:8192]
            assert chunk["sha256"] == source_sha
            closed_read = await self._request("POST","/api/internal/dsh-bridge/device.file.transfer",
                json={**transfer_common,"request_id":"remote-large-read-close-1","action":"close"},headers=headers)
            assert closed_read.status_code == 200 and closed_read.json()["transfer"]["closed"] is True

            attachment = b"approved streamed attachment\x00" * 8192
            attachment_sha = __import__("hashlib").sha256(attachment).hexdigest()
            write_body = {**envelope,"request_id":"remote-large-write-1","action":"write",
                "path":"D:\\AI\\approved\\new-file.bin","attachment":{"attachment_id":f"sha256:{attachment_sha}",
                    "name":"new-file.bin","bytes":len(attachment)}}
            prepared = await self._request("POST","/api/internal/dsh-bridge/device.file",json=write_body,headers=headers)
            assert prepared.status_code == 200, prepared.text
            ticket = prepared.json()["task"]["result"]
            write_common = {**envelope,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"parent_request_id":write_body["request_id"],
                "transfer_id":ticket["transfer_id"]}
            for offset in range(0, len(attachment), 64 * 1024):
                part = attachment[offset:offset + 64 * 1024]
                appended = await self._request("POST","/api/internal/dsh-bridge/device.file.transfer",
                    json={**write_common,"request_id":f"remote-large-write-chunk-{offset}","action":"upload_chunk",
                        "offset":offset,"data_base64":base64.b64encode(part).decode("ascii")},headers=headers)
                assert appended.status_code == 200, appended.text
                assert appended.json()["transfer"]["next_offset"] == offset + len(part)
            finish_body = {**write_common,"request_id":"remote-large-write-finish-1","action":"finish_upload",
                "expected_bytes":len(attachment),"expected_sha256":attachment_sha}
            finish_call = asyncio.create_task(self._request("POST","/api/internal/dsh-bridge/device.file.transfer",
                json=finish_body,headers=headers))
            download_poll, _headers = await self._signed_gateway_request(private_key,device_id,"/v1/session/poll",poll_body)
            download_task = download_poll.json()["task"]
            assert download_task["operation"] == "file.sftp.download"
            assert download_task["arguments"]["local_path"] == write_body["path"]
            download_receipt = {"path":download_task["arguments"]["remote_path"],"bytes":len(attachment),
                "sha256":attachment_sha,"transfer_id":ticket["transfer_id"]}
            download_complete = {"protocol_version":1,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"task_id":download_task["task_id"],
                "sequence":download_task["sequence"],"core_session_id":download_task["core_session_id"],
                "succeeded":True,"result":download_receipt}
            finished, _headers = await self._signed_gateway_request(private_key,device_id,
                "/v1/session/complete",download_complete)
            assert finished.status_code == 200
            write_result = await finish_call
            assert write_result.status_code == 200, write_result.text
            assert write_result.json()["task"]["status"] == "succeeded"
            closed_write = await self._request("POST","/api/internal/dsh-bridge/device.file.transfer",
                json={**write_common,"request_id":"remote-large-write-close-1","action":"close"},headers=headers)
            assert closed_write.status_code == 200

            closed_body = {"protocol_version":1,"connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"]}
            closed, _headers = await self._signed_gateway_request(private_key,device_id,"/v1/session/close",closed_body)
            assert closed.status_code == 200
            self.runtime.dsh.bridge_hub.unregister(session)

        asyncio.run(scenario())

    def test_device_pairing_requires_desktop_auth_and_explicit_host_approval(self) -> None:
        async def scenario():
            token = "test-device-pairing-token"
            with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": token}):
                unauthorized = await self._request(
                    "POST", "/internal/application/device-pairing", json={"operation": "status"},
                )
                assert unauthorized.status_code == 403

                headers = {"Authorization": f"Bearer {token}"}
                status = await self._request(
                    "POST", "/internal/application/device-pairing", json={"operation": "status"}, headers=headers,
                )
                assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
                assert status.json()["trust"]["state"] == "missing"

                opened = await self._request(
                    "POST", "/internal/application/device-pairing",
                    json={"operation": "open", "ttl_seconds": 120}, headers=headers,
                )
                assert opened.status_code == 200
                pairing_code = opened.json()["pairing_code"]
                assert len(pairing_code) == 8 and pairing_code.isdigit()
                assert not (self.root / ".local" / "state" / "device-trust.json").exists()

                private_key = Ed25519PrivateKey.generate()
                raw_key = private_key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw,
                )
                algorithm = b"ssh-ed25519"
                public_blob = (struct.pack(">I", len(algorithm)) + algorithm
                               + struct.pack(">I", len(raw_key)) + raw_key)
                public_key = f"ssh-ed25519 {base64.b64encode(public_blob).decode('ascii')} test-device"
                _raw, fingerprint = parse_ed25519_public_key(public_key)
                nonce = b"client-nonce-1234567890"
                signature = private_key.sign(pairing_proof_message("client-a", fingerprint, pairing_code, nonce))
                wrong_port = await self._request(
                    "POST", "/v1/pairing/request", json={"protocol_version": 1},
                )
                assert wrong_port.status_code == 404
                submitted = await self._gateway_request(
                    "POST", "/v1/pairing/request",
                    json={
                        "protocol_version": 1,
                        "device_id": "client-a",
                        "public_key": public_key,
                        "pairing_code": pairing_code,
                        "client_nonce": base64.b64encode(nonce).decode("ascii"),
                        "signature": base64.b64encode(signature).decode("ascii"),
                    },
                )
                assert submitted.status_code == 202 and submitted.headers["cache-control"] == "no-store"
                assert "pairing_code" not in submitted.json() and "public_key" not in submitted.json()
                request_id = submitted.json()["request_id"]

                pending = await self._request(
                    "POST", "/internal/application/device-pairing", json={"operation": "status"}, headers=headers,
                )
                assert len(pending.json()["pending_requests"]) == 1
                assert pending.json()["trust"]["devices"] == []

                approved = await self._request(
                    "POST", "/internal/application/device-pairing",
                    json={"operation": "approve", "request_id": request_id}, headers=headers,
                )
                assert approved.status_code == 200
                assert approved.json()["trust"]["devices"][0]["state"] == "trusted"
                assert "public_key" not in approved.json()["trust"]["devices"][0]

                business, business_headers = await self._signed_gateway_request(
                    private_key, "client-a", "/v1/status", {"protocol_version": 1},
                )
                assert business.status_code == 200 and business.json()["ready"] is True
                replay = await self._gateway_request(
                    "POST", "/v1/status", json={"protocol_version": 1}, headers=business_headers,
                )
                assert replay.status_code == 401

                session_open, _session_headers = await self._signed_gateway_request(
                    private_key, "client-a", "/v1/session/open",
                    {"protocol_version": 1, "capabilities": ["desktop_control"]},
                )
                assert session_open.status_code == 200
                lease = session_open.json()
                pending_action = asyncio.create_task(self.runtime.execute_device_task(
                    request_id="revoke-active-device-task", device_id="client-a",
                    operation="windows", arguments={}, timeout_seconds=30,
                ))
                poll_body = {"protocol_version": 1,
                    "connection_generation": lease["connection_generation"],
                    "executor_generation": lease["executor_generation"], "wait_ms": 1000}
                polled, _poll_headers = await self._signed_gateway_request(
                    private_key, "client-a", "/v1/session/poll", poll_body,
                )
                assert polled.status_code == 200 and polled.json()["task"]["task_id"] == "revoke-active-device-task"

                revoked = await self._request(
                    "POST", "/internal/application/device-pairing",
                    json={"operation": "revoke", "device_id": "client-a"}, headers=headers,
                )
                assert revoked.status_code == 200
                assert revoked.json()["trust"]["devices"][0]["state"] == "revoked"
                connection = self.runtime.device_registry.connection("client-a")
                assert connection is not None and connection.connected is False
                from voice_terminal.devices.router import DeviceOutcomeUnknown
                with self.assertRaises(DeviceOutcomeUnknown):
                    await pending_action
                task_record = await self.runtime.device_router.query(
                    request_id="revoke-active-device-task", device_id="client-a",
                )
                assert task_record is not None and task_record.status == "unknown"
                after_revoke, _revoked_headers = await self._signed_gateway_request(
                    private_key, "client-a", "/v1/status", {"protocol_version": 1},
                )
                assert after_revoke.status_code == 401

                wrong_port_challenge = await self._request(
                    "POST", "/v1/challenge",
                    json={"protocol_version": 1, "device_id": "client-a", "method": "POST", "path": "/v1/status"},
                )
                assert wrong_port_challenge.status_code == 404

                closed = await self._request(
                    "POST", "/internal/application/device-pairing", json={"operation": "close"}, headers=headers,
                )
                assert closed.status_code == 200 and closed.json()["window_open"] is False
        asyncio.run(scenario())

    def test_dsh_device_list_requires_current_authorized_activation(self) -> None:
        from types import SimpleNamespace

        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            body = {"protocol_version": "2.0", "voice_session_id": "current",
                    "dsh_session_id": "dsh", "epoch": 2, "activation_id": "active"}
            headers = {"authorization": f"Bearer {hub._token}"}
            path = "/api/internal/dsh-bridge/device.list"
            assert (await self._request("POST", path, json=body)).status_code == 401
            assert (await self._request("POST", path, json={**body, "epoch": 1}, headers=headers)).status_code == 409
            assert (await self._request("POST", path, json={**body, "activation_id": "old"}, headers=headers)).status_code == 409
            response = await self._request("POST", path, json=body, headers=headers)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            devices = response.json()["devices"]
            public = await self._request("GET", "/api/v1/devices")
            assert public.status_code == 200 and public.headers["cache-control"] == "no-store"
            assert public.json()["devices"] == devices
            assert len(devices) == 1
            assert devices[0]["device_id"] == "local" and devices[0]["connected"] and devices[0]["interaction_active"]
            assert devices[0]["connection_generation"] == devices[0]["executor_generation"] == 1
            assert isinstance(devices[0]["capabilities"], list)
            hub.unregister(session)
            assert (await self._request("POST", path, json=body, headers=headers)).status_code == 404
        asyncio.run(scenario())

    def test_dsh_device_windows_uses_fixed_device_task_transport(self) -> None:
        from types import SimpleNamespace

        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            request_id = "windows-1"
            body = {"protocol_version": "2.0", "request_id": request_id, "voice_session_id": "current",
                    "dsh_session_id": "dsh", "epoch": 2, "context_revision": 1,
                    "activation_id": "active", "device_id": "local"}
            headers = {"authorization": f"Bearer {hub._token}"}
            with patch.object(self.runtime, "execute_device_task", new_callable=AsyncMock) as execute:
                execute.return_value = SimpleNamespace(request_id=request_id, device_id="local", operation="windows",
                    status="succeeded", result=[{"windowId": 11, "processId": 22}])
                path = "/api/internal/dsh-bridge/device.windows"
                remote = await self._request("POST", path, json={**body, "device_id": "unpaired-device"}, headers=headers)
                self.assertEqual(remote.status_code, 503)
                execute.assert_not_awaited()

                response = await self._request("POST", path, json=body, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["device_id"], "local")
                self.assertEqual(response.json()["windows"][0]["windowId"], 11)
                execute.assert_awaited_once_with(request_id=request_id, device_id="local",
                    operation="windows", arguments={}, timeout_seconds=15.0)

                self.runtime.device_registry.connect("paired-device", capabilities=("desktop_control",))

                class RemoteTransport:
                    async def execute(self, _request):
                        return None

                await self.runtime.device_router.attach_transport("paired-device", RemoteTransport())
                remote_request = {**body, "request_id": "windows-remote", "device_id": "paired-device"}
                execute.return_value = SimpleNamespace(request_id="windows-remote", device_id="paired-device",
                    operation="windows", status="succeeded", result=[{"windowId": 33, "processId": 44}])
                accepted = await self._request("POST", path, json=remote_request, headers=headers)
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(accepted.json()["device_id"], "paired-device")
                self.assertEqual(accepted.json()["windows"][0]["windowId"], 33)
                execute.assert_awaited_with(request_id="windows-remote", device_id="paired-device",
                    operation="windows", arguments={}, timeout_seconds=15.0)
            hub.unregister(session)
        asyncio.run(scenario())

    def test_dsh_device_observe_and_action_enforce_generation_and_query_is_read_only(self) -> None:
        from types import SimpleNamespace

        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            headers = {"authorization": f"Bearer {hub._token}"}
            connection = self.runtime.device_registry.connection("local")
            self.runtime.history.screen_capture = SimpleNamespace(
                config=SimpleNamespace(enabled=True), available_for=lambda _device: True)
            snapshot = {"windowId": 10, "processId": 20, "executablePath": "C:\\app.exe", "title": "App",
                        "className": "Window", "rect": {"x": 0, "y": 0, "width": 100, "height": 80},
                        "foreground": True, "treeHash": "tree", "shotHash": "pixels", "elementCount": 0}
            observe = {"protocol_version": "2.0", "request_id": "observe-1", "voice_session_id": "current",
                       "dsh_session_id": "dsh", "epoch": 2, "context_revision": 1, "activation_id": "active",
                       "device_id": "local", "window_id": 10, "max_elements": 20, "max_depth": 5,
                       "include_pixels": False}
            observation_result = {"snapshot": snapshot, "elements": [], "pixels": []}
            with patch.object(self.runtime, "execute_device_task", new_callable=AsyncMock) as execute, \
                    patch.object(self.runtime, "query_device_task", new_callable=AsyncMock) as query:
                execute.return_value = SimpleNamespace(request_id="observe-1", device_id="local", operation="tree",
                    status="succeeded", result=observation_result)
                stale = await self._request("POST", "/api/internal/dsh-bridge/device.observe",
                    json={**observe, "connection_generation": connection.connection_generation + 1,
                          "executor_generation": connection.executor_generation}, headers=headers)
                self.assertEqual(stale.status_code, 409)
                execute.assert_not_awaited()

                accepted = await self._request("POST", "/api/internal/dsh-bridge/device.observe",
                    json={**observe, "connection_generation": connection.connection_generation,
                          "executor_generation": connection.executor_generation}, headers=headers)
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(accepted.json()["device_id"], "local")
                execute.assert_awaited_once_with(request_id="observe-1", device_id="local", operation="tree",
                    arguments={"target": {"windowId": 10}, "maxElements": 20, "maxDepth": 5, "includePixels": False},
                    timeout_seconds=20.0)

                action = {"protocol_version": "2.0", "request_id": "action-1", "voice_session_id": "current",
                          "dsh_session_id": "dsh", "epoch": 2, "context_revision": 1, "activation_id": "active",
                          "device_id": "local", "connection_generation": connection.connection_generation,
                          "executor_generation": connection.executor_generation + 1, "operation": "click",
                          "arguments": {"x": 5, "y": 6, "hardware": False}, "timeout_ms": 30_000}
                stale_action = await self._request("POST", "/api/internal/dsh-bridge/device.action",
                                                   json=action, headers=headers)
                self.assertEqual(stale_action.status_code, 409)
                self.assertEqual(execute.await_count, 1)

                execute.return_value = SimpleNamespace(request_id="action-1", device_id="local", operation="click",
                                                       status="succeeded", result={"ok": True})
                action["executor_generation"] = connection.executor_generation
                completed = await self._request("POST", "/api/internal/dsh-bridge/device.action",
                                                json=action, headers=headers)
                self.assertEqual(completed.status_code, 200, completed.text)
                execute.assert_awaited_with(request_id="action-1", device_id="local", operation="click",
                    arguments={"x": 5, "y": 6, "hardware": False}, timeout_seconds=38.0)

                query.return_value = None
                query_body = {"protocol_version": "2.0", "request_id": "query-1", "voice_session_id": "current",
                              "dsh_session_id": "dsh", "epoch": 2, "context_revision": 1,
                              "activation_id": "active", "device_id": "local", "task_id": "action-1"}
                queried = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                                              json=query_body, headers=headers)
                self.assertEqual(queried.status_code, 200, queried.text)
                self.assertIsNone(queried.json()["task"])
                query.assert_awaited_once_with("action-1", "local")
                self.assertEqual(execute.await_count, 2)
            hub.unregister(session)
        asyncio.run(scenario())

    def test_dsh_capture_explicit_local_target_is_not_redirected_by_voice_switch(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from voice_terminal.devices.screen import ScreenCaptureRouter

        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            local = SimpleNamespace(config=SimpleNamespace(enabled=True), available=True, generation=1,
                                    lease_generation=Mock(return_value=0), invalidate=Mock(), consume=Mock(),
                                    grab=AsyncMock(return_value={"capture_id": "image"}))
            registry = self.runtime.device_registry
            registry.connect("remote")
            lease = registry.prepare_switch("remote", registry.generation)
            assert registry.commit_switch(lease)
            self.runtime.history.screen_capture = ScreenCaptureRouter(local, registry)
            body = {"protocol_version": "2.0", "voice_session_id": "current", "dsh_session_id": "dsh",
                    "epoch": 2, "activation_id": "active"}
            headers = {"authorization": f"Bearer {hub._token}"}
            path = "/api/internal/dsh-bridge/screen.capture"
            assert (await self._request("POST", path, json=body, headers=headers)).status_code == 503
            assert (await self._request("POST", path, json={**body, "device_id": " "}, headers=headers)).status_code == 422
            response = await self._request("POST", path, json={**body, "device_id": "local"}, headers=headers)
            assert response.status_code == 200 and response.json()["capture"]["capture_id"] == "image"
            assert response.json()["capture"]["execution_device_id"] == "local"
            local.grab.assert_awaited_once_with(source="dsh", scope="active")
            hub.unregister(session)
        asyncio.run(scenario())

    def test_dsh_attachment_read_authenticates_current_binding_without_event_lock(self) -> None:
        from types import SimpleNamespace
        from tests.test_screen_capture import packet
        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      _event_lock=asyncio.Lock(), prepare_callback_binding=AsyncMock(side_effect=AssertionError("取图不能重入事件锁")))
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            stored = self.runtime.history.store_screen_capture(packet(), "current", "turn")
            aid = stored["attachments"][0]["attachment_id"]
            body = {"protocol_version": "2.0", "voice_session_id": "current", "dsh_session_id": "dsh", "epoch": 2, "attachment_id": aid}
            headers = {"authorization": f"Bearer {hub._token}"}
            path = "/api/internal/dsh-bridge/attachment.read"
            assert (await self._request("POST", path, json=body)).status_code == 401
            assert (await self._request("POST", path, json={**body, "epoch": 1}, headers=headers)).status_code == 409
            assert (await self._request("POST", path, json={**body, "attachment_id": "absent"}, headers=headers)).status_code == 404
            assert (await self._request("POST", path, content="broken", headers=headers)).status_code == 400
            async with session._event_lock:
                response = await asyncio.wait_for(self._request("POST", path, json=body, headers=headers), .5)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert response.json()["attachment"]["base64"] == stored["attachments"][0]["base64"]
            session.prepare_callback_binding.assert_not_awaited()
            hub.unregister(session)
            assert (await self._request("POST", path, json=body, headers=headers)).status_code == 409
        asyncio.run(scenario())

    def test_retired_environment_transcript_endpoints_are_unavailable(self) -> None:
        """已下线的环境转写入口不再暴露记录或清空能力。"""
        async def scenario():
            for method, path in (
                ("GET", "/api/v1/ambient-context"),
                ("DELETE", "/api/v1/ambient-context"),
                ("POST", "/api/internal/dsh-bridge/shadow.ambient.read"),
            ):
                with self.subTest(method=method, path=path):
                    response = await self._request(method, path)
                    self.assertEqual(response.status_code, 404)
        asyncio.run(scenario())

    def test_speaker_capture_commit_and_reload_keep_three_templates_without_raw_audio(self) -> None:
        """声纹完整接口链路：替代硬件采样和推理，实际提交、存储并重新加载资料。"""
        from types import SimpleNamespace
        from voice_terminal.speaker.features import FRONTEND_VERSION
        from voice_terminal.speaker.registry import SpeakerProfileStore

        pcm = b"\x00\x10" * (16000 * 8)
        fingerprint = {"frontend_version": FRONTEND_VERSION,
                       "model_id": self.runtime.config.speaker.model_id, "embedding_dim": 3}

        def captured_segment(**kwargs):
            kwargs["control"].closed.set()
            return SimpleNamespace(pcm=pcm, speech_ms=7200, duration_ms=8000,
                                   rms_dbfs=-18.0, clipped_ratio=0.0, processed=False)

        async def run():
            start = await self._request("POST", "/api/v1/speaker/enrollment/start")
            self.assertEqual(start.status_code, 200)
            enrollment_id = start.json()["enrollment_id"]
            for index in range(3):
                recorded = await self._request("POST", f"/api/v1/speaker/enrollment/{enrollment_id}/capture", json={
                    "index": index, "label": f"第 {index + 1} 段", "seconds": 8, "capture_id": f"capture-{index}",
                })
                self.assertEqual(recorded.status_code, 200, recorded.text)
                self.assertEqual(recorded.json()["received"], index + 1)
            committed = await self._request("POST", f"/api/v1/speaker/enrollment/{enrollment_id}/commit")
            self.assertEqual(committed.status_code, 200, committed.text)
            self.assertEqual(len(committed.json()["templates"]), 3)
            self.assertNotIn(enrollment_id, self.runtime._speaker_enrollment)
            reloaded = SpeakerProfileStore(self.runtime.speaker_profile.root, model_id=self.runtime.config.speaker.model_id)
            reloaded.set_runtime_fingerprint(fingerprint, (1.0, 0.0, 0.0))
            self.assertTrue(reloaded.available)
            self.assertEqual({item.label for item in reloaded.templates.values()}, {"第 1 段", "第 2 段", "第 3 段"})
            self.assertFalse(any(p.suffix in {".wav", ".pcm"} for p in reloaded.root.rglob("*")))

        with patch("voice_terminal.server.speaker_runtime.capture_segment", captured_segment), \
             patch.object(self.runtime, "_embed_speaker_audio", new=AsyncMock(return_value=(1.0, 0.0, 0.0))), \
             patch.object(self.runtime, "_speaker_fingerprint", new=AsyncMock(return_value=(fingerprint, (1.0, 0.0, 0.0)))):
            asyncio.run(run())

    def test_fixed_model_api_rejects_runtime_model_switching(self) -> None:
        original_config = self.runtime.config
        response = asyncio.run(self._request(
            "POST", "/api/v1/models/assignments/apply",
            json={"assignments": {"main_llm": "other-model"}, "confirmed": True},
        ))
        self.assertEqual(response.status_code, 409)
        self.assertIn("本地固定", response.json()["detail"])
        self.assertIs(self.runtime.config, original_config)

    def test_compute_api_switches_only_the_requested_model_immediately(self) -> None:
        before = self.runtime.product_settings.snapshot()["compute"]
        devices = dict(before["devices"])
        devices["emotion"] = "cuda"
        status_models = {
            name: {"device": "cpu", "requested_device": devices[name]}
            for name in COMPUTE_MODEL_IDS
        }
        saved_compute = {"devices": devices, "fallback_devices": dict(before["fallback_devices"])}
        saved_compute["fallback_devices"]["emotion"] = "cuda"
        async def apply(body, *, ticket):
            return await self._mock_compute_operation(body, ticket=ticket, result=saved_compute)
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(side_effect=apply)) as switch:
            saved, operation = self._compute_request_and_finish({"model_id": "emotion", "device": "cuda"})
        self.assertEqual(saved.status_code, 200)
        self.assertFalse(saved.json()["restart_required"])
        self.assertTrue(saved.json()["pending"])
        self.assertTrue(saved.json()["accepted"])
        self.assertEqual(saved.json()["effective_from"], "immediate")
        switch.assert_awaited_once_with({"model_id": "emotion", "device": "cuda"}, ticket=ANY)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["target"], "compute_device:emotion")
        self.assertEqual({key: value for key, value in devices.items() if key != "emotion"},
                         {key: value for key, value in before["devices"].items() if key != "emotion"})
        with patch.object(
            self.runtime,
            "compute_status",
            new=AsyncMock(return_value={"models": status_models, "hardware": {}}),
        ):
            status = asyncio.run(self._request("GET", "/api/v1/product/compute-status"))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(set(status.json()), {"hardware", "models"})
        self.assertEqual(set(status.json()["models"]), set(COMPUTE_MODEL_IDS))

    def test_compute_api_keeps_restart_behavior_for_unopened_device_rows(self) -> None:
        before = self.runtime.product_settings.snapshot()["compute"]
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock()) as switch:
            saved = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"model_id": "smart_turn", "device": "auto"},
            ))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["effective_from"], "application_restart")
        self.assertTrue(saved.json()["restart_required"])
        self.assertTrue(saved.json()["pending"])
        switch.assert_not_awaited()
        compute = saved.json()["settings"]["compute"]
        self.assertEqual(compute["devices"]["smart_turn"], "auto")
        self.assertEqual(compute["fallback_devices"], before["fallback_devices"])
        for model_id, mode in before["devices"].items():
            if model_id != "smart_turn":
                self.assertEqual(compute["devices"][model_id], mode)

    def test_compute_api_rejects_gpu_when_model_backend_is_missing(self) -> None:
        previous = self.runtime.product_settings.snapshot()["compute"]["devices"]["emotion"]
        async def apply(body, *, ticket):
            return await self._mock_compute_operation(body, ticket=ticket, failure=ValueError("emotion 暂不能使用 GPU：后端未安装"))
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(side_effect=apply)):
            response, operation = self._compute_request_and_finish({"model_id": "emotion", "device": "cuda"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(
            self.runtime.product_settings.snapshot()["compute"]["devices"]["emotion"],
            previous,
        )

    def test_compute_api_reports_model_switch_and_rollback_failures(self) -> None:
        async def apply(body, *, ticket):
            return await self._mock_compute_operation(body, ticket=ticket, failure=RuntimeError("设备切换失败且恢复原模型失败"))
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(side_effect=apply)):
            response, operation = self._compute_request_and_finish({"model_id": "paraformer", "device": "cuda"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pending"])
        self.assertEqual(operation["state"], "failed")

    def test_compute_refresh_requests_new_environment_check(self) -> None:
        with patch.object(self.runtime, "compute_status", new=AsyncMock(return_value={"models": {}, "hardware": {}})) as probe:
            response = asyncio.run(self._request("GET", "/api/v1/product/compute-status?refresh=true"))
        self.assertEqual(response.status_code, 200)
        probe.assert_awaited_once_with(refresh_capabilities=True)

    def test_compute_rejects_gpu_when_framework_exists_but_model_file_is_missing(self) -> None:
        previous = self.runtime.product_settings.snapshot()["compute"]["devices"]["paraformer"]
        async def apply(body, *, ticket):
            return await self._mock_compute_operation(body, ticket=ticket, failure=ValueError("Paraformer 资源未配置完整"))
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(side_effect=apply)):
            response, operation = self._compute_request_and_finish({"model_id": "paraformer", "device": "cuda"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(self.runtime.product_settings.snapshot()["compute"]["devices"]["paraformer"], previous)

    def test_owner_only_barge_in_requires_profile_and_worker_even_when_mode_off(self) -> None:
        payload = {
            "mode": "off", "confirm_threshold": 0.5,
            "continuity_threshold": 0.4, "owner_only_barge_in": True,
        }
        self.assertFalse(self.runtime.product_settings.snapshot()["speaker"]["owner_only_barge_in"])
        response = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/speaker", json=payload,
        ))
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.runtime.product_settings.snapshot()["speaker"]["owner_only_barge_in"])

    def test_owner_only_barge_in_saves_independently_of_speaker_mode(self) -> None:
        payload = {
            "mode": "off", "confirm_threshold": 0.5,
            "continuity_threshold": 0.4, "owner_only_barge_in": True,
        }
        with patch("voice_terminal.speaker.registry.SpeakerProfileStore.available", new_callable=PropertyMock, return_value=True), \
             patch.object(self.runtime, "ensure_speaker_worker", new=AsyncMock(return_value=True)), \
             patch.object(self.runtime, "set_speaker_settings", new=AsyncMock(return_value={
                 "owner_only_barge_in_selected": True,
                 "owner_only_barge_in_effective": True,
                 "owner_only_barge_in_reason": None,
             })) as apply_settings:
            response = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/speaker", json=payload,
            ))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["settings"]["speaker"]["owner_only_barge_in"])
        self.assertEqual(apply_settings.await_args.args[0]["mode"], "off")
        self.assertTrue(apply_settings.await_args.args[0]["owner_only_barge_in"])

    def test_snapshot_is_product_safe_and_settings_routes_are_bounded(self) -> None:
        response = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body), {"schema_version", "runtime", "conversation", "shadow", "dsh", "settings", "models", "warnings", "screen_capture_available"},
        )
        self.assertIn("current", body["conversation"])
        self.assertIn("services", body["runtime"])
        rendered = repr(body).lower()
        self.assertNotIn("api_key", rendered)
        self.assertNotIn("credential_ref", rendered)
        self.assertNotIn("token=", rendered)

    def test_screen_capture_settings_save_and_reject_invalid_values_atomically(self) -> None:
        value = {"enabled": False, "crop_width": 1200, "crop_height": 700, "global_max_edge": 1920,
                 "app_overrides": [{"executable": "Game.EXE", "mode": "center"}]}
        response = asyncio.run(self._request("PUT", "/api/v1/product/settings/screen-capture", json=value))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.runtime.history.screen_capture.config.enabled)
        saved = asyncio.run(self._request("GET", "/api/v1/product/snapshot")).json()["settings"]["screen_capture"]
        self.assertEqual(saved["app_overrides"][0]["executable"], "game.exe")
        invalid = asyncio.run(self._request("PUT", "/api/v1/product/settings/screen-capture", json={**value, "crop_width": 10}))
        self.assertEqual(invalid.status_code, 422)
        after = asyncio.run(self._request("GET", "/api/v1/product/snapshot")).json()["settings"]["screen_capture"]
        self.assertEqual(after, saved)

        invalid_silence = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/silence", json={"seconds": 4},
        ))
        self.assertEqual(invalid_silence.status_code, 422)

        invalid_persona = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/persona",
            json={"instructions": "", "personality": "", "user": "", "shadow": "", "path": "x"},
        ))
        self.assertEqual(invalid_persona.status_code, 422)

        keywords_file = self.runtime.config.kws_keywords_file
        keywords_file.parent.mkdir(parents=True, exist_ok=True)
        keywords_file.write_text("phones @HEI_CLAW\n", encoding="utf-8")
        with patch.object(self.runtime, "_build_kws_worker"):
            valid_wake = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/wake",
                json={"enabled": True, "keyword": "hei claw", "sensitivity": 75},
            ))
        self.assertEqual(valid_wake.status_code, 200)
        self.assertEqual(valid_wake.json()["effective_from"], "immediate")
        self.assertFalse(valid_wake.json()["pending"])

        invalid_shadow = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/shadow",
            json={"experimental_context_gate_enabled": True, "extra": False},
        ))
        self.assertEqual(invalid_shadow.status_code, 422)
        valid_shadow = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/shadow",
            json={"experimental_context_gate_enabled": True},
        ))
        self.assertEqual(valid_shadow.status_code, 200)
        self.assertEqual(valid_shadow.json()["effective_from"], "immediate")
        self.assertFalse(valid_shadow.json()["pending"])
        self.assertTrue(valid_shadow.json()["settings"]["shadow"]["experimental_context_gate_enabled"])

        valid_persona = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/persona",
            json={
                "personality": "personality",
                "user": "user",
            },
        ))
        self.assertEqual(valid_persona.status_code, 200)
        self.assertEqual(valid_persona.json()["effective_from"], "next_session")
        self.assertTrue(valid_persona.json()["pending"])
        entry = {
            "id": "search", "trigger": "帮我查查", "target": "shadow",
            "instruction": "查询用户所问的资料。", "enabled": True,
        }
        saved = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/custom-instructions",
            json={"custom_instructions": [entry]},
        ))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["effective_from"], "next_request")
        self.assertEqual(saved.json()["settings"]["custom_instructions"], [entry])
        invalid = asyncio.run(self._request(
            "PUT", "/api/v1/product/settings/custom-instructions",
            json={"custom_instructions": [entry, entry]},
        ))
        self.assertEqual(invalid.status_code, 422)

    def test_bootstrap_returns_the_server_ordered_conversation(self) -> None:
        ledger = self.runtime.history
        ledger.conversation.append(
            USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="灵月是什么游戏？",
            status="completed", delivery={"status": "completed", "termination": "completed"},
        )
        ledger.conversation.append(
            SYSTEM_SPEECH, conversation_id="conversation_a", turn_id="wake_1", speech_id="speech_wake",
            text="我在，请说。", status="completed",
            delivery={"status": "full", "termination": "completed"},
        )

        response = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))

        self.assertEqual(response.status_code, 200)
        conversation = response.json()["conversation"]
        self.assertEqual(conversation["latest_seq"], 2)
        self.assertEqual(
            [(entry["kind"], entry["seq"], entry["text"]) for entry in conversation["entries"]],
            [(USER_TURN, 1, "灵月是什么游戏？"), (SYSTEM_SPEECH, 2, "我在，请说。")],
        )

    def test_cors_only_allows_configured_desktop_origin(self) -> None:
        allowed = asyncio.run(self._request(
            "OPTIONS", "/api/v1/product/snapshot",
            headers={
                "Origin": "http://127.0.0.1:9123",
                "Access-Control-Request-Method": "GET",
            },
        ))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://127.0.0.1:9123")

        for legacy in ("http://localhost:5173", "http://127.0.0.1:5173"):
            denied = asyncio.run(self._request("OPTIONS", "/api/v1/product/snapshot", headers={
                "Origin": legacy, "Access-Control-Request-Method": "GET",
            }))
            self.assertEqual(denied.status_code, 400)
            self.assertNotIn("access-control-allow-origin", denied.headers)

        blocked = asyncio.run(self._request(
            "OPTIONS", "/api/v1/product/snapshot",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        ))
        self.assertNotEqual(blocked.headers.get("access-control-allow-origin"), "*")
        self.assertIsNone(blocked.headers.get("access-control-allow-origin"))

    def test_dsh_launch_is_origin_json_and_ready_gated(self) -> None:
        status = asyncio.run(self._request("GET", "/api/v1/dsh/status"))
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("token", repr(status.json()).lower())

        blocked = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://evil.example"},
        ))
        self.assertEqual(blocked.status_code, 403)

        wrong_type = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", content="{}",
            headers={"Origin": "http://127.0.0.1:9123", "Content-Type": "text/plain"},
        ))
        self.assertEqual(wrong_type.status_code, 415)

        self.runtime.dsh._state = "starting"
        starting = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://127.0.0.1:9123"},
        ))
        self.assertEqual(starting.status_code, 409)

        self.runtime.dsh._state = "ready"
        self.runtime.dsh._launch_url = "http://127.0.0.1:3081/?token=secret-launch-token"
        ready = asyncio.run(self._request(
            "POST", "/api/v1/dsh/ui-launch", json={},
            headers={"Origin": "http://127.0.0.1:9123"},
        ))
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.headers["cache-control"], "no-store")
        self.assertEqual(ready.json()["url"], self.runtime.dsh._launch_url)

        snapshot = asyncio.run(self._request("GET", "/api/v1/product/snapshot"))
        self.assertNotIn("secret-launch-token", snapshot.text)


if __name__ == "__main__":
    unittest.main()
