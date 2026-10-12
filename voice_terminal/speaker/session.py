"""激活会话声纹：句内迟滞、固定临时质心和有界文本暂存。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import math
import time
from uuid import uuid4

import numpy as np

from .engine import WeSpeakerOnnxEngine
from .contracts import SpeakerState
from .settings import TUNING_DEFAULTS

cosine = WeSpeakerOnnxEngine.similarity


def unit(vector) -> tuple[float, ...]:
    """归一化向量；空、非有限值和零向量不构成证据。"""
    value = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(value))
    if not value.size or not np.isfinite(value).all() or not math.isfinite(norm) or norm <= 1e-8:
        return ()
    return tuple(float(x) for x in value / norm)


@dataclass
class TranscriptPiece:
    """转写增量；状态变化不撤销已经放行的正文。"""
    turn_id: str  # 话语编号。
    generation: int  # 输入代次。
    revision: int  # 识别修订号。
    start: int  # 音频起点样本。
    end: int  # 音频终点样本。
    text: str  # 新增识别正文。
    created_at: float  # 单调时钟创建时间。
    status: str = "pending"  # 待确认、获准、非主人或混合跨度。
    embedding: tuple[float, ...] = field(default_factory=tuple, repr=False)  # 声纹证据。


class SessionSpeakerPolicy:
    """每个控制器持有一份；普通话语结束不会清除会话质心。"""
    def __init__(self, config, scorer) -> None:
        self.config = config
        self._wall_offset = time.time()-time.monotonic() # 原始采集时间转换为展示时间。
        self.scorer = scorer
        self.settings_revision = 0  # 设置版本。
        self.activation_id = ""  # 激活会话编号。
        self.session_centroid: tuple[float, ...] = ()  # 固定会话质心。
        self.samples: list[tuple[tuple[float, ...], int]] = []
        self.pieces: list[TranscriptPiece] = []
        self.evidence: deque = deque(maxlen=256)
        self._seen: set[tuple[str, int, int]] = set()
        self._last_end: dict[tuple[str, int], int] = {}
        self._states: dict[tuple[str, int], SpeakerState] = {}
        self._negative: dict[tuple[str, int], int] = {}
        self._boundary: dict[tuple[str, int], int] = {}
        self._granted_keys: set[tuple[str,int]] = set() # 仅凭初始授权成立的句内身份。
        self.last_owner_at: float | None = None
        self._continuity = False
        self.started_at: float | None = None
        self.calibration_config: dict = {}
        self.calibration_status = "inactive"
        self.calibration_speech_ms = 0
        self._calibration_end = -1
        self.last_scores: dict = {}
        self.timeout_count = 0
        self.recovered_count = 0

    def tune(self, config) -> None:
        self.config = config
        self.settings_revision += 1
        self._negative.clear()  # 不累加使用不同阈值的反证。

    def activate(self, source: str, *, now: float | None = None) -> None:
        if self.activation_id:
            return  # 重复开始不刷新授权期限。
        self.activation_id = uuid4().hex
        if source in {"manual", "kws"}:
            self.started_at = time.monotonic() if now is None else now
            self.calibration_config = {k: getattr(self.config, k, v) for k, v in TUNING_DEFAULTS.items()}
            self.calibration_status = "collecting"

    def clear(self) -> None:
        config, scorer, revision = self.config, self.scorer, self.settings_revision
        self.__init__(config, scorer)
        self.settings_revision = revision + 1

    def grace(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.calibration_status != "collecting" or self.started_at is None:
            return False
        if (now - self.started_at) * 1000 >= self.calibration_config["calibration_max_ms"]:
            self.calibration_status = "failed"
            self.samples.clear()
            for key in self._granted_keys:
                self._states.pop(key,None)
            self._granted_keys.clear()
            return False
        return True

    def _collect(self, vector: tuple[float, ...], start: int, end: int, speech_ms: int, now: float) -> None:
        if not self.grace(now):
            return
        # 音频时间段去重；重叠上下文仅按本次新增音频比例计权。
        new_samples = max(0, end - max(start, self._calibration_end))
        self._calibration_end = max(end, self._calibration_end)
        new_ms = min(speech_ms, new_samples * 1000 // 16000)
        if new_ms <= 0:
            return
        if self.samples:
            established = unit(sum((np.asarray(v)*weight for v,weight in self.samples), np.zeros(len(vector))))
            if not established or cosine(vector, established) < self.calibration_config["calibration_similarity"]:
                return  # 明显异人样本不替换已经采集的一致样本。
        candidate = self.samples + [(vector, new_ms)]
        centre = unit(sum((np.asarray(v) * weight for v, weight in candidate), np.zeros(len(vector))))
        minimum = self.calibration_config["calibration_similarity"]
        candidate = [(v, weight) for v, weight in candidate if cosine(v, centre) >= minimum]
        if not candidate:
            return
        self.samples = candidate
        self.calibration_speech_ms = sum(weight for _, weight in candidate)
        centre = unit(sum((np.asarray(v) * weight for v, weight in candidate), np.zeros(len(vector))))
        if (self.calibration_speech_ms >= self.calibration_config["calibration_speech_ms"]
                and all(cosine(v, centre) >= minimum for v, _ in candidate)):
            self.session_centroid = centre
            self.calibration_status = "ready"
            self.samples.clear()  # 冻结质心后不再更新。

    def observe(self, vector, *, turn_id: str, generation: int, start: int, end: int,
                speech_ms: int, now: float | None = None) -> SpeakerState:
        now = time.monotonic() if now is None else now
        key = (turn_id, generation)
        previous = self.state(*key, now=now)
        vector = unit(vector)
        if not vector or end <= self._last_end.get(key, -1):
            return previous
        self._last_end[key] = end
        granted = self.grace(now)
        self._collect(vector, start, end, speech_ms, now)
        permanent = self.scorer(vector)
        temporary = cosine(vector, self.session_centroid) if self.session_centroid else None
        score = permanent
        if permanent is not None and temporary is not None:
            weight = self.config.session_weight
            score = (1 - weight) * permanent + weight * temporary
        self.last_scores = {"permanent_score": permanent, "session_score": temporary, "combined_score": score}
        threshold = self.config.confirm_threshold
        limit = self.config.continuity_max_ms if self._continuity else self.config.continuity_window_ms
        continuous = self.last_owner_at is not None and 0 <= (now - self.last_owner_at) * 1000 <= limit
        if continuous:
            threshold = self.config.continuity_threshold
        if granted:
            state = SpeakerState.OWNER_CONFIRMED
            self._negative[key] = 0
            if score is not None and math.isfinite(score) and score >= threshold:
                self._granted_keys.discard(key)
                if score >= self.config.confirm_threshold:
                    self.last_owner_at = now
                    self._continuity = False
            else:
                self._granted_keys.add(key)
        elif score is None or not math.isfinite(score):
            state = previous
        elif score >= threshold:
            state = SpeakerState.OWNER_CONFIRMED if score >= self.config.confirm_threshold else SpeakerState.OWNER_CONTINUITY
            self._negative[key] = 0
            self._granted_keys.discard(key)
            if score >= self.config.confirm_threshold:
                self.last_owner_at = now
            self._continuity = state == SpeakerState.OWNER_CONTINUITY
        elif score < self.config.non_owner_threshold:
            self._negative[key] = self._negative.get(key, 0) + 1
            state = previous
            if self._negative[key] >= self.config.non_owner_ticks:
                state = SpeakerState.NON_OWNER_CONFIRMED
                if previous in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
                    self._boundary[key] = end  # 只拦截此时之后的增量。
                self.last_owner_at = None
        else:
            self._negative[key] = 0
            state = previous
        self._states[key] = state
        self.last_scores["threshold_used"] = threshold
        self.evidence.append((key, start, end, vector, state))
        if state == SpeakerState.NON_OWNER_CONFIRMED:
            for piece in self.pieces:
                if (piece.status == "pending" and (piece.turn_id,piece.generation) == key
                        and piece.embedding and 1-cosine(piece.embedding,vector) <= self.config.recovery_max_distance):
                    piece.status = "rejected"
        self._recover(key, vector, state, now)
        return state

    def state(self, turn_id: str, generation: int, *, now: float | None = None) -> SpeakerState:
        if self.grace(now):
            return SpeakerState.OWNER_CONFIRMED
        key = (turn_id,generation)
        if key in self._granted_keys:
            self._states.pop(key,None)
            self._granted_keys.discard(key)
        return self._states.get((turn_id, generation), SpeakerState.UNKNOWN)

    def turn_embedding(self, turn_id: str, generation: int) -> tuple[float, ...]:
        vectors = [v for key, _, _, v, _ in self.evidence if key == (turn_id, generation)]
        return unit(np.mean(vectors, axis=0)) if vectors else ()

    def ingest(self, *, turn_id: str, generation: int, revision: int, start: int, end: int,
               text: str, bypass: bool = False, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.expire(now)
        identity = (turn_id, generation, revision)
        if not text or identity in self._seen:
            return
        self._seen.add(identity)
        key = (turn_id, generation)
        overlaps = [item for item in self.evidence if item[0] == key and item[1] < end and item[2] > start]
        vector = unit(np.mean([item[3] for item in overlaps], axis=0)) if overlaps else ()
        state = self.state(*key, now=now)
        boundary = self._boundary.get(key)
        status = "pending"
        if bypass or self.grace(now):
            status = "admitted"
        elif boundary is not None and start < boundary < end:
            status = "mixed"
        elif state == SpeakerState.NON_OWNER_CONFIRMED and (boundary is None or start >= boundary):
            status = "rejected"
        elif state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY} and overlaps:
            # 当前局部确认不能覆盖已经记录的非主人区间。
            if any(item[4] == SpeakerState.NON_OWNER_CONFIRMED for item in overlaps):
                status = "mixed"
            elif vector and 1 - cosine(vector, overlaps[-1][3]) <= self.config.recovery_max_distance:
                status = "admitted"
        self.pieces.append(TranscriptPiece(turn_id, generation, revision, start, end, text, now, status, vector))
        self.expire(now)

    def _recover(self, key, vector, state, now) -> None:
        self.expire(now)
        if state not in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
            return
        for piece in self.pieces:
            if piece.status != "pending":
                continue
            if not piece.embedding:
                candidates = [e for e in self.evidence if e[0] == (piece.turn_id, piece.generation)
                              and e[1] <= piece.start and e[2] >= piece.end]
                if candidates and all(e[4] != SpeakerState.NON_OWNER_CONFIRMED for e in candidates):
                    piece.embedding = candidates[-1][3]
            if piece.embedding and 1 - cosine(piece.embedding, vector) <= self.config.recovery_max_distance:
                piece.status = "admitted"
                self.recovered_count += 1

    def expire(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        pending = list(dict.fromkeys(p.turn_id for p in self.pieces if p.status in {"pending", "mixed"}))
        evicted = set(pending[:-self.config.pending_max_turns])
        self.pieces[:] = [p for p in self.pieces if (p.status == "admitted" or p.turn_id not in evicted)
                         and (p.status == "admitted" or (now - p.created_at) * 1000 < self.config.pending_ttl_ms)]
        # 已放行正文仍在上下文中；账本只保留最近64轮，避免长会话增长无界。
        turns = list(dict.fromkeys(p.turn_id for p in self.pieces))
        removed = set(turns[:-64])
        self.pieces[:] = [p for p in self.pieces if p.turn_id not in removed]
        self._seen = {(p.turn_id, p.generation, p.revision) for p in self.pieces}
        active = {(p.turn_id, p.generation) for p in self.pieces}
        for mapping in (self._states, self._negative, self._boundary, self._last_end):
            if len(mapping) > 128:
                for key in list(mapping):
                    if key not in active:
                        mapping.pop(key, None)

    def admitted_text(self, turn_id: str, generation: int | None = None) -> str:
        return "".join(p.text for p in self.pieces if p.turn_id == turn_id and p.status == "admitted"
                       and (generation is None or p.generation == generation))

    def admitted_turns(self) -> list[dict]:
        result = []
        for turn_id in dict.fromkeys(p.turn_id for p in self.pieces):
            text = self.admitted_text(turn_id)
            if text:
                first = next(p for p in self.pieces if p.turn_id == turn_id)
                result.append({"turn_id": turn_id, "text": text, "generation": first.generation, "occurred_at": first.created_at+self._wall_offset})
        return result

    def fully_admitted(self, turn_id: str) -> bool:
        pieces = [p for p in self.pieces if p.turn_id == turn_id]
        return bool(pieces) and all(p.status == "admitted" for p in pieces)

    def snapshot(self) -> dict:
        self.grace()
        return {"activation_id": self.activation_id, "settings_revision": self.settings_revision,
                "calibration_status": self.calibration_status, "calibration_speech_ms": self.calibration_speech_ms,
                "session_centroid_ready": bool(self.session_centroid), "session_weight": self.config.session_weight,
                "pending_count": sum(p.status in {"pending","mixed"} for p in self.pieces),
                "recovered_count": self.recovered_count, "timeout_count": self.timeout_count, **self.last_scores}
