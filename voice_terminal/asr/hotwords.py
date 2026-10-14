"""HotwordService（热词纠正）：预建索引、短句投影、分通道限时处理。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import logging
import math
import re
import time

logger = logging.getLogger(__name__)
DEFAULT_HOTWORDS = {"enabled": True, "threshold": 0.85, "entries": []}  # 默认配置。
MAX_HOTWORDS = 5000  # MAX_HOTWORDS（最大目标词数）。
MAX_FILE_BYTES = 1024 * 1024  # MAX_FILE_BYTES（导入文件上限）。
_BOUNDARIES = re.compile(r"([，。！？；,!?;\r\n]+|\.(?=\s|$))")  # 不跨短句边界；保留名称内部的点号。


def validate_hotwords(value: object) -> dict:
    """validate_hotwords（严格校验）：字段只接受明确的用户配置。"""
    if not isinstance(value, dict) or set(value) != {"enabled", "threshold", "entries"}:
        raise ValueError("热词设置须包含 enabled（启用）、threshold（相似度阈值）和 entries（词表）。")
    enabled, threshold, entries = value["enabled"], value["threshold"], value["entries"]
    if not isinstance(enabled, bool):
        raise ValueError("热词启用状态必须是布尔值。")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0.5 <= threshold <= 1:
        raise ValueError("热词相似度阈值必须在 0.5～1 之间。")
    if not isinstance(entries, list) or len(entries) > MAX_HOTWORDS:
        raise ValueError(f"热词最多支持 {MAX_HOTWORDS} 条。")
    cleaned, seen = [], set()
    def word(raw):
        if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 96:
            raise ValueError("目标词、别名及排除词须为 1～96 个字符。")
        result = raw.strip()
        if any(c in result for c in "|~\r\n\x00") or result.startswith("#"):
            raise ValueError("词条不能包含分隔符、换行、空字符或以 # 开头。")
        return result
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"word", "aliases", "blacklist"}:
            raise ValueError("词条须包含 word（目标词）、aliases（别名）和 blacklist（邻近排除词）。")
        target = word(entry["word"])
        if target in seen:
            raise ValueError(f"目标词重复：{target}")
        seen.add(target)
        row = {"word": target}
        for name in ("aliases", "blacklist"):
            values = entry[name]
            if not isinstance(values, list) or len(values) > 8:
                raise ValueError("每条热词最多支持 8 个别名和 8 个邻近排除词。")
            row[name] = list(dict.fromkeys(word(v) for v in values))
        row["aliases"] = [v for v in row["aliases"] if v != target]
        cleaned.append(row)
    result = {"enabled": enabled, "threshold": float(threshold), "entries": cleaned}
    if len(to_hotword_text(cleaned).encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("热词文件不能超过 1MiB（二进制兆字节）。")
    return result


def to_hotword_text(entries: list[dict]) -> str:
    """to_hotword_text（导出原生词表）：第一项为目标，随后为别名及排除词。"""
    return "\n".join(" | ".join([e["word"], *e["aliases"]]) +
                     (" ~~~ " + " | ".join(e["blacklist"]) if e["blacklist"] else "") for e in entries)


def parse_hotword_text(text: object) -> list[dict]:
    """parse_hotword_text（导入文本）：注释、空行和重复目标合并。"""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("请导入不超过 1MiB（二进制兆字节）的文本文件。")
    rows = {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("~~~")
        if len(parts) > 2:
            raise ValueError(f"第 {number} 行包含多个排除词分隔符。")
        words = [v.strip() for v in parts[0].split("|")]
        blacklist = [v.strip() for v in parts[1].split("|")] if len(parts) == 2 else []
        if not all(words) or (len(parts) == 2 and not all(blacklist)):
            raise ValueError(f"第 {number} 行包含空目标词、别名或排除词。")
        row = rows.setdefault(words[0], {"word": words[0], "aliases": [], "blacklist": []})
        row["aliases"] = list(dict.fromkeys([*row["aliases"], *words[1:]]))
        row["blacklist"] = list(dict.fromkeys([*row["blacklist"], *blacklist]))
    return validate_hotwords({**DEFAULT_HOTWORDS, "entries": list(rows.values())})["entries"]


@dataclass
class HotwordBank:
    """HotwordBank（不可变词表代次）：两条通道各自持有匹配器。"""
    settings: dict  # settings（已校验配置）。
    matchers: dict = field(default_factory=dict)  # matchers（短句与整句实例）。


class HotwordService:
    """HotwordService（热词服务）：每轮固定词表，原始文本不回写。"""
    def __init__(self):
        self.update_lock = asyncio.Lock()  # update_lock（索引配置及持久化互斥）。
        self.bank = HotwordBank(deepcopy(DEFAULT_HOTWORDS))  # bank（当前词表）。
        self.turns = OrderedDict()  # turns（最近轮次及固定词表、结果缓存）。
        self.executors = {lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"hotword-{lane}") for lane in ("stream", "final")}
        self.pending = {}  # pending（仍在执行的任务；超时后也禁止堆积）。
        self.last_status = "empty"  # last_status（最近一次处理状态）。

    @staticmethod
    def prepare(value: object) -> HotwordBank:
        settings = validate_hotwords(value)
        bank = HotwordBank(settings)
        if settings["enabled"] and settings["entries"]:
            from ..vendor.asr_hotword import PhonemeCorrector
            text = to_hotword_text(settings["entries"])
            for lane in ("stream", "final"):
                matcher = PhonemeCorrector(threshold=settings["threshold"])
                matcher.update_hotwords(text)
                matcher.correct("测试热词纠正。")  # 提前预热，不在提交路径导入依赖。
                bank.matchers[lane] = matcher
        return bank

    async def configure(self, value: object):
        async with self.update_lock:
            settings = validate_hotwords(value() if callable(value) else value)  # 在锁内读取，避免启动旧快照覆盖刚保存的设置。
            if settings != self.bank.settings:
                self.bank = await asyncio.to_thread(self.prepare, settings)

    def _turn(self, key):
        key = key[0] if isinstance(key, tuple) else key  # 同一发言续段变更代次，词表仍固定。
        if key not in self.turns:
            self.turns[key] = {"bank": self.bank, "cache": {}, "sealed": []}
            while len(self.turns) > 64:
                self.turns.popitem(last=False)
        self.turns.move_to_end(key)
        return self.turns[key]

    @staticmethod
    def run(matcher, text: str) -> dict:
        output, matches = [], []
        for part in _BOUNDARIES.split(text):
            if not part:
                continue
            if _BOUNDARIES.fullmatch(part):
                output.append(part)
                continue
            result = matcher.correct(part)
            output.append(result.text)
            matches.extend({"original": a, "replacement": b, "score": score} for a, b, score in result.matches)
        return {"text": "".join(output), "matches": matches}

    async def correct(self, text: str, key, *, lane="stream", cache_key=None) -> dict:
        turn = self._turn(key)
        cache_key = (cache_key, text)
        if cache_key in turn["cache"]:
            return deepcopy(turn["cache"][cache_key])
        matcher = turn["bank"].matchers.get(lane)
        started = time.perf_counter()
        status, result = "disabled" if not turn["bank"].settings["enabled"] else "empty", {"text": text, "matches": []}
        if matcher and text.strip():
            previous = self.pending.get(lane)
            if previous is not None and not previous.done():
                status = "busy"
            else:
                future = asyncio.get_running_loop().run_in_executor(self.executors[lane], self.run, matcher, text)
                self.pending[lane] = future
                # shield（保护任务）：超时不伪装成线程停止，迟到结果绝不提交。
                future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                try:
                    result = await asyncio.wait_for(asyncio.shield(future), 0.05 if lane == "stream" else 0.10)
                    status = "corrected" if result["matches"] else "unchanged"
                except asyncio.TimeoutError:
                    status = "timeout"
                except Exception:
                    status = "error"
                    logger.exception("热词纠正失败，保留原始文本")
        result.update(raw_text=text, status=status, elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        self.last_status = status
        turn["cache"][cache_key] = result
        while len(turn["cache"]) > 256:
            turn["cache"].pop(next(iter(turn["cache"])))
        return deepcopy(result)

    async def sentences(self, rows: list[dict], key) -> list[dict]:
        """sentences（短句投影）：编号不变，不修改分句器的原始记录。"""
        projected = []
        for row in rows:
            result = await self.correct(str(row.get("text") or ""), key, cache_key=(row.get("sentence_id"), row.get("revision")))
            projected.append({**row, "text": result["text"], "raw_text": result["raw_text"], "hotword_matches": result["matches"]})
            projected[-1]["hotword_status"] = result["status"]  # hotword_status（是否需最终稿补纠正）。
        self._turn(key)["sealed"] = projected
        return projected

    async def finalized(self, text: str, key) -> dict:
        """finalized（最终稿投影）：复用已提交短句，避免再次改写。"""
        sealed = self._turn(key)["sealed"]
        raw_prefix = "".join(row["raw_text"] for row in sealed)
        reusable = all(row["hotword_status"] not in {"timeout", "busy", "error"}
                       and bool(_BOUNDARIES.fullmatch(row["raw_text"].rstrip()[-1:])) for row in sealed)
        if raw_prefix and reusable and text.startswith(raw_prefix):
            tail = await self.correct(text[len(raw_prefix):], key, lane="final", cache_key="tail")
            return {**tail, "raw_text": text,
                    "text": "".join(row["text"] for row in sealed)+tail["text"],
                    "matches": [m for row in sealed for m in row["hotword_matches"]]+tail["matches"]}
        return await self.correct(text, key, lane="final", cache_key="final")

    def final_sentences(self, rows: list[dict], raw: str, result: dict, key) -> list[dict]:
        """final_sentences（最终短句投影）：原句编号和原文保留，派生文本与最终稿对齐。"""
        previous = self._turn(key)["sealed"]
        if "".join(row["text"] for row in rows) != raw:
            result["sentence_alignment_complete"] = False  # 原始识别回退，不重写已封口原句。
            return deepcopy(previous)
        opcodes = SequenceMatcher(None, raw, result["text"], autojunk=False).get_opcodes()
        def boundary(position):
            if position == 0:
                return 0
            if position == len(raw):
                return len(result["text"])
            for tag, i1, i2, j1, j2 in opcodes:
                if i1 <= position <= i2:
                    return j1 + position - i1 if tag == "equal" else j1 if position == i1 else j2
            return len(result["text"])
        projected, cursor = [], 0
        previous_ids = {row.get("sentence_id"): row for row in previous}
        for row in rows:
            end = cursor + len(row["text"])
            text = result["text"][boundary(cursor):boundary(end)]
            old = previous_ids.get(row.get("sentence_id"))
            revision = max(row.get("revision", 1), old.get("revision", 1) if old else 1)
            if old and old["text"] != text:
                revision += 1
            projected.append({**row, "text": text, "raw_text": row["text"], "revision": revision,
                              "hotword_matches": [m for m in result["matches"] if m["original"] in row["text"] or m["replacement"] in text], "hotword_status": result["status"]})
            cursor = end
        result["sentence_alignment_complete"] = "".join(row["text"] for row in projected) == result["text"]
        self._turn(key)["sealed"] = projected
        return projected

    def protect_boundary(self, text: str, limit: int, key, min_chars: int) -> int:
        """protect_boundary（强制分句保护）：已知名称或别名完整出现时避开中间切点。"""
        settings = self._turn(key)["bank"].settings
        if not settings["enabled"]:
            return limit
        for entry in settings["entries"]:
            for word in (entry["word"], *entry["aliases"]):
                position = text.find(word, max(0, limit-len(word)+1))
                if 0 <= position < limit < position + len(word):
                    return position if position >= min_chars else position + len(word)
        return limit

    def clear(self):
        self.turns.clear()
        self.pending.clear()
        for executor in self.executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        self.executors = {lane: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"hotword-{lane}") for lane in ("stream", "final")}

    def close(self):
        self.clear()
        for executor in self.executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
