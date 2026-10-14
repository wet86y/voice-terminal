from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import asr_result, decode_audio_chunk
from ..compute import process_memory_bytes, hardware_status
from ..resources import model_thread_budget, process_resource_status, registry, model_load_footprint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AsrInferenceResult:
    """累计转写以及本次是否输出新增文字或结束标点。"""

    text: str
    emitted: bool
    latency: dict[str, float | int] | None = None  # latency（本次识别分段耗时，毫秒）。
    segments: list[dict] | None = None # 新增识别正文及来源采样范围。


class AsrWorkerEngine:
    def __init__(
        self,
        *,
        model_dir: Path,
        device: str,
        sample_rate: int = 16000,
        chunk_size: tuple[int, int, int] = (0, 8, 4),  # chunk_size（分块配置）：480 毫秒输入、240 毫秒前瞻。
        encoder_chunk_look_back: int = 4,
        decoder_chunk_look_back: int = 1,
        fallback_device: str = "cpu",
        punctuation_model_dir: Path | None = None,  # punctuation_model_dir（实时标点目录；空值关闭）。
        punctuation_device: str = "cpu",  # punctuation_device（标点推理设备）。
    ) -> None:
        self.model_dir = model_dir
        # device（请求设备）：auto/cpu/cuda；self.device（实际设备）由加载结果决定。
        self.requested_device = device
        self.device = "cpu"
        self.fallback_device = fallback_device
        self.load_error: str | None = None
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.encoder_chunk_look_back = encoder_chunk_look_back
        self.decoder_chunk_look_back = decoder_chunk_look_back
        self._model: Any = None
        self.punctuation = None  # punctuation（与识别共用专用线程的实时标点通道）。
        if punctuation_model_dir is not None:
            from .punctuation import StreamingPunctuation
            self.punctuation = StreamingPunctuation(punctuation_model_dir, punctuation_device)
        self._np: Any = None
        self._cache_by_turn: dict[str, dict[str, Any]] = {}
        # paraformer-zh-streaming emits text for the current streaming
        # window, not a complete transcript for the whole turn.  Keep the
        # cumulative transcript separately so later windows cannot replace
        # earlier words with only the last emitted characters.
        self._transcript_by_turn: dict[str, str] = {}
        self._pending_audio_by_turn: dict[str, bytearray] = {}
        self._chunk_count_by_turn: dict[str, int] = {}
        self._byte_count_by_turn: dict[str, int] = {}
        self._model_call_count_by_turn: dict[str, int] = {}
        self._last_log_at_by_turn: dict[str, float] = {}
        # executor（专用执行线程）：模型加载、预热和推理使用同一条原生通道。
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-inference")
        self._inference_lock = asyncio.Lock()  # inference_lock（识别通道锁）：保护共享模型与轮次状态。
        self._load_lock = threading.RLock()
        self._timing = threading.local()  # timing（线程内计时）：不把线程排队算进模型调用。
        self._call_latency: dict[str, float | int] = {}
        self._span_positions: dict[str, int] = {}
        self._span_revisions: dict[str, int] = {}
        self._current_spans: list[dict] = []
        self.model_warmed = False  # model_warmed（已完成真实推理预热）。
        self.startup_timings_ms = {}  # startup_timings_ms（各启动阶段耗时，毫秒）。
        registry.observe(self, "asr_pending_pcm", lambda value: sum(len(p) for p in value._pending_audio_by_turn.values()))

    @property
    def chunk_stride_samples(self) -> int:
        """chunk_stride_samples（模型输入窗口采样数）。

        Paraformer（流式识别模型）在 16 千赫兹时使用 chunk_size[1] * 960
        作为输入步长；默认 [0, 8, 4] 对应 7,680 个采样点，即 480 毫秒。
        """

        middle_chunks = self.chunk_size[1] if len(self.chunk_size) > 1 else 8
        return max(1, int(self.sample_rate * middle_chunks * 60 / 1000))

    @property
    def model_loaded(self) -> bool:
        """Whether the real recognizer has completed loading."""

        return self._model is not None

    @property
    def chunk_stride_bytes(self) -> int:
        # The Worker accepts mono signed 16-bit PCM.
        return self.chunk_stride_samples * 2

    def load(self) -> None:
        with self._load_lock:
            self._load_model()
            if self.punctuation is not None:
                if not getattr(self.punctuation, "_load_attempted", False):
                    with model_load_footprint() as footprint:
                        self.punctuation.load()
                    self.punctuation.resource_footprint = footprint

    def _load_model(self) -> None:
        if self._model is not None:
            return
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"Paraformer（流式识别）模型目录不存在：{self.model_dir}")
        started = time.perf_counter()
        try:
            import numpy as np  # type: ignore
            from .model_loader import auto_model_class
            AutoModel = auto_model_class(self.model_dir)
        except ImportError as exc:
            raise RuntimeError("缺少 NumPy（数值计算库）或 FunASR（语音框架），请先运行 scripts\\setup.ps1（环境安装脚本）。") from exc
        self.startup_timings_ms["imports"] = (time.perf_counter() - started) * 1000  # imports（依赖导入）。
        self._np = np
        fallback = self.fallback_device if self.fallback_device in {"cpu", "cuda"} else "cpu"
        requested = self.requested_device
        from ..compute import startup_device, loaded_funasr_device
        started = time.perf_counter()
        initial = startup_device(requested, "paraformer")
        self.startup_timings_ms["hardware"] = (time.perf_counter() - started) * 1000  # hardware（设备探测）。
        candidates = [initial]
        if initial == "cuda":
            if fallback != initial:
                candidates.append(fallback)
            if "cpu" not in candidates:
                candidates.append("cpu")
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                started = time.perf_counter()
                self._model = AutoModel(
                    model=str(self.model_dir),
                    device=candidate,
                    disable_update=True,
                    disable_pbar=True,
                    ncpu=model_thread_budget(),
                )
                self.startup_timings_ms["model"] = (time.perf_counter() - started) * 1000  # model（模型构造与权重加载）。
                self.device = loaded_funasr_device(self._model, candidate)
                # 首次原生算子／CUDA（图形处理器加速）初始化在开放接口前完成；
                # 实际推理失败也进入设备回退，不能只凭权重加载宣称 GPU（图形处理器）可用。
                self._warm_up_model()
                self.load_error = (
                    f"CUDA（图形处理器）加载失败，已回退到 {self.device}：{type(last_error).__name__}: {last_error}"
                    if last_error is not None else
                    "FunASR（语音模型加载器）未使用请求的 CUDA（图形处理器），实际运行于 CPU（中央处理器）。"
                    if candidate == "cuda" and self.device == "cpu" else None
                )
                logger.info("asr.worker.model_ready model_dir=%s device=%s", self.model_dir, self.device)
                logger.info("识别启动阶段（毫秒）：依赖导入=%.1f，设备探测=%.1f，模型加载=%.1f，真实预热=%.1f",
                    self.startup_timings_ms["imports"], self.startup_timings_ms["hardware"],
                    self.startup_timings_ms["model"], self.startup_timings_ms["warmup"])
                return
            except Exception as exc:
                last_error = exc
                self._model = None
                self.model_warmed = False
                if candidate == "cuda":
                    try:
                        import torch  # type: ignore
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
        self._model = None
        self.load_error = str(last_error) if last_error else "模型加载失败"
        raise RuntimeError(self.load_error) from last_error

    def _warm_up_model(self) -> None:
        """用独立的静音缓存预热完整窗和空最终刷新，不进入用户转写状态。"""
        started = time.perf_counter()
        cache: dict[str, Any] = {}
        for final in (False, False, True):
            audio = self._np.zeros(0 if final else self.chunk_stride_samples, dtype=self._np.float32)
            self._model.generate(
                input=audio, cache=cache, is_final=final, chunk_size=list(self.chunk_size),
                encoder_chunk_look_back=self.encoder_chunk_look_back,
                decoder_chunk_look_back=self.decoder_chunk_look_back,
            )
        self.model_warmed = True
        self.startup_timings_ms["warmup"] = (time.perf_counter() - started) * 1000  # warmup（真实推理预热）。
        logger.info("asr.worker.warmed device=%s elapsed_ms=%.1f", self.device, (time.perf_counter() - started) * 1000)

    async def prepare(self) -> None:
        """prepare（准备）：在识别专用线程完成加载和预热。"""
        future = asyncio.get_running_loop().run_in_executor(self._executor, self.load)
        await self._await_native(future)

    def close(self) -> None:
        """close（关闭）：工作进程结束时回收专用通道。"""
        self._executor.shutdown(wait=True, cancel_futures=True)
        if self.punctuation is not None:
            self.punctuation.close()

    @staticmethod
    async def _await_native(future: asyncio.Future) -> Any:
        # shield（取消屏障）：缓存和租约只能在原生调用完成之后清理。
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if future.done() and not future.cancelled():
                future.exception()
            raise

    def _infer_sync(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        # 租约在原生线程内持有；异步请求取消不释放正在读取的输入。
        with registry.lease("asr_native_pcm", pcm, release_on="native_complete"):
            return self._infer_owned_sync(turn_id, pcm, is_final)

    def _infer_owned_sync(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        self.load()
        if len(pcm) % 2:
            pcm = pcm[:-1]
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        cache = self._cache_by_turn.setdefault(turn_id, {})
        started = time.perf_counter_ns()
        result = self._model.generate(
            input=audio,
            cache=cache,
            is_final=is_final,
            chunk_size=list(self.chunk_size),
            encoder_chunk_look_back=self.encoder_chunk_look_back,
            decoder_chunk_look_back=self.decoder_chunk_look_back,
        )
        self._timing.model_ms = (time.perf_counter_ns() - started) / 1_000_000
        text = ""
        if isinstance(result, list) and result and isinstance(result[0], dict):
            value = result[0].get("text")
            if isinstance(value, str):
                text = value
        # Do not strip each window before concatenating.  A leading space can
        # be meaningful at an English word boundary; trim only when the
        # cumulative transcript is published.
        return text

    def _clear_turn(self, turn_id: str) -> None:
        if self.punctuation is not None:
            self.punctuation.clear_turn(turn_id)
        self._cache_by_turn.pop(turn_id, None)
        self._transcript_by_turn.pop(turn_id, None)
        self._pending_audio_by_turn.pop(turn_id, None)
        self._chunk_count_by_turn.pop(turn_id, None)
        self._byte_count_by_turn.pop(turn_id, None)
        self._model_call_count_by_turn.pop(turn_id, None)
        self._last_log_at_by_turn.pop(turn_id, None)
        self._span_positions.pop(turn_id, None)
        self._span_revisions.pop(turn_id, None)

    async def _infer_window(self, turn_id: str, pcm: bytes, is_final: bool) -> str:
        """在同一专用线程执行识别窗口与实时标点，返回可追加的文本增量。"""

        self._model_call_count_by_turn[turn_id] = self._model_call_count_by_turn.get(turn_id, 0) + 1
        submitted = time.perf_counter_ns()

        def run() -> tuple[str, float, float, float, float]:
            started = time.perf_counter_ns()
            self._timing.model_ms = 0.0
            text = self._infer_sync(turn_id, pcm, is_final)
            model_ms = self._timing.model_ms
            punctuation_ms = 0.0  # punctuation_ms（本窗标点处理耗时，独立于识别推理）。
            if self.punctuation is not None:
                punc_started = time.perf_counter_ns()
                text = self.punctuation.process(turn_id, text, final=is_final)
                punctuation_ms = (time.perf_counter_ns() - punc_started) / 1_000_000
            return (text, (started - submitted) / 1_000_000,
                    (time.perf_counter_ns() - started) / 1_000_000, model_ms, punctuation_ms)

        future = asyncio.get_running_loop().run_in_executor(self._executor, run)
        text, executor_wait_ms, native_ms, model_ms, punctuation_ms = await self._await_native(future)
        start_sample = self._span_positions.get(turn_id, 0)
        end_sample = start_sample + len(pcm) // 2
        self._span_positions[turn_id] = end_sample
        if text:
            revision = self._span_revisions.get(turn_id, 0) + 1
            self._span_revisions[turn_id] = revision
            self._current_spans.append({"revision": revision, "text_delta": text,
                "audio_start_sample": start_sample if end_sample > start_sample else max(0, end_sample-self.chunk_stride_samples),
                "audio_end_sample": end_sample})

        for key, value in (("executor_wait_ms", executor_wait_ms), ("native_ms", native_ms),
                           ("model_ms", model_ms), ("punctuation_ms", punctuation_ms)):
            self._call_latency[key] = self._call_latency.get(key, 0.0) + value
        self._call_latency["model_calls"] = self._call_latency.get("model_calls", 0) + 1
        logger.info(
            "asr.worker.window turn_id=%s final=%s audio_ms=%.1f executor_wait_ms=%.1f native_ms=%.1f model_ms=%.1f",
            turn_id, is_final, len(pcm) / (self.sample_rate * 2) * 1000,
            executor_wait_ms, native_ms, model_ms,
        )
        return text

    async def infer(self, turn_id: str, pcm: bytes, is_final: bool) -> AsrInferenceResult:
        submitted = time.perf_counter_ns()
        async with self._inference_lock:
            self._call_latency = {"lane_wait_ms": (time.perf_counter_ns() - submitted) / 1_000_000,
                                  "executor_wait_ms": 0.0, "native_ms": 0.0, "model_ms": 0.0,
                                  "punctuation_ms": 0.0, "model_calls": 0}
            try:
                return await self._infer_serial(turn_id, pcm, is_final)
            except BaseException:
                self._clear_turn(turn_id)
                raise

    async def _infer_serial(self, turn_id: str, pcm: bytes, is_final: bool) -> AsrInferenceResult:
        started = time.perf_counter()
        self._current_spans = []
        self._chunk_count_by_turn[turn_id] = self._chunk_count_by_turn.get(turn_id, 0) + 1
        self._byte_count_by_turn[turn_id] = self._byte_count_by_turn.get(turn_id, 0) + len(pcm)

        pending = self._pending_audio_by_turn.setdefault(turn_id, bytearray())
        pending.extend(pcm)
        emitted_parts: list[str] = []

        if is_final:
            # Normally the final call contains only the remaining partial
            # window.  If the transport ever batches more than one window,
            # flush complete windows first and mark only the last call final.
            while len(pending) > self.chunk_stride_bytes:
                window = bytes(pending[: self.chunk_stride_bytes])
                del pending[: self.chunk_stride_bytes]
                emitted_parts.append(await self._infer_window(turn_id, window, False))
            # is_final（最终刷新）：整窗音频已消费时，仍用空 PCM（脉冲编码音频）
            # 刷新 Paraformer（流式识别模型）的缓存尾字；从未推理的空轮次不调用模型。
            if pending or self._model_call_count_by_turn.get(turn_id, 0):
                window = bytes(pending)
                pending.clear()
                emitted_parts.append(await self._infer_window(turn_id, window, True))
        else:
            # Microphone callbacks arrive every 20 ms, while FunASR's
            # paraformer streaming API expects a much larger model window
            # chunk_size（分块配置）默认 [0, 8, 4]，对应 480 毫秒输入窗口。
            while len(pending) >= self.chunk_stride_bytes:
                window = bytes(pending[: self.chunk_stride_bytes])
                del pending[: self.chunk_stride_bytes]
                emitted_parts.append(await self._infer_window(turn_id, window, False))

        emitted_text = "".join(emitted_parts)
        if emitted_text:
            self._transcript_by_turn[turn_id] = self._transcript_by_turn.get(turn_id, "") + emitted_text
        text = self._transcript_by_turn.get(turn_id, "").strip()
        elapsed_ms = (time.perf_counter() - started) * 1000
        now = time.monotonic()
        last_log_at = self._last_log_at_by_turn.get(turn_id, 0.0)
        if is_final or emitted_text or not last_log_at or now - last_log_at >= 1.0:
            logger.info(
                "asr.worker.infer turn_id=%s chunk_count=%d bytes=%d final=%s pending_bytes=%d model_calls=%d elapsed_ms=%.1f emitted_len=%d transcript_len=%d text=%r",
                turn_id,
                self._chunk_count_by_turn[turn_id],
                self._byte_count_by_turn[turn_id],
                is_final,
                len(pending),
                self._model_call_count_by_turn.get(turn_id, 0),
                elapsed_ms,
                len(emitted_text),
                len(text),
                text[:120],
            )
            self._last_log_at_by_turn[turn_id] = now
        else:
            logger.debug(
                "asr.worker.infer_chunk turn_id=%s bytes=%d final=%s pending_bytes=%d elapsed_ms=%.1f transcript_len=%d",
                turn_id,
                len(pcm),
                is_final,
                len(pending),
                elapsed_ms,
                len(text),
            )
        if is_final:
            # Return the final cumulative text before releasing per-turn
            # model cache and aggregation state.
            self._clear_turn(turn_id)
        return AsrInferenceResult(text=text, emitted=bool(emitted_text), segments=list(self._current_spans), latency=dict(self._call_latency))


async def handle_asr_connection(websocket: Any, engine: AsrWorkerEngine) -> None:
    owned_turns: set[str] = set()
    peer = getattr(websocket, "remote_address", None)
    logger.info("asr.worker.connection_open peer=%s", peer)
    try:
        async for raw in websocket:
            if isinstance(raw, bytes):
                logger.warning("asr.worker.binary_message_ignored bytes=%d", len(raw))
                continue
            message: dict[str, Any] = {}
            try:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("消息必须是 JSON 对象")
                message_type = message.get("type")
                if message_type == "health":
                    request_id = message.get("request_id")
                    if request_id is not None and not isinstance(request_id, str):
                        raise ValueError("health request_id 必须是字符串")
                    try:
                        engine.load()
                        response = {
                            "type": "health",
                            "status": "READY",
                            "model_loaded": engine.model_loaded,
                            "model_warmed": getattr(engine, "model_warmed", None),  # model_warmed（真实推理预热完成）。
                            "runtime": "PyTorch（推理框架）" if engine.model_loaded else None,  # runtime（实际加载引擎）。
                            "device": getattr(engine, "device", None),
                            "requested_device": getattr(engine, "requested_device", None),
                            "load_error": getattr(engine, "load_error", None),
                            "punctuation": engine.punctuation.status() if getattr(engine, "punctuation", None) is not None else {"status": "DISABLED"},  # punctuation（标点通道健康状态）。
                            "process_memory_rss_bytes": process_memory_bytes(),
                            "resource_status": process_resource_status(),  # resource_status（资源状态）。
                        }
                    except Exception as exc:
                        # Keep the connection available long enough for the
                        # caller to receive a classified readiness failure.
                        response = {
                            "type": "health",
                            "status": "ERROR",
                            "model_loaded": False,
                            "error": str(exc),
                            "device": getattr(engine, "device", None),
                            "requested_device": getattr(engine, "requested_device", None),
                            "process_memory_rss_bytes": process_memory_bytes(),
                        }
                    if response.get("status") == "READY" and os.environ.get("VOICE_RESOURCE_ROLE") == "asr":
                        response["hardware_capabilities"] = hardware_status()  # hardware_capabilities（重型后端能力）。
                    if isinstance(request_id, str):
                        response["request_id"] = request_id
                    await websocket.send(json.dumps(response, ensure_ascii=False))
                    logger.info(
                        "asr.worker.health_reply request_id=%s status=%s model_loaded=%s",
                        request_id,
                        response["status"],
                        response["model_loaded"],
                    )
                    continue
                if message_type == "turn.finish":
                    turn_id = message.get("turn_id")
                    if isinstance(turn_id, str):
                        engine._clear_turn(turn_id)
                        owned_turns.discard(turn_id)
                        logger.info("asr.worker.turn_finish turn_id=%s", turn_id)
                    continue
                if message_type != "audio.chunk":
                    raise ValueError(f"不支持的 ASR 消息类型：{message_type}")
                chunk = decode_audio_chunk(message)
                received_at = time.monotonic_ns()
                queue_wait_ms = None  # queue_wait_ms（本机传输及此前音频积压耗时，毫秒）。
                if chunk.sent_at_monotonic_ns is not None:
                    delta_ns = received_at - chunk.sent_at_monotonic_ns
                    # 仅本机工作进程共享单调时钟；跨主机或无效时间不参与统计。
                    if 0 <= delta_ns <= 600 * 1_000_000_000:
                        queue_wait_ms = delta_ns / 1_000_000
                owned_turns.add(chunk.turn_id)
                logger.debug(
                    "asr.worker.audio_chunk turn_id=%s bytes=%d final=%s",
                    chunk.turn_id,
                    len(chunk.pcm),
                    chunk.is_final,
                )
                inference = await engine.infer(chunk.turn_id, chunk.pcm, chunk.is_final)
                worker_service_ms = (time.monotonic_ns() - received_at) / 1_000_000
                latency = None
                if inference.latency is not None:
                    latency = {**inference.latency, "queue_wait_ms": queue_wait_ms,
                               "worker_service_ms": worker_service_ms}  # worker_service_ms（取包后的识别处理耗时）。
                    if chunk.is_final or inference.latency.get("model_calls", 0):
                        logger.info("asr.worker.latency turn_id=%s final=%s latency=%s",
                                    chunk.turn_id, chunk.is_final, json.dumps(latency, ensure_ascii=False))
                if chunk.is_final:
                    owned_turns.discard(chunk.turn_id)
                # A final message is a protocol acknowledgement even when the
                # recognizer has no text.  Otherwise the Voice Server waits
                # until its timeout and cannot decide whether to continue.
                if inference.emitted or chunk.is_final:
                    result_type = "asr.final" if chunk.is_final else "asr.partial"
                    response = asr_result(result_type, chunk.turn_id, inference.text)
                    if inference.segments:
                        response["segments"] = inference.segments
                    if latency is not None:
                        response["latency"] = latency  # latency（分段耗时）：旧接收端可以忽略。
                    await websocket.send(
                        json.dumps(response, ensure_ascii=False)
                    )
                    logger.info(
                        "asr.worker.result turn_id=%s type=%s text_len=%d text=%r",
                        chunk.turn_id,
                        result_type,
                        len(inference.text),
                        inference.text[:120],
                    )
            except Exception as exc:
                message_type = message.get("type") if isinstance(message, dict) else None
                turn_id = message.get("turn_id") if isinstance(message, dict) else None
                logger.exception("asr.worker.message_failed turn_id=%s", turn_id)
                code = "ASR_PROTOCOL_ERROR" if not isinstance(message, dict) or message_type in {"health", "audio.chunk", "turn.finish"} else "ASR_INFERENCE_FAILED"
                error_payload = {
                    "type": "error",
                    "code": code,
                    "message": str(exc),
                    "turn_id": turn_id if isinstance(turn_id, str) else None,
                }
                if isinstance(message, dict) and isinstance(message.get("request_id"), str):
                    error_payload["request_id"] = message["request_id"]
                await websocket.send(json.dumps(error_payload, ensure_ascii=False))
    finally:
        for turn_id in owned_turns:
            engine._clear_turn(turn_id)
        logger.info("asr.worker.connection_closed peer=%s", peer)
