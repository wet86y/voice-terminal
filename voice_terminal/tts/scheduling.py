from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Mapping, Protocol


class TtsSchedulingError(RuntimeError):
    """Raised when a scheduling policy cannot make progress."""


@dataclass(frozen=True)
class PlaybackProgress:
    """Provider audio and local output progress in milliseconds."""

    returned_ms: int = 0
    played_ms: int = 0
    queued_ms: int = 0
    started: bool = False
    # ``played_ms`` keeps the historical callback-consumed counter.  When a
    # player can estimate the device output position, scheduling should use
    # that conservative position instead of silently changing the meaning of
    # the existing counter.
    output_estimated_ms: int | None = None

    @property
    def remaining_ms(self) -> int:
        played = self.played_ms if self.output_estimated_ms is None else self.output_estimated_ms
        return max(0, self.returned_ms - played)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PlaybackProgress":
        def integer(name: str) -> int:
            item = value.get(name, 0)
            return int(item) if isinstance(item, (int, float)) else 0

        return cls(
            returned_ms=integer("returned_ms"),
            played_ms=integer("played_ms"),
            queued_ms=integer("queued_ms"),
            started=bool(value.get("started", False)),
            output_estimated_ms=(
                integer("output_estimated_ms")
                if isinstance(value.get("output_estimated_ms"), (int, float))
                else None
            ),
        )


@dataclass(frozen=True)
class TtsSegmentReceipt:
    """Provider-neutral completion boundary for one submitted text block.

    ``end_received_bytes`` is cumulative within the current speech
    transaction.  It identifies the exact PCM position after this block
    without requiring subtitles or provider-specific sentence events.
    """

    index: int
    text_chars: int
    end_received_bytes: int


class TtsSegmentSession(Protocol):
    """Provider-neutral session contract used by sentence scheduling.

    Online and future local engines may buffer or synthesize differently, but
    the controller only needs ordered PCM plus one cumulative boundary receipt
    for each successfully submitted text block.
    """

    async def start(self) -> None: ...

    async def send_text(self, text: str) -> None: ...

    def audio_chunks(self) -> AsyncIterator[bytes]: ...

    async def wait_for_segment(self, previous_index: int) -> TtsSegmentReceipt: ...

    async def finish(self) -> None: ...

    async def close(self) -> None: ...


ProgressReader = Callable[[], PlaybackProgress | Mapping[str, object]]
ValidityCheck = Callable[[], bool]
ProgressReporter = Callable[[PlaybackProgress], Awaitable[None] | None]
FailureReader = Callable[[], Exception | None]


class DirectStreamingPolicy:
    """Release text as soon as the provider accepts it.

    This is the normal policy for a provider which performs its own sentence
    buffering and backpressure, such as MiniMax's Bidi endpoint.
    """

    name = "direct"
    lead_ms = 0

    async def wait_before_send(
        self,
        progress: ProgressReader,
        valid: ValidityCheck,
        report: ProgressReporter | None = None,
    ) -> None:
        del progress, valid, report

    async def wait_for_audio(self, *_args: object, **_kwargs: object) -> None:
        return


