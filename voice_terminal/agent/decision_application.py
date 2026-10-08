"""Application of a validated main-agent decision to speech output."""
from __future__ import annotations
from hashlib import sha256

from .decision_envelope import DecisionEnvelope
from .streamed_speech import StreamedSpeechAdmissionState, queue_streamed_speech


class ProtocolError(ValueError):
    """A model-controlled tool/protocol error eligible for immediate recovery."""


async def apply_decision_v06(self, identity, turn_id, envelope: DecisionEnvelope,
                              *, dispatch_context=None):
    """Commit one fully validated V0.6 decision to the local transaction."""
    decision = envelope.speak
    if (isinstance(dispatch_context, dict)
            and dispatch_context.get("prompt_kind") == "kws_wake"
            and (decision.state != "speak" or not decision.text.strip() or envelope.actions)):
        raise ProtocolError("kws_wake requires nonempty speak and actions=[]")
    raw_snapshot = getattr(self.output, "speak_snapshot", None)
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}
    snapshot["_output_busy"] = bool(getattr(self.output, "busy", False))
    queue_status = snapshot.get("queue_status", "empty") if isinstance(snapshot, dict) else "empty"
    await self.log("SPEAK_STATE_CHANGED", identity=identity, turn_id=turn_id,
                   state=decision.state, queue_status=queue_status,
                   action_count=len(envelope.actions))
    if decision.state == "wait":
        self.current_action = "wait"
        await self.log("WAIT", identity=identity, turn_id=turn_id, reason="speak_wait")
    elif decision.state == "cancel":
        # ``cancel`` is an explicit output decision, including while audio
        # is already audible.  Empty text keeps the original emergency-stop
        # meaning; non-empty text stops and replaces in one step, which is
        # the only shape that can tell the listener why it changed.
        replacement = decision.text.strip()
        if not replacement:
            cancel = getattr(self.output, "cancel_all", None)
            if callable(cancel):
                cancel("cancelled")
            else:
                suspend = getattr(self.output, "suspend_pending", None)
                if callable(suspend):
                    suspend()
            await self.log("SPEAK_CANCELLED", identity=identity, turn_id=turn_id,
                           queue_status=queue_status)
            self.current_action = "wait"
        else:
            replace = getattr(self.output, "replace_current", None)
            if not callable(replace):
                # Test/legacy outputs without the replacement primitive keep
                # the pre-existing stop-only behaviour.
                cancel = getattr(self.output, "cancel_all", None)
                if callable(cancel):
                    cancel("cancelled")
                await self.log("SPEAK_CANCELLED", identity=identity, turn_id=turn_id,
                               queue_status=queue_status)
                self.current_action = "wait"
            else:
                handle = await replace(identity, turn_id, emotion=decision.emotion)
                self._track_system_check_speech(handle, dispatch_context)
                await handle.feed(decision.text)
                await handle.finish()
                self.current_action = "speak"
                await self.log("SPEAK_CANCELLED_WITH_REPLACEMENT", identity=identity,
                               turn_id=turn_id, speech_id=handle.speech_id,
                               queue_status=queue_status, replacement_chars=len(decision.text),
                               system_check_id=(dispatch_context.get("system_check_id")
                                                if isinstance(dispatch_context, dict) else None))
    elif decision.state == "revision":
        admission = getattr(self, "_applying_speech_admission", None)
        # ``revision`` carries only the new tail.  The locked prefix is
        # local authority, so nothing here compares strings to guess the
        # model's intent.
        revised = await self._apply_revision_tail(
            identity, turn_id,
            admission.snapshot if admission is not None else snapshot,
            decision.text,
            emotion=decision.emotion, dispatch_context=dispatch_context,
        )
        self.current_action = "speak" if revised is not None else "wait"
    else:
        # 完整响应与流式响应使用同一个 speak（新播报）受理入口。
        admission = getattr(self, "_applying_speech_admission", None)
        if admission is None:
            admission = StreamedSpeechAdmissionState(
                identity, turn_id, snapshot, 0,
                dispatch_context.get("system_check_id") if isinstance(dispatch_context, dict) else None,
                bool(self._applying_transcript_reassessment), dispatch_context,
            )
        await queue_streamed_speech(self, admission, decision, decision.text, final=True)
        if (not admission.rejected and admission.speech_handle is None
                and getattr(self, "should_defer_speech", lambda: False)()
                and decision.text.strip()):
            source_id = str(turn_id or identity[1]) + ":" + sha256(decision.text.encode("utf-8")).hexdigest()
            self.defer_speech(source_id, decision.text)
        handle = admission.speech_handle
        if handle is not None:
            await handle.finish()
        self.current_action = "speak" if handle is not None else "wait"
