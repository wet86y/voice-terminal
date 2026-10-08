from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
import json
import logging
import time
import unicodedata
from threading import Lock
from uuid import uuid4

from .scheduler import AgentDecisionScheduler
from .streamed_speech import StreamedSpeechAdmissionState, queue_streamed_speech
from .decision_application import ProtocolError, apply_decision_v06
from .context_builder import AgentContextMixin
from .deferred_speech import DeferredSpeechMixin
from .runtime_events import RecoverySignal, HeartbeatPulse, EpisodeHeartbeatClock
from .prompts import (
    PROMPT, DEEP_THINKING_PROMPT,
    PERSONA_DOCUMENT_POLICY, SECONDARY_MAILBOX_PROMPT,
    FINAL_SILENCE_PROMPT, KWS_WAKE_PROMPT,
)
from .options import AgentOptions
from .context_audit import capture_context_snapshot, capture_request_outcome
from .decision_envelope import (
    Action, DecisionEnvelope, EnvelopeError, SpeechDecisionEnvelopeStream,
    envelope_error_diagnostics, json_structure_fingerprint,
    parse_compatible_decision_envelope, provider_schema_diagnostics,
)
from .persona import PersonaDocumentLoader, PersonaDocuments
from .sentences import map_refinement
from ..session.context import (
    CURRENT_USER_INPUT_TYPE, FINAL_OUTPUT_CONSTRAINT, OUTPUT_CONSTRAINT_KIND,
    VOICE_REQUEST_CONTEXT_TYPE, CurrentInput, LLMContextProjector,
    consolidate_request_tail,
)
from ..session.context_frames import ContextFrame, ContextLane
from ..session.runtime import ConversationRuntime
from ..shadow_agent.contracts import DurableVoiceEvent, ShadowEvent, parse_shadow_wake


logger = logging.getLogger(__name__)


# ``tiktoken.get_encoding`` may populate an encoding cache on its first call.
# That can synchronously read/download data and therefore must never execute
# on the realtime event-loop path.  Until the background warm-up finishes the
# context guard uses the conservative character-based estimate below.
_O200K_ENCODING = None
_O200K_STATE = "uninitialized"
_O200K_LOCK = Lock()


def _fallback_token_estimate(serialized: str) -> int:
    ascii_chars = sum(char.isascii() for char in serialized)
    return max(0, round(ascii_chars / 4 + (len(serialized) - ascii_chars) * 1.1))


def _load_o200k_encoding() -> None:
    """Load the optional exact tokenizer off the realtime event loop."""

    global _O200K_ENCODING, _O200K_STATE
    try:
        import tiktoken  # type: ignore
        encoding = tiktoken.get_encoding("o200k_base")
    except Exception as exc:
        logger.warning("agent.tokenizer_prewarm_failed error=%s", str(exc)[:200])
        with _O200K_LOCK:
            _O200K_STATE = "failed"
        return
    with _O200K_LOCK:
        _O200K_ENCODING = encoding
        _O200K_STATE = "ready"
    logger.info("agent.tokenizer_prewarm_ready encoding=o200k_base")








def _heard_delivery(value) -> dict | None:
    """Sanitize one played-and-heard delivery fact for the request tail.

    Only the audible text and its settlement kind cross the model boundary;
    speech, request and turn identifiers are execution-layer data and must
    never reach the provider (see the context cache contract).
    """

    if not isinstance(value, dict):
        return None
    heard = str(value.get("heard_text") or "").strip()
    if not heard:
        return None
    fact = {"heard_text": heard}
    status = str(value.get("status") or "").strip()
    if status:
        fact["status"] = status
    termination = str(value.get("termination") or "").strip()
    if termination:
        fact["termination"] = termination
    return fact


def semantic_text(text: str) -> str:
    """Return a comparison form for ASR refinements.

    ASR engines commonly disagree about spaces and sentence punctuation in
    Chinese.  Those display-only changes should not wake the agent.  Removing
    every punctuation code point, however, turns values such as ``-1.5`` into
    ``15`` and corrupts paths, URLs and identifiers.  Keep punctuation when it
    is attached to an ASCII token, and only discard the small set of ordinary
    sentence separators when they sit between non-ASCII prose characters.
    """

    def ascii_token(value: str) -> bool:
        return bool(value) and value.isascii() and value.isalnum()

    sentence_marks = set("，。！？；、,!?;.")
    result: list[str] = []
    for index, char in enumerate(text):
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        if char.isspace():
            # Spaces inside an ASCII name/path remain meaningful; spaces used
            # to separate Chinese words remain display-only.
            if ascii_token(previous) and ascii_token(following):
                result.append(char)
            continue
        if unicodedata.category(char).startswith("P") and char in sentence_marks:
            if not (ascii_token(previous) or ascii_token(following)):
                continue
        result.append(char)
    return "".join(result)




