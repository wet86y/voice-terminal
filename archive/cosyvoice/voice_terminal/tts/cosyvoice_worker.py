"""Isolated CosyVoice synthesis Worker; no conversation or playback state."""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect


PROMPT_TEXT = "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。"
PROMPT_SPK_ID = "voice-terminal-default-prompt"
FIRST_TOKEN_HOP_LEN = 16
WARMUP_TEXT = "欢迎使用本地语音合成服务，这是模型启动预热语句。"
USE_FP16 = True
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


def cache_prompt_inputs() -> None:
    """每个 Worker（工作进程）只提取一次参考音频特征，后续文本分块复用。"""
    if model is None or prompt_wav is None:
        raise RuntimeError("CosyVoice 模型或参考音频尚未就绪")

    model.add_zero_shot_spk(PROMPT_TEXT, str(prompt_wav), PROMPT_SPK_ID)
    base = model.frontend.spk2info[PROMPT_SPK_ID]
    for emotion, instruction in EMOTION_INSTRUCTIONS.items():
        instruction_tokens, instruction_len = model.frontend._extract_text_token(instruction)
        variant = dict(base)
        variant["prompt_text"] = instruction_tokens
        variant["prompt_text_len"] = instruction_len
        model.frontend.spk2info[f"{PROMPT_SPK_ID}:{emotion}"] = variant


def warmup_model() -> None:
    """Worker（工作进程）报告就绪前先跑一段无声预热，避免首个用户请求承担 CUDA 冷启动。"""
    if model is None or prompt_wav is None:
        raise RuntimeError("CosyVoice 模型或参考音频尚未就绪")

    started = time.perf_counter()
    audio_samples = 0

    def warmup_text():
        # 使用与在线合成相同的文本流入路径，预热双向流推理分支。
        yield WARMUP_TEXT

    with model_lock:
        if hasattr(model.model, "token_hop_len"):
            model.model.token_hop_len = FIRST_TOKEN_HOP_LEN
        with preserve_cuda_allocator_cache():
            for item in model.inference_zero_shot(
                warmup_text(), PROMPT_TEXT, str(prompt_wav),
                zero_shot_spk_id=PROMPT_SPK_ID, stream=True,
            ):
                audio_samples += int(item["tts_speech"].numel())

    logging.info(
        "cosyvoice.warmup.ready mode=bistream elapsed_seconds=%.3f audio_seconds=%.3f first_token_hop_len=%d",
        time.perf_counter() - started,
        audio_samples / 24000,
        FIRST_TOKEN_HOP_LEN,
    )


