"""音频增强：声学段精修、非唤醒情绪背景和独立声学信箱。"""
from __future__ import annotations

import asyncio
import logging

from ..audio_enhancements import AcousticMailbox, AudioEnhancements
from ..asr.protocol import AudioSegment

logger = logging.getLogger(__name__)


class AudioEnhancementsMixin:
    def _init_enhancements(self, settings: AudioEnhancements) -> None:
        self.audio_enhancements = settings
        self.perception = {"voice_emotion": None, "acoustic": AcousticMailbox(), "scope": 0, "speaker_scope": 0}
        self._enhancement_epoch = 0
        self._dual_epoch = 0  # dual_epoch（独立精修代次，不干扰情绪与声学背景）。
        self._refinement_tasks: set[asyncio.Task] = set()
        self._enhancement_streams: dict[str, dict] = {}
        self._enhancement_segment = 0
        self._enhancement_start: int | None = None
        self._perception_pcm = bytearray()
        self._emotion_pcm = bytearray()
        self._perception_origin = 0
        self._perception_end = 0
        self._perception_due = {"emotion": 0, "acoustic": 0}
        self._perception_pending: dict[str, tuple] = {}
        self._perception_tasks: dict[str, asyncio.Task] = {}
        self._acoustic_next: int | None = None

    def _register_enhancement_stream(self) -> None:
        if not hasattr(self, "_enhancement_streams"):
            self._init_enhancements(AudioEnhancements(False, False, False))
        retired = {id(row["workspace"]): row["workspace"] for row in self._enhancement_streams.values()
                   if row["workspace"] is not self.workspace}
        for ws in retired.values():
            self._retire_enhancement_streams(ws)
        self._enhancement_streams[self._sub_id] = {
            "workspace": self.workspace, "text": "", "segments": {},
            "sequence": self._sub_sequence, "epoch": self._dual_epoch,
            "session_scope": self.perception["scope"],
        }

    def _clear_emotion_background(self) -> None:
        self.perception["voice_emotion"] = None
        self.perception.pop("emotion_observation", None)
        self.perception["speaker_scope"] += 1
        self._emotion_pcm.clear()

    def _close_acoustic_segment(self, ws) -> None:
        """仅 VAD（语音活动检测）闭段调用；业务轮次结束不能调用。"""
        if not hasattr(self, "audio_enhancements"):
            self._init_enhancements(AudioEnhancements(False, False, False))
        start, self._enhancement_start = self._enhancement_start, None
        end = self._speech_end_sample
        if not self.audio_enhancements.dual_pass or start is None or end is None:
            return
        start = max(start, ws.audio.start_sample or 0)
        end = min(end, ws.audio.end_sample)
        if end <= start:
            return
        if self._sub_id not in self._enhancement_streams:
            self._register_enhancement_stream()
        self._enhancement_segment += 1
        segment_id = f"{ws.turn_id}/vad/{self._enhancement_segment}"
        offset = start - (ws.audio.start_sample or 0)
        pcm = bytes(ws.audio.pcm[offset * 2:(offset + end - start) * 2])
        self._enhancement_streams[self._sub_id]["segments"][segment_id] = {
            "start_sample": start, "end_sample": end, "revision": 1, "text": None,
        }
        hotwords = self.hotwords.native_targets(ws.token) if getattr(self, "hotwords", None) is not None else ()
        self._queue_asr(AudioSegment(self._sub_id, pcm, segment_id, start, end, hotwords=hotwords))

    async def _enhancement_result(self, message: dict) -> bool:
        kind = message.get("type")
        if kind not in {"asr.refined", "asr.refinement_failed"}:
            return False
        stream = getattr(self, "_enhancement_streams", {}).get(message.get("turn_id"))
        if (stream is None or stream["epoch"] != self._dual_epoch
                or stream.get("session_scope", self.perception["scope"]) != self.perception["scope"]):
            return True
        segment = stream["segments"].get(message.get("segment_id"))
        if segment is not None and message.get("revision", 1) != segment.get("revision", 1):
            return True
        if segment is None or kind == "asr.refinement_failed":
            if kind == "asr.refinement_failed":
                logger.warning("双路片段精修失败：%s", message.get("error"))
                if segment is not None:
                    segment["failed"] = True
                    if getattr(self, "workspace", None) is not stream["workspace"]:
                        self._retire_enhancement_streams(stream["workspace"])
            return True
        if (segment["start_sample"], segment["end_sample"]) != (
                message.get("start_sample"), message.get("end_sample")):
            return True
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            segment["failed"] = True
            if getattr(self, "workspace", None) is not stream["workspace"]:
                self._retire_enhancement_streams(stream["workspace"])
            return True
        if segment["text"] is not None:
            return True
        if message.get("parent_turn_id", stream["workspace"].turn_id) != stream["workspace"].turn_id:
            return True
        segment["text"] = text
        stream["workspace"].timings["paraformer_refine_ms"] = message.get("elapsed_ms")
        self._schedule_segment_refinements(stream["workspace"])
        return True

    def _schedule_segment_refinements(self, ws) -> None:
        """收包回调不等待整段标点；独立任务可继续接收主路及标点响应。"""
        ws._punctuation_pending = getattr(ws, "_punctuation_pending", 0) + 1
        async def publish():
            try:
                if not hasattr(ws, "_dual_publish_lock"):
                    ws._dual_publish_lock = asyncio.Lock()
                async with ws._dual_publish_lock:
                    await self._publish_segment_refinements(ws)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("精修快照发布失败：turn_id（轮次）=%s", ws.turn_id)
            finally:
                ws._punctuation_pending -= 1
                if getattr(self, "workspace", None) is not ws:
                    self._retire_enhancement_streams(ws)
        task = asyncio.create_task(publish())
        self._refinement_tasks.add(task)
        task.add_done_callback(self._refinement_tasks.discard)

    async def _publish_segment_refinements(self, ws) -> None:
        if getattr(ws, "_enhancement_invalidated", False):
            return
        streams = sorted((value for value in getattr(self, "_enhancement_streams", {}).values()
                          if value["workspace"] is ws and value["epoch"] == self._dual_epoch),
                         key=lambda value: value["sequence"])
        from ..asr.text import join_asr_segments
        rows = sorted((segment for stream in streams for segment in stream["segments"].values()
                       if segment["text"] is not None), key=lambda row: row["start_sample"])
        text = join_asr_segments([row["text"] for row in rows])
        available = bool(rows)
        if ws.speaker_mode == "filter":
            text, available = self._admitted_segment_text(ws, streams)
        final = bool(getattr(ws, "_refinement_input_finished", False))
        request_key = (text, final, self._dual_epoch)
        if available and text and request_key != getattr(ws, "_last_dual_request", None):
            epoch = self._dual_epoch
            result = {"text": text, "punctuation_status": "DEGRADED", "punctuation_error": "整段标点服务不可用"}
            punctuate = getattr(getattr(self, "asr", None), "punctuate_text", None)
            if callable(punctuate):
                try:
                    result = await punctuate(text, final=final)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result["punctuation_error"] = str(exc)
                    logger.exception("精修标点失败，保留正文")
            if epoch != self._dual_epoch or getattr(ws, "_enhancement_invalidated", False):
                return
            if final != bool(getattr(ws, "_refinement_input_finished", False)):
                self._schedule_segment_refinements(ws)
                return  # 续说已撤销结束状态；旧标点快照不能以结束稿发布。
            streams = [value for value in self._enhancement_streams.values()
                       if value["workspace"] is ws and value["epoch"] == self._dual_epoch]
            current_rows = sorted((segment for stream in streams for segment in stream["segments"].values()
                                   if segment["text"] is not None), key=lambda row: row["start_sample"])
            current_text = join_asr_segments([row["text"] for row in current_rows])
            if ws.speaker_mode == "filter":
                current_text, _ = self._admitted_segment_text(ws, streams)
            if current_text != text:
                self._schedule_segment_refinements(ws)
                return  # 等标点期间新增片段或准入改变，重算最新快照。
            ws._last_dual_request = request_key
            ws._last_dual_text = result["text"]
            ws.analysis["refinement_punctuation"] = {"status": result["punctuation_status"], "error": result.get("punctuation_error")}
            ws._dual_revision = getattr(ws, "_dual_revision", 0) + 1
            await self._refinement_completed(ws, {"text": result["text"], "source": "paraformer_offline",
                "source_revision": ws._dual_revision, **({"admission_checked": True} if ws.speaker_mode == "filter" else {})})
        if getattr(self, "workspace", None) is not ws and streams:
            self._retire_enhancement_streams(ws)

    def _admitted_segment_text(self, ws, streams):
        """准入归属于输入音频片段，续说的输出代次不能撤销已批准片段。"""
        from ..asr.text import join_asr_segments
        self._capture_refinement_admission(ws)
        pieces = getattr(ws, "_refinement_speaker_pieces", [])
        segments = sorted((row for stream in streams for row in stream["segments"].values()
                           if row["text"] is not None), key=lambda row: row["start_sample"])
        turn = getattr(self.speaker_policy, "turns", {}).get(ws.turn_id)
        if turn is not None:
            return (join_asr_segments([row["text"] for row in segments]), bool(segments)) if turn.admitted else ("", False)
        if ws.speaker_summary:
            return (join_asr_segments([row["text"] for row in segments]), bool(segments)) if ws.speaker_summary.get("admitted") else ("", False)
        admitted = []
        for segment in segments:
            overlapping = [piece for piece in pieces
                           if piece.start < segment["end_sample"] and piece.end > segment["start_sample"]]
            if not overlapping or any(piece.status != "admitted" for piece in overlapping):
                continue
            cursor = segment["start_sample"]
            for piece in sorted(overlapping, key=lambda value: value.start):
                if piece.start > cursor:
                    break
                cursor = max(cursor, piece.end)
            if cursor >= segment["end_sample"]:
                admitted.append(segment)
        return join_asr_segments([row["text"] for row in admitted]), bool(admitted)

    def _capture_refinement_admission(self, ws) -> None:
        if ws.speaker_mode != "filter":
            return
        previous = {id(piece): piece for piece in getattr(ws, "_refinement_speaker_pieces", [])}
        previous.update({id(piece): piece for piece in self.speaker_policy.pieces if piece.turn_id == ws.turn_id})
        ws._refinement_speaker_pieces = list(previous.values())

    async def _invalidate_dual_refinements(self) -> None:
        """只作废精修通道，不停止输入，不改变主路缓存和声学背景。"""
        self._dual_epoch += 1
        self._enhancement_start = None  # 不回放跨越开关边界的半段音频。
        self._enhancement_streams.clear()
        tasks = tuple(self._refinement_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _retire_enhancement_streams(self, ws) -> None:
        """已完成精修的退休输入释放音频工作区；未回包段仍保留原归属。"""
        self._enhancement_streams = {key: row for key, row in getattr(self, "_enhancement_streams", {}).items()
                                    if row["epoch"] == self._dual_epoch
                                    and row.get("session_scope", self.perception["scope"]) == self.perception["scope"]}
        streams = [row for row in getattr(self, "_enhancement_streams", {}).values() if row["workspace"] is ws]
        if getattr(ws, "_punctuation_pending", 0):
            return
        if any(segment["text"] is None and not segment.get("failed")
               for row in streams for segment in row["segments"].values()):
            return
        if ws.speaker_mode == "filter":
            self._capture_refinement_admission(ws)
            if any(piece.status == "pending" for piece in ws._refinement_speaker_pieces):
                return  # 身份待定片段仍可由同一会话稍后的声纹证据恢复；拒绝和过期片段释放。
        self._enhancement_streams = {key: row for key, row in getattr(self, "_enhancement_streams", {}).items()
                                    if row["workspace"] is not ws}

    def _perception_frame(self, frame, activity) -> None:
        """按采样时间投递最新窗口；写背景数据本身不唤醒推理。"""
        if not hasattr(self, "audio_enhancements"):
            self._init_enhancements(AudioEnhancements(False, False, False))
        if getattr(getattr(self, "episode", None), "closed", False):
            return
        if not (self.audio_enhancements.emotion or self.audio_enhancements.acoustic_events):
            return
        if activity.speech_started:
            self._clear_emotion_background()
        if self._perception_end != frame.start_sample:
            self._perception_pcm.clear()
            self._perception_origin = frame.start_sample
            self._acoustic_next = frame.start_sample
            self.perception["acoustic"].interrupt()
        self._perception_pcm.extend(frame.pcm)
        self._perception_end = frame.end_sample
        excess = max(0, len(self._perception_pcm) // 2 - 16000 * 8)
        if excess:
            del self._perception_pcm[:excess * 2]
            self._perception_origin += excess
        if activity.is_speech:
            self._emotion_pcm.extend(frame.pcm)
            del self._emotion_pcm[:-64000]
        if (self.audio_enhancements.emotion and activity.is_speech and len(self._emotion_pcm) >= 12800
                and frame.end_sample >= self._perception_due["emotion"]
                and (self.speaker_mode != "filter" or
                     (self.workspace is not None and self.speaker_policy.fully_admitted(self.workspace.turn_id)))):
            self._perception_due["emotion"] = frame.end_sample + 6400
            self._submit_perception("emotion", bytes(self._emotion_pcm), frame.end_sample - len(self._emotion_pcm)//2)
        if self.audio_enhancements.acoustic_events and frame.end_sample >= self._perception_due["acoustic"]:
            self._perception_due["acoustic"] = frame.end_sample + 16000
            start = self._acoustic_next if self._acoustic_next is not None else self._perception_origin
            if start < self._perception_origin:
                start += ((self._perception_origin - start + 7679) // 7680) * 7680
                self.perception["acoustic"].interrupt()
            if frame.end_sample - start >= 15600:
                self._submit_perception("acoustic", bytes(self._perception_pcm[(start-self._perception_origin)*2:]), start)

    def _submit_perception(self, operation: str, pcm: bytes, start: int) -> None:
        self._perception_pending[operation] = (pcm, start, self._enhancement_epoch,
            self.perception["scope"], self.perception["speaker_scope"])
        task = self._perception_tasks.get(operation)
        if task is None or task.done():
            self._perception_tasks[operation] = asyncio.create_task(self._run_perception(operation))

    async def _run_perception(self, operation: str) -> None:
        while operation in self._perception_pending:
            pcm, start, epoch, scope, speaker_scope = self._perception_pending.pop(operation)
            if operation == "acoustic" and self._acoustic_next is not None and self._acoustic_next > start:
                pcm = pcm[(self._acoustic_next - start)*2:]
                start = self._acoustic_next
                if len(pcm) < 31200:
                    continue
            try:
                result = await self.analysis.analyze(operation, pcm, turn_id="background",
                                                      generation_id=epoch, timeout_ms=10000)
                result = result.get("result", result)
                if epoch != self._enhancement_epoch or scope != self.perception["scope"]:
                    continue
                if operation == "emotion":
                    if speaker_scope != self.perception["speaker_scope"]:
                        continue
                    self.perception["voice_emotion"] = result.get("label") or None
                    self.perception["emotion_observation"] = {**result, "start_sample": start,
                                                              "end_sample": start + len(pcm)//2}
                else:
                    for row in result.get("frames", []):
                        end = start + int(row["end_sample"])
                        absolute = start + int(row["start_sample"])
                        self.perception["acoustic"].append(row["labels"], absolute, end, row.get("label_ids"))
                        self._acoustic_next = max(self._acoustic_next or absolute, absolute + 7680)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("音频背景通道 %s 推理失败", operation)

    async def _stop_enhancements(self, *, clear: bool = False) -> None:
        if not hasattr(self, "_enhancement_epoch"):
            self._init_enhancements(AudioEnhancements(False, False, False))
        self._enhancement_epoch += 1
        self.perception["scope"] += 1
        for row in self._enhancement_streams.values():
            row["workspace"]._enhancement_invalidated = True
        await self._invalidate_dual_refinements()
        tasks = tuple(self._perception_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._perception_tasks.clear()
        self._perception_pending.clear()
        self._perception_pcm.clear()
        self._emotion_pcm.clear()
        self._perception_end = 0
        self._perception_due = {"emotion": 0, "acoustic": 0}
        self._acoustic_next = None
        self._enhancement_start = None
        self.perception["voice_emotion"] = None
        self.perception.pop("emotion_observation", None)
        if clear:
            self.perception["acoustic"].clear()
        else:
            self.perception["acoustic"].interrupt()
