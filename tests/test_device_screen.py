"""主语音和代理共享截图目标，切换期间不交付旧设备图片。"""
import asyncio
import base64
import hashlib
import io
from types import SimpleNamespace

import pytest

from voice_terminal.devices.file_transfer import DeviceFileTransferStore
from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.screen import ScreenCaptureRouter
from voice_terminal.screen_capture import ScreenCaptureConfig, ScreenCaptureService


class LocalScreen:
    def __init__(self):
        self.available = True
        self.calls = []
        self.invalidations = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def invalidate(self, **kwargs):
        self.invalidations.append(kwargs)

    async def grab(self, *, source, scope, binary=False):
        self.calls.append((source, scope))
        self.entered.set()
        await self.release.wait()
        return {"capture_id": "local-image"}


def _activate(registry, name):
    lease = registry.prepare_switch(name, registry.generation)
    assert registry.commit_switch(lease)


def _images():
    import random
    from PIL import Image
    global_image = io.BytesIO()
    local_image = io.BytesIO()
    Image.new("RGB", (1280, 720), (12, 34, 56)).save(global_image, format="JPEG")
    pixels = random.Random(81).randbytes(960 * 640 * 3)
    Image.frombytes("RGB", (960, 640), pixels).save(local_image, format="PNG")
    return global_image.getvalue(), local_image.getvalue()


class FakeRemote:
    """FakeRemote（远端设备路由替身）：记录操作并返回固定设备回执。"""

    def __init__(self, files, *, tamper=False):
        self.calls = []
        self.files = files
        self.images = _images()
        self.tamper = tamper

    async def execute(self, *, request_id, device_id, operation, arguments, timeout_seconds,
                      expected_connection):
        self.calls.append({"device_id": device_id, "operation": operation, "arguments": arguments})
        assert operation == "screen.capture_file"
        raw = b"".join(self.images)
        assert len(raw) > 512 * 1024
        ticket = {key: getattr(expected_connection, key)
                  for key in ("device_id", "connection_generation", "executor_generation")}
        ticket["transfer_id"] = arguments["transfer_id"]
        channel = self.files.open_device_file_channel(**ticket, operation="file.sftp.upload",
                                                      maximum_bytes=len(raw))
        channel_args = {**ticket, "channel_token": channel["channel_token"]}
        assert channel["total_bytes"] == len(raw)
        for offset in range(0, len(raw), 65536):
            chunk = raw[offset:offset + 65536]
            self.files.append_device_channel_chunk(**channel_args, offset=offset,
                data_base64=base64.b64encode(chunk).decode("ascii"),
                chunk_sha256=hashlib.sha256(chunk).hexdigest())
        sha256 = hashlib.sha256(raw).hexdigest()
        self.files.finish_device_channel_upload(**channel_args, expected_bytes=len(raw), expected_sha256=sha256)
        return SimpleNamespace(status="succeeded", result={
            "transfer_id": arguments["transfer_id"], "bytes": len(raw),
            "sha256": "0" * 64 if self.tamper else sha256,
            "capture": {
                "monitor_width": 1280, "monitor_height": 720, "center_x": 300, "center_y": 200,
                "center_source": "window_center", "executable": "C:\\App.exe",
                "crop_left": 0, "crop_top": 0, "crop_width": 960, "crop_height": 640,
                "global_bytes": len(self.images[0]),
            },
        })


def test_main_and_agent_share_local_target_and_remote_never_falls_back():
    async def run():
        registry = DeviceRegistry()
        registry.connect("local")
        registry.connect("remote")
        _activate(registry, "local")
        local = LocalScreen()
        router = ScreenCaptureRouter(local, registry)
        assert router.available
        assert (await router.grab(source="llm", scope="session"))["capture_id"] == "local-image"
        assert (await router.grab(source="dsh", scope="activation"))["capture_id"] == "local-image"
        _activate(registry, "remote")
        assert not router.available
        with pytest.raises(RuntimeError, match="屏幕捕获不可用"):
            await router.grab(source="dsh", scope="activation")
        assert local.calls == [("llm", "session"), ("dsh", "activation")]
        assert len(local.invalidations) == 3
        assert local.invalidations[0] == {"source": "llm", "scope": "session"}
        assert local.invalidations[1] == {"source": "dsh", "scope": "activation"}
        assert router.available_for("local")
        assert (await router.grab(source="dsh", scope="activation", device_id="local"))["capture_id"] == "local-image"
        assert not router.available_for("remote")
    asyncio.run(run())