@contextmanager
def preserve_cuda_allocator_cache():
    """保留 PyTorch CUDA（显卡计算）分配器缓存，供后续文本块复用显存。"""
    import torch

    empty_cache = torch.cuda.empty_cache
    if torch.cuda.is_available():
        torch.cuda.empty_cache = lambda: None
    try:
        yield
    finally:
        torch.cuda.empty_cache = empty_cache


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

    def next_text_request() -> dict | None:
        """从 asyncio（异步事件循环）队列安全地等待下一段文本。"""
        future = asyncio.run_coroutine_threadsafe(requests.get(), loop)
        while not cancelled.is_set():
            try:
                return future.result(timeout=0.1)
            except FutureTimeoutError:
                continue
        future.cancel()
        return None

    def generate_stream(first_request: dict) -> int:
        """一轮对话只启动一次模型，把后续文本片段持续喂给双向流推理。"""
        assert model is not None and prompt_wav is not None
        if cancelled.is_set():
            return 0
        state = None
        total = 0
        text_chars = 0
        text_blocks = 0
        first_text_at: float | None = None
        first_audio_at: float | None = None
        emotion = first_request["emotion"]

        def text_chunks():
            nonlocal first_text_at, text_chars, text_blocks
            request: dict | None = first_request
            while request is not None and not cancelled.is_set():
                if request["emotion"] != emotion:
                    raise ValueError("CosyVoice 双向流推理不支持在同一轮中途切换情绪")
                text = request["text"]
                if first_text_at is None:
                    first_text_at = time.perf_counter()
                text_chars += len(text)
                text_blocks += 1
                logging.info(
                    "cosyvoice.bistream.text index=%d text_chars=%d",
                    text_blocks - 1,
                    len(text),
                )
                yield text
                request = next_text_request()

        with model_lock:
            if cancelled.is_set():
                return 0
            # CosyVoice3 会在每个流式块后增大此共享值。每段新文本都应从
            # 上游设定的首块步长开始，避免沿用之前累积出的较大步长。
            if hasattr(model.model, "token_hop_len"):
                model.model.token_hop_len = FIRST_TOKEN_HOP_LEN
            if emotion == "calm":
                outputs = model.inference_zero_shot(
                    text_chunks(), PROMPT_TEXT, str(prompt_wav),
                    zero_shot_spk_id=PROMPT_SPK_ID, stream=True,
                )
            else:
                outputs = model.inference_instruct2(
                    text_chunks(), EMOTION_INSTRUCTIONS[emotion], str(prompt_wav),
                    zero_shot_spk_id=f"{PROMPT_SPK_ID}:{emotion}", stream=True,
                )
            # 整段生成期间保留锁：token_hop_len 和 CUDA 模型状态均为共享对象。
            # 上游每段结束会调用 empty_cache；在此局部屏蔽以复用分配器缓存，
            # 避免下一分句重新向驱动申请显存。取消或异常时会恢复原函数。
            with preserve_cuda_allocator_cache():
                for item in outputs:
                    if cancelled.is_set():
                        return total
                    values = item["tts_speech"].detach().cpu().numpy().reshape(-1)
                    pcm24 = (np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                    pcm32, state = resample_24k_to_32k(pcm24, state)
                    if pcm32:
                        total += len(pcm32)
                        if first_audio_at is None:
                            first_audio_at = time.perf_counter()
                            if first_text_at is not None:
                                logging.info(
                                    "cosyvoice.bistream.first_pcm elapsed_seconds=%.3f text_blocks=%d",
                                    first_audio_at - first_text_at,
                                    text_blocks,
                                )
                        put({"type": "audio", "pcm": base64.b64encode(pcm32).decode("ascii")})
        elapsed = max(0.001, time.perf_counter() - (first_text_at or time.perf_counter()))
        audio_seconds = total / (32000 * 2)
        logging.info(
            "cosyvoice.bistream.complete text_blocks=%d text_chars=%d audio_bytes=%d audio_seconds=%.3f rtf=%.3f",
            text_blocks,
            text_chars,
            total,
            audio_seconds,
            elapsed / audio_seconds if audio_seconds else 0.0,
        )
        return total

    async def produce() -> None:
        try:
            first_request = await requests.get()
            if first_request is not None and not cancelled.is_set():
                await asyncio.to_thread(generate_stream, first_request)
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
                try:
                    requests.put_nowait(None)
                except asyncio.QueueFull:
                    pass
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
        try:
            requests.put_nowait(None)
        except asyncio.QueueFull:
            pass
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
    model = AutoModel(model_dir=str(directory), fp16=USE_FP16, load_trt=False, load_vllm=False)
    if model.sample_rate != 24000:
        raise RuntimeError(f"模型采样率应为 24000，实际为 {model.sample_rate}")
    cache_prompt_inputs()
    warmup_model()
    logging.info(
        "cosyvoice.ready device=%s fp16=%s trt=%s cached_prompt_variants=%d speech_tokenizer_providers=%s campplus_providers=%s",
        model.model.device,
        USE_FP16,
        False,
        len(EMOTION_INSTRUCTIONS) + 1,
        ",".join(model.frontend.speech_tokenizer_session.get_providers()),
        ",".join(model.frontend.campplus_session.get_providers()),
    )
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["VOICE_COSY_PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
