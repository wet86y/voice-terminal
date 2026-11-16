"""主语音和代理共享截图目标，切换期间不交付旧设备图片。"""
import asyncio

import pytest

from voice_terminal.devices.registry import DeviceRegistry
from voice_terminal.devices.screen import ScreenCaptureRouter


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
        with pytest.raises(RuntimeError, match="尚不可用"):
            await router.grab(source="dsh", scope="activation")
        assert local.calls == [("llm", "session"), ("dsh", "activation")]
        assert len(local.invalidations) == 3
        assert local.invalidations[0] == {"source": "llm", "scope": "session"}
        assert local.invalidations[1] == {"source": "dsh", "scope": "activation"}
        assert router.available_for("local")
        assert (await router.grab(source="dsh", scope="activation", device_id="local"))["capture_id"] == "local-image"
        assert not router.available_for("remote")
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
