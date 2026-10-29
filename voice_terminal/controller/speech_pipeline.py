from __future__ import annotations
import asyncio
import logging
import time
from contextlib import nullcontext
from ..stream.text_bridge import TextBridge
from ..tts.alignment import DeliveryResolver
from ..tts.scheduling import DirectStreamingPolicy, PlaybackProgress, PlaybackWindowPolicy, TtsSegmentReceipt
from ..tts.sentence_segmenter import PunctuationSentenceSegmenter, split_complete_text
from ..tts.text_merger import BidiTextMerger
from ..quiet_hours import PassiveQuietSuppressed
from ..gpu_performance import GpuSnapshot
from .speech_admission import ADMISSION_STAGING_CHARS, AdmissionState, SubmittedClause
from .stream_buffer import FLUSH_DUE, buffered_stream

logger = logging.getLogger(__name__)


# How long one sent frame may wait for the provider to return its first PCM
# before the lead window is evaluated again.  Bounded so a slow provider keeps
# the previous direct behaviour instead of stalling the transaction.
ADMISSION_CATCH_UP_SECONDS = 0.5

# 起播／恢复储备与两道队列门槛之间的安全间隔。两道门槛都会让"攒够储备"变成
# 不可能事件，从而把整轮播报挂死（详见 safe_stream_reserve_ms）：
#   * read_audio 在队列高于 tts_local_buffer_ms 时暂停入队；
#   * wait_for_sentence_window 只在"当前块未播内容 ≤ sentence_prefetch_ms"时提交下一块。
# 留出该间隔吸收回调粒度（20 毫秒）、设备尾音与边界取整。
STREAM_RESERVE_MARGIN_MS = 500
# R≥1 时自适应抬高的恢复门槛上限；实际值还要受队列上限与句窗门约束。
ADAPTIVE_RESUME_MAX_MS = 2000
# 恢复门槛必须严格小于入队上限，否则攒够门槛的音频会被 read_audio 的 cap 卡住。
ADAPTIVE_RESUME_CAP_MARGIN_MS = 200


def safe_stream_reserve_ms(*, requested_ms: int, cap_ms: int, prefetch_ms: int,
                           cap_margin_ms: int = 0,
                           prefetch_margin_ms: int = STREAM_RESERVE_MARGIN_MS) -> int:
    """safe_stream_reserve_ms（流式储备安全上界）：把储备夹在两道队列门槛之下。

    起播储备与恢复储备都必须**可达**，否则播放永远不开始：

    * ``cap_ms``：``read_audio`` 在播放器队列高于该值时就停止入队
      （``speech_pipeline.py`` 的 read_audio），储备超过它永远攒不够；
    * ``prefetch_ms − margin``：句窗门提交下一块的条件（``wait_for_sentence_window``）。
      储备若超过它，首块音频用尽后播放仍不起播，而发送方正卡在句窗门上等播放推进，
      于是既不合成也不再入队 —— 整轮活锁（实测 startup=4000／prefetch=3000 时
      120 秒内播放 0 毫秒）。
    """

    ceiling = min(
        max(0, int(cap_ms) - max(0, int(cap_margin_ms))),
        max(0, int(prefetch_ms) - max(0, int(prefetch_margin_ms))),
    )
    return max(0, min(int(requested_ms), ceiling))


def stream_reserve_clamp_reason(*, requested_ms: int, cap_ms: int, prefetch_ms: int,
                                cap_margin_ms: int = 0) -> str:
    """说明储备被夹低的原因：queue_cap / sentence_prefetch，两者可同时成立。"""

    reasons = []
    if int(requested_ms) > max(0, int(cap_ms) - max(0, int(cap_margin_ms))):
        reasons.append("queue_cap")
    if int(requested_ms) > max(0, int(prefetch_ms) - STREAM_RESERVE_MARGIN_MS):
        reasons.append("sentence_prefetch")
    return "+".join(reasons) if reasons else "none"


