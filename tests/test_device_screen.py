"""主语音和代理共享截图目标，切换期间不交付旧设备图片。"""
import asyncio
import base64
import io
from types import SimpleNamespace

import pytest

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

    async def grab(self, *, source, scope):
        self.calls.append((source, scope))
        self.entered.set()
        await self.release.wait()
        return {"capture_id": "local-image"}


def _activate(registry, name):
    lease = registry.prepare_switch(name, registry.generation)
    assert registry.commit_switch(lease)


def _png(width, height):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class FakeRemote:
    """FakeRemote（远端设备路由替身）：记录操作并返回固定设备回执。"""

    def __init__(self):
        self.calls = []

    async def execute(self, *, request_id, device_id, operation, arguments, timeout_seconds,
                      expected_connection):
        self.calls.append({"device_id": device_id, "operation": operation, "arguments": arguments})
        if operation == "tree":
            return SimpleNamespace(status="succeeded", result={"snapshot": {
                "rect": {"x": 100, "y": 50, "width": 400, "height": 300},
                "executablePath": "C:\\App.exe",
            }})
        return SimpleNamespace(status="succeeded", result={
            "pngBase64": _png(1920, 1080), "width": 1920, "height": 1080,
            "snapshot": {"rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
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


def test_remote_device_capture_uses_tree_and_shot():
    async def run():
        registry = DeviceRegistry()
        registry.connect("local")
        registry.connect("remote", capabilities=("desktop_control",))
        _activate(registry, "remote")
        local = ScreenCaptureService(ScreenCaptureConfig(), capture=lambda config: {"capture_id": "local"})
        remote = FakeRemote()
        router = ScreenCaptureRouter(local, registry, remote)
        try:
            assert router.available
            assert router.available_for("remote")
            assert router.available_for("local")  # 本机能力独立于租约归属。
            packet = await router.grab(source="dsh", scope="activation-a")
            assert packet["execution_device_id"] == "remote"
            assert [item["role"] for item in packet["attachments"]] == ["global", "local"]
            assert [call["operation"] for call in remote.calls] == ["tree", "shot"]
            assert remote.calls[0]["device_id"] == "remote"
            assert remote.calls[0]["arguments"] == {"maxElements": 1, "maxDepth": 1, "includePixels": False}
            assert remote.calls[1]["arguments"] == {"wholeScreen": True, "maxSide": 1920}
            assert packet["monitor_bounds"] == (0, 0, 1920, 1080)
            assert packet["crop_rect"] == (0, 0, 960, 640)
            assert packet["center_point"] == (300, 200)
            assert packet["executable"] == "C:\\App.exe"
            assert packet["attachments"][0]["sha256"] and packet["attachments"][1]["sha256"]
            # 未消费图片在有效期内的再次读取只复用，不重复派发设备操作。
            again = await router.grab(source="dsh", scope="activation-a")
            assert again["capture_id"] == packet["capture_id"]
            assert len(remote.calls) == 2
            # 其他来源范围独立捕获。
            await router.grab(source="llm", scope="session-a")
            assert len(remote.calls) == 4
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
