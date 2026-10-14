"""显卡性能模式请求（NVML PowerMizer）测试：免提权加持、幂等、可还原、缺库降级。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from voice_terminal.gpu_performance import (
    POWER_MIZER_ADAPTIVE,
    POWER_MIZER_MAX_PERFORMANCE,
    GpuPerformanceMode,
)
from voice_terminal.providers.audio_cpp_tts import AudioCppTtsClient


class FakeNvml:
    """NVML（英伟达管理库）替身：只实现本模块用到的接口，并记录调用。"""

    def __init__(self, *, current=0, supported=0b0111, set_code=0, init_code=0,
                 clock=780, power_mw=23000, utilization=93):
        self.current = current
        self.supported = supported
        self.set_code = set_code
        self.init_code = init_code
        self.clock = clock
        self.power_mw = power_mw
        self.utilization = utilization
        self.calls = []

    def nvmlInit_v2(self):
        self.calls.append(("init",))
        return self.init_code

    def nvmlDeviceGetHandleByIndex_v2(self, index, handle):
        self.calls.append(("handle", index.value))
        return 0

    def nvmlDeviceGetPowerMizerMode_v1(self, handle, modes):
        self.calls.append(("get",))
        modes._obj.currentMode = self.current
        modes._obj.mode = self.current
        modes._obj.supportedModes = self.supported
        return 0

    def nvmlDeviceSetPowerMizerMode_v1(self, handle, modes):
        self.calls.append(("set", modes._obj.mode))
        if self.set_code:
            return self.set_code
        self.current = modes._obj.mode
        return 0

    def nvmlDeviceGetClockInfo(self, handle, kind, value):
        value._obj.value = self.clock
        return 0

    def nvmlDeviceGetPowerUsage(self, handle, value):
        value._obj.value = self.power_mw
        return 0

    def nvmlDeviceGetUtilizationRates(self, handle, value):
        value._obj.gpu = self.utilization
        value._obj.memory = 0
        return 0


def _missing(name):
    raise OSError(name)


def test_disabled_mode_never_touches_driver():
    library = FakeNvml()
    mode = GpuPerformanceMode(enabled=False, library=library)
    status = mode.apply()
    assert status["enabled"] is False
    assert status["applied"] is False
    assert library.calls == []
    assert mode.restore()["applied"] is False


def test_apply_requests_max_performance_and_restore_returns_previous():
    library = FakeNvml(current=POWER_MIZER_ADAPTIVE)
    mode = GpuPerformanceMode(library=library)

    applied = mode.apply()
    assert applied["applied"] is True
    assert applied["mode"] == "prefer-maximum-performance"
    assert applied["previous_mode"] == "adaptive"
    assert library.current == POWER_MIZER_MAX_PERFORMANCE

    restored = mode.restore()
    assert restored["applied"] is False
    assert library.current == POWER_MIZER_ADAPTIVE


def test_apply_is_idempotent_and_keeps_original_mode():
    library = FakeNvml(current=POWER_MIZER_ADAPTIVE)
    mode = GpuPerformanceMode(library=library)

    mode.apply()
    mode.apply()
    mode.apply()
    assert [call for call in library.calls if call[0] == "set"] == [("set", POWER_MIZER_MAX_PERFORMANCE)]
    assert mode.status()["previous_mode"] == "adaptive"
    mode.restore()
    assert library.current == POWER_MIZER_ADAPTIVE


def test_unsupported_device_is_tolerated():
    library = FakeNvml(set_code=3)
    mode = GpuPerformanceMode(library=library)
    status = mode.apply()
    assert status["applied"] is False
    assert status["last_error"] == "nvml-not-supported"
    assert mode.restore()["applied"] is False


def test_missing_library_is_tolerated():
    mode = GpuPerformanceMode(loader=_missing)
    status = mode.apply()
    assert status["available"] is False
    assert status["last_error"] == "nvml-library-missing"
    assert status["applied"] is False


def test_permission_denied_is_reported_without_raising():
    library = FakeNvml(set_code=4)
    mode = GpuPerformanceMode(library=library)
    assert mode.apply()["last_error"] == "nvml-no-permission"


def test_snapshot_reads_clock_power_and_utilization():
    mode = GpuPerformanceMode(library=FakeNvml(clock=2445, power_mw=43210, utilization=87))
    snapshot = mode.snapshot().as_dict()
    assert snapshot == {"clock_mhz": 2445, "sm_clock_mhz": 2445, "power_w": 43.21,
                        "utilization_percent": 87}


def test_status_lists_supported_modes():
    mode = GpuPerformanceMode(library=FakeNvml())
    mode.apply()
    assert mode.status()["supported_modes"] == ["adaptive", "prefer-maximum-performance", "auto"]


def test_client_requests_performance_when_ready_and_restores_on_close():
    async def scenario():
        library = FakeNvml()
        server = SimpleNamespace(
            base_url="http://127.0.0.1:18881", model_id="voxcpm1-stream", native_sample_rate=16000,
            local_model="voxcpm1", capabilities={}, ensure_ready=AsyncMock(),
            close=AsyncMock(), status_snapshot=lambda: {"local_model": "voxcpm1"},
        )
        client = AudioCppTtsClient(server, gpu_max_performance=False)
        client.gpu_performance = GpuPerformanceMode(library=library)

        await client.ensure_ready()
        assert library.current == POWER_MIZER_MAX_PERFORMANCE
        assert client.model_status()["gpu_performance"]["applied"] is True

        await client.close()
        assert library.current == POWER_MIZER_ADAPTIVE
        assert client.gpu_performance.status()["applied"] is False

    asyncio.run(scenario())
