"""音色后台任务：模型、素材与操作身份隔离，不进入对话历史。"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from uuid import uuid4
from ..tts.voices import VoiceLibraryError, ResolvedLocalTtsVoice, safe_voice_diagnostic

DESIGN_SAMPLE_TEXT = "你好，很高兴认识你。现在开始测试这个音色，我会用自然的语气清楚地说话。"


def _collapsed_text(value: Any) -> str:
    """去掉转写首尾空白并压缩内部空白，与保存校验使用的规范化一致。"""
    return " ".join(str(value or "").split())


class TtsVoiceOperationError(ValueError):
    """音色后台操作无法执行。"""

@dataclass
class VoiceOperation:
    id: str  # id（独立操作标识）。
    kind: str  # kind（生成、试听或精修转写）。
    request: dict[str, Any]  # request（冻结输入参数及模型归属）。
    state: str = "running"  # state（任务状态）。
    task: Any = None  # task（可取消异步任务）。
    session: Any = None  # session（本次合成会话）。
    result: Any = None  # result（生成素材与候选参数）。
    error: str | None = None  # error（可展示错误）。
    completed_monotonic: float | None = None  # 完成时刻，用于回收过期操作记录。

    def payload(self):
        return {"operation_id": self.id, "kind": self.kind, "state": self.state,
                "model": self.request["model"], "result": self.result, "error": self.error}

class TtsVoiceOperationManager:
    """TtsVoiceOperationManager（音色任务管理）：同一运行时仅一个音色操作。"""
    def __init__(self, runtime):
        self.runtime = runtime
        self._items = {}
        self._active_id = None

    @property
    def busy(self):
        return self._active_id is not None

    def current_payload(self):
        value = self._items.get(self._active_id)
        return value.payload() if value else None

    def owns_capture(self, capture_id):
        return False

    def get(self, operation_id):
        self._prune()
        if operation_id not in self._items:
            raise KeyError("音色操作不存在或已过期。")
        return self._items[operation_id].payload()

    def _new(self, kind, request, runner):
        self._prune()
        if self.busy:
            raise TtsVoiceOperationError("另一个音色任务正在进行，请等待或取消。")
        self._require_library(request.get("model"))
        item = VoiceOperation(uuid4().hex, kind, dict(request))
        self._items[item.id] = item
        self._active_id = item.id
        item.task = asyncio.create_task(self._run(item, runner), name=f"tts-voice-{kind}-{item.id}")
        return item.payload()

    async def _run(self, item, runner):
        try:
            result = await runner(item)
            if item.state != "cancelled":
                item.result = result
                item.state = "succeeded"
        except asyncio.CancelledError:
            item.state = "cancelled"
        except Exception as exc:
            if item.state != "cancelled":
                item.state = "failed"
                item.error = safe_voice_diagnostic(exc)
        finally:
            item.completed_monotonic = time.monotonic()
            if self._active_id == item.id:
                self._active_id = None

    def _require_library(self, model):
        if self.runtime.voice_library is None:
            raise TtsVoiceOperationError("音色库不可用。")
        return self.runtime.voice_library.for_model(model)

    def _require_local_client(self, model):
        if self.runtime.config.tts.backend != "voxcpm1" or self.runtime.config.tts.local_model != model:
            raise TtsVoiceOperationError("请先切换到目标本地模型，再执行试音。")
        client = getattr(self.runtime.tts, "local", None)
        if client is None:
            raise TtsVoiceOperationError("本地模型尚未就绪。")
        return client

    def start_preview(self, model, voice, text=DESIGN_SAMPLE_TEXT):
        if not isinstance(voice, Mapping) or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise TtsVoiceOperationError("音色草稿或试音正文无效，正文应为 1–1000 字。")
        return self._new("preview", {"model": model, "voice": dict(voice), "text": text.strip()}, self._preview)

    def start_play(self, model, asset_id):
        self._require_library(model).asset_path(asset_id)
        return self._new("play", {"model": model, "asset_id": asset_id}, self._play)

    def start_transcribe(self, model, asset_id):
        self._require_library(model).asset_path(asset_id)
        return self._new("transcribe", {"model": model, "asset_id": asset_id}, self._transcribe)

    async def _transcribe(self, operation):
        import soundfile as sf
        library = self._require_library(operation.request["model"])
        samples, rate = await asyncio.to_thread(sf.read, library.asset_path(operation.request["asset_id"]), dtype="int16")
        if rate != 16000:
            raise TtsVoiceOperationError("参考音频必须为 16 kHz（千赫兹）。")
        from ..analysis.client import AnalysisClient
        client = AnalysisClient(self.runtime.config.smart_turn.analysis_url)
        health = await client.health()
        lane = health.get("refine", {})
        if lane.get("status") != "READY":
            raise TtsVoiceOperationError("当前精修模型未就绪，请稍后重试或手动填写。")
        try:
            reply = await client.analyze("refine", samples.astype("<i2").tobytes(), "voice-ref-" + operation.id, 0, 30000)
        except TimeoutError as exc:
            raise TtsVoiceOperationError("精修转写超过 30 秒，音频已保留，可重试或手填。") from exc
        except RuntimeError as exc:
            if "busy" in str(exc):
                raise TtsVoiceOperationError("当前精修模型忙，音频已保留，可稍后重试或手填。") from exc
            raise
        result = reply.get("result", {})
        if not result.get("complete") or not isinstance(result.get("text"), str) or not result["text"].strip():
            raise TtsVoiceOperationError("未识别到完整人声，音频已保留，请校对或重试。")
        return {"asset_id": operation.request["asset_id"], "text": result["text"].strip()}

    async def _preview(self, operation):
        model = operation.request["model"]
        library = self._require_library(model)
        client = self._require_local_client(model)
        await client.ensure_ready()
        voice = dict(operation.request["voice"])
        description = " ".join(str(voice.get("design_description") or "").split())
        source = voice.get("source_reference_asset_id") or (voice.get("reference_asset_id") if not description else None)
        # 参考音频转写决定克隆保真度：官方把"参考音频 + 精确转写"列为最高相似度配方，
        # 只给音频不给转写会退化为 isolated 参考通道。两种创建方式都要带上。
        source_text = _collapsed_text(voice.get("source_reference_text") or voice.get("reference_text", ""))
        if description:
            if model != "voxcpm2":
                raise TtsVoiceOperationError("VoxCPM1（第一代模型）不支持音色设计。")
            snapshot = library.resolve_design(description, is_preview=True)
            if source:
                snapshot = replace(snapshot, reference_path=library.asset_path(source),
                                   reference_mode="voice", reference_text=source_text)
        else:
            snapshot = library.resolve_draft(model, {"name": voice.get("name") or "新音色", "source": "import",
                "reference_asset_id": source, "reference_text": source_text})
        steps = client.inference_steps_by_model[model]  # 本次候选固定步数。
        pcm, high_fidelity = await self._collect_speech(operation, client, snapshot, operation.request["text"])
        asset = await asyncio.to_thread(library.put_pcm, pcm, sample_rate=16000)
        if high_fidelity is not None:
            native_pcm, native_rate = high_fidelity
            try:
                stored = await asyncio.to_thread(
                    library.put_high_fidelity_pcm, asset["asset_id"], native_pcm, sample_rate=native_rate)
                if stored:
                    asset["high_fidelity"] = stored
            except Exception as exc:  # noqa: BLE001 - 高保真副本失败不能作废候选样音。
                import logging
                logging.getLogger(__name__).warning(
                    "tts_voice.high_fidelity_store_failed asset=%s error_type=%s error=%s",
                    asset["asset_id"], type(exc).__name__, str(exc)[:200])
        payload = {"name": voice.get("name") or "新音色", "source": "design" if description else "import",
            "reference_asset_id": asset["asset_id"] if description else source,
            "reference_text": operation.request["text"] if description else snapshot.reference_text,
            "source_reference_asset_id": source if description else None,
            "source_reference_text": source_text,
            "design_description": description, "design_text": operation.request["text"],
            "design_model": "voxcpm2", "inference_steps": steps}
        if description:
            library.mark_design(asset["asset_id"], payload)
        operation.result = {**asset, "voice": payload}
        operation.request["asset_id"] = asset["asset_id"]
        await self._play(operation)
        return operation.result

    async def _collect_speech(self, operation: VoiceOperation, client, snapshot: ResolvedLocalTtsVoice,
                              text: str) -> tuple[bytes, tuple[bytes, int] | None]:
        """返回 (16 kHz 编码器素材, 原生率高保真副本或 None)。

        编码器固定读 16 kHz，所以主素材必须是 16 kHz；原生 48 kHz 样音另存
        一份，保留 8 kHz 以上带宽供后续音色处理。
        """
        session = client.session(voice_snapshot=snapshot, preview=True)
        operation.session = session
        output = bytearray()

        async def collect() -> None:
            async for pcm in session.audio_chunks():
                output.extend(pcm)
                if len(output) > int(getattr(client, "sample_rate", 16000)) * 2 * 17:
                    raise TtsVoiceOperationError("生成音频超过 15 秒，请缩短正文后重新生成。")

        reader = asyncio.create_task(collect(), name=f"tts-voice-reader-{operation.id[:8]}")
        try:
            await session.start()
            await session.send_text(text)
            await session.finish()
            await reader
            rate = int(getattr(client, "sample_rate", 16000))
            native = bytes(output)
            if rate != 16000:
                import numpy as np
                import soxr
                # 音色库主素材固定 16 kHz；播放时按当前设备时钟转换。
                samples = np.frombuffer(native, dtype="<i2")
                converted = soxr.resample(samples, rate, 16000, quality="HQ").astype("<i2", copy=False).tobytes()
                return converted, (native, rate)
            return native, None
        except BaseException:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await session.cancel()
            raise
        finally:
            # 候选和试听占用单个参考编码槽，不能沿用 formal voice 的预热标记。
            client._warmed = False
            try:
                await session.close()
            finally:
                operation.session = None

    async def _play(self, operation: VoiceOperation) -> dict[str, Any]:
        runtime = self.runtime
        library = self._require_library(operation.request["model"])
        player = getattr(runtime, "player", None)
        if player is None:
            raise TtsVoiceOperationError("程序播放器尚未启动。")
        asset_id = operation.request["asset_id"]
        path = library.asset_path(asset_id)
        try:
            import numpy as np
            import soundfile as sf
            from scipy.signal import resample_poly
            with path.open("rb") as handle:
                samples, rate = sf.read(handle, dtype="int16", always_2d=True)
        except Exception as exc:
            raise TtsVoiceOperationError("音频素材无法解码为 WAV。") from exc
        mono = samples.astype(np.int32).mean(axis=1).astype(np.int16)
        if rate != player.sample_rate:
            from fractions import Fraction
            ratio = Fraction(int(player.sample_rate), int(rate))
            converted = resample_poly(mono.astype(np.float32), ratio.numerator, ratio.denominator)
            mono = np.clip(np.rint(converted), -32768, 32767).astype(np.int16)
        pcm = mono.astype("<i2", copy=False).tobytes()
        if player.channels == 2:
            pcm = np.repeat(mono.reshape(-1, 1), 2, axis=1).astype("<i2", copy=False).tobytes()
        player.allow(operation.id)
        player.enqueue(operation.id, pcm)
        duration = len(pcm) / (player.sample_rate * player.channels * 2)
        try:
            await player.wait_until_output_complete(timeout_seconds=max(10.0, duration * 2 + 3.0))
            if not player.has_started(operation.id):
                raise TtsVoiceOperationError("音频未从当前输出设备播放，请检查静音策略或设备状态。")
        finally:
            player.stop_and_snapshot(operation.id)
        return {"asset_id": asset_id, "duration_ms": round(duration * 1000)}

    def cancel(self, operation_id):
        item = self._items.get(operation_id)
        if item is None:
            raise KeyError("音色任务不存在。")
        if item.state == "running":
            item.state = "cancelled"
            player = getattr(self.runtime, "player", None)
            if player:
                player.stop_and_snapshot(item.id)
            if item.task:
                item.task.cancel()
        return item.payload()

    async def close(self):
        item = self._items.get(self._active_id)
        if item:
            self.cancel(item.id)
            await asyncio.gather(item.task, return_exceptions=True)

    def _prune(self):
        now = time.monotonic()
        for key, item in list(self._items.items()):
            if item.completed_monotonic is not None and now - item.completed_monotonic > 900:
                del self._items[key]
