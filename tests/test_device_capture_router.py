"""可切换采集路由的启停、待读帧取消和显式设备选择回归。"""
import asyncio

import pytest

from voice_terminal.controller.turn_workspace import AudioFrame
from voice_terminal.devices.capture_router import CaptureRouteUnavailable, SelectableCapturePort


class _CapturePort:
    """_CapturePort（可控采集端口测试替身）。"""

    def __init__(self, *, fail_start: bool = False, read_error: Exception | None = None) -> None:
        self._frames: asyncio.Queue[AudioFrame] = asyncio.Queue()  # _frames（有界测试帧队列）。
        self.running = False  # running（端口采集状态）。
        self.diagnostics: dict = {}  # diagnostics（测试诊断快照）。
        self.starts = 0  # starts（启动调用数）。
        self.stops = 0  # stops（停止调用数）。
        self.fail_start = fail_start  # fail_start（是否模拟启动失败）。
        self.read_error = read_error  # read_error（可选读取故障）。

    async def start(self) -> None:
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("capture start failed")
        self.running = True

    async def stop(self) -> None:
        self.stops += 1
        self.running = False

    async def read_frame(self) -> AudioFrame:
        if self.read_error is not None:
            raise self.read_error
        return await self._frames.get()

    async def feed(self, frame: AudioFrame) -> None:
        await self._frames.put(frame)

    def pending_frame_count(self) -> int:
        return self._frames.qsize()

    def read_pending_frame(self) -> AudioFrame:
        return self._frames.get_nowait()

    def preroll(self, _milliseconds: int) -> bytes:
        return b"local-preroll"


def _frame(sample: int = 0) -> AudioFrame:
    return AudioFrame(bytes(640), sample, 10.0 + sample / 16_000, 0.001)


def test_capture_router_starts_local_and_switches_waiter_to_remote_frame():
    async def run():
        local = _CapturePort()
        remote = _CapturePort()
        route = SelectableCapturePort(local)
        await route.start()
        pending = asyncio.create_task(route.read_frame())
        await asyncio.sleep(0)

        await route.select("device-remote", remote)
        await remote.feed(_frame(320))
        result = await asyncio.wait_for(pending, timeout=1)

        assert result.start_sample == 320
        assert route.active_device_id == "device-remote"
        assert local.starts == 1 and local.stops == 1
        assert remote.starts == 1 and remote.running
        await route.stop()
        assert remote.stops == 1

    asyncio.run(run())


def test_capture_router_does_not_fallback_after_remote_read_failure():
    async def run():
        local = _CapturePort()
        remote = _CapturePort(read_error=RuntimeError("remote stream lost"))
        route = SelectableCapturePort(local)
        await route.start()
        await route.select("device-remote", remote)

        with pytest.raises(RuntimeError, match="remote stream lost"):
            await route.read_frame()

        assert route.active_device_id == "device-remote"
        assert local.starts == 1
        assert remote.starts == 1
        await route.stop()

    asyncio.run(run())


def test_capture_router_failed_switch_leaves_no_implicit_local_fallback():
    async def run():
        local = _CapturePort()
        remote = _CapturePort(fail_start=True)
        route = SelectableCapturePort(local)
        await route.start()

        with pytest.raises(RuntimeError, match="capture start failed"):
            await route.select("device-remote", remote)

        assert route.active_device_id is None
        assert route.running is False
        assert local.stops == 1
        assert local.starts == 1
        with pytest.raises(CaptureRouteUnavailable):
            await route.read_frame()

    asyncio.run(run())


def test_capture_router_requires_an_explicit_lease_side_local_selection():
    async def run():
        local = _CapturePort()
        remote = _CapturePort()
        route = SelectableCapturePort(local)
        await route.start()
        await route.select("device-remote", remote)
        await route.select_local()

        assert route.active_device_id == "local"
        assert local.starts == 2
        assert remote.stops == 1

    asyncio.run(run())
