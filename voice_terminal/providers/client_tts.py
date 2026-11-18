"""Client-aware MiniMax TTS（由服务端授权客户端原生直接合成）。"""
from __future__ import annotations

from typing import Any

from ..devices.client_tts import RemoteClientTtsSession
from ..tts.capabilities import TtsCapabilities


class ClientAwareMiniMaxTtsClient:
    """ClientAwareMiniMaxTtsClient（服务端权威持有 MiniMax 凭据并授权客户端直放）。"""

    supports_speech_controls = True  # supports_speech_controls（原生支持语速、音量与情绪）。

    def __init__(self, *, runtime: Any, fallback: Any | None, config: Any) -> None:
        self.runtime = runtime  # runtime（权威核心与活动设备注册表）。
        self.fallback = fallback  # fallback（主机自身 MiniMax 合成后端，可为空）。
        self.config = config  # config（固定模型、音色和采样率配置）。

    def __getattr__(self, name: str) -> Any:
        fallback = object.__getattribute__(self, "fallback")
        if fallback is None:
            raise AttributeError(name)
        return getattr(fallback, name)

    @property
    def transport(self) -> str:
        """transport（当前活跃设备对应的 TTS 传输）。"""
        return "websocket_bidi" if self.runtime.active_remote_client_tts_outbox() else getattr(
            self.fallback, "transport", "websocket",
        )

    @property
    def scheduling(self) -> str:
        """scheduling（当前活跃设备对应的文本调度策略）。"""
        return "direct" if self.runtime.active_remote_client_tts_outbox() else getattr(
            self.fallback, "scheduling", "auto",
        )

    @property
    def effective_scheduling(self) -> str:
        """effective_scheduling（实际文本到音频调度策略）。"""
        return "direct" if self.runtime.active_remote_client_tts_outbox() else getattr(
            self.fallback, "effective_scheduling", self.scheduling,
        )

    @property
    def capabilities(self) -> TtsCapabilities:
        """capabilities（当前 provider 的正式能力快照）。"""
        if self.runtime.active_remote_client_tts_outbox():
            return TtsCapabilities(cancellation=True)
        value = getattr(self.fallback, "capabilities", None)
        return value if isinstance(value, TtsCapabilities) else TtsCapabilities()

    @property
    def sample_rate(self) -> int:
        """sample_rate（在线 TTS PCM 输出采样率）。"""
        return int(getattr(self.config, "online_sample_rate", None) or self.config.sample_rate)

    @property
    def playback_startup_buffer_ms(self) -> int:
        """playback_startup_buffer_ms（在线首音直通时不额外储备）。"""
        if self.runtime.active_remote_client_tts_outbox():
            return 0
        return int(getattr(self.fallback, "playback_startup_buffer_ms", 0) or 0)

    @property
    def playback_resume_buffer_ms(self) -> int:
        """playback_resume_buffer_ms（在线首音直通时不额外储备）。"""
        if self.runtime.active_remote_client_tts_outbox():
            return 0
        return int(getattr(self.fallback, "playback_resume_buffer_ms", 0) or 0)

    @property
    def vad_prewarm(self) -> bool:
        """vad_prewarm（客户端在线合成不执行主机本地预热）。"""
        return False if self.runtime.active_remote_client_tts_outbox() else bool(
            getattr(self.fallback, "vad_prewarm", False),
        )

    def bind_speech_client(self):
        """bind_speech_client（为新语音事务冻结 provider 与交互设备租约）。"""
        active = self.runtime.active_remote_client_tts_outbox()
        if active is None:
            if self.fallback is None:
                raise RuntimeError("当前没有在线客户端，且没有服务端 MiniMax 凭据。")
            return self.fallback
        lease, outbox = active
        credential = self._server_credential()
        if not credential:
            raise RuntimeError("服务端没有 MiniMax 凭据，不能授权客户端直放。")
        return _BoundClientTtsClient(
            runtime=self.runtime, lease=lease, outbox=outbox, config=self.config,
            credential=credential,
        )

    def _server_credential(self) -> str:
        """_server_credential（读取服务端运行时凭据）：仅在活跃远端语音事务开始时调用。"""
        getter = getattr(self.runtime, "_api_key", None)
        if callable(getter):
            value = getter(self.config.api_key_env)
            return value if isinstance(value, str) else ""
        client = getattr(self.fallback, "client", None)
        value = getattr(client, "api_key", "")
        return value if isinstance(value, str) else ""

    async def close(self) -> None:
        """close（关闭 provider）：事务会话由原语音所有者单独收尾。"""
        if self.fallback is not None:
            await self.fallback.close()


