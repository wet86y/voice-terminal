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
            "sequence": self._sub_sequence, "epoch": self._enhancement_epoch,
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
        if end <= start or self._sub_id not in self._enhancement_streams:
            return
        self._enhancement_segment += 1
        segment_id = f"{ws.turn_id}/vad/{self._enhancement_segment}"
        offset = start - (ws.audio.start_sample or 0)
        pcm = bytes(ws.audio.pcm[offset * 2:(offset + end - start) * 2])
        self._enhancement_streams[self._sub_id]["segments"][segment_id] = {
            "start_sample": start, "end_sample": end, "text": None,
        }
        self._queue_asr(AudioSegment(self._sub_id, pcm, segment_id, start, end))

    async def _enhancement_result(self, message: dict) -> bool:
        kind = message.get("type")
        if kind not in {"asr.refined", "asr.refinement_failed"}:
            return False
        stream = getattr(self, "_enhancement_streams", {}).get(message.get("turn_id"))
        if (stream is None or stream["epoch"] != self._enhancement_epoch
                or stream.get("session_scope", self.perception["scope"]) != self.perception["scope"]):
            return True
        segment = stream["segments"].get(message.get("segment_id"))
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
        if not isinstance(text, str) or not text.strip() or segment["text"] is not None:
            return True
        first, last = message.get("start_char", 0), message.get("end_char", 0)
        if (type(first) is not int or type(last) is not int or not 0 <= first <= last <= len(stream["text"])
                or message.get("parent_turn_id", stream["workspace"].turn_id) != stream["workspace"].turn_id):
            return True
        segment.update(text=text, start_char=message.get("start_char", 0),
                       end_char=message.get("end_char", 0))
        stream["workspace"].timings["paraformer_refine_ms"] = message.get("elapsed_ms")
        await self._publish_segment_refinements(stream["workspace"])
        return True

    async def _publish_segment_refinements(self, ws) -> None:
        if getattr(ws, "_enhancement_invalidated", False):
            return
        streams = sorted((value for value in getattr(self, "_enhancement_streams", {}).values()
                          if value["workspace"] is ws and value["epoch"] == self._enhancement_epoch),
                         key=lambda value: value["sequence"])
        pieces, available = [], False
        for stream in streams:
            original = stream["text"]
            cursor = 0
            for segment in sorted(stream["segments"].values(), key=lambda row: row["start_sample"]):
                if segment["text"] is None:
                    continue
                start, end = int(segment["start_char"]), int(segment["end_char"])
                if not cursor <= start <= end <= len(original):
                    continue
                pieces.extend((original[cursor:start], segment["text"]))
                cursor, available = end, True
            pieces.append(original[cursor:])
        text = "".join(pieces)
        if ws.speaker_mode == "filter":
            text, available = self._admitted_segment_text(ws, streams)
        if available and text and text != getattr(ws, "_last_dual_text", None):
            ws._last_dual_text = text
            ws._dual_revision = getattr(ws, "_dual_revision", 0) + 1
            await self._refinement_completed(ws, {"text": text, "source": "paraformer_offline",
                "source_revision": ws._dual_revision, **({"admission_checked": True} if ws.speaker_mode == "filter" else {})})
        if getattr(self, "workspace", None) is not ws and streams:
            self._retire_enhancement_streams(ws)

    def _admitted_segment_text(self, ws, streams):
        """只替换身份已获准的原音频跨度，待定或拒绝的原稿也不能混入精修稿。"""
        pieces = [piece for piece in self.speaker_policy.pieces if piece.turn_id == ws.turn_id]
        segments = sorted((row for stream in streams for row in stream["segments"].values()
                           if row["text"] is not None), key=lambda row: row["start_sample"])
        admitted = []
        for segment in segments:
            overlapping = [piece for piece in pieces
                           if piece.start < segment["end_sample"] and piece.end > segment["start_sample"]]
            if overlapping and all(piece.status == "admitted" for piece in overlapping):
                admitted.append(segment)
        output, inserted = [], set()
        for piece in sorted(pieces, key=lambda value: (value.start, value.revision)):
            if piece.status != "admitted":
                continue
            overlapping = [index for index, segment in enumerate(admitted)
                           if piece.start < segment["end_sample"] and piece.end > segment["start_sample"]]
            if not overlapping:
                output.append(piece.text)
            else:
                for index in overlapping:
                    if index not in inserted:
                        output.append(admitted[index]["text"])
                        inserted.add(index)
        return "".join(output), bool(inserted)

    def _retire_enhancement_streams(self, ws) -> None:
        """已完成精修的退休输入释放音频工作区；未回包段仍保留原归属。"""
        self._enhancement_streams = {key: row for key, row in getattr(self, "_enhancement_streams", {}).items()
                                    if row["epoch"] == self._enhancement_epoch
                                    and row.get("session_scope", self.perception["scope"]) == self.perception["scope"]}
        streams = [row for row in getattr(self, "_enhancement_streams", {}).values() if row["workspace"] is ws]
        if any(segment["text"] is None and not segment.get("failed")
               for row in streams for segment in row["segments"].values()):
            return
        if ws.speaker_mode == "filter" and any(piece.status == "pending" for piece in self.speaker_policy.pieces
                                              if piece.turn_id == ws.turn_id):
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
        self._enhancement_streams.clear()
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
