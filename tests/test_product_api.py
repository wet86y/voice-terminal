from __future__ import annotations

import asyncio
import base64
import json
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
from voice_terminal.devices.capture_router import SelectableCapturePort
from voice_terminal.devices.media_protocol import MediaFrame, MediaProtocolError
from voice_terminal.devices.pcm_stream import PcmInputStream
from voice_terminal.devices.remote_capture import RemoteAudioCapture, RemoteAudioCaptureError
from voice_terminal.devices.router import DeviceOutcomeUnknown
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
        from tests.device_gateway_support import configure_test_gateway
        self.gateway_port = configure_test_gateway(runtime)
        # 仅此测试进程模拟桌面原生层写入隔离临时信任库；正式服务保持只读。
        assert self.runtime.device_trust_store is not None
        self.runtime.device_trust_store._read_only = False
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
        async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{self.gateway_port}") as client:
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
        assert challenge_response.status_code == 200, challenge_response.text
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
                "capabilities", "error", "interaction_active", "media_ready", "recording_phase",
            }
            assert not any("key" in key.lower() or "credential" in key.lower() for key in payload)
            assert response.headers["cache-control"] == "no-store"
            assert (await self._gateway_request("POST", "/v1/devices", json={"protocol_version": 1, "device_id": "local"})).status_code == 422
            assert (await self._gateway_request("POST", "/v1/devices", json={"protocol_version": 2})).status_code == 422
        asyncio.run(scenario())

    def test_signed_fixed_file_gateway_uses_existing_dsh_ticket_and_bounded_chunks(self) -> None:
        from voice_terminal.server.http_api import device_file_transfer_id

        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "file-gateway-client"
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000)
            opened, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["file_transfer"]})
            assert opened.status_code == 200, opened.text
            lease = opened.json()
            parent_request_id = "dsh-file-gateway-parent"
            connection = self.runtime.device_registry.connection(device_id)
            assert connection is not None
            transfer_id = device_file_transfer_id(connection, parent_request_id)
            content = b"signed fixed FILE payload" * 3000
            digest = __import__("hashlib").sha256(content).hexdigest()
            store = self.runtime.device_file_transfer_store
            store.prepare_device_upload(device_id=device_id,
                connection_generation=lease["connection_generation"],
                executor_generation=lease["executor_generation"], expected_bytes=len(content),
                transfer_id=transfer_id)
            begin = {"protocol_version":1,"action":"begin","transfer_id":transfer_id,
                "operation":"file.sftp.upload","parent_request_id":parent_request_id,
                "connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"maximum_bytes":len(content),
                "fields":{"protocol_version":"设备文件通道协议版本","action":"固定为 begin 的原票据申请",
                    "transfer_id":"原 DSH 文件传输编号","operation":"兼容保留的文件任务操作编号",
                    "parent_request_id":"生成原文件票据的稳定 DSH 请求编号",
                    "connection_generation":"当前 SSH 设备连接代次",
                    "executor_generation":"当前设备执行器代次","maximum_bytes":"原审批允许的最大字节数"}}
            missing_auth = await self._gateway_request("POST", "/v1/file-transfer", json=begin)
            assert missing_auth.status_code == 401
            started, auth_headers = await self._signed_gateway_request(private_key, device_id, "/v1/file-transfer", begin)
            assert started.status_code == 200, started.text
            channel = started.json()["transfer"]
            assert channel["transfer_id"] == transfer_id
            assert channel["total_bytes"] == len(content)
            assert channel["ready"] is False
            assert "path" not in channel and "sftp_path" not in channel and "target_path" not in channel
            replay = await self._gateway_request("POST", "/v1/file-transfer", json=begin, headers=auth_headers)
            assert replay.status_code == 401

            token = channel["channel_token"]
            common = {"protocol_version":1,"device_id":device_id,
                "connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"transfer_id":transfer_id,
                "channel_token":token}
            offset = 0
            for part in (content[:64 * 1024], content[64 * 1024:]):
                payload = {**common,"action":"upload_chunk","offset":offset,
                    "data_base64":base64.b64encode(part).decode("ascii"),
                    "chunk_sha256":__import__("hashlib").sha256(part).hexdigest()}
                payload["fields"] = {key:"中文字段说明" for key in payload}
                stale = {**payload,"executor_generation":lease["executor_generation"] + 1}
                assert (await self._gateway_request("POST", "/v1/file-transfer", json=stale)).status_code == 409
                wrong_token = {**payload,"channel_token":"x" * len(token)}
                assert (await self._gateway_request("POST", "/v1/file-transfer", json=wrong_token)).status_code == 409
                accepted = await self._gateway_request("POST", "/v1/file-transfer", json=payload)
                assert accepted.status_code == 200, accepted.text
                offset = accepted.json()["transfer"]["next_offset"]
            assert offset == len(content)
            wrong_digest = {**common,"action":"finish_upload","expected_bytes":len(content),
                "expected_sha256":"0" * 64}
            wrong_digest["fields"] = {key:"中文字段说明" for key in wrong_digest}
            assert (await self._gateway_request("POST", "/v1/file-transfer", json=wrong_digest)).status_code == 409
            finish = {**common,"action":"finish_upload","expected_bytes":len(content),"expected_sha256":digest}
            finish["fields"] = {key:"中文字段说明" for key in finish}
            completed = await self._gateway_request("POST", "/v1/file-transfer", json=finish)
            assert completed.status_code == 200, completed.text
            assert completed.json()["transfer"]["state"] == "ready"
            assert completed.json()["transfer"]["sha256"] == digest
            assert "target_path" not in completed.json()["transfer"]

            download_parent = "dsh-file-gateway-download"
            download_transfer_id = device_file_transfer_id(connection, download_parent)
            attachment = b"signed fixed download" * 4000
            attachment_sha = __import__("hashlib").sha256(attachment).hexdigest()
            store.prepare_core_upload(device_id=device_id,
                connection_generation=lease["connection_generation"],
                executor_generation=lease["executor_generation"], expected_bytes=len(attachment),
                expected_sha256=attachment_sha, target_path=r"D:\approved\target.bin",
                transfer_id=download_transfer_id)
            for chunk_offset in range(0, len(attachment), 64 * 1024):
                part = attachment[chunk_offset:chunk_offset + 64 * 1024]
                store.append_core_chunk(transfer_id=download_transfer_id, device_id=device_id,
                    connection_generation=lease["connection_generation"],
                    executor_generation=lease["executor_generation"], offset=chunk_offset,
                    data_base64=base64.b64encode(part).decode("ascii"))
            store.finish_core_upload(transfer_id=download_transfer_id, device_id=device_id,
                connection_generation=lease["connection_generation"],
                executor_generation=lease["executor_generation"], expected_bytes=len(attachment),
                expected_sha256=attachment_sha)
            download_begin = {"protocol_version":1,"action":"begin","transfer_id":download_transfer_id,
                "operation":"file.sftp.download","parent_request_id":download_parent,
                "connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"maximum_bytes":len(attachment),
                "expected_sha256":attachment_sha,
                "fields":{"protocol_version":"设备文件通道协议版本","action":"固定为 begin 的原票据申请",
                    "transfer_id":"原 DSH 文件传输编号","operation":"兼容保留的文件任务操作编号",
                    "parent_request_id":"生成原文件票据的稳定 DSH 请求编号",
                    "connection_generation":"当前 SSH 设备连接代次",
                    "executor_generation":"当前设备执行器代次","maximum_bytes":"原审批允许的最大文件字节数",
                    "expected_sha256":"服务端已锁定的附件完整摘要"}}
            download_started, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/file-transfer", download_begin)
            assert download_started.status_code == 200, download_started.text
            download_channel = download_started.json()["transfer"]
            assert download_channel["ready"] is True and download_channel["sha256"] == attachment_sha
            assert "path" not in download_channel and "target_path" not in download_channel
            download_common = {"protocol_version":1,"device_id":device_id,
                "connection_generation":lease["connection_generation"],
                "executor_generation":lease["executor_generation"],"transfer_id":download_transfer_id,
                "channel_token":download_channel["channel_token"]}
            received = bytearray()
            for chunk_offset in range(0, len(attachment), 64 * 1024):
                read_payload = {**download_common,"action":"read_chunk","offset":chunk_offset,"limit":64 * 1024}
                read_payload["fields"] = {key:"中文字段说明" for key in read_payload}
                read_response = await self._gateway_request("POST", "/v1/file-transfer", json=read_payload)
                assert read_response.status_code == 200, read_response.text
                transfer = read_response.json()["transfer"]
                part = base64.b64decode(transfer["data_base64"], validate=True)
                assert transfer["offset"] == chunk_offset
                assert transfer["chunk_sha256"] == __import__("hashlib").sha256(part).hexdigest()
                received.extend(part)
            assert bytes(received) == attachment
            finish_download = {**download_common,"action":"finish_download",
                "expected_bytes":len(attachment),"expected_sha256":attachment_sha}
            finish_download["fields"] = {key:"中文字段说明" for key in finish_download}
            download_finished = await self._gateway_request("POST", "/v1/file-transfer", json=finish_download)
            assert download_finished.status_code == 200, download_finished.text
            assert download_finished.json()["transfer"]["sha256"] == attachment_sha
            assert "target_path" not in download_finished.json()["transfer"]
            closed, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/close",
                {"protocol_version":1,"connection_generation":lease["connection_generation"],
                 "executor_generation":lease["executor_generation"]})
            assert closed.status_code == 200

        asyncio.run(scenario())

    def test_realtime_device_status_and_stop_supersede_a_waiting_start(self) -> None:
        async def scenario():
            from unittest.mock import Mock

            key = Ed25519PrivateKey.generate()
            device_id = "voice-device"
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(key))
            connection = await self.runtime.open_remote_device_session(device_id, ["audio_stream"])
            query = {
                "connection_generation": connection.connection_generation,
                "executor_generation": connection.executor_generation,
            }
            path = "/v1/voice"
            response = await self._gateway_request("POST", "/v1/challenge", json={
                "protocol_version": 1, "device_id": device_id, "method": "GET", "path": path,
            })
            assert response.status_code == 200
            challenge = response.json()
            timestamp = time.time_ns() // 1_000_000
            proof = device_request_proof_message(
                method="GET", path=path, device_id=device_id, core_session_id=challenge["core_session_id"],
                challenge=challenge["challenge"], timestamp_ms=timestamp, payload={"protocol_version": 1, **query},
            )
            headers = {
                "host": f"127.0.0.1:{self.gateway_port}", "x-voice-terminal-device-id": device_id,
                "x-voice-terminal-core-session-id": challenge["core_session_id"],
                "x-voice-terminal-challenge": challenge["challenge"], "x-voice-terminal-timestamp-ms": str(timestamp),
                "x-voice-terminal-signature": base64.b64encode(key.sign(proof)).decode("ascii"),
            }
            incoming = asyncio.Queue()
            outgoing = asyncio.Queue()
            scope = {
                "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
                "scheme": "ws", "root_path": "", "path": path, "raw_path": path.encode(),
                "query_string": "&".join(f"{name}={value}" for name, value in query.items()).encode(),
                "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
                "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", self.gateway_port), "subprotocols": [],
            }

            async def command(name, request_id):
                await incoming.put({"type": "websocket.receive", "text": json.dumps({
                    "type": "command", "request_id": request_id, "command": name, "payload": {},
                })})

            async def receive(kind, request_id=None):
                while True:
                    raw = await asyncio.wait_for(outgoing.get(), timeout=2)
                    assert raw["type"] == "websocket.send", raw
                    message = json.loads(raw["text"])
                    if message["type"] == kind and (request_id is None or message["payload"]["request_id"] == request_id):
                        return message["payload"]

            await incoming.put({"type": "websocket.connect"})
            task = asyncio.create_task(self.app(scope, incoming.get, outgoing.put))
            try:
                assert (await asyncio.wait_for(outgoing.get(), timeout=2))["type"] == "websocket.accept"
                await command("subscribe", "subscribe")
                initial = await receive("devices.snapshot")
                assert initial["self_device_id"] == device_id
                assert next(row for row in initial["devices"] if row["device_id"] == device_id)["connected"]
                await command("session.start", "standby")
                assert (await receive("device.intent", "standby"))["result"] == "rejected"

                registry = self.runtime.device_registry
                lease = registry.prepare_switch(device_id, registry.generation)
                assert registry.commit_switch(lease)
                controller = Mock(start_listening=AsyncMock(), stop_listening=AsyncMock())
                with patch.object(self.runtime, "remote_media_control_ready", return_value=True), \
                     patch.object(self.runtime, "controller", controller):
                    self.runtime.device_state_changed()
                    selected = await receive("devices.snapshot")
                    assert selected["active_device_id"] == device_id
                    assert selected["revision"] > initial["revision"]
                    revision = self.runtime._listen_stop_revision
                    async with self.runtime._session_lock:
                        await command("session.start", "start")
                        await command("session.stop", "stop")
                        async with asyncio.timeout(2):
                            while self.runtime._listen_stop_revision == revision:
                                await asyncio.sleep(0)
                    assert (await receive("device.intent", "stop"))["result"] == "accepted"
                    controller.stop_listening.assert_awaited_once()
                    controller.start_listening.assert_not_awaited()
                    assert not self.runtime.listening_intent_pending()
            finally:
                await incoming.put({"type": "websocket.disconnect", "code": 1000})
                await asyncio.wait_for(task, timeout=2)

        asyncio.run(scenario())

    def test_signed_remote_management_routes_allowlisted_reads_and_existing_settings_writes(self) -> None:
        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "client-management"
            host_id = "host-management"
            self.runtime.device_host_id = host_id
            self.runtime.device_trust_store.approve(
                device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            opened, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["audio_stream"]},
            )
            assert opened.status_code == 200
            generation = opened.json()["connection_generation"]

            fields = {
                "protocol_version": "远程管理协议版本",
                "request_id": "唯一操作编号",
                "target_host_id": "目标服务端设备编号",
                "session_generation": "当前管理会话代次",
                "operation": "固定服务端业务操作编号",
                "arguments": "与操作编号对应的类型化参数",
            }

            async def management(operation: str, arguments: dict, *, target=host_id, session=generation):
                from uuid import uuid4
                payload = {
                    "protocol_version": 1,
                    "request_id": uuid4().hex,
                    "target_host_id": target,
                    "session_generation": session,
                    "operation": operation,
                    "arguments": arguments,
                    "fields": fields,
                }
                return (await self._signed_gateway_request(
                    private_key, device_id, "/v1/management", payload,
                ))[0]

            snapshot = await management("product.snapshot", {})
            assert snapshot.status_code == 200
            assert snapshot.json()["operation"] == "product.snapshot"
            assert snapshot.json()["body"]["settings"]["silence"]["seconds"] == 30

            with patch.object(self.runtime, "compute_status", new=AsyncMock(return_value={"refreshed": True})) as compute_status:
                refreshed = await management("product.compute_status.refresh", {})
            assert refreshed.status_code == 200
            assert refreshed.json()["status"] == 200
            assert refreshed.json()["body"] == {"refreshed": True}
            compute_status.assert_awaited_once_with(refresh_capabilities=True)

            prior = self.runtime.product_settings.settings_only()["silence"]["seconds"]
            updated = prior + 1 if prior < 120 else prior - 1
            saved = await management("settings.silence.save", {"value": {"seconds": updated}})
            assert saved.status_code == 200
            assert saved.json()["status"] == 200
            assert self.runtime.product_settings.settings_only()["silence"]["seconds"] == updated

            with patch.object(self.runtime, "start_listening", new=AsyncMock()) as start_listening:
                rejected = await management("voice.command", {"command": "session.start"})
            assert rejected.status_code == 422  # 远程录制控制使用已认证的实时连接。
            start_listening.assert_not_awaited()

            manual_task_body = {
                "text": "远程服务端提醒", "at": "2026-11-17T12:00:00+08:00",
                "target": "shadow_agent", "reference": "", "creation_source": "manual",
            }
            with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": "test-host-token"}):
                with patch.object(
                    self.runtime.dsh.bridge, "task_api",
                    new=AsyncMock(return_value={"task_id": "server-task-1", "result": "accepted"}),
                ) as create_task:
                    created_task = await management("wake_task.create", {"value": {
                        "text": "  远程服务端提醒  ", "at": "2026-11-17T12:00:00+08:00",
                    }})
            assert created_task.status_code == 200, created_task.text
            assert created_task.json()["status"] == 200
            assert created_task.json()["body"]["task_id"] == "server-task-1"
            create_task.assert_awaited_once_with("POST", "tasks", manual_task_body)

            missing_task = await management(
                "wake_task.update", {"task_id": "missing-task", "value": {"text": "测试"}},
            )
            assert missing_task.status_code == 200
            # The DSH scheduler may be absent in this isolated runtime, but the fixed
            # management operation must still reach its existing handler.
            assert missing_task.json()["status"] in {404, 503}, missing_task.text
            traversal = await management(
                "wake_task.update", {"task_id": "../outside", "value": {"text": "测试"}},
            )
            assert traversal.status_code == 422

            wrong_host = await management("product.snapshot", {}, target="another-host")
            assert wrong_host.status_code == 409
            stale_session = await management("product.snapshot", {}, session=generation + 1)
            assert stale_session.status_code == 409
            arbitrary = await management("product.snapshot?path=/api/v1/models/credentials", {})
            assert arbitrary.status_code == 422

        asyncio.run(scenario())

    def test_remote_product_snapshot_has_ordered_bounded_conversation_and_current_subtitles(self) -> None:
        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "client-conversation-snapshot"
            host_id = "host-conversation-snapshot"
            self.runtime.device_host_id = host_id
            self.runtime.device_trust_store.approve(
                device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            opened, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["audio_stream"]},
            )
            assert opened.status_code == 200
            generation = opened.json()["connection_generation"]
            fields = {
                "protocol_version": "远程管理协议版本",
                "request_id": "唯一操作编号",
                "target_host_id": "目标服务端设备编号",
                "session_generation": "当前管理会话代次",
                "operation": "固定服务端业务操作编号",
                "arguments": "与操作编号对应的类型化参数",
            }

            async def product_snapshot():
                from uuid import uuid4

                payload = {
                    "protocol_version": 1,
                    "request_id": uuid4().hex,
                    "target_host_id": host_id,
                    "session_generation": generation,
                    "operation": "product.snapshot",
                    "arguments": {},
                    "fields": fields,
                }
                return (await self._signed_gateway_request(
                    private_key, device_id, "/v1/management", payload,
                ))[0]

            self.runtime.state.current.update({
                "turn_id": "turn-live-caption",
                "output_generation": 7,
                "assistant_text": "助手逐字字幕全文",
                "tts_text": "当前正在播报的字幕片段",
                "tts_status": "STREAMING",
            })
            for index in range(205):
                kind = USER_TURN if index % 2 == 0 else SYSTEM_SPEECH
                self.runtime.history.conversation.append(
                    kind,
                    conversation_id="conversation-remote",
                    turn_id=f"turn-{index}",
                    speech_id="" if kind == USER_TURN else f"speech-{index}",
                    text=f"对话第 {index} 条",
                    status="completed",
                    delivery={
                        "status": "partial", "termination": "interrupted",
                        "heard_text": "已听到的部分", "precision": "device_time",
                        "untrusted_debug_secret": "delivery-secret-must-not-leak",
                    },
                )

            response = await product_snapshot()
            assert response.status_code == 200, response.text
            result = response.json()
            conversation = result["body"]["conversation"]
            assert result["operation"] == "product.snapshot"
            assert conversation["latest_seq"] == 205
            assert [item["seq"] for item in conversation["entries"]] == list(range(6, 206))
            assert set(conversation) == {"recent", "entries", "latest_seq", "current"}
            assert conversation["current"]["turn_id"] == "turn-live-caption"
            assert conversation["current"]["output_generation"] == 7
            assert conversation["current"]["assistant_text"] == "助手逐字字幕全文"
            assert conversation["current"]["tts_text"] == "当前正在播报的字幕片段"
            assert conversation["current"]["tts_status"] == "STREAMING"
            entry = conversation["entries"][-1]
            assert set(entry) == {
                "seq", "kind", "conversation_id", "turn_id", "speech_id", "text",
                "status", "termination", "delivery", "at",
            }
            assert entry["delivery"] == {
                "status": "partial", "termination": "interrupted",
                "heard_text": "已听到的部分", "precision": "device_time",
            }
            assert "delivery-secret-must-not-leak" not in response.text

            for index in range(205, 405):
                self.runtime.history.conversation.append(
                    SYSTEM_SPEECH,
                    conversation_id="conversation-remote",
                    turn_id=f"turn-long-{index}",
                    speech_id=f"speech-long-{index}",
                    text="界" * 4_000,
                    status="completed",
                    delivery={
                        "status": "full", "termination": "completed",
                        "heard_text": "听到" * 4_000,
                        "uncertain_text": "不确定" * 4_000,
                        "unheard_text": "未听到" * 4_000,
                    },
                )

            large = await product_snapshot()
            assert large.status_code == 200, large.text
            large_snapshot = large.json()["body"]
            large_conversation = large_snapshot["conversation"]
            sequences = [item["seq"] for item in large_conversation["entries"]]
            assert large_conversation["latest_seq"] == 405
            assert sequences and sequences == sorted(set(sequences))
            assert sequences[-1] == 405
            assert len(large_conversation["entries"]) <= 200
            assert all(len(item["text"]) <= 4_000 for item in large_conversation["entries"])
            assert all(len(item["delivery"].get("heard_text", "")) <= 1_000
                       for item in large_conversation["entries"])
            assert len(json.dumps(large_conversation, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 1_048_576
            assert len(large.content) <= 2 * 1024 * 1024

            # A new session snapshot starts from the new ledger head, never the previous session's entries.
            self.runtime.history.conversation.reset()
            self.runtime.state.clear_current()
            reset = await product_snapshot()
            assert reset.status_code == 200, reset.text
            reset_conversation = reset.json()["body"]["conversation"]
            assert reset_conversation["latest_seq"] == 0
            assert reset_conversation["entries"] == []
            assert reset_conversation["current"]["turn_id"] is None

        asyncio.run(scenario())

    def test_remote_management_maps_typed_tts_and_hotword_operations_without_file_paths(self) -> None:
        from voice_terminal.server.http_api import _remote_management_request_spec

        assert _remote_management_request_spec("hotwords.preview", {"value": {"text": "测试"}}) == (
            "POST", "/api/v1/product/hotwords/preview", {"text": "测试"},
        )
        assert _remote_management_request_spec("product.devices.select", {"value": {
            "device_id": "client-a", "expected_interaction_generation": 3,
        }}) == (
            "POST", "/api/v1/devices/interaction",
            {"device_id": "client-a", "expected_interaction_generation": 3},
        )
        assert _remote_management_request_spec("settings.screen_capture.save", {"value": {
            "enabled": True,
        }}) == ("PUT", "/api/v1/product/settings/screen-capture", {"enabled": True})
        assert _remote_management_request_spec("tts.voices.read", {"model_id": "aniflive"}) == (
            "GET", "/api/v1/tts/voices/aniflive", None,
        )
        assert _remote_management_request_spec("tts.voices.capabilities", {"model_id": "aniflive"}) == (
            "GET", "/api/v1/tts/voices/aniflive/capabilities", None,
        )
        assert _remote_management_request_spec("tts.voices.update", {
            "model_id": "aniflive", "voice_id": "voice-1",
            "value": {"name": "测试音色", "expected_revision": 2},
        }) == (
            "PUT", "/api/v1/tts/voices/aniflive/voice-1",
            {"name": "测试音色", "expected_revision": 2},
        )
        assert _remote_management_request_spec("tts.operation.cancel", {"operation_id": "op-123"}) == (
            "POST", "/api/v1/tts/operations/op-123/cancel", {},
        )
        assert _remote_management_request_spec("tts.assets.clip", {
            "model_id": "aniflive", "asset_id": "asset-1", "value": {"start_ms": 20, "end_ms": 80},
        }) == (
            "POST", "/api/v1/tts/assets/aniflive/asset-1/clip", {"start_ms": 20, "end_ms": 80},
        )
        prepare_args = {"model_id":"minimax-female-shaonv-zh","purpose":"source_and_asset",
            "filename":"reference.wav","expected_bytes":4096,"parent_request_id":"tts-parent-1"}
        assert _remote_management_request_spec("tts.assets.import.prepare", prepare_args) == (
            "POST", "/internal/application/tts-assets/import/prepare", prepare_args,
        )
        commit_args = {**prepare_args,"expected_sha256":"a" * 64,"transfer_id":"b" * 32}
        assert _remote_management_request_spec("tts.assets.import.commit", commit_args) == (
            "POST", "/internal/application/tts-assets/import/commit", commit_args,
        )
        abort_args = {"parent_request_id":"tts-parent-1","transfer_id":"b" * 32}
        assert _remote_management_request_spec("tts.assets.import.abort", abort_args) == (
            "POST", "/internal/application/tts-assets/import/abort", abort_args,
        )
        play_args = {"value":{"model":"minimax-female-shaonv-zh","asset_id":"a" * 32}}
        assert _remote_management_request_spec("tts.operation.play", play_args) == (
            "POST", "/api/v1/tts/operations/play", play_args["value"],
        )
        approval_args = {"approval_id": "approval-1", "decision": "rejected", "run_seq": 3}
        assert _remote_management_request_spec("dsh.approval.decide", approval_args) == (
            "POST", "/internal/application/dsh-approval", approval_args,
        )
        for invalid_approval in (
            {**approval_args, "path": "/api/v1/application/control"},
            {**approval_args, "run_seq": True},
            {**approval_args, "run_seq": 1 << 53},
            {**approval_args, "decision": {"allowed": True}},
            {**approval_args, "approval_id": " "},
        ):
            with self.subTest(invalid_approval=invalid_approval), self.assertRaisesRegex(ValueError, "DSH 权限答复"):
                _remote_management_request_spec("dsh.approval.decide", invalid_approval)
        with self.assertRaisesRegex(ValueError, "TTS 单文件导入操作字段"):
            _remote_management_request_spec("tts.assets.import.commit", {**commit_args,"path":"C:/bad"})
        assert _remote_management_request_spec("tts.aniflive.import.reference", {
            "task_id": "task-1", "value": {"reference_text": "你好", "reference_language": "zh"},
        }) == (
            "POST", "/api/v1/tts/aniflive/import/jobs/task-1/reference",
            {"reference_text": "你好", "reference_language": "zh"},
        )
        assert _remote_management_request_spec("tts.aniflive.import.scan", {"value": {
            "source_type": "huggingface", "source": "owner/repository",
        }}) == (
            "POST", "/api/v1/tts/aniflive/import/scan",
            {"source_type": "huggingface", "source": "owner/repository"},
        )
        assert _remote_management_request_spec("speaker.enrollment.capture_stop", {
            "enrollment_id": "enrollment-1", "value": {"capture_id": "capture-1", "cancel": True},
        }) == (
            "POST", "/api/v1/speaker/enrollment/enrollment-1/capture/stop",
            {"capture_id": "capture-1", "cancel": True},
        )
        assert _remote_management_request_spec("speaker.template.update", {
            "template_id": "template-1", "value": {"include_in_centroid": False},
        }) == (
            "PATCH", "/api/v1/speaker/templates/template-1", {"include_in_centroid": False},
        )
        with self.assertRaisesRegex(ValueError, "客户端本机目录"):
            _remote_management_request_spec("tts.aniflive.import.scan", {"value": {
                "source_type": "local", "source": "C:/client/data/models",
            }})
        with self.assertRaisesRegex(ValueError, "资源编号无效"):
            _remote_management_request_spec("tts.voices.read", {"model_id": "../outside"})
        with self.assertRaisesRegex(ValueError, "固定白名单"):
            _remote_management_request_spec("tts.assets.upload", {"path": "client-local-path.wav"})

    def test_remote_dsh_approval_uses_authenticated_host_handler_and_internal_token(self) -> None:
        async def scenario():
            private_key = Ed25519PrivateKey.generate()
            device_id = "client-dsh-approval"
            host_id = "host-dsh-approval"
            self.runtime.device_host_id = host_id
            self.runtime.device_trust_store.approve(
                device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            opened, _headers = await self._signed_gateway_request(
                private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["audio_stream"]},
            )
            assert opened.status_code == 200
            generation = opened.json()["connection_generation"]
            fields = {
                "protocol_version": "远程管理协议版本",
                "request_id": "唯一操作编号",
                "target_host_id": "目标服务端设备编号",
                "session_generation": "当前管理会话代次",
                "operation": "固定服务端业务操作编号",
                "arguments": "审批标识、决定及 DSH 运行序号",
            }

            class FakeDshShadowSession:
                def __init__(self):
                    self.decisions = []

                async def decide_approval(self, approval_id, decision, run_seq):
                    self.decisions.append((approval_id, decision, run_seq))
                    return {"result": "accepted", "fields": {"result": "权威服务端已接受的决定"}}

            session = FakeDshShadowSession()
            self.runtime.shadow_session = session

            async def management(operation, arguments, *, target=host_id, session_generation=generation):
                from uuid import uuid4

                payload = {
                    "protocol_version": 1,
                    "request_id": uuid4().hex,
                    "target_host_id": target,
                    "session_generation": session_generation,
                    "operation": operation,
                    "arguments": arguments,
                    "fields": fields,
                }
                return (await self._signed_gateway_request(
                    private_key, device_id, "/v1/management", payload,
                ))[0]

            with patch("voice_terminal.server.http_api.DshShadowAgentSession", FakeDshShadowSession):
                with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": "isolated-host-token"}):
                    accepted = await management("dsh.approval.decide", {
                        "approval_id": "approval-9",
                        "decision": "allowed-until-silence",
                        "run_seq": 12,
                    })
                    assert accepted.status_code == 200, accepted.text
                    assert accepted.json()["status"] == 200
                    assert accepted.json()["body"]["result"] == "accepted"
                    assert session.decisions == [("approval-9", "allowed-until-silence", 12)]
                    assert "isolated-host-token" not in accepted.text

                    for arguments in (
                        {"approval_id": "approval-9", "decision": "allowed-once", "run_seq": True},
                        {"approval_id": "approval-9", "decision": "unknown", "run_seq": 12},
                        {"approval_id": "approval-9", "decision": "rejected", "run_seq": 12,
                         "url": "http://127.0.0.1/api/v1/settings/inference"},
                    ):
                        rejected = await management("dsh.approval.decide", arguments)
                        assert rejected.status_code == 422, rejected.text
                    arbitrary = await management("dsh.approval.decide?path=/internal/application/control", {
                        "approval_id": "approval-9", "decision": "rejected", "run_seq": 12,
                    })
                    assert arbitrary.status_code == 422
                    wrong_host = await management("dsh.approval.decide", {
                        "approval_id": "approval-9", "decision": "rejected", "run_seq": 12,
                    }, target="another-host")
                    assert wrong_host.status_code == 409
                    stale_session = await management("dsh.approval.decide", {
                        "approval_id": "approval-9", "decision": "rejected", "run_seq": 12,
                    }, session_generation=generation + 1)
                    assert stale_session.status_code == 409
                    assert session.decisions == [("approval-9", "allowed-until-silence", 12)]

                with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": ""}):
                    unavailable = await management("dsh.approval.decide", {
                        "approval_id": "approval-9", "decision": "rejected", "run_seq": 12,
                    })
                assert unavailable.status_code == 503
                assert session.decisions == [("approval-9", "allowed-until-silence", 12)]

        asyncio.run(scenario())

    def test_remote_management_wake_task_create_preserves_host_authorization_and_payload(self) -> None:
        from voice_terminal.server.http_api import _remote_management_request_spec

        value = {"text": "测试提醒", "at": "2026-11-17T12:00:00+08:00"}
        assert _remote_management_request_spec("wake_task.create", {"value": value}) == (
            "POST", "/internal/application/wake-tasks", value,
        )
        for arguments in ({}, {"value": []}, {"value": value, "task_id": "unexpected"}):
            with self.assertRaisesRegex(ValueError, "JSON 对象参数 value"):
                _remote_management_request_spec("wake_task.create", arguments)

    def test_remote_tts_asset_import_uses_dft_ticket_and_idempotent_library_commit(self) -> None:
        async def scenario():
            import hashlib
            import io
            import numpy as np
            import soundfile as sf
            from voice_terminal.server.http_api import device_file_transfer_id
            from voice_terminal.devices.file_transfer import MAX_CHUNK_BYTES

            private_key = Ed25519PrivateKey.generate()
            device_id = "tts-import-client"
            host_id = "tts-import-host"
            self.runtime.device_host_id = host_id
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000)
            opened, _ = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version":1,"capabilities":["file_transfer"]})
            assert opened.status_code == 200, opened.text
            lease = opened.json()
            connection = self.runtime.device_registry.connection(device_id)
            assert connection is not None
            model_id = self.runtime.config.tts.local_model

            async def management(request_id, operation, arguments, *, session_generation=None,
                                 expected_http_status=200):
                payload = {"protocol_version":1,"request_id":request_id,"target_host_id":host_id,
                    "session_generation":session_generation or lease["connection_generation"],
                    "operation":operation,"arguments":arguments,
                    "fields":{"protocol_version":"远程管理协议版本","request_id":"稳定操作编号",
                        "target_host_id":"目标服务端编号","session_generation":"当前设备会话代次",
                        "operation":"固定管理操作","arguments":"固定操作参数"}}
                response, _ = await self._signed_gateway_request(private_key, device_id, "/v1/management", payload)
                assert response.status_code == expected_http_status, response.text
                return response.json()

            sample_rate = 16_000
            time_axis = np.arange(sample_rate * 5, dtype=np.float32) / sample_rate
            signal = (0.15 * np.sin(2 * np.pi * 220 * time_axis)).astype(np.float32)
            wav = io.BytesIO()
            sf.write(wav, signal, sample_rate, format="WAV", subtype="PCM_16")
            content = wav.getvalue()
            digest = hashlib.sha256(content).hexdigest()
            parent_request_id = "tts-import-parent-1"
            prepare_arguments = {"model_id":model_id,"purpose":"source_and_asset",
                "filename":"reference.wav","expected_bytes":len(content),
                "parent_request_id":parent_request_id}
            prepared = await management("a" * 32, "tts.assets.import.prepare", prepare_arguments)
            assert prepared["status"] == 200, prepared["body"]
            ticket = prepared["body"]
            expected_transfer_id = device_file_transfer_id(connection, parent_request_id)
            assert ticket["transfer_id"] == expected_transfer_id
            assert ticket["expected_bytes"] == len(content)
            assert "path" not in ticket and "staging_dir" not in ticket

            store = self.runtime.device_file_transfer_store
            library = self.runtime.voice_library.for_model(model_id)
            channel = store.open_device_file_channel(transfer_id=expected_transfer_id, device_id=device_id,
                connection_generation=lease["connection_generation"],executor_generation=lease["executor_generation"],
                operation="file.sftp.upload",maximum_bytes=len(content))
            channel_key = {"transfer_id":expected_transfer_id,"device_id":device_id,
                "connection_generation":lease["connection_generation"],"executor_generation":lease["executor_generation"],
                "channel_token":channel["channel_token"]}
            for offset in range(0, len(content), MAX_CHUNK_BYTES):
                chunk = content[offset:offset + MAX_CHUNK_BYTES]
                store.append_device_channel_chunk(**channel_key,offset=offset,
                    data_base64=base64.b64encode(chunk).decode("ascii"),
                    chunk_sha256=hashlib.sha256(chunk).hexdigest())
            store.finish_device_channel_upload(**channel_key,expected_bytes=len(content),expected_sha256=digest)
            commit_arguments = {**prepare_arguments,"expected_sha256":digest,"transfer_id":expected_transfer_id}
            committed = await management("b" * 32,"tts.assets.import.commit",commit_arguments)
            assert committed["status"] == 200, committed["body"]
            result = committed["body"]
            assert set(result) == {"asset","source"}
            assert result["asset"]["asset_id"] != result["source"]["asset_id"]
            assert expected_transfer_id not in store._records
            before_count = len(self.runtime.voice_library.for_model(model_id)._index["assets"])
            repeated = await management("c" * 32,"tts.assets.import.commit",commit_arguments)
            assert repeated["status"] == 200, repeated["body"]
            assert repeated["body"] == result
            assert expected_transfer_id not in store._records
            assert len(self.runtime.voice_library.for_model(model_id)._index["assets"]) == before_count

            # 模拟 VoiceLibrary（音色库）已提交、HTTP 回执丢失但 DFT 暂存仍在的重放路径。
            replay_parent = "tts-import-replay-parent"
            replay_prepare = {**prepare_arguments, "purpose":"asset", "parent_request_id":replay_parent}
            replay_ticket_result = await management("2" * 32, "tts.assets.import.prepare", replay_prepare)
            assert replay_ticket_result["status"] == 200, replay_ticket_result["body"]
            replay_transfer_id = replay_ticket_result["body"]["transfer_id"]
            replay_channel = store.open_device_file_channel(transfer_id=replay_transfer_id, device_id=device_id,
                connection_generation=lease["connection_generation"],executor_generation=lease["executor_generation"],
                operation="file.sftp.upload",maximum_bytes=len(content))
            replay_channel_key = {"transfer_id":replay_transfer_id,"device_id":device_id,
                "connection_generation":lease["connection_generation"],"executor_generation":lease["executor_generation"],
                "channel_token":replay_channel["channel_token"]}
            for offset in range(0, len(content), MAX_CHUNK_BYTES):
                chunk = content[offset:offset + MAX_CHUNK_BYTES]
                store.append_device_channel_chunk(**replay_channel_key,offset=offset,
                    data_base64=base64.b64encode(chunk).decode("ascii"),
                    chunk_sha256=hashlib.sha256(chunk).hexdigest())
            store.finish_device_channel_upload(**replay_channel_key,expected_bytes=len(content),expected_sha256=digest)
            replay_binding = {"transfer_id":replay_transfer_id,"device_id":device_id,
                "connection_generation":lease["connection_generation"],"executor_generation":lease["executor_generation"],
                "parent_request_id":replay_parent,"model_id":model_id,"purpose":"asset",
                "filename":"reference.wav","expected_bytes":len(content),"expected_sha256":digest}
            replay_staged, replay_verified = store.read_tts_asset_import(**replay_binding)
            assert replay_verified == replay_binding
            durable_result = await asyncio.to_thread(library.commit_remote_import, replay_staged, **replay_binding)
            assert replay_transfer_id in store._records
            replay_commit_arguments = {**replay_prepare,"expected_sha256":digest,"transfer_id":replay_transfer_id}
            replayed = await management("3" * 32,"tts.assets.import.commit",replay_commit_arguments)
            assert replayed["status"] == 200 and replayed["body"] == durable_result
            assert replay_transfer_id not in store._records

            mismatched_transfer = {**commit_arguments,"transfer_id":"f" * 32}
            rejected = await management("d" * 32,"tts.assets.import.commit",mismatched_transfer)
            assert rejected["status"] == 409
            stale = await management("e" * 32,"tts.assets.import.commit",commit_arguments,
                session_generation=lease["connection_generation"] + 1, expected_http_status=409)
            assert "detail" in stale

            abort_parent = "tts-import-abort-1"
            abort_arguments = {"model_id":"aniflive","purpose":"source","filename":"abort.wav",
                "expected_bytes":len(content),"parent_request_id":abort_parent}
            abort_ticket = await management("f" * 32,"tts.assets.import.prepare",abort_arguments)
            assert abort_ticket["status"] == 200
            abort_id = abort_ticket["body"]["transfer_id"]
            aborted = await management("1" * 32,"tts.assets.import.abort",
                {"parent_request_id":abort_parent,"transfer_id":abort_id})
            assert aborted["status"] == 200 and aborted["body"]["aborted"] is True
            assert abort_id not in store._records

            closed, _ = await self._signed_gateway_request(private_key, device_id,"/v1/session/close",
                {"protocol_version":1,"connection_generation":lease["connection_generation"],
                 "executor_generation":lease["executor_generation"]})
            assert closed.status_code == 200

        token = "test-tts-import-internal-token"
        with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": token}):
            asyncio.run(scenario())

    def test_remote_tts_reference_uses_current_audio_output_without_file_transfer(self) -> None:
        async def scenario():
            import hashlib
            import io
            from types import SimpleNamespace
            import numpy as np
            import soundfile as sf

            private_key = Ed25519PrivateKey.generate()
            device_id = "tts-audio-client"
            host_id = "tts-audio-host"
            self.runtime.device_host_id = host_id
            self.runtime.device_trust_store.approve(device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000)
            opened, _ = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["audio_stream"]})
            assert opened.status_code == 200, opened.text
            connection = opened.json()
            registry = self.runtime.device_registry
            lease = registry.prepare_switch(device_id, registry.generation)
            assert registry.commit_switch(lease)
            assert "file_transfer" not in registry.connection(device_id).capabilities

            model_id = self.runtime.config.tts.local_model
            library = self.runtime.voice_library.for_model(model_id)
            short_audio = io.BytesIO()
            tone = 0.15 * np.sin(2 * np.pi * 220 * np.arange(80_000) / 16_000)
            sf.write(short_audio, tone, 16_000, format="WAV")
            asset_id = library.put_import(short_audio.getvalue(), "reference.wav")["asset_id"]
            pcm = b"\x23\x01" * 600_000
            library.put_high_fidelity_pcm(asset_id, pcm, sample_rate=48_000)
            hifi_path = library.high_fidelity_path(asset_id)
            assert hifi_path is not None and hifi_path.stat().st_size > 1024 * 1024
            with library._lock:
                library._index["assets"][asset_id]["remote_import"] = {
                    "high_fidelity_sha256": hashlib.sha256(hifi_path.read_bytes()).hexdigest(),
                }
            source_audio = io.BytesIO()
            sf.write(source_audio, np.frombuffer(pcm, dtype="<i2"), 48_000, format="WAV")
            source = library.put_source(source_audio.getvalue(), "long-original.wav")
            assert source["size_bytes"] > 1024 * 1024

            async def management(operation, arguments, *, generation=None, expected_status=200):
                payload = {
                    "protocol_version": 1, "request_id": os.urandom(16).hex(), "target_host_id": host_id,
                    "session_generation": generation or connection["connection_generation"],
                    "operation": operation, "arguments": arguments,
                    "fields": {"protocol_version":"协议版本", "request_id":"请求编号", "target_host_id":"目标服务端",
                        "session_generation":"设备会话代次", "operation":"音色业务操作", "arguments":"音色操作参数"},
                }
                response, _ = await self._signed_gateway_request(private_key, device_id, "/v1/management", payload)
                assert response.status_code == expected_status, response.text
                return response.json()

            async def play(identifier):
                response = await management("tts.operation.play", {"value": {"model": model_id, "asset_id": identifier}})
                assert response["status"] == 202, response["body"]
                started = response["body"]
                assert started["kind"] == "play" and started["result"] is None
                assert set(started) == {"operation_id", "kind", "state", "model", "result", "error", "playback_outcome"}
                operation_id = started["operation_id"]
                await asyncio.wait_for(self.runtime.tts_voice_operations._items[operation_id].task, 5)
                completed = await management("tts.operation.read", {"operation_id": operation_id})
                assert completed["status"] == 200, completed["body"]
                return completed["body"]

            async def enqueue(key, samples):
                assert await self.runtime._enqueue_player_audio(key, 48_000, samples)

            self.runtime.player = SimpleNamespace(sample_rate=48_000, channels=1, buffered_ms=0,
                allow_user_initiated=lambda _key: None, enqueue_async=enqueue,
                finish_stream_async=AsyncMock(), wait_until_output_complete=AsyncMock(),
                stop_and_snapshot_confirmed=AsyncMock(), has_started=lambda _key: True)
            with patch.object(self.runtime, "enqueue_remote_playback", new_callable=AsyncMock) as media:
                for identifier in (asset_id, source["asset_id"]):
                    media.reset_mock()
                    completed = await play(identifier)
                    assert completed["state"] == "succeeded", completed
                    assert completed["playback_outcome"] == "stopped"
                    assert completed["result"] == {"asset_id": identifier, "duration_ms": 12_500}
                    calls = media.await_args_list
                    assert len(calls) > 1 and b"".join(call.args[2] for call in calls) == pcm
                    assert all(call.args[:2] == (completed["operation_id"], 48_000)
                               and call.kwargs == {"delivery_device_id": device_id} for call in calls)

                media.reset_mock()
                damaged = bytearray(hifi_path.read_bytes())
                damaged[-1] ^= 1
                hifi_path.write_bytes(damaged)
                corrupt = await play(asset_id)
                assert corrupt["state"] == "failed" and "摘要" in corrupt["error"]
                media.assert_not_awaited()

            for arguments in ({"model": model_id, "asset_id": "../outside"},
                              {"model": model_id, "asset_id": "f" * 32},
                              {"model": model_id, "asset_id": asset_id, "path": "C:/private"}):
                bad = await management("tts.operation.play", {"value": arguments})
                assert bad["status"] == 422
            await management("tts.operation.play", {"value": {"model": model_id, "asset_id": asset_id}},
                             generation=connection["connection_generation"] + 1, expected_status=409)
            await management("tts.assets.audio.read", {"value": {"model_id": model_id, "asset_id": asset_id}},
                             expected_status=422)
            old_route = await self._request("POST", "/internal/application/tts-assets/audio/read", json={})
            assert old_route.status_code == 404

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
            offline = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                json=query_body, headers=headers)
            assert offline.status_code == 200 and offline.json()["task"] == action_response.json()["task"]

            reopened, _headers = await self._signed_gateway_request(private_key, device_id, "/v1/session/open",
                {"protocol_version": 1, "capabilities": ["file_transfer", "terminal_control"]})
            assert reopened.status_code == 200 and "desktop_control" not in reopened.json()["capabilities"]
            for response in (terminal_response, file_response):
                original_task = response.json()["task"]
                queried = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                    json={**query_body, "task_id": original_task["request_id"]}, headers=headers)
                assert queried.status_code == 200 and queried.json()["task"]["status"] == "succeeded"
            wrong_device = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                json={**query_body, "device_id": "local"}, headers=headers)
            assert wrong_device.status_code == 200 and wrong_device.json()["task"] is None
            stale = await self._request("POST", "/api/internal/dsh-bridge/device.task.query",
                json={**query_body, "activation_id": "stale"}, headers=headers)
            assert stale.status_code == 409
            await self.runtime.device_remote_sessions.close_all()
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

            source = bytes(range(256)) * (12 * 1024 + 1)
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
            assert "remote_path" not in upload_task["arguments"]
            assert upload_task["arguments"]["parent_request_id"] == read_body["request_id"]
            transfer_id = upload_task["arguments"]["transfer_id"]
            store = self.runtime.device_file_transfer_store
            channel = store.open_device_file_channel(transfer_id=transfer_id, device_id=device_id,
                connection_generation=lease["connection_generation"], executor_generation=lease["executor_generation"],
                operation="file.sftp.upload", maximum_bytes=len(source))
            channel_key = {"transfer_id":transfer_id,"device_id":device_id,
                "connection_generation":lease["connection_generation"],"executor_generation":lease["executor_generation"],
                "channel_token":channel["channel_token"]}
            for offset in range(0, len(source), 64 * 1024):
                part = source[offset:offset + 64 * 1024]
                store.append_device_channel_chunk(**channel_key, offset=offset,
                    data_base64=base64.b64encode(part).decode("ascii"),
                    chunk_sha256=__import__("hashlib").sha256(part).hexdigest())
            store.finish_device_channel_upload(**channel_key, expected_bytes=len(source), expected_sha256=source_sha)
            upload_receipt = {"path":f"transfer:{transfer_id}","bytes":len(source),
                "sha256":source_sha,"transfer_id":transfer_id}
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
            transfer_path = "/api/internal/dsh-bridge/device.file.transfer"
            read_request = {**transfer_common,"request_id":"remote-file-content-1","action":"read_stream"}
            stale = await self._request("POST", transfer_path,
                json={**read_request, "activation_id": "retired"}, headers=headers)
            assert stale.status_code == 409
            removed = await self._request("POST", transfer_path,
                json={**read_request, "action": "read_chunk", "offset": 0, "limit": 8192}, headers=headers)
            assert removed.status_code == 422
            content = await self._request("POST", transfer_path, json=read_request, headers=headers)
            assert content.status_code == 200
            assert content.headers["content-type"] == "application/octet-stream"
            assert int(content.headers["content-length"]) == len(source)
            assert content.content == source
            assert not list(store.root.iterdir())
            repeated = await self._request("POST", transfer_path, json=read_request, headers=headers)
            assert repeated.status_code == 409

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
            assert "remote_path" not in download_task["arguments"]
            assert download_task["arguments"]["parent_request_id"] == write_body["request_id"]
            download_channel = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id=device_id,
                connection_generation=lease["connection_generation"], executor_generation=lease["executor_generation"],
                operation="file.sftp.download", maximum_bytes=len(attachment), expected_sha256=attachment_sha)
            download_key = {"transfer_id":ticket["transfer_id"],"device_id":device_id,
                "connection_generation":lease["connection_generation"],"executor_generation":lease["executor_generation"],
                "channel_token":download_channel["channel_token"]}
            downloaded = bytearray()
            for offset in range(0, len(attachment), 64 * 1024):
                part = store.read_device_channel_chunk(**download_key, offset=offset, limit=64 * 1024)
                downloaded.extend(base64.b64decode(part["data_base64"], validate=True))
            assert bytes(downloaded) == attachment
            store.finish_device_channel_download(**download_key,
                expected_bytes=len(attachment), expected_sha256=attachment_sha)
            download_receipt = {"path":f"transfer:{ticket['transfer_id']}","bytes":len(attachment),
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

    def test_inbound_control_requires_desktop_auth_and_current_session_identity(self) -> None:
        async def scenario():
            from types import SimpleNamespace
            connection = SimpleNamespace(connected=True, connection_generation=3, executor_generation=4)
            token = "inbound-control-test-token"
            path = "/internal/application/device-inbound"
            with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": token}), \
                    patch.object(self.runtime.device_registry, "connection", return_value=connection), \
                    patch.object(self.runtime, "close_remote_device_session", new_callable=AsyncMock, return_value=True) as close:
                status_args = {"action": "status", "device_id": "client-a"}
                denied = await self._request("POST", path, json=status_args)
                assert denied.status_code == 403
                headers = {"Authorization": f"Bearer {token}"}
                status = await self._request("POST", path, json=status_args, headers=headers)
                assert status.status_code == 200
                assert status.json()["connected"] is True
                args = {"action": "disconnect", "device_id": "client-a",
                        "expected_core_session_id": self.runtime.core_session_id,
                        "expected_connection_generation": 3, "expected_executor_generation": 4}
                for field, bad in (("expected_core_session_id", "old-core"),
                                   ("expected_connection_generation", 2), ("expected_executor_generation", 3)):
                    stale = await self._request("POST", path, json={**args, field: bad}, headers=headers)
                    assert stale.status_code == 409
                    close.assert_not_awaited()
                invalid = await self._request("POST", path, json={**args, "command": "anything"}, headers=headers)
                assert invalid.status_code == 422
                result = await self._request("POST", path, json=args, headers=headers)
                assert result.status_code == 200 and result.json()["connected"] is False
                close.assert_awaited_once_with("client-a", 3)
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
                assert len(pairing_code) == 6 and pairing_code.isdigit()
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

    def test_revoking_audio_stream_only_device_closes_media_lease_and_recovers_local(self) -> None:
        async def scenario():
            class LocalCapture:
                def __init__(self) -> None:
                    self.running = False
                    self.diagnostics = {}

                async def start(self) -> None:
                    self.running = True

                async def stop(self) -> None:
                    self.running = False

                async def read_frame(self):
                    await asyncio.Future()

                def pending_frame_count(self) -> int:
                    return 0

                def read_pending_frame(self):
                    raise asyncio.QueueEmpty

                def preroll(self, _milliseconds: int) -> bytes:
                    return b""

            device_id = "audio-only-client"
            private_key = Ed25519PrivateKey.generate()
            self.runtime.device_trust_store.approve(
                device_id, self._ssh_public_key(private_key), trusted_at_ms=1_000,
            )
            capture_route = SelectableCapturePort(LocalCapture())
            self.runtime._capture_router = capture_route
            await capture_route.start()

            class ListeningController:
                def __init__(self, runtime) -> None:
                    self.runtime = runtime

                async def stop_listening(self) -> None:
                    self.runtime.state.listening = False
                    await capture_route.stop()

                async def start_listening(self) -> None:
                    await capture_route.start()
                    self.runtime.state.listening = True

            self.runtime.controller = ListeningController(self.runtime)
            self.runtime.state.listening = True

            connection = await self.runtime.open_remote_device_session(device_id, ["audio_stream"])
            lease = await self.runtime.prepare_interaction_switch(
                device_id, self.runtime.device_registry.generation,
            )
            stream = PcmInputStream(
                stream_id="stream-audio-only", core_session_id=self.runtime.core_session_id,
                lease=lease, registry=self.runtime.device_registry,
                map_device_time=lambda value: (10.0 + value / 1_000_000, 0.001),
            )
            capture = RemoteAudioCapture(stream)
            await self.runtime.select_remote_capture(capture, lease)
            await self.runtime.complete_remote_interaction_switch(capture, lease)
            assert self.runtime.device_registry.active == lease

            token = "audio-only-revoke-token"
            with patch.dict(os.environ, {"VOICE_APP_INTERNAL_TOKEN": token}):
                revoked = await self._request(
                    "POST", "/internal/application/device-pairing",
                    json={"operation": "revoke", "device_id": device_id},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert revoked.status_code == 200
            assert revoked.json()["trust"]["devices"][0]["state"] == "revoked"
            disconnected = self.runtime.device_registry.connection(device_id)
            assert disconnected is not None and disconnected.connected is False
            assert not self.runtime.device_registry.accepts(lease)
            active = self.runtime.device_registry.active
            assert active is not None and active.device_id == "local"
            assert capture_route.active_device_id == "local"
            assert capture.running is False

            late_stream = PcmInputStream(
                stream_id="stream-late-audio-only", core_session_id=self.runtime.core_session_id,
                lease=lease, registry=self.runtime.device_registry,
                map_device_time=lambda value: (20.0 + value / 1_000_000, 0.001),
            )
            late_frame = MediaFrame(
                stream_id="stream-late-audio-only", sequence=0, start_sample=0,
                sample_count=320, device_time_us=1_000,
                payload=b"\x00\x00" * 320, device_id=device_id,
                core_session_id=self.runtime.core_session_id,
                connection_generation=lease.connection_generation,
                executor_generation=lease.executor_generation,
                interaction_generation=lease.interaction_generation,
            )
            with self.assertRaisesRegex(MediaProtocolError, "交互设备租约已失效"):
                late_stream.accept(late_frame)
            with self.assertRaises(RemoteAudioCaptureError):
                capture.accept_frame(late_frame)

            await capture_route.stop()
            await self.runtime.device_remote_sessions.close_all()

        asyncio.run(scenario())

    def test_device_snapshot_does_not_report_expired_remote_session_as_connected(self) -> None:
        from voice_terminal.devices.remote_session import REMOTE_SESSION_IDLE_SECONDS

        async def scenario():
            hub = self.runtime.device_remote_sessions
            connection = await hub.open("client-presence", ["desktop_control"])
            try:
                initial = await self._request("GET", "/api/v1/devices")
                row = next(item for item in initial.json()["devices"] if item["device_id"] == "client-presence")
                assert row["connected"] is True
                hub._sessions["client-presence"].last_seen -= REMOTE_SESSION_IDLE_SECONDS + 1
                assert self.runtime.device_registry.connection("client-presence").connected is True
                expired = await self._request("GET", "/api/v1/devices")
                row = next(item for item in expired.json()["devices"] if item["device_id"] == "client-presence")
                assert row["connected"] is False
                assert row["connection_generation"] == connection.connection_generation
            finally:
                await hub.close_all()
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
            assert {"desktop_control", "file_transfer", "terminal_control"} <= set(devices[0]["capabilities"])
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
                    arguments={"x": 5, "y": 6, "hardware": False}, timeout_seconds=38.0,
                    expected_connection=connection)

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
                execute.side_effect = DeviceOutcomeUnknown("隔离测试没有收到设备回执")
                terminal = {**query_body, "request_id": "terminal-receipt-lost", "action": "create",
                            "terminal_id": "terminal-1"}
                del terminal["task_id"]
                unknown = await self._request("POST", "/api/internal/dsh-bridge/device.terminal",
                                              json=terminal, headers=headers)
                self.assertEqual(unknown.status_code, 504)
                self.assertIn(terminal["request_id"], unknown.json()["detail"])
            hub.unregister(session)
        asyncio.run(scenario())

    def test_dsh_capture_explicit_local_target_is_not_redirected_by_voice_switch(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from voice_terminal.devices.screen import ScreenCaptureRouter
        from tests.test_screen_capture import packet

        async def scenario():
            session = SimpleNamespace(voice_session_id="current", dsh_session_id="dsh", epoch=2,
                                      activation_id="active", prepare_callback_binding=AsyncMock())
            hub = self.runtime.dsh.bridge_hub
            hub.register(session)
            captured = packet()
            for image in captured["attachments"]:
                image["data"] = base64.b64decode(image.pop("base64"), validate=True)
            local = SimpleNamespace(config=SimpleNamespace(enabled=True), available=True, generation=1,
                                    lease_generation=Mock(return_value=0), invalidate=Mock(), consume=Mock(),
                                    grab=AsyncMock(return_value=captured))
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
            assert response.status_code == 200 and response.headers["content-type"] == "application/octet-stream"
            size = int.from_bytes(response.content[:4], "big")
            manifest = json.loads(response.content[4:4 + size])
            assert manifest["capture_id"] == captured["capture_id"]
            assert manifest["execution_device_id"] == "local"
            assert manifest["attachments"] == [{k: v for k, v in item.items() if k != "data"}
                                               for item in captured["attachments"]]
            assert response.content[4 + size:] == b"".join(image["data"] for image in captured["attachments"])
            local.grab.assert_awaited_once_with(source="dsh", scope="active", binary=True)
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
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.content == base64.b64decode(stored["attachments"][0]["base64"], validate=True)
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

    def test_debug_call_header_requires_debug_mode_and_configured_origin(self) -> None:
        async def preflight(origin):
            return await self._request("OPTIONS", "/api/v1/devices", headers={
                "Origin": origin, "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-voice-agent-call",
            })

        desktop_origin = "http://127.0.0.1:9123"
        self.assertEqual(asyncio.run(preflight(desktop_origin)).status_code, 400)
        with patch.dict(os.environ, {"VOICE_WEB_ORIGINS": desktop_origin}), \
                patch("voice_terminal.agent_debug.ENABLED", True):
            self.app = create_app(self.runtime)
        allowed = asyncio.run(preflight(desktop_origin))
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("x-voice-agent-call", allowed.headers["access-control-allow-headers"].lower())
        denied = asyncio.run(preflight("http://127.0.0.1:5173"))
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("access-control-allow-origin", denied.headers)

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
