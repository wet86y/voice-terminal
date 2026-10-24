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
from .turn_admission import TurnAdmission
from .continuity import OwnerContinuity
from .audit import audit, parameter_snapshot, parameter_effects, transcript_audit

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
    def __init__(self, config, scorer, *, settings_revision: int = 0) -> None:
        self.config = config
        self._wall_offset = time.time()-time.monotonic() # 原始采集时间转换为展示时间。
        self.scorer = scorer
        self.settings_revision = settings_revision  # 设置版本。
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
        self.turns: dict[str, TurnAdmission] = {}  # 正式语音轮次，与独立打断候选分开。
        self.continuity = OwnerContinuity()  # 普通输入和独立打断共享的跨轮资格。
        self.candidates: dict[str, TurnAdmission] = {}  # 打断候选仅跟踪证据，不发布普通文字。
        audit("settings_initialized（初始参数）", settings_revision=self.settings_revision,
              parameters=parameter_snapshot(config), effects=parameter_effects())

    def tune(self, config) -> None:
        keys = ("mode", "confirm_threshold", "continuity_threshold", "continuity_relaxation", "intra_turn_relaxation",
                "session_weight", "continuity_window_ms", "continuity_max_ms", "non_owner_ticks")
        changed = any(getattr(self.config, key, None) != getattr(config, key, None) for key in keys)
        self.config = config
        self.settings_revision += 1
        self._negative.clear()  # 不累加使用不同阈值的反证。
        if changed:
            self.revoke_continuity("settings_changed")
        for turn in (*self.turns.values(), *self.candidates.values()):
            turn.positive_ticks = turn.negative_ticks = 0
            if changed:
                turn.continuity_grant = {}
        if changed:
            for candidate in self.candidates.values():
                candidate.owner_confirmed = False
                candidate.normal_owner_epoch = None
                candidate.observation_state = SpeakerState.UNKNOWN
        audit("settings_applied（参数已应用）", activation_id=self.activation_id,
              settings_revision=self.settings_revision, effective=parameter_snapshot(config),
              calibration_config=self.calibration_config, effects=parameter_effects())

    def activate(self, source: str, *, now: float | None = None) -> None:
        if self.activation_id:
            return  # 重复开始不刷新授权期限。
        self.activation_id = uuid4().hex
        if source in {"manual", "kws"}:
            self.started_at = time.monotonic() if now is None else now
            self.calibration_config = {k: getattr(self.config, k, v) for k, v in TUNING_DEFAULTS.items()}
            self.calibration_status = "collecting"
        audit("activation（激活参数快照）", activation_id=self.activation_id,
              settings_revision=self.settings_revision, source=source,
              calibration_config=self.calibration_config)

    def begin_turn(self, turn_id: str, *, start_sample: int, generation: int = 0,
                   started_at: float | None = None, filtering: bool = True) -> TurnAdmission:
        """首段起点随逻辑轮次固定，续说只更新输入代次。"""
        turn = self.turns.get(turn_id)
        if turn is None:
            at = time.monotonic() if started_at is None else started_at
            grant = self.continuity.capture(at, self.config)
            turn = self.turns[turn_id] = TurnAdmission(turn_id, start_sample, generation,
                filtering=filtering, continuity_grant=grant)
            if not filtering:
                turn.phase, turn.admitted = "observing", True
            audit("turn_started（首段积累开始）", turn_id=turn_id, generation=generation,
                  activation_id=self.activation_id, settings_revision=self.settings_revision,
                  start_sample=start_sample, captured_at=at, continuity_grant=grant, **turn.summary())
        else:
            turn.generation = generation
        # 只淘汰已结束记录，活跃轮次的原文不受待定超时裁剪。
        self._prune_closed_turns()
        return turn

    def begin_candidate(self, candidate_id: str, *, start_sample: int, started_at: float) -> None:
        key = f"barge-{candidate_id}"
        if key not in self.candidates:
            self.candidates.clear()  # 只有当前播放候选有效，不积攒旧候选授权。
            self.candidates[key] = TurnAdmission(key, start_sample, filtering=False, admitted=True,
                phase="observing", continuity_grant=self.continuity.capture(started_at, self.config))
            audit("candidate_started（共享资格的打断候选）", turn_id=key, captured_at=started_at,
                  continuity_grant=self.candidates[key].continuity_grant, settings_revision=self.settings_revision)

    def revoke_continuity(self, reason: str) -> None:
        self.continuity.revoke(reason)
        self.last_owner_at, self._continuity = None, False  # 兼容观察不能回灌另一份授权时钟。
        audit("continuity_revoked（跨轮资格撤销）", reason=reason,
              settings_revision=self.settings_revision, continuity=self.continuity.snapshot(self.config))

    def boundary(self, turn_id: str, *, at: float, reason: str) -> None:
        turn = self.turns.get(turn_id)
        if turn is not None and not turn.closed:
            turn.boundary_revision += 1
            turn.boundary_at, turn.boundary_reason = at, reason
            audit("boundary_candidate（语音边界待定）", turn_id=turn_id, boundary_at=at,
                  boundary_revision=turn.boundary_revision, reason=reason, settings_revision=self.settings_revision)

    def resume_turn(self, turn_id: str) -> None:
        turn = self.turns.get(turn_id)
        if turn is not None and not turn.closed and turn.boundary_at is not None:
            turn.boundary_revision += 1
            turn.boundary_at, turn.boundary_reason = None, ""
            audit("boundary_cancelled（同轮续说撤销边界）", turn_id=turn_id,
                  boundary_revision=turn.boundary_revision, settings_revision=self.settings_revision)

    def _prune_closed_turns(self) -> None:
        closed = [key for key, value in self.turns.items() if value.closed]
        now = time.monotonic()
        for key in closed:
            item = self.turns[key]
            if key in closed[:-self.config.pending_max_turns] or (item.closed_at is not None
                    and (now-item.closed_at)*1000 >= self.config.pending_ttl_ms):
                self.turns.pop(key, None)
                audit("record_evicted（闭段诊断记录淘汰）", turn_id=key,
                      reason="closed_record_budget", settings_revision=self.settings_revision)

    def set_turn_text(self, turn_id: str, text: str, *, stage: str = "raw_asr", generation: int = 0) -> None:
        turn = self.turns.get(turn_id)
        if turn is None or turn.closed or turn.text == text:
            return
        previous = turn.text
        turn.text, turn.generation = text, generation
        turn.text_revision += 1
        transcript_audit(turn_id, stage, text, previous, turn.text_revision,
                         generation=generation, settings_revision=self.settings_revision,
                         admission_phase=turn.phase)

    def observe_turn(self, turn_id: str, vector, *, end: int, start: int,
                     speech_ms: int, first: bool = False, quality: str = "ready",
                     sequence: int = 0, latency_ms: float = 0, now: float | None = None,
                     window_speech_ms: int | None = None) -> SpeakerState:
        turn = self.turns.get(turn_id) or self.candidates.get(turn_id)
        if turn is None or turn.closed:
            return SpeakerState.UNKNOWN
        if not first and end <= turn.last_end:
            audit("observation_skipped（重复音频不计数）", turn_id=turn_id,
                  end_sample=end, reason="no_new_audio", settings_revision=self.settings_revision)
            return turn.state
        now = time.monotonic() if now is None else now
        permanent = temporary = None
        try:
            vector = unit(vector) if vector is not None else ()
            if vector and quality == "ready":
                permanent = self.scorer(vector)
                if permanent is not None and not math.isfinite(permanent):
                    permanent = None
                temporary = cosine(vector, self.session_centroid) if self.session_centroid else None
                if temporary is not None and not math.isfinite(temporary):
                    temporary = None
                # 当前样本可以完成校准，但不能在同次判断中与自身混分。
                self._collect(vector, start, end, speech_ms, now)
        except Exception as exc:
            # 模型/资料评分异常与无有效分数一致，首段必须走未确认放行。
            permanent = temporary = None
            quality = "unavailable"
            audit("scoring_failed（评分异常）", turn_id=turn_id,
                  settings_revision=self.settings_revision, reason=f"{type(exc).__name__}: {str(exc)[:240]}")
        weight = self.config.session_weight if permanent is not None and temporary is not None else 0.0
        score = (1-weight)*permanent + weight*temporary if weight else permanent
        inherited = bool(turn.continuity_grant and
                         turn.continuity_grant["continuity_epoch"] == self.continuity.epoch)
        scope = ("intra_turn" if turn.owner_confirmed and not first else
                 "cross_turn" if inherited and turn.phase != "held" else "normal")
        threshold = (self.config.intra_turn_threshold if scope == "intra_turn" else
                     self.config.continuity_threshold if scope == "cross_turn" else self.config.confirm_threshold)
        scores = {"permanent_score": permanent, "session_score": temporary,
                  "combined_score": score, "effective_session_weight": weight,
                  "threshold_used": threshold, "threshold_scope": scope,
                  "identity_result": ("owner" if score >= threshold else "non_owner") if score is not None else None}
        was_recovered = turn.recovered
        previous_state = turn.state
        changed = turn.observe(scores, self.config, end=end, first=first)
        if (turn_id in self.candidates and previous_state in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}
                and turn.state == SpeakerState.NON_OWNER_CONFIRMED and turn.negative_ticks < self.config.non_owner_ticks):
            turn.observation_state = previous_state  # 独立打断保留既有短时波动容忍。
        turn.observation_count += 1
        turn.speech_ms += max(0, speech_ms)
        if score is not None and score >= self.config.confirm_threshold:
            turn.normal_owner_epoch = self.continuity.epoch
            self.last_owner_at, self._continuity = now, False  # 独立打断策略继续读取真实主人证据。
        elif turn.negative_ticks >= self.config.non_owner_ticks:
            if (turn.normal_owner_epoch is not None or self.continuity.normal_end_at is not None
                    or turn.continuity_grant):
                self.revoke_continuity("sustained_non_owner")
            turn.normal_owner_epoch, turn.continuity_grant = None, {}
        self.last_scores = scores
        if turn.recovered and not was_recovered:
            self.recovered_count += 1
        audit("inference_result（声纹推理结果）", turn_id=turn_id, generation=turn.generation,
              activation_id=self.activation_id, settings_revision=self.settings_revision, sequence=sequence,
              object_kind="barge" if turn_id in self.candidates else "input", first=first, start_sample=start, end_sample=end,
              speech_ms=speech_ms if window_speech_ms is None else window_speech_ms,
              new_speech_ms=speech_ms, quality=quality, latency_ms=latency_ms,
              confirm_threshold=self.config.confirm_threshold,
              non_owner_ticks=self.config.non_owner_ticks, session_weight=self.config.session_weight,
              intra_turn_relaxation=self.config.intra_turn_relaxation,
              continuity_relaxation=self.config.confirm_threshold-self.config.continuity_threshold,
              context_max_ms=self.config.context_max_ms, positive_ticks=turn.positive_ticks,
              negative_ticks=turn.negative_ticks, observation_count=turn.observation_count,
              turn_speech_ms=turn.speech_ms, **scores, **turn.summary())
        if changed:
            audit("admission_changed（轮次准入变化）", turn_id=turn_id,
                  settings_revision=self.settings_revision, **turn.summary())
        return turn.state

    def close_turn(self, turn_id: str, *, completed: bool = False, reason: str = "cancelled") -> None:
        turn = self.turns.get(turn_id)
        if turn is not None and not turn.closed:
            if completed and turn.boundary_at is not None:
                disposition = self.continuity.complete(turn, boundary_at=turn.boundary_at)
                audit("continuity_boundary（最终边界资格交接）", turn_id=turn_id, boundary_at=turn.boundary_at,
                      reason=turn.boundary_reason, disposition=disposition,
                      settings_revision=self.settings_revision, continuity=self.continuity.snapshot(self.config))
            elif not completed:
                self.revoke_continuity(reason)
            turn.closed = True
            turn.closed_at = time.monotonic()
            turn.phase = "closed"
            audit("turn_closed（声纹轮次结束）", turn_id=turn_id,
                  settings_revision=self.settings_revision, **turn.summary())
            self.pieces[:] = [piece for piece in self.pieces if piece.turn_id != turn_id]
            self._prune_closed_turns()

    def turn_summary(self, turn_id: str) -> dict:
        turn = self.turns.get(turn_id)
        return turn.summary() if turn else {}

    def clear(self) -> None:
        config, scorer, revision = self.config, self.scorer, self.settings_revision
        self.__init__(config, scorer, settings_revision=revision + 1)

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
        if turn_id in self.candidates:
            return self.observe_turn(turn_id, vector, end=end, start=start, speech_ms=speech_ms, now=now)
        if turn_id in self.turns:
            turn = self.turns[turn_id]
            return self.observe_turn(turn_id, vector, end=end, start=start, speech_ms=speech_ms,
                                     first=turn.first_pending, now=now)
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
        weight = 0.0
        if permanent is not None and temporary is not None:
            weight = self.config.session_weight
            score = (1 - weight) * permanent + weight * temporary
        self.last_scores = {"permanent_score": permanent, "session_score": temporary,
                            "combined_score": score, "effective_session_weight": weight}
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
        else:  # 所有有效低于当次主人阈值的分数均为非主人，不再保留分数灰区。
            self._negative[key] = self._negative.get(key, 0) + 1
            state = previous
            if self._negative[key] >= self.config.non_owner_ticks:
                state = SpeakerState.NON_OWNER_CONFIRMED
                if previous in {SpeakerState.OWNER_CONFIRMED, SpeakerState.OWNER_CONTINUITY}:
                    self._boundary[key] = end  # 只拦截此时之后的增量。
                self.last_owner_at = None
        self._states[key] = state
        self.last_scores["threshold_used"] = threshold
        self.last_scores["identity_result"] = ("owner" if score >= threshold else "non_owner") \
            if score is not None and math.isfinite(score) else None
        self.evidence.append((key, start, end, vector, state))
        if state == SpeakerState.NON_OWNER_CONFIRMED:
            for piece in self.pieces:
                if (piece.status == "pending" and (piece.turn_id,piece.generation) == key
                        and piece.embedding and 1-cosine(piece.embedding,vector) <= self.config.recovery_max_distance):
                    piece.status = "rejected"
        self._recover(key, vector, state, now)
        return state

    def state(self, turn_id: str, generation: int, *, now: float | None = None) -> SpeakerState:
        if turn_id in self.candidates:
            return self.candidates[turn_id].state
        if turn_id in self.turns:
            return self.turns[turn_id].state
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
        if turn_id in self.turns:
            return  # 普通拦截轮次由完整ASR修订更新，不按增量片段剪裁。
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
        if turn_id in self.turns:
            turn = self.turns[turn_id]
            return turn.text if turn.admitted else ""
        return "".join(p.text for p in self.pieces if p.turn_id == turn_id and p.status == "admitted"
                       and (generation is None or p.generation == generation))

    def admitted_turns(self) -> list[dict]:
        result = [{"turn_id": turn.turn_id, "text": turn.text, "generation": turn.generation,
                   "speaker_summary": turn.summary(), "occurred_at": time.time()}
                  for turn in self.turns.values() if turn.admitted and turn.text and not turn.closed]
        for turn_id in dict.fromkeys(p.turn_id for p in self.pieces):
            text = self.admitted_text(turn_id)
            if text:
                first = next(p for p in self.pieces if p.turn_id == turn_id)
                result.append({"turn_id": turn_id, "text": text, "generation": first.generation, "occurred_at": first.created_at+self._wall_offset})
        return result

    def fully_admitted(self, turn_id: str) -> bool:
        if turn_id in self.turns:
            return self.turns[turn_id].admitted
        pieces = [p for p in self.pieces if p.turn_id == turn_id]
        return bool(pieces) and all(p.status == "admitted" for p in pieces)

    def snapshot(self) -> dict:
        self.grace()
        return {"activation_id": self.activation_id, "settings_revision": self.settings_revision,
                "continuity": self.continuity.snapshot(self.config),
                "calibration_status": self.calibration_status, "calibration_speech_ms": self.calibration_speech_ms,
                "session_centroid_ready": bool(self.session_centroid), "session_weight": self.config.session_weight,
                "pending_count": sum(p.status in {"pending","mixed"} for p in self.pieces),
                "recovered_count": self.recovered_count, "timeout_count": self.timeout_count, **self.last_scores}
