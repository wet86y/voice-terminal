"""Audio capture contract（音频采集契约），不依赖具体设备或传输。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, Protocol

from ..controller.turn_workspace import AudioFrame

if TYPE_CHECKING:
    from ..audio.player import PlaybackSnapshot


class CapturePort(Protocol):
    """CapturePort（采集接口）：读取采样及唤醒准备期间的待处理帧。"""

    @property
    def running(self) -> bool: ...

    @property
    def diagnostics(self) -> dict: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def read_frame(self) -> AudioFrame: ...

    def pending_frame_count(self) -> int: ...

    def read_pending_frame(self) -> AudioFrame: ...

    def preroll(self, milliseconds: int) -> bytes: ...


class TimedCaptureControl(Protocol):
    """TimedCaptureControl（限时录音控制）：取消、停止与设备释放彼此独立。"""

    stop: Event
    cancel: Event
    closed: Event
    duration_ms: int


class TimedRecordingPort(Protocol):
    """TimedRecordingPort（限时采集接口）：供声纹注册和现场测试共享默认麦克风。"""

    def record_bounded(self, *, sample_rate: int, channels: int, input_device: str | int | None,
                       seconds: float, block_ms: int, control: TimedCaptureControl) -> tuple[bytes, str]: ...


class ProcessingPort(Protocol):
    """ProcessingPort（前处理接口）：处理状态与受保护打断属于同一设备。"""

    enabled: bool
    status: str
    error: str | None
    last_voice_activity: bool | None

    @property
    def available(self) -> bool: ...

    @property
    def protected_interruption_available(self) -> bool: ...

    def process(self, pcm: bytes) -> bytes: ...

    def push_reverse(self, pcm: bytes) -> None: ...

    def reclock_reverse(self, sample_rate: int) -> None: ...

    def reset(self) -> None: ...

    def recover(self) -> bool: ...

    def close(self) -> None: ...


class PlaybackTransactionPort(Protocol):
    """PlaybackTransactionPort（播放事务子接口）：跨设备操作必须等待带编号的设备回执。"""

    async def stop_and_snapshot_confirmed(self, key: str | None = None) -> "PlaybackSnapshot": ...

    async def pause_confirmed(self, key: str) -> bool: ...

    async def resume_confirmed(self, key: str) -> bool: ...

    async def prepare_replacement_transaction(self, key: str | None, *, max_ms: int = 600) -> "PlaybackReplacementTicket": ...

    async def bind_replacement_transaction(self, ticket: "PlaybackReplacementTicket", key: str) -> bool: ...

    async def abort_replacement_transaction(self, ticket: "PlaybackReplacementTicket", reason: str = "stopped") -> "PlaybackReplacementResult": ...

    async def wait_replacement_transaction(self, ticket: "PlaybackReplacementTicket") -> "PlaybackReplacementResult": ...


class PlaybackPort(Protocol):
    """PlaybackPort（播放接口）：本机阶段保留同步状态和停止快照语义。"""

    sample_rate: int
    channels: int
    on_playback_started: Callable[[str], None] | None

    @property
    def running(self) -> bool: ...

    @property
    def buffered_ms(self) -> int: ...

    @property
    def diagnostics(self) -> dict: ...

    @property
    def replacement_active(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def allow(self, key: str) -> None: ...

    def allow_user_initiated(self, key: str) -> None: ...

    def enqueue(self, turn_id: str, pcm: bytes) -> None: ...

    def clear(self, turn_id: str | None = None) -> None: ...

    def hold(self, key: str) -> bool: ...

    def pause(self, key: str) -> bool: ...

    def resume(self, key: str) -> bool: ...

    def block(self) -> bool: ...

    def has_started(self, key: str) -> bool: ...

    def progress(self, key: str | None = None) -> dict: ...

    def stop_and_snapshot(self, key: str | None = None) -> PlaybackSnapshot: ...

    async def stop_and_snapshot_confirmed(self, key: str | None = None) -> PlaybackSnapshot: ...

    async def prepare_replacement_transaction(self, key: str | None, *, max_ms: int = 600) -> "PlaybackReplacementTicket": ...

    async def bind_replacement_transaction(self, ticket: "PlaybackReplacementTicket", key: str) -> bool: ...

    async def abort_replacement_transaction(self, ticket: "PlaybackReplacementTicket", reason: str = "stopped") -> "PlaybackReplacementResult": ...

    async def wait_replacement_transaction(self, ticket: "PlaybackReplacementTicket") -> "PlaybackReplacementResult": ...

    def configure_stream_buffer(self, key: str, *, startup_ms: int, resume_ms: int) -> None: ...

    def set_stream_reserve(self, key: str, *, startup_ms: int | None = None,
                           resume_ms: int | None = None) -> bool: ...

    def finish_stream(self, key: str) -> None: ...

    def stream_reserve_ms(self, key: str) -> dict: ...

    def play_wake_cue(self, key: str) -> None: ...

    def output_position_at(self, captured_at: float) -> int | None: ...

    def reclock(self, sample_rate: int, *, on_rate_changed: Callable[[int], None] | None = None) -> bool: ...

    async def wait_until_output_complete(self, timeout_seconds: float = 10.0) -> None: ...


class PlaybackOutcomeUnknown(RuntimeError):
    """PlaybackOutcomeUnknown（播放结果未知）：设备未确认停止或替换，不可推断已静音。"""

    def __init__(self, message: str, *, request_id: str | None = None,
                 device_id: str | None = None, operation: str | None = None):
        super().__init__(message)
        self.request_id = request_id  # request_id（可供查询的原设备请求编号）。
        self.device_id = device_id  # device_id（固定的执行设备编号）。
        self.operation = operation  # operation（未知结果所属操作）。


def _playback_snapshot_wire(snapshot: "PlaybackSnapshot") -> dict:
    """_playback_snapshot_wire（播放状态协议载荷）：只输出设备协议 v1 定义的可信字段。"""
    return {
        "playback_key": snapshot.key,
        "enqueued_ms": snapshot.enqueued_ms,
        "played_ms": snapshot.played_ms,
        "buffered_ms": snapshot.buffered_ms,
        "output_estimated_ms": snapshot.output_estimated_ms,
        "started": snapshot.started,
        "device_time_us": snapshot.device_time_us,
        "precision": snapshot.precision,
    }


@dataclass(frozen=True)
class PlaybackReplacementTicket:
    """PlaybackReplacementTicket（替换事务票据）：只传纯数据，不包含播放器内存对象。"""

    replacement_id: str  # replacement_id（替换事务编号）。
    old_key: str | None  # old_key（固定的原播放事务标识）。
    snapshot: "PlaybackSnapshot"  # snapshot（替换受理时的可信状态快照）。
    prepared_device_time_us: int  # prepared_device_time_us（设备单调准备时刻，微秒）。

    def to_dict(self) -> dict:
        return {"replacement_id": self.replacement_id, "old_playback_key": self.old_key,
                "snapshot": _playback_snapshot_wire(self.snapshot),
                "prepared_device_time_us": self.prepared_device_time_us}


@dataclass(frozen=True)
class PlaybackReplacementResult:
    """PlaybackReplacementResult（替换结算）：由原票据与最终设备事实组成。"""

    replacement_id: str  # replacement_id（原替换事务编号）。
    snapshot: "PlaybackSnapshot"  # snapshot（设备停止旧音后的可信状态）。
    reason: str  # reason（旧音结束原因）。
    started_device_time_us: int  # started_device_time_us（设备单调受理时刻，微秒）。
    finished_device_time_us: int  # finished_device_time_us（设备确认结束时刻，微秒）。
    events_device_time_us: dict[str, int]  # events_device_time_us（设备单调时序事实，微秒）。
    attenuated_range_ms: tuple[int, int]  # attenuated_range_ms（旧音不确定听达区间）。

    @property
    def duration_ms(self) -> float:
        """duration_ms（设备确认的替换经过时长，毫秒）。"""
        return max(0, self.finished_device_time_us - self.started_device_time_us) / 1000

    def to_dict(self) -> dict:
        return {"replacement_id": self.replacement_id,
                "snapshot": _playback_snapshot_wire(self.snapshot),
                "reason": self.reason, "started_device_time_us": self.started_device_time_us,
                "finished_device_time_us": self.finished_device_time_us,
                "events_device_time_us": dict(self.events_device_time_us),
                "attenuated_range_ms": list(self.attenuated_range_ms)}
