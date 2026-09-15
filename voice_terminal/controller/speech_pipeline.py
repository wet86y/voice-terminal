from __future__ import annotations
import asyncio
from collections import deque
import logging
import time
from ..stream.text_bridge import TextBridge
from ..tts.alignment import DeliveryResolver
from ..tts.scheduling import DirectStreamingPolicy, PlaybackProgress, PlaybackWindowPolicy
from ..tts.text_merger import BidiTextMerger

logger = logging.getLogger(__name__)


FLUSH_DUE = object()


async def buffered_stream(source, bridge):
    """Arm one local flush deadline only while a speakable prefix is buffered.

    The pending upstream read is never restarted on timeout. This flushes a
    stalled model's text without making another model request or polling it.
    """
    iterator = source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            text = getattr(bridge, "pending_text", getattr(bridge, "buffer", ""))
            timeout = bridge.max_wait_ms / 1000 if len(text) >= getattr(bridge, "min_chars", 1) else None
            ready, _ = await asyncio.wait({pending}, timeout=timeout)
            if not ready:
                yield FLUSH_DUE
                continue
            try:
                value = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield value
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close:
            await close()


class SpeechPipeline:
    """Shared Bidi/legacy streaming sink; the host owns authorization and state."""

    def _delivery_resolver_for(self, ws, *, speech_id: str | None = None) -> DeliveryResolver:
        """Return the delivery accumulator for one output transaction.

        A user turn can legitimately contain several speech transactions when
        the LLM replaces a provisional answer.  Their subtitle/audio spans
        must therefore never share a turn-scoped accumulator.
        """

        resolver_key = speech_id or ws.turn_id
        return self._delivery_resolvers.setdefault(resolver_key, DeliveryResolver())

    def _new_tts_session(self, ws, *, fresh=False):
        """Factory hook for transaction-owned output implementations."""
        try:
            return self.tts.session(fresh=fresh)
        except TypeError:
            return self.tts.session()

    def _bind_tts_session(self, ws, session):
        self._tts_session = session

    def _release_tts_session(self, ws, session, *, retain):
        if self._tts_session is session:
            self._tts_session = session if retain else None

    async def _wait_for_playback_permission(self, ws, token):
        """Hook for output owners that defer pre-playback TTS."""
        return

    async def _play_stream(self, ws, token, deltas, *, speech_id: str | None = None) -> str:
        transport = getattr(self.tts, "transport", "websocket") if self.tts is not None else "websocket"
        bridge = BidiTextMerger(max_chars=128, max_wait_ms=30) if transport == "websocket_bidi" else TextBridge(**self.bridge_options)
        session = None
        reader = None
        sender_task = None
        progress_task = None
        parts = []
        tts_failed: Exception | None = None
        tts_disabled = False
        playback_key = ws.playback_key
        configured_scheduling = getattr(
            self.tts,
            "effective_scheduling",
            getattr(self.tts, "scheduling", "auto") if self.tts is not None else "auto",
        )
        if transport == "websocket_bidi" or configured_scheduling == "direct":
            scheduling_policy = DirectStreamingPolicy()
        elif configured_scheduling == "playback_window" or self.options.tts_pacing:
            scheduling_policy = PlaybackWindowPolicy(max(1, self.options.tts_lead_ms))
        else:
            scheduling_policy = DirectStreamingPolicy()
        pacing_enabled = isinstance(scheduling_policy, PlaybackWindowPolicy) and self.player is not None
        lead_ms = scheduling_policy.lead_ms
        # The queue keeps the LLM consumer independent from the playback
        # clock, while still putting a hard bound on text waiting for TTS.
        pending_segments: deque[str] = deque()
        pending_signal = asyncio.Event()
        audio_signal = asyncio.Event()
        progress_stop = asyncio.Event()
        pending_limit = 32
        producer_done = False
        audio_returned_bytes = 0
        tts_text_sent = False
        progress_lock = asyncio.Lock()
        last_progress: tuple[int, int, int, int, int] | None = None
        last_progress_at = 0.0
        # Delivery is a speech-transaction fact, not a turn fact.  Multiple
        # LLM decisions may replace each other inside the same user turn; a
        # turn-keyed resolver both mixes their subtitle boundaries and leaves
        # the speech-keyed finalizer empty (reported as delivery=unknown).
        # Keep the turn key solely as a compatibility fallback for callers
        # outside EpisodeSpeechOutput which have no speech ID.
        resolver = self._delivery_resolver_for(ws, speech_id=speech_id)
        completed_markers = 0
        alignment_cursor = 0
        timing_owner = getattr(self, "controller", self)

        def sync_turn_timings() -> None:
            shared = getattr(timing_owner, "_turn_timings", {}).get(ws.turn_id)
            if isinstance(shared, dict):
                ws.timings.update(shared)
        audio_bytes_per_second = (
            self.player.sample_rate * self.player.channels * 2
            if self.player is not None else 32000 * 2
        )
        await self._wait_for_playback_permission(ws, token)
        if not self._valid(token) or not ws.committed:
            return ""
        if self.player:
            self.player.allow(playback_key)

        def playback_progress() -> dict[str, int | bool | str | None]:
            # The provider may have received far more than the player has
            # consumed from audio_chunks(). Include its entire PCM spool.
            received_bytes = max(audio_returned_bytes, getattr(session, "received_bytes", 0))
            returned_ms = int(received_bytes * 1000 / audio_bytes_per_second)
            if self.player is None:
                played_ms = 0
                output_estimated_ms = 0
                queued_ms = 0
                started = False
            else:
                current = self.player.progress(playback_key)
                played_ms = int(current["played_ms"])
                output_estimated_ms = int(
                    current.get("output_estimated_ms", current["played_ms"]) or 0
                )
                queued_ms = int(current["buffered_ms"])
                started = bool(current["started"])
            return {
                "returned_ms": returned_ms,
                "played_ms": played_ms,
                # Keep callback consumption visible as ``played_ms`` while
                # using the conservative device position for scheduling and
                # remaining-audio calculations.
                "output_estimated_ms": output_estimated_ms,
                "remaining_ms": max(0, returned_ms - output_estimated_ms),
                "queued_ms": queued_ms,
                "started": started,
            }

        async def emit_progress(*, status: str | None = None, waiting_text: str | None = None,
                                trigger: str | None = None, force: bool = False) -> None:
            nonlocal last_progress, last_progress_at
            if not self._valid(token) or not ws.committed:
                return
            async with progress_lock:
                snapshot = playback_progress()
                now = time.monotonic()
                signature = (
                    int(snapshot["returned_ms"]), int(snapshot["played_ms"]),
                    int(snapshot["output_estimated_ms"]),
                    int(snapshot["remaining_ms"]), int(snapshot["queued_ms"]),
                )
                # Audio callbacks can arrive dozens of times per second.  A
                # half-second diagnostic cadence is enough for the console
                # and WebUI while keeping the event bus and log bounded.
                if not force and (now - last_progress_at < 0.5 or signature == last_progress):
                    return
                last_progress = signature
                last_progress_at = now
                current_status = status or self.state.current.get("tts_status") or "IDLE"
                if waiting_text is not None:
                    self.state.current["tts_text"] = waiting_text
                self.state.current.update(
                    tts_returned_ms=int(snapshot["returned_ms"]),
                    tts_played_ms=int(snapshot["played_ms"]),
                    tts_output_estimated_ms=int(snapshot["output_estimated_ms"]),
                    tts_remaining_ms=int(snapshot["remaining_ms"]),
                    audio_buffer_ms=int(snapshot["queued_ms"]),
                )
                logger.info(
                    "controller.tts_progress turn_id=%s status=%s returned_ms=%d played_ms=%d remaining_ms=%d queued_ms=%d trigger=%s",
                    ws.turn_id,
                    current_status,
                    int(snapshot["returned_ms"]),
                    int(snapshot["played_ms"]),
                    int(snapshot["remaining_ms"]),
                    int(snapshot["queued_ms"]),
                    trigger or "monitor",
                )
                await self._detail(
                    "tts.progress",
                    provider="minimax",
                    status=current_status,
                    waiting_text=waiting_text if waiting_text is not None else self.state.current.get("tts_text", ""),
                    audio_buffer_ms=int(snapshot["queued_ms"]),
                    audio_returned_ms=int(snapshot["returned_ms"]),
                    audio_played_ms=int(snapshot["played_ms"]),
                    audio_output_estimated_ms=int(snapshot["output_estimated_ms"]),
                    audio_remaining_ms=int(snapshot["remaining_ms"]),
                    pacing_enabled=pacing_enabled,
                    lead_ms=lead_ms,
                    trigger=trigger,
                )

        async def read_audio(active_session):
            nonlocal audio_returned_bytes, completed_markers, alignment_cursor

            audio_iterator = None
            try:
                audio_iterator = active_session.audio_chunks().__aiter__()
                while True:
                    # Gate before pulling the next provider chunk. This keeps
                    # a held pre-playback transaction from accumulating an
                    # unbounded application-side PCM buffer.
                    await self._wait_for_playback_permission(ws, token)
                    if not self._valid(token) or not ws.committed:
                        return
                    try:
                        pcm = await audio_iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    # The user may have become active while the provider
                    # yielded this chunk. Re-check before admitting it to the
                    # local player.
                    await self._wait_for_playback_permission(ws, token)
                    if not self._valid(token) or not ws.committed:
                        return
                    audio_returned_bytes += len(pcm)
                    audio_signal.set()
                    ws.timings.setdefault("first_audio", time.monotonic())
                    if self.player:
                        # Keep the native player bounded.  Audio held by the
                        # provider spool is included in audio_remaining_ms
                        # through the session's network receive counter.
                        while self.player.buffered_ms > max(1, self.options.tts_local_buffer_ms):
                            await asyncio.sleep(.02)
                            if not self._valid(token):
                                return
                        self.player.enqueue(playback_key, pcm)
                    # Bidi sentence/request boundaries do not include the
                    # sentence text, so they cannot safely close a local
                    # client chunk.  Do not equate a task_continue frame with
                    # a server sentence; wait for a provider subtitle span.
                    current_markers = (
                        int(getattr(active_session, "completed_segments", 0))
                        if not getattr(active_session, "bidi", False)
                        else 0
                    )
                    while completed_markers < current_markers:
                        completed_markers += 1
                        resolver.mark_boundary(int(playback_progress()["returned_ms"]))
                    nonlocal_alignment = getattr(active_session, "alignment_events", ())
                    for alignment in nonlocal_alignment[alignment_cursor:]:
                        if alignment is not None:
                            resolver.add_provider_alignment(alignment)
                    alignment_cursor = len(nonlocal_alignment)
                    self.state.current["tts_status"] = "STREAMING"
                    await emit_progress(status="STREAMING", trigger="audio_received")
                    await self._latency(ws)
            finally:
                close = getattr(audio_iterator, "aclose", None) if audio_iterator is not None else None
                if close is not None:
                    await close()
                # Wake a sender waiting for the first PCM even when the
                # provider ends or fails without returning audio.
                audio_signal.set()

        async def monitor_progress() -> None:
            try:
                while True:
                    try:
                        await asyncio.wait_for(progress_stop.wait(), .2)
                    except asyncio.TimeoutError:
                        pass
                    if progress_stop.is_set() or not self._valid(token) or not ws.committed:
                        return
                    await emit_progress()
            except asyncio.CancelledError:
                raise

        async def reader_exception() -> Exception | None:
            if reader is None or not reader.done() or reader.cancelled():
                return None
            return reader.exception()

        def reader_failure() -> Exception | None:
            if reader is None or not reader.done() or reader.cancelled():
                return None
            return reader.exception()

        async def wait_for_audio(before_bytes: int) -> None:
            if not pacing_enabled:
                return
            await scheduling_policy.wait_for_audio(
                before_bytes,
                lambda: audio_returned_bytes,
                lambda: self._valid(token) and not tts_disabled,
                audio_signal,
                failure=reader_failure,
            )
            await emit_progress(status="STREAMING", trigger="audio_available")

        async def wait_for_playback_window() -> None:
            if not pacing_enabled:
                return
            await scheduling_policy.wait_before_send(
                lambda: PlaybackProgress.from_mapping(playback_progress()),
                lambda: self._valid(token) and not tts_disabled,
                lambda snapshot: emit_progress(status="WAITING_PLAYBACK", trigger="lead_window"),
            )

        async def next_segment() -> str | None:
            while not pending_segments and not producer_done and not tts_disabled and self._valid(token):
                pending_signal.clear()
                if pending_segments or producer_done or tts_disabled:
                    continue
                await pending_signal.wait()
            if not pending_segments:
                return None
            segment = pending_segments.popleft()
            # Wake a producer waiting on the bounded queue.
            pending_signal.set()
            return segment

        async def tts_sender() -> None:
            nonlocal session, reader, progress_task, tts_failed, tts_disabled, tts_text_sent
            sent_any = False
            try:
                while True:
                    segment = await next_segment()
                    if segment is None:
                        return
                    if not self._valid(token):
                        return
                    await self._wait_for_playback_permission(ws, token)
                    if not self._valid(token) or not ws.committed:
                        return
                    if session is None:
                        await self._await_tts_cancel_barrier()
                        await self._wait_for_playback_permission(ws, token)
                        if not self._valid(token) or not ws.committed:
                            return
                        sync_turn_timings()
                        session = self._new_tts_session(ws)
                        self._bind_tts_session(ws, session)
                        checkout = getattr(self.tts, "checkout_prewarm", None)
                        warm = bool(checkout(session)) if callable(checkout) else False
                        checkout_reason = getattr(
                            self.tts, "last_prewarm_checkout_reason",
                            "not_available" if not callable(checkout) else "not_prewarmed",
                        )
                        warm_connection_id = getattr(session, "connection_id", None) if warm else None
                        ws.timings.setdefault("tts_session_start", time.monotonic())
                        try:
                            await self._wait_for_playback_permission(ws, token)
                            if not self._valid(token) or not ws.committed:
                                return
                            await session.start()
                            if warm and getattr(session, "connection_id", None) != warm_connection_id:
                                # The retained socket disappeared between
                                # checkout and start(), so this transaction
                                # transparently became a cold connection.
                                warm = False
                            prewarm_ready = getattr(session, "_prewarm_ready", None)
                            if (not warm and getattr(session, "_prewarm_inflight", False)
                                    and prewarm_ready is not None):
                                await prewarm_ready.wait()
                                if callable(checkout):
                                    warm = bool(checkout(session))
                            if not getattr(session, "reusable", True):
                                # A failed/cancelled prewarm may have closed
                                # the shared candidate while this sender was
                                # waiting for it.  Fall back to a fresh cold
                                # session rather than sending text to a dead
                                # socket.
                                await self._wait_for_playback_permission(ws, token)
                                if not self._valid(token) or not ws.committed:
                                    return
                                session = self._new_tts_session(ws, fresh=True)
                                self._bind_tts_session(ws, session)
                                ws.timings["tts_session_start"] = time.monotonic()
                                await self._wait_for_playback_permission(ws, token)
                                if not self._valid(token) or not ws.committed:
                                    return
                                await session.start()
                                warm = False
                            ws.timings.setdefault("tts_session_ready", time.monotonic())
                            if ws.timings.get("replacement_detected"):
                                ws.timings.setdefault("new_tts_started", time.monotonic())
                                await self._detail("new_tts_started", status="STARTED")
                            # If the prewarm task was still finishing when we
                            # inspected the session, claim it after the
                            # serialized start returns.  This keeps a racing
                            # first speak on the warm path without sending a
                            # second task_start.
                            if not warm and callable(checkout):
                                warm = bool(checkout(session))
                            checkout_reason = getattr(
                                self.tts, "last_prewarm_checkout_reason",
                                "not_available" if not callable(checkout) else "not_prewarmed",
                            )
                            ws.timings["prewarm_checkout_result"] = "hit" if warm else "miss"
                            ws.timings["prewarm_checkout_reason"] = checkout_reason
                            await self._detail(
                                "prewarm_checkout_result",
                                result="hit" if warm else "miss",
                                reason=checkout_reason,
                            )
                            if warm:
                                ws.timings.setdefault("tts_prewarm_checkout", time.monotonic())
                                ws.timings["tts_prewarm_used"] = True
                            elif ws.timings.get("tts_prewarm_requested"):
                                ws.timings["tts_prewarm_fallback"] = True
                        except Exception as exc:
                            warm = False
                            ws.timings["tts_prewarm_fallback"] = bool(ws.timings.get("tts_prewarm_requested"))
                            # The optional MiniMax Bidi emotion extension is
                            # verified on task_start.  A rejection closes that
                            # connection and retries the still-unsent first
                            # segment with the neutral provider payload.
                            if not getattr(exc, "emotion_unsupported", False) or not self._valid(token):
                                raise
                            await self._detail("tts.emotion_unsupported", status="FALLBACK_CALM")
                            await self._wait_for_playback_permission(ws, token)
                            if not self._valid(token) or not ws.committed:
                                return
                            session = self._new_tts_session(ws, fresh=True)
                            self._bind_tts_session(ws, session)
                            ws.timings["tts_session_start"] = time.monotonic()
                            await self._wait_for_playback_permission(ws, token)
                            if not self._valid(token) or not ws.committed:
                                return
                            await session.start()
                            ws.timings["tts_session_ready"] = time.monotonic()
                            sync_turn_timings()
                        await self._wait_for_playback_permission(ws, token)
                        if not self._valid(token) or not ws.committed:
                            return
                        reader = asyncio.create_task(read_audio(session), name=f"tts-audio-{ws.turn_id}")
                        progress_task = asyncio.create_task(monitor_progress(), name=f"tts-progress-{ws.turn_id}")
                    if sent_any:
                        await self._wait_for_playback_permission(ws, token)
                        if not self._valid(token) or not ws.committed:
                            return
                        await wait_for_playback_window()
                    if not self._valid(token):
                        return
                    await self._wait_for_playback_permission(ws, token)
                    if not self._valid(token) or not ws.committed:
                        return
                    receiver_error = await reader_exception()
                    if receiver_error is not None:
                        raise receiver_error
                    before_bytes = audio_returned_bytes
                    before_segments = getattr(session, "completed_segments", 0)
                    ws.timings.setdefault("first_tts_text", time.monotonic())
                    self.state.current["tts_text"] = segment
                    resolver.add_text(segment, int(playback_progress()["returned_ms"]))
                    await session.send_text(segment)
                    tts_text_sent = True
                    await emit_progress(
                        status="QUEUED",
                        waiting_text=segment,
                        trigger="first_segment" if not sent_any else "remaining_below_lead",
                        force=True,
                    )
                    sent_any = True
                    # A PCM chunk can belong to earlier text. Wait for the
                    # current segment's final response before evaluating the
                    # total playback backlog and releasing another segment.
                    wait_for_segment = getattr(session, "wait_for_segment", None)
                    if pacing_enabled and wait_for_segment is not None:
                        await wait_for_segment(before_segments)
                    else:
                        await wait_for_audio(before_bytes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tts_failed = exc
                tts_disabled = True
                pending_segments.clear()
                pending_signal.set()
                progress_stop.set()
                logger.warning("controller.tts_degraded turn_id=%s error=%s", ws.turn_id, exc)

        async def send(segments):
            nonlocal sender_task
            if not segments or self.tts is None or tts_disabled or not self._valid(token):
                return
            if sender_task is None:
                sender_task = asyncio.create_task(tts_sender(), name=f"tts-sender-{ws.turn_id}")
            for segment in segments:
                if not segment or not segment.strip():
                    continue
                while len(pending_segments) >= pending_limit and not tts_disabled and self._valid(token):
                    pending_signal.clear()
                    if len(pending_segments) < pending_limit:
                        continue
                    await pending_signal.wait()
                if tts_disabled or not self._valid(token):
                    return
                pending_segments.append(segment)
                pending_signal.set()

        async def finish_send_queue() -> None:
            nonlocal producer_done
            producer_done = True
            pending_signal.set()

        stream = buffered_stream(deltas, bridge)
        try:
            async for delta in stream:
                if not self._valid(token) or not ws.committed:
                    return ""
                if delta is FLUSH_DUE:
                    await send(bridge.flush_due(now=time.monotonic()))
                    continue
                if delta is None:
                    await send(bridge.finish())
                    continue
                ws.timings.setdefault("first_token", time.monotonic())
                parts.append(delta)
                self.state.current["assistant_text"] = "".join(parts)
                ws.staged_response = "".join(parts).strip()
                await self._detail("assistant.delta", text_delta=delta)
                await send(bridge.flush_due(now=time.monotonic()))
                await send(bridge.feed(delta))
                await self._latency(ws)
            ws.staged_response = "".join(parts).strip()
            await send(bridge.finish())
            await finish_send_queue()
            if sender_task is not None:
                await sender_task
            if session:
                try:
                    flush = getattr(session, "flush", None)
                    if flush is not None and getattr(session, "bidi", False):
                        await flush()
                    elif not getattr(session, "bidi", False):
                        await session.finish()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tts_failed = tts_failed or exc
                    tts_disabled = True
                    logger.warning("controller.tts_finish_degraded turn_id=%s error=%s", ws.turn_id, exc)
                if reader is not None:
                    try:
                        # Bound stalls, not the duration of a long answer.
                        # Even after a send failure all cached PCM must drain.
                        reader_progress_marker = None
                        deadline = asyncio.get_running_loop().time() + 120.0
                        while not reader.done():
                            snapshot = playback_progress()
                            progress = (snapshot["returned_ms"], snapshot["played_ms"])
                            now = asyncio.get_running_loop().time()
                            if progress != reader_progress_marker:
                                reader_progress_marker = progress
                                deadline = now + max(120.0, float(snapshot["remaining_ms"]) / 1000 + 30.0)
                            if now >= deadline:
                                raise TimeoutError("TTS 接收或播放长时间没有进展")
                            await asyncio.wait({reader}, timeout=.2)
                        await reader
                        alignment_events = getattr(session, "alignment_events", ())
                        for alignment in alignment_events[alignment_cursor:]:
                            if alignment is not None:
                                resolver.add_provider_alignment(alignment)
                        alignment_cursor = len(alignment_events)
                        # A Bidi flush may have no usable subtitle or sentence
                        # boundary.  Finish local fallback spans at the last
                        # returned PCM position; never infer word timing from
                        # text length.
                        if getattr(session, "generation_finished", False) and self.player is not None:
                            resolver.complete_pending(int(playback_progress()["returned_ms"]))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        tts_failed = tts_failed or exc
                        tts_disabled = True
                        logger.warning("controller.tts_receive_degraded turn_id=%s error=%s", ws.turn_id, exc)
            # ``task_flushed`` only confirms that the Bidi server has ended
            # this generation.  It is not evidence that the server produced
            # usable PCM.  Calling that a completed speech made the UI show
            # a fully answered request while the user heard nothing.  Record
            # the transport fact before final status/delivery resolution so
            # the transaction can be retried once by EpisodeSpeechOutput.
            ws.timings["tts_audio_bytes"] = audio_returned_bytes
            ws.timings["tts_text_sent"] = int(tts_text_sent)
            ws.timings["playback_output_complete"] = False
            if session and tts_text_sent and audio_returned_bytes == 0 and tts_failed is None:
                tts_failed = RuntimeError("MiniMax TTS 已确认结束但未返回 PCM")
                tts_disabled = True
                logger.error(
                    "controller.tts_empty_audio turn_id=%s transport=%s generation_finished=%s",
                    ws.turn_id,
                    transport,
                    getattr(session, "generation_finished", False),
                )
            if self.player:
                playback_timeout = max(120.0, 30.0 + self.player.buffered_ms / 1000)
                try:
                    await self.player.wait_until_output_complete(timeout_seconds=playback_timeout)
                    ws.timings["playback_output_complete"] = True
                    ws.timings["playback_output_completed_at"] = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    tts_failed = tts_failed or exc
                    tts_disabled = True
                    logger.warning("controller.playback_degraded turn_id=%s error=%s", ws.turn_id, exc)
            if session:
                if tts_failed is not None and self._valid(token):
                    self.state.current["tts_status"] = "DEGRADED"
                elif self._valid(token):
                    self.state.current["tts_status"] = "FINISHED"
                await emit_progress(
                    status=self.state.current.get("tts_status"),
                    trigger="playback_finished",
                    force=True,
                )
            if tts_failed is not None and self._valid(token):
                self.state.current["tts_status"] = "DEGRADED"
                await self._detail(
                    "tts.status",
                    provider="minimax",
                    status="DEGRADED",
                    waiting_text="",
                    audio_buffer_ms=self.player.buffered_ms if self.player else 0,
                    error=str(tts_failed),
                )
            return ws.staged_response
        finally:
            await stream.aclose()
            progress_stop.set()
            if progress_task and not progress_task.done():
                progress_task.cancel()
                await asyncio.gather(progress_task, return_exceptions=True)
            if sender_task and not sender_task.done():
                sender_task.cancel()
                await asyncio.gather(sender_task, return_exceptions=True)
            if reader:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            # Isolated replacement sessions are valid for their one active
            # transaction, but never become an implicit reusable session.
            retain_session = bool(
                getattr(session, "reusable", False)
                and tts_failed is None
                and not getattr(session, "_isolated_transaction", False)
            )
            if session and not retain_session:
                await session.close()
            self._release_tts_session(
                ws,
                session,
                retain=retain_session,
            )
