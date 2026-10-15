"""Projection and revision of a speech transaction frontier."""
from __future__ import annotations


class SpeechFrontierMixin:
    @property
    def speak_snapshot(self):
        h = (self.handle if self.handle and not self.handle.reason and not self.handle.task.done() else None)
        h = h or next((item for item in self._pending if not item.reason and not item.task.done()), None)
        if h is None:
            continuation = self._continuation_snapshot()
            if continuation is not None:
                return continuation
            value = {"queue_status": "empty", "text_only_queue": True}
            self._add_delivered_frontier(value)
            return value
        if h.workspace.playback_started:
            status = "speaking"
        elif h.state == "SUSPENDED":
            status = "suspended"
        else:
            status = "queued"
        text, locked_chars = self._accumulated_text(h)
        tail = getattr(h, "continuation_tail", "")  # tail（发送结束后接受的待接播尾巴）。
        value = {"queue_status": status, "text_only_queue": True, "text": text,
                 "speech_id": h.speech_id, "output_generation": h.workspace.generation,
                 "conversation_id": h.identity[0], "turn_id": h.turn_id,
                 "emotion": h.emotion_requested,
                 "text_revision": getattr(h, "text_revision", 0),  # text_revision（尾巴修订版本）。
                 # Admission frontiers.  ``text`` stays the accepted response;
                 # only ``sent_chars`` delimits what can no longer be replaced.
                 # A handle shape without them is legacy/collaborator input:
                 # report the whole text as already sent, which reproduces the
                 # previous strict behaviour exactly.
                 "sent_chars": locked_chars + getattr(h, "sent_chars", len(h.text)),
                 "rewritable": bool(getattr(h, "rewritable", False))}
        self._add_playback_frontiers(value, h)
        self._add_model_text_parts(value, h, locked_chars=locked_chars)
        if tail:
            value["text"] += tail
            value["_model_text_parts"]["revisable_tail"] += tail
            value["_model_text_parts_valid"] = "".join(value["_model_text_parts"].values()) == value["text"]
        return value

    def _add_delivered_frontier(self, value):
        """Expose the settled frontier while no transaction is active.

        The queue-empty snapshot carries the two halves of the delivery fact:
        ``delivered_text`` is what this turn already committed to playback and
        ``delivery.heard_text`` is what the listener actually heard.  Without
        them a new decision has no baseline and a repeated answer replays from
        its first character.
        """

        frontier = getattr(self, "delivered", None)
        frontier = frontier if isinstance(frontier, dict) else {}
        text = str(frontier.get("text") or "")
        turn_id = frontier.get("turn_id")
        delivery = self._delivery_fact()
        if delivery is not None:
            value["delivery"] = delivery
        # An interrupted answer is a deliberate user barge-in, so its text is
        # never *resumed*: it may only serve as a repeat baseline, never as a
        # continuation target that could suppress new content.
        interrupted = (delivery is not None
                       and delivery.get("status") == "partial"
                       and delivery.get("termination") in {"interrupted", "replaced"})
        if text and isinstance(turn_id, str) and turn_id:
            value["delivered_text"] = text
            value["delivered_turn_id"] = turn_id
            value["delivered_resumable"] = not interrupted

    def _delivery_fact(self):
        """Return the latest delivered-and-heard fact without any runtime id.

        This is the queue-empty half of the delivery frontier: when nothing is
        active, the only honest answer to "where does the speech continue" is
        what the listener already heard.
        """

        item = getattr(self, "last_delivery", None)
        item = item if isinstance(item, dict) else None
        if not item:
            return None
        heard = str(item.get("heard_text") or "")
        if not heard:
            return None
        return {"heard_text": heard,
                "status": item.get("status"),
                "termination": item.get("termination")}

    def _accumulated_text(self, h):
        """Return ``(text, locked_chars)`` for the model-visible transaction.

        ``text`` is the accepted response with the turn's already-delivered
        frontier restored in front of the active handle, and ``locked_chars``
        is the length restored before the handle's own text. A successor starts *after*
        the frontier, so the two halves are concatenated rather than compared;
        a handle that already carries the cumulative text is left alone.
        """

        handle_text = str(getattr(h, "text", "") or "")
        fixed_prefix = getattr(h, "delivered_prefix", None)
        if isinstance(fixed_prefix, str):
            # 接播只接收本段正文，前序已结算文字固定保存，不能按内容猜重。
            return fixed_prefix + handle_text, len(fixed_prefix)
        frontier = getattr(self, "delivered", None)
        frontier = frontier if isinstance(frontier, dict) else {}
        prefix = str(frontier.get("text") or "")
        if not prefix or not h.turn_id or frontier.get("turn_id") != h.turn_id:
            return handle_text, 0
        if not handle_text:
            return prefix, len(prefix)
        if handle_text.startswith(prefix):
            # The handle already carries the whole cumulative text.
            return handle_text, 0
        overlap = self._frontier_overlap(prefix, handle_text)
        if overlap is not None:
            return prefix + handle_text[overlap:], len(prefix) - overlap
        # A successor handle normally begins exactly where the frontier ends.
        return prefix + handle_text, len(prefix)

    @staticmethod
    def _frontier_overlap(prefix, handle_text):
        """Return how much of ``handle_text`` the delivered frontier covers.

        The frontier is a concatenation of earlier transactions, so a handle is
        cumulative when it starts anywhere near the end of it.  ``None`` means
        no overlap was found and the caller treats the handle as new text.
        """

        for start in range(min(len(prefix), 8), 0, -1):
            if handle_text.startswith(prefix[start:]):
                return len(prefix) - start
        return None

    def _add_playback_frontiers(self, value, h):
        """Attach playback position facts used for diagnostics and tuning."""

        workspace = getattr(h, "workspace", None)
        playback_key = getattr(workspace, "playback_key", None)
        player = getattr(self, "player", None)
        if player is None or playback_key is None:
            return
        try:
            current = player.progress(playback_key)
        except Exception:
            return
        bytes_per_second = player.sample_rate * player.channels * 2
        received = int(getattr(getattr(h, "tts_session", None), "received_bytes", 0) or 0)
        value["played_ms"] = int(current.get("output_estimated_ms") or 0)
        value["synthesized_ms"] = (
            int(received * 1000 / bytes_per_second) if bytes_per_second and received else 0
        )

    def _add_model_text_parts(self, value, h, *, locked_chars=0):
        """Attach one non-overlapping semantic text partition for projection.

        The raw snapshot keeps its local execution fields.  The Agent later
        selects only these text parts plus ownership/authority for the model,
        so playback counters and transaction identifiers never cross the
        request-tail boundary.
        """

        text = str(value.get("text") or "")
        state = getattr(h, "admission", None)
        if state is None:
            value["_model_text_parts"] = {
                "committed_prefix": text,
                "current_clause": "",
                "prefetched_clause": "",
                "revisable_tail": "",
            }
            value["_model_text_parts_valid"] = True
            return
        player = getattr(self, "player", None)
        output_estimated_ms = int(value.get("played_ms") or 0)
        bytes_per_second = 0
        if player is not None:
            bytes_per_second = int(player.sample_rate * player.channels * 2)
        parts, valid = state.model_text_parts(
            h.text,
            output_estimated_ms=output_estimated_ms,
            bytes_per_second=bytes_per_second,
        )
        if locked_chars > 0:
            # Shift the split's boundary by the restored frontier so the four
            # parts still concatenate to the snapshot's accepted text.
            prefix = text[:locked_chars]
            parts = {
                "committed_prefix": prefix + parts["committed_prefix"],
                "current_clause": parts["current_clause"],
                "prefetched_clause": parts["prefetched_clause"],
                "revisable_tail": parts["revisable_tail"],
            }
            valid = valid and "".join(parts.values()) == text
        value["_model_text_parts"] = parts
        value["_model_text_parts_valid"] = valid

    async def rewrite_unsent(self, identity, turn_id, *, speech_id, text,
                             expected_generation, expected_sent_chars, expected_text,
                             expected_text_revision=0):
        """revision（尾巴修订）的原子确认：事务、版本、文本、提交边界必须全部匹配。

        None（未受理）表示确认边界失效，调用方不得降级为追加或新建事务。
        校验与修改之间没有 await（异步让出），提交中的文本也不能被改写。
        expected_generation（期望输出代数）、expected_sent_chars（期望已提交字符数）、
        expected_text（期望原计划文本）、expected_text_revision（期望文本修订版本）
        都来自模型生成请求时的本地快照，不能用落地时的新边界代替。
        """
        h = self.handle or self._continuation_handle
        if (h is None or h.speech_id != speech_id or h.reason
                or h.identity[0] != identity[0] or h.turn_id != turn_id):
            return None
        state = h.admission
        if state is None or not h.rewritable:
            return None
        snapshot = self.speak_snapshot
        if (h.workspace.generation != expected_generation
                or snapshot.get("sent_chars") != expected_sent_chars
                or snapshot.get("text") != expected_text
                or getattr(h, "text_revision", 0) != expected_text_revision):
            return None
        baseline = h.text[:state.sent_chars]
        if state.closed:
            if not self._can_continue(h):
                return None
            dropped = len(h.continuation_tail)
            h.continuation_tail = text
            h.continuation_identity = identity
            h.continuation_signal.set()
        else:
            dropped = state.replace_unsent(text)
            if dropped < 0:
                return None
            h.text = baseline + text
        h.text_revision = getattr(h, "text_revision", 0) + 1
        # 分段器已结束，权威文本同时用于事务结算及本地状态快照。
        accepted = h.text + h.continuation_tail
        if not state.closed:
            h.workspace.staged_response = h.text.strip()
        self.state.current["assistant_text"] = accepted
        await self.runtime.emit("speech.tail_rewritten", {
            "speech_id": h.speech_id, "conversation_id": h.identity[0],
            "turn_id": h.turn_id, "output_generation": h.workspace.generation,
            "text_revision": h.text_revision,  # text_revision（单事务文本版本）。
            "text": accepted,  # text（本事务正文及待接播尾巴，不重复前序事务文字）。
            "sent_chars": state.sent_chars,
            "dropped_chars": dropped, "staged_chars": len(text),
            "cumulative_chars": len(accepted), "provider": h.tts_backend,
        })
        return h
