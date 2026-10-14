"""NVIDIA（英伟达）PowerMizer（性能模式）请求：本地合成期间让显卡优先最大性能。

部分机器上会出现“合成期间核心频率停在低频、驱动把降频原因标成 GPU Idle（空闲）”的状态，
实时因子因此退化到 1 以上（详见 docs/local-tts-rtf-branch-comparison-20261014.md）。这里用官方
NVML（英伟达管理库）接口把**本进程**的 PowerMizer 模式设为 PREFER_MAXIMUM_PERFORMANCE
（优先最大性能），效果等价于 NVIDIA 控制面板的“电源管理模式 = 优先最大性能”。

本机实测性质（2026-10-14，RTX 4060 Laptop / 驱动 616.56）：

- 该设置**不需要管理员权限**；锁定频率的 ``nvmlDeviceSetGpuLockedClocks`` 则需要（实测返回无权限）。
- 设置值按进程记录（另一个进程读到的仍是原值），但**整卡生效**：旁路进程加持后，
  整卡空闲频率从 210 兆赫升到 2370 兆赫，进程退出后回到 210 兆赫。
- 因此进程退出即自动还原，不会给用户机器留下持久改动。

没有 NVIDIA 驱动、接口缺失、设备不支持或权限不足时全部降级为无操作，绝不影响合成。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# PowerMizer 模式取值（NVML_POWER_MIZER_MODE_*）。
POWER_MIZER_ADAPTIVE = 0  # 按利用率调频。
POWER_MIZER_MAX_PERFORMANCE = 1  # 在温度等约束内把频率提到最高。
POWER_MIZER_AUTO = 2  # 驱动自行控制。
POWER_MIZER_CONSISTENT = 3  # 锁基准频率。

POWER_MIZER_NAMES = {
    POWER_MIZER_ADAPTIVE: "adaptive",
    POWER_MIZER_MAX_PERFORMANCE: "prefer-maximum-performance",
    POWER_MIZER_AUTO: "auto",
    POWER_MIZER_CONSISTENT: "prefer-consistent-performance",
}

_NVML_ERRORS = {
    0: "success",
    1: "uninitialized",
    2: "invalid-argument",
    3: "not-supported",
    4: "no-permission",
    5: "already-initialized",
    6: "not-found",
    7: "insufficient-size",
    8: "insufficient-power",
    9: "driver-not-loaded",
    10: "timeout",
    12: "library-not-found",
    13: "function-not-found",
    15: "gpu-is-lost",
    16: "reset-required",
    17: "operating-system",
    18: "gpu-not-ready",
    19: "invalid-state",
    999: "unknown",
}

_LIBRARY_NAMES = ("nvml.dll", "libnvidia-ml.so.1", "libnvidia-ml.so")
_CLOCK_GRAPHICS = 0  # NVML_CLOCK_GRAPHICS。
_CLOCK_SM = 1  # NVML_CLOCK_SM。


class _PowerMizerModes(ctypes.Structure):
    """nvmlDevicePowerMizerModes_v1_t（性能模式结构）：currentMode/mode/supportedModes。"""

    _fields_ = [
        ("currentMode", ctypes.c_uint),
        ("mode", ctypes.c_uint),
        ("supportedModes", ctypes.c_uint),
    ]


class _Utilization(ctypes.Structure):
    """nvmlUtilization_t（利用率结构）。"""

    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


@dataclass(frozen=True)
class GpuSnapshot:
    """一次只读采样；字段为 None 表示该接口不可用。"""

    clock_mhz: int | None = None
    sm_clock_mhz: int | None = None
    power_w: float | None = None
    utilization_percent: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock_mhz": self.clock_mhz,
            "sm_clock_mhz": self.sm_clock_mhz,
            "power_w": self.power_w,
            "utilization_percent": self.utilization_percent,
        }


class GpuPerformanceMode:
    """按进程请求显卡优先最大性能；不可用时安全跳过。"""

    def __init__(self, *, enabled: bool = True, library: Any | None = None,
                 loader: Callable[[str], Any] | None = None, device_index: int = 0) -> None:
        self.enabled = bool(enabled)
        self._device_index = int(device_index)
        self._loader = loader or ctypes.CDLL
        self._library = library
        self._handle: Any | None = None
        self._ready = library is not None
        self._bound = False
        self._applied = False
        self._current_mode: int | None = None
        self._previous_mode: int | None = None
        self._supported_modes: int | None = None
        self._last_error: str | None = None
        self._last_action: str = "idle"

    # -- 内部 ---------------------------------------------------------------

    def _bind(self, library: Any) -> None:
        """给真实 NVML 动态库声明签名；测试用的替身对象跳过。"""
        for name, argtypes in (
            ("nvmlInit_v2", []),
            ("nvmlDeviceGetHandleByIndex_v2", [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]),
            ("nvmlDeviceGetPowerMizerMode_v1", [ctypes.c_void_p, ctypes.POINTER(_PowerMizerModes)]),
            ("nvmlDeviceSetPowerMizerMode_v1", [ctypes.c_void_p, ctypes.POINTER(_PowerMizerModes)]),
            ("nvmlDeviceGetClockInfo", [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]),
            ("nvmlDeviceGetPowerUsage", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]),
            ("nvmlDeviceGetUtilizationRates", [ctypes.c_void_p, ctypes.POINTER(_Utilization)]),
        ):
            function = getattr(library, name, None)
            if function is None:
                continue
            with contextlib.suppress(AttributeError, TypeError, ValueError):
                function.argtypes = argtypes
                function.restype = ctypes.c_int

    def _load(self) -> Any | None:
        if self._library is not None:
            return self._library
        for name in _LIBRARY_NAMES:
            try:
                self._library = self._loader(name)
            except OSError:
                continue
            return self._library
        self._last_error = "nvml-library-missing"
        return None

    def _ensure_handle(self) -> bool:
        """装载动态库并取得设备句柄；任何一步失败都只记录原因。"""
        if self._handle is not None:
            return True
        library = self._load()
        if library is None:
            return False
        if not self._bound:
            self._bind(library)
            self._bound = True
        init = getattr(library, "nvmlInit_v2", None) or getattr(library, "nvmlInit", None)
        if init is None:
            self._last_error = "nvml-init-missing"
            return False
        code = int(init())
        if code != 0:
            self._last_error = self._error_name(code)
            return False
        getter = getattr(library, "nvmlDeviceGetHandleByIndex_v2", None)
        if getter is None:
            self._last_error = "nvml-handle-api-missing"
            return False
        handle = ctypes.c_void_p()
        code = int(getter(ctypes.c_uint(self._device_index), ctypes.byref(handle)))
        if code != 0:
            self._last_error = self._error_name(code)
            return False
        self._handle = handle
        self._ready = True
        return True

    @staticmethod
    def _error_name(code: int) -> str:
        return f"nvml-{_NVML_ERRORS.get(code, code)}"

    @staticmethod
    def _mode_name(mode: int | None) -> str | None:
        return None if mode is None else POWER_MIZER_NAMES.get(mode, f"mode-{mode}")

    def _read_modes(self) -> tuple[int, int, int] | None:
        library = self._library
        getter = getattr(library, "nvmlDeviceGetPowerMizerMode_v1", None)
        if getter is None:
            self._last_error = "nvml-powermizer-api-missing"
            return None
        modes = _PowerMizerModes()
        code = int(getter(self._handle, ctypes.byref(modes)))
        if code != 0:
            self._last_error = self._error_name(code)
            return None
        self._current_mode = int(modes.currentMode)
        self._supported_modes = int(modes.supportedModes)
        return self._current_mode, int(modes.mode), int(modes.supportedModes)

    def _write_mode(self, mode: int) -> bool:
        library = self._library
        setter = getattr(library, "nvmlDeviceSetPowerMizerMode_v1", None)
        if setter is None:
            self._last_error = "nvml-powermizer-setter-missing"
            return False
        target = _PowerMizerModes()
        target.mode = mode
        code = int(setter(self._handle, ctypes.byref(target)))
        if code != 0:
            self._last_error = self._error_name(code)
            return False
        self._last_error = None
        return True

    # -- 对外 ---------------------------------------------------------------

    def snapshot(self) -> GpuSnapshot:
        """只读采样当前频率、功耗与利用率；接口不可用时对应字段为空。"""
        if not self._ensure_handle():
            return GpuSnapshot()
        library = self._library
        clock = sm_clock = power = utilization = None
        getter = getattr(library, "nvmlDeviceGetClockInfo", None)
        if getter is not None:
            for clock_type, attribute in ((_CLOCK_GRAPHICS, "clock"), (_CLOCK_SM, "sm")):
                value = ctypes.c_uint()
                if int(getter(self._handle, ctypes.c_uint(clock_type), ctypes.byref(value))) == 0:
                    if attribute == "clock":
                        clock = int(value.value)
                    else:
                        sm_clock = int(value.value)
        power_getter = getattr(library, "nvmlDeviceGetPowerUsage", None)
        if power_getter is not None:
            value = ctypes.c_uint()
            if int(power_getter(self._handle, ctypes.byref(value))) == 0:
                power = round(int(value.value) / 1000, 2)  # 毫瓦转瓦。
        utilization_getter = getattr(library, "nvmlDeviceGetUtilizationRates", None)
        if utilization_getter is not None:
            value = _Utilization()
            if int(utilization_getter(self._handle, ctypes.byref(value))) == 0:
                utilization = int(value.gpu)
        return GpuSnapshot(clock_mhz=clock, sm_clock_mhz=sm_clock, power_w=power,
                           utilization_percent=utilization)

    def apply(self) -> dict[str, Any]:
        """请求优先最大性能；已生效或不可用时只读一次状态。幂等，可重复调用。"""
        self._last_action = "apply"
        if not self.enabled:
            return self.status()
        if not self._ensure_handle():
            logger.info("gpu_performance.unavailable reason=%s", self._last_error)
            return self.status()
        modes = self._read_modes()
        if modes is None:
            logger.warning("gpu_performance.read_failed reason=%s", self._last_error)
            return self.status()
        current, _, _ = modes
        if self._previous_mode is None:
            self._previous_mode = current  # 只记一次，避免重复调用把“原值”改成目标值。
        if current == POWER_MIZER_MAX_PERFORMANCE:
            self._applied = True
            return self.status()
        if not self._write_mode(POWER_MIZER_MAX_PERFORMANCE):
            logger.warning("gpu_performance.apply_failed reason=%s", self._last_error)
            return self.status()
        reread = self._read_modes()
        self._applied = reread is not None and reread[0] == POWER_MIZER_MAX_PERFORMANCE
        if self._applied:
            logger.info("gpu_performance.applied mode=%s previous=%s supported=%s snapshot=%s",
                        self._mode_name(POWER_MIZER_MAX_PERFORMANCE), self._mode_name(self._previous_mode),
                        self._supported_modes, self.snapshot().as_dict())
        else:
            logger.warning("gpu_performance.apply_unconfirmed reason=%s", self._last_error)
        return self.status()

    def restore(self) -> dict[str, Any]:
        """还原到加持前的模式；未加持或不可用时不做任何事。"""
        self._last_action = "restore"
        if not self._applied or self._previous_mode is None:
            return self.status()
        if self._write_mode(self._previous_mode):
            self._applied = False
            self._current_mode = self._previous_mode
            logger.info("gpu_performance.restored mode=%s", self._mode_name(self._previous_mode))
        else:
            logger.warning("gpu_performance.restore_failed reason=%s", self._last_error)
        return self.status()

    def status(self) -> dict[str, Any]:
        """状态快照：供日志与 TTS 状态接口展示，不含任何敏感信息。"""
        return {
            "enabled": self.enabled,  # enabled（配置开关）。
            "available": self._ready and self._handle is not None,  # available（NVML 与设备可用）。
            "applied": self._applied,  # applied（当前是否已请求优先最大性能）。
            "mode": self._mode_name(self._current_mode),  # mode（最近读到的模式）。
            "previous_mode": self._mode_name(self._previous_mode),  # previous_mode（加持前模式）。
            "supported_modes": None if self._supported_modes is None else [
                self._mode_name(index) for index in range(4)
                if self._supported_modes >> index & 1
            ],
            "last_error": self._last_error,  # last_error（最近一次失败原因）。
            "last_action": self._last_action,  # last_action（最近一次动作）。
        }
