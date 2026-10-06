"""Recovery signals and the thread-backed episode heartbeat clock."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Event, Lock, Thread
from uuid import uuid4

@dataclass
class RecoverySignal:
    """One model-correctable protocol failure.

    A signal is consumed exactly once by the immediate recovery lane.  A
    failed recovery returns to the single scheduler; it must never create an
    independent retry/watchdog loop.  Later ordinary or forced windows may
    still re-evaluate the live episode under the bounded recovery rules.
    """

    code: str
    message: str
    source_request_id: str | None = None
    source_turn_id: str | None = None
    occurred_at: float = 0.0
    received_at: float = 0.0
    delivery_feedback: dict | None = None
    system_check_id: str | None = None
    owner_scope: str | None = None
    dispatch_context: dict | None = None
    shadow_information: dict | None = None
    secondary_context: dict | None = None  # 二等进度冻结快照，只供原请求的协议恢复使用。
    error_id: str = ""
    consumed: bool = False
    queued_at_monotonic: float = 0.0

    def __post_init__(self):
        if not self.occurred_at:
            self.occurred_at = time.time()
        if not self.received_at:
            self.received_at = time.time()
        if not self.error_id:
            self.error_id = uuid4().hex
        if not self.queued_at_monotonic:
            self.queued_at_monotonic = time.monotonic()


@dataclass(frozen=True)
class HeartbeatPulse:
    """One absolute clock pulse delivered to the asyncio scheduler loop."""

    episode_generation: int
    sequence: int
    scheduled_at: float
    coalesced_count: int = 0


class EpisodeHeartbeatClock:
    """Maintain a fixed monotonic heartbeat without touching runtime state.

    The timer thread owns only its deadline, sequence, and one coalescing
    mailbox.  The callback always runs on the owning asyncio event loop.
    """

    def __init__(self, *, interval: float, episode_generation: int, loop, callback):
        self.interval = max(0.001, float(interval))
        self.episode_generation = int(episode_generation)
        self.loop = loop
        self.callback = callback
        self._stop = Event()
        self._lock = Lock()
        self._pending: HeartbeatPulse | None = None
        self._callback_scheduled = False
        self._thread = Thread(
            target=self._run,
            name=f"agent-heartbeat-{self.episode_generation}",
            daemon=True,
        )

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 1.0) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        deadline = time.monotonic() + self.interval
        sequence = 0
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if self._stop.wait(remaining):
                return
            now = time.monotonic()
            periods = max(1, int(math.floor((now - deadline) / self.interval)) + 1)
            sequence += periods
            scheduled_at = deadline + (periods - 1) * self.interval
            pulse = HeartbeatPulse(
                episode_generation=self.episode_generation,
                sequence=sequence,
                scheduled_at=scheduled_at,
                coalesced_count=periods - 1,
            )
            should_schedule = False
            with self._lock:
                if self._pending is not None:
                    pulse = HeartbeatPulse(
                        episode_generation=pulse.episode_generation,
                        sequence=pulse.sequence,
                        scheduled_at=pulse.scheduled_at,
                        coalesced_count=(self._pending.coalesced_count
                                         + pulse.coalesced_count + 1),
                    )
                self._pending = pulse
                if not self._callback_scheduled:
                    self._callback_scheduled = True
                    should_schedule = True
            if should_schedule:
                try:
                    self.loop.call_soon_threadsafe(self._deliver)
                except RuntimeError:
                    return
            # Keep the absolute axis.  A suspended process may wake after many
            # deadlines; advance directly to the first future one.
            deadline += periods * self.interval

    def _deliver(self) -> None:
        with self._lock:
            pulse = self._pending
            self._pending = None
            self._callback_scheduled = False
        if pulse is not None and not self._stop.is_set():
            self.callback(pulse)
        should_schedule = False
        with self._lock:
            if self._pending is not None and not self._callback_scheduled and not self._stop.is_set():
                self._callback_scheduled = True
                should_schedule = True
        if should_schedule:
            self.loop.call_soon(self._deliver)
