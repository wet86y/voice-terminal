"""Deterministic local decision scheduler; the episode runtime owns dispatch."""
from __future__ import annotations

import math
import time

class AgentDecisionScheduler:
    """The sole scheduler and its fixed 400 ms local heartbeat.

    The heartbeat is deliberately independent from provider/TTS work.  The
    runtime owns one ``_run`` loop; this object only owns deterministic timing
    state and never creates an asyncio task of its own.
    """

    SECONDARY_INTERVAL_MS = 4000  # 二等信箱检查间隔，复用单一心跳。
    FINAL_WINDOW = 75
    TTS_EVENTS = frozenset({
        "TTS_STARTED", "TTS_COMPLETED", "TTS_CANCELLED", "TTS_REPLACED",
        "TTS_FAILED", "TTS_SEGMENT", "TTS_PLAYBACK_STARTED", "TTS_PLAYBACK_ENDED",
    })
    NON_SEMANTIC_EVENTS = frozenset({
        "SPEECH_STARTED", "SPEECH_ENDED", "ENDPOINT_CANDIDATE", "VOICE_ACTIVE",
        *TTS_EVENTS,
    })

    def __init__(self, options):
        self.options = options
        self.interval = max(0.001, float(options.scheduler_tick_ms) / 1000.0)
        # Product settings can stage a different final-silence boundary for
        # the next episode.
        self.FINAL_WINDOW = max(
            1,
            math.ceil(float(getattr(options, "final_silence_ms", 30000))
                      / max(1, int(options.scheduler_tick_ms))),
        )
        self.secondary_interval_windows = max(1, math.ceil(
            self.SECONDARY_INTERVAL_MS / 1000.0 / self.interval
        ))  # 每次二等检查跨过的本地心跳窗口数。
        self.last_tick = 0.0
        self.next_heartbeat = None
        self.anchor_monotonic = None
        self.anchor_wall_time = None
        self.anchor_turn_id: str | None = None
        self.anchor_source: str | None = None
        self.window_index = 0
        self.armed = False
        self.final_pending = False
        self.final_pending_since: float | None = None
        self.final_dispatched = False
        self.generation = 0
        self._dispatched_windows: list[int] = []
        self._resolutions: dict[int, str] = {}
        self.last_resolution = None
        self._deferred_generation: int | None = None

    def accepts(self, _kind):
        # TTS is output-side state, not a scheduler input.  All other event
        # kinds still enter the same mailbox; the runtime filters
        # speech-activity-only entries before choosing a dispatch.
        return not (isinstance(_kind, str) and _kind.startswith("TTS_"))

    def delay(self):
        if not self.last_tick:
            return 0
        return max(0, self.last_tick + self.interval - time.monotonic())

    def tick(self):
        self.last_tick = time.monotonic()

    def reset(self):
        """Reset the compatibility ordinary-gate timestamp only.

        Recovery uses this method.  It must not reset the user silence anchor
        or the silence window; system speech and recovery are not user input.
        """
        self.last_tick = time.monotonic()

    def start_heartbeat(self, now=None):
        """Start the fixed deadline axis without moving an existing axis."""

        now = time.monotonic() if now is None else float(now)
        if self.next_heartbeat is None:
            self.next_heartbeat = now + self.interval
        return self.next_heartbeat

    def heartbeat_delay(self, now=None):
        now = time.monotonic() if now is None else float(now)
        self.start_heartbeat(now)
        return max(0.0, self.next_heartbeat - now)

    def heartbeat_due(self, now=None):
        now = time.monotonic() if now is None else float(now)
        self.start_heartbeat(now)
        return now + 1e-9 >= self.next_heartbeat

    def heartbeat(self, now=None):
        """Consume at most one heartbeat and rebase the next fixed deadline.

        A late event-loop callback is evaluated at its real time, rather than
        replaying every missed 400ms deadline after a pause.
        """

        now = time.monotonic() if now is None else float(now)
        self.start_heartbeat(now)
        if now + 1e-9 < self.next_heartbeat:
            return None
        self.next_heartbeat = now + self.interval
        self.last_tick = now
        self.advance(now)
        return self.forced_due(now=now)

    def heartbeat_lateness(self, now=None) -> float:
        now = time.monotonic() if now is None else float(now)
        self.start_heartbeat(now)
        return max(0.0, now - self.next_heartbeat)

    def _reset_ladder(self):
        """Clear one ladder without changing the fixed heartbeat axis."""

        self.window_index = 0
        self.final_pending = False
        self.final_pending_since = None
        self.final_dispatched = False
        self._dispatched_windows.clear()
        self._resolutions.clear()
        self.last_resolution = None

    def _set_anchor(self, now, *, wall_time=None, turn_id=None, anchor_source=None):
        now = time.monotonic() if now is None else float(now)
        self.anchor_monotonic = now
        self.anchor_wall_time = time.time() if wall_time is None else float(wall_time)
        self.anchor_turn_id = turn_id
        self.anchor_source = anchor_source
        self._reset_ladder()
        self.armed = True

    def arm(self, now=None, *, wall_time=None, turn_id=None, anchor_source=None):
        """Arm a new ladder from a meaningful finalized/refined input."""

        self.generation += 1
        self._deferred_generation = None
        self._set_anchor(
            now, wall_time=wall_time, turn_id=turn_id,
            anchor_source=anchor_source,
        )
        return self.generation

    def defer(self, now=None, *, wall_time=None, turn_id=None):
        """Invalidate the old ladder while waiting for an output boundary.

        KWS sessions use this two-phase lifecycle for a finalized input.  The
        generation advances immediately, so forced candidates from the old
        turn become stale, but no new silence window can fire until the
        runtime supplies the authoritative playback/settlement timestamp.
        """

        # ``now`` is accepted for symmetry with ``arm`` and to make callers'
        # event timestamps explicit, but the deferred state intentionally has
        # no active monotonic anchor yet.
        _ = time.monotonic() if now is None else float(now)
        self._reset_ladder()
        self.armed = False
        self.anchor_monotonic = None
        self.anchor_wall_time = None
        self.anchor_turn_id = None
        self.anchor_source = None
        self.generation += 1
        self._deferred_generation = self.generation
        return self.generation

    def is_deferred(self, generation=None):
        """Whether ``generation`` is waiting for its real output boundary."""

        return self._deferred_generation is not None and (
            generation is None or int(generation) == self._deferred_generation
        )

    def activate_deferred(self, now=None, *, wall_time=None, turn_id=None,
                          generation=None, anchor_source=None):
        """Activate the latest deferred generation without incrementing it."""

        expected = self._deferred_generation
        if expected is None or expected != self.generation:
            return False
        if generation is not None and int(generation) != expected:
            return False
        self._deferred_generation = None
        self._set_anchor(
            now, wall_time=wall_time, turn_id=turn_id,
            anchor_source=anchor_source,
        )
        return True

    def advance(self, now=None):
        """Advance relative 400 ms windows using a caller-controlled clock.

        The final window remains pending until an idle dispatch can consume it.
        """

        if not self.armed or self.anchor_monotonic is None:
            return None
        now = time.monotonic() if now is None else float(now)
        elapsed = max(0.0, now - self.anchor_monotonic)
        target = max(0, int(math.floor((elapsed + 1e-9) / self.interval)))
        if target <= self.window_index:
            return None
        self.window_index = target
        if target >= self.FINAL_WINDOW and not self.final_dispatched:
            if not self.final_pending:
                self.final_pending_since = now
            self.final_pending = True
        return self.forced_due(now=now)

    def secondary_due(self, previous_window: int) -> bool:
        """活跃静默窗口跨过一个 4 秒检查点；暂停后只评估当前状态，不补发。"""
        return bool(
            self.armed and not self.final_pending
            and self.window_index < self.FINAL_WINDOW
            and self.window_index // self.secondary_interval_windows
            > previous_window // self.secondary_interval_windows
        )

    def secondary_candidate(self, *, now=None):
        """构建 secondary_mailbox（二等信箱评估）候选；最终静默优先。"""
        if not self.armed or self.final_pending or self.window_index >= self.FINAL_WINDOW:
            return None
        return self._descriptor(
            "secondary_mailbox", self.window_index,
            time.monotonic() if now is None else float(now),
        )

    def pending_checkpoint_windows(self):
        windows = []
        if self.final_pending and not self.final_dispatched:
            windows.append(self.FINAL_WINDOW)
        return windows

    def normal_due(self, pending=None, *, at_heartbeat=True):
        """Whether pending ordinary work may be considered at this tick."""

        return bool(pending) and bool(at_heartbeat)

    def forced_due(self, *, has_normal=False, now=None):
        """Return an unconsumed final candidate, with ordinary priority."""

        if has_normal or not self.armed:
            return None
        now = time.monotonic() if now is None else float(now)
        if self.final_pending and not self.final_dispatched:
            return self._descriptor("final_silence", self.FINAL_WINDOW, now)
        return None

    def mark_forced_dispatched(self, candidate, *, now=None):
        """Consume one forced candidate at the moment its LLM task starts."""

        if not isinstance(candidate, dict):
            return False
        window = int(candidate.get("window_index") or 0)
        prompt_kind = candidate.get("prompt_kind")
        at = time.monotonic() if now is None else float(now)
        if prompt_kind == "final_silence" or window == self.FINAL_WINDOW:
            self.final_pending = False
            self.final_pending_since = None
            self.final_dispatched = True
            self._dispatched_windows.append(self.FINAL_WINDOW)
            self._resolutions[self.FINAL_WINDOW] = "dispatched"
            self.last_resolution = {
                "kind": "final_dispatched", "window_index": self.FINAL_WINDOW, "at": at,
            }
            # Keep the final result visible in diagnostics while closing the
            # active ladder.  A new meaningful input calls arm() again.
            # The runtime still owns the single local heartbeat after the
            # ladder closes, so retain its fixed deadline axis.  A later
            # meaningful input calls arm() and starts a fresh ladder.
            self.disarm(preserve_final=True, preserve_heartbeat=True)
            return True
        self._dispatched_windows.append(window)
        self._resolutions[window] = "dispatched"
        self.last_resolution = {"kind": "forced_dispatched", "window_index": window, "at": at}
        return True

    def mark_final_pending(self):
        if not self.armed or self.final_dispatched:
            return False
        was_pending = self.final_pending
        self.final_pending = True
        if not was_pending:
            self.final_pending_since = time.monotonic()
        return not was_pending

    def disarm(self, *, preserve_final=False, preserve_heartbeat=False):
        """Close the ladder on final dispatch, reset, stop, or episode end."""

        final_dispatched = self.final_dispatched if preserve_final else False
        self._deferred_generation = None
        self.armed = False
        self.anchor_monotonic = None
        self.anchor_wall_time = None
        self.anchor_turn_id = None
        self.anchor_source = None
        self.final_pending = False
        self.final_pending_since = None
        self.final_dispatched = final_dispatched
        if not preserve_heartbeat:
            self.next_heartbeat = None
            self.last_tick = 0.0
        if not preserve_final:
            self.window_index = 0
            self._dispatched_windows.clear()
            self._resolutions.clear()
            self.last_resolution = None

    def _descriptor(self, prompt_kind, window_index, now):
        elapsed = 0.0
        if self.anchor_monotonic is not None:
            elapsed = max(0.0, now - self.anchor_monotonic)
        descriptor = {
            "prompt_kind": prompt_kind,
            "dispatch_kind": prompt_kind,
            "window_index": int(window_index),
            "silence_elapsed_ms": int(round(elapsed * 1000)),
            "anchor_wall_time": self.anchor_wall_time,
            "generation": self.generation,
        }
        if self.anchor_source is not None:
            descriptor["anchor_source"] = self.anchor_source
        if self.anchor_turn_id is not None:
            descriptor["anchor_turn_id"] = self.anchor_turn_id
            descriptor["speech_turn_id"] = self.anchor_turn_id
        return descriptor

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else float(now)
        silence_elapsed_ms = None
        if self.anchor_monotonic is not None:
            silence_elapsed_ms = int(round(max(0.0, now - self.anchor_monotonic) * 1000))
        value = {
            "interval_ms": int(round(self.interval * 1000)),
            "heartbeat_deadline": self.next_heartbeat,
            "armed": self.armed,
            "generation": self.generation,
            "anchor_monotonic": self.anchor_monotonic,
            "anchor_wall_time": self.anchor_wall_time,
            "anchor_turn_id": self.anchor_turn_id,
            "window_index": self.window_index,
            "silence_elapsed_ms": silence_elapsed_ms,
            "final_pending": self.final_pending,
            "final_pending_duration_ms": (
                int(round(max(0.0, now - self.final_pending_since) * 1000))
                if self.final_pending and self.final_pending_since is not None else 0
            ),
            "final_dispatched": self.final_dispatched,
            "dispatched_windows": list(self._dispatched_windows),
            "resolutions": dict(self._resolutions),
            "last_resolution": dict(self.last_resolution) if isinstance(self.last_resolution, dict) else None,
        }
        return value
