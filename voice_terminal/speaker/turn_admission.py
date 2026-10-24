"""普通输入的首段身份与整轮准入；与主人打断观察状态分开。"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .contracts import SpeakerState

FIRST_AUDIO_MS = 1200  # 首段积累时长，复用三个400毫秒心跳时隙。
OWNER_RECOVERY_TICKS = 3  # 首段非主人后的连续明确主人结果次数。


@dataclass
class TurnAdmission:
    turn_id: str  # 逻辑语音轮次编号。
    start_sample: int  # 本轮实际语音起点。
    generation: int = 0  # 最近输入代次；同轮续说保留准入。
    filtering: bool = True  # 仅标记/独立打断时跟踪边界，但不拦截普通文字。
    continuity_grant: dict = field(default_factory=dict)  # 开口时锁定的跨轮资格，不继承全文通行。
    normal_owner_epoch: int | None = None  # 本轮正常主人证据的资格代次，放宽分数不能生成。
    boundary_revision: int = 0  # 候选闭段版本，续说撤销时递增。
    boundary_at: float | None = None  # 最终成立候选所对应的实际语音终点。
    boundary_reason: str = ""  # SmartTurn或兜底结束原因。
    phase: str = "collecting"  # 积累、推理、暂缓、放宽、未确认放行、结束。
    initial_identity: str = "unknown"  # 首段原始身份结论。
    admitted: bool = False  # 本轮获准后不可撤销。
    owner_confirmed: bool = False  # 本轮曾有明确主人证据。
    observation_state: SpeakerState = SpeakerState.UNKNOWN  # 当次身份观察，与整轮放行分开。
    recovered: bool = False  # 初判非主人已被推翻。
    foreign_voice_suspected: bool = False  # 仅供语义参考，不删除文字。
    positive_ticks: int = 0  # 连续明确主人新结果。
    negative_ticks: int = 0  # 连续明确非主人新结果。
    last_end: int = -1  # 已消费音频末端，重复结果不计数。
    observation_count: int = 0  # 本轮新音频判断次数，包含身份未知的结果。
    speech_ms: int = 0  # 本轮已观察的不重复有效人声毫秒。
    closed: bool = False  # 终态之后不再释放文字。
    closed_at: float | None = None  # 已关闭记录的单调时钟，用于有界诊断留存。
    text: str = ""  # 原始完整转写；不由片段准入拼接。
    text_revision: int = 0  # 完整转写修订号。
    scores: dict = field(default_factory=dict)  # 当次实际评分分量与参数。
    reason: str = "awaiting_first_audio"  # 判定原因。

    @property
    def first_pending(self) -> bool:
        return not self.closed and self.phase in {"collecting", "inferring"}

    @property
    def state(self) -> SpeakerState:
        if self.phase == "held" or (self.closed and not self.admitted and self.initial_identity == "non_owner"):
            return SpeakerState.NON_OWNER_CONFIRMED
        return self.observation_state

    def observe(self, scores: dict, config, *, end: int, first: bool = False) -> bool:
        if self.closed or (not first and end <= self.last_end):
            return False
        self.last_end = max(self.last_end, end)
        before = self.summary()
        self.scores = dict(scores)
        score = scores.get("combined_score")
        valid = isinstance(score, (int, float)) and math.isfinite(score)
        if first:
            if not self.first_pending:
                return False
            if valid and score >= scores["threshold_used"]:
                self.initial_identity = "owner"
                self.admitted = self.owner_confirmed = True
                self.observation_state = (SpeakerState.OWNER_CONFIRMED if score >= config.confirm_threshold
                                          else SpeakerState.OWNER_CONTINUITY)
                self.phase, self.reason = "relaxed", "first_owner_confirmed"
            elif valid:
                self.initial_identity = "non_owner"
                self.observation_state = SpeakerState.NON_OWNER_CONFIRMED
                self.phase, self.reason = "held", "first_non_owner_held"
            else:
                self.admitted = True
                self.phase, self.reason = "unconfirmed", "first_identity_uncertain"
        elif valid:
            # 待恢复仍用正常阈值；已有主人证据后，放宽量实际参与当前身份观察。
            threshold = scores["threshold_used"]
            owner_evidence = score >= threshold
            self.positive_ticks = self.positive_ticks + 1 if owner_evidence else 0
            self.negative_ticks = self.negative_ticks + 1 if not owner_evidence else 0
            if self.phase == "held" and self.positive_ticks >= OWNER_RECOVERY_TICKS:
                self.admitted = self.owner_confirmed = self.recovered = True
                self.observation_state = SpeakerState.OWNER_CONFIRMED
                self.foreign_voice_suspected = True
                self.phase, self.reason = "relaxed", "initial_non_owner_overturned"
            elif self.admitted:
                self.observation_state = (SpeakerState.OWNER_CONFIRMED if score >= config.confirm_threshold
                    else SpeakerState.OWNER_CONTINUITY if owner_evidence else SpeakerState.NON_OWNER_CONFIRMED)
                if owner_evidence:
                    self.owner_confirmed = True
                    self.phase = "relaxed"
                if self.negative_ticks >= config.non_owner_ticks:
                    self.observation_state = SpeakerState.NON_OWNER_CONFIRMED
                    self.foreign_voice_suspected = True
                    self.reason = "non_owner_observed_after_admission"
        return self.summary() != before

    def summary(self) -> dict:
        return {"admission_phase": self.phase, "initial_identity": self.initial_identity,
                "admitted": self.admitted, "owner_confirmed": self.owner_confirmed,
                "observation_state": self.observation_state.value,
                "recovered": self.recovered, "foreign_voice_suspected": self.foreign_voice_suspected,
                "reason": self.reason, "closed": self.closed,
                "continuity_source": self.continuity_grant.get("source_turn_id"),
                "boundary_revision": self.boundary_revision, "boundary_reason": self.boundary_reason,
                "interpretation": "声纹仅供身份参考；疑似异声也可能是识别波动，具体文字归属未确定，完整转写继续有效。"}
