"""ControlledTtsSession（可滚动原生任务）：一个播报队列内保持累计音频时钟。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from .scheduling import TtsSegmentReceipt


_END = object()


class ControlledTtsSession:
    managed_controls = True  # managed_controls（自身负责参数冻结与连接重试）。

    def __init__(self, client, session, control_reader, factory):
        self.client = client
        self.provider_session = session
        self._control_reader = control_reader
        self._factory = factory
        self._values = session.controls
        self._audio = asyncio.Queue(maxsize=64)
        self._reader = None
        self._closed = False
        self._done = False
        self._error = None
        self._base_bytes = 0
        self._base_segments = 0
        self._base_sentences = 0
        self._base_text = 0
        self._submitted_chars = 0
        self._alignments = []
        self._alignment_cursor = 0
        self._receipts: list[TtsSegmentReceipt] = []
        self._send_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self.provider_session, name)

    @property
    def received_bytes(self):
        return self._base_bytes + self.provider_session.received_bytes

    @property
    def completed_segments(self):
        return self._base_segments + self.provider_session.completed_segments

    @property
    def completed_sentences(self):
        return self._base_sentences + self.provider_session.completed_sentences

    @property
    def generation_finished(self):
        return self._done

    @property
    def reusable(self):
        return bool(self._done and not self._closed and not self._error
                    and getattr(self.provider_session, "reusable", False))

    @staticmethod
    def _offset_text(value: Any, base: int):
        if isinstance(value, list):
            return [ControlledTtsSession._offset_text(item, base) for item in value]
        if isinstance(value, dict):
            return {key: item + base if key in {"text_start", "text_end", "char_start", "char_end"}
                    and type(item) is int else ControlledTtsSession._offset_text(item, base)
                    for key, item in value.items()}
        return value

    def _capture_alignment(self):
        events = tuple(self.provider_session.alignment_events)
        count = getattr(self.provider_session, "alignment_event_count", len(events))
        # 底层缓存可滚动淘汰旧快照；用累计序号读取，不能把固定缓存长度当作游标。
        offset = max(0, self._alignment_cursor - (count - len(events)))
        for event in events[offset:]:
            item = dict(event)
            item["audio_base_ms"] = int(item.get("audio_base_ms", 0)) + round(
                self._base_bytes * 1000 / (self.client.sample_rate * 2))
            if isinstance(item.get("audio_returned_ms"), (int, float)):
                item["audio_returned_ms"] += round(self._base_bytes * 1000 / (self.client.sample_rate * 2))
            item["sentence_index"] = int(item.get("sentence_index", 0)) + self._base_sentences
            if "value" in item:
                item["value"] = self._offset_text(item["value"], self._base_text)
            self._alignments.append(item)
        self._alignment_cursor = count

    @property
    def alignment_events(self):
        self._capture_alignment()
        return tuple(self._alignments)

    async def start(self):
        if self._reader is not None:
            return
        try:
            self.provider_session = await self.client.start_session_with_retry(self.provider_session)
        except Exception as exc:
            # 此连接尚未发送任何正文，才能安全降级原生情绪后重试。
            if not getattr(exc, "emotion_unsupported", False) or self._closed:
                raise
            await self.provider_session.close()
            self._values = self._control_reader()
            self.provider_session = self._factory(self._values, True)
            self.provider_session = await self.client.start_session_with_retry(self.provider_session)
        if self._closed:
            await self.provider_session.close()
            raise RuntimeError("受控合成队列已经取消。")
        self._reader = asyncio.create_task(self._read_current(), name="speech-controlled-audio")

    async def _read_current(self):
        try:
            async for pcm in self.provider_session.audio_chunks():
                if self._closed:
                    return
                self._capture_alignment()
                await self._audio.put(pcm)
            self._capture_alignment()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            await self._audio.put(exc)

    async def _finish_current(self):
        raw = self.provider_session
        if raw.bidi:
            await raw.flush()
        else:
            await raw.finish()
        if self._reader is not None:
            await self._reader
        if self._error:
            raise self._error
        self._capture_alignment()
        # 保存当前任务全部回执，后续任务的局部编号不能泄漏给播报调度器。
        while len(self._receipts) < self.completed_segments:
            local_index = len(self._receipts) - self._base_segments
            receipt = await raw.wait_for_segment(local_index)
            self._receipts.append(replace(receipt, index=len(self._receipts),
                                          end_received_bytes=self._base_bytes + receipt.end_received_bytes))

    async def send_text(self, text):
        async with self._send_lock:
            if self._closed or self._done:
                raise RuntimeError("受控合成队列已经结束。")
            values = self._control_reader()
            if values != self._values:
                await self._finish_current()
                if self._closed:
                    raise RuntimeError("受控合成队列已经取消。")
                raw = self.provider_session
                self._base_bytes += raw.received_bytes
                self._base_segments += raw.completed_segments
                self._base_sentences += max(raw.completed_sentences,
                                            int(getattr(raw, "_sentence_index", 0)))
                self._base_text = self._submitted_chars
                await raw.close()
                await self.client.prepare_speech_controls(values, detach_active=True)
                if self._closed:
                    raise RuntimeError("受控合成队列已经取消。")
                self.provider_session = self._factory(values, True)
                self._values = values
                self._alignment_cursor = 0
                self._reader = None
                await self.start()
            if self._closed:
                raise RuntimeError("受控合成队列已经取消。")
            await self.provider_session.send_text(text)
            self._submitted_chars += len(text)

    async def wait_for_segment(self, previous_count):
        if previous_count < len(self._receipts):
            return self._receipts[previous_count]
        raw = self.provider_session
        receipt = await raw.wait_for_segment(previous_count - self._base_segments)
        return replace(receipt, index=previous_count,
                       end_received_bytes=self._base_bytes + receipt.end_received_bytes)

    async def finish(self):
        if self._done or self._closed:
            return
        await self._finish_current()
        self._done = True
        await self._audio.put(_END)

    async def flush(self):
        await self.finish()

    async def audio_chunks(self):
        while not self._closed:
            item = await self._audio.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def cancel(self, **kwargs):
        self._closed = True
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        try:
            return await self.provider_session.cancel(**kwargs)
        finally:
            self._wake_closed_reader()

    def _wake_closed_reader(self):
        while not self._audio.empty():
            self._audio.get_nowait()
        self._audio.put_nowait(_END)

    async def close(self):
        self._closed = True
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self.provider_session.close()
        self._wake_closed_reader()
