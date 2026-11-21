"""Context audit（上下文诊断）：关闭正文采集后保留请求摘要，省去完整快照工作。"""
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from voice_terminal.agent import context_audit
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextFrame, ContextLane


@pytest.mark.parametrize("transport", ["responses", "chat_completions"])
def test_disabled_capture_keeps_the_same_digest_without_building_full_diagnostics(monkeypatch, transport):
    provider = (DeepSeekClient(base_url="https://example.invalid", model="deepseek-v4-flash",
                              api_key="test", transport=transport)
                if transport == "responses" else SimpleNamespace(model="test", transport=transport))
    messages = [ContextFrame.make(ContextLane.HISTORY, "user", "关闭诊断后仍保留相同请求身份。")]
    metadata = dict(conversation_id="c", request_id="r", turn_id="t", events=[], mode="normal",
                    reasoning_effort="none", context_compaction={}, context_token_estimate=20)
    expected = context_audit.build_context_snapshot(provider, messages, {}, **metadata)["wire_sha256"]
    full_capture = Mock(side_effect=AssertionError("关闭诊断不能构造完整快照"))
    monkeypatch.setattr(context_audit, "build_context_snapshot", full_capture)
    monkeypatch.setattr(context_audit.logger, "disabled", True)
    assert context_audit.capture_context_snapshot(provider, messages, {}, **metadata) == expected
    full_capture.assert_not_called()


def test_enabled_capture_still_records_the_full_context(monkeypatch, caplog):
    monkeypatch.setattr(context_audit.logger, "disabled", False)
    with caplog.at_level(logging.INFO, logger=context_audit.logger.name):
        digest = context_audit.capture_context_snapshot(
            SimpleNamespace(model="test"), [ContextFrame.make(ContextLane.HISTORY, "user", "需要诊断的正文")], {},
            conversation_id="c", request_id="r", turn_id="t", events=[], mode="normal",
            reasoning_effort="none", context_compaction={}, context_token_estimate=10,
        )
    assert digest
    assert "需要诊断的正文" in caplog.text
    assert '"message_manifest"' in caplog.text


def test_disabled_outcome_does_not_serialize_diagnostics(monkeypatch):
    monkeypatch.setattr(context_audit.logger, "disabled", True)
    serialize = Mock(side_effect=AssertionError("关闭诊断不能序列化完成记录"))
    monkeypatch.setattr(context_audit, "_canonical", serialize)
    context_audit.capture_request_outcome(conversation_id="c", request_id="r", turn_id="t",
                                          wire_sha256="digest", outcome="completed")
    serialize.assert_not_called()