class _BoundClientTtsClient:
    """_BoundClientTtsClient（绑定单个媒体租约的在线 MiniMax 客户端）。"""

    uses_client_tts = True  # uses_client_tts（必须将播放键传入客户端合成会话）。
    supports_speech_controls = True  # supports_speech_controls（支持原生控制值）。
    transport = "websocket_bidi"  # transport（客户端 MiniMax 双向合成通道）。
    scheduling = "direct"  # scheduling（把受授权文本直接送到客户端播放器事务）。
    effective_scheduling = "direct"  # effective_scheduling（实际客户端原生直放策略）。
    direct_playback = True  # direct_playback（PCM 只在客户端合成并进入设备播放器）。
    playback_startup_buffer_ms = 0  # playback_startup_buffer_ms（无额外起播储备）。
    playback_resume_buffer_ms = 0  # playback_resume_buffer_ms（无额外恢复储备）。
    sentence_prefetch_ms = 3000  # sentence_prefetch_ms（短句调度配置兼容字段）。
    sentence_tick_ms = 50  # sentence_tick_ms（短句调度配置兼容字段）。
    sentence_min_effective_chars = 10  # sentence_min_effective_chars（短句有效字符数兼容字段）。
    first_clause_timeout_ms = 400  # first_clause_timeout_ms（首句调度兼容字段）。
    capabilities = TtsCapabilities(cancellation=True)  # capabilities（客户端取消由媒体回执确认）。

    def __init__(self, *, runtime: Any, lease: Any, outbox: Any, config: Any, credential: str) -> None:
        self.runtime = runtime  # runtime（核心播放事务绑定入口）。
        self.lease = lease  # lease（固定连接、执行器与语音占用代次）。
        self.outbox = outbox  # outbox（固定媒体租约的双向合成队列）。
        self.config = config  # config（provider 固定参数）。
        self._credential = credential  # _credential（服务端供应商凭据，只用于临时授权消息）。
        self.sample_rate = int(getattr(config, "online_sample_rate", None) or config.sample_rate)

    async def prepare_speech_controls(self, controls, *, detach_active: bool = False) -> None:
        """prepare_speech_controls（记录由 ControlledTtsSession 固定的参数快照）。"""
        del controls, detach_active

    async def start_session_with_retry(self, session):
        """start_session_with_retry（确认当前固定租约仍可用于播放）。"""
        await session.start()
        return session

    def session(
        self,
        *,
        fresh: bool = False,
        turn_id: str | None = None,
        speech_id: str | None = None,
        playback_key: str,
        emotion: str | None = None,
        controls=None,
    ) -> RemoteClientTtsSession:
        """session（创建绑定播放事务的远端合成会话）。"""
        del fresh, turn_id, speech_id
        guidance = getattr(controls, "guidance", None) if controls is not None else None
        selected_emotion = guidance if guidance in {
            "calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
        } else emotion or getattr(self.config, "default_emotion", "calm")
        if selected_emotion not in {
            "calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised",
        }:
            selected_emotion = None
        speed = float(getattr(controls, "speed", 1.0))
        volume = float(getattr(controls, "volume", 1.0))
        credential = self._credential
        self._credential = ""
        return RemoteClientTtsSession(
            runtime=self.runtime, lease=self.lease, outbox=self.outbox,
            playback_key=playback_key, credential=credential,
            model=self.config.model, voice_id=self.config.voice_id,
            sample_rate=self.sample_rate, emotion=selected_emotion, speed=speed, volume=volume,
            controls=controls,
        )
