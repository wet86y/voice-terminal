"""Request-correlated, content-free LLM context diagnostics."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Mapping, Sequence

from ..session.context_frames import ContextFrame


logger = logging.getLogger("voice_terminal.llm_context_audit")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_context_snapshot(
    provider: Any,
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    *,
    conversation_id: str | None,
    request_id: str | None,
    turn_id: str | None,
    events: Sequence[str],
    mode: str,
    dispatch_kind: str = "normal",
    prompt_kind: str = "normal",
    scheduler_generation: int | None = None,
    window_index: int | None = None,
    system_check_id: str | None = None,
    owner_scope: str | None = None,
    reasoning_effort: str,
    context_compaction: Mapping[str, Any],
    context_token_estimate: int,
) -> dict[str, Any]:
    """Build the exact header-free body and a compact comparison manifest."""

    frozen_messages = [dict(message) for message in messages]
    transport = str(getattr(provider, "transport", "chat_completions"))
    if transport == "responses" and callable(getattr(provider, "_responses_payload", None)):
        wire_payload = provider._responses_payload(
            frozen_messages,
            model=str(getattr(provider, "model", "")),
            schema=schema,
            schema_name="voice_decision_v06",
            reasoning_effort=reasoning_effort,
            max_output_tokens=int(getattr(provider, "max_output_tokens", 8192)),
        )
    else:
        # This fallback remains exact at the model-facing message level for
        # providers without a public payload builder.
        wire_payload = {
            "model": str(getattr(provider, "model", "")),
            "messages": frozen_messages,
            "json_mode": True,
            "schema": dict(schema),
            "reasoning_effort": reasoning_effort,
        }
    manifest = []
    lane_chars: dict[str, int] = {}
    for index, message in enumerate(frozen_messages):
        content = str(message.get("content") or "")
        lane = ContextFrame.lane_of(message).value
        lane_chars[lane] = lane_chars.get(lane, 0) + len(content)
        manifest.append({
            "index": index,
            "role": str(message.get("role") or ""),
            "lane": lane,
            "chars": len(content),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
    wire_serialized = _canonical(wire_payload)
    return {
        "record_type": "request_context",
        "version": 2,
        "at": time.time(),
        "contains_conversation_content": False,
        "conversation_id": conversation_id,
        "request_id": request_id,
        "turn_id": turn_id,
        "events": list(events),
        "mode": mode,
        "dispatch_kind": dispatch_kind,
        "prompt_kind": prompt_kind,
        "scheduler_generation": scheduler_generation,
        "window_index": window_index,
        "system_check_id": system_check_id,
        "owner_scope": owner_scope,
        "transport": transport,
        "reasoning_effort": reasoning_effort,
        "wire_sha256": _digest(wire_payload),
        "wire_bytes": len(wire_serialized.encode("utf-8")),
        "schema_sha256": _digest(schema),
        "message_count": len(frozen_messages),
        "lane_chars": lane_chars,
        "context_token_estimate": context_token_estimate,
        "context_compaction": dict(context_compaction),
        "message_manifest": manifest,
    }


def capture_context_snapshot(*args, **kwargs) -> str | None:
    """Append one exact context record and return its wire digest."""

    try:
        record = build_context_snapshot(*args, **kwargs)
        logger.info(_canonical(record))
        return str(record["wire_sha256"])
    except Exception as exc:
        logging.getLogger(__name__).warning("llm.context_audit_failed error=%s", str(exc)[:300])
        return None


def capture_request_outcome(
    *,
    conversation_id: str | None,
    request_id: str | None,
    turn_id: str | None,
    wire_sha256: str | None,
    outcome: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Append a content-free terminal record correlated to one context."""

    try:
        record = {
            "record_type": "request_outcome",
            "version": 1,
            "at": time.time(),
            "conversation_id": conversation_id,
            "request_id": request_id,
            "turn_id": turn_id,
            "wire_sha256": wire_sha256,
            "outcome": outcome,
            "details": dict(details or {}),
        }
        logger.info(_canonical(record))
    except Exception as exc:
        logging.getLogger(__name__).warning("llm.outcome_audit_failed error=%s", str(exc)[:300])