class AgentEpisodeRuntime(DeferredSpeechMixin, AgentContextMixin):
    # Tool adapters already bound the provider response, but a search can
    # still contain several thousand characters of snippets and links.  Keep
    # the model-facing protocol bounded without changing the cached result or
    # the trace payload used for diagnostics.
    CONTEXT_TOOL_RESULT_CHARS = 4096
    CONTEXT_ASSISTANT_CONTENT_CHARS = 4000

    def __init__(self, provider, registry, output, emit, *, conversation: ConversationRuntime, ledger,
                 options=None, tools_enabled=True, reasoning_mode="fast", persona_config=None,
                 custom_instructions_provider=None,
                 shadow_waker=None, on_final_system_check_settled=None,
                 can_dispatch_final_silence=None,
                 defer_silence_until_output=False, continuity_document_provider=None,
                 on_user_activity=None, on_silence_anchor=None,
                 on_speaker_heartbeat=None, admit_user_turn=None):
        self.provider, self.registry, self.output, self.emit = provider, registry, output, emit
        self.quiet_policy = None
        self.deferred_capture = None
        self.deferred_append = None
        self._deferred_outputs = []
        self._deferred_seen = set()
        self.options = options or AgentOptions()
        self.tools_enabled = tools_enabled
        if reasoning_mode not in {"fast", "deep"}:
            raise ValueError("reasoning_mode（运行模式）必须是 fast（快速）或 deep（深度）")
        self.reasoning_mode = reasoning_mode  # reasoning_mode（运行模式）：本轮冻结的快速或深度选择。
        self.conversation = conversation
        self.ledger = ledger
        self.shadow_waker = shadow_waker
        self.on_final_system_check_settled = on_final_system_check_settled
        self.can_dispatch_final_silence = can_dispatch_final_silence
        self.continuity_document_provider = continuity_document_provider
        self.on_user_activity = on_user_activity
        self.on_silence_anchor = on_silence_anchor
        self.on_speaker_heartbeat = on_speaker_heartbeat
        self.admit_user_turn = admit_user_turn
        # This is enabled only for the resident-KWS product mode.  Ordinary
        # sessions retain the historical behavior where the finalized input
        # itself arms the silence window.
        self.defer_silence_until_output = bool(defer_silence_until_output)
        self.context_projector = LLMContextProjector()
        self.persona_loader = PersonaDocumentLoader(persona_config) if persona_config is not None else None
        self.persona_documents = PersonaDocuments()
        self.custom_instructions_provider = custom_instructions_provider
        self.custom_instructions = []
        self.scheduler = AgentDecisionScheduler(self.options)
        self.episode_id = None
        self.agent_step_id = None
        self.user_speaking = None
        self.state = "IDLE"
        self.current_action = None
        # Set only while one completed model envelope is being applied.  It
        # keeps the public/private apply method signature stable for adapters
        # and tests while narrowing completed-speech continuation to an ASR
        # reassessment request.
        self._applying_transcript_reassessment = False
        self.inputs = {}
        self.trace = deque(maxlen=self.options.trace_limit)
        self.outputs = deque(maxlen=self.options.output_limit)
        self._last_model_speak_snapshot = {}  # 原始输出边界快照，仅本地确认使用。
        self._applying_speech_admission = None  # 当前请求的播报受理状态。
        self._last_model_speak_state = {
            "state": "empty", "owner_scope": "none", "continuation_mode": "none",
        }
        self._completed_speech_ids: set[str] = set()
        self.pending = set()
        self._secondary_enqueued = 0  # 登记的实质进度变化数。
        self._secondary_consumed_normal = 0  # 普通请求消费的来源数。
        self._secondary_consumed_check = 0  # 二等评估消费的来源数。
        self._secondary_cleared_final = 0  # 最终静默检查清理的来源数。
        self._secondary_dispatched = 0  # 二等评估派发次数。
        self._secondary_skipped_speaking = 0  # 因用户讲话跳过的检查次数。
        self._secondary_skipped_busy = 0  # 因忙碌或退避跳过的检查次数。
        self._scheduled_wakes: list[dict] = []
        self._pending_since: float | None = None
        self._pending_transcript_turn_id: str | None = None
        self.event_inbox = deque(maxlen=128)
        self.queue = asyncio.Queue(maxsize=1)
        self.heartbeat_queue = asyncio.Queue(maxsize=1)
        self._heartbeat_clock: EpisodeHeartbeatClock | None = None
        self._heartbeat_episode_generation = 0
        self._heartbeat_generated = 0
        self._heartbeat_delivered = 0
        self._heartbeat_coalesced = 0
        self.runner = None
        self.last_reasoning = None
        self.delay_task = None
        self.input_scope_closed = False
        self.recovery_queue: deque[RecoverySignal] = deque()
        self.deferred_recovery_feedback: deque[RecoverySignal] = deque(maxlen=4)
        self.minor_protocol_feedback: deque[dict[str, str]] = deque(maxlen=8)
        self.active_recovery: RecoverySignal | None = None
        self.last_llm_completed_at: float | None = None
        self.scheduler_last_dispatch_at: float | None = None
        self.backoff_until = 0.0
        self.backoff_step = 0
        self.voice_activation_task = None
        self.active_request = None
        self.active_request_kinds: set[str] = set()
        self.active_request_identity = None
        self.active_request_source_turn_id = None
        self.active_dispatch_context: dict | None = None
        # Advances only when a new microphone speech interval begins.  The
        # scheduler generation intentionally waits for finalized/refined ASR,
        # but an unplayed system-check answer must become stale earlier: it
        # must never be released behind a newer user utterance merely because
        # that utterance's ASR final has not arrived yet.
        self._user_speech_epoch = 0
        self._system_check_turn_ids: set[str] = set()
        self._system_check_output_turn_ids: set[str] = set()
        self._system_check_speeches: dict[str, dict] = {}
        self._immediate_system_check: dict | None = None
        self._final_system_checks: dict[str, dict] = {}
        self._settled_final_system_checks: set[str] = set()
        self._pending_silence_anchor: dict | None = None
        self._speech_transactions_by_request: dict[str, set[str]] = {}
        self.closed = False
        self.request_count = 0
        self.active_request_count = 0
        self.max_request_concurrency = 0
        self.request_started_at = None
        self.last_turn_id = None
        # A model step can finish after a newer input has become the live
        # turn.  Keep the source turn alongside the step identity so late
        # traces cannot inherit ``last_turn_id`` from that newer input.
        self._identity_turns: dict[tuple, str | None] = {}
        self.input_finished_at: dict[str, float] = {}
        self._trace_seq = 0
        self.context_limited = False
        self.context_limit_reason = None
        self.context_compacted = False
        self.context_compaction = {}
        self.context_chars = 0
        self.context_token_estimate = 0
        self.context_usage_percent = 0.0
        self.provider_prompt_tokens = None
        self.provider_input_tokens = None
        self.provider_cached_tokens = None
        self.provider_cache_miss_tokens = None
        self.provider_cache_hit_ratio = None
        self.static_context_digest = None
        self.history_context_digest = None
        self.schema_digest = None
        self._history_prefix: list[dict] = []
        self._background_tools: set[asyncio.Task] = set()
        self._tokenizer_prewarm_task: asyncio.Task | None = None
        self._recovery_attempts_by_turn: dict[str, int] = {}
        self._transport_retry_attempts_by_turn: dict[str, int] = {}
        self._retry_shadow_information_by_turn: dict[str, dict] = {}
        self._retry_secondary_by_turn: dict[str, dict] = {}  # 原逻辑请求冻结的二等进度。
        self._retry_system_check: dict | None = None  # 二等评估传输重试的原始场景和所有权。
        self._frozen_action_definitions: tuple[dict, ...] | None = None
        self._frozen_tool_catalog: tuple[dict, ...] | None = None

    def _ensure_runner(self) -> None:
        if self.closed or self.episode_id is None:
            return
        if self._heartbeat_clock is None:
            clock = self._start_heartbeat_clock_sync()
            if clock is not None:
                asyncio.create_task(self.log(
                    "SCHEDULER_HEARTBEAT_THREAD_STARTED",
                    episode_generation=clock.episode_generation,
                    interval_ms=int(round(self.scheduler.interval * 1000)),
                ))
        if self.runner is None or self.runner.done():
            self.runner = asyncio.create_task(self._run(), name="agent-episode")

    def _receive_heartbeat_pulse(self, pulse: HeartbeatPulse) -> None:
        """Publish one clock pulse into the loop-owned capacity-one mailbox."""

        if (
            self.closed or self.episode_id is None
            or pulse.episode_generation != self._heartbeat_episode_generation
        ):
            try:
                asyncio.get_running_loop().create_task(self.log(
                    "SCHEDULER_HEARTBEAT_STALE",
                    episode_generation=pulse.episode_generation,
                    pulse_sequence=pulse.sequence,
                ))
            except RuntimeError:
                pass
            return
        # The optional speaker sampler is a one-shot child of this clock,
        # never another periodic scheduler or a blocker of the Agent mailbox.
        if callable(self.on_speaker_heartbeat):
            try:
                self.on_speaker_heartbeat(pulse)
            except Exception:
                logger.exception("speaker.heartbeat_schedule_failed")
        self._heartbeat_generated = max(self._heartbeat_generated, pulse.sequence)
        coalesced = pulse.coalesced_count
        if self.heartbeat_queue.full():
            try:
                previous = self.heartbeat_queue.get_nowait()
            except asyncio.QueueEmpty:
                previous = None
            if isinstance(previous, HeartbeatPulse):
                coalesced += previous.coalesced_count + 1
        delivered = HeartbeatPulse(
            episode_generation=pulse.episode_generation,
            sequence=pulse.sequence,
            scheduled_at=pulse.scheduled_at,
            coalesced_count=coalesced,
        )
        self.heartbeat_queue.put_nowait(delivered)
        self._ensure_runner()

    def _start_heartbeat_clock_sync(self):
        if self._heartbeat_clock is not None:
            return None
        self._heartbeat_episode_generation += 1
        generation = self._heartbeat_episode_generation
        loop = asyncio.get_running_loop()
        self.scheduler.start_heartbeat(time.monotonic())
        clock = EpisodeHeartbeatClock(
            interval=self.scheduler.interval,
            episode_generation=generation,
            loop=loop,
            callback=self._receive_heartbeat_pulse,
        )
        self._heartbeat_clock = clock
        try:
            clock.start()
        except Exception as exc:
            self._heartbeat_clock = None
            logger.exception("agent.heartbeat_thread_start_failed")
            asyncio.create_task(self.log(
                "SCHEDULER_HEARTBEAT_THREAD_ERROR",
                episode_generation=generation,
                phase="start",
                error_type=type(exc).__name__,
            ))
            return None
        return clock

    async def _start_heartbeat_clock(self) -> None:
        clock = self._start_heartbeat_clock_sync()
        if clock is None:
            return
        await self.log(
            "SCHEDULER_HEARTBEAT_THREAD_STARTED",
            episode_generation=clock.episode_generation,
            interval_ms=int(round(self.scheduler.interval * 1000)),
        )

    async def _stop_heartbeat_clock(self) -> None:
        clock = self._heartbeat_clock
        if clock is None:
            return
        self._heartbeat_clock = None
        clock.stop()
        await asyncio.to_thread(clock.join)
        while not self.heartbeat_queue.empty():
            try:
                self.heartbeat_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await self.log(
            "SCHEDULER_HEARTBEAT_THREAD_STOPPED",
            episode_generation=clock.episode_generation,
            thread_alive=clock.alive,
        )
        if clock.alive:
            await self.log(
                "SCHEDULER_HEARTBEAT_THREAD_ERROR",
                episode_generation=clock.episode_generation,
                phase="stop",
                error_type="join_timeout",
            )

    def snapshot(self, *, include_history=True):
        value = {"conversation_id": self.episode_id, "trace_id": self.agent_step_id,
                "request_id": self.agent_step_id,
                "agent_busy": self.state not in {"IDLE", "WAITING_EVENT"},
                "user_speaking": self.user_speaking,
                 "current_action": self.current_action, "request_count": self.request_count,
                 "max_request_concurrency": self.max_request_concurrency,
                 "playback_state": self.output.status,
                 "active_speech": getattr(self.output, "active_snapshot", None),
                 "speech_queue": getattr(self.output, "queue_snapshot", []),
                 "speak_snapshot": getattr(self.output, "speak_snapshot", {"queue_status": "empty", "text_only_queue": True}),
                 "recovery_pending": len(self.recovery_queue),
                 "reasoning": self.last_reasoning,
                  "reasoning_mode": self.reasoning_mode,
                  "scheduler": self.scheduler.snapshot(),
                  "secondary_mailbox": {  # 二等信箱诊断，不进入模型语义历史。
                      "pending_sources": sorted(self._secondary_pending),  # 待评估来源。
                      "pending_count": len(self._secondary_pending),  # 待评估来源数。
                      "latest_version": self.ledger._shadow_progress_version,  # 最新本地版本。
                      "enqueued": self._secondary_enqueued,  # 已登记变化数。
                      "consumed_normal": self._secondary_consumed_normal,  # 普通请求消费数。
                      "consumed_check": self._secondary_consumed_check,  # 二等请求消费数。
                      "cleared_final": self._secondary_cleared_final,  # 最终静默清理数。
                      "dispatched": self._secondary_dispatched,  # 二等派发次数。
                      "skipped_speaking": self._secondary_skipped_speaking,  # 讲话跳过次数。
                      "skipped_busy": self._secondary_skipped_busy,  # 忙碌跳过次数。
                  },
                  "heartbeat": {
                      "episode_generation": self._heartbeat_episode_generation,
                      "generated": self._heartbeat_generated,
                      "delivered": self._heartbeat_delivered,
                      "coalesced": self._heartbeat_coalesced,
                      "timer_thread_alive": bool(
                          self._heartbeat_clock and self._heartbeat_clock.alive
                      ),
                  }}
        value.update(context_limited=self.context_limited,
                    context_limit_reason=self.context_limit_reason,
                    context_compacted=self.context_compacted,
                    context_compaction=dict(self.context_compaction),
                    context_chars=self.context_chars,
                    context_limit_tokens=self.options.max_context_tokens,
                    context_token_estimate=self.context_token_estimate,
                    provider_prompt_tokens=self.provider_prompt_tokens,
                    provider_input_tokens=self.provider_input_tokens,
                    provider_cached_tokens=self.provider_cached_tokens,
                    provider_cache_miss_tokens=self.provider_cache_miss_tokens,
                    provider_cache_hit_ratio=self.provider_cache_hit_ratio,
                    static_context_digest=self.static_context_digest,
                    history_context_digest=self.history_context_digest,
                    schema_digest=self.schema_digest,
                    context_usage_percent=round(self.context_usage_percent, 1))
        if include_history:
            value.update(trace=list(self.trace), outputs=list(self.outputs))
        return value

    async def log(self, kind, *, identity=None, turn_id=None, **payload):
        self._trace_seq += 1
        source = identity or self.identity()
        source_turn_id = self._identity_turns.get(source, self.last_turn_id)
        item = {"trace_seq": self._trace_seq, "kind": kind, "conversation_id": source[0],
                "trace_id": source[1], "request_id": source[1],
                "turn_id": source_turn_id if turn_id is None else turn_id,
                "at": time.time(), **payload}
        self.trace.append(item)
        # WebSocket debug logging deliberately avoids payloads, which made a
        # production incident impossible to attribute from server logs.  Log
        # the small, non-content control-plane subset needed to reconstruct
        # Agent wakeups and output gates.  User/assistant/tool text and tool
        # arguments remain confined to the bounded in-memory trace.
        if kind == "LLM_SPEAK_CONTEXT":
            logger.info(
                "agent.speak_context episode_id=%s request_id=%s turn=%s state=%s owner_scope=%s "
                "continuation_mode=%s committed_prefix_chars=%s current_clause_chars=%s "
                "prefetched_clause_chars=%s revisable_tail_chars=%s planned_chars=%s locked_chars=%s "
                "heard_chars=%s active_same_turn=%s",
                source[0], source[1], item["turn_id"], payload.get("speak_state"),
                payload.get("owner_scope"), payload.get("continuation_mode"),
                payload.get("committed_prefix_chars"), payload.get("current_clause_chars"),
                payload.get("prefetched_clause_chars"), payload.get("revisable_tail_chars"),
                payload.get("planned_chars"), payload.get("locked_chars"), payload.get("heard_chars"),
                payload.get("active_same_turn"),
            )
        elif kind in {"SPEECH_PREFIX_RECONCILED", "CROSS_TURN_SPEECH_PREFIX_STRIPPED"}:
            logger.info(
                "agent.speech_prefix kind=%s episode_id=%s request_id=%s turn=%s baseline_source=%s "
                "baseline_chars=%s new_chars=%s suffix_chars=%s stripped=%s reason=%s speech_id=%s",
                kind, source[0], source[1], item["turn_id"], payload.get("baseline_source"),
                payload.get("baseline_chars"), payload.get("new_chars"), payload.get("suffix_chars"),
                payload.get("stripped"), payload.get("reason"), payload.get("speech_id"),
            )
        elif kind in {
            "AGENT_INVOKED", "SPEAK_REQUESTED", "SPEAK_ACCEPTED", "SPEAK_BLOCKED",
            "WAIT", "TTS_EMPTY_AUDIO", "TTS_EMPTY_AUDIO_RETRY_FAILED", "TTS_STARTED",
            "TTS_HELD", "TTS_RELEASED",
            "TTS_COMPLETED", "TTS_INTERRUPTED", "TTS_FAILED",
            "USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED", "USER_INPUT_REFINED",
            "SPEECH_STARTED", "SPEECH_ENDED", "ENDPOINT_CANDIDATE",
            "BARGE_IN_CONFIRMED", "BARGE_IN_CANDIDATE_STARTED", "BARGE_IN_CANDIDATE_CLEARED",
            "BARGE_IN_REJECTED", "AGENT_ERROR", "ASSISTANT_CONTENT_DISCARDED",
            "TTS_REPLACED", "TTS_REPLACE_FAILED", "TTS_PREWARM_REQUESTED", "TTS_PREWARM_READY",
            "TTS_PREWARM_FAILED", "TTS_PREWARM_FALLBACK",
            "SPEAK_CONFIRMATION_MISSING", "SPEAK_PROTOCOL_ERROR", "TOOL_PROTOCOL_ERROR",
            "LLM_RECOVERY_QUEUED", "LLM_RECOVERY_STARTING", "LLM_RECOVERY_STARTED", "LLM_RECOVERY_FAILED",
            "LLM_RECOVERY_COMPLETED", "LLM_DISPATCHED", "LLM_COALESCED",
            "LLM_ACTIVE_REQUEST_FINISHED",
            "SCHEDULER_ARMED", "SCHEDULER_EVENT_ENQUEUED",
            "SCHEDULER_ANCHOR_DEFERRED", "SCHEDULER_ANCHOR_ACTIVATED",
            "SCHEDULER_ANCHOR_WAITING_FOR_OUTPUT", "TTS_PLAYBACK_COMPLETED",
            "SCHEDULER_TICK", "SCHEDULER_FORCED_COVERED", "SCHEDULER_FORCED_RESOLVED",
            "SCHEDULER_FINAL_PENDING", "SYSTEM_CHECK_SUPERSEDED", "SYSTEM_CHECK_STALE",
            "SCHEDULER_HEARTBEAT_DELAYED", "SCHEDULER_HEARTBEAT_COALESCED",
            "SCHEDULER_HEARTBEAT_STALE", "SCHEDULER_HEARTBEAT_THREAD_STARTED",
            "SCHEDULER_HEARTBEAT_THREAD_STOPPED", "SCHEDULER_HEARTBEAT_THREAD_ERROR",
            "SYSTEM_CHECK_ACTION_REJECTED",
            "SPEAK_BUSY_IGNORED", "SPEECH_REVISED", "SPEECH_REVISION_REJECTED", "SPEECH_REVISION_NOOP",
            "SPEECH_IDENTICAL_IGNORED",
            "SENTENCE_CREATED", "SENTENCE_REVISED", "SPEAK_STATE_CHANGED",
            "ACTION_STARTED", "ACTION_COMPLETED", "SPEAK_ENVELOPE_INVALID", "EMOTION_NORMALIZED",
            "LLM_EMPTY_OUTPUT", "LLM_OUTPUT_COMPAT_RECOVERED", "LLM_CONTEXT_CAPTURED",
            "LLM_REQUEST_COMPLETED", "SSE_STREAM_INTEGRITY",
            "RESPONSES_INCOMPLETE", "LLM_OUTPUT_FILTERED", "LLM_PROVIDER_UNAVAILABLE",
            "OUTPUT_CANCELLED", "OUTPUT_REPLACED",
            "LLM_RECOVERY_EXHAUSTED", "LLM_TRANSPORT_RETRY_EXHAUSTED",
        }:
            logger.info(
                "agent.trace kind=%s episode_id=%s request_id=%s turn=%s events=%s reason=%s "
                "status=%s speech_id=%s code=%s attempts=%s limit=%s terminal=%s "
                "effort=%s classification=%s error=%s raw_chars=%s non_whitespace_chars=%s "
                "provisional_chars=%s partial_speech=%s json_syntax_valid=%s "
                "json_first=%s json_last=%s json_fenced=%s json_error_kind=%s "
                "json_error_pos=%s json_error_at_end=%s json_brace_balance=%s "
                "json_bracket_balance=%s json_string_open=%s json_top_type=%s "
                "json_has_speak=%s json_has_actions=%s json_top_key_count=%s repairs=%s "
                "context_digest=%s validation=%s action_count=%s audit_capture_ms=%s "
                "error_layer=%s error_code=%s provider_schema_valid=%s local_contract_valid=%s "
                "sse_events=%s sse_malformed=%s sequence_missing=%s sequence_duplicate=%s "
                "sequence_non_increasing=%s sequence_jump=%s dispatch_kind=%s prompt_kind=%s "
                "scheduler_generation=%s window_index=%s owner_scope=%s system_check_id=%s lateness_ms=%s "
                "anchor_source=%s user_silence_monotonic=%s tts_playback_completed_monotonic=%s "
                "anchor_monotonic=%s",
                kind, source[0], source[1], item["turn_id"],
                payload.get("events"), payload.get("reason"), payload.get("status"), payload.get("speech_id"),
                payload.get("code"), payload.get("attempts"), payload.get("limit"),
                payload.get("terminal_kind"),
                payload.get("reasoning_effort"), payload.get("classification"), payload.get("error"),
                payload.get("raw_chars"), payload.get("non_whitespace_chars"),
                payload.get("provisional_chars"), payload.get("partial_speech"),
                payload.get("json_syntax_valid"), payload.get("json_first"),
                payload.get("json_last"), payload.get("json_fenced"),
                payload.get("json_error_kind"), payload.get("json_error_pos"),
                payload.get("json_error_at_end"), payload.get("json_brace_balance"),
                payload.get("json_bracket_balance"), payload.get("json_string_open"),
                payload.get("json_top_type"), payload.get("json_has_speak"),
                payload.get("json_has_actions"), payload.get("json_top_key_count"),
                payload.get("repairs"),
                payload.get("context_digest"), payload.get("validation"), payload.get("action_count"),
                payload.get("audit_capture_ms"),
                payload.get("error_layer"), payload.get("error_code"),
                payload.get("provider_schema_valid"), payload.get("local_contract_valid"),
                payload.get("sse_event_count"), payload.get("sse_malformed_count"),
                payload.get("sequence_missing_count"), payload.get("sequence_duplicate_count"),
                payload.get("sequence_non_increasing_count"), payload.get("sequence_jump_count"),
                payload.get("dispatch_kind"), payload.get("prompt_kind"),
                 payload.get("scheduler_generation"), payload.get("window_index"),
                 payload.get("owner_scope"), payload.get("system_check_id"), payload.get("lateness_ms"),
                 payload.get("anchor_source"), payload.get("user_silence_monotonic"),
                 payload.get("tts_playback_completed_monotonic"), payload.get("anchor_monotonic"),
            )
        await self.emit("agent.trace", item)
        # A late output transaction may still need a trace after its Episode
        # has ended.  It must not rewrite the live Episode snapshot.
        if source == self.identity():
            await self.emit("agent.episode", self.snapshot(include_history=False))

    def identity(self):
        return self.episode_id, self.agent_step_id

    def scope_active(self, identity):
        # Request identities are correlation only.  Input finalization,
        # refinement and tool completion never make an in-flight request
        # invalid.  Explicit stop/reset paths close the Episode or cancel the
        # request task instead.
        return (not getattr(self, "closed", False)
                and getattr(self, "episode_id", None) is not None
                and identity[0] == self.episode_id)

    # Compatibility alias for integrations that used the old gate callback.
    # It is a scope check only; it never compares input revisions or decides
    # that an ordinary event is stale.
    def valid(self, identity):
        return self.scope_active(identity)

    def _start_tokenizer_prewarm(self) -> None:
        """Warm optional exact token counting without delaying voice input."""

        global _O200K_STATE
        with _O200K_LOCK:
            if _O200K_STATE != "uninitialized":
                return
            _O200K_STATE = "loading"
        self._tokenizer_prewarm_task = asyncio.create_task(
            asyncio.to_thread(_load_o200k_encoding), name="agent-tokenizer-prewarm"
        )

    @staticmethod
    def _estimate_tokens(serialized: str) -> int:
        """Return exact tokens only after the background cache is ready.

        Calling a cold ``tiktoken.get_encoding`` here previously froze the
        sole realtime loop, delaying both LLM invocation and session.stop.
        """

        encoding = _O200K_ENCODING
        if encoding is not None:
            try:
                return len(encoding.encode(serialized))
            except Exception as exc:
                logger.warning("agent.tokenizer_estimate_failed error=%s", str(exc)[:200])
        return _fallback_token_estimate(serialized)

    def _update_context_metrics(self, messages, schemas=None) -> str:
        request_shape = {"messages": messages, "tools": schemas or [], "thinking": {"type": "enabled"},
                         "reasoning_effort": "low"}
        serialized = json.dumps(request_shape, ensure_ascii=False)
        self.context_chars = len(serialized)
        self.context_token_estimate = self._estimate_tokens(serialized)
        self.context_usage_percent = (
            self.context_token_estimate * 100 / self.options.max_context_tokens
            if self.options.max_context_tokens else 0.0
        )
        return serialized

    @staticmethod
    def _compact_audio_cues(value):
        if not isinstance(value, list):
            return []
        cues = []
        for cue in value[:96]:
            if not isinstance(cue, dict):
                continue
            compact = {}
            for key in ("text", "start", "end", "emotion", "score", "label", "type"):
                item = cue.get(key)
                if isinstance(item, str):
                    compact[key] = item[:160]
                elif isinstance(item, (int, float, bool)):
                    compact[key] = item
            if compact:
                cues.append(compact)
        if len(value) > len(cues):
            cues.append({"truncated": True, "total": len(value)})
        return cues

    def _context_inputs(self):
        result = {}
        for turn_id, value in self.inputs.items():
            if not isinstance(value, dict):
                continue
            item = {}
            for key in ("text", "refined_text", "refinement_available", "provisional_transcript", "final", "input_revision", "generation_id"):
                if key in value:
                    item[key] = value[key]
            cues = self._compact_audio_cues(value.get("audio_cues"))
            if cues:
                item["audio_cues"] = cues
            analysis = value.get("analysis")
            if isinstance(analysis, dict):
                compact_analysis = {}
                for lane in ("endpoint", "refine"):
                    details = analysis.get(lane)
                    if not isinstance(details, dict):
                        continue
                    compact_analysis[lane] = {
                        key: details[key]
                        for key in ("status", "model", "model_loaded", "fallback", "probability", "error")
                        if isinstance(details.get(key), (str, int, float, bool))
                    }
                if compact_analysis:
                    item["analysis"] = compact_analysis
            result[turn_id] = item
        return result

    def _context_outputs(self):
        result = []
        for output in list(self.outputs)[-8:]:
            delivery = output.get("delivery") if isinstance(output, dict) else None
            item = {
                key: output.get(key)
                for key in ("speech_id", "episode_id", "turn_id", "status", "generation_id", "kind")
                if isinstance(output, dict) and key in output
            }
            if isinstance(delivery, dict):
                item["delivery"] = {
                    key: value[:4000] if isinstance(value, str) else value
                    for key, value in delivery.items()
                    if key in {"status", "termination", "precision", "heard_text", "uncertain_text", "unheard_text"}
                }
            result.append(item)
        return result

    async def ensure_episode(self, *, turn_id=None, text=None, **payload):
        """Create the Episode/input association without scheduling a request.

        Controllers use this while stopping a half-spoken turn so history can
        retain the stable Episode ID before the runtime is torn down.
        """
        if self.closed or self.episode_id is not None:
            if turn_id and text and self.episode_id:
                self.last_turn_id = turn_id
                previous = self.inputs.get(turn_id, {})
                self.inputs[turn_id] = {**previous, **payload, "text": text}
            return self.episode_id
        # System/action events can start a conversation without fabricating a
        # user turn.  Voice input remains the usual path, but it is no longer
        # the scheduler's only activation authority.
        if text is not None and not isinstance(text, str):
            return None
        # Read the configured Markdown documents exactly once for this Agent
        # episode. Subsequent requests use this immutable snapshot, so editing
        # a file cannot invalidate the static prompt prefix mid-conversation.
        self.persona_documents = (
            self.persona_loader.load()
            if self.persona_loader is not None
            else PersonaDocuments()
        )
        self.custom_instructions = (
            list(self.custom_instructions_provider())
            if callable(self.custom_instructions_provider) else []
        )
        self.episode_id = self.conversation.conversation_id
        self.state = "WAITING_EVENT"
        self.inputs.clear()
        self.ledger.clear_shadow_progress(reset_seen=True)
        self._secondary_enqueued = 0
        self._secondary_consumed_normal = 0
        self._secondary_consumed_check = 0
        self._secondary_cleared_final = 0
        self._secondary_dispatched = 0
        self._secondary_skipped_speaking = 0
        self._secondary_skipped_busy = 0
        # Request correlation is local to this Episode and is never consulted
        # to cancel an in-flight call.
        self.context_chars = 0
        self.context_token_estimate = 0
        self.context_usage_percent = 0.0
        self.context_compacted = False
        self.context_compaction = {}
        self.provider_prompt_tokens = None
        self.provider_input_tokens = None
        self.provider_cached_tokens = None
        self.provider_cache_miss_tokens = None
        self.provider_cache_hit_ratio = None
        self.static_context_digest = None
        self.history_context_digest = None
        self.schema_digest = None
        self._history_prefix = []
        self.minor_protocol_feedback.clear()
        self.deferred_recovery_feedback.clear()
        self._recovery_attempts_by_turn.clear()
        self._transport_retry_attempts_by_turn.clear()
        self._retry_shadow_information_by_turn.clear()
        self._retry_secondary_by_turn.clear()
        self._retry_system_check = None
        self._heartbeat_generated = 0
        self._heartbeat_delivered = 0
        self._heartbeat_coalesced = 0
        definitions = self.registry.action_definitions() if self.tools_enabled and hasattr(self.registry, "action_definitions") else []
        # A session-stable catalog keeps the strict output schema and the
        # request prefix cacheable. Live backend availability is a tail fact.
        self._frozen_action_definitions = tuple(json.loads(json.dumps(item)) for item in definitions if isinstance(item, dict))
        self._frozen_tool_catalog = tuple(self._tool_catalog(self._frozen_action_definitions))
        self._start_tokenizer_prewarm()
        self.registry.begin_turn(self.episode_id)
        if turn_id and text and text.strip():
            self.last_turn_id = turn_id
            self.inputs[turn_id] = {**payload, "text": text, "final": False}
        await self._start_heartbeat_clock()
        await self.log("EPISODE_STARTED")
        if self._scheduled_wakes:
            self._wake("SCHEDULED_WAKE")
        return self.episode_id

    async def schedule_system_check(self, prompt_kind: str, source_id: str,
                                    arm_silence: bool = False, **context) -> int | None:
        """Queue one system-owned check without fabricating a user input."""

        if prompt_kind != "kws_wake" or not isinstance(source_id, str) or not source_id:
            raise ValueError("unsupported system check")
        if await self.ensure_episode() is None:
            return None
        generation = self.scheduler.generation
        if arm_silence:
            generation = self.scheduler.arm(
                time.monotonic(), wall_time=time.time(), turn_id=source_id,
            )
        candidate = {
            "dispatch_kind": "system_check",
            "prompt_kind": prompt_kind,
            "source_id": source_id,
            "anchor_turn_id": source_id,
            "anchor_wall_time": self.scheduler.anchor_wall_time,
            "silence_elapsed_ms": 0,
            "window_index": 0,
            "generation": generation,
            **context,
        }
        current = self._immediate_system_check
        if current is None or int(current.get("generation") or 0) <= generation:
            self._immediate_system_check = candidate
        self._ensure_runner()
        return generation

    async def arm_silence_window(self, turn_id: str | None = None, *,
                                 anchor_source: str = "model_speech") -> int | None:
        """Arm the KWS final-silence ladder without dispatching a check.

        ``kws_wake`` and a finalized user input already enter ASR ownership
        together with their timer.  A model-initiated speech (scheduled/DSH
        wake task) rotates KWS→ASR as well, and the ladder's final window is
        the only operation that gives the microphone back to KWS.

        Returns the armed generation, or ``None`` when no ladder was armed:
        an active or deferred user ladder already owns the handover, and a
        closed or not-yet-started Episode has no ownership to protect.
        """

        if self.closed or self.episode_id is None:
            return None
        if self.scheduler.armed or self.scheduler.is_deferred():
            return None
        anchor_turn_id = turn_id or self.last_turn_id
        generation = self.scheduler.arm(
            time.monotonic(), wall_time=time.time(),
            turn_id=anchor_turn_id, anchor_source=anchor_source,
        )
        self._ensure_runner()
        await self.log("SCHEDULER_ARMED", turn_id=anchor_turn_id,
                       generation=generation, anchor_source=anchor_source)
        return generation

    async def enqueue_scheduled_wake(self, task: dict) -> bool:
        """Queue a project wake for the next ordinary heartbeat dispatch."""

        task_id = str(task.get("task_id") or "").strip()
        occurrence_id = str(task.get("occurrence_id") or "").strip()
        text = str(task.get("text") or "").strip()
        if not task_id or not occurrence_id or not text or self.closed:
            return False
        if await self.ensure_episode() is None:
            return False
        self.open_input_scope()
        self._scheduled_wakes.append({
            "task_id": task_id,
            "occurrence_id": occurrence_id,
            "text": text[:500],
        })
        self._wake("SCHEDULED_WAKE")
        return True

    def gate(self, identity):
        if not self.scope_active(identity):
            return "scope_closed"
        return None

    @staticmethod
    def _is_system_check_context(context) -> bool:
        return bool(
            isinstance(context, dict)
            and context.get("owner_scope") == "system_check"
            and context.get("system_check_id")
        )

    def _system_check_stale_reason(self, context) -> str | None:
        if not self._is_system_check_context(context):
            return None
        if int(context.get("generation") or 0) != self.scheduler.generation:
            return "scheduler_generation_changed"
        # 最终静默及关键词唤醒检查可在讲话时结算；二等检查派发前须确认用户未讲话。
        # 所有系统检查的语音都受讲话代次保护，不能播出缺少新话语上下文的旧候选。
        if bool(context.get("user_speaking_at_dispatch")):
            return "user_speaking_at_dispatch"
        dispatch_epoch = context.get("user_speech_epoch")
        if dispatch_epoch is not None and int(dispatch_epoch) != self._user_speech_epoch:
            return "user_speech_started_after_dispatch"
        progress_generation = context.get("shadow_progress_generation")
        if progress_generation is not None and (
            context.get("shadow_activation_id") != self.ledger.shadow_activation_id
            or int(progress_generation) != self.ledger._shadow_progress_generation
            or not self.ledger.shadow_active or not self.ledger.shadow_available
        ):
            return "shadow_activation_expired"  # 影子激活已结束或进度作用域已失效。
        return None

    def _system_check_is_stale(self, context) -> bool:
        return self._system_check_stale_reason(context) is not None

    def _track_system_check_speech(self, handle, context) -> None:
        speech_id = getattr(handle, "speech_id", None)
        if not speech_id or not self._is_system_check_context(context):
            return
        system_check_id = str(context["system_check_id"])
        speech_context = {
            "system_check_id": system_check_id,
            "speech_turn_id": context.get("speech_turn_id"),
            "scheduler_generation": context.get("generation"),
        }
        if context.get("prompt_kind") is not None:
            speech_context["prompt_kind"] = context.get("prompt_kind")
        self._system_check_speeches[speech_id] = speech_context
        self._system_check_output_turn_ids.add(system_check_id)

    def _wake(self, kind, *, turn_id=None):
        if self.context_limited:
            return
        if not self.scheduler.accepts(kind):
            return
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}:
            self._supersede_recovery_without_request_cancel(kind)
        was_pending = bool(self.pending)
        self.pending.add(kind)
        if not was_pending:
            self._pending_since = time.monotonic()
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"} and turn_id:
            self._pending_transcript_turn_id = turn_id
        try:
            loop = asyncio.get_running_loop()
            identity = self.identity()
            turn_id = turn_id or self._identity_turns.get(identity, self.last_turn_id)
            pending_count = len(self.pending)
            loop.create_task(self.log("SCHEDULER_EVENT_ENQUEUED", identity=identity,
                                      turn_id=turn_id, event=kind,
                                      pending_count=pending_count))
            if was_pending:
                loop.create_task(self.log("LLM_COALESCED", identity=identity,
                                          turn_id=turn_id, event=kind,
                                          pending_count=pending_count))
        except RuntimeError:
            pass
        if self.queue.empty():
            self.queue.put_nowait(True)
        self._ensure_runner()
        # Once any ordinary, recovery, forced-poll, or final-silence request
        # has started, later input never preempts it.  New events remain in
        # the shared mailbox for the next idle heartbeat.

    def _notify_user_activity(self) -> None:
        if self.quiet_policy is not None:
            self.quiet_policy.user_interaction = True
        callback = self.on_user_activity
        if callable(callback):
            try:
                callback()
            except Exception:
                logger.exception("agent.continuity_user_activity_failed")

    def _notify_silence_anchor(self, wall_time: float) -> None:
        callback = self.on_silence_anchor
        if callable(callback):
            try:
                callback(float(wall_time))
            except Exception:
                logger.exception("agent.continuity_anchor_failed")

    def _supersede_recovery_without_request_cancel(self, kind: str) -> None:
        """Preserve queue/output supersede behavior without cancelling LLM.

        The former implementation also cancelled ``active_request``.  That
        single operation is intentionally removed; unstarted recovery work
        and speculative output retain their existing invalidation behavior.
        """

        queued_signals = list(self.recovery_queue)
        queued = len(queued_signals)
        if queued_signals:
            self.recovery_queue.clear()
            for signal in queued_signals:
                if (signal.system_check_id
                        and signal.system_check_id not in self._system_check_output_turn_ids):
                    self._system_check_turn_ids.discard(signal.system_check_id)
        running = self.active_recovery is not None
        if running:
            cancel_output = getattr(self.output, "cancel_all", None)
            if callable(cancel_output):
                cancel_output("superseded_recovery")
        if not queued and not running:
            return
        try:
            loop = asyncio.get_running_loop()
            identity = self.identity()
            turn_id = self._identity_turns.get(identity, self.last_turn_id)
            loop.create_task(self.log(
                "LLM_RECOVERY_SUPERSEDED",
                identity=identity,
                turn_id=turn_id,
                superseded_by=kind,
                queued=queued,
                running=running,
                cancelled_request=False,
            ))
        except RuntimeError:
            pass

    def _supersede_system_output_without_request_cancel(self, kind: str) -> None:
        """Invalidate only unplayed system-check output, never its LLM task."""

        context = self.active_dispatch_context
        running = bool(
            isinstance(context, dict)
            and context.get("owner_scope") == "system_check"
        )
        cancelled_output = 0
        candidates = ([getattr(self.output, "handle", None)]
                      + list(getattr(self.output, "_pending", []) or []))
        for candidate in candidates:
            speech_id = getattr(candidate, "speech_id", None) if candidate is not None else None
            if not speech_id or speech_id not in self._system_check_speeches:
                continue
            workspace = getattr(candidate, "workspace", None)
            if bool(getattr(workspace, "playback_started", False)):
                continue
            cancel = getattr(self.output, "cancel", None)
            if callable(cancel) and speech_id:
                cancel(speech_id, "superseded_system_check")
                cancelled_output += 1

        if running or cancelled_output:
            try:
                loop = asyncio.get_running_loop()
                identity = self.identity()
                turn_id = self._identity_turns.get(identity, self.last_turn_id)
                loop.create_task(self.log(
                    "SYSTEM_CHECK_SUPERSEDED",
                    identity=identity,
                    turn_id=turn_id,
                    superseded_by=kind,
                    cancelled_request=False,
                    request_continued=running,
                    cancelled_output=cancelled_output,
                ))
            except RuntimeError:
                pass

    async def _queue_recovery(self, code, message, *, identity=None, turn_id=None,
                              delivery_feedback=None):
        """Queue one immediate, model-correctable protocol recovery.

        This method is intentionally the only bypass of the ordinary 400 ms
        gate.  Once the turn-scoped budget is exhausted, the failure is
        reported locally and retained for the next ordinary dispatch; the
        bounded scheduler may also surface it on a later forced window.
        Exhaustion itself never schedules an unbounded provider loop.
        """
        source_turn = turn_id or (self._identity_turns.get(identity) if identity else self.last_turn_id)
        active_context = self.active_dispatch_context
        is_active_identity = identity is None or identity == self.active_request_identity
        system_check_id = (
            active_context.get("system_check_id")
            if is_active_identity and isinstance(active_context, dict)
            and active_context.get("owner_scope") == "system_check"
            else None
        )
        if (
            system_check_id is None and isinstance(active_context, dict)
            and active_context.get("system_check_id") in self._system_check_turn_ids
            and source_turn in self._system_check_turn_ids
        ):
            system_check_id = active_context.get("system_check_id")
            active_context = {**active_context, "owner_scope": "system_check"}
        dispatch_context = (
            dict(active_context)
            if system_check_id and isinstance(active_context, dict)
            else None
        )
        signal = RecoverySignal(
            code=str(code), message=str(message)[:500],
            source_request_id=identity[1] if identity else self.agent_step_id,
            source_turn_id=source_turn,
            delivery_feedback=delivery_feedback if isinstance(delivery_feedback, dict) else None,
            system_check_id=system_check_id,
            owner_scope="system_check" if system_check_id else None,
            dispatch_context=dispatch_context,
            shadow_information=(dict(self._retry_shadow_information_by_turn[source_turn or ""])
                                if (source_turn or "") in self._retry_shadow_information_by_turn
                                else None),
            secondary_context=self._retry_secondary_by_turn.get(source_turn or ""),
        )
        if getattr(self.provider, "transport", "chat_completions") == "responses":
            limit = int(getattr(self.provider, "protocol_recovery_limit", 1))
            attempts = self._recovery_attempts_by_turn.get(source_turn or "", 0)
            if attempts >= limit:
                await self.log("LLM_RECOVERY_EXHAUSTED", identity=identity, turn_id=source_turn,
                               code=str(code), attempts=attempts, limit=limit)
                self.deferred_recovery_feedback.append(signal)
                await self.emit("error", {
                    "conversation_id": identity[0] if identity else self.episode_id,
                    "request_id": identity[1] if identity else self.agent_step_id,
                    "turn_id": source_turn,
                    "input_id": source_turn,
                    "code": "LLM_PROTOCOL_RECOVERY_EXHAUSTED",
                    "message": "模型连续返回无效格式，本轮未能生成回复；你可以继续说话。",
                    "recoverable": True,
                    "problem_code": str(code),
                    "retry_after_event": True,
                })
                latest = self.inputs.get(source_turn or "", {})
                if latest.get("final") and not self.output.active:
                    await self._emit_turn_settled(identity or self.identity(), source_turn,
                                                  "protocol_recovery_exhausted")
                return None
            self._recovery_attempts_by_turn[source_turn or ""] = attempts + 1
        if self.active_recovery is not None:
            await self.log("LLM_RECOVERY_FAILED", identity=identity,
                           turn_id=signal.source_turn_id,
                           error_id=self.active_recovery.error_id,
                           next_error_id=signal.error_id,
                           code=signal.code)
        self.recovery_queue.append(signal)
        await self.log("LLM_RECOVERY_QUEUED", identity=identity, turn_id=signal.source_turn_id,
                       error_id=signal.error_id, code=signal.code,
                       owner_scope=signal.owner_scope,
                       system_check_id=signal.system_check_id)
        if self.queue.empty():
            self.queue.put_nowait(True)
        self._ensure_runner()
        return signal

    @staticmethod
    def _recovery_origin_prompt_kind(signal: RecoverySignal | None) -> str:
        context = signal.dispatch_context if signal is not None else None
        if not isinstance(context, dict):
            return "normal"
        value = str(context.get("origin_prompt_kind") or context.get("prompt_kind") or "normal")
        return value if value in {"secondary_mailbox", "final_silence", "kws_wake"} else "normal"

    @staticmethod
    def _recovery_instruction(code: str, *, deferred: bool = False,
                              origin_prompt_kind: str = "normal") -> str:
        """Return a short deterministic instruction for a known model error."""

        if deferred:
            prefix = (
                "上一次即时纠正仍未通过，失败输出未写入会话历史。"
                "不要延续、解释或复现错误；本次只依据当前请求及最新上下文重新生成。"
            )
        elif origin_prompt_kind == "normal":
            prefix = "失败输出未写入会话历史。这是本用户轮次唯一一次即时自动纠正。"
        else:
            prefix = "失败输出未写入会话历史。这是原系统检查唯一一次即时自动纠正。"
        templates = {
            "LLM_EMPTY_OUTPUT": (
                "上一次输出为空白。只输出一个完整 JSON 对象，顶层只能有 speak 和 actions；"
                "首个非空白字符必须是 {，末个必须是 }。需要等待也必须输出完整的 "
                "speak.state=wait，不能用空白代替。"
            ),
            "SPEAK_ENVELOPE_INVALID": (
                "上一次输出未通过语音信封校验。只输出完整 JSON，顶层只能有 speak 和 actions。"
                "state=speak 时 text 必须非空；state=wait 或 cancel 时 text 必须为空字符串。"
                "不要输出 JSON 外文本。"
            ),
            "TOOL_PROTOCOL_ERROR": (
                "上一次 actions 不符合当前动作目录，失败动作未执行。actions 只能使用当前目录中的"
                "名称和精确参数；不需要动作时输出 actions:[]。同时输出完整 speak 字段，"
                "不要输出 JSON 外文本。"
            ),
            "RESPONSES_INCOMPLETE": (
                "上一次输出因长度上限未完成，截断内容未提交。请缩短内容并输出一个完整 JSON 对象；"
                "顶层只能有 speak 和 actions，不要输出 JSON 外文本。"
            ),
        }
        instruction = templates.get(code, templates["SPEAK_ENVELOPE_INVALID"])
        if deferred:
            scope = "遵守本次请求当前携带的场景约束，不要把上一次失败输出当成新的会话事实。"
        elif origin_prompt_kind == "secondary_mailbox":
            scope = "继续原 secondary_mailbox（二等信箱）状态评估；本次没有新的用户输入，不要重新回答历史用户请求。"
        elif origin_prompt_kind == "final_silence":
            scope = "继续原 final-silence 状态评估；不要把最后一条历史用户输入当成新的请求。"
        elif origin_prompt_kind == "kws_wake":
            scope = "只重新生成原唤醒确认；不要处理历史用户任务。"
        else:
            scope = "重新处理最后一条用户输入。"
        return prefix + instruction + scope

    async def _record_emotion_normalizations(self, parser, *, identity, turn_id) -> None:
        """Make benign model protocol repairs visible to the next LLM poll."""

        for item in getattr(parser, "emotion_normalizations", ()):
            provided = item.get("provided") if isinstance(item, dict) else None
            applied = item.get("applied") if isinstance(item, dict) else None
            if not isinstance(provided, str) or not isinstance(applied, str):
                continue
            feedback = {"field": "speak.emotion", "provided": provided, "applied": applied}
            self.minor_protocol_feedback.append(feedback)
            await self.log("EMOTION_NORMALIZED", identity=identity, turn_id=turn_id,
                           provided=provided, applied=applied, severity="minor")

    def _note_identical_speak(self, baseline_source: str) -> None:
        """Tell the model that a repeat was handled locally instead of silently.

        The previous silent ignore left the model believing its decision had
        been applied, so an identical heartbeat dispatch repeated the same
        mistake indefinitely.  This rides the existing bounded minor-feedback
        channel: it reaches the next request without spending the protocol
        recovery budget or scheduling an extra provider round-trip.
        """

        self.minor_protocol_feedback.append({
            "field": "speak",
            "baseline_source": str(baseline_source),
            "applied": "ignored_identical_to_delivered",
            "hint": "该内容与已交付语音一致，本地未重播；没有新内容时使用 wait，"
                    "需要改写尚未播出的尾巴时使用 revision",
        })

    def cancel_active_request(self, reason: str = "cancelled") -> None:
        """Cancel only an explicit lifecycle scope (stop/barge-in/close)."""
        request = self.active_request
        if request is not None and request is not asyncio.current_task() and not request.done():
            request.cancel(reason)
        self.recovery_queue.clear()
        if self.delay_task is not None and not self.delay_task.done():
            self.delay_task.cancel()
        logger.info("agent.request_cancelled reason=%s episode_id=%s request_id=%s",
                    reason, self.episode_id, self.agent_step_id)

    def close_input_scope(self) -> None:
        """Stop starting new input-driven work without cancelling live work.

        Stopping the microphone is an input boundary: a response that is
        already being generated or is waiting in the TTS FIFO still has to be
        heard, so the active request keeps running.  Only the paths that would
        dispatch *new* work are closed here — a queued recovery signal or a
        scheduled revisit must not produce speech after the user stopped.
        """

        self.input_scope_closed = True
        self.ledger.clear_shadow_progress(reset_seen=True)
        self._retry_secondary_by_turn.clear()
        self._discard_secondary_retry()
        self.recovery_queue.clear()
        if self.delay_task is not None and not self.delay_task.done():
            self.delay_task.cancel()

    def open_input_scope(self) -> None:
        """Re-open dispatch for a session that starts listening again."""

        self.input_scope_closed = False

    async def _emit_turn_settled(self, identity, turn_id, outcome):
        """Tell the controller that a finalized turn ended without speech.

        Speech-bearing turns settle from ``speech.completed`` because only the
        player knows what was actually delivered.  Control-only paths still
        need one explicit terminal signal instead of relying on controller
        input cleanup to guess that the turn ended.
        """
        if not turn_id:
            return
        await self.emit("agent.turn_settled", {
            "conversation_id": identity[0],
            "request_id": identity[1],
            "turn_id": turn_id,
            "input_id": turn_id,
            "outcome": outcome,
            "status": "completed",
        })

    @staticmethod
    def _positive_time(value):
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        return None

    def _output_is_busy(self) -> bool:
        """Return the output transaction's conservative busy state."""

        output = self.output
        if output is None:
            return False
        if bool(getattr(output, "busy", False)) or bool(getattr(output, "active", False)):
            return True
        settling = getattr(output, "_settling_speech_ids", None)
        if settling:
            return True
        pending = getattr(output, "_pending", None)
        if pending:
            return any(
                item is not None
                and not getattr(item, "reason", None)
                and not getattr(getattr(item, "task", None), "done", lambda: False)()
                for item in pending
            )
        return False

    def _register_speech_transaction(self, handle) -> None:
        """Associate a speech transaction with its originating LLM request."""

        identity = getattr(handle, "identity", None)
        speech_id = getattr(handle, "speech_id", None)
        if not isinstance(identity, (tuple, list)) or len(identity) < 2:
            return
        if not identity[1] or not speech_id:
            return
        request_id = str(identity[1])
        self._speech_transactions_by_request.setdefault(request_id, set()).add(str(speech_id))

    def _forget_speech_transaction(self, record) -> None:
        if not isinstance(record, dict):
            return
        request_id = record.get("request_id") or record.get("trace_id") or record.get("agent_step_id")
        speech_id = record.get("speech_id")
        if not request_id or not speech_id:
            return
        speech_ids = self._speech_transactions_by_request.get(str(request_id))
        if not speech_ids:
            return
        speech_ids.discard(str(speech_id))
        if not speech_ids:
            self._speech_transactions_by_request.pop(str(request_id), None)

    def _bind_pending_silence_request(self, identity, turn_id, *, allow_rebind=False) -> None:
        pending = self._pending_silence_anchor
        if not self.defer_silence_until_output or not isinstance(pending, dict):
            return
        generation = pending.get("generation")
        if (pending.get("turn_id") != turn_id
                or generation != self.scheduler.generation
                or not self.scheduler.is_deferred(generation)):
            return
        if pending.get("request_id") is None or allow_rebind:
            pending["conversation_id"] = identity[0]
            pending["request_id"] = identity[1]

    def _defer_silence_anchor_for_input(self, turn_id, payload):
        now = time.monotonic()
        user_silence = self._positive_time(payload.get("input_finished_at")) or now
        user_wall_time = self._positive_time(payload.get("occurred_at")) or time.time()
        previous = self._pending_silence_anchor
        generation = self.scheduler.defer(
            now=now, wall_time=user_wall_time, turn_id=turn_id,
        )
        pending = {
            "turn_id": turn_id,
            "conversation_id": self.episode_id,
            "request_id": None,
            "generation": generation,
            "user_silence_monotonic": user_silence,
            "user_silence_wall_time": user_wall_time,
            "tts_playback_completed_monotonic": None,
            "tts_playback_completed_wall_time": None,
            "request_settled_at": None,
            "request_outcome": None,
            "waiting_for_output": False,
        }
        self._pending_silence_anchor = pending
        return previous, pending

    def _pending_silence_matches(self, *, turn_id=None, request_id=None,
                                 generation=None, allow_output_settlement=False) -> bool:
        pending = self._pending_silence_anchor
        if not self.defer_silence_until_output or not isinstance(pending, dict):
            return False
        expected_generation = pending.get("generation")
        if (expected_generation != self.scheduler.generation
                or not self.scheduler.is_deferred(expected_generation)):
            return False
        if generation is not None and generation != expected_generation:
            return False
        if turn_id is not None and pending.get("turn_id") != turn_id:
            return False
        expected_request = pending.get("request_id")
        if expected_request is None:
            return request_id is None
        if allow_output_settlement and pending.get("waiting_for_output"):
            return True
        return request_id is not None and str(request_id) == str(expected_request)

    async def _activate_pending_silence_anchor(
        self, *, turn_id, request_id=None, generation=None,
        playback_completed_at=None, playback_wall_time=None,
        settled_at=None, settled_wall_time=None, source_hint=None,
        speech_id=None, allow_output_settlement=False,
    ) -> bool:
        if not self._pending_silence_matches(
                turn_id=turn_id, request_id=request_id, generation=generation,
                allow_output_settlement=allow_output_settlement):
            return False
        pending = self._pending_silence_anchor
        user_silence = pending["user_silence_monotonic"]
        playback_at = self._positive_time(playback_completed_at)
        settled = self._positive_time(settled_at)
        candidate = playback_at if source_hint == "tts_playback_completed" else settled
        if candidate is None and source_hint == "tts_playback_completed":
            candidate = time.monotonic()
        if candidate is None and source_hint == "transaction_settled":
            candidate = time.monotonic()

        if candidate is not None and candidate >= user_silence:
            anchor_at = candidate
            if source_hint == "tts_playback_completed" and playback_at is not None:
                anchor_source = "tts_playback_completed"
                anchor_wall_time = (
                    self._positive_time(playback_wall_time) or time.time()
                )
            elif source_hint == "transaction_settled":
                anchor_source = "transaction_settled"
                anchor_wall_time = (
                    self._positive_time(settled_wall_time) or time.time()
                )
            else:
                anchor_source = "user_silence"
                anchor_wall_time = pending["user_silence_wall_time"]
        else:
            anchor_at = user_silence
            anchor_source = "user_silence"
            anchor_wall_time = pending["user_silence_wall_time"]

        activated = self.scheduler.activate_deferred(
            anchor_at,
            wall_time=anchor_wall_time,
            turn_id=pending["turn_id"],
            generation=pending["generation"],
            anchor_source=anchor_source,
        )
        if not activated:
            return False
        self._notify_silence_anchor(anchor_wall_time)
        pending.update({
            "tts_playback_completed_monotonic": playback_at,
            "tts_playback_completed_wall_time": self._positive_time(playback_wall_time),
            "anchor_monotonic": anchor_at,
            "anchor_wall_time": anchor_wall_time,
            "anchor_source": anchor_source,
            "speech_id": speech_id,
        })
        self._pending_silence_anchor = None
        identity = (
            pending.get("conversation_id") or self.episode_id,
            pending.get("request_id") or self.agent_step_id,
        )
        await self.log(
            "SCHEDULER_ANCHOR_ACTIVATED",
            identity=identity,
            turn_id=pending["turn_id"],
            request_id=pending.get("request_id"),
            speech_id=speech_id,
            scheduler_generation=pending["generation"],
            anchor_source=anchor_source,
            user_silence_monotonic=pending["user_silence_monotonic"],
            user_silence_wall_time=pending["user_silence_wall_time"],
            tts_playback_completed_monotonic=playback_at,
            tts_playback_completed_wall_time=self._positive_time(playback_wall_time),
            settled_at=settled,
            anchor_monotonic=anchor_at,
            anchor_wall_time=anchor_wall_time,
        )
        await self.log(
            "SCHEDULER_ARMED",
            identity=identity,
            turn_id=pending["turn_id"],
            request_id=pending.get("request_id"),
            generation=pending["generation"],
            anchor_time=anchor_wall_time,
            anchor_source=anchor_source,
            user_silence_monotonic=pending["user_silence_monotonic"],
            tts_playback_completed_monotonic=playback_at,
            anchor_monotonic=anchor_at,
        )
        self._ensure_runner()
        return True

    async def playback_completed(self, *, turn_id, speech_id=None,
                                 conversation_id=None, request_id=None,
                                 output_generation=None,
                                 playback_completed_at=None, wall_time=None):
        """Activate a deferred KWS ladder at the real player drain boundary."""

        if not self.defer_silence_until_output:
            return False
        if conversation_id is not None and conversation_id != self.episode_id:
            return False
        pending = self._pending_silence_anchor
        generation = pending.get("generation") if isinstance(pending, dict) else None
        if not self._pending_silence_matches(
                turn_id=turn_id, request_id=request_id, generation=generation):
            return False
        await self.log(
            "TTS_PLAYBACK_COMPLETED",
            identity=(conversation_id or self.episode_id, request_id),
            turn_id=turn_id,
            request_id=request_id,
            speech_id=speech_id,
            output_generation=output_generation,
            playback_completed_at=playback_completed_at,
        )
        return await self._activate_pending_silence_anchor(
            turn_id=turn_id,
            request_id=request_id,
            generation=generation,
            playback_completed_at=playback_completed_at,
            playback_wall_time=wall_time,
            source_hint="tts_playback_completed",
            speech_id=speech_id,
        )

    async def _settle_pending_silence_after_request(self, identity, turn_id,
                                                     outcome) -> bool:
        """Use request settlement only when that request created no speech."""

        request_id = identity[1] if isinstance(identity, (tuple, list)) and len(identity) > 1 else None
        pending = self._pending_silence_anchor
        if not self._pending_silence_matches(
                turn_id=turn_id, request_id=request_id,
                generation=(pending.get("generation") if isinstance(pending, dict) else None),
        ):
            return False
        if request_id and self._speech_transactions_by_request.get(str(request_id)):
            return False
        settled_at = time.monotonic()
        pending["request_settled_at"] = settled_at
        pending["request_outcome"] = outcome
        if self._output_is_busy():
            pending["waiting_for_output"] = True
            await self.log(
                "SCHEDULER_ANCHOR_WAITING_FOR_OUTPUT",
                identity=identity,
                turn_id=turn_id,
                request_id=request_id,
                scheduler_generation=pending["generation"],
                outcome=outcome,
            )
            return False
        return await self._activate_pending_silence_anchor(
            turn_id=turn_id,
            request_id=request_id,
            generation=pending["generation"],
            settled_at=settled_at,
            settled_wall_time=time.time(),
            source_hint="transaction_settled",
        )

    async def output_transaction_settled(self, record) -> bool:
        """Complete the no-audio fallback after output bookkeeping settles."""

        if not isinstance(record, dict) or record.get("status") not in {"completed", "failed"}:
            return False
        pending = self._pending_silence_anchor
        if not isinstance(pending, dict) or not pending.get("waiting_for_output"):
            return False
        request_id = record.get("request_id") or record.get("trace_id") or record.get("agent_step_id")
        turn_id = record.get("turn_id") or record.get("input_id")
        strict_match = self._pending_silence_matches(
            turn_id=turn_id, request_id=request_id,
            generation=pending.get("generation"),
        )
        if not strict_match:
            if not self._pending_silence_matches(
                    turn_id=None, request_id=request_id,
                    generation=pending.get("generation"),
                    allow_output_settlement=True,
            ):
                return False
            turn_id = pending["turn_id"]
        return await self._activate_pending_silence_anchor(
            turn_id=turn_id,
            request_id=pending.get("request_id") if not strict_match else request_id,
            generation=pending["generation"],
            settled_at=time.monotonic(),
            settled_wall_time=time.time(),
            source_hint="transaction_settled",
            speech_id=record.get("speech_id"),
            allow_output_settlement=not strict_match,
        )

    async def observe(self, kind, *, turn_id=None, text=None, **payload):
        if self.closed:
            return
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}:
            self._notify_user_activity()
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"} and (
            not isinstance(text, str) or not text.strip()
        ):
            return
        payload.setdefault("received_at", time.time())
        payload.setdefault("occurred_at", payload["received_at"])
        self.event_inbox.append({
            "event_id": uuid4().hex, "source": str(payload.get("source") or kind.lower()),
            "kind": kind, "turn_id": turn_id,
            "revision": payload.get("source_revision", payload.get("input_revision")),
            "occurred_at": payload["occurred_at"], "received_at": payload["received_at"],
        })
        if not self.scheduler.accepts(kind):
            # TTS is an output-side lifecycle signal.  Keep its diagnostic
            # trace, but do not let even a malformed/overloaded TTS payload
            # create an Episode, mutate last_turn_id, arm the silence ladder,
            # or wake the scheduler mailbox.
            await self.log(kind, **({"text": text} if text is not None else {}), **payload)
            return
        if self.episode_id is None:
            if kind in {"USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED"} and (not text or not text.strip()):
                return
            await self.ensure_episode(turn_id=turn_id, text=text, **payload)
            # Let this observation pass through the normal change path so an
            # actual first utterance schedules a decision.  ``ensure_episode``
            # pre-populates input only for controller stop/error cleanup.
            if turn_id:
                self.inputs.pop(turn_id, None)
        elif self.context_limited and turn_id and turn_id not in self.inputs:
            # Keep the over-budget input in the turn history owned by the
            # controller, but do not grow the Agent context indefinitely.
            self.last_turn_id = turn_id
            await self.log(kind, **({"text": text} if text is not None else {}), **payload)
            return
        late_refinement = bool(
            turn_id and self.last_turn_id and turn_id != self.last_turn_id
            and kind == "USER_INPUT_REFINED"
        )
        if turn_id and not late_refinement:
            self.last_turn_id = turn_id
        changed = False
        transcript_update = None
        duplicate_transcript_update = False
        if kind == "USER_INPUT_REFINED" and turn_id:
            previous = self.inputs.get(turn_id, {})
            source = str(payload.get("source") or "sensevoice")
            source_revision = payload.get("source_revision")
            update_key = (kind, source, source_revision)
            update_keys = set(previous.get("_transcript_update_keys") or ())
            duplicate_transcript_update = source_revision is not None and update_key in update_keys
            if duplicate_transcript_update:
                await self.log("TRANSCRIPT_UPDATE_DUPLICATE", turn_id=turn_id,
                               update_kind="refinement", source=source,
                               source_revision=source_revision)
                return
            update_keys.add(update_key)
            changed = True
            primary = previous.get("text", "")
            sentences = previous.get("sentences") if isinstance(previous.get("sentences"), list) else payload.get("sentences", [])
            mapping = map_refinement(sentences if isinstance(sentences, list) else [], text or "")
            revision = max(
                [int(item.get("revision") or 0) for item in previous.get("transcript_updates", [])
                 if isinstance(item, dict)] or [0]
            ) + 1
            transcript_update = {
                "kind": "refinement", "source": source, "text": text or "", "revision": revision,
            }
            if isinstance(payload.get("audio_cues"), list):
                transcript_update["audio_cues"] = payload["audio_cues"]
            updates = [
                dict(item) for item in previous.get("transcript_updates", [])
                if isinstance(item, dict) and item.get("kind") != "refinement"
            ]
            updates.append(transcript_update)
            # Refined ASR is evidence, not an automatic replacement of the
            # Paraformer final that anchors the user input.
            self.inputs[turn_id] = {**previous, **payload, "text": primary,
                                    "refined_text": text or "", "refinement_available": bool(text),
                                    "refinement_mapping": mapping,
                                    "transcript_updates": updates,
                                    "_transcript_update_keys": update_keys,
                                    "final": previous.get("final", False)}
            await self.log("REFINEMENT_MAPPED" if not mapping["unmapped_refinement"] else "REFINEMENT_UNMAPPED",
                           turn_id=turn_id, sentence_ids=mapping["sentence_ids"], confidence=mapping["confidence"])
        elif text is not None and turn_id:
            previous = self.inputs.get(turn_id, {})
            changed = (
                True if kind == "USER_INPUT_FINALIZED"
                else semantic_text(previous.get("text", "")) != semantic_text(text)
            )
            revision = int(previous.get("input_revision") or 0) + (1 if changed or kind == "USER_INPUT_FINALIZED" else 0)
            if kind == "USER_INPUT_FINALIZED":
                source = str(payload.get("source") or "paraformer")
                source_revision = payload.get("source_revision")
                update_key = (kind, source, source_revision)
                update_keys = set(previous.get("_transcript_update_keys") or ())
                duplicate_transcript_update = source_revision is not None and update_key in update_keys
                if duplicate_transcript_update:
                    await self.log("TRANSCRIPT_UPDATE_DUPLICATE", turn_id=turn_id,
                                   update_kind="asr_final", source=source,
                                   source_revision=source_revision)
                    return
                update_keys.add(update_key)
                transcript_revision = max(
                    [int(item.get("revision") or 0) for item in previous.get("transcript_updates", [])
                     if isinstance(item, dict)] or [0]
                ) + 1
                transcript_update = {
                    "kind": "asr_final", "source": source, "text": text,
                    "revision": transcript_revision,
                }
                streamed_text = str(previous.get("text") or "")
                if streamed_text.strip():
                    transcript_update["streamed_text"] = streamed_text
            previous_sentences = previous.get("sentences") if isinstance(previous.get("sentences"), list) else []
            incoming_sentences = payload.get("sentences") if isinstance(payload.get("sentences"), list) else None
            updated = {**previous, **payload, "text": text,
                       "input_revision": revision,
                       "final": kind == "USER_INPUT_FINALIZED"
                       or previous.get("final", False)}
            if kind == "USER_INPUT_FINALIZED":
                updates = [
                    dict(item) for item in previous.get("transcript_updates", [])
                    if isinstance(item, dict) and item.get("kind") != "asr_final"
                ]
                updates.append(transcript_update)
                updated["transcript_updates"] = updates
                updated["_transcript_update_keys"] = update_keys
                updated["primary_text"] = text
            self.inputs[turn_id] = updated
            if incoming_sentences is not None:
                old_by_id = {
                    int(item.get("sentence_id")): item for item in previous_sentences
                    if isinstance(item, dict) and isinstance(item.get("sentence_id"), int)
                }
                for item in incoming_sentences:
                    if not isinstance(item, dict) or not isinstance(item.get("sentence_id"), int):
                        continue
                    sentence_id = item["sentence_id"]
                    if sentence_id not in old_by_id:
                        await self.log("SENTENCE_CREATED", turn_id=turn_id,
                                       sentence_id=sentence_id,
                                       sentence_revision=item.get("revision", 1))
                    elif old_by_id[sentence_id].get("text") != item.get("text"):
                        await self.log("SENTENCE_REVISED", turn_id=turn_id,
                                       sentence_id=sentence_id,
                                       sentence_revision=item.get("revision", 1))
            if kind == "USER_INPUT_FINALIZED":
                finished_at = payload.get("input_finished_at")
                if isinstance(finished_at, (int, float)) and finished_at > 0:
                    self.input_finished_at[turn_id] = float(finished_at)
                    if len(self.input_finished_at) > 64:
                        oldest = next(iter(self.input_finished_at))
                        self.input_finished_at.pop(oldest, None)
        if changed and kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}:
            self.current_action = None
        valid_anchor = (
            kind == "USER_INPUT_FINALIZED"
            and isinstance(text, str)
            and bool(text.strip())
        ) or (
            kind == "USER_INPUT_REFINED"
            and isinstance(text, str)
            and bool(text.strip())
        )
        if valid_anchor:
            # A meaningful user event is the only operation that re-arms or
            # replaces the silence ladder.  Re-arming invalidates only an
            # unstarted final_pending candidate; it never cancels an in-flight
            # request.  TTS and microphone activity never enter the semantic
            # scheduler mailbox.
            self._supersede_system_output_without_request_cancel(kind)
            superseded_windows = self.scheduler.pending_checkpoint_windows()
            superseded_generation = self.scheduler.generation
            superseded_turn_id = self.scheduler.anchor_turn_id
            if self.defer_silence_until_output:
                superseded_anchor = self._pending_silence_anchor
                generation, pending_anchor = self._defer_silence_anchor_for_input(
                    turn_id, payload,
                )
                anchor_log = {
                    "turn_id": turn_id,
                    "generation": generation,
                    "user_silence_monotonic": pending_anchor["user_silence_monotonic"],
                    "user_silence_wall_time": pending_anchor["user_silence_wall_time"],
                    "superseded_generation": (
                        superseded_anchor.get("generation")
                        if isinstance(superseded_anchor, dict) else None
                    ),
                    "superseded_turn_id": (
                        superseded_anchor.get("turn_id")
                        if isinstance(superseded_anchor, dict) else None
                    ),
                }
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.log(
                        "SCHEDULER_ANCHOR_DEFERRED", **anchor_log,
                    ))
                except RuntimeError:
                    pass
            else:
                self._pending_silence_anchor = None
                generation = self.scheduler.arm(
                    now=time.monotonic(),
                    wall_time=(payload.get("occurred_at")
                               if isinstance(payload.get("occurred_at"), (int, float))
                               else None),
                    turn_id=turn_id,
                )
                if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}:
                    self._notify_silence_anchor(self.scheduler.anchor_wall_time)
            try:
                loop = asyncio.get_running_loop()
                for window in superseded_windows:
                    loop.create_task(self.log(
                        "SCHEDULER_FORCED_RESOLVED",
                        turn_id=superseded_turn_id or turn_id,
                        window_index=window,
                        scheduler_generation=superseded_generation,
                        current_scheduler_generation=generation,
                        expected=True,
                        resolution="superseded_by_new_generation",
                        reason=kind,
                    ))
                if not self.defer_silence_until_output:
                    loop.create_task(self.log(
                        "SCHEDULER_ARMED",
                        turn_id=turn_id,
                        anchor_time=payload.get("occurred_at"),
                        generation=generation,
                    ))
            except RuntimeError:
                pass
        await self.log(kind, **({"text": text} if text is not None else {}), **payload)
        if transcript_update is not None:
            scheduler_action = "coalesced" if self.pending else "enqueued"
            await self.log(
                "TRANSCRIPT_UPDATE", turn_id=turn_id,
                update_kind=transcript_update["kind"], source=transcript_update["source"],
                revision=transcript_update["revision"], scheduler_action=scheduler_action,
                same_turn=True,
            )
            logger.info(
                "transcript.update turn_id=%s kind=%s source=%s revision=%s scheduler_action=%s",
                turn_id, transcript_update["kind"], transcript_update["source"],
                transcript_update["revision"], scheduler_action,
            )
        if kind in {"SPEECH_STARTED", "SPEECH_ENDED", "ENDPOINT_CANDIDATE"}:
            # Activity updates local state and diagnostics, not either decision mailbox.
            return
        if kind == "USER_SEGMENT_UPDATED" and not changed:
            return
        if kind in {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"} and turn_id:
            # Keep the turn hint outside _wake so compatibility probes that
            # replace the wake callback still observe the unchanged API.
            self._pending_transcript_turn_id = turn_id
        self._wake(kind)

    async def speech_activity(self, speaking, *, interrupt_reason: str | None = None):
        """Update microphone activity without conflating it with interruption.

        Ordinary microphone activity is deliberately non-destructive: the LLM
        request and any not-yet-audible speech candidate remain alive while the
        user's ASR input enters the normal coalescing scheduler.  Only callers
        that have crossed an explicit lifecycle boundary (confirmed barge-in,
        unavailable input, etc.) may pass ``interrupt_reason``.
        """
        explicit_interrupt = bool(interrupt_reason)
        # A confirmed barge-in may arrive after passive activity has already
        # set user_speaking=True.  Do not let the duplicate-state fast path
        # swallow that explicit cancellation boundary.
        if speaking == self.user_speaking and not explicit_interrupt:
            return
        previous = self.user_speaking
        self.user_speaking = speaking
        if speaking is True and previous is not True:
            self._notify_user_activity()
            self._user_speech_epoch += 1
            # Withdraw only unplayed system-check speech.  The active provider
            # request deliberately keeps running; its later output is rejected
            # by the epoch check above.  Ordinary user-requested candidates are
            # still held and released by the existing speech lifecycle.
            self._supersede_system_output_without_request_cancel("SPEECH_STARTED")
        if speaking is True and not explicit_interrupt:
            hold = getattr(self.output, "hold_unplayed", None)
            if callable(hold):
                hold()
            # 被动开始讲话不使当前模型请求失效；转写更新仍进入普通 400 毫秒调度窗口。
            if self.voice_activation_task:
                self.voice_activation_task.cancel()
            async def activate_voice():
                try:
                    await asyncio.sleep(self.options.voice_activation_ms / 1000)
                    if self.user_speaking is True:
                        prewarm = getattr(self.provider, "prewarm", None)
                        if callable(prewarm):
                            await prewarm()
                        await self.log("SCHEDULER_VOICE_ACTIVE")
                except asyncio.CancelledError:
                    pass
            self.voice_activation_task = asyncio.create_task(activate_voice(), name="agent-voice-activation")
        elif explicit_interrupt:
            # 显式打断边界使动作失效，再取消 Agent（代理）侧工作。
            self.current_action = None
            self.cancel_active_request(interrupt_reason)
            # The controller owns USER_INTERRUPTED.  This callback is invoked
            # after that boundary and only invalidates Agent-side work.
            self.output.cancel_all(
                "interrupted" if interrupt_reason == "user_interrupted" else interrupt_reason
            )
            if speaking is True and self.voice_activation_task:
                self.voice_activation_task.cancel()
        if self.episode_id and speaking is not None and speaking != previous:
            await self.observe("SPEECH_STARTED" if speaking else "SPEECH_ENDED")
        if speaking is False and previous is True:
            release = getattr(self.output, "release_held", None)
            if callable(release):
                release()

    def _meaningful_pending(self):
        return {
            kind for kind in self.pending
            if kind not in AgentDecisionScheduler.NON_SEMANTIC_EVENTS
            and not (isinstance(kind, str) and kind.startswith("TTS_"))
        }

    def discard_speaker_vetoed_input(self, turn_id: str) -> None:
        """Remove provisional text after a confirmed pre-dispatch veto."""
        self.inputs.pop(turn_id, None)
        if self.last_turn_id == turn_id:
            self.pending.difference_update({
                "USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED", "USER_INPUT_REFINED",
            })
        if self._pending_transcript_turn_id == turn_id:
            self._pending_transcript_turn_id = None

    def _dispatch_allowed(self, *, respect_backoff=True):
        if self.closed or not self.episode_id:
            return False
        if self.input_scope_closed:
            # Microphone input was stopped; the request already in flight may
            # finish, but no new turn may be dispatched for it.
            return False
        if self.active_request is not None:
            return False
        if respect_backoff and self.backoff_until > time.monotonic():
            return False
        return True

    def _busy_reason(self):
        if self.active_request is not None:
            return "active_request"
        if self.backoff_until > time.monotonic():
            return "backoff"
        return "not_allowed"

    async def _start_dispatch(self, kinds, *, recovery_signal=None, candidate=None):
        """Create one request task without ever waiting for its result."""

        recovery = recovery_signal is not None
        dispatch_kind = "normal"
        prompt_kind = "normal"
        dispatch_context = None
        if isinstance(candidate, dict):
            dispatch_kind = str(candidate.get("dispatch_kind") or candidate.get("prompt_kind") or "system_check")
            prompt_kind = str(candidate.get("prompt_kind") or dispatch_kind)
            dispatch_context = dict(candidate)
        elif recovery_signal and recovery_signal.system_check_id:
            dispatch_kind = "system_check_recovery"
            prompt_kind = "exception_recovery"
            dispatch_context = dict(recovery_signal.dispatch_context or {})
            origin_prompt_kind = self._recovery_origin_prompt_kind(recovery_signal)
            dispatch_context.update({
                "system_check_id": recovery_signal.system_check_id,
                "dispatch_kind": dispatch_kind,
                "prompt_kind": prompt_kind,
                "origin_prompt_kind": origin_prompt_kind,
                "owner_scope": "system_check",
            })
        elif "LLM_RETRY" in kinds and self._retry_system_check is not None:
            # 传输重试仍走原单循环；新普通事件优先，旧系统评估不能冒充新用户请求。
            ordinary = set(kinds) - self.scheduler.NON_SEMANTIC_EVENTS - {"LLM_RETRY"}
            if ordinary or self._system_check_stale_reason(self._retry_system_check):
                self._discard_secondary_retry()
                kinds = set(kinds) - {"LLM_RETRY"}
                if not ordinary:
                    return True  # 已消费过期重试事件，不创建请求。
            else:
                dispatch_context = dict(self._retry_system_check)
                dispatch_kind = "system_check_retry"
                prompt_kind = "secondary_mailbox"
                dispatch_context.update(dispatch_kind=dispatch_kind, prompt_kind=prompt_kind)

        scheduled_wakes = []
        if candidate is None and not recovery and "SCHEDULED_WAKE" in kinds:
            scheduled_wakes = list(self._scheduled_wakes)
            if scheduled_wakes:
                dispatch_context = {
                    "dispatch_kind": "scheduled_wake",
                    "scheduled_wakes": scheduled_wakes,
                }
                dispatch_kind = "scheduled_wake"

        if not self._dispatch_allowed(respect_backoff=not recovery):
            return False
        # A system check is only valid while the ordinary mailbox is empty.
        # Re-check after each await before creating the provider task.
        if candidate is not None and self._meaningful_pending():
            return False

        self.agent_step_id = uuid4().hex
        self.state = "INVOKING"
        identity = self.identity()
        source_turn_id = (
            recovery_signal.source_turn_id if recovery_signal and recovery_signal.source_turn_id
            else self.last_turn_id
        )
        if (
            not recovery and candidate is None
            and {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}.intersection(kinds)
            and self._pending_transcript_turn_id
        ):
            source_turn_id = self._pending_transcript_turn_id
        if candidate is None and self.defer_silence_until_output:
            final_input_dispatch = bool(
                {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}.intersection(kinds)
            )
            recovery_or_retry = bool(recovery or "LLM_RETRY" in kinds)
            if final_input_dispatch or recovery_or_retry:
                self._bind_pending_silence_request(
                    identity, source_turn_id, allow_rebind=recovery_or_retry,
                )
        system_check_id = None
        if candidate is not None:
            system_check_id = f"system-check-{uuid4().hex}"
            source_turn_id = (
                candidate.get("source_id")
                or candidate.get("speech_turn_id")
                or candidate.get("anchor_turn_id")
            )
            if not source_turn_id:
                return False
            dispatch_context["system_check_id"] = system_check_id
            dispatch_context["speech_turn_id"] = source_turn_id
            dispatch_context["dispatch_kind"] = dispatch_kind
            dispatch_context["prompt_kind"] = prompt_kind
            dispatch_context["owner_scope"] = "system_check"
            dispatch_context["user_speech_epoch"] = self._user_speech_epoch
            dispatch_context["user_speaking_at_dispatch"] = self.user_speaking is True
            if prompt_kind == "secondary_mailbox":
                dispatch_context["shadow_activation_id"] = self.ledger.shadow_activation_id  # 进度所属激活。
                dispatch_context["shadow_progress_generation"] = self.ledger._shadow_progress_generation  # 进度作用域代次。
            self._system_check_turn_ids.add(system_check_id)
            if prompt_kind == "final_silence":
                self._final_system_checks[system_check_id] = dict(dispatch_context)
        elif recovery_signal and recovery_signal.system_check_id:
            system_check_id = recovery_signal.system_check_id
            source_turn_id = (
                dispatch_context.get("speech_turn_id")
                if isinstance(dispatch_context, dict) else recovery_signal.source_turn_id
            ) or recovery_signal.source_turn_id
            if isinstance(dispatch_context, dict):
                dispatch_context["speech_turn_id"] = source_turn_id
                dispatch_context.setdefault("user_speech_epoch", self._user_speech_epoch)
                dispatch_context.setdefault(
                    "user_speaking_at_dispatch", self.user_speaking is True,
                )
            self._system_check_turn_ids.add(system_check_id)
        elif self._is_system_check_context(dispatch_context):
            system_check_id = dispatch_context["system_check_id"]
            source_turn_id = dispatch_context["speech_turn_id"]
            self._system_check_turn_ids.add(system_check_id)
        reasoning_effort = "low" if self.reasoning_mode == "deep" else "none"
        if prompt_kind == "kws_wake":
            reasoning_effort = "none"
        self._identity_turns[identity] = source_turn_id
        self.active_request_identity = identity
        self.active_request_source_turn_id = source_turn_id
        self.active_request_kinds = set(kinds)
        self.active_recovery = recovery_signal
        self.active_dispatch_context = dispatch_context
        self.scheduler_last_dispatch_at = time.monotonic()
        pending_wait_ms = (
            round(max(0.0, self.scheduler_last_dispatch_at - self._pending_since) * 1000, 3)
            if self._pending_since is not None and candidate is None and not recovery else None
        )
        if not recovery:
            await self.log(
                "SCHEDULER_TICK", identity=identity, turn_id=source_turn_id,
                events=sorted(kinds) if candidate is None else ["SYSTEM_CHECK"],
                mode=prompt_kind, dispatch_kind=dispatch_kind,
                prompt_kind=prompt_kind,
                scheduler_generation=(dispatch_context.get("generation")
                                      if isinstance(dispatch_context, dict) else self.scheduler.generation),
                window_index=(dispatch_context.get("window_index")
                              if isinstance(dispatch_context, dict) else None),
                system_check_id=system_check_id,
            )
            if {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}.intersection(kinds):
                current_updates = self.inputs.get(source_turn_id or "", {}).get(
                    "transcript_updates", []
                )
                available = [
                    item.get("kind") for item in current_updates
                    if isinstance(item, dict) and item.get("kind") in {"asr_final", "refinement"}
                ]
                triggered_by = [
                    name for event, name in (
                        ("USER_INPUT_FINALIZED", "asr_final"),
                        ("USER_INPUT_REFINED", "refinement"),
                    ) if event in kinds
                ]
                await self.log(
                    "TRANSCRIPT_UPDATES_DISPATCH", identity=identity,
                    turn_id=source_turn_id,
                    transcript_updates_available=available,
                    transcript_updates_triggered_by=triggered_by,
                    same_turn=True,
                )
                logger.info(
                    "transcript.dispatch turn_id=%s available=%s triggered_by=%s same_turn=true",
                    source_turn_id, available, triggered_by,
                )
        await self.log("LLM_DISPATCHED", identity=identity,
                       mode="exception_recovery" if recovery else dispatch_kind,
                       dispatch_kind=("normal" if recovery else dispatch_kind),
                       prompt_kind=("normal" if recovery else prompt_kind),
                       system_check_id=system_check_id,
                       window_index=(dispatch_context.get("window_index")
                                    if isinstance(dispatch_context, dict) else None),
                       silence_elapsed_ms=(dispatch_context.get("silence_elapsed_ms")
                                           if isinstance(dispatch_context, dict) else None),
                       pending_wait_ms=pending_wait_ms,
                       events=sorted(kinds), reasoning_effort=reasoning_effort,
                       error_id=(recovery_signal.error_id if recovery_signal else None))
        if recovery and recovery_signal is not None:
            await self.log("LLM_RECOVERY_STARTED", identity=identity,
                           error_id=recovery_signal.error_id, code=recovery_signal.code)
        if (
            candidate is not None
            and int(candidate.get("generation") or 0) != self.scheduler.generation
        ):
            await self.log(
                "SCHEDULER_FORCED_RESOLVED",
                identity=identity,
                turn_id=source_turn_id,
                window_index=candidate.get("window_index"),
                scheduler_generation=candidate.get("generation"),
                current_scheduler_generation=self.scheduler.generation,
                resolution="superseded_by_new_generation",
                reason="scheduler_generation_changed_before_task_start",
                system_check_id=system_check_id,
            )
            self._identity_turns.pop(identity, None)
            if system_check_id:
                self._system_check_turn_ids.discard(system_check_id)
                self._final_system_checks.pop(system_check_id, None)
            self.active_request_identity = None
            self.active_request_source_turn_id = None
            self.active_request_kinds = set()
            self.active_recovery = None
            self.active_dispatch_context = None
            self.state = "WAITING_EVENT" if self.episode_id else "IDLE"
            return False
        if candidate is not None and self._meaningful_pending():
            self._identity_turns.pop(identity, None)
            if system_check_id:
                self._system_check_turn_ids.discard(system_check_id)
                self._final_system_checks.pop(system_check_id, None)
            self.active_request_identity = None
            self.active_request_source_turn_id = None
            self.active_request_kinds = set()
            self.active_recovery = None
            self.active_dispatch_context = None
            self.state = "WAITING_EVENT" if self.episode_id else "IDLE"
            return False
        if candidate is not None and prompt_kind == "secondary_mailbox" and (
            self.user_speaking is True or not self._secondary_snapshot()
            or self.scheduler.final_pending or not self.scheduler.armed
            or self._system_check_is_stale(dispatch_context)
        ):
            # 日志和界面回调会让出执行权，派发前必须再次确认讲话、信箱及最终期限。
            self._system_check_turn_ids.discard(system_check_id)
            self._identity_turns.pop(identity, None)
            self.active_request_identity = None
            self.active_request_source_turn_id = None
            self.active_request_kinds = set()
            self.active_dispatch_context = None
            self.state = "WAITING_EVENT"
            return False
        self.active_request = asyncio.create_task(
            self._step(identity, kinds, recovery=recovery,
                       recovery_signal=recovery_signal,
                       reasoning_effort=reasoning_effort,
                       dispatch_context=dispatch_context),
            name=f"agent-step-{identity[1]}",
        )
        if candidate is not None and prompt_kind in {"secondary_mailbox", "final_silence"}:
            self.scheduler.mark_forced_dispatched(candidate)
        if scheduled_wakes:
            count = min(len(scheduled_wakes), len(self._scheduled_wakes))
            del self._scheduled_wakes[:count]
        self.active_request_count += 1
        if not recovery and candidate is None:
            self._pending_since = None
        if (not recovery and candidate is None
                and self._pending_transcript_turn_id == source_turn_id):
            self._pending_transcript_turn_id = None
        self.max_request_concurrency = max(self.max_request_concurrency, self.active_request_count)
        return True

    async def _start_recovery(self):
        if not self.recovery_queue or not self._dispatch_allowed(respect_backoff=False):
            return False
        signal = self.recovery_queue.popleft()
        signal.consumed = True
        if self._recovery_origin_prompt_kind(signal) == "secondary_mailbox" and self._system_check_is_stale(signal.dispatch_context):
            self._retry_secondary_by_turn.pop(signal.source_turn_id or "", None)
            self._retry_shadow_information_by_turn.pop(signal.source_turn_id or "", None)
            self._system_check_turn_ids.discard(signal.system_check_id)
            await self.log("LLM_RECOVERY_SUPERSEDED", turn_id=signal.source_turn_id,
                           error_id=signal.error_id, reason=self._system_check_stale_reason(signal.dispatch_context),
                           cancelled_request=False)
            return False
        scheduler_generation = self.scheduler.generation
        recovery_queue_wait_ms = round(
            max(0.0, time.monotonic() - signal.queued_at_monotonic) * 1000, 3,
        )
        await self.log("LLM_RECOVERY_STARTING", turn_id=signal.source_turn_id,
                       error_id=signal.error_id, code=signal.code,
                       owner_scope=signal.owner_scope,
                       system_check_id=signal.system_check_id,
                       recovery_queue_wait_ms=recovery_queue_wait_ms)
        if scheduler_generation != self.scheduler.generation:
            # The provider task has not been created yet.  Preserve the
            # existing pre-dispatch ownership rule without cancelling any
            # in-flight request.
            await self.log("LLM_RECOVERY_SUPERSEDED", turn_id=signal.source_turn_id,
                           error_id=signal.error_id, reason="new_input_during_dispatch",
                           cancelled_request=False)
            return False
        started = await self._start_dispatch(
            {"EXCEPTION_RECOVERY", signal.code}, recovery_signal=signal,
        )
        if not started:
            self.recovery_queue.appendleft(signal)
        return started

    async def _on_heartbeat(self, heartbeat_at, *, pulse: HeartbeatPulse | None = None,
                            delivered_at: float | None = None):
        if pulse is not None:
            delivered_at = time.monotonic() if delivered_at is None else delivered_at
            # 使用回调实际到达时间；暂停后直接跨到当前检查点，不能重放旧二等请求。
            heartbeat_at = max(heartbeat_at, delivered_at)
        previous_window = self.scheduler.window_index
        was_final_pending = self.scheduler.final_pending
        candidate = self.scheduler.heartbeat(heartbeat_at)
        current_window = self.scheduler.window_index
        secondary_due = self.scheduler.secondary_due(previous_window)
        if pulse is not None:
            delivered_at = time.monotonic() if delivered_at is None else delivered_at
            delivery_delay_ms = max(0.0, (delivered_at - pulse.scheduled_at) * 1000)
            self._heartbeat_delivered += 1
            self._heartbeat_coalesced += pulse.coalesced_count
            if delivery_delay_ms >= self.scheduler.interval * 1000:
                await self.log(
                    "SCHEDULER_HEARTBEAT_DELAYED",
                    episode_generation=pulse.episode_generation,
                    pulse_sequence=pulse.sequence,
                    scheduled_at=pulse.scheduled_at,
                    delivered_at=delivered_at,
                    delivery_delay_ms=round(delivery_delay_ms, 3),
                    coalesced_count=pulse.coalesced_count,
                    scheduler_generation=self.scheduler.generation,
                    previous_window=previous_window,
                    current_window=current_window,
                    selected_checkpoint=(candidate.get("window_index") if candidate else None),
                )
            if pulse.coalesced_count:
                await self.log(
                    "SCHEDULER_HEARTBEAT_COALESCED",
                    episode_generation=pulse.episode_generation,
                    pulse_sequence=pulse.sequence,
                    scheduled_at=pulse.scheduled_at,
                    delivered_at=delivered_at,
                    delivery_delay_ms=round(delivery_delay_ms, 3),
                    coalesced_count=pulse.coalesced_count,
                    scheduler_generation=self.scheduler.generation,
                    previous_window=previous_window,
                    current_window=current_window,
                    selected_checkpoint=(candidate.get("window_index") if candidate else None),
                )
        if self.scheduler.final_pending and not was_final_pending:
            await self.log("SCHEDULER_FINAL_PENDING",
                           window_index=self.scheduler.FINAL_WINDOW,
                           reason="final_window_crossed",
                           scheduler_generation=self.scheduler.generation)
        immediate = self._immediate_system_check
        if immediate is not None:
            if int(immediate.get("generation") or 0) != self.scheduler.generation:
                self._immediate_system_check = None
            elif self.active_request is not None or not self._dispatch_allowed():
                return
            elif await self._start_dispatch({"SYSTEM_CHECK"}, candidate=immediate):
                if self._immediate_system_check is immediate:
                    self._immediate_system_check = None
                return
            else:
                return
        if candidate is not None and self._retry_system_check is not None and not (
            self._meaningful_pending() - {"LLM_RETRY"}
        ):
            # 二等评估的重试仍属系统工作；最终静默检查到期时优先结算和交接。
            self._discard_secondary_retry()
        meaningful = self._meaningful_pending()
        if meaningful:
            if not self.scheduler.normal_due(meaningful, at_heartbeat=True):
                return
            if self.active_request is not None:
                return
            if not self._dispatch_allowed():
                return
            kinds = set(self.pending)
            user_kinds = kinds.intersection({
                "USER_SEGMENT_UPDATED", "USER_INPUT_FINALIZED", "USER_INPUT_REFINED",
            })
            source_turn_id = self._pending_transcript_turn_id or self.last_turn_id
            if user_kinds and source_turn_id and callable(self.admit_user_turn):
                try:
                    admitted = bool(self.admit_user_turn(source_turn_id))
                except Exception:
                    # An optional speaker failure must preserve the ordinary
                    # fail-open request policy.
                    logger.exception("speaker.admission_check_failed")
                    admitted = True
                if not admitted:
                    self.inputs.pop(source_turn_id, None)
                    self.pending.difference_update(user_kinds)
                    kinds.difference_update(user_kinds)
                    await self.log("SPEAKER_REQUEST_VETOED", turn_id=source_turn_id,
                                   events=sorted(user_kinds))
                    if not kinds or not self._meaningful_pending():
                        self.pending.clear()
                        self._pending_since = None
                        return
            self.pending.clear()
            if not await self._start_dispatch(kinds):
                self.pending.update(kinds)
            return

        # Activity-only entries are consumed at the heartbeat but never become
        # an LLM request or a Voice-model context item.
        self.pending.clear()
        self._pending_since = None
        if candidate is not None:
            if candidate.get("prompt_kind") == "final_silence":
                readiness = self.can_dispatch_final_silence
                if callable(readiness):
                    try:
                        if not readiness():
                            return
                    except Exception:
                        logger.exception("agent.final_silence_readiness_check_failed")
                        return
            if not self._dispatch_allowed():
                return
            if await self._start_dispatch({"SYSTEM_CHECK"}, candidate=candidate):
                await self.log(
                    "SCHEDULER_FINAL_DISPATCHED",
                    turn_id=candidate.get("speech_turn_id") or candidate.get("anchor_turn_id"),
                    window_index=candidate.get("window_index"),
                    scheduler_generation=candidate.get("generation"),
                    system_check_id=(self.active_dispatch_context.get("system_check_id")
                                     if isinstance(self.active_dispatch_context, dict) else None),
                )
                return
            return

        if not secondary_due or not self._secondary_snapshot():
            return
        if self.user_speaking is True:
            self._secondary_skipped_speaking += 1
            await self.log("SECONDARY_MAILBOX_SKIPPED", reason="user_speaking",
                           window_index=current_window, pending_count=len(self._secondary_pending))
            return
        if not self._dispatch_allowed():
            self._secondary_skipped_busy += 1
            await self.log("SECONDARY_MAILBOX_SKIPPED", reason=self._busy_reason(),
                           window_index=current_window, pending_count=len(self._secondary_pending))
            return
        secondary_candidate = self.scheduler.secondary_candidate(now=heartbeat_at)
        if secondary_candidate is not None and await self._start_dispatch(
            {"SYSTEM_CHECK"}, candidate=secondary_candidate,
        ):
            self._secondary_dispatched += 1
            await self.log("SECONDARY_MAILBOX_DISPATCHED", window_index=current_window,
                           pending_count=len(self._secondary_pending),
                           system_check_id=self.active_dispatch_context.get("system_check_id"))

    async def _settle_final_system_check(self, system_check_id: str) -> None:
        context = self._final_system_checks.pop(system_check_id, None)
        if context is None or system_check_id in self._settled_final_system_checks:
            return
        self._settled_final_system_checks.add(system_check_id)
        if int(context.get("generation") or 0) != self.scheduler.generation:
            return
        cleared = self.ledger.clear_shadow_progress()
        self._secondary_cleared_final += cleared
        if cleared:
            await self.log("SECONDARY_MAILBOX_CLEARED_FINAL", count=cleared,
                           scheduler_generation=self.scheduler.generation)
        callback = self.on_final_system_check_settled
        if not callable(callback):
            return
        result = callback(dict(context))
        if asyncio.iscoroutine(result):
            await result

    async def _finish_active_request(self, request):
        """Consume a completed request task without blocking the heartbeat."""

        if request is None or request is not self.active_request:
            return
        identity = self.active_request_identity or self.identity()
        source_turn_id = self.active_request_source_turn_id
        recovery_signal = self.active_recovery
        recovery = recovery_signal is not None
        dispatch_context = self.active_dispatch_context
        system_check_id = (
            dispatch_context.get("system_check_id")
            if self._is_system_check_context(dispatch_context) else None
        )

        def discard_shadow_snapshot(snapshot) -> None:
            if not isinstance(snapshot, dict):
                return
            version = snapshot.get("version")
            if isinstance(version, int) and not isinstance(version, bool):
                self.ledger.consume_shadow_information(version)

        settle_pending_silence = False
        step_outcome = None
        try:
            step_outcome = request.result()
            settle_pending_silence = step_outcome in {
                "completed", "system_check_stale", "terminal_failure",
                "recovery_exhausted",
            }
            if recovery and recovery_signal is not None:
                await self.log(
                    "LLM_RECOVERY_COMPLETED" if step_outcome == "completed" else "LLM_RECOVERY_FAILED",
                    identity=identity, error_id=recovery_signal.error_id,
                    outcome=step_outcome or "cancelled",
                )
            if step_outcome == "completed":
                self._transport_retry_attempts_by_turn.pop(source_turn_id or "", None)
                self._retry_shadow_information_by_turn.pop(source_turn_id or "", None)
                self._retry_secondary_by_turn.pop(source_turn_id or "", None)
            elif recovery:
                retry_snapshot = self._retry_shadow_information_by_turn.pop(source_turn_id or "", None)
                discard_shadow_snapshot(
                    recovery_signal.shadow_information if recovery_signal is not None
                    else retry_snapshot
                )
            self.backoff_step = 0
            self.backoff_until = 0.0
        except asyncio.CancelledError:
            if self.closed:
                raise
            discard_shadow_snapshot(
                self._retry_shadow_information_by_turn.pop(source_turn_id or "", None)
            )
        except Exception as exc:
            # 最终静默及关键词唤醒检查的传输失败直接结算；二等检查可限次重试，
            # 但必须保留原系统评估作用域，不能退化为普通用户请求。
            failed_prompt_kind = (
                dispatch_context.get("origin_prompt_kind") or dispatch_context.get("prompt_kind")
                if isinstance(dispatch_context, dict) else None
            )  # 失败请求的原始场景；协议恢复失败后传输重试也必须保留。
            terminal_system_check = bool(
                system_check_id and isinstance(dispatch_context, dict)
                and failed_prompt_kind in {"final_silence", "kws_wake"}
            )
            retryable = bool(getattr(exc, "retryable", False)) and not terminal_system_check
            await self.log(
                "AGENT_ERROR", error=str(exc)[:300],
                error_layer=getattr(exc, "error_layer", "runtime"),
                error_code=getattr(exc, "error_code", type(exc).__name__),
                retryable=retryable,
            )
            if retryable:
                retry_key = source_turn_id or ""
                attempts = self._transport_retry_attempts_by_turn.get(retry_key, 0)
                limit = int(getattr(self.provider, "transport_retry_limit", 2))
                if attempts >= limit:
                    discard_shadow_snapshot(
                        self._retry_shadow_information_by_turn.pop(retry_key, None)
                    )
                    settle_pending_silence = True
                    await self.log("LLM_TRANSPORT_RETRY_EXHAUSTED", identity=identity,
                                   turn_id=source_turn_id, attempts=attempts, limit=limit)
                    await self.emit("error", {
                        "conversation_id": identity[0], "request_id": identity[1],
                        "turn_id": source_turn_id, "input_id": source_turn_id,
                        "code": "LLM_PROVIDER_UNAVAILABLE",
                        "message": "模型服务暂时不可用，本轮未完成；请稍后继续。",
                        "recoverable": True, "retry_after_event": True,
                    })
                    latest = self.inputs.get(source_turn_id or "", {})
                    if latest.get("final") and not self.output.active:
                        await self._emit_turn_settled(identity, source_turn_id,
                                                      "provider_unavailable")
                    self.backoff_step = 0
                    self.backoff_until = 0.0
                else:
                    self._transport_retry_attempts_by_turn[retry_key] = attempts + 1
                    waits = (0.8, 1.6, 3.2, 8.0)
                    self.backoff_step = min(attempts + 1, len(waits))
                    wait_seconds = waits[self.backoff_step - 1]
                    self.backoff_until = time.monotonic() + wait_seconds
                    await self.log("LLM_BACKOFF", seconds=wait_seconds, step=self.backoff_step)
                    self.pending.add("LLM_RETRY")
                    if system_check_id and failed_prompt_kind == "secondary_mailbox":
                        self._retry_system_check = dict(dispatch_context)
                    if self.queue.empty():
                        self.queue.put_nowait(True)
            else:
                discard_shadow_snapshot(
                    self._retry_shadow_information_by_turn.pop(source_turn_id or "", None)
                )
                settle_pending_silence = True
                self.backoff_step = 0
                self.backoff_until = 0.0
            latest = self.inputs.get(source_turn_id or "", {})
            if latest.get("final") and not self.output.active and not retryable:
                await self._emit_turn_settled(identity, source_turn_id, "agent_error")
        finally:
            secondary_retry_pending = bool(
                self._retry_system_check and "LLM_RETRY" in self.pending
                and self._retry_system_check.get("system_check_id") == system_check_id
            )
            if "LLM_RETRY" not in self.pending and not any(
                signal.source_turn_id == source_turn_id for signal in self.recovery_queue
            ):
                self._retry_secondary_by_turn.pop(source_turn_id or "", None)
                if self._retry_system_check and self._retry_system_check.get("system_check_id") == system_check_id:
                    self._retry_system_check = None
            if settle_pending_silence:
                await self._settle_pending_silence_after_request(
                    identity, source_turn_id,
                    step_outcome or "request_settled",
                )
            active_request_duration_ms = (
                round(max(0.0, time.monotonic() - self.request_started_at) * 1000, 3)
                if self.request_started_at is not None else None
            )
            await self.log(
                "LLM_ACTIVE_REQUEST_FINISHED",
                identity=identity,
                turn_id=source_turn_id,
                system_check_id=system_check_id,
                active_request_duration_ms=active_request_duration_ms,
                active_request_concurrency_peak=self.max_request_concurrency,
            )
            self.request_started_at = None
            self.active_request_count = max(0, self.active_request_count - 1)
            self.active_request = None
            self.active_request_identity = None
            self.active_request_source_turn_id = None
            self.active_request_kinds = set()
            self.active_recovery = None
            self.active_dispatch_context = None
            self.last_llm_completed_at = time.monotonic()
            if (system_check_id in self._system_check_turn_ids
                    and system_check_id not in self._system_check_output_turn_ids
                    and not secondary_retry_pending
                    and not any(
                        signal.system_check_id == system_check_id
                        for signal in self.recovery_queue
                    )):
                # No speech candidate was created by this system-check.  If
                # one was created, output_completed owns the final cleanup;
                # keeping the ID until then prevents a cancellation race from
                # losing owner_scope=system_check.
                self._system_check_turn_ids.discard(system_check_id)
                await self._settle_final_system_check(system_check_id)
        self.state = "WAITING_EVENT" if self.episode_id else "IDLE"
        await self.emit("agent.episode", self.snapshot(include_history=False))

    async def _run(self):
        """Serialise runtime state while an independent thread owns the clock."""

        wake_task = asyncio.create_task(self.queue.get(), name="agent-episode-wakeup")
        heartbeat_task = asyncio.create_task(
            self.heartbeat_queue.get(), name="agent-heartbeat-wakeup",
        )
        try:
            while not self.closed and self.episode_id is not None:
                active = self.active_request
                waiters = {wake_task, heartbeat_task}
                if active is not None:
                    waiters.add(active)
                done, _ = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED,
                )
                wake_ready = wake_task in done
                if wake_ready:
                    try:
                        wake_task.result()
                    except asyncio.CancelledError:
                        if self.closed:
                            raise
                    wake_task = asyncio.create_task(self.queue.get(), name="agent-episode-wakeup")
                heartbeat_pulse = None
                if heartbeat_task in done:
                    heartbeat_pulse = heartbeat_task.result()
                    heartbeat_task = asyncio.create_task(
                        self.heartbeat_queue.get(), name="agent-heartbeat-wakeup",
                    )
                if active is not None and active in done and active is self.active_request:
                    await self._finish_active_request(active)

                # Recovery is the existing bounded exception path.  It shares
                # this loop but may begin as soon as its queue is observed;
                # ordinary and forced dispatches remain heartbeat-bound.
                await self._start_recovery()

                # If a provider task finished exactly as the timeout fired,
                # consume it before considering a new heartbeat dispatch.
                if self.active_request is not None and self.active_request.done():
                    await self._finish_active_request(self.active_request)

                if heartbeat_pulse is not None:
                    if heartbeat_pulse.episode_generation != self._heartbeat_episode_generation:
                        await self.log(
                            "SCHEDULER_HEARTBEAT_STALE",
                            episode_generation=heartbeat_pulse.episode_generation,
                            pulse_sequence=heartbeat_pulse.sequence,
                        )
                    else:
                        delivered_at = time.monotonic()
                        await self._on_heartbeat(
                            heartbeat_pulse.scheduled_at,
                            pulse=heartbeat_pulse,
                            delivered_at=delivered_at,
                        )
        except asyncio.CancelledError:
            pass
        finally:
            if wake_task is not None and not wake_task.done():
                wake_task.cancel()
                await asyncio.gather(wake_task, return_exceptions=True)
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if self.runner is asyncio.current_task():
                if self._heartbeat_clock is not None:
                    await self._stop_heartbeat_clock()
                self.runner = None









    @staticmethod
    def _model_speak_state(snapshot, *, conversation_id=None, turn_id=None):
        """Expose speech semantics, not queue implementation identifiers."""

        if not isinstance(snapshot, dict):
            return {"state": "empty", "owner_scope": "none", "continuation_mode": "none"}
        state = str(snapshot.get("queue_status") or "empty")
        if state == "empty":
            value = {"state": "empty", "owner_scope": "none", "continuation_mode": "none"}
            # With no active transaction the only honest location of "where the
            # speech continues" is what the listener already heard.  Without
            # this the authoritative state block reported nothing at exactly
            # the moment a barge-in interrupted the answer, which is why a
            # model re-issued the whole sentence from the first character.
            delivery = _heard_delivery(snapshot.get("delivery"))
            if delivery is not None:
                value["delivery"] = delivery
            return value
        same_turn = bool(
            conversation_id and turn_id
            and snapshot.get("conversation_id") == conversation_id
            and snapshot.get("turn_id") == turn_id
        )
        text = str(snapshot.get("text") or "")
        raw_parts = snapshot.get("_model_text_parts")
        part_names = (
            "committed_prefix", "current_clause", "prefetched_clause", "revisable_tail",
        )
        parts_valid = bool(snapshot.get("_model_text_parts_valid", True))
        if isinstance(raw_parts, dict) and all(isinstance(raw_parts.get(key), str) for key in part_names):
            parts = {key: raw_parts[key] for key in part_names}
            parts_valid = parts_valid and "".join(parts.values()) == text
            sent_chars = snapshot.get("sent_chars")
            if isinstance(sent_chars, int):
                locked = parts["committed_prefix"] + parts["current_clause"] + parts["prefetched_clause"]
                parts_valid = parts_valid and 0 <= sent_chars <= len(text) and locked == text[:sent_chars]
        else:
            sent_chars = snapshot.get("sent_chars")
            if isinstance(sent_chars, int) and 0 <= sent_chars <= len(text):
                parts = {
                    "committed_prefix": text[:sent_chars],
                    "current_clause": "",
                    "prefetched_clause": "",
                    "revisable_tail": text[sent_chars:],
                }
            else:
                parts = {
                    "committed_prefix": text,
                    "current_clause": "",
                    "prefetched_clause": "",
                    "revisable_tail": "",
                }
                parts_valid = False
        if not parts_valid:
            parts = {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }
        continuation_mode = (
            "rewrite_tail"
            if same_turn and parts_valid and bool(snapshot.get("rewritable"))
            else "none"
        )
        value = {
            "state": state,
            "owner_scope": "current_turn" if same_turn else "previous_turn",
            "continuation_mode": continuation_mode,
            "text_parts": parts,
        }
        delivery = snapshot.get("delivery")
        if isinstance(delivery, dict) and delivery.get("speech_id") == snapshot.get("speech_id"):
            heard = delivery.get("heard_text")
            if isinstance(heard, str) and heard:
                value["delivery"] = {"heard_text": heard}
        return value

    async def _apply_revision_tail(self, identity, turn_id, snapshot, tail, *, emotion,
                                   dispatch_context=None):
        """revision（尾巴修订）只修改请求快照仍匹配的活动事务，拒绝过期边界。"""
        proposal = str(tail or "")
        if not proposal.strip():
            return None
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        current = getattr(self.output, "speak_snapshot", {})
        current = current if isinstance(current, dict) else {}
        speech_id = snapshot.get("speech_id")
        reason = None
        if (not speech_id or snapshot.get("conversation_id") != identity[0]
                or snapshot.get("turn_id") != turn_id):
            reason = "no_owned_transaction"
        elif any(current.get(key) != snapshot.get(key) for key in (
                "speech_id", "output_generation", "sent_chars", "text", "text_revision")):
            reason = "request_boundary_changed"
        elif not snapshot.get("rewritable") or not current.get("rewritable"):
            reason = "revision_window_closed"
        active = str(snapshot.get("text") or "")
        sent_chars = snapshot.get("sent_chars")
        if not isinstance(sent_chars, int):
            reason = reason or "missing_confirmation_boundary"
            sent_chars = len(active)
        locked = active[:sent_chars]
        system_check_id = (dispatch_context.get("system_check_id")
                           if isinstance(dispatch_context, dict) else None)
        if reason is None and locked.endswith(proposal):
            self.minor_protocol_feedback.append({
                "field": "speak.revision", "provided_chars": len(proposal),
                "applied": "noop_tail_already_locked",
                "hint": "该尾巴已锁定无需修订；没有新内容时使用 wait（等待）。",
            })
            await self.log("SPEECH_REVISION_NOOP", identity=identity, turn_id=turn_id,
                           speech_id=speech_id, locked_chars=len(locked),
                           new_tail_chars=len(proposal), reason="tail_already_locked",
                           system_check_id=system_check_id)
            return "noop"
        rewrite = getattr(self.output, "rewrite_unsent", None)
        if reason is None and callable(rewrite):
            rewritten = await rewrite(
                identity, turn_id, speech_id=speech_id, text=proposal,
                expected_generation=snapshot.get("output_generation"),
                expected_sent_chars=sent_chars, expected_text=active,
                expected_text_revision=snapshot.get("text_revision", 0),
            )
            if rewritten is not None:
                await self.log("SPEECH_REVISED", identity=identity, turn_id=turn_id,
                               path="rewrite", speech_id=rewritten.speech_id,
                               locked_chars=len(locked), new_tail_chars=len(proposal),
                               system_check_id=system_check_id)
                return rewritten
        reason = reason or "confirmation_boundary_changed"
        self.minor_protocol_feedback.append({
            "field": "speak.revision", "applied": "rejected_boundary_changed",
            "reason": reason,
            "hint": "修订未受理：请求中的尾巴边界已失效或不可修订。核对最新输出状态；硬替换使用带文本的 cancel（取消）。",
        })
        await self.log("SPEECH_REVISION_REJECTED", identity=identity, turn_id=turn_id,
                       speech_id=speech_id, reason=reason, context_locked_chars=sent_chars,
                       apply_locked_chars=current.get("sent_chars"),
                       system_check_id=system_check_id)
        return None

    def _completed_speech_guard(self, identity, turn_id, *, allow_same_turn=False):
        """Return a settled delivery usable as an exact-text baseline.

        A preceding turn is observational only: it may protect against an
        accidental copied prefix, but it never grants same-turn continuation.
        A completed speech from this turn is eligible only during an explicit
        ASR transcript reassessment.  That narrow opt-in lets a late
        refinement suppress an exact replay or create a suffix successor
        without making arbitrary heartbeats extend old speech.

        An interrupted delivery qualifies through its ``heard_text`` instead
        of its full text: the listener provably received that prefix, so a
        re-issued answer may resume there rather than from the first
        character.  The partial remainder is never treated as delivered.
        """

        item = getattr(self.output, "last_completed_speech", None)
        conversation_id = identity[0] if isinstance(identity, (tuple, list)) and identity else None
        if not isinstance(item, dict):
            return None
        heard = item.get("heard_text")
        if (
            not conversation_id or not turn_id
            or item.get("conversation_id") != conversation_id
            or not isinstance(heard, str) or not heard
        ):
            return None
        completed = (item.get("status") == "completed"
                     and item.get("delivery_status") == "full")
        interrupted = (item.get("delivery_status") == "partial"
                       and item.get("termination") in {"interrupted", "replaced"})
        if not completed and not interrupted:
            return None
        same_turn = item.get("turn_id") == turn_id
        if same_turn and not allow_same_turn:
            return None
        return {
            "text": heard,
            "speech_id": item.get("speech_id"),
            "generation": item.get("output_generation"),
            "source": ("completed_same_turn_guard" if same_turn
                       else "completed_guard" if completed
                       else "heard_prefix_guard"),
        }





    def _provider_stream(self, messages, *, reasoning_effort=None):
        if getattr(self.provider, "supports_decision_schema", False):
            return self.provider.stream_events(
                messages,
                json_mode=True,
                schema=self._decision_schema(),
                reasoning_effort=reasoning_effort,
            )
        return self.provider.stream_events(messages, json_mode=True)

    async def _submit_shadow_actions(self, identity, turn_id, actions: tuple[Action, ...]) -> bool:
        """Submit content-free wakes after the whole decision validates."""

        for action in actions:
            try:
                if action.name != "request_shadow" or self.shadow_waker is None:
                    raise ValueError(f"action not permitted: {action.name}")
                parse_shadow_wake(action.arguments)
                current = self.inputs.get(turn_id, {})
                merge = getattr(self.ledger, "project_shadow_current_turn", None)
                if callable(merge) and isinstance(current, dict):
                    merge(
                        turn_id, str(current.get("text") or ""),
                        str(current.get("refined_text") or ""),
                    )
                result = self.shadow_waker()
                if asyncio.iscoroutine(result):
                    result = await result
                await self.log(
                    "SHADOW_WAKE", identity=identity, turn_id=turn_id, result=str(result),
                )
            except (ValueError, TypeError) as exc:
                await self.log("SHADOW_WAKE_ERROR", identity=identity, turn_id=turn_id,
                               error=str(exc)[:300])
                return False
        return True

    def _track_background_tool(self, task: asyncio.Task) -> None:
        self._background_tools.add(task)
        task.add_done_callback(self._background_tools.discard)

    async def _dispatch_actions_v06(self, identity, turn_id, envelope: DecisionEnvelope,
                                    *, system_check=None, system_check_id=None,
                                    system_check_prompt_kind=None) -> None:
        actions = envelope.actions
        if system_check is None:
            system_check = turn_id in self._system_check_turn_ids
        # 二等检查沿用普通动作能力；最终静默及关键词唤醒检查仍禁止动作。
        # SHADOW_INFORMATION is an ordinary dispatch and follows the normal
        # advertised-action policy.
        if system_check and system_check_prompt_kind != "secondary_mailbox" and actions:
            await self.log(
                "SYSTEM_CHECK_ACTION_REJECTED", identity=identity, turn_id=turn_id,
                system_check_id=system_check_id or turn_id, owner_scope="system_check",
                prompt_kind=system_check_prompt_kind,
                action_names=[action.name for action in actions],
                action_count=len(actions),
            )
            return
        shadow_actions = tuple(action for action in actions if action.name == "request_shadow")
        invalid = tuple(action for action in actions if action.name != "request_shadow")
        if invalid:
            raise ProtocolError("unsupported action: " + ", ".join(action.name for action in invalid))
        if shadow_actions:
            self._track_background_tool(asyncio.create_task(
                self._submit_shadow_actions(identity, turn_id, shadow_actions),
                name=f"shadow-wake-{turn_id}",
            ))

    async def accept_shadow_event(self, event: ShadowEvent) -> None:
        """Project a Bridge event into the Voice ledger and scheduler.

        The event's lifecycle fields are local control metadata; its text is
        explicitly marked untrusted by the ledger before a later main-model
        request sees it.
        """
        if self.closed or (event.kind == "progress" and self.input_scope_closed):
            return
        await self.ensure_episode()
        previous_version = self.ledger._shadow_progress_version
        if not self.ledger.commit_shadow_event(event):
            await self.log("SHADOW_EVENT_DUPLICATE", event_id=event.event_id, event_kind=event.kind)
            return
        if self.ledger._shadow_progress_version != previous_version:
            self._secondary_enqueued += 1
            await self.log("SECONDARY_MAILBOX_ENQUEUED", source=event.source,
                           version=self.ledger._shadow_progress_version,
                           pending_count=len(self._secondary_pending))
        await self.log("SHADOW_EVENT", event_id=event.event_id, event_kind=event.kind)
        if event.kind == "information":
            if self.ledger.shadow_information_snapshot() is not None:
                self._wake("SHADOW_INFORMATION")
            else:
                await self.log("SHADOW_INFORMATION_ALREADY_EVALUATED", event_id=event.event_id)
        elif event.kind in {"input_required", "result", "failed"}:
            self._wake("SHADOW_EVENT_PRIORITY")

    @property
    def _secondary_pending(self) -> dict[str, int]:
        """待评估来源与 version（版本）的诊断视图；账本拥有唯一信箱状态。"""
        return {source: state["version"] for source, state in self._secondary_snapshot().items()}

    def _secondary_snapshot(self) -> dict[str, dict]:
        return self.ledger.shadow_progress_snapshot()

    def _consume_secondary(self, captured: dict[str, dict], *, prompt_kind: str) -> list[str]:
        consumed = self.ledger.consume_shadow_progress(captured)
        if prompt_kind == "final_silence":
            self._secondary_cleared_final += len(consumed)
        elif prompt_kind == "secondary_mailbox":
            self._secondary_consumed_check += len(consumed)
        else:
            self._secondary_consumed_normal += len(consumed)
        return consumed

    def _discard_secondary_retry(self) -> None:
        """丢弃已被新输入或生命周期边界替换的系统评估重试。"""
        context = self._retry_system_check
        self._retry_system_check = None
        if context is None:
            return
        self.pending.discard("LLM_RETRY")
        source = context.get("speech_turn_id") or ""
        self._retry_secondary_by_turn.pop(source, None)
        self._retry_shadow_information_by_turn.pop(source, None)
        self._transport_retry_attempts_by_turn.pop(source, None)
        self._system_check_turn_ids.discard(context.get("system_check_id"))

    async def accept_durable_voice_event(self, event: DurableVoiceEvent) -> None:
        """Commit a scheduled durable event independently of Shadow information."""

        await self.ensure_episode()
        self.ledger.commit_semantic_event("durable_voice_event", {
            "event_id": event.event_id,
            "text": event.text,
            "source_session_id": event.source_session_id,
            "payload": dict(event.payload),
        })
        await self.log("DURABLE_VOICE_EVENT", event_id=event.event_id)
        self._wake("DURABLE_VOICE_EVENT")

    async def _apply_decision_v06(self, identity, turn_id, envelope: DecisionEnvelope,
                                  *, dispatch_context=None):
        return await apply_decision_v06(
            self, identity, turn_id, envelope, dispatch_context=dispatch_context,
        )

    @staticmethod
    def _request_prompt_kind(dispatch_context, *, recovery=False):
        """_request_prompt_kind（请求场景）：恢复请求沿用原始评估场景。"""
        if not isinstance(dispatch_context, dict):
            return "normal"
        selected = (dispatch_context.get("origin_prompt_kind")
                    if recovery else dispatch_context.get("prompt_kind"))
        return str(selected or "normal")

    def _request_secondary_context(self, source_turn_id, kinds, *, recovery=False,
                                   recovery_signal=None):
        """_request_secondary_context（有效二等上下文）：重试保留原请求快照。"""
        retry_key = source_turn_id or ""
        if recovery and recovery_signal is not None and recovery_signal.secondary_context is not None:
            return self.ledger.valid_shadow_progress(recovery_signal.secondary_context)
        if ("LLM_RETRY" in kinds and retry_key in self._retry_secondary_by_turn
                and not (set(kinds) - self.scheduler.NON_SEMANTIC_EVENTS - {"LLM_RETRY"})):
            return self.ledger.valid_shadow_progress(self._retry_secondary_by_turn[retry_key])
        return self._secondary_snapshot()

    async def _step(self, identity, kinds, *, recovery=False, recovery_signal=None,
                    reasoning_effort=None, dispatch_context=None):
        """有效请求才冻结语音提交；输出应用及所有退出路径均释放边界。"""
        if kinds != {"DEFERRED_OUTPUT"} and (self.closed or not self.episode_id):
            return
        source_turn_id = self._identity_turns.get(identity, self.last_turn_id)
        prompt_kind = self._request_prompt_kind(dispatch_context, recovery=recovery)
        if prompt_kind == "secondary_mailbox" and (
                self._system_check_is_stale(dispatch_context)
                or (not recovery and not self._request_secondary_context(
                    source_turn_id, kinds, recovery=recovery, recovery_signal=recovery_signal))):
            return "system_check_stale"
        boundary = getattr(self.output, "inference_boundary", None)
        if boundary is None or kinds == {"DEFERRED_OUTPUT"}:
            return await self._step_with_speech_boundary(
                identity, kinds, recovery=recovery, recovery_signal=recovery_signal,
                reasoning_effort=reasoning_effort, dispatch_context=dispatch_context,
            )
        async with boundary.request(identity):
            return await self._step_with_speech_boundary(
                identity, kinds, recovery=recovery, recovery_signal=recovery_signal,
                reasoning_effort=reasoning_effort, dispatch_context=dispatch_context,
            )

    async def _step_with_speech_boundary(self, identity, kinds, *, recovery=False,
                                       recovery_signal=None, reasoning_effort=None,
                                       dispatch_context=None):
        """The V0.6 content-envelope request path (no native provider tools)."""
        if kinds == {"DEFERRED_OUTPUT"}:
            await self._normalize_deferred_outputs()
            return "deferred_output"
        # 协议恢复会复用 identity（请求标识）；本次流仍须重新通过完整决策校验。
        self._validated_envelope_identity = None
        source_turn_id = self._identity_turns.get(identity, self.last_turn_id)
        system_check = self._is_system_check_context(dispatch_context)
        system_check_id = (
            str(dispatch_context.get("system_check_id")) if system_check else None
        )
        prompt_kind = self._request_prompt_kind(dispatch_context, recovery=recovery)
        same_turn_transcript_reassessment = bool(
            {"USER_INPUT_FINALIZED", "USER_INPUT_REFINED"}.intersection(kinds)
        )
        if prompt_kind == "secondary_mailbox" and self._system_check_is_stale(dispatch_context):
            return "system_check_stale"
        retry_key = source_turn_id or ""
        secondary_context = self._request_secondary_context(
            source_turn_id, kinds, recovery=recovery, recovery_signal=recovery_signal,
        )
        if prompt_kind == "secondary_mailbox" and not secondary_context and not recovery:
            return "system_check_stale"
        if recovery and recovery_signal is not None and recovery_signal.shadow_information:
            shadow_information = dict(recovery_signal.shadow_information)
        elif "LLM_RETRY" in kinds and retry_key in self._retry_shadow_information_by_turn:
            shadow_information = dict(self._retry_shadow_information_by_turn[retry_key])
        else:
            shadow_information = self.ledger.shadow_information_snapshot()
        messages = self.messages(
            kinds,
            recovery_signal=recovery_signal,
            prompt_kind=prompt_kind,
            force_context=dispatch_context,
            current_turn_id=source_turn_id,
            shadow_information=shadow_information,
            secondary_context=secondary_context,
        )
        request_speak_snapshot = dict(self._last_model_speak_snapshot)
        request_speak_context = dict(self._last_model_speak_state)
        if self.closed or not self.episode_id:
            return
        messages, compacted = self._fit_context(messages, [])
        # Cache history is intentionally never summarized, dropped or
        # rewritten.  ``over_budget_base`` is an observation, not a
        # compaction, so the UI must not claim otherwise.
        self.context_compacted = any(
            value for key, value in compacted.items() if key != "over_budget_base"
        )
        self.context_compaction = compacted if self.context_compacted else {}
        audit_schema = (
            self._decision_schema()
            if getattr(self.provider, "supports_decision_schema", False)
            else {}
        )
        audit_capture_started = time.perf_counter()
        request_context_digest = capture_context_snapshot(
            self.provider,
            messages,
            audit_schema,
            conversation_id=identity[0],
            request_id=identity[1],
            turn_id=source_turn_id,
            events=sorted(kinds),
            mode=("system_check" if system_check
                  else "exception_recovery" if recovery else "normal"),
            dispatch_kind=(dispatch_context.get("dispatch_kind")
                           if isinstance(dispatch_context, dict)
                           else "exception_recovery" if recovery else "normal"),
            prompt_kind=prompt_kind,
            scheduler_generation=(dispatch_context.get("generation")
                                  if isinstance(dispatch_context, dict) else self.scheduler.generation),
            window_index=(dispatch_context.get("window_index")
                          if isinstance(dispatch_context, dict) else None),
            system_check_id=(dispatch_context.get("system_check_id")
                             if isinstance(dispatch_context, dict) else None),
            owner_scope=("system_check" if system_check else None),
            reasoning_effort=reasoning_effort or "none",
            context_compaction=self.context_compaction,
            context_token_estimate=self.context_token_estimate,
        )
        if shadow_information and self.ledger.consume_shadow_information(int(shadow_information["version"])):
            await self.log(
                "SHADOW_INFORMATION_CONSUMED", identity=identity,
                turn_id=source_turn_id, prompt_kind=prompt_kind,
                source=shadow_information["source"],
                captured_version=shadow_information["version"],
            )
        # 冻结与审计成功后才保存和确认版本；构建失败或旧版本确认不能丢失新状态。
        self._retry_secondary_by_turn[retry_key] = secondary_context
        if shadow_information is not None:
            self._retry_shadow_information_by_turn[retry_key] = dict(shadow_information)
        consumed_secondary = self._consume_secondary(secondary_context, prompt_kind=prompt_kind)
        if consumed_secondary:
            await self.log("SECONDARY_MAILBOX_CONSUMED", identity=identity,
                           turn_id=source_turn_id, prompt_kind=prompt_kind,
                           sources=consumed_secondary,
                           captured_version=max(state["version"] for state in secondary_context.values()),
                           pending_count=len(self._secondary_pending))
        audit_capture_ms = round((time.perf_counter() - audit_capture_started) * 1000, 2)
        self.request_count += 1
        self.request_started_at = time.monotonic()
        await self.log("AGENT_INVOKED", events=sorted(kinds),
                       mode=("system_check" if system_check
                             else "exception_recovery" if recovery else "normal"),
                       dispatch_kind=("normal" if recovery else prompt_kind),
                       prompt_kind=prompt_kind,
                       system_check_id=(dispatch_context.get("system_check_id")
                                        if isinstance(dispatch_context, dict) else None))
        await self.log(
            "LLM_CONTEXT_CAPTURED",
            identity=identity,
            turn_id=source_turn_id,
            context_digest=request_context_digest,
            message_count=len(messages),
            context_chars=self.context_chars,
            context_token_estimate=self.context_token_estimate,
            context_compacted=self.context_compacted,
            audit_capture_ms=audit_capture_ms,
        )
        speak_context = request_speak_context
        # One request-scoped view of the accepted transaction.  ``messages``
        # already projected it, so the streaming fallbacks must judge against
        # the same frontier the model was shown.
        snapshot = request_speak_snapshot
        speak_parts = speak_context.get("text_parts")
        if not isinstance(speak_parts, dict):
            speak_parts = {}
        committed_prefix = str(speak_parts.get("committed_prefix") or "")
        current_clause = str(speak_parts.get("current_clause") or "")
        prefetched_clause = str(speak_parts.get("prefetched_clause") or "")
        revisable_tail = str(speak_parts.get("revisable_tail") or "")
        context_locked_chars = len(committed_prefix) + len(current_clause) + len(prefetched_clause)
        delivery = speak_context.get("delivery")
        heard_text = str(delivery.get("heard_text") or "") if isinstance(delivery, dict) else ""

        await self.log(
            "LLM_SPEAK_CONTEXT", identity=identity, turn_id=source_turn_id,
            speak_state=speak_context.get("state"),
            owner_scope=speak_context.get("owner_scope"),
            continuation_mode=speak_context.get("continuation_mode"),
            committed_prefix_chars=len(committed_prefix),
            current_clause_chars=len(current_clause),
            prefetched_clause_chars=len(prefetched_clause),
            revisable_tail_chars=len(revisable_tail),
            planned_chars=(len(committed_prefix) + len(current_clause)
                           + len(prefetched_clause) + len(revisable_tail)),
            locked_chars=context_locked_chars,
            heard_chars=len(heard_text),
            active_same_turn=bool(speak_context.get("owner_scope") == "current_turn"),
        )
        await self.emit("llm.context", {"messages": messages, "message_count": len(messages),
                                         "context_chars": self.context_chars,
                                         "context_limit_tokens": self.options.max_context_tokens,
                                         "context_token_estimate": self.context_token_estimate,
                                         "provider_input_tokens": self.provider_input_tokens,
                                         "provider_cached_tokens": self.provider_cached_tokens,
                                         "provider_cache_miss_tokens": self.provider_cache_miss_tokens,
                                         "provider_cache_hit_ratio": self.provider_cache_hit_ratio,
                                         "context_digest": request_context_digest,
                                         "conversation_id": identity[0], "request_id": identity[1]})
        parser = SpeechDecisionEnvelopeStream(
            allow_empty_control_text=getattr(self.provider, "transport", "chat_completions") == "responses"
        )
        reasoning: list[str] = []
        content_chars = 0
        finished = False
        provisional = ""
        admission = StreamedSpeechAdmissionState(
            identity, source_turn_id, snapshot, context_locked_chars,
            system_check_id, same_turn_transcript_reassessment, dispatch_context,
        )
        speech_handle_finished = False
        validation = "strict_valid"
        audit_outcome_recorded = False
        stale_reported = False
        early_stream_abort = False
        early_stream_abort_non_whitespace_chars: int | None = None

        async def report_stale() -> None:
            nonlocal stale_reported
            if stale_reported:
                return
            stale_reported = True
            await self.log(
                "SYSTEM_CHECK_STALE",
                identity=identity,
                turn_id=source_turn_id,
                system_check_id=system_check_id,
                speech_turn_id=source_turn_id,
                scheduler_generation=(dispatch_context.get("generation")
                                      if isinstance(dispatch_context, dict) else None),
                current_scheduler_generation=self.scheduler.generation,
                stale_reason=self._system_check_stale_reason(dispatch_context),
                system_check_user_speech_epoch=(dispatch_context.get("user_speech_epoch")
                                                if isinstance(dispatch_context, dict) else None),
                current_user_speech_epoch=self._user_speech_epoch,
                user_speaking_at_dispatch=(dispatch_context.get("user_speaking_at_dispatch")
                                           if isinstance(dispatch_context, dict) else None),
                stale_system_check_output_discarded=True,
            )

        def record_audit_outcome(outcome: str, **details) -> None:
            nonlocal audit_outcome_recorded
            if audit_outcome_recorded:
                return
            audit_outcome_recorded = True
            full_details = {
                **details,
                "output_text": parser.raw,
                "reasoning_text": "".join(reasoning),
            }
            capture_request_outcome(
                conversation_id=identity[0],
                request_id=identity[1],
                turn_id=source_turn_id,
                wire_sha256=request_context_digest,
                outcome=outcome,
                details=full_details,
            )

        async def finish_streamed_speech() -> None:
            nonlocal speech_handle_finished
            if admission.speech_handle is not None and not speech_handle_finished:
                try:
                    await admission.speech_handle.finish()
                except ValueError:
                    # A rewritten transaction is the already-running handle
                    # from the previous decision.  It may settle between the
                    # replacement and this close, and a closed producer is not
                    # a protocol error in the model's output.
                    pass
                speech_handle_finished = True


        try:
            async with aclosing(self._provider_stream(messages, reasoning_effort=reasoning_effort)) as stream:
                async for item in stream:
                    if self.closed or not self.episode_id:
                        return
                    if item["type"] == "finish":
                        finished = True
                        metrics = item.get("stream_metrics")
                        if isinstance(metrics, dict):
                            await self.log(
                                "SSE_STREAM_INTEGRITY", identity=identity,
                                turn_id=source_turn_id, **metrics,
                            )
                    elif item["type"] == "reasoning":
                        reasoning.append(item["text"])
                        self.last_reasoning = {"conversation_id": identity[0], "request_id": identity[1],
                                               "text": "".join(reasoning)}
                        await self.emit("agent.reasoning", dict(self.last_reasoning))
                    elif item["type"] == "usage":
                        usage = item.get("usage") or {}
                        if isinstance(usage.get("prompt_tokens"), int):
                            self.provider_prompt_tokens = usage["prompt_tokens"]
                            self.provider_input_tokens = usage["prompt_tokens"]
                        if isinstance(usage.get("cached_tokens"), int):
                            self.provider_cached_tokens = usage["cached_tokens"]
                        if isinstance(usage.get("cache_miss_tokens"), int):
                            self.provider_cache_miss_tokens = usage["cache_miss_tokens"]
                        elif isinstance(self.provider_input_tokens, int) and isinstance(self.provider_cached_tokens, int):
                            self.provider_cache_miss_tokens = max(0, self.provider_input_tokens - self.provider_cached_tokens)
                        if isinstance(self.provider_input_tokens, int) and self.provider_input_tokens > 0 and isinstance(self.provider_cached_tokens, int):
                            self.provider_cache_hit_ratio = self.provider_cached_tokens / self.provider_input_tokens
                        await self.log(
                            "LLM_USAGE", identity=identity, turn_id=source_turn_id,
                            provider_input_tokens=self.provider_input_tokens,
                            provider_cached_tokens=self.provider_cached_tokens,
                            provider_cache_miss_tokens=self.provider_cache_miss_tokens,
                            provider_cache_hit_ratio=self.provider_cache_hit_ratio,
                        )
                    elif item["type"] == "content":
                        content_chars += len(item["text"])
                        if content_chars > 32768:
                            raise EnvelopeError("decision content too large")
                        text_delta = parser.feed(item["text"])
                        if parser.definitive_non_json_prefix():
                            early_stream_abort = True
                            early_stream_abort_non_whitespace_chars = sum(
                                not char.isspace() for char in parser.raw
                            )
                            raise EnvelopeError(
                                "streamed decision is prose instead of JSON"
                            )
                        if text_delta:
                            provisional += text_delta
                            if system_check and self._system_check_is_stale(dispatch_context):
                                await report_stale()
                                continue
                            prefix = parser.speak_prefix()
                            if prefix is not None and prefix.state == "speak":
                                await queue_streamed_speech(self, admission, prefix, provisional)
                            # Keep this event as a model-stream diagnostic for
                            # existing clients.  The actual queue truth is
                            # speech.queued/speech.delta.
                            await self.emit("speak.preview", {
                                "conversation_id": identity[0],
                                "request_id": identity[1],
                                "turn_id": source_turn_id,
                                "state": "speak",
                                "text_delta": text_delta,
                                "text": provisional,
                                "committed": False,
                            })
            if not finished:
                raise EnvelopeError("model stream missing finish")
            responses_transport = getattr(self.provider, "transport", "chat_completions") == "responses"
            try:
                envelope = parser.finish()
            except (EnvelopeError, ValueError, json.JSONDecodeError) as strict_exc:
                if not responses_transport:
                    raise
                try:
                    compatible = parse_compatible_decision_envelope(
                        parser.raw, allow_empty_control_text=True,
                    )
                except (EnvelopeError, ValueError, json.JSONDecodeError):
                    # Keep the original contract failure actionable for the
                    # recovery prompt; compatibility rejection itself adds no
                    # useful model-facing information.
                    raise strict_exc
                envelope = compatible.envelope
                validation = "compat_recovered"
                await self.log(
                    "LLM_OUTPUT_COMPAT_RECOVERED",
                    identity=identity,
                    turn_id=source_turn_id,
                    repairs=list(compatible.repairs),
                    raw_chars=len(parser.raw),
                    classification="bounded_json_repair",
                    **json_structure_fingerprint(parser.raw),
                )
            if responses_transport:
                envelope = self._normalize_schema_envelope(envelope)
            schema_diagnostics = provider_schema_diagnostics(parser.raw, audit_schema)
            if system_check and self._system_check_is_stale(dispatch_context):
                await report_stale()
                if admission.speech_handle is not None:
                    cancel = getattr(self.output, "cancel", None)
                    if callable(cancel):
                        cancel(getattr(admission.speech_handle, "speech_id", None), "superseded_system_check")
                record_audit_outcome(
                    "system_check_stale",
                    content_chars=content_chars,
                    system_check_id=system_check_id,
                    actions_executed=0,
                )
                return "system_check_stale"
            await self._record_emotion_normalizations(parser, identity=identity, turn_id=source_turn_id)
            self._validated_envelope_identity = identity
            if admission.speech_handle is not None:
                admission.speech_handle.envelope_validated = True
            if (admission.speech_handle is not None
                    and getattr(admission.speech_handle, "reason", None) == "passive_quiet"):
                handle = admission.speech_handle
                handle.text = envelope.speak.text[admission.prefix_chars_stripped:]
                handle.finished = True
                remainder, heard = self.output.deferred_remainder(handle, handle.text)
                self.defer_speech(admission.speech_handle.speech_id,
                                  remainder, heard_text=heard)
            if admission.speech_handle is not None:
                await finish_streamed_speech()
                await self.log("SPEAK_STATE_CHANGED", identity=identity, turn_id=source_turn_id,
                               state=envelope.speak.state,
                               queue_status=getattr(self.output, "speak_snapshot", {}).get("queue_status", "queued"),
                               action_count=len(envelope.actions))
            else:
                previous_reassessment = self._applying_transcript_reassessment
                previous_admission = getattr(self, "_applying_speech_admission", None)
                self._applying_speech_admission = admission
                self._applying_transcript_reassessment = same_turn_transcript_reassessment
                try:
                    if system_check:
                        await self._apply_decision_v06(
                            identity, source_turn_id, envelope, dispatch_context=dispatch_context,
                        )
                    else:
                        await self._apply_decision_v06(identity, source_turn_id, envelope)
                finally:
                    self._applying_transcript_reassessment = previous_reassessment
                    self._applying_speech_admission = previous_admission
            if system_check:
                await self._dispatch_actions_v06(
                    identity, source_turn_id, envelope,
                    system_check=True,
                    system_check_id=system_check_id,
                    system_check_prompt_kind=(
                        (dispatch_context.get("origin_prompt_kind")
                         or dispatch_context.get("prompt_kind"))
                        if isinstance(dispatch_context, dict) else None
                    ),
                )
            else:
                await self._dispatch_actions_v06(identity, source_turn_id, envelope)
            await self._normalize_deferred_outputs()
            record_audit_outcome(
                validation,
                state=envelope.speak.state,
                action_count=len(envelope.actions),
                content_chars=content_chars,
                reasoning_effort=reasoning_effort or "none",
                local_contract_valid=True,
                action_adapter_valid=(True if not envelope.actions else None),
                **schema_diagnostics,
            )
            await self.log(
                "LLM_REQUEST_COMPLETED",
                identity=identity,
                turn_id=source_turn_id,
                context_digest=request_context_digest,
                validation=validation,
                state=envelope.speak.state,
                action_count=len(envelope.actions),
                content_chars=content_chars,
                local_contract_valid=True,
                action_adapter_valid=(True if not envelope.actions else None),
                **schema_diagnostics,
            )
            await self.emit("speak.preview", {
                "conversation_id": identity[0], "request_id": identity[1],
                "turn_id": source_turn_id, "status": "committed",
            })
            return "completed"
        except asyncio.CancelledError as exc:
            record_audit_outcome(
                "cancelled",
                cancel_reason=str(exc) or "cancelled",
                content_chars=content_chars,
                finished=finished,
                reasoning_effort=reasoning_effort or "none",
            )
            raise
        except (EnvelopeError, ValueError, json.JSONDecodeError) as exc:
            error_text = str(exc)[:300]
            failure_diagnostics = {
                **json_structure_fingerprint(parser.raw),
                **envelope_error_diagnostics(parser.raw, exc, audit_schema),
                "early_stream_abort": early_stream_abort,
                "early_stream_abort_non_whitespace_chars": (
                    early_stream_abort_non_whitespace_chars
                ),
            }
            if system_check and self._system_check_is_stale(dispatch_context):
                await report_stale()
                if admission.speech_handle is not None:
                    cancel = getattr(self.output, "cancel", None)
                    if callable(cancel):
                        cancel(getattr(admission.speech_handle, "speech_id", None), "superseded_system_check")
                record_audit_outcome(
                    "system_check_stale",
                    content_chars=content_chars,
                    system_check_id=system_check_id,
                    actions_executed=0,
                    **failure_diagnostics,
                )
                return "system_check_stale"
            recovery_code = (
                "LLM_EMPTY_OUTPUT" if not parser.raw.strip()
                else "TOOL_PROTOCOL_ERROR" if any(marker in error_text for marker in ("action", "tool"))
                else "SPEAK_ENVELOPE_INVALID"
            )
            # The streamed ``speak`` prefix is deliberately validated apart
            # from the complete decision envelope.  A later malformed tail or
            # invalid action must not cut off speech that is already safely in
            # the local text-only queue.  This is a one-way compatibility
            # downgrade: no unvalidated action or control state is applied,
            # and the next ordinary heartbeat receives a chance to make a
            # fresh complete decision.
            speak_prefix = None
            if admission.speech_handle is not None:
                try:
                    speak_prefix = parser.speak_prefix()
                except (EnvelopeError, ValueError, json.JSONDecodeError):
                    speak_prefix = None
            if speak_prefix is not None and speak_prefix.state == "speak" and speak_prefix.text.strip():
                try:
                    await finish_streamed_speech()
                except Exception:
                    # The queue has already accepted text, but a failure to
                    # close its producer is not a reason to execute actions
                    # or start a protocol-recovery loop.
                    pass
                workspace = getattr(admission.speech_handle, "workspace", None)
                if workspace is not None and isinstance(getattr(workspace, "timings", None), dict):
                    workspace.timings["partial_speech_protocol_error"] = True
                    workspace.timings["speak_only_compat_accepted"] = True
                invalid_components = (
                    ["actions"] if recovery_code == "TOOL_PROTOCOL_ERROR"
                    else ["envelope_tail"]
                )
                await self.log(
                    "LLM_OUTPUT_COMPAT_SPEAK_ACCEPTED",
                    identity=identity,
                    turn_id=source_turn_id,
                    error=error_text,
                    recovery_code=recovery_code,
                    speak_chars=len(speak_prefix.text),
                    queued_speech_id=getattr(admission.speech_handle, "speech_id", None),
                    invalid_components=invalid_components,
                    actions_executed=0,
                    next_dispatch="next_triggered_request",
                    **failure_diagnostics,
                )
                await self.emit("speak.preview", {
                    "conversation_id": identity[0], "request_id": identity[1],
                    "turn_id": source_turn_id, "status": "committed_speak_only",
                    "invalid_components": invalid_components,
                    "actions_executed": 0,
                })
                record_audit_outcome(
                    "compat_speak_only",
                    recovery_code=recovery_code,
                    recovery_outcome="not_queued",
                    speak_chars=len(speak_prefix.text),
                    invalid_components=invalid_components,
                    actions_executed=0,
                    content_chars=content_chars,
                    **failure_diagnostics,
                )
                return "compat_speak_only"
            if admission.speech_handle is not None:
                try:
                    await finish_streamed_speech()
                except Exception:
                    pass
            if recovery_code == "LLM_EMPTY_OUTPUT":
                # Preserve the frozen V0.6 aggregate while exposing the new,
                # actionable empty-output classification.
                await self.log("SPEAK_ENVELOPE_INVALID", identity=identity,
                               turn_id=source_turn_id, error=error_text,
                               classification="LLM_EMPTY_OUTPUT", legacy_alias=True)
            await self.log(recovery_code, identity=identity, turn_id=source_turn_id,
                           error=error_text,
                           classification=("empty_output" if recovery_code == "LLM_EMPTY_OUTPUT"
                                           else "envelope_validation"),
                           raw_chars=len(parser.raw),
                            non_whitespace_chars=len(parser.raw.strip()),
                            provisional_chars=len(provisional),
                            partial_speech=admission.speech_handle is not None,
                            **failure_diagnostics)
            if admission.speech_handle is not None:
                workspace = getattr(admission.speech_handle, "workspace", None)
                if workspace is not None and isinstance(getattr(workspace, "timings", None), dict):
                    workspace.timings["partial_speech_protocol_error"] = True
            if admission.speech_handle is not None:
                # A legal prefix can start speaking before a later JSON error.
                # Stop that provisional generation; the superseded output is
                # intentionally never resumed.
                cancel = getattr(self.output, "cancel", None)
                if callable(cancel):
                    cancel(getattr(admission.speech_handle, "speech_id", None), "cancelled")
            queued = await self._queue_recovery(recovery_code, error_text,
                                                identity=identity, turn_id=source_turn_id)
            outcome = "recovery_queued" if queued is not None else "recovery_exhausted"
            record_audit_outcome(
                "invalid_envelope",
                recovery_code=recovery_code,
                recovery_outcome=outcome,
                content_chars=content_chars,
                **failure_diagnostics,
            )
            return outcome
        except Exception as exc:
            terminal_kind = getattr(exc, "terminal_kind", None)
            if terminal_kind is None:
                error_layer = getattr(exc, "error_layer", "runtime")
                error_code = getattr(exc, "error_code", type(exc).__name__)
                record_audit_outcome(
                    "transport_error" if error_layer == "transport" else "runtime_error",
                    error_type=type(exc).__name__,
                    error_layer=error_layer,
                    error_code=error_code,
                    retryable=bool(getattr(exc, "retryable", False)),
                    status_code=getattr(exc, "status_code", None),
                    received_content=getattr(exc, "received_content", None),
                    content_chars=content_chars,
                )
                raise
            reason = getattr(exc, "reason", None)
            stream_metrics = getattr(exc, "stream_metrics", None)
            if isinstance(stream_metrics, dict):
                await self.log(
                    "SSE_STREAM_INTEGRITY", identity=identity,
                    turn_id=source_turn_id, **stream_metrics,
                )
            if admission.speech_handle is not None:
                try:
                    await finish_streamed_speech()
                except Exception:
                    pass
                cancel = getattr(self.output, "cancel", None)
                if callable(cancel):
                    cancel(getattr(admission.speech_handle, "speech_id", None), "cancelled")
            if terminal_kind == "response.incomplete" and reason == "max_output_tokens":
                await self.log("RESPONSES_INCOMPLETE", identity=identity,
                               turn_id=source_turn_id, reason=reason,
                               partial_speech=admission.speech_handle is not None)
                queued = await self._queue_recovery(
                    "RESPONSES_INCOMPLETE", "max_output_tokens",
                    identity=identity, turn_id=source_turn_id,
                )
                outcome = "recovery_queued" if queued is not None else "recovery_exhausted"
                record_audit_outcome(
                    "provider_incomplete",
                    terminal_kind=terminal_kind,
                    reason=reason,
                    recovery_outcome=outcome,
                    content_chars=content_chars,
                )
                return outcome
            if terminal_kind == "response.completed" and reason == "stream_final_mismatch":
                await self.log("SPEAK_ENVELOPE_INVALID", identity=identity,
                               turn_id=source_turn_id,
                               classification="stream_final_mismatch",
                               partial_speech=admission.speech_handle is not None)
                queued = await self._queue_recovery(
                    "SPEAK_ENVELOPE_INVALID", "stream_final_mismatch",
                    identity=identity, turn_id=source_turn_id,
                )
                outcome = "recovery_queued" if queued is not None else "recovery_exhausted"
                record_audit_outcome(
                    "stream_final_mismatch",
                    terminal_kind=terminal_kind,
                    reason=reason,
                    recovery_outcome=outcome,
                    content_chars=content_chars,
                )
                return outcome

            filtered = terminal_kind == "response.incomplete" and reason == "content_filter"
            code = "LLM_OUTPUT_FILTERED" if filtered else "LLM_PROVIDER_UNAVAILABLE"
            message = (
                "本轮内容无法生成；请换一种说法。" if filtered else
                "模型服务暂时不可用，本轮未完成；请稍后继续。"
            )
            await self.log(code, identity=identity, turn_id=source_turn_id,
                           terminal_kind=terminal_kind, reason=reason)
            await self.emit("error", {
                "conversation_id": identity[0], "request_id": identity[1],
                "turn_id": source_turn_id, "input_id": source_turn_id,
                "code": code, "message": message, "recoverable": True,
                "retry_after_event": True,
            })
            latest = self.inputs.get(source_turn_id or "", {})
            if latest.get("final") and not self.output.active:
                await self._emit_turn_settled(identity, source_turn_id,
                                              "output_filtered" if filtered else "provider_unavailable")
            record_audit_outcome(
                "output_filtered" if filtered else "provider_unavailable",
                terminal_kind=terminal_kind,
                reason=reason,
                content_chars=content_chars,
            )
            return "terminal_failure"
        finally:
            if not audit_outcome_recorded:
                record_audit_outcome(
                    "abandoned",
                    content_chars=content_chars,
                    finished=finished,
                )
            if admission.speech_handle is not None and not speech_handle_finished:
                try:
                    await finish_streamed_speech()
                except Exception:
                    pass
            if admission.first_speech_text_at is not None:
                workspace = getattr(admission.speech_handle, "workspace", None)
                if workspace is not None and isinstance(getattr(workspace, "timings", None), dict):
                    finish_at = time.monotonic()
                    workspace.timings["first_speech_text_to_json_finish_ms"] = round(
                        (finish_at - admission.first_speech_text_at) * 1000
                    )
                    request_at = workspace.timings.get("llm_request")
                    if request_at is not None:
                        workspace.timings["llm_request_to_first_speech_text_ms"] = round(
                            (admission.first_speech_text_at - request_at) * 1000
                        )
            self.last_llm_completed_at = time.monotonic()

    async def output_completed(self, record):
        speech_id = record.get("speech_id")
        if not isinstance(speech_id, str) or not speech_id:
            return
        if speech_id in self._completed_speech_ids:
            return
        self._completed_speech_ids.add(speech_id)
        self._forget_speech_transaction(record)
        if len(self._completed_speech_ids) > self.options.output_limit * 4:
            self._completed_speech_ids = set(list(self._completed_speech_ids)[-self.options.output_limit * 2:])
        system_context = self._system_check_speeches.pop(speech_id, None)
        system_check_id = (
            system_context.get("system_check_id")
            if isinstance(system_context, dict) else None
        )
        if not system_check_id and record.get("turn_id") in self._system_check_turn_ids:
            # Backward-compatible completion for transactions created before
            # speech/system ownership was split.
            system_check_id = record.get("turn_id")
            system_context = {}
        if system_check_id:
            # This additive marker lets the controller keep delivery in the
            # assistant ledger without treating a system check as a user turn.
            record["owner_scope"] = "system_check"
            record["system_check_id"] = system_check_id
            record["speech_turn_id"] = record.get("turn_id")
            record["scheduler_generation"] = system_context.get("scheduler_generation")
        self.outputs.append(record)
        record_identity = (record.get("conversation_id") or record.get("episode_id"),
                           record.get("request_id") or record.get("trace_id") or record.get("agent_step_id"))
        current_output = record_identity == (self.episode_id, self.agent_step_id)
        if current_output:
            self.current_action = None
        await self.emit("speech.completed", record)
        turn_latency = record.get("turn_latency")
        if isinstance(turn_latency, dict):
            latency_payload = {
                "turn_id": record.get("turn_id"),
                "input_id": record.get("input_id") or record.get("turn_id"),
                "speech_id": speech_id,
                "turn": dict(turn_latency),
                "latency": dict(record.get("latency") or {}),
            }
            if record.get("owner_scope") == "system_check":
                latency_payload.update({
                    "owner_scope": "system_check",
                    "system_check_id": record.get("system_check_id"),
                })
            await self.emit("turn.latency", latency_payload)
        status = record.get("status")
        trace_kind = ("TTS_COMPLETED" if status == "completed" else
                      "TTS_REPLACED" if status == "replaced" else
                      "TTS_INTERRUPTED" if status in {"interrupted", "scope_closed", "stopped", "reset", "cancelled"} else
                      "TTS_FAILED")
        await self.log(trace_kind,
                       identity=record_identity, turn_id=record.get("turn_id"),
                       speech_id=speech_id, status=status)
        if record.get("owner_scope") == "system_check":
            system_check_id = str(record.get("system_check_id") or "")
            self._system_check_turn_ids.discard(system_check_id)
            self._system_check_output_turn_ids.discard(system_check_id)
            await self._settle_final_system_check(system_check_id)

    async def end(self, reason):
        old = self.episode_id
        self.scheduler.disarm()
        self.ledger.clear_shadow_progress(reset_seen=True)
        await self._stop_heartbeat_clock()
        self.recovery_queue.clear()
        self.active_dispatch_context = None
        self.output.cancel_all(reason)
        current_task = asyncio.current_task()
        active_request = self.active_request
        if active_request and active_request is not current_task and not active_request.done():
            active_request.cancel(str(reason) or "episode_ended")
        if self.delay_task is not None and not self.delay_task.done():
            self.delay_task.cancel()

        # ``end`` is also used by reset/fatal-error paths while the single
        # scheduler loop is waiting on its queue or on an active provider
        # task.  Reap both tasks here so a new Episode cannot inherit a stale
        # completed request or a runner whose heartbeat axis belongs to the
        # previous Episode.
        runner = self.runner
        if runner is not None and runner is not current_task and not runner.done():
            runner.cancel()
        if runner is not None and runner is not current_task:
            await asyncio.gather(runner, return_exceptions=True)
            if self.runner is runner:
                self.runner = None
        if active_request is not None and active_request is not current_task:
            await asyncio.gather(active_request, return_exceptions=True)
            if self.active_request is active_request:
                self.active_request = None
        if active_request is not current_task:
            self.active_request_identity = None
            self.active_request_source_turn_id = None
            self.active_request_kinds = set()
            self.active_recovery = None
            self.active_request_count = 0
        self.delay_task = None
        background = list(self._background_tools)
        for task in background:
            if not task.done():
                task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._background_tools.clear()
        if old:
            self.registry.cancel_turn(old)
        self.pending.clear()
        self._pending_silence_anchor = None
        self._speech_transactions_by_request.clear()
        self._immediate_system_check = None
        self._system_check_speeches.clear()
        self._system_check_turn_ids.clear()
        self._system_check_output_turn_ids.clear()
        self._final_system_checks.clear()
        self._settled_final_system_checks.clear()
        await self.log("EPISODE_ENDED", reason=reason)
        self.episode_id = None
        self.persona_documents = PersonaDocuments()
        self.agent_step_id = None
        self.current_action = None
        self.inputs.clear()
        self.context_limited = False
        self.context_limit_reason = None
        self.context_chars = 0
        self.context_token_estimate = 0
        self.context_usage_percent = 0.0
        self.context_compacted = False
        self.context_compaction = {}
        self.provider_prompt_tokens = None
        self.provider_input_tokens = None
        self.provider_cached_tokens = None
        self.provider_cache_miss_tokens = None
        self.provider_cache_hit_ratio = None
        self.static_context_digest = None
        self.history_context_digest = None
        self.schema_digest = None
        self._history_prefix = []
        self._recovery_attempts_by_turn.clear()
        self._transport_retry_attempts_by_turn.clear()
        self._retry_shadow_information_by_turn.clear()
        self._retry_secondary_by_turn.clear()
        self._retry_system_check = None
        self.deferred_recovery_feedback.clear()
        self.state = "IDLE"
        await self.emit("agent.episode", self.snapshot(include_history=False))

    async def stop(self):
        self.closed = True
        await self.end("stopped")
        await self.output.close()
        self._system_check_turn_ids.clear()
        self._system_check_output_turn_ids.clear()
        self._identity_turns.clear()
        self.input_finished_at.clear()
