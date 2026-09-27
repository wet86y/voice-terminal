"""Admission of a streamed decision into one speech transaction."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamedSpeechAdmissionState:
    identity: tuple
    source_turn_id: str | None
    snapshot: Any
    context_locked_chars: int
    system_check_id: str | None
    same_turn_transcript_reassessment: bool
    dispatch_context: dict | None
    speech_handle: Any = None
    first_speech_text_at: float | None = None
    prefix_baseline: dict | None = None
    prefix_wait_reported: bool = False
    speech_fed_chars: int = 0
    rewrite_speech_id: str | None = None
    rewrite_tail_chars: int = 0


def common_prefix_chars(left: str, right: str) -> int:
    """Return the length of the longest common character prefix.

    The divergence offset is the only quantity that decides where a live
    response stops agreeing with a new one, so it is recorded on every
    reconciliation rather than inferred from the two total lengths.
    """

    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


async def queue_streamed_speech(runtime, state: StreamedSpeechAdmissionState,
                                decision, full_text) -> bool:
    self = runtime
    identity = state.identity
    source_turn_id = state.source_turn_id
    snapshot = state.snapshot
    context_locked_chars = state.context_locked_chars
    system_check_id = state.system_check_id
    same_turn_transcript_reassessment = state.same_turn_transcript_reassessment
    dispatch_context = state.dispatch_context
    speech_handle = state.speech_handle
    first_speech_text_at = state.first_speech_text_at
    prefix_baseline = state.prefix_baseline
    prefix_wait_reported = state.prefix_wait_reported
    speech_fed_chars = state.speech_fed_chars
    rewrite_speech_id = state.rewrite_speech_id
    rewrite_tail_chars = state.rewrite_tail_chars
    try:
        if rewrite_speech_id is not None:
            # A live transaction's unsent tail is already being replaced.
            # Each further delta extends that same replacement rather than
            # opening another transaction.  ``rewrite_unsent`` now takes the
            # new tail only, so the extension is the part of the accepted
            # text that has not been handed over yet.
            extension = full_text[rewrite_tail_chars:]
            rewrite = getattr(self.output, "rewrite_unsent", None)
            if callable(rewrite) and extension:
                rewritten = await rewrite(
                    identity, source_turn_id,
                    speech_id=rewrite_speech_id, text=extension,
                )
                if rewritten is not None:
                    rewrite_tail_chars = len(full_text)
                    speech_handle = rewritten
                    return True
            elif callable(rewrite) and not extension:
                return True
            # The tail became irreversible while the model was still
            # writing.  Fall through so the correction is still delivered
            # through the ordinary replacement path.
            await self.log(
                "SPEECH_TAIL_REWRITE_UNAVAILABLE", identity=identity,
                turn_id=source_turn_id, baseline_source="active_same_turn",
                reason="tail_became_irreversible_mid_stream",
                rewrite_result="rewrite_unavailable",
                context_locked_chars=context_locked_chars,
                speech_id=rewrite_speech_id, new_chars=len(full_text),
                system_check_id=system_check_id,
            )
            rewrite_speech_id = None
            rewrite_tail_chars = 0
            prefix_baseline = None
            speech_handle = None
        if speech_handle is not None:
            return False
        queue_status = snapshot.get("queue_status", "empty") if isinstance(snapshot, dict) else "empty"
        existing_text = str(snapshot.get("text") or "") if isinstance(snapshot, dict) else ""
        active_same_turn = bool(
            isinstance(snapshot, dict)
            and snapshot.get("conversation_id") == identity[0]
            and snapshot.get("turn_id") == source_turn_id
        )
        if prefix_baseline is None and existing_text and active_same_turn:
            # Only text the provider already received is irreversible.
            # Everything after it is still held locally, so the
            # continuation baseline of a live response is its sent
            # frontier rather than the whole accepted text.  A snapshot
            # without that field is a legacy/collaborator shape: treat
            # every accepted character as already sent, which reproduces
            # the previous strict behaviour exactly.
            if isinstance(snapshot.get("sent_chars"), int):
                sent_chars = max(0, min(len(existing_text), snapshot["sent_chars"]))
            else:
                sent_chars = len(existing_text)
            prefix_baseline = {
                "text": existing_text[:sent_chars],
                "full_text": existing_text,
                "sent_chars": sent_chars,
                "speech_id": snapshot.get("speech_id"),
                "generation": snapshot.get("output_generation"),
                "rewritable": bool(snapshot.get("rewritable")),
                "source": "active_same_turn",
            }
        elif (
            prefix_baseline is None and existing_text
            and isinstance(snapshot, dict)
            and snapshot.get("conversation_id") == identity[0]
        ):
            # This is observational state from a preceding turn, not a
            # continuation target.  Delay only long enough to detect and
            # strip a model violation before any repeated prefix reaches
            # TTS; a divergence remains a normal cross-turn replacement.
            prefix_baseline = {
                "text": existing_text,
                "speech_id": snapshot.get("speech_id"),
                "generation": snapshot.get("output_generation"),
                "source": "active_previous_guard",
            }
        elif prefix_baseline is None and queue_status == "empty":
            prefix_baseline = self._completed_speech_guard(
                identity, source_turn_id,
                allow_same_turn=same_turn_transcript_reassessment,
            )
            if (prefix_baseline is None and isinstance(snapshot, dict)
                    and snapshot.get("delivered_turn_id") == source_turn_id
                    and snapshot.get("delivered_resumable", True)):
                # A same-turn continuation whose transaction already
                # settled: the accepted frontier outlives the queue head.
                delivered = str(snapshot.get("delivered_text") or "")
                if delivered:
                    prefix_baseline = {
                        "text": delivered,
                        "speech_id": None,
                        "generation": None,
                        "source": "same_turn_delivered_frontier",
                    }
        if prefix_baseline is not None:
            baseline = prefix_baseline["text"]
            if baseline.startswith(full_text):
                if not prefix_wait_reported:
                    prefix_wait_reported = True
                    await self.emit("speech.append_waiting_prefix", {
                        "speech_id": prefix_baseline["speech_id"], "turn_id": source_turn_id,
                        "baseline_source": prefix_baseline["source"],
                        "baseline_chars": len(baseline), "received_chars": len(full_text),
                    })
                return False
            if full_text.startswith(baseline):
                suffix = full_text[len(baseline):]
                if prefix_baseline["source"] in {
                    "completed_guard", "completed_same_turn_guard", "active_previous_guard",
                    "same_turn_delivered_frontier",
                } and suffix:
                    speech_handle = (
                        await self.output.replace_current(identity, source_turn_id, emotion=decision.emotion)
                        if prefix_baseline["source"] == "active_previous_guard"
                        else await self.output.begin(identity, source_turn_id, emotion=decision.emotion)
                    )
                    self._track_system_check_speech(speech_handle, dispatch_context)
                    await speech_handle.feed(suffix)
                    speech_fed_chars = len(suffix)
                    first_speech_text_at = time.monotonic()
                    await self.log(
                        "SPEECH_COMPLETED_SAME_TURN_PREFIX_STRIPPED"
                        if prefix_baseline["source"] == "completed_same_turn_guard"
                        else "CROSS_TURN_SPEECH_PREFIX_STRIPPED",
                        identity=identity, turn_id=source_turn_id,
                        baseline_source=prefix_baseline["source"],
                        baseline_chars=len(baseline), new_chars=len(full_text),
                        suffix_chars=len(suffix), stripped=True,
                        reason=("active_previous_turn_prefix"
                                if prefix_baseline["source"] == "active_previous_guard"
                                else "completed_same_turn_extension"
                                if prefix_baseline["source"] == "completed_same_turn_guard"
                                else "same_turn_delivered_frontier_extension"
                                if prefix_baseline["source"] == "same_turn_delivered_frontier"
                                else "completed_previous_turn_prefix"),
                        speech_id=speech_handle.speech_id,
                    )
                    return True
                rewrite = getattr(self.output, "rewrite_unsent", None)
                if (prefix_baseline["source"] == "active_same_turn"
                        and prefix_baseline.get("rewritable") and callable(rewrite)
                        and full_text.startswith(baseline) and len(full_text) > len(baseline)):
                    # ``rewrite_unsent`` takes the new tail only now, so the
                    # caller derives it from the accepted frontier instead of
                    # re-sending the whole accumulated answer.
                    rewritten = await rewrite(
                        identity, source_turn_id,
                        speech_id=prefix_baseline["speech_id"], text=suffix,
                    )
                    if rewritten is not None:
                        speech_handle = rewritten
                        rewrite_speech_id = rewritten.speech_id
                        rewrite_tail_chars = len(full_text)
                        speech_fed_chars = len(rewritten.text)
                        first_speech_text_at = time.monotonic()
                        await self.log(
                            "SPEECH_TAIL_REWRITTEN", identity=identity,
                            turn_id=source_turn_id,
                            baseline_source="active_same_turn",
                            baseline_chars=len(baseline), new_chars=len(full_text),
                            lcp_chars=len(baseline),
                            sent_chars=prefix_baseline.get("sent_chars"),
                            context_locked_chars=context_locked_chars,
                            apply_locked_chars=len(baseline),
                            frontier_advanced_during_llm=len(baseline) > context_locked_chars,
                            rewrite_result="tail_rewritten",
                            suffix_chars=len(suffix),
                            speech_id=rewritten.speech_id,
                            system_check_id=system_check_id,
                        )
                        return True
                    await self.log(
                        "SPEECH_TAIL_REWRITE_UNAVAILABLE", identity=identity,
                        turn_id=source_turn_id,
                        baseline_source="active_same_turn",
                        reason="sent_prefix_contradicts_new_text",
                        baseline_chars=len(baseline), new_chars=len(full_text),
                        sent_chars=prefix_baseline.get("sent_chars"),
                        context_locked_chars=context_locked_chars,
                        apply_locked_chars=len(baseline),
                        frontier_advanced_during_llm=len(baseline) > context_locked_chars,
                        rewrite_result="rewrite_unavailable",
                        speech_id=prefix_baseline["speech_id"],
                        system_check_id=system_check_id,
                    )
                append = getattr(self.output, "append_successor", None)
                if prefix_baseline["source"] == "active_same_turn" and callable(append):
                    await self.log(
                        "SPEECH_APPEND_ELIGIBLE", identity=identity,
                        turn_id=source_turn_id,
                        system_check_id=system_check_id,
                        model_continuation_mode=(
                            "rewrite_tail" if prefix_baseline.get("rewritable") else "append_only"
                        ),
                        baseline_chars=len(baseline), suffix_chars=len(suffix),
                    )
                    speech_handle = await append(
                        identity, source_turn_id,
                        baseline_speech_id=prefix_baseline["speech_id"],
                        baseline_generation=prefix_baseline["generation"],
                        suffix=suffix, emotion=decision.emotion,
                    )
                    self._track_system_check_speech(speech_handle, dispatch_context)
                    speech_fed_chars = len(suffix)
                    first_speech_text_at = time.monotonic()
                    await self.log(
                        "SPEECH_PREFIX_RECONCILED",
                        identity=identity, turn_id=source_turn_id,
                        baseline_source="active_same_turn",
                        baseline_chars=len(baseline), new_chars=len(full_text),
                        suffix_chars=len(suffix), stripped=True,
                        reason="same_turn_extension",
                        speech_id=speech_handle.speech_id,
                    )
                    await self.log(
                        "SPEECH_APPEND_SUCCEEDED", identity=identity,
                        turn_id=source_turn_id,
                        speech_id=speech_handle.speech_id,
                        system_check_id=system_check_id,
                        suffix_chars=len(suffix),
                    )
                    return True
                # Test/legacy outputs have no continuation primitive;
                # preserve their old safe replacement behaviour.
            # The first disagreement inside the already accepted text is
            # a real correction, not an append.
            if prefix_baseline["source"] in {"active_same_turn", "active_previous_guard"}:
                queue_status = "speaking"
            await self.log(
                "SPEECH_PREFIX_RECONCILED",
                identity=identity, turn_id=source_turn_id,
                baseline_source=prefix_baseline["source"],
                baseline_chars=len(baseline), new_chars=len(full_text),
                suffix_chars=0, stripped=False,
                lcp_chars=common_prefix_chars(baseline, full_text),
                sent_chars=prefix_baseline.get("sent_chars"),
                context_locked_chars=context_locked_chars,
                apply_locked_chars=len(baseline),
                frontier_advanced_during_llm=(
                    prefix_baseline["source"] == "active_same_turn"
                    and len(baseline) > context_locked_chars
                ),
                rewrite_result=(
                    "hard_replace_frontier_advanced"
                    if prefix_baseline["source"] == "active_same_turn"
                    and len(baseline) > context_locked_chars
                    else "hard_replace_locked_divergence"
                ),
                reason=("same_turn_text_diverged"
                        if prefix_baseline["source"] == "active_same_turn"
                        else "same_turn_guard_diverged"
                        if prefix_baseline["source"] == "completed_same_turn_guard"
                        else "new_turn_text_diverged"),
            )
            prefix_baseline = None
        replace = queue_status in {"speaking", "queued", "suspended"}
        speech_handle = (await self.output.replace_current(identity, source_turn_id, emotion=decision.emotion)
                         if replace else await self.output.begin(identity, source_turn_id, emotion=decision.emotion))
        self._track_system_check_speech(speech_handle, dispatch_context)
        first_speech_text_at = time.monotonic()
        workspace = getattr(speech_handle, "workspace", None)
        if workspace is not None and isinstance(getattr(workspace, "timings", None), dict):
            workspace.timings["speak_queued"] = first_speech_text_at
            workspace.timings["first_speech_text"] = first_speech_text_at
            workspace.timings["speech_started_before_envelope_commit"] = True
        await self.log("SPEAK_ACCEPTED", identity=identity, turn_id=source_turn_id,
                       speech_id=speech_handle.speech_id, replaced=replace, partial=True)
        if replace:
            await self.log(
                "SPEECH_DECISION_REPLACED", identity=identity,
                turn_id=source_turn_id,
                speech_id=speech_handle.speech_id,
                system_check_id=system_check_id,
            )
        return False
    finally:
        state.speech_handle = speech_handle
        state.first_speech_text_at = first_speech_text_at
        state.prefix_baseline = prefix_baseline
        state.prefix_wait_reported = prefix_wait_reported
        state.speech_fed_chars = speech_fed_chars
        state.rewrite_speech_id = rewrite_speech_id
        state.rewrite_tail_chars = rewrite_tail_chars
