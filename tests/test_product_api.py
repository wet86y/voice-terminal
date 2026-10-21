from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import httpx
from unittest.mock import AsyncMock, PropertyMock, patch

from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.config import COMPUTE_MODEL_IDS
from voice_terminal.model_registry.service import ModelRegistryService
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime, create_app
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

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

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
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(return_value=saved_compute)) as switch:
            saved = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"model_id": "emotion", "device": "cuda"},
            ))
        self.assertEqual(saved.status_code, 200)
        self.assertFalse(saved.json()["restart_required"])
        self.assertFalse(saved.json()["pending"])
        self.assertEqual(saved.json()["effective_from"], "immediate")
        switch.assert_awaited_once_with({"model_id": "emotion", "device": "cuda"})
        self.assertEqual(saved.json()["settings"]["compute"]["devices"], devices)
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
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(
                side_effect=ValueError("emotion 暂不能使用 GPU：后端未安装"))):
            response = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"model_id": "emotion", "device": "cuda"},
            ))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.runtime.product_settings.snapshot()["compute"]["devices"]["emotion"],
            previous,
        )

    def test_compute_api_reports_model_switch_and_rollback_failures(self) -> None:
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(
                side_effect=RuntimeError("设备切换失败且恢复原模型失败，已停止监听：工作进程加载失败"))):
            response = asyncio.run(self._request(
                "PUT", "/api/v1/product/settings/compute", json={"model_id": "paraformer", "device": "cuda"},
            ))
        self.assertEqual(response.status_code, 503)
        self.assertIn("恢复原模型失败", response.json()["detail"])

    def test_compute_refresh_requests_new_environment_check(self) -> None:
        with patch.object(self.runtime, "compute_status", new=AsyncMock(return_value={"models": {}, "hardware": {}})) as probe:
            response = asyncio.run(self._request("GET", "/api/v1/product/compute-status?refresh=true"))
        self.assertEqual(response.status_code, 200)
        probe.assert_awaited_once_with(refresh_capabilities=True)

    def test_compute_rejects_gpu_when_framework_exists_but_model_file_is_missing(self) -> None:
        previous = self.runtime.product_settings.snapshot()["compute"]["devices"]["paraformer"]
        with patch.object(self.runtime, "switch_compute_device", new=AsyncMock(
                side_effect=ValueError("Paraformer 资源未配置完整"))):
            response = asyncio.run(self._request("PUT", "/api/v1/product/settings/compute",
                json={"model_id": "paraformer", "device": "cuda"}))
        self.assertEqual(response.status_code, 422)
        self.assertIn("资源未配置完整", response.json()["detail"])
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
