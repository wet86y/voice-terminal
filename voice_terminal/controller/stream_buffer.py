"""Bounded asynchronous text-buffer flush without restarting upstream reads."""
from __future__ import annotations

import asyncio
import time

FLUSH_DUE = object()

async def buffered_stream(source, bridge):
    """Arm one local flush deadline only while a speakable prefix is buffered.

    The pending upstream read is never restarted on timeout. This flushes a
    stalled model's text without making another model request or polling it.
    """
    iterator = source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(iterator.__anext__())
            text = getattr(bridge, "pending_text", getattr(bridge, "buffer", ""))
            wait_reader = getattr(bridge, "flush_wait_seconds", None)
            if callable(wait_reader):
                timeout = wait_reader(now=time.monotonic())
            else:
                timeout = bridge.max_wait_ms / 1000 if len(text) >= getattr(bridge, "min_chars", 1) else None
            ready, _ = await asyncio.wait({pending}, timeout=timeout)
            if not ready:
                yield FLUSH_DUE
                continue
            try:
                value = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield value
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
