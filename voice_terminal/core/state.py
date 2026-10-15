from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TerminalState(StrEnum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    FINALIZING = "FINALIZING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    ERROR = "ERROR"


SERVICE_READY_STATES = {"READY", "RUNNING", "STREAMING", "CONNECTED"}
# SERVICE_LOADING_STATES（加载中状态）：瞬态而非故障。界面按“启动中”呈现并暂时拦截主监听，
# 宿主也不据此判定核心能力失败；只有终态失败才进入重试入口。
SERVICE_LOADING_STATES = {"STARTING", "LOADING", "WAITING", "CONNECTING", "RETRYING"}
# BLOCKING_SERVICES（阻塞能力）：任一不就绪都不能开始语音会话。
BLOCKING_SERVICES = ("audio", "asr", "llm", "tts")
# KEYWORD_SERVICE（唤醒能力）：停用放行；唤醒常开时沿用既有 ERROR 放行规则。
KEYWORD_SERVICE = "kws"
# SERVICE_LABELS（能力中文名称）：宿主探测与语音运行时共用一份口径。
SERVICE_LABELS = {
    "audio": "音频设备", "kws": "KWS（关键词唤醒）", "asr": "ASR（语音识别）",
    "llm": "LLM（大语言模型）", "tts": "TTS（语音合成）", "speaker": "声纹工作进程",
    "dsh": "DSH（代理执行宿主）", "shadow_agent": "影子代理", "web_search": "网络搜索",
}


@dataclass
class RuntimeState:
    state: TerminalState = TerminalState.STARTING
    listening: bool = False
    audio_generation: int = 0  # audio_generation（音频轮代）：状态变化后拒绝旧音量帧。
    wake_always_on: bool = False
    input_owner: str = "none"
    agent: dict[str, Any] = field(default_factory=dict)
    services: dict[str, str] = field(
        default_factory=lambda: {
            "audio": "OFFLINE",
            "kws": "OFFLINE",
            "asr": "OFFLINE",
            "speaker": "OFFLINE",
            "llm": "OFFLINE",
            "tts": "OFFLINE",
            "web_search": "OFFLINE",
            "shadow_agent": "OFFLINE",
        }
    )
    current: dict[str, Any] = field(
        default_factory=lambda: {
            "partial_text": "",
            "final_text": "",
            "assistant_text": "",
            "tts_text": "",
            "tts_status": "IDLE",
            "audio_buffer_ms": 0,
            "tts_returned_ms": 0,
            "tts_played_ms": 0,
            "tts_output_estimated_ms": 0,
            "tts_remaining_ms": 0,
            "turn_id": None,
            "input_runtime": True,
            # Runtime capabilities are kept in the snapshot so a reconnecting
            # client can explain why protected barge-in is unavailable and
            # which TTS scheduling path is active.
            "audio_processing_status": "DISABLED",
            "audio_processing_error": "",
            "tts_transport": "websocket",
            "tts_scheduling": "auto",
            "delivery": {},
            "refinement_available": False,
            "tts_prewarm_started": False,
            "tts_prewarm_status": "NOT_REQUESTED",
        }
    )
    latency: dict[str, Any] = field(
        default_factory=lambda: {
            "complete": False,
            "values": {
                "wake_detection_ms": None,
                "asr_first_text_ms": None,
                "speech_end_confirmation_ms": None,
                "llm_first_token_ms": None,
                "llm_request_to_first_tts_text_ms": None,
                "tts_text_to_first_audio_ms": None,
                "first_audio_to_playback_ms": None,
                "speech_end_to_playback_ms": None,
                "asr_final_to_commit_ms": None,
                "refine_duration_ms": None,
                "refine_to_redecision_ms": None,
                "agent_request_to_speak_ms": None,
                "tts_prewarm_requested_ms": None,
                "tts_prewarm_ready_ms": None,
                "tts_checkout_ms": None,
                "tts_cold_start_ms": None,
                "speech_end_to_first_pcm_ms": None,
                "first_pcm_to_player_submit_ms": None,
                "replacement_count": 0,
                "turn_total_ms": None,
                "first_segment_ms": None,
                "smart_turn_ms": None,
                "sensevoice_refine_ms": None,
                "commit_to_final_llm_ms": None,
            },
        }
    )

    def _service_unavailable(self, name: str) -> bool:
        """_service_unavailable（能力缺口）：沿用既有唤醒特例，其余以就绪集合判定。"""
        status = self.services.get(name)
        if status in SERVICE_READY_STATES:
            return False
        if name == KEYWORD_SERVICE and (status == "DISABLED"
                                        or (self.wake_always_on and status == "ERROR")):
            return False
        return True

    @property
    def blocking_unavailable(self) -> tuple[str, ...]:
        """blocking_unavailable（阻塞能力缺口）：未就绪的阻塞能力与唤醒能力。"""
        return tuple(name for name in (*BLOCKING_SERVICES, KEYWORD_SERVICE)
                     if self._service_unavailable(name))

    @property
    def can_start_reason(self) -> dict[str, Any] | None:
        """can_start_reason（不能开始监听的原因）：区分“加载中”与“已失败”。

        加载中是瞬态：界面显示“启动中”并暂时拦截主监听，等能力转就绪后可直接开始，
        不进入报错或重试页面。已失败才需要用户手动恢复对应组件。
        """
        if self.listening:
            return {"code": "SESSION_ACTIVE", "services": [], "loading": [],
                    "message": "语音会话已在进行中。"}
        if self.state == TerminalState.ERROR:
            return {"code": "RUNTIME_ERROR", "services": [], "loading": [],
                    "message": "语音运行时处于错误状态。"}
        missing = self.blocking_unavailable
        if not missing:
            return None
        loading = [name for name in missing if self.services.get(name) in SERVICE_LOADING_STATES]
        code = "CAPABILITY_LOADING" if len(loading) == len(missing) else "CAPABILITY_FAILED"
        return {
            "code": code,
            "services": list(missing),  # services（缺口能力标识）。
            "loading": loading,  # loading（其中仍在加载的能力标识）。
            "message": "、".join(SERVICE_LABELS.get(name, name) for name in missing),
        }

    @property
    def can_start(self) -> bool:
        return self.can_start_reason is None

    def set_service(self, name: str, status: str) -> None:
        if name not in self.services:
            raise KeyError(f"unknown service: {name}")
        self.services[name] = status

    def set_state(self, state: TerminalState, *, listening: bool | None = None) -> None:
        if state != self.state or (listening is not None and listening != self.listening):
            self.audio_generation += 1
        self.state = state
        if listening is not None:
            self.listening = listening

    def clear_current(self) -> None:
        self.current.update(
            partial_text="",
            final_text="",
            assistant_text="",
            tts_text="",
            tts_status="IDLE",
            audio_buffer_ms=0,
            tts_returned_ms=0,
            tts_played_ms=0,
            tts_output_estimated_ms=0,
            tts_remaining_ms=0,
            delivery={},
        )
        for key in ("turn_id", "output_generation", "phase", "committed", "agent_phase", "paraformer_final", "refined_text", "refinement_available", "segments", "tools", "audio_cues", "analysis", "tts_segments", "speaker_decision"):
            self.current.pop(key, None)
        self.current.update(tts_prewarm_started=False, tts_prewarm_status="NOT_REQUESTED")
        self.latency = {
            "complete": False,
            "values": {key: None for key in self.latency["values"]},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "agent": dict(self.agent),
            "state": self.state.value,
            "listening": self.listening,
            "audio_generation": self.audio_generation,
            "can_start": self.can_start,
            "can_start_reason": self.can_start_reason,
            "wake_always_on": self.wake_always_on,
            "input_owner": self.input_owner,
            "services": dict(self.services),
            "current": dict(self.current),
            "latency": {
                "complete": self.latency["complete"],
                "values": dict(self.latency["values"]),
            },
        }
