"""DSH attachment transport（代理宿主回环附件传输）：只负责二进制响应与流生命周期。"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import BinaryIO

import anyio
from starlette.responses import Response, StreamingResponse


ATTACHMENT_TYPE = "application/octet-stream"  # ATTACHMENT_TYPE（原始二进制响应媒体类型）。
MAX_CAPTURE_METADATA = 16 * 1024  # MAX_CAPTURE_METADATA（截图清单上限，不计图片字节）。
FILE_READ_SIZE = 256 * 1024  # FILE_READ_SIZE（磁盘读取缓冲区大小，无逐块请求或回执）。


def attachment_response(data: bytes) -> Response:
    return Response(data, media_type=ATTACHMENT_TYPE, headers={"Cache-Control": "no-store"})


def capture_response(packet: dict) -> StreamingResponse:
    """capture_response（截图响应）：四字节大端清单长度、清单正文、按清单顺序的原始图像。"""
    images = packet["attachments"]
    data = [image["data"] for image in images]
    manifest = {**packet, "attachments": [
        {key: value for key, value in image.items() if key != "data"} for image in images
    ]}
    metadata = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(metadata) <= MAX_CAPTURE_METADATA:
        raise ValueError("截图附件清单长度超出范围。")
    prefix = len(metadata).to_bytes(4, "big") + metadata
    return StreamingResponse(iter((prefix, *data)), media_type=ATTACHMENT_TYPE,
                             headers={"Cache-Control": "no-store",
                                      "Content-Length": str(len(prefix) + sum(map(len, data)))})


class StagedAttachmentResponse(StreamingResponse):
    """StagedAttachmentResponse（暂存文件响应）：绑定经验证的读句柄，断开后也归还票据。"""

    def __init__(self, handle: BinaryIO, size: int, *, is_current: Callable[[], bool],
                 release: Callable[[], None]) -> None:
        self._release = release

        async def body():
            remaining = size
            while remaining:
                chunk = await anyio.to_thread.run_sync(handle.read, min(FILE_READ_SIZE, remaining))
                if not is_current():
                    raise RuntimeError("附件读取期间设备或 DSH 激活已变化。")
                if not chunk:
                    raise RuntimeError("暂存文件在读取期间缩短。")
                remaining -= len(chunk)
                yield chunk

        super().__init__(body(), media_type=ATTACHMENT_TYPE,
                         headers={"Cache-Control": "no-store", "Content-Length": str(size)})

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._release)
