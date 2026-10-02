"""Application of a validated main-agent decision to speech output."""
from __future__ import annotations
from hashlib import sha256

from .decision_envelope import DecisionEnvelope
from .streamed_speech import common_prefix_chars


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
    if (getattr(self, "should_defer_speech", lambda: False)()
            and decision.text.strip()):
        source_id = str(turn_id or identity[1]) + ":" + sha256(decision.text.encode("utf-8")).hexdigest()
        self.defer_speech(source_id, decision.text)
        self.current_action = "wait"
        return
    snapshot = getattr(self.output, "speak_snapshot", {})
    queue_status = snapshot.get("queue_status", "empty") if isinstance(snapshot, dict) else "empty"
    active_same_turn = bool(
        isinstance(snapshot, dict)
        and snapshot.get("conversation_id") == identity[0]
        and snapshot.get("turn_id") == turn_id
    )
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
        # ``revision`` carries only the new tail.  The locked prefix is
        # local authority, so nothing here compares strings to guess the
        # model's intent.
        revised = await self._apply_revision_tail(
            identity, turn_id, snapshot, decision.text,
            emotion=decision.emotion, dispatch_context=dispatch_context,
        )
        self.current_action = "speak" if revised is not None else "wait"
    elif queue_status == "speaking":
        existing_text = str(snapshot.get("text") or "")
        existing_id = snapshot.get("speech_id")
        existing_generation = snapshot.get("output_generation")
        if isinstance(snapshot.get("sent_chars"), int):
            sent_chars = max(0, min(len(existing_text), snapshot["sent_chars"]))
        else:
            sent_chars = len(existing_text)
        sent_prefix = existing_text[:sent_chars]
        # Text the provider has not received yet is still replaceable.
        # Rewriting that tail in place keeps the audible prefix playing
        # instead of cutting the sentence in half.  ``rewrite_unsent`` now
        # takes the new tail only, so the caller has to derive it from the
        # accepted frontier instead of re-sending the whole answer.
        rewrite = getattr(self.output, "rewrite_unsent", None)
        if (active_same_turn and existing_text and snapshot.get("rewritable")
                and callable(rewrite)):
            tail = self._snapshot_progress(decision, snapshot)
            if tail:
                rewritten = await rewrite(identity, turn_id, speech_id=existing_id,
                                          text=tail)
                if rewritten is not None:
                    self.current_action = "speak"
                    await self.log(
                        "SPEECH_TAIL_REWRITTEN", identity=identity, turn_id=turn_id,
                        baseline_source="active_same_turn",
                        baseline_chars=len(sent_prefix), new_chars=len(decision.text),
                        lcp_chars=len(sent_prefix), sent_chars=sent_chars,
                        suffix_chars=len(tail),
                        speech_id=rewritten.speech_id,
                        system_check_id=(dispatch_context.get("system_check_id")
                                         if isinstance(dispatch_context, dict) else None),
                    )
                    return
            await self.log(
                "SPEECH_TAIL_REWRITE_UNAVAILABLE", identity=identity, turn_id=turn_id,
                baseline_source="active_same_turn",
                reason="revision_window_closed",
                baseline_chars=len(sent_prefix), new_chars=len(decision.text),
                sent_chars=sent_chars, speech_id=existing_id,
            )
        # A completed envelope can be a strict extension of the audible
        # text.  Queue only its suffix; repeating the prefix would sound
        # like a model interruption even though the decision agrees.
        append = getattr(self.output, "append_successor", None)
        if (active_same_turn and existing_text and decision.text.startswith(existing_text)
                and callable(append)):
            suffix = decision.text[len(existing_text):]
            await self.log(
                "SPEECH_APPEND_ELIGIBLE", identity=identity, turn_id=turn_id,
                system_check_id=(dispatch_context.get("system_check_id")
                                 if isinstance(dispatch_context, dict) else None),
                model_continuation_mode="rewrite_tail" if snapshot.get("rewritable") else "append_only",
                baseline_chars=len(existing_text), suffix_chars=len(suffix),
                lcp_chars=common_prefix_chars(existing_text, decision.text),
                sent_chars=sent_chars,
            )
            if not suffix:
                await self.emit("speech.append_ignored", {
                    "speech_id": existing_id, "turn_id": turn_id,
                    "reason": "identical_text",
                })
                self.current_action = "speak"
                await self.log(
                    "SPEECH_IDENTICAL_IGNORED", identity=identity, turn_id=turn_id,
                    system_check_id=(dispatch_context.get("system_check_id")
                                     if isinstance(dispatch_context, dict) else None),
                )
                self._note_identical_speak("active_same_turn")
                return
            handle = await append(identity, turn_id, baseline_speech_id=existing_id,
                                  baseline_generation=existing_generation, suffix=suffix,
                                  emotion=decision.emotion)
            self._track_system_check_speech(handle, dispatch_context)
            await handle.finish()
            await self.log(
                "SPEECH_APPEND_SUCCEEDED", identity=identity, turn_id=turn_id,
                speech_id=handle.speech_id,
                system_check_id=(dispatch_context.get("system_check_id")
                                 if isinstance(dispatch_context, dict) else None),
                suffix_chars=len(suffix),
            )
            self.current_action = "speak"
            return
        speech_text = decision.text
        if active_same_turn:
            # Same-turn decisions never repeat what the queue already
            # accepted; only the genuinely new remainder is synthesised.
            speech_text = self._snapshot_progress(decision, snapshot) or ""
        elif (existing_text and speech_text.startswith(existing_text)
                and len(speech_text) > len(existing_text)):
            speech_text = speech_text[len(existing_text):]
            await self.log(
                "CROSS_TURN_SPEECH_PREFIX_STRIPPED",
                identity=identity, turn_id=turn_id,
                baseline_source="active_previous_guard",
                baseline_chars=len(existing_text), new_chars=len(decision.text),
                suffix_chars=len(speech_text), stripped=True,
                reason="active_previous_turn_prefix",
            )
        if not speech_text:
            # Nothing in this answer is a continuation any more.  Whatever
            # the queue already accepted must not be synthesised again, so
            # no replacement transaction opens at all.
            await self.emit("speech.append_ignored", {
                "speech_id": existing_id, "turn_id": turn_id,
                "reason": "already_delivered",
            })
            self.current_action = "speak"
            await self.log("SPEECH_IDENTICAL_IGNORED", identity=identity,
                           turn_id=turn_id, speech_id=existing_id,
                           baseline_source="active_same_turn",
                           reason="already_delivered")
            self._note_identical_speak("active_same_turn")
            return
        # The complete, validated decision is already authoritative.  A
        # second model request here makes the user wait through old TTS
        # cancellation plus another LLM round-trip.
        handle = await self.output.replace_current(identity, turn_id, emotion=decision.emotion)
        self._track_system_check_speech(handle, dispatch_context)
        await handle.feed(speech_text)
        await handle.finish()
        self.current_action = "speak"
        await self.log("SPEAK_ACCEPTED", identity=identity, turn_id=turn_id,
                       speech_id=handle.speech_id, replaced=True)
        await self.log(
            "SPEECH_DECISION_REPLACED", identity=identity, turn_id=turn_id,
            speech_id=handle.speech_id,
            system_check_id=(dispatch_context.get("system_check_id")
                             if isinstance(dispatch_context, dict) else None),
        )
    else:
        # Candidate text is complete and valid at this point.  Existing
        # output owns all queue replacement/cancellation generation rules.
        speech_text = decision.text
        existing_text = str(snapshot.get("text") or "") if isinstance(snapshot, dict) else ""
        if (not active_same_turn and existing_text
                and speech_text.startswith(existing_text)
                and len(speech_text) > len(existing_text)):
            speech_text = speech_text[len(existing_text):]
            await self.log(
                "CROSS_TURN_SPEECH_PREFIX_STRIPPED",
                identity=identity, turn_id=turn_id,
                baseline_source="active_previous_guard",
                baseline_chars=len(existing_text), new_chars=len(decision.text),
                suffix_chars=len(speech_text), stripped=True,
                reason="queued_previous_turn_prefix",
            )
        elif queue_status == "empty":
            guard = self._completed_speech_guard(
                identity, turn_id,
                allow_same_turn=bool(self._applying_transcript_reassessment),
            )
            baseline = guard["text"] if guard else ""
            stripped_by = guard["source"] if guard else ""
            if (not baseline and isinstance(snapshot, dict)
                    and snapshot.get("delivered_turn_id") == turn_id
                    and snapshot.get("delivered_resumable", True)):
                # Same-turn continuation after the queue drained.  The
                # accepted frontier is the real baseline; it survives the
                # settlement of the transaction that produced it.
                baseline = str(snapshot.get("delivered_text") or "")
                stripped_by = "same_turn_delivered_frontier"
            if (baseline and speech_text == baseline
                    and stripped_by == "completed_same_turn_guard"):
                await self.emit("speech.append_ignored", {
                    "speech_id": guard["speech_id"], "turn_id": turn_id,
                    "reason": "completed_same_turn_identical",
                })
                self.current_action = "wait"
                await self.log(
                    "SPEECH_IDENTICAL_IGNORED", identity=identity, turn_id=turn_id,
                    speech_id=guard["speech_id"], baseline_source=stripped_by,
                    reason="completed_same_turn_identical",
                )
                self._note_identical_speak(stripped_by)
                return
            if baseline and speech_text.startswith(baseline) and len(speech_text) > len(baseline):
                speech_text = speech_text[len(baseline):]
                await self.log(
                    "SPEECH_COMPLETED_SAME_TURN_PREFIX_STRIPPED"
                    if stripped_by == "completed_same_turn_guard"
                    else "CROSS_TURN_SPEECH_PREFIX_STRIPPED",
                    identity=identity, turn_id=turn_id,
                    baseline_source=stripped_by,
                    baseline_chars=len(baseline), new_chars=len(decision.text),
                    suffix_chars=len(speech_text), stripped=True,
                    reason="completed_same_turn_extension"
                    if stripped_by == "completed_same_turn_guard"
                    else "completed_previous_turn_prefix",
                )
            elif baseline and stripped_by == "same_turn_delivered_frontier":
                await self.emit("speech.append_ignored", {
                    "speech_id": guard["speech_id"] if guard else None, "turn_id": turn_id,
                    "reason": "already_delivered",
                })
                self.current_action = "speak"
                await self.log("SPEECH_IDENTICAL_IGNORED", identity=identity, turn_id=turn_id,
                               baseline_source=stripped_by,
                               reason="already_delivered")
                self._note_identical_speak(stripped_by)
                return
        replace = queue_status in {"queued", "suspended"}
        handle = (await self.output.replace_current(identity, turn_id, emotion=decision.emotion)
                  if replace else await self.output.begin(identity, turn_id, emotion=decision.emotion))
        self._track_system_check_speech(handle, dispatch_context)
        await handle.feed(speech_text)
        await handle.finish()
        self.current_action = "speak"
        await self.log("SPEAK_ACCEPTED", identity=identity, turn_id=turn_id,
                       speech_id=handle.speech_id, replaced=replace)
