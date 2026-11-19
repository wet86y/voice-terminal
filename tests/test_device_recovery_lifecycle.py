"""远端输入替换、能力恢复与管理控制的跨对象生命周期回归。"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from voice_terminal.application.supervisor import ApplicationHost
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.devices.capture_router import SelectableCapturePort
from voice_terminal.devices.pcm_stream import PcmInputStream
from voice_terminal.devices.remote_capture import RemoteAudioCapture
from voice_terminal.devices.remote_playback import RemotePlaybackOutbox
from voice_terminal.devices.client_tts import RemoteClientTtsOutbox
from voice_terminal.server.product import ProductSettingsStore
from voice_terminal.server.websocket import SessionRuntime
from tests.test_device_media_endpoint import _LocalCapture


@pytest.mark.parametrize("client_tts", [False, True])
def test_old_media_cleanup_cannot_recover_local_over_a_new_stream_with_the_same_lease(tmp_path, client_tts):
    async def run():
        config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"))
        settings = ProductSettingsStore(path=tmp_path / "settings.json", config=config)
        runtime = SessionRuntime(config, product_settings=settings, runtime_data_dir=tmp_path / "runtime")
        runtime._capture_router = SelectableCapturePort(_LocalCapture())
        await runtime.open_remote_device_session("client-recovery", ["audio_stream", *(["client_tts"] if client_tts else [])])
        lease = await runtime.prepare_interaction_switch("client-recovery", runtime.device_registry.generation)

        async def attach(stream_id):
            capture = RemoteAudioCapture(PcmInputStream(
                stream_id=stream_id, core_session_id=runtime.core_session_id, lease=lease,
                registry=runtime.device_registry, map_device_time=lambda value: (value / 1_000_000, .001)))
            playback = RemotePlaybackOutbox(device_id=lease.device_id, core_session_id=runtime.core_session_id,
                connection_generation=lease.connection_generation, executor_generation=lease.executor_generation,
                interaction_generation=lease.interaction_generation, stream_id=stream_id)
            await runtime.select_remote_capture(capture, lease)
            await runtime.attach_remote_playback_outbox(lease, playback)
            tts = None
            if client_tts:
                tts = RemoteClientTtsOutbox(lease=lease, core_session_id=runtime.core_session_id, stream_id=stream_id)
                await runtime.attach_remote_client_tts_outbox(lease, tts)
            await runtime.complete_remote_interaction_switch(capture, lease)
            return capture, playback, tts

        old_capture, old_playback, old_tts = await attach("stream-old")
        new_capture, new_playback, new_tts = await attach("stream-new")
        if old_tts is not None:
            assert not await runtime.detach_remote_client_tts_outbox(lease, old_tts)
        assert not await runtime.detach_remote_playback_outbox(lease, old_playback)
        assert not await runtime.clear_remote_capture(old_capture, lease)
        assert not await runtime.abort_interaction_switch(lease)
        assert not await runtime.recover_local_after_remote_media_loss(lease, capture=old_capture)
        assert runtime.device_registry.active == lease and not new_playback.closed
        assert runtime._remote_media_captures[lease.device_id] is new_capture
        assert runtime.remote_media_control_ready(lease.device_id, lease.connection_generation)
        assert not runtime.remote_media_control_ready("client-other", lease.connection_generation)
        if new_tts is not None:
            assert runtime._remote_client_tts_outboxes[lease.device_id] is new_tts
        await runtime.clear_remote_capture(new_capture, lease)
        # 新流也已销毁后，旧所有者缓存不得永久阻挡本机恢复；恢复后新处理器收尾保持幂等。
        assert await runtime.recover_local_after_remote_media_loss(lease, capture=old_capture)
        assert await runtime.recover_local_after_remote_media_loss(lease, capture=new_capture)
        assert runtime.device_registry.active.device_id == "local" and new_playback.closed
        assert not runtime.remote_media_control_ready(lease.device_id, lease.connection_generation)
        await runtime.device_remote_sessions.close_all()

    asyncio.run(run())


def test_reported_capability_status_reaches_real_single_capability_recovery():
    async def run():
        app = ApplicationHost(Path.cwd())
        try:
            alive = Mock()
            alive.poll.return_value = None
            app.processes["voice"] = alive
            app.state.update(startup_complete=True)
            row = {"id": "tts", "label": "语音合成", "state": "ERROR", "blocking": True,
                   "phase": "合成失败", "error": "本地模型暂不可用", "retry": "tts"}
            data = {"speaker": {"state": "disabled"}, "dsh": {"state": "disabled"}, "capabilities": [row]}
            with patch.object(app, "http", AsyncMock(return_value=data)):
                await app.delegated_status()
            assert app.capability_rows == [row]
            assert app.state.snapshot()["unavailable_capabilities"] == [row]
            assert app._pending_capability_row()["id"] == "tts"
            with patch.object(app, "voice_control", AsyncMock(return_value={"ok": True})) as control, \
                 patch.object(app, "probe", AsyncMock(return_value=True)), \
                 patch.object(app, "http", AsyncMock(return_value=data)), \
                 patch.object(app, "_refresh_resource_issues", return_value={}):
                await app.retry("voice")
            assert control.await_args.args == ("capability-retry",)
            assert control.await_args.kwargs["payload"] == {"capability": "tts"}
            alive.request_stop.assert_not_called()
        finally:
            app.outer_job.close()

    asyncio.run(run())
