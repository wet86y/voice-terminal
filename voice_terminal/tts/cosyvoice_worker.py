"""Isolated CosyVoice synthesis Worker; no conversation or playback state."""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect


PROMPT_TEXT = "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。"
EMOTION_INSTRUCTIONS = {
    "happy": "You are a helpful assistant. 请用开心的语气朗读。<|endofprompt|>",
    "sad": "You are a helpful assistant. 请用悲伤的语气朗读。<|endofprompt|>",
    "angry": "You are a helpful assistant. 请用愤怒的语气朗读。<|endofprompt|>",
    "fearful": "You are a helpful assistant. 请用恐惧的语气朗读。<|endofprompt|>",
    "disgusted": "You are a helpful assistant. 请用厌恶的语气朗读。<|endofprompt|>",
    "surprised": "You are a helpful assistant. 请用惊讶的语气朗读。<|endofprompt|>",
}

app = FastAPI()
model = None
prompt_wav: Path | None = None
model_lock = threading.Lock()


def resample_24k_to_32k(pcm24: bytes, state: object) -> tuple[bytes, object]:
    """有状态重采样，避免流式 PCM 块边界重新起算。"""
    return audioop.ratecv(pcm24, 2, 1, 24000, 32000, state)


@app.get("/health")
async def health(x_worker_token: str | None = Header(default=None)) -> dict[str, bool]:
    if x_worker_token != os.environ.get("VOICE_COSY_TOKEN"):
        raise HTTPException(status_code=403, detail="forbidden")
    return {"ready": model is not None and prompt_wav is not None}


@app.websocket("/tts")
async def synthesize(websocket: WebSocket) -> None:
    if websocket.query_params.get("token") != os.environ.get("VOICE_COSY_TOKEN"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    requests: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=32)
    outgoing: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
    cancelled = threading.Event()
    loop = asyncio.get_running_loop()

    def put(message: dict) -> None:
        future = asyncio.run_coroutine_threadsafe(outgoing.put(message), loop)
        while True:
            try:
                future.result(timeout=0.1)
                return
            except FutureTimeoutError:
                if cancelled.is_set():
                    future.cancel()
                    return

    def generate_block(index: int, text: str, emotion: str, previous_bytes: int) -> int:
        assert model is not None and prompt_wav is not None
        if cancelled.is_set():
            return previous_bytes
        with model_lock:
            if cancelled.is_set():
                return previous_bytes
            if emotion == "calm":
                outputs = model.inference_zero_shot(text, PROMPT_TEXT, str(prompt_wav), stream=True)
            else:
                outputs = model.inference_instruct2(
                    text, EMOTION_INSTRUCTIONS[emotion], str(prompt_wav), stream=True,
                )
            state = None
            total = previous_bytes
            for item in outputs:
                if cancelled.is_set():
                    return total
                values = item["tts_speech"].detach().cpu().numpy().reshape(-1)
                pcm24 = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                pcm32, state = resample_24k_to_32k(pcm24, state)
                if pcm32:
                    total += len(pcm32)
                    put({"type": "audio", "pcm": base64.b64encode(pcm32).decode("ascii")})
            if not cancelled.is_set():
                put({"type": "done", "index": index, "text_chars": len(text), "end_received_bytes": total})
            return total

    async def produce() -> None:
        total = 0
        index = 0
        try:
            while not cancelled.is_set():
                request = await requests.get()
                if request is None:
                    break
                total = await asyncio.to_thread(
                    generate_block, index, request["text"], request["emotion"], total,
                )
                index += 1
            await outgoing.put({"type": "finished"})
        except Exception as exc:
            await outgoing.put({"type": "error", "message": f"CosyVoice 合成失败：{type(exc).__name__}: {str(exc)[:180]}"})

    async def send_output() -> None:
        while True:
            message = await outgoing.get()
            await websocket.send_text(json.dumps(message, ensure_ascii=False))
            if message["type"] in {"finished", "error"}:
                return

    producer = asyncio.create_task(produce())
    sender = asyncio.create_task(send_output())
    try:
        while True:
            incoming = asyncio.create_task(websocket.receive_text())
            completed, _ = await asyncio.wait({incoming, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in completed:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                await sender
                break
            data = json.loads(incoming.result())
            kind = data.get("type")
            if kind == "text":
                text = data.get("text")
                emotion = data.get("emotion")
                if not isinstance(text, str) or not text or len(text) > 2000 or emotion not in {"calm", *EMOTION_INSTRUCTIONS}:
                    raise ValueError("文本块或情绪值无效。")
                await requests.put({"text": text, "emotion": emotion})
            elif kind == "finish":
                await requests.put(None)
            elif kind == "cancel":
                cancelled.set()
                # A model call may take several seconds before its next
                # yield.  Acknowledge the cancellation immediately so the
                # Voice Server can settle playback without killing the whole
                # Worker (which may serve a different transaction next).
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                try:
                    await websocket.send_text(json.dumps({"type": "finished"}))
                except (WebSocketDisconnect, RuntimeError):
                    pass
                break
            else:
                raise ValueError("未知 Worker 请求。")
        if not sender.done():
            await sender
    except (WebSocketDisconnect, ValueError, json.JSONDecodeError):
        cancelled.set()
    finally:
        cancelled.set()
        if not producer.done():
            producer.cancel()
        if not sender.done():
            sender.cancel()
        await asyncio.gather(producer, sender, return_exceptions=True)


def main() -> None:
    global model, prompt_wav
    from cosyvoice.cli.cosyvoice import AutoModel
    import uvicorn

    directory = Path(os.environ["VOICE_COSY_MODEL_DIR"])
    prompt_wav = Path(os.environ["VOICE_COSY_PROMPT_WAV"])
    if not prompt_wav.is_file():
        raise FileNotFoundError(prompt_wav)
    model = AutoModel(model_dir=str(directory), fp16=False, load_trt=False, load_vllm=False)
    if model.sample_rate != 24000:
        raise RuntimeError(f"模型采样率应为 24000，实际为 {model.sample_rate}")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["VOICE_COSY_PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
