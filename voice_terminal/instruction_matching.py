"""InstructionMatcher（指令候选检索）：音素匹配、预热与有界等待，不改写转写。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import logging
import time

from .asr.hotwords import _BOUNDARIES

logger = logging.getLogger(__name__)
MATCH_TIMEOUT = 0.020  # MATCH_TIMEOUT（单次候选检索等待预算，秒）。


@dataclass(frozen=True)
class InstructionBank:
    """InstructionBank（已预热索引）：关键词仅存在于内部，不提供给模型。"""
    matcher: object  # matcher（ASR-Hotword 音素匹配器）。
    owners: dict[str, tuple[str, ...]]  # owners（关键词对应的指令标识）。
    order: tuple[str, ...]  # order（设置中的稳定条目顺序）。


class InstructionMatcher:
    """InstructionMatcher（会话级检索服务）：单匹配线程且最多一份在途检索。"""

    def __init__(self):
        self.bank = None  # bank（当前可用索引）。
        self.generation = 0  # generation（设置代次）。
        self._settings = None  # 已换入的配置副本。
        self._executor = None  # 单工作线程，首次存在关键词时才创建。
        self._pending = None  # 超时后仍在运行的任务，不重复排队。
        self._preparing = None  # 独立预热任务，不被旧匹配占用的线程拖住。
        self._ready = False  # 当前设置的索引准备是否已经完成。
        self._lock = asyncio.Lock()
        self._turns = OrderedDict()  # 与热词服务一致：最多 64 轮，每轮 256 份缓存。
        self._closed = False
        self.last_status = "empty"  # last_status（最近检索状态）。

    @staticmethod
    def prepare(entries) -> InstructionBank | None:
        owners = {}
        order = []
        for item in entries:
            if not item["enabled"]:
                continue
            for word in item.get("keywords", []):
                owners.setdefault(word, []).append(item["id"])
            order.append(item["id"])
        if not owners:
            return None
        from .vendor.asr_hotword import PhonemeCorrector
        matcher = PhonemeCorrector(threshold=0.85, similar_threshold=0.65)
        matcher.update_hotwords("\n".join(owners))
        # 预热真实命中路径，包含音素精筛；无关短句只能预热粗筛。
        matcher.correct(next(iter(owners)), k=len(owners))
        return InstructionBank(matcher, {k: tuple(v) for k, v in owners.items()}, tuple(order))

    def _submit(self, function, *args):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="instruction-match")
        future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        # 消费超时/取消后的异常，线程任务不因调用方取消而伪装成停止。
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self._pending = future
        return future

    async def configure(self, entries) -> None:
        snapshot = deepcopy(entries)
        async with self._lock:
            if self._closed:
                return
            if self._preparing is not None and not self._preparing.done():
                same = snapshot == self._settings
                try:
                    await asyncio.shield(self._preparing)
                except Exception:
                    pass  # 完成回调负责记录失败，允许相同设置重新准备。
                if self._closed:
                    return
                if same and self._ready:
                    return
            if snapshot == self._settings and self._ready:
                return
            self._settings = snapshot
            self.generation += 1
            generation = self.generation
            self._ready = False
            self.bank = None
            self._turns.clear()
            if not any(item["enabled"] and item.get("keywords") for item in snapshot):
                self._ready = True
                return
            # 预热与匹配分离；同一时刻最多一份预热任务。即使等待方取消，完成回调
            # 仍按代次安装结果，避免相同设置因已登记而永久跳过初始化。
            preparing = asyncio.create_task(asyncio.to_thread(self.prepare, snapshot))
            self._preparing = preparing
            def completed(done):
                try:
                    bank = done.result()
                    if not self._closed and generation == self.generation:
                        self.bank = bank
                        self._ready = True
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("指令音素索引准备失败，继续使用触发语义")
                finally:
                    # 已完成任务会持有结果索引，安装后释放，确保停用时可回收。
                    if self._preparing is done:
                        self._preparing = None
            preparing.add_done_callback(completed)
            try:
                await asyncio.shield(preparing)
            except Exception:
                pass  # 原始异常由完成回调记录，不影响原有语义路由。

    def configured_for(self, entries) -> bool:
        """只给与当前设置相符的请求提供候选，避免取消保存后的快照错配。"""
        return self._ready and entries == self._settings

    @staticmethod
    def run(bank: InstructionBank, texts: tuple[str, ...]) -> tuple[str, ...]:
        scores = {}
        for text in dict.fromkeys(texts):
            for part in _BOUNDARIES.split(text):
                if not part.strip() or _BOUNDARIES.fullmatch(part):
                    continue
                # 只消费相似候选，永远不使用纠正文本或执行替换结果。
                result = bank.matcher.correct(part, k=len(bank.owners))
                for _, word, score in result.similars:
                    for identifier in bank.owners.get(word, ()):
                        scores[identifier] = max(scores.get(identifier, 0), score)
        return tuple(sorted(scores, key=lambda key: (-scores[key], bank.order.index(key))))

    async def match(self, turn_id: str, texts: tuple[str, ...]) -> tuple[str, ...]:
        bank, generation = self.bank, self.generation
        if self._closed or bank is None or not texts:
            self.last_status = "unavailable" if self._closed else "empty"
            return ()
        cache = self._turns.setdefault(turn_id, OrderedDict())
        self._turns.move_to_end(turn_id)
        while len(self._turns) > 64:
            self._turns.popitem(last=False)
        if texts in cache:
            cache.move_to_end(texts)
            self.last_status = "cached"
            return cache[texts]
        if self._pending is not None and not self._pending.done():
            self.last_status = "busy"
            return ()
        started = time.perf_counter()
        try:
            future = self._submit(self.run, bank, texts)
            deadline = started + MATCH_TIMEOUT
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), remaining)
                    break
                except asyncio.TimeoutError:
                    # Windows（视窗系统）的事件循环时钟可能比高精度计时器更粗，
                    # 定时器可提前到期；仍仅使用原有预算，不重新提交匹配任务。
                    if time.perf_counter() >= deadline:
                        raise
            if time.perf_counter() > deadline:
                raise asyncio.TimeoutError
            if self._closed or generation != self.generation:
                self.last_status = "stale"
                return ()
            cache[texts] = result
            while len(cache) > 256:
                cache.popitem(last=False)
            self.last_status = "matched" if result else "unmatched"
            return result
        except asyncio.TimeoutError:
            self.last_status = "timeout"
            return ()
        except Exception:
            self.last_status = "error"
            logger.exception("指令音素检索失败，继续使用触发语义")
            return ()
        finally:
            logger.debug("指令候选检索 状态=%s 耗时毫秒=%.3f", self.last_status,
                         (time.perf_counter() - started) * 1000)

    def close(self) -> None:
        self._closed = True
        self.bank = None
        self._settings = None
        self._ready = False
        self._preparing = None
        self._pending = None
        self._turns.clear()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