def test_remote_device_capture_uses_file_channel_without_reencoding(tmp_path):
    async def run():
        registry = DeviceRegistry()
        registry.connect("local")
        registry.connect("remote", capabilities=("desktop_control", "file_transfer"))
        _activate(registry, "remote")
        local = ScreenCaptureService(ScreenCaptureConfig(), capture=lambda config: {"capture_id": "local"})
        files = DeviceFileTransferStore(tmp_path)
        remote = FakeRemote(files)
        router = ScreenCaptureRouter(local, registry, remote, files)
        try:
            assert not router.available
            registry.connect("remote", capabilities=("desktop_control", "file_transfer", "screen_capture"))
            _activate(registry, "remote")
            assert router.available
            assert router.available_for("remote")
            assert router.available_for("local")  # 本机能力独立于租约归属。
            packet = await router.grab(source="dsh", scope="activation-a", binary=True)
            assert packet["execution_device_id"] == "remote"
            assert [item["role"] for item in packet["attachments"]] == ["global", "local"]
            assert [call["operation"] for call in remote.calls] == ["screen.capture_file"]
            assert remote.calls[0]["device_id"] == "remote"
            assert remote.calls[0]["arguments"]["max_side"] == 1920
            assert packet["monitor_bounds"] == (0, 0, 1280, 720)
            assert packet["crop_rect"] == (0, 0, 960, 640)
            assert packet["center_point"] == (300, 200)
            assert packet["executable"] == "C:\\App.exe"
            assert tuple(item["data"] for item in packet["attachments"]) == remote.images
            assert all("base64" not in item for item in packet["attachments"])
            assert not list(files.root.iterdir())
            # 未消费图片在有效期内的再次读取只复用，不重复派发设备操作。
            again = await router.grab(source="dsh", scope="activation-a")
            assert again["capture_id"] == packet["capture_id"]
            assert tuple(base64.b64decode(item["base64"]) for item in again["attachments"]) == remote.images
            assert len(remote.calls) == 1
            # 其他来源范围独立捕获。
            await router.grab(source="llm", scope="session-a")
            assert len(remote.calls) == 2
            remote.tamper = True
            with pytest.raises(RuntimeError, match="回执与双图上传票据不匹配"):
                await router.grab(source="dsh", scope="tampered")
            assert not list(files.root.iterdir())
        finally:
            local.close()
    asyncio.run(run())


def test_explicit_target_stays_fixed_when_voice_device_changes():
    async def run():
        registry = DeviceRegistry()
        registry.connect("local")
        registry.connect("remote")
        _activate(registry, "local")
        local = LocalScreen()
        local.release.clear()
        router = ScreenCaptureRouter(local, registry)
        pending = asyncio.create_task(router.grab(source="dsh", scope="a", device_id="local"))
        await local.entered.wait()
        _activate(registry, "remote")
        local.release.set()
        assert (await pending)["capture_id"] == "local-image"
    asyncio.run(run())


def test_switch_during_capture_invalidates_old_result():
    async def run():
        registry = DeviceRegistry()
        registry.connect("local")
        registry.connect("remote")
        _activate(registry, "local")
        local = LocalScreen()
        local.release.clear()
        router = ScreenCaptureRouter(local, registry)
        pending = asyncio.create_task(router.grab(source="llm", scope="session"))
        await local.entered.wait()
        _activate(registry, "remote")
        local.release.set()
        with pytest.raises(RuntimeError, match="交互设备已变化"):
            await pending
        assert local.invalidations[-1] == {"source": "llm", "scope": "session"}
    asyncio.run(run())