class PlaybackWindowPolicy:
    """Keep the legacy playback-ahead window as a reusable policy.

    The policy deliberately knows nothing about a provider's wire protocol. It
    only consumes a progress snapshot and a validity callback.  A future TTS
    adapter can reuse it if it exposes the same two signals.
    """

    name = "playback_window"

    def __init__(self, lead_ms: int = 2000, *, timeout_seconds: float = 600.0) -> None:
        if lead_ms <= 0:
            raise ValueError("lead_ms must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.lead_ms = lead_ms
        self.timeout_seconds = timeout_seconds

    async def wait_before_send(
        self,
        progress: ProgressReader,
        valid: ValidityCheck,
        report: ProgressReporter | None = None,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while valid():
            current = progress()
            snapshot = current if isinstance(current, PlaybackProgress) else PlaybackProgress.from_mapping(current)
            if snapshot.remaining_ms <= self.lead_ms:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TtsSchedulingError("TTS 播放进度长时间没有下降")
            if report is not None:
                result = report(snapshot)
                if result is not None:
                    await result
            await asyncio.sleep(0.05)
        return

    async def wait_for_audio(
        self,
        before_bytes: int,
        current_bytes: Callable[[], int],
        valid: ValidityCheck,
        signal: asyncio.Event,
        failure: FailureReader | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        """Wait for the first returned PCM after a text send."""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while current_bytes() <= before_bytes:
            if not valid():
                return
            if failure is not None:
                error = failure()
                if error is not None:
                    raise error
            signal.clear()
            if current_bytes() > before_bytes:
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TtsSchedulingError("TTS 文本发送后未收到音频")
            try:
                await asyncio.wait_for(signal.wait(), remaining)
            except asyncio.TimeoutError as exc:
                raise TtsSchedulingError("TTS 文本发送后未收到音频") from exc


@dataclass(frozen=True)
class TtsSchedulingState:
    generation_id: str | int | None
    policy: str
    pending_text: int
    sent_text: int
    ended: bool
    cancelled: bool
    waiting_reason: str | None = None


class TtsGenerationScheduler:
    """Provider-neutral lifecycle wrapper around a scheduling policy.

    Controllers can use this small coordinator when they do not need a
    provider-specific queue.  It keeps text sends bounded, checks generation
    identity before and after a playback-window wait, and wakes an in-flight
    wait when ``cancel`` is called.  Provider adapters remain responsible for
    their wire protocol and audio/subtitle events.
    """

    def __init__(
        self,
        policy: DirectStreamingPolicy | PlaybackWindowPolicy,
        send_text: Callable[[str], Awaitable[None]],
        progress: ProgressReader,
        *,
        max_pending: int = 32,
    ) -> None:
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self.policy = policy
        self._send_text = send_text
        self._progress = progress
        self._max_pending = max_pending
        self._generation_id: str | int | None = None
        self._pending = 0
        self._sent = 0
        self._ended = False
        self._cancelled = False
        self._wake = asyncio.Event()
        self._waiting_reason: str | None = None
        self._send_lock = asyncio.Lock()

    def begin(self, generation_id: str | int) -> None:
        self._generation_id = generation_id
        self._pending = 0
        self._sent = 0
        self._ended = False
        self._cancelled = False
        self._waiting_reason = None
        self._wake.clear()

    def _valid(self, generation_id: str | int) -> bool:
        return self._generation_id == generation_id and not self._cancelled and not self._ended

    async def send(self, generation_id: str | int, text: str) -> None:
        if not text:
            return
        async with self._send_lock:
            if not self._valid(generation_id):
                raise TtsSchedulingError("TTS generation 已失效")
            if self._pending >= self._max_pending:
                raise TtsSchedulingError("TTS 文本待发送队列已达到上限")
            self._waiting_reason = "playback_window" if isinstance(self.policy, PlaybackWindowPolicy) else None
            self._pending += 1
            policy_task: asyncio.Task[None] | None = None
            wake_task: asyncio.Task[bool] | None = None
            try:
                policy_task = asyncio.create_task(
                    self.policy.wait_before_send(
                        self._progress,
                        lambda: self._valid(generation_id),
                    )
                )
                wake_task = asyncio.create_task(self._wake.wait())
                done, _pending_tasks = await asyncio.wait(
                    {policy_task, wake_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wake_task in done and policy_task not in done:
                    raise TtsSchedulingError("TTS generation 在等待期间被取消")
                await policy_task
                if not self._valid(generation_id):
                    raise TtsSchedulingError("TTS generation 在等待期间被取消")
                await self._send_text(text)
                self._sent += 1
            finally:
                for task in (policy_task, wake_task):
                    if task is not None and not task.done():
                        task.cancel()
                if policy_task is not None or wake_task is not None:
                    await asyncio.gather(
                        *(task for task in (policy_task, wake_task) if task is not None),
                        return_exceptions=True,
                    )
                self._pending = max(0, self._pending - 1)
                self._waiting_reason = None

    def end(self, generation_id: str | int) -> None:
        if self._generation_id == generation_id and not self._cancelled:
            self._ended = True
            self._wake.set()

    def cancel(self, generation_id: str | int | None = None) -> None:
        if generation_id is None or generation_id == self._generation_id:
            self._cancelled = True
            self._ended = True
            self._wake.set()

    def snapshot(self) -> TtsSchedulingState:
        return TtsSchedulingState(
            generation_id=self._generation_id,
            policy=self.policy.name,
            pending_text=self._pending,
            sent_text=self._sent,
            ended=self._ended,
            cancelled=self._cancelled,
            waiting_reason=self._waiting_reason,
        )
