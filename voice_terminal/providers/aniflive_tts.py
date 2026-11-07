"""AnifLive-TTS（本地语音合成服务）Speech Sessions（语音会话）适配器。"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
import ipaddress
from urllib.parse import urlsplit
from typing import Any

from ..gpu_performance import GpuPerformanceMode
from ..tts.capabilities import TtsCapabilities

logger = logging.getLogger(__name__)
_CURRENT_REFERENCE = object()  # 当前参考选择；与显式随包默认参考 None 区分。


class AnifLiveTtsError(RuntimeError):
    """AnifLive-TTS（本地语音合成服务）请求失败。"""


class AnifLiveTtsClient:
    """通过 HTTP（超文本传输协议）接入 AnifLive PCM（脉冲编码调制）会话。"""

    transport = "http_pcm"  # transport（传输方式）：HTTP（超文本传输协议）承载原始 PCM（脉冲编码调制）音频。
    supports_speech_controls = True  # supports_speech_controls（语音调节）：按文本段提交原生语速。
    freeze_session_at_admission = True  # freeze_session_at_admission（受理回答时固定参考及选项）。
    scheduling = "playback_window"  # scheduling（调度策略）：使用播放窗口限制文本提交量。
    effective_scheduling = "playback_window"  # effective_scheduling（生效调度策略）。
    playback_startup_buffer_ms = 0  # playback_startup_buffer_ms（起播缓冲毫秒）：首块音频直接进入播放器。
    playback_resume_buffer_ms = 0  # playback_resume_buffer_ms（欠载恢复缓冲毫秒）。
    capabilities = TtsCapabilities(acoustic_continuation=False, idle_prewarm=True, cancellation=True)  # capabilities（能力）：支持独立静默推理预热及会话取消。

    def __init__(
        self,
        *,
        server: Any | None = None,  # server（应用托管服务）。
        base_url: str = "http://127.0.0.1:9880",
        model_id: str = "minimax-female-shaonv-zh",
        voice_profile: str = "default",
        language: str = "zh",
        sample_rate: int = 32000,
        timeout_seconds: float = 120.0,
        sentence_min_effective_chars: int = 10,
        sentence_prefetch_ms: int = 3000,
        sentence_tick_ms: int = 50,
        aniflive_chunk_optimization: bool = True,  # aniflive_chunk_optimization（后续分块优化）。
        aniflive_first_audio_wait_ms: int = 0,  # 首音到达后等待毫秒。
        gpu_max_performance: bool = False,  # gpu_max_performance（显卡优先最大性能请求）。
        prewarm_timeout_seconds: float = 3.0,  # prewarm_timeout_seconds（静默预热总时限，秒）。
        prewarm_enabled: bool = True,  # prewarm_enabled（静默预热开关），由本地优化配置传入。
        voice_library: Any | None = None,  # voice_library（当前基底的参考音色库）。
    ) -> None:
        endpoint = urlsplit(base_url)
        try:
            local_host = ipaddress.ip_address(endpoint.hostname or "").is_loopback
            valid_port = endpoint.port is None or 1 <= endpoint.port <= 65535
        except ValueError:
            local_host = valid_port = False
        if (endpoint.scheme != "http" or not local_host or not valid_port or endpoint.username is not None
                or endpoint.password is not None or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
            raise ValueError("AnifLive（本地语音服务）地址必须为本机回环 IP（网络地址），且不能包含凭据或额外路径。")
        self.server = server
        self.local_model = model_id  # local_model（统一持久化模型编号）。
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.voice_profile = voice_profile
        self.language = language
        self.sample_rate = int(sample_rate)
        self.timeout_seconds = float(timeout_seconds)
        self.sentence_min_effective_chars = int(sentence_min_effective_chars)
        self.sentence_prefetch_ms = int(sentence_prefetch_ms)
        self.sentence_tick_ms = int(sentence_tick_ms)
        self._http: Any | None = None
        self._health: dict[str, Any] = {}
        self._ready = False
        self._created_at = time.monotonic()
        self._session_attempt_count = 0
        self._last_session_attempt_at: float | None = None
        self.gpu_performance = GpuPerformanceMode(enabled=gpu_max_performance)
        self.prewarm_timeout_seconds = float(prewarm_timeout_seconds)
        self.local_optimization = bool(prewarm_enabled)  # local_optimization（保留的预热偏好）。
        self.prewarm_enabled = self.local_optimization and not gpu_max_performance
        self.capabilities = TtsCapabilities(acoustic_continuation=False, idle_prewarm=self.prewarm_enabled, cancellation=True)
        self._prewarm_task: asyncio.Task | None = None
        self._prewarm_turn_id: str | None = None
        self._foreground_sessions: set[AnifLiveTtsSession] = set()  # foreground_sessions（占用服务的正式会话）。
        self._closing = False
        self._requested_gpu_max_performance = bool(gpu_max_performance)
        self.aniflive_chunk_optimization = aniflive_chunk_optimization
        self.aniflive_first_audio_wait_ms = aniflive_first_audio_wait_ms
        self.voice_library = voice_library
        self._reference_lock = asyncio.Lock()  # reference_lock（参考准备与会话创建的互斥锁）。

    async def prepare_reference(self, snapshot=None, *, use_selection=True, chunk_optimization=None) -> None:
        """prepare_reference（准备参考条件）：固定基底模型，不安装额外模型包。"""
        if self.voice_library is None:
            return
        if snapshot is None and use_selection:
            selection = self.voice_library.active_selection
            snapshot = self.voice_library.resolve("aniflive", selection) if selection else None
        token_path = self.voice_library.root / "reference-control.token"
        if not token_path.is_file():
            raise AnifLiveTtsError("应用托管的参考音色服务尚未就绪，请在组件设置中重试加载。")
        optimized = self.aniflive_chunk_optimization if chunk_optimization is None else chunk_optimization
        values = {"stream_chunk_tokens": 48 if optimized else 96, "model": self.model_id, "asset_id": snapshot.reference_path.stem if snapshot else None,
                  "reference_text": snapshot.reference_text if snapshot else "",
                  "reference_language": snapshot.reference_language if snapshot else "zh"}
        http = await self._client()
        response = await http.post(f"{self.base_url}/internal/application/reference", json=values,
                                   headers={"Authorization": "Bearer " + token_path.read_text(encoding="ascii").strip()})
        if response.status_code != 200:
            raise AnifLiveTtsError("参考音色准备失败，请确认本机参考音色服务已启动、没有其他语音会话，并检查录音与转写。")

    async def activate_model(self, model_id: str, revision=None) -> None:
        """activate_model（切换模型包）：经本机认证入口，确认服务实际加载的模型。"""
        if self.server is None:
            raise AnifLiveTtsError("本地模型切换需要应用托管服务。")
        await self.discard_prewarm()
        if self._foreground_sessions:
            raise AnifLiveTtsError("当前语音会话尚未结束。")
        await self.server.switch_model(model_id, revision)
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self.model_id = self.local_model = model_id
        self._ready = False
        await self.ensure_ready()

    async def switch_model(self, model_id: str, revision=None) -> None:
        """switch_model（统一模型切换入口）。"""
        previous, previous_revision = self.model_id, getattr(self.server, 'revision', None)
        try:
            await self.activate_model(model_id, revision)
            await self.prepare_reference()
        except BaseException:
            if self.server is not None and self.server.local_model != previous:
                await self.server.close()
                self.server.local_model, self.server.revision = previous, previous_revision
            self.model_id = self.local_model = previous
            if self._http is not None:
                await self._http.aclose()
                self._http = None
            await self.ensure_ready()
            await self.prepare_reference()
            raise

    @property
    def vad_prewarm(self) -> bool:
        """vad_prewarm（按人声事件刷新预热开关）：没有定时保鲜任务。"""
        return self.prewarm_enabled

    async def configure_optimization(self, *, gpu_max_performance: bool, local_optimization: bool, aniflive_chunk_optimization: bool = True, aniflive_first_audio_wait_ms: int = 0, **_options) -> None:
        """configure_optimization（运行时切换）：高性能与静默预热互斥，保留预热偏好。"""
        self.aniflive_chunk_optimization = aniflive_chunk_optimization
        self.aniflive_first_audio_wait_ms = aniflive_first_audio_wait_ms
        self.local_optimization = local_optimization
        self.prewarm_enabled = local_optimization and not gpu_max_performance
        self.capabilities = TtsCapabilities(acoustic_continuation=False, idle_prewarm=self.prewarm_enabled, cancellation=True)
        if not self.prewarm_enabled:
            await self.discard_prewarm()
        self._requested_gpu_max_performance = gpu_max_performance
        if not self._foreground_sessions:
            self._apply_performance(gpu_max_performance)

    def _apply_performance(self, enabled: bool) -> None:
        """应用回答快照的性能请求；保存新偏好不改变在途回答。"""
        if getattr(self.gpu_performance, 'enabled', None) == enabled:
            return
        self.gpu_performance.enabled = enabled
        if not enabled:
            self.gpu_performance.restore()
        elif self._ready:
            self.gpu_performance.apply()

    async def _client(self):
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - exercised only in a broken install.
                raise AnifLiveTtsError("缺少 httpx（异步 HTTP 客户端），无法连接 AnifLive-TTS。") from exc
            timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
            self._http = httpx.AsyncClient(timeout=timeout, trust_env=False,
                headers={"Authorization": "Bearer " + self.server.token} if self.server else None)
        return self._http

    async def ensure_ready(self) -> None:
        if self.server is not None:
            await self.server.ensure_ready()
            self.base_url = self.server.url
        client = await self._client()
        try:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            self._ready = False
            raise AnifLiveTtsError(f"AnifLive-TTS（本地语音服务）健康检查失败：{type(exc).__name__}: {str(exc)[:240]}") from exc
        if not isinstance(payload, dict) or payload.get("ready") is not True:
            self._ready = False
            detail = payload.get("status") if isinstance(payload, dict) else "响应格式无效"
            raise AnifLiveTtsError(f"AnifLive-TTS（本地语音服务）尚未就绪：{detail}")
        active_model = payload.get("model")
        active_voice = payload.get("voice")
        if active_model != self.model_id or active_voice != self.voice_profile:
            self._ready = False
            raise AnifLiveTtsError(
                "AnifLive-TTS 当前已激活的模型或音色与应用配置不一致："
                f"model={active_model!r}，voice_profile={active_voice!r}。"
            )
        self._health = payload
        self._ready = True
        self.gpu_performance.apply()

    def model_status(self) -> dict[str, Any]:
        if self.server is not None:
            return {**self.server.model_status(), "gpu_performance": self.gpu_performance.status(),
                    "prewarm_enabled": self.prewarm_enabled}
        return {
            "backend": self._health.get("backend"),  # backend（推理后端）。
            "model_id": self.model_id,  # model_id（模型包名称）。
            "voice_profile": self.voice_profile,  # voice_profile（音色配置）。
            "sample_rate": self.sample_rate,  # sample_rate（采样率，赫兹）。
            "ready": self._ready,  # ready（服务是否就绪）。
            "service_version": self._health.get("version"),  # service_version（服务版本）。
            "gpu_performance": self.gpu_performance.status(),  # gpu_performance（性能模式实际请求结果）。
            "prewarm_enabled": self.prewarm_enabled,  # prewarm_enabled（静默推理预热是否启用）。
        }

    async def prewarm(self, *, turn_id: str | None = None) -> dict[str, Any]:
        """prewarm（静默预热）：独立会话完整推理，不提交到播放器或对话历史。"""
        if not self.prewarm_enabled:
            return {"status": "DISABLED", "reason": "gpu_max_performance_enabled" if self._requested_gpu_max_performance else "local_optimization_disabled"}
        if self._closing or self._foreground_sessions:
            return {"status": "DISABLED", "reason": "formal_session_active"}
        task = self._prewarm_task
        if task is None or task.done():
            self._prewarm_turn_id = turn_id
            task = asyncio.create_task(self._run_prewarm(turn_id), name="aniflive-silent-prewarm")
            self._prewarm_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.discard_prewarm(turn_id=turn_id)
            raise
        finally:
            if self._prewarm_task is task and task.done():
                self._prewarm_task = None
                self._prewarm_turn_id = None

    async def _run_prewarm(self, turn_id: str | None) -> dict[str, Any]:
        started = time.monotonic()
        session = AnifLiveTtsSession(self, turn_id=turn_id, prewarm=True)
        reader: asyncio.Task | None = None
        try:
            async with asyncio.timeout(self.prewarm_timeout_seconds):
                await self.ensure_ready()
                await session.start()

                async def drain() -> None:
                    async for _ in session.audio_chunks():
                        pass

                reader = asyncio.create_task(drain(), name="aniflive-prewarm-drain")
                await session.send_text("你好，今天天气很好。")
                await session.finish()
                await reader
                response = await session._request("GET", f"/v1/sessions/{session.session_id}")
                snapshot = response.json()
                segments = snapshot.get("segments", [])
                if (not session.audio_bytes_received or snapshot.get("state") != "closed"
                        or len(segments) != 1 or segments[0].get("state") != "completed"
                        or segments[0].get("error")):
                    raise AnifLiveTtsError("静默预热没有成功完成独立文本段。")
            elapsed = (time.monotonic() - started) * 1000
            logger.info("aniflive_tts.prewarm_ready（静默推理预热完成） turn_id=%s elapsed_ms（耗时毫秒）=%.1f pcm_bytes（音频字节）=%d",
                        turn_id or "-", elapsed, session.audio_bytes_received)
            return {"status": "READY", "elapsed_ms": elapsed}  # elapsed_ms（完整预热耗时毫秒）。
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            await session.close()

    async def discard_prewarm(self, *, turn_id: str | None = None) -> None:
        """discard_prewarm（取消预热）：只取消自己的预热会话，不使用服务全局取消。"""
        task = self._prewarm_task
        if task is None or (turn_id is not None and self._prewarm_turn_id != turn_id):
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._prewarm_task is task:
            self._prewarm_task = None
            self._prewarm_turn_id = None

    def session(self, *, fresh: bool = False, turn_id: str | None = None, voice_snapshot=_CURRENT_REFERENCE, **_: Any) -> "AnifLiveTtsSession":
        del fresh
        session = AnifLiveTtsSession(self, turn_id=turn_id, voice_snapshot=voice_snapshot)
        return session

    async def close(self) -> None:
        self._closing = True
        try:
            await self.discard_prewarm()
            for session in tuple(self._foreground_sessions):
                await session.close()
            if self._http is not None:
                await self._http.aclose()
                self._http = None
        finally:
            self.gpu_performance.restore()
            if self.server is not None:
                await self.server.close()
            self._ready = False
            self._closing = False


class AnifLiveTtsSession:
    """一笔文本输入与对应 PCM（脉冲编码调制）流。"""

    bidi = False
    reusable = False

    def __init__(self, client: AnifLiveTtsClient, *, turn_id: str | None = None, prewarm: bool = False, voice_snapshot=_CURRENT_REFERENCE) -> None:
        self.client = client
        self.chunk_optimization = client.aniflive_chunk_optimization  # 回答固定的语义分块策略。
        self.first_audio_wait_ms = client.aniflive_first_audio_wait_ms  # 回答固定的首音等待。
        self.gpu_max_performance = client._requested_gpu_max_performance  # 回答固定的性能请求。
        self.turn_id = turn_id
        self.session_id: str | None = None
        self._stream_context: Any | None = None
        self._response: Any | None = None
        self._started = False
        self._closed = False
        self._finished = False
        self._consumer_started = False
        self._segment_index = 0
        self._started_at = 0.0
        self._session_attempt_index = 0
        self._client_age_ms = 0.0
        self._since_previous_session_attempt_ms: float | None = None
        self._session_create_ms: float | None = None
        self._audio_stream_open_ms: float | None = None
        self._first_segment_submit_at: float | None = None
        self._first_segment_ack_at: float | None = None
        self._first_segment_post_ms: float | None = None
        self._first_segment_chars = 0
        self._first_pcm_received_at: float | None = None
        self._first_pcm_perf_at: float | None = None  # Windows 高分辨率到达时钟，用于逐毫秒等待。
        self._first_pcm_bytes = 0
        self._first_pcm_timing_logged = False
        self.audio_chunks_received = 0
        self.audio_bytes_received = 0
        self.server_finished = False
        self._prewarm = prewarm  # prewarm（独立静默预热会话标记）。
        self._release_voice = None  # release_voice（释放在途素材引用）。
        library = client.voice_library
        if library is not None:
            with library.snapshot_guard():
                if voice_snapshot is _CURRENT_REFERENCE:
                    selection = library.active_selection
                    voice_snapshot = library.resolve('aniflive', selection) if selection else None
                if voice_snapshot is not None:
                    self._release_voice = library.pin(voice_snapshot)
        self.voice_snapshot = None if voice_snapshot is _CURRENT_REFERENCE else voice_snapshot
        self._receiver: asyncio.Task | None = None
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=3)  # audio_queue（有界网络接收队列）：最多三块，每块不超过64KiB。
        if not prewarm:
            client._foreground_sessions.add(self)  # 包括尚未联网的已受理回答；客户端关闭时一并释放参考租约。

    @property
    def first_audio_ready_at(self) -> float | None:
        """first_audio_ready_at（网络实际首块到达的单调时刻）。"""
        return self._first_pcm_received_at

    @property
    def first_audio_perf_at(self) -> float | None:
        """first_audio_perf_at（高分辨率网络到达时刻），不与公共事件时钟混用。"""
        return self._first_pcm_perf_at

    @property
    def completed_segments(self) -> int:
        """completed_segments（完成文本段数）：此服务不发送逐段完成回执。"""
        return getattr(self, "_completed_segments", 0)

    async def _request(self, method: str, path: str, **kwargs: Any):
        http = await self.client._client()
        try:
            response = await http.request(method, f"{self.client.base_url}{path}", **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            body = ""
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    body = response.text[:240]
                except Exception:
                    body = ""
            raise AnifLiveTtsError(
                f"AnifLive-TTS 请求失败：{method} {path}：{type(exc).__name__} "
                f"{body or str(exc)[:240]}"
            ) from exc

    async def start(self) -> None:
        if self._started:
            return
        if self._closed or self.client._closing:
            raise AnifLiveTtsError("会话或客户端正在关闭。")
        if not self._prewarm:
            self.client._foreground_sessions.add(self)
            try:
                await self.client.discard_prewarm()
                await self._start_with_reference()
            except BaseException:
                await self.close()
                raise
        else:
            await self._start_with_reference()

    async def _start_with_reference(self):
        self.client._apply_performance(self.gpu_max_performance)
        await self.client.ensure_ready()
        async with self.client._reference_lock:
            library = self.client.voice_library
            if library is not None:
                await self.client.prepare_reference(self.voice_snapshot, use_selection=False, chunk_optimization=self.chunk_optimization)
            await self._start_stream()

    async def _start_stream(self) -> None:
        start_at = time.monotonic()
        previous_attempt_at = self.client._last_session_attempt_at
        self.client._session_attempt_count += 1
        self._session_attempt_index = self.client._session_attempt_count
        self._client_age_ms = (start_at - self.client._created_at) * 1000.0
        self._since_previous_session_attempt_ms = (
            (start_at - previous_attempt_at) * 1000.0 if previous_attempt_at is not None else None
        )
        self.client._last_session_attempt_at = start_at
        create_at = start_at
        response = await self._request(
            "POST",
            "/v1/sessions",
            json={
                "model": self.client.model_id,  # model（模型包名称）。
                "voice_profile": self.client.voice_profile,  # voice_profile（音色配置）。
                "continuity_policy": "A",  # continuity_policy（分段上下文延续策略）。
            },
        )
        self._session_create_ms = (time.monotonic() - create_at) * 1000.0
        payload = response.json()
        session_id = payload.get("id", payload.get("session_id")) if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise AnifLiveTtsError("AnifLive-TTS 会话响应缺少 session_id（会话标识）。")
        self.session_id = session_id
        http = await self.client._client()
        self._stream_context = http.stream("GET", f"{self.client.base_url}/v1/sessions/{session_id}/audio")
        audio_open_at = time.monotonic()
        try:
            self._response = await self._stream_context.__aenter__()
            self._response.raise_for_status()
        except Exception as exc:
            await self._stream_context.__aexit__(type(exc), exc, exc.__traceback__)
            self._stream_context = None
            await self._request("POST", f"/v1/sessions/{session_id}/cancel")
            raise AnifLiveTtsError(f"AnifLive-TTS PCM 音频流连接失败：{type(exc).__name__}: {str(exc)[:240]}") from exc
        self._audio_stream_open_ms = (time.monotonic() - audio_open_at) * 1000.0
        response_rate = self._response.headers.get("X-TTS-Sample-Rate")
        if response_rate and response_rate.isdigit() and int(response_rate) != self.client.sample_rate:
            await self.close()
            raise AnifLiveTtsError(
                f"AnifLive-TTS 采样率为 {response_rate} Hz，与播放器配置 {self.client.sample_rate} Hz 不匹配。"
            )
        self._started_at = time.monotonic()
        self._started = True
        self._receiver = asyncio.create_task(self._receive_audio(), name=f"aniflive-receive-{session_id}")
        since_previous_session_attempt = (
            "none"
            if self._since_previous_session_attempt_ms is None
            else f"{self._since_previous_session_attempt_ms:.1f}"
        )
        logger.info(
            "aniflive_tts.session_ready（TTS 会话就绪） turn_id=%s session_id=%s model_id=%s "
            "service_version=%s backend=%s sample_rate_hz=%d session_attempt_index=%d "
            "client_age_ms=%.1f since_previous_session_attempt_ms=%s idle_prewarm_enabled=%s "
            "session_create_ms=%.1f "
            "audio_stream_open_ms=%.1f session_total_ready_ms=%.1f",
            self.turn_id or "-",
            self.session_id,
            self.client.model_id,
            self.client._health.get("version") or "unknown",
            self.client._health.get("backend") or "unknown",
            self.client.sample_rate,
            self._session_attempt_index,
            self._client_age_ms,
            since_previous_session_attempt,
            self.client.capabilities.idle_prewarm,
            self._session_create_ms,
            self._audio_stream_open_ms,
            (self._started_at - start_at) * 1000.0,
        )

    def _log_first_pcm_timing(self) -> None:
        """Record client-observed TTS milestones without logging speech content."""
        if (
            self._first_pcm_timing_logged
            or self._first_pcm_received_at is None
            or self._first_segment_submit_at is None
            or self._first_segment_ack_at is None
        ):
            return
        self._first_pcm_timing_logged = True
        session_ready_to_first_pcm_ms = (
            (self._first_pcm_received_at - self._started_at) * 1000.0
            if self._started_at
            else 0.0
        )
        logger.info(
            "aniflive_tts.first_pcm_timing（首个 PCM 音频块时延） turn_id=%s session_id=%s "
            "segment_index=1 segment_chars=%d segment_post_ms=%.1f "
            "segment_submit_to_first_pcm_ms=%.1f segment_ack_to_first_pcm_ms=%.1f "
            "session_ready_to_first_pcm_ms=%.1f first_pcm_bytes=%d",
            self.turn_id or "-",
            self.session_id or "-",
            self._first_segment_chars,
            self._first_segment_post_ms or 0.0,
            (self._first_pcm_received_at - self._first_segment_submit_at) * 1000.0,
            (self._first_pcm_received_at - self._first_segment_ack_at) * 1000.0,
            session_ready_to_first_pcm_ms,
            self._first_pcm_bytes,
        )

    async def send_text(self, text: str) -> None:
        if not self._started or self._finished or self.session_id is None:
            raise AnifLiveTtsError("AnifLive-TTS 会话未开启或已结束，不能继续追加文本。")
        content = str(text).strip()
        if not content:
            return
        self._segment_index += 1
        submit_at = time.monotonic()
        if self._segment_index == 1:
            self._first_segment_submit_at = submit_at
            self._first_segment_chars = len(content)
        await self._request(
            "POST",
            f"/v1/sessions/{self.session_id}/segments",
            json={
                "segment_id": f"app-{uuid.uuid4().hex}",  # segment_id（唯一文本段标识）。
                "text": content,  # text（待合成文本）。
                "language": self.client.language,  # language（文本语言）。
                "generation": {"speed": getattr(self.speech_control_reader(), "speed", 1.0)}
                    if callable(getattr(self, "speech_control_reader", None)) else {},  # generation（逐段生成参数）。
            },
        )
        ack_at = time.monotonic()
        if self._segment_index == 1:
            self._first_segment_ack_at = ack_at
            self._first_segment_post_ms = (ack_at - submit_at) * 1000.0
        logger.info(
            "aniflive_tts.segment_added segment=%d chars=%d language=%s",
            self._segment_index, len(content), self.client.language,
        )
        logger.info(
            "aniflive_tts.segment_timing（文本段提交时延） turn_id=%s session_id=%s "
            "segment_index=%d segment_chars=%d segment_post_ms=%.1f "
            "segment_submit_since_ready_ms=%.1f",
            self.turn_id or "-",
            self.session_id,
            self._segment_index,
            len(content),
            (ack_at - submit_at) * 1000.0,
            (ack_at - self._started_at) * 1000.0,
        )
        if self._segment_index == 1:
            self._log_first_pcm_timing()

    async def finish(self) -> None:
        if self._finished or self.session_id is None:
            return
        await self._request("POST", f"/v1/sessions/{self.session_id}/flush")
        self._finished = True

    async def cancel(self, *, timeout_seconds: float | None = None) -> bool:
        if self.session_id is None or self._closed:
            return False
        self._closed = True
        try:
            async with asyncio.timeout(timeout_seconds if timeout_seconds is not None else 2.0):
                await self._request("POST", f"/v1/sessions/{self.session_id}/cancel")
        finally:
            try:
                await self._close_audio_stream()
            finally:
                self.client._foreground_sessions.discard(self)
                if not self.client._foreground_sessions and not self.client._closing:
                    self.client._apply_performance(self.client._requested_gpu_max_performance)
                if self._release_voice is not None:
                    self._release_voice()
                    self._release_voice = None
        return True

    async def _receive_audio(self) -> None:
        """独立接收网络音频；播放挂起不会污染首块网络到达计时。"""
        pending = b""
        try:
            async for raw in self._response.aiter_raw():
                if raw and self._first_pcm_received_at is None:
                    self._first_pcm_perf_at = time.perf_counter()
                    self._first_pcm_received_at = time.monotonic()
                for offset in range(0, len(raw), 64 * 1024):
                    data = pending + raw[offset:offset + 64 * 1024]
                    aligned = len(data) & ~1
                    pending = data[aligned:]
                    if not aligned:
                        continue
                    chunk = data[:aligned]
                    self.audio_chunks_received += 1
                    self.audio_bytes_received += len(chunk)
                    if not self._first_pcm_bytes:
                        self._first_pcm_bytes = len(chunk)
                        self._log_first_pcm_timing()
                    await self._audio_queue.put(chunk)
            if pending:
                raise AnifLiveTtsError("AnifLive-TTS 流结束时出现不完整的 16 位 PCM 采样。")
            response = await self._request("GET", f"/v1/sessions/{self.session_id}")
            final = response.json()
            segments = final.get("segments", [])
            if (final.get("state") != "closed" or len(segments) != self._segment_index
                    or not segments or any(row.get("state") != "completed" or row.get("error") for row in segments)):
                raise AnifLiveTtsError("AnifLive 会话或文本段未成功完成，不能按完整播报结算。")
            self._completed_segments = len(segments)
            self.server_finished = True
            await self._audio_queue.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, AnifLiveTtsError) else AnifLiveTtsError(
                f"AnifLive-TTS PCM 音频流中断：{type(exc).__name__}: {str(exc)[:240]}")
            await self._audio_queue.put(error)

    async def audio_chunks(self):
        if not self._started or self._response is None:
            raise AnifLiveTtsError("必须先开启 AnifLive-TTS 音频会话，再读取 PCM 流。")
        if self._consumer_started:
            raise AnifLiveTtsError("AnifLive-TTS 音频流只允许一个读取者。")
        self._consumer_started = True
        consumed = False
        while True:
            chunk = await self._audio_queue.get()
            if chunk is None:
                return
            if isinstance(chunk, BaseException):
                raise chunk
            if not consumed:
                consumed = True
                deadline = self._first_pcm_perf_at + self.first_audio_wait_ms / 1000
                while (remaining := deadline - time.perf_counter()) > 0:
                    await asyncio.sleep(remaining)
                logger.info("aniflive_tts.first_pcm_consumed（播放器管线取走首块） turn_id=%s session_id=%s "
                            "received_to_consumer_ms（网络到达至取走毫秒）=%.1f",
                            self.turn_id or "-", self.session_id,
                            (time.monotonic() - self._first_pcm_received_at) * 1000)
            yield chunk

    async def _close_audio_stream(self) -> None:
        if self._receiver is not None:
            receiver, self._receiver = self._receiver, None
            if not receiver.done():
                receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        if self._stream_context is not None:
            context, self._stream_context = self._stream_context, None
            try:
                await context.__aexit__(None, None, None)
            except Exception:
                pass
        self._response = None
        while not self._audio_queue.empty():
            self._audio_queue.get_nowait()
        self._audio_queue.put_nowait(None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.session_id is not None and not self.server_finished:
                try:
                    async with asyncio.timeout(2.0):
                        await self._request("POST", f"/v1/sessions/{self.session_id}/cancel")
                except (AnifLiveTtsError, TimeoutError):
                    logger.debug("aniflive_tts.session_cancel_on_close_failed")
        finally:
            try:
                await self._close_audio_stream()
            finally:
                self.client._foreground_sessions.discard(self)
                if self._release_voice is not None:
                    self._release_voice()
                    self._release_voice = None
                if not self.client._foreground_sessions and not self.client._closing:
                    self.client._apply_performance(self.client._requested_gpu_max_performance)