def gpu_snapshot_fields(client) -> dict:
    """读取显卡取证字段，供"合成慢于实时"现场定位使用。

    优先用 ``AudioCppTtsClient.gpu_performance`` 的 ``contention()``：它在既有
    NVML 绑定（频率/功耗/利用率）之上补了两项现场必需证据——**外部显卡进程**
    与 **Windows 硬件加速 GPU 调度（HAGS）状态**。这两项正是"是外部程序在抢
    显卡，还是原生推理本身变慢"的判别依据；只靠原来那个快照无法区分。

    采样失败一律降级为全空字段，绝不抛出、绝不阻塞播报。调用点只在判定降级时
    执行一次，正常运行路径不会触发任何 NVML 进程枚举。
    """

    provider = getattr(client, "gpu_performance", None)
    take = getattr(provider, "contention", None)
    if callable(take):
        try:
            reading = take()
        except Exception:
            logger.debug("controller.gpu_contention_failed", exc_info=True)
        else:
            if isinstance(reading, dict) and reading:
                return reading
    # 退路：只能拿到频率/功耗/利用率，至少保持字段完整。
    snapshot = getattr(provider, "snapshot", None)
    if not callable(snapshot):
        return GpuSnapshot().as_dict()
    try:
        taken = snapshot()
    except Exception:
        logger.debug("controller.gpu_snapshot_failed", exc_info=True)
        return GpuSnapshot().as_dict()
    as_dict = getattr(taken, "as_dict", None)
    if callable(as_dict):
        try:
            return as_dict()
        except Exception:
            logger.debug("controller.gpu_snapshot_failed", exc_info=True)
    return GpuSnapshot().as_dict()


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
            return self.tts.session(fresh=fresh, turn_id=ws.turn_id)
        except TypeError:
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

    async def _play_stream(self, ws, token, deltas, *, speech_id: str | None = None,
                           handle=None) -> str:
        tts_client = handle.tts_client if handle is not None else self.tts
        if tts_client is None and not hasattr(self.tts, "bind"):
            tts_client = self.tts
        tts_backend = handle.tts_backend if handle is not None else "minimax"
        transport = getattr(tts_client, "transport", "websocket") if tts_client is not None else "websocket"

        async def synthesis_permission():
            # synthesis_permission（合成许可）与设备播放许可独立。
            production = getattr(handle, "production_ready", None)
            if production is not None:
                await production.wait()
            if not self._valid(token):
                raise asyncio.CancelledError

        await synthesis_permission()
        session = None
        reader = None
        sender_task = None
        progress_task = None
        parts = []
        tts_failed: Exception | None = None
        tts_disabled = False
        playback_key = ws.playback_key
        configured_scheduling = getattr(
            tts_client,
            "effective_scheduling",
            getattr(tts_client, "scheduling", "auto") if tts_client is not None else "auto",
        )
        sentence_window_enabled = configured_scheduling == "sentence_window"
        if sentence_window_enabled:
            bridge = PunctuationSentenceSegmenter(
                first_clause_timeout_ms=max(
                    1, int(getattr(tts_client, "first_clause_timeout_ms", 400))
                )
            )
        elif transport == "websocket_bidi":
            bridge = BidiTextMerger(max_chars=128, max_wait_ms=30)
        else:
            bridge = TextBridge(**self.bridge_options)
        # The lead window and the legacy per-segment completion wait are two
        # independent behaviours.  A Bidi server breaks sentences itself, so
        # one sent frame is never one server segment and waiting for "this
        # frame finished" would stall; its lead window is still useful.
        if configured_scheduling == "playback_window":
            lead_ms = max(1, int(getattr(tts_client, "admission_lead_ms", 0) or self.options.tts_lead_ms))
        elif self.options.tts_pacing:
            lead_ms = max(1, int(self.options.tts_lead_ms))
        else:
            lead_ms = 0
        lead_window_enabled = bool(lead_ms and self.player is not None and not sentence_window_enabled)
        segment_waits_enabled = bool(lead_window_enabled and transport != "websocket_bidi")
        scheduling_policy = (
            PlaybackWindowPolicy(lead_ms) if lead_window_enabled else DirectStreamingPolicy()
        )
        # Text accepted after ``sent_chars`` is still ours to replace.
        state = AdmissionState(
            lead_ms=lead_ms,
            restage=split_complete_text if sentence_window_enabled else None,
            sentence_window=sentence_window_enabled,
        )
        if handle is not None:
            handle.admission = state
        audio_signal = asyncio.Event()
        progress_stop = asyncio.Event()
        producer_done = False
        audio_returned_bytes = 0
        tts_text_sent = False
        prefetch_miss_boundary_ms: int | None = None
        sentence_prefetch_ms = max(1, int(getattr(tts_client, "sentence_prefetch_ms", 3000)))
        inference_boundary = getattr(self, "inference_boundary", None)  # inference_boundary（请求期间提交闸门）。
        sentence_tick_seconds = max(0.001, int(getattr(tts_client, "sentence_tick_ms", 50)) / 1000)
        sentence_min_effective_chars = max(
            1, int(getattr(tts_client, "sentence_min_effective_chars", 10))
        )  # sentence_min_effective_chars（每个锁定文本块最少有效字符）。
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

        async def start_tts_session(active_session):
            # MiniMax can retry a connection handshake before accepting text;
            # other providers keep their existing single-start behavior.
            if getattr(active_session, "managed_controls", False):
                await active_session.start()
                return active_session
            retry_start = getattr(tts_client, "start_session_with_retry", None)
            if callable(retry_start):
                return await retry_start(active_session)
            await active_session.start()
            return active_session

        def sync_turn_timings() -> None:
            shared = getattr(timing_owner, "_turn_timings", {}).get(ws.turn_id)
            if isinstance(shared, dict):
                ws.timings.update(shared)
        audio_bytes_per_second = (
            self.player.sample_rate * self.player.channels * 2
            if self.player is not None else 32000 * 2
        )

        def reject_audio() -> None:
            """异常生成立即停止待播队列，已经听到的正文不自动重播。"""
            if self.player is not None:
                self.player.stop_and_snapshot(playback_key)
        await synthesis_permission()
        if not self._valid(token) or not ws.committed:
            return ""
        armed_startup_ms = 0
        armed_resume = 0
        if self.player:
            self.player.allow(playback_key)
            configure_buffer = getattr(self.player, "configure_stream_buffer", None)
            startup_ms = int(getattr(tts_client, "playback_startup_buffer_ms", 0) or 0)
            resume_ms = int(getattr(tts_client, "playback_resume_buffer_ms", 0) or 0)
            if callable(configure_buffer) and (startup_ms > 0 or resume_ms > 0):
                # startup_ms=0（无起播储备）仍须单独装配欠载恢复门槛。
                # 起播储备必须能在句窗门放行之前达成（safe_stream_reserve_ms）。
                # 这里的夹紧不是限流，而是把"隐式安全余量"变成显式不变量：此前只有
                # min(tts_local_buffer_ms, startup_ms) 一道，句窗门那条约束完全没有保护。
                armed_startup = safe_stream_reserve_ms(
                    requested_ms=startup_ms,
                    cap_ms=self.options.tts_local_buffer_ms,
                    prefetch_ms=sentence_prefetch_ms,
                )
                armed_resume = (
                    safe_stream_reserve_ms(
                        requested_ms=resume_ms,
                        cap_ms=self.options.tts_local_buffer_ms,
                        prefetch_ms=sentence_prefetch_ms,
                    )
                    if resume_ms > 0 else 0
                )
                armed_startup_ms = armed_startup
                configure_buffer(playback_key, startup_ms=armed_startup, resume_ms=armed_resume)
                # 装配必须留痕：startup_ms=0（无起播储备）是明确策略，不能静默跳过恢复闸门。
                logger.info("audio.player_stream_buffer_armed key=%s startup_ms=%d resume_ms=%d "
                            "client=%s", playback_key, armed_startup, armed_resume,
                            type(tts_client).__name__)
                if armed_startup < startup_ms or (resume_ms > 0 and armed_resume < resume_ms):
                    # 夹紧必须说明原因与数值，否则又回到"静默失效"那类缺陷。
                    logger.warning(
                        "controller.tts_stream_reserve_clamped key=%s requested_startup_ms=%d "
                        "armed_startup_ms=%d requested_resume_ms=%d armed_resume_ms=%d "
                        "cap_ms=%d prefetch_ms=%d reason=%s client=%s",
                        playback_key, startup_ms, armed_startup, resume_ms, armed_resume,
                        self.options.tts_local_buffer_ms, sentence_prefetch_ms,
                        stream_reserve_clamp_reason(
                            requested_ms=max(startup_ms, resume_ms),
                            cap_ms=self.options.tts_local_buffer_ms,
                            prefetch_ms=sentence_prefetch_ms,
                        ),
                        type(tts_client).__name__,
                    )
            else:
                # 没有声明起播或恢复储备的后端按到达节奏直接播放。
                logger.debug("audio.player_stream_buffer_skipped key=%s startup_ms=%d resume_ms=%d callable=%s "
                             "client=%s", playback_key, startup_ms, resume_ms, callable(configure_buffer),
                             type(tts_client).__name__)
            if handle is not None and getattr(handle, "playback_ready", None) is not None and not handle.playback_ready.is_set():
                hold = getattr(self.player, "hold", None)
                if callable(hold):
                    handle.player_held = hold(playback_key) is not False

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

        # 每轮降级信号（F3）与本轮自适应储备（F2）共用的事实收集。
        # ``gpu`` 在**首次**判定降级时采样：那时合成仍在进行，显卡争用才看得见；
        # 若整轮只在收尾时才判出降级，则退回收尾时刻采样。
        degradation = {
            "active": False,   # 本轮是否出现过 underrun 或 rtf ≥ 1。
            "max_rtf": 0.0,    # 本轮观测到的最大实时因子。
            "resume_ms": 0,    # 已抬高的恢复门槛（0 表示仍用装配值）。
            "gpu": None,       # 首次判定降级时的显卡快照字段。
            "gpu_task": None,  # 显卡取证在线程池执行，避免阻塞后续分段。
            "reported": False,  # 是否已经输出过降级 WARNING。
        }
        adaptive_resume_ms = safe_stream_reserve_ms(
            requested_ms=ADAPTIVE_RESUME_MAX_MS,
            cap_ms=self.options.tts_local_buffer_ms,
            prefetch_ms=sentence_prefetch_ms,
            cap_margin_ms=ADAPTIVE_RESUME_CAP_MARGIN_MS,
        )

        async def capture_gpu() -> None:
            started_at = time.perf_counter()
            snapshot = await asyncio.to_thread(gpu_snapshot_fields, tts_client)
            snapshot["sample_elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
            degradation["gpu"] = snapshot

        def start_gpu_capture() -> None:
            if degradation["gpu_task"] is None:
                degradation["gpu_task"] = asyncio.create_task(capture_gpu(), name="tts-gpu-diagnostics")

        def degradation_facts() -> tuple[int, float]:
            diagnostics = getattr(self.player, "diagnostics", None)
            underrun_ms = 0
            if isinstance(diagnostics, dict):
                try:
                    underrun_ms = int(diagnostics.get("underrun_ms", 0) or 0)
                except (TypeError, ValueError):
                    underrun_ms = 0
            rtf = 0.0
            metrics = getattr(session, "request_metrics", None) if session is not None else None
            if isinstance(metrics, (list, tuple)) and metrics:
                last = metrics[-1]
                if isinstance(last, dict):
                    try:
                        rtf = float(last.get("rtf") or 0.0)
                    except (TypeError, ValueError):
                        rtf = 0.0
            return underrun_ms, rtf

        def apply_adaptive_reserve() -> None:
            """F2：实测慢于实时或已发生欠载时抬高恢复门槛。

            R>1 时静音占比恒为 1−1/R，应用侧改不了；能改的是**碎裂粒度**：
            400 毫秒门槛下每约 2.1 秒断一次，抬高后变成句级停顿。R<1 时既不会
            有 ``underrun_ms`` 也不会有 ``rtf ≥ 1``，本函数直接返回，**零代价**。
            """

            nonlocal armed_resume
            underrun_ms, rtf = degradation_facts()
            if rtf > degradation["max_rtf"]:
                degradation["max_rtf"] = rtf
            if underrun_ms <= 0 and rtf < 1.0:
                return
            if not degradation["active"]:
                degradation["active"] = True
                start_gpu_capture()
            setter = getattr(self.player, "set_stream_reserve", None)
            if not callable(setter) or adaptive_resume_ms <= 0:
                return
            current = int(armed_resume or 0)
            if current >= adaptive_resume_ms:
                return
            try:
                applied = setter(playback_key, resume_ms=adaptive_resume_ms) is not False
            except Exception:
                logger.debug("controller.tts_resume_adapt_failed", exc_info=True)
                return
            if applied:
                armed_resume = adaptive_resume_ms
                degradation["resume_ms"] = adaptive_resume_ms

        def playback_degradation_fields() -> dict[str, object]:
            diagnostics = getattr(self.player, "diagnostics", None)
            underrun_ms = 0
            if isinstance(diagnostics, dict):
                try:
                    underrun_ms = int(diagnostics.get("underrun_ms", 0) or 0)
                except (TypeError, ValueError):
                    underrun_ms = 0
            played_ms = 0
            try:
                played_ms = int(self.player.progress(playback_key).get("played_ms", 0) or 0)
            except Exception:
                played_ms = 0
            audible_ms = underrun_ms + played_ms
            resume_ms = int(degradation["resume_ms"] or armed_resume or 0)
            reserve = getattr(self.player, "stream_reserve_ms", None)
            if callable(reserve):
                try:
                    reading = reserve(playback_key) or {}
                    if reading.get("armed"):
                        resume_ms = int(reading.get("resume_ms", resume_ms) or 0)
                except Exception:
                    logger.debug("controller.tts_reserve_read_failed", exc_info=True)
            return {
                "turn_id": ws.turn_id,
                "max_rtf": round(float(degradation["max_rtf"]), 3),
                "underrun_ms": underrun_ms,
                "played_ms": played_ms,
                "silence_ratio": round(underrun_ms / audible_ms, 4) if audible_ms > 0 else 0.0,
                "resume_ms": resume_ms,
                "startup_ms": int(armed_startup_ms or 0),
                "gpu": degradation["gpu"] or {"reason": "snapshot-unavailable"},
            }

        async def report_playback_degradation() -> None:
            """F3：本轮若慢于实时或出现过静音，输出一条可判定的降级信号。"""

            if degradation["reported"]:
                return
            underrun_ms = 0
            diagnostics = getattr(self.player, "diagnostics", None)
            if isinstance(diagnostics, dict):
                try:
                    underrun_ms = int(diagnostics.get("underrun_ms", 0) or 0)
                except (TypeError, ValueError):
                    underrun_ms = 0
            if underrun_ms <= 0 and float(degradation["max_rtf"]) < 1.0:
                return
            degradation["reported"] = True
            start_gpu_capture()
            try:
                await asyncio.wait_for(degradation["gpu_task"], timeout=0.25)
            except Exception as exc:
                degradation["gpu"] = {"reason": f"snapshot-{type(exc).__name__}"}
            fields = playback_degradation_fields()
            gpu = fields.get("gpu") or {}
            # 进程表只证明进程存在，不证明它占用了多少算力；查询失败也不能视为无竞争。
            external = gpu.get("external_processes") if isinstance(gpu, dict) else None
            own = gpu.get("own_processes") if isinstance(gpu, dict) else None
            scheduling = gpu.get("scheduling") if isinstance(gpu, dict) else None
            logger.warning(
                "controller.tts_playback_degraded turn_id=%s max_rtf=%.3f underrun_ms=%d "
                "played_ms=%d silence_ratio=%.4f resume_ms=%d startup_ms=%d "
                "gpu_external=%d gpu_own=%d gpu_hags=%s gpu_clock_mhz=%s gpu_power_w=%s "
                "gpu_util_pct=%s gpu_external_detail=%s gpu_unavailable=%s "
                "gpu_sample_ms（显卡采样耗时毫秒）=%s gpu_query_available（进程表可用状态）=%s gpu=%s",
                fields["turn_id"], fields["max_rtf"], fields["underrun_ms"],
                fields["played_ms"], fields["silence_ratio"], fields["resume_ms"],
                fields["startup_ms"],
                len(external) if isinstance(external, list) else -1,
                len(own) if isinstance(own, list) else -1,
                (scheduling or {}).get("hwschmode") if isinstance(scheduling, dict) else None,
                gpu.get("clock_mhz") if isinstance(gpu, dict) else None,
                gpu.get("power_w") if isinstance(gpu, dict) else None,
                gpu.get("utilization_percent") if isinstance(gpu, dict) else None,
                external if isinstance(external, list) else None,
                gpu.get("reason") if isinstance(gpu, dict) else None,
                gpu.get("sample_elapsed_ms") if isinstance(gpu, dict) else None,
                gpu.get("process_query_available") if isinstance(gpu, dict) else None,
                gpu,
            )
            ws.timings["playback_degraded"] = True
            ws.timings["playback_max_rtf"] = fields["max_rtf"]
            ws.timings["playback_underrun_ms"] = fields["underrun_ms"]
            ws.timings["playback_silence_ratio"] = fields["silence_ratio"]
            ws.timings["playback_resume_ms"] = fields["resume_ms"]
            ws.timings["playback_startup_ms"] = fields["startup_ms"]
            ws.timings["playback_gpu_external"] = (
                len(external) if isinstance(external, list) else None)
            try:
                await self._detail("tts.playback_degraded", **fields)
            except Exception:
                logger.debug("controller.tts_playback_degraded_detail_failed", exc_info=True)

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
                    provider=tts_backend,
                    status=current_status,
                    waiting_text=waiting_text if waiting_text is not None else self.state.current.get("tts_text", ""),
                    audio_buffer_ms=int(snapshot["queued_ms"]),
                    audio_returned_ms=int(snapshot["returned_ms"]),
                    audio_played_ms=int(snapshot["played_ms"]),
                    audio_output_estimated_ms=int(snapshot["output_estimated_ms"]),
                    audio_remaining_ms=int(snapshot["remaining_ms"]),
                    # playback_caption（实时语音字幕）：不包含聊天工具消息和截图附件。
                    playback_caption=resolver.caption_progress(
                        snapshot, handle.text if handle is not None else "".join(parts)
                    ),
                    pacing_enabled=lead_window_enabled,
                    segment_waits_enabled=segment_waits_enabled,
                    sentence_window_enabled=sentence_window_enabled,
                    sent_chars=state.sent_chars,
                    staged_chars=state.staged_chars,
                    lead_ms=lead_ms,
                    trigger=trigger,
                )

        async def read_audio(active_session):
            nonlocal audio_returned_bytes, completed_markers, alignment_cursor

            audio_iterator = None
            first_enqueued = False
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
                    if "first_audio" not in ws.timings:
                        from ..speaker.audit import audit
                        ws.timings["first_audio"] = time.monotonic()
                        audit("synthesis_first_audio（合成首音频）", turn_id=ws.turn_id,
                              generation=ws.generation, timings=dict(ws.timings))
                    ws.timings.setdefault("first_pcm", time.monotonic())
                    if self.player:
                        if getattr(active_session, "audio_rejected", False):
                            return
                        # Keep the native player bounded.  Audio held by the
                        # provider spool is included in audio_remaining_ms
                        # through the session's network receive counter.
                        while self.player.buffered_ms > max(1, self.options.tts_local_buffer_ms):
                            await asyncio.sleep(.02)
                            if getattr(active_session, "audio_rejected", False):
                                return
                            if not self._valid(token):
                                return
                        await self._wait_for_playback_permission(ws, token)
                        if not self._valid(token):
                            return
                        if getattr(active_session, "audio_rejected", False):
                            return
                        self.player.enqueue(playback_key, pcm)
                        if not first_enqueued:
                            first_enqueued = True
                            ready_at = getattr(active_session, "first_audio_ready_at", None)
                            logger.info(
                                "controller.tts_first_pcm_enqueued turn=%s speech=%s session=%s key=%s "
                                "chunk_bytes（首块字节数）=%d ready_to_enqueue_ms（合成首音至入队毫秒）=%.1f",
                                ws.turn_id, speech_id, getattr(active_session, "session_tag", None), playback_key,
                                len(pcm), (time.monotonic() - ready_at) * 1000 if ready_at is not None else -1.0,
                            )
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
                finish_stream = getattr(self.player, "finish_stream", None)
                if callable(finish_stream):
                    finish_stream(playback_key)
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
            if not segment_waits_enabled:
                return
            await scheduling_policy.wait_for_audio(
                before_bytes,
                lambda: audio_returned_bytes,
                lambda: self._valid(token) and not tts_disabled,
                audio_signal,
                failure=reader_failure,
            )
            await emit_progress(status="STREAMING", trigger="audio_available")

        async def wait_for_provider_catch_up(before_bytes: int) -> None:
            """Let the provider answer the frame we just sent, then re-measure.

            The lead window compares returned audio against playback.  A burst
            of frames sent faster than the provider can synthesise makes that
            measurement stale -- at the very start of an answer nothing has
            been returned yet, so ``remaining_ms`` reads zero and the window
            stays open for the whole response.  Waiting for one new PCM chunk
            keeps the next window decision based on what the provider actually
            produced.  The wait is bounded and never fatal: a slow answer keeps
            the previous behaviour instead of degrading the transaction.
            """

            if not lead_window_enabled:
                return
            loop = asyncio.get_running_loop()
            deadline = loop.time() + ADMISSION_CATCH_UP_SECONDS
            while audio_returned_bytes <= before_bytes:
                if not self._valid(token) or tts_disabled:
                    return
                # The provider has ended its PCM stream.  There can be no
                # catch-up chunk, so do not spend one timeout per staged
                # frame while draining the Voice Server's text queue.
                if reader is not None and reader.done():
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                audio_signal.clear()
                if audio_returned_bytes > before_bytes:
                    return
                try:
                    await asyncio.wait_for(audio_signal.wait(), remaining)
                except asyncio.TimeoutError:
                    return

        async def wait_for_playback_window() -> None:
            if not lead_window_enabled:
                return
            if state.overrun:
                # Playback stalled long enough to exhaust the staging budget.
                # Release text immediately rather than throttling the LLM
                # reader; the admission benefit is simply lost for this turn.
                if not state.overrun_reported:
                    state.overrun_reported = True
                    logger.warning(
                        "controller.tts_admission_overrun turn_id=%s staged_chars=%d limit=%d",
                        ws.turn_id, state.staged_chars, state.char_limit,
                    )
                    await self._detail(
                        "tts.admission_overrun",
                        staged_chars=state.staged_chars, limit=state.char_limit,
                    )
                raise RuntimeError("播报文本暂存超过预算，已停止本次合成。")
            await scheduling_policy.wait_before_send(
                lambda: PlaybackProgress.from_mapping(playback_progress()),
                lambda: self._valid(token) and not tts_disabled,
                lambda snapshot: emit_progress(status="WAITING_PLAYBACK", trigger="lead_window"),
            )

        async def wait_for_sentence_window() -> None:
            if not sentence_window_enabled or self.player is None:
                return
            started_at = time.perf_counter()
            before = playback_progress()
            await wait_for_sentence_window_inner()
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            if elapsed_ms >= 50:
                logger.info(
                    "controller.tts_sentence_window_wait turn=%s speech=%s key=%s "
                    "elapsed_ms（句窗等待毫秒）=%.1f remaining_before_ms（等待前剩余音频毫秒）=%d "
                    "remaining_after_ms（等待后剩余音频毫秒）=%d",
                    ws.turn_id, speech_id, playback_key, elapsed_ms,
                    int(before["remaining_ms"]), int(playback_progress()["remaining_ms"]),
                )

        async def wait_for_sentence_window_inner() -> None:
            """Admit one more complete clause from local staging.

            Receipts are cumulative PCM boundaries.  Keeping at most two of
            them means the provider owns only the currently playing block and
            one prefetched block; every later clause remains locally
            replaceable regardless of provider synthesis speed.
            """

            nonlocal prefetch_miss_boundary_ms
            if not sentence_window_enabled or self.player is None:
                return
            while self._valid(token) and not tts_disabled:
                output_ms = int(playback_progress()["output_estimated_ms"])
                for boundary_ms, had_prefetched_successor in state.prune_played(
                    output_estimated_ms=output_ms,
                    bytes_per_second=audio_bytes_per_second,
                ):
                    if not had_prefetched_successor and output_ms > boundary_ms:
                        prefetch_miss_boundary_ms = boundary_ms
                active = tuple(state.submitted_clauses)
                if not active:
                    return
                current_end_ms = state._boundary_ms(active[0], audio_bytes_per_second)
                if current_end_ms is None:
                    await asyncio.sleep(sentence_tick_seconds)
                    continue
                remaining_ms = max(0, current_end_ms - output_ms)
                if (inference_boundary is not None and inference_boundary.prefilling(handle)
                        and len(active) == 1):
                    return
                if len(active) == 1 and remaining_ms <= sentence_prefetch_ms:
                    return
                await emit_progress(
                    status="WAITING_PLAYBACK",
                    trigger="sentence_window",
                )
                await asyncio.sleep(sentence_tick_seconds)

        async def next_segment() -> str | None:
            while True:
                segment = (
                    state.pop_sentence_block(
                        min_effective_chars=sentence_min_effective_chars,
                        allow_short=producer_done,
                    ) if sentence_window_enabled else state.pop()
                )
                if segment is not None:
                    return segment
                if producer_done or tts_disabled or not self._valid(token):
                    state.close()
                    return None
                state.signal.clear()
                await state.signal.wait()

        async def tts_sender() -> None:
            nonlocal session, reader, progress_task, tts_failed, tts_disabled, tts_text_sent
            nonlocal prefetch_miss_boundary_ms, completed_markers
            sent_any = False
            try:
                while True:
                    if inference_boundary is not None:
                        ready = await inference_boundary.wait_ready(
                            handle, lambda: self._valid(token) and not tts_disabled,
                        )
                        if not ready:
                            return
                    if sent_any and producer_done and not state.staged:
                        state.close()
                        return
                    if sent_any:
                        # Wait for the admission window before taking ownership
                        # of the next unsent frame.  A frame that is merely
                        # staged can still be replaced by a later decision;
                        # one already popped cannot.
                        await synthesis_permission()
                        if not self._valid(token) or not ws.committed:
                            return
                        if sentence_window_enabled:
                            await wait_for_sentence_window()
                        else:
                            await wait_for_playback_window()
                        if not self._valid(token):
                            return
                    segment = await next_segment()
                    if segment is None:
                        return
                    epoch = state.epoch
                    if not self._valid(token):
                        return
                    await synthesis_permission()
                    if not self._valid(token) or not ws.committed:
                        return
                    if session is None:
                        await self._await_tts_cancel_barrier()
                        await synthesis_permission()
                        if not self._valid(token) or not ws.committed:
                            return
                        sync_turn_timings()
                        session = self._new_tts_session(ws)
                        if hasattr(session, "audio_rejected_callback"):
                            session.audio_rejected_callback = reject_audio
                        if hasattr(session, "first_audio_callback"):
                            session.first_audio_callback = lambda: ws.timings.setdefault("synthesis_first_audio", time.monotonic())
                        self._bind_tts_session(ws, session)
                        checkout = getattr(tts_client, "checkout_prewarm", None)
                        if callable(checkout):
                            try:
                                warm = bool(checkout(session, turn_id=ws.turn_id))
                            except TypeError:
                                warm = bool(checkout(session))
                        else:
                            warm = False
                        checkout_reason = getattr(
                            tts_client, "last_prewarm_checkout_reason",
                            "not_available" if not callable(checkout) else "not_prewarmed",
                        )
                        warm_connection_id = getattr(session, "connection_id", None) if warm else None
                        ws.timings.setdefault("tts_session_start", time.monotonic())
                        try:
                            await synthesis_permission()
                            if not self._valid(token) or not ws.committed:
                                return
                            session = await start_tts_session(session)
                            self._bind_tts_session(ws, session)
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
                                    try:
                                        warm = bool(checkout(session, turn_id=ws.turn_id))
                                    except TypeError:
                                        warm = bool(checkout(session))
                            session_usable = (
                                getattr(session, "reusable", True)
                                if getattr(session, "bidi", False)
                                else getattr(session, "connection_ready", True)
                            )
                            if not session_usable:
                                # A failed/cancelled prewarm may have closed
                                # the shared candidate while this sender was
                                # waiting for it.  Fall back to a fresh cold
                                # session rather than sending text to a dead
                                # socket.
                                await synthesis_permission()
                                if not self._valid(token) or not ws.committed:
                                    return
                                session = self._new_tts_session(ws, fresh=True)
                                self._bind_tts_session(ws, session)
                                ws.timings["tts_session_start"] = time.monotonic()
                                await synthesis_permission()
                                if not self._valid(token) or not ws.committed:
                                    return
                                session = await start_tts_session(session)
                                self._bind_tts_session(ws, session)
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
                                try:
                                    warm = bool(checkout(session, turn_id=ws.turn_id))
                                except TypeError:
                                    warm = bool(checkout(session))
                            checkout_reason = getattr(
                                tts_client, "last_prewarm_checkout_reason",
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
                            await synthesis_permission()
                            if not self._valid(token) or not ws.committed:
                                return
                            session = self._new_tts_session(ws, fresh=True)
                            self._bind_tts_session(ws, session)
                            ws.timings["tts_session_start"] = time.monotonic()
                            await synthesis_permission()
                            if not self._valid(token) or not ws.committed:
                                return
                            session = await start_tts_session(session)
                            self._bind_tts_session(ws, session)
                            ws.timings["tts_session_ready"] = time.monotonic()
                            sync_turn_timings()
                        await synthesis_permission()
                        if not self._valid(token) or not ws.committed:
                            return
                        reader = asyncio.create_task(read_audio(session), name=f"tts-audio-{ws.turn_id}")
                        progress_task = asyncio.create_task(monitor_progress(), name=f"tts-progress-{ws.turn_id}")
                    if sent_any:
                        await synthesis_permission()
                        if not self._valid(token) or not ws.committed:
                            return
                    if not self._valid(token):
                        return
                    await synthesis_permission()
                    if not self._valid(token) or not ws.committed:
                        return
                    receiver_error = await reader_exception()
                    if receiver_error is not None:
                        raise receiver_error
                    if state.epoch != epoch:
                        # A replacement invalidated this frame while we held
                        # it.  It was never sent, so drop it silently.
                        continue
                    await synthesis_permission()
                    if not self._valid(token):
                        return
                    if state.epoch != epoch:
                        continue
                    submission = (inference_boundary.submission(
                        handle, lambda: self._valid(token) and not tts_disabled,
                    ) if inference_boundary is not None else nullcontext(True))
                    async with submission as allowed:
                        if not allowed:
                            return
                        production = getattr(handle, "production_ready", None)
                        if production is not None and not production.is_set():
                            # 暂停与提交原子检查：释放推理锁，保留尚未发送的块，恢复后重试。
                            if state.epoch == epoch:
                                state.staged.appendleft(segment)
                                state.staged_chars += len(segment)
                            continue
                        if state.epoch != epoch:
                            # 推理期间已改写：丢弃手中尚未发送的旧块。
                            continue
                        before_bytes = audio_returned_bytes
                        before_segments = getattr(session, "completed_segments", 0)
                        ws.timings.setdefault("first_tts_text", time.monotonic())
                        ws.timings.setdefault("first_task_continue", time.monotonic())
                        self.state.current["tts_text"] = segment
                        state.sending = True  # sending（提交中，禁止修订）。
                        ws.timings.setdefault("synthesis_submitted", time.monotonic())
                        resolver.add_text(segment, int(playback_progress()["returned_ms"]))
                        try:
                            await session.send_text(segment)
                            state.commit_segment(segment, receipt_index=before_segments)
                            if inference_boundary is not None:
                                inference_boundary.committed(handle)
                        finally:
                            state.sending = False
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
                    if sentence_window_enabled:
                        if wait_for_segment is None:
                            raise RuntimeError("TTS session 不支持短句块完成回执")
                        previous_boundary_ms = (
                            int(state.last_completed_boundary_bytes * 1000 / audio_bytes_per_second)
                            if state.last_completed_boundary_bytes and audio_bytes_per_second > 0
                            else None
                        )
                        receipt = await wait_for_segment(before_segments)
                        if not isinstance(receipt, TtsSegmentReceipt):
                            try:
                                receipt = TtsSegmentReceipt(
                                    index=int(receipt.index),
                                    text_chars=int(receipt.text_chars),
                                    end_received_bytes=int(receipt.end_received_bytes),
                                )
                            except (AttributeError, TypeError, ValueError) as exc:
                                raise RuntimeError("TTS session 返回了无效的短句块回执") from exc
                        state.complete_segment(receipt)
                        if completed_markers <= receipt.index:
                            completed_markers = receipt.index + 1
                            resolver.mark_boundary(int(receipt.end_received_bytes * 1000 / audio_bytes_per_second))
                        miss_boundary_ms = (
                            previous_boundary_ms
                            if previous_boundary_ms is not None
                            else prefetch_miss_boundary_ms
                        )
                        prefetch_miss_boundary_ms = None
                        # F2 自适应恢复门槛：每块回执后按实测事实决定是否抬高 resume，
                        # 不依赖"块边界被跨过"这一次采样（该警告只在特定边界打印）。
                        apply_adaptive_reserve()
                        if miss_boundary_ms is not None:
                            diagnostics = getattr(self.player, "diagnostics", {})
                            # 音频播放位置不含静音墙钟时间；不能用“新句已播放时长”冒充句间延迟。
                            miss_ms = int(diagnostics.get("underrun_ms", 0)) if isinstance(diagnostics, dict) else 0
                            if miss_ms > int(ws.timings.get("prefetch_miss_ms", 0)):
                                ws.timings["prefetch_miss_ms"] = max(
                                    int(ws.timings.get("prefetch_miss_ms", 0)), miss_ms
                                )
                                ws.timings["playback_underrun_ms"] = miss_ms
                                logger.warning(
                                    "controller.tts_prefetch_miss turn_id=%s miss_ms=%d block=%d",
                                    ws.turn_id, miss_ms, receipt.index,
                                )
                    elif segment_waits_enabled and wait_for_segment is not None:
                        await wait_for_segment(before_segments)
                    else:
                        await wait_for_provider_catch_up(before_bytes)
            except asyncio.CancelledError:
                raise
            except PassiveQuietSuppressed:
                raise
            except Exception as exc:
                tts_failed = exc
                tts_disabled = True
                state.close()
                progress_stop.set()
                if getattr(exc, "discard_audio", False):
                    reject_audio()
                    self.state.current["tts_status"] = "DEGRADED"
                    await self._detail("tts.status", provider=tts_backend, status="DEGRADED",
                                       waiting_text="", audio_buffer_ms=0, error=str(exc))
                logger.warning("controller.tts_degraded turn_id=%s error=%s", ws.turn_id, exc)

        async def send(segments):
            nonlocal sender_task
            if not segments or tts_client is None or tts_disabled or not self._valid(token):
                return
            if sender_task is None:
                sender_task = asyncio.create_task(tts_sender(), name=f"tts-sender-{ws.turn_id}")
            for segment in segments:
                if not segment or not segment.strip():
                    continue
                if tts_disabled or not self._valid(token):
                    return
                ws.timings.setdefault("first_clause_ready", time.monotonic())
                # Staging never blocks its producer.  Blocking here would
                # propagate the playback clock back into the LLM reader, and a
                # long answer would then stall until the provider deadline.
                state.push(segment)

        async def finish_send_queue() -> None:
            nonlocal producer_done
            producer_done = True
            state.producer_done = True
            state.signal.set()

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
                ws.timings.setdefault("first_llm_text", time.monotonic())
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
                    except PassiveQuietSuppressed:
                        raise
                    except Exception as exc:
                        tts_failed = tts_failed or exc
                        tts_disabled = True
                        logger.warning("controller.tts_receive_degraded turn_id=%s error=%s", ws.turn_id, exc)
            # ``task_flushed`` only confirms that the Bidi server has ended
            # this generation.  It is not evidence that the server produced
            # usable PCM.  Calling that a completed speech made the UI show
            # a fully answered request while the user heard nothing.  Record
            # the transport fact before final status/delivery resolution;
            # EpisodeSpeechOutput preserves the MiniMax-only empty-audio retry.
            ws.timings["tts_audio_bytes"] = audio_returned_bytes
            ws.timings["tts_text_sent"] = int(tts_text_sent)
            ws.timings["playback_output_complete"] = False
            if session and tts_text_sent and audio_returned_bytes == 0 and tts_failed is None:
                tts_failed = RuntimeError(f"{tts_backend} TTS 已确认结束但未返回 PCM")
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
                # F3 每轮降级信号：一行的 underrun/RTF/静音占比/实际 resume 与显卡快照，
                # 进程表和采样耗时用于后续对照，不把进程存在直接判作算力争用。
                await report_playback_degradation()
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
                # 结算态失败原因同样落盘：之前该明细只发往界面，服务端日志里没有痕迹。
                logger.warning(
                    "controller.tts_status turn_id=%s provider=%s status=DEGRADED "
                    "audio_buffer_ms=%s error_type=%s error=%s",
                    ws.turn_id, tts_backend,
                    self.player.buffered_ms if self.player else 0,
                    type(tts_failed).__name__, str(tts_failed)[:300],
                )
                await self._detail(
                    "tts.status",
                    provider=tts_backend,
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
            gpu_task = degradation["gpu_task"]
            if gpu_task is not None and not gpu_task.done():
                gpu_task.cancel()
                await asyncio.gather(gpu_task, return_exceptions=True)
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
