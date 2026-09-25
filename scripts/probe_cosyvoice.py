"""用真实模型检查首块音频、连续文本块、回执、取消及可选扬声器播放。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from voice_terminal.audio.player import PcmPlayer
from voice_terminal.tts.alignment import DeliveryResolver
from voice_terminal.tts.cosyvoice_runtime import CosyVoiceTtsClient, CosyVoiceWorkerManager


async def probe(*, play: bool, model_dir: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    manager = CosyVoiceWorkerManager(project_root=root, model_dir=model_dir)
    player = None
    try:
        await manager.ensure_ready()
        client = CosyVoiceTtsClient(manager)
        session = client.session(emotion="calm")
        chunks: list[bytes] = []
        first_audio = asyncio.Event()
        first_audio_at = 0.0

        async def collect() -> None:
            nonlocal first_audio_at
            async for pcm in session.audio_chunks():
                if not first_audio.is_set():
                    first_audio_at = time.monotonic()
                    first_audio.set()
                chunks.append(pcm)

        await session.start()
        collector = asyncio.create_task(collect())
        started = time.monotonic()
        await session.send_text("你好，这是本地语音合成测试。")
        await asyncio.wait_for(first_audio.wait(), 120)
        first = await asyncio.wait_for(session.wait_for_segment(0), 120)
        await session.send_text("现在开始第二个文本块。")
        second = await asyncio.wait_for(session.wait_for_segment(1), 120)
        await session.finish()
        await asyncio.wait_for(collector, 30)
        await session.close()

        pcm = b"".join(chunks)
        assert first.end_received_bytes > 0
        assert second.end_received_bytes == len(pcm)
        assert len(pcm) % 2 == 0
        summary = {
            "first_audio_ms": round((first_audio_at - started) * 1000),
            "pcm_chunks": len(chunks),
            "pcm_bytes": len(pcm),
            "duration_ms_at_32khz": round(len(pcm) / 64000 * 1000),
            "receipt_boundaries": [first.end_received_bytes, second.end_received_bytes],
        }

        if play:
            player = PcmPlayer(sample_rate=32000, channels=1, output_device=None)
            player.start()
            player.allow("cosyvoice-probe")
            for part in chunks:
                player.enqueue("cosyvoice-probe", part)
            await player.wait_until_output_complete(max(20, len(pcm) / 64000 + 10))
            summary["player"] = player.progress("cosyvoice-probe")

            # 用真实回执和播放器停止快照验证 Voice Server 的文本块结算。
            first_text = "第一段已经播放完成。"
            second_text = "第二段在打断时尚未确认完成。"
            settled = client.session(emotion="calm")
            resolver = DeliveryResolver()
            resolver.add_text(first_text, 0)
            key = "cosyvoice-interrupt-probe"
            player.allow(key)
            await settled.start()
            enqueued_bytes = 0
            accept_audio = True

            async def enqueue_audio() -> None:
                nonlocal enqueued_bytes
                async for part in settled.audio_chunks():
                    if accept_audio:
                        player.enqueue(key, part)
                        enqueued_bytes += len(part)

            enqueue_task = asyncio.create_task(enqueue_audio())
            await settled.send_text(first_text)
            first_receipt = await asyncio.wait_for(settled.wait_for_segment(0), 120)
            first_boundary_ms = int(first_receipt.end_received_bytes / 64000 * 1000)
            resolver.mark_boundary(first_boundary_ms)
            deadline = asyncio.get_running_loop().time() + 10
            while enqueued_bytes < first_receipt.end_received_bytes:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("第一个文本块的 PCM 未进入播放器。")
                await asyncio.sleep(0.01)
            await player.wait_until_output_complete(20)
            resolver.add_text(second_text, first_boundary_ms)
            await settled.send_text(second_text)
            accept_audio = False
            stopped = player.stop_and_snapshot(key)
            await settled.cancel(timeout_seconds=2)
            await asyncio.wait_for(enqueue_task, 5)
            delivery = resolver.resolve(stopped, first_text + second_text)
            assert delivery["heard_text"] == first_text
            assert second_text in delivery["uncertain_text"]
            summary["interrupted_delivery"] = delivery

        interrupted = client.session(emotion="calm")
        await interrupted.start()
        await interrupted.send_text("这是一条用于检查语音中断的长文本。" * 8)
        await asyncio.wait_for(interrupted.cancel(timeout_seconds=2), 15)
        summary["cancelled"] = True
        summary["worker_after_cancel"] = manager.status()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        if player is not None:
            player.stop()
        await manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="真实 CosyVoice 模型验收探针")
    parser.add_argument("--play", action="store_true", help="通过系统默认扬声器播放合成音频")
    parser.add_argument("--model-dir", type=Path,
                        default=Path(r"D:\模型\voice-terminal\cosyvoice3-0.5b-2512"))
    args = parser.parse_args()
    asyncio.run(probe(play=args.play, model_dir=args.model_dir))


if __name__ == "__main__":
    main()
