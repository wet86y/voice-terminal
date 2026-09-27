"""Voice Server owned CosyVoice process and provider session.

The child knows only synthesis requests.  Speech ownership, scheduling and
playback remain in the Voice Server controller.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
from pathlib import Path
from typing import AsyncIterator

import httpx
import websockets

from .scheduling import TtsSegmentReceipt


class CosyVoiceRuntimeError(RuntimeError):
    pass


class CosyVoiceWorkerManager:
    def __init__(self, *, project_root: Path, model_dir: Path) -> None:
        self.project_root = project_root
        self.model_dir = model_dir
        self.runtime_dir = project_root / ".runtime" / "cosyvoice"
        self.python = self.runtime_dir / "env" / "Scripts" / "python.exe"
        self.source = self.runtime_dir / "source"
        self.prompt_wav = self.runtime_dir / "asset" / "zero_shot_prompt.wav"
        self.process: asyncio.subprocess.Process | None = None
        self.port: int | None = None
        self.token: str | None = None
        self.state = "stopped"
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._log_handle = None

    def status(self) -> dict[str, object]:
        if self.process is not None and self.process.returncode is not None and self.state in {"ready", "draining", "starting"}:
            self.state = "error"
            self.last_error = "CosyVoice Worker 已退出。"
        return {"state": self.state, "message": self.last_error, "model_present": self.model_dir.is_dir()}

    @staticmethod
    def _available_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self.state in {"ready", "draining"} and await self._healthy():
                return
            await self._stop_locked()
            if not self.python.is_file():
                self.state = "error"
                self.last_error = "缺少项目内 CosyVoice Python 环境；请运行 scripts/setup-cosyvoice.ps1。"
                raise CosyVoiceRuntimeError(self.last_error)
            if not self.source.is_dir() or not self.prompt_wav.is_file() or not (self.model_dir / "cosyvoice3.yaml").is_file():
                self.state = "error"
                self.last_error = "CosyVoice 上游代码、参考音频或模型文件缺失；请运行安装脚本。"
                raise CosyVoiceRuntimeError(self.last_error)
            self.state = "starting"
            self.last_error = None
            self.port = self._available_port()
            self.token = secrets.token_urlsafe(32)
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self._log_handle = (self.runtime_dir / "worker.log").open("ab")
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join((str(self.source), str(self.source / "third_party" / "Matcha-TTS"), str(self.project_root)))
            env["VOICE_COSY_MODEL_DIR"] = str(self.model_dir)
            env["VOICE_COSY_PROMPT_WAV"] = str(self.prompt_wav)
            env["VOICE_COSY_TOKEN"] = self.token
            env["VOICE_COSY_PORT"] = str(self.port)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    str(self.python), "-m", "voice_terminal.tts.cosyvoice_worker",
                    cwd=str(self.project_root), env=env,
                    stdout=self._log_handle, stderr=self._log_handle,
                )
                loop = asyncio.get_running_loop()
                # Windows 上加载权重和缓存零样本提示音常超过一分钟，给冷启动留余量。
                deadline = loop.time() + 180.0
                while loop.time() < deadline:
                    if self.process.returncode is not None:
                        raise CosyVoiceRuntimeError("CosyVoice Worker 启动时退出；请检查 .runtime/cosyvoice/worker.log。")
                    if await self._healthy():
                        self.state = "ready"
                        return
                    await asyncio.sleep(0.5)
                raise CosyVoiceRuntimeError("CosyVoice Worker 健康检查超时。")
            except Exception as exc:
                self.last_error = str(exc)[:300]
                await self._stop_locked()
                self.state = "error"
                raise CosyVoiceRuntimeError(self.last_error) from exc

    async def _healthy(self) -> bool:
        if self.process is None or self.process.returncode is not None or self.port is None or self.token is None:
            return False
        try:
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
                response = await client.get(
                    f"http://127.0.0.1:{self.port}/health", headers={"X-Worker-Token": self.token},
                )
            return response.status_code == 200 and response.json().get("ready") is True
        except (httpx.HTTPError, ValueError):
            return False

    @property
    def websocket_url(self) -> str:
        # A transaction bound before switching back to MiniMax may open its
        # session while the Worker is draining.  The lease keeps it alive.
        if self.state not in {"ready", "draining"} or self.port is None or self.token is None:
            raise CosyVoiceRuntimeError("CosyVoice Worker 尚未就绪。")
        return f"ws://127.0.0.1:{self.port}/tts?token={self.token}"

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()
            self.state = "stopped"
            self.last_error = None

    async def _stop_locked(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.port = None
        self.token = None


class CosyVoiceTtsSession:
    bidi = True
    reusable = True

    def __init__(self, client: "CosyVoiceTtsClient", *, emotion: str) -> None:
        self.client = client
        self.emotion = emotion
        self._socket = None
        self._reader: asyncio.Task | None = None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        self._receipts: list[TtsSegmentReceipt] = []
        self._receipt_event = asyncio.Event()
        self._error: Exception | None = None
        self._send_lock = asyncio.Lock()
        self.received_bytes = 0
        self.completed_segments = 0
        self._closed = False
        self._finish_sent = False
        self.generation_finished = False

    async def start(self) -> None:
        if self._socket is not None:
            return
        await self.client.manager.ensure_ready()
        self._socket = await websockets.connect(self.client.manager.websocket_url, max_size=8 * 1024 * 1024)
        self._reader = asyncio.create_task(self._receive(), name="cosyvoice-receive")

    async def _receive(self) -> None:
        try:
            assert self._socket is not None
            async for raw in self._socket:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "audio":
                    pcm = base64.b64decode(message["pcm"])
                    self.received_bytes += len(pcm)
                    await self._audio.put(pcm)
                elif kind == "done":
                    receipt = TtsSegmentReceipt(
                        index=int(message["index"]), text_chars=int(message["text_chars"]),
                        end_received_bytes=int(message["end_received_bytes"]),
                    )
                    if receipt.index != self.completed_segments or receipt.end_received_bytes != self.received_bytes:
                        raise CosyVoiceRuntimeError("CosyVoice 文本块回执与 PCM 边界不一致。")
                    self._receipts.append(receipt)
                    self.completed_segments += 1
                    self._receipt_event.set()
                elif kind == "error":
                    raise CosyVoiceRuntimeError(str(message.get("message") or "CosyVoice 合成失败。")[:300])
                elif kind == "finished":
                    self.generation_finished = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._error = exc
                self.client.manager.state = "error"
                self.client.manager.last_error = str(exc)[:300]
        finally:
            self._receipt_event.set()
            if self._closed:
                if self._audio.full():
                    self._audio.get_nowait()
                self._audio.put_nowait(None)
            else:
                await self._audio.put(None)

    async def send_text(self, text: str) -> None:
        if not text or self._closed or self._socket is None:
            raise CosyVoiceRuntimeError("CosyVoice 会话未启动或文本块为空。")
        async with self._send_lock:
            await self._socket.send(json.dumps({"type": "text", "text": text, "emotion": self.emotion}, ensure_ascii=False))

    async def wait_for_segment(self, previous_index: int) -> TtsSegmentReceipt:
        while self.completed_segments <= previous_index:
            if self._error is not None:
                raise self._error
            if self._reader is not None and self._reader.done():
                raise CosyVoiceRuntimeError("CosyVoice 未确认文本块完成。")
            self._receipt_event.clear()
            await self._receipt_event.wait()
        return self._receipts[previous_index]

    async def audio_chunks(self) -> AsyncIterator[bytes]:
        while True:
            pcm = await self._audio.get()
            if pcm is None:
                if self._error is not None:
                    raise self._error
                return
            yield pcm

    async def finish(self) -> None:
        if self._socket is not None and not self._closed and not self._finish_sent:
            async with self._send_lock:
                await self._socket.send(json.dumps({"type": "finish"}))
                self._finish_sent = True

    async def flush(self) -> None:
        """结束当前双向流文本输入，并等待 Worker 刷出最后音频块。"""
        await self.finish()

    async def cancel(self, *, timeout_seconds: float | None = None) -> bool:
        if self._socket is not None and not self._closed:
            try:
                async with self._send_lock:
                    await self._socket.send(json.dumps({"type": "cancel"}))
                await asyncio.wait_for(self._reader, timeout_seconds or 2.0)
            except (asyncio.TimeoutError, Exception):
                await self.client.manager.stop()
            finally:
                await self.close()
        return True

    async def close(self) -> None:
        self._closed = True
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)


class CosyVoiceTtsClient:
    transport = "websocket_bidi"
    effective_scheduling = "direct"
    scheduling = "direct"
    emotion_transport = "instruction"
    emotion_status = "applied"
    sentence_prefetch_ms = 2000
    sentence_tick_ms = 50
    first_clause_timeout_ms = 400

    def __init__(self, manager: CosyVoiceWorkerManager) -> None:
        self.manager = manager

    async def prepare_emotion(self, emotion: str | None, **_kwargs) -> str:
        allowed = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
        return emotion if isinstance(emotion, str) and emotion in allowed else "calm"

    def session(self, *, emotion: str = "calm", **_kwargs) -> CosyVoiceTtsSession:
        return CosyVoiceTtsSession(self, emotion=emotion)

    async def close(self) -> None:
        # Session objects own their sockets; the Voice Server owns the Worker.
        return
