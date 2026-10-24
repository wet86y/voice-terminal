"""声纹参数实际消费、全文审计和主/后台上下文的关联回归。"""
from dataclasses import replace
import json
import logging
import subprocess
import sys
from unittest.mock import Mock

import pytest

from voice_terminal.config import SpeakerConfig
from voice_terminal.session.context import CurrentInput, LLMContextProjector
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.speaker.audit import audit, parameter_snapshot, parameter_effects, transcript_audit
from voice_terminal.speaker.session import SessionSpeakerPolicy


def test_full_text_audit_records_revisions_differences_and_chinese_fields(caplog):
    caplog.set_level(logging.INFO, logger="voice_terminal.llm_context_audit")
    transcript_audit("turn-a", "admitted（实际放行）", "首句开头完整😀", "开头完整😀", 2,
                     settings_revision=4, generation=1)
    record = next(json.loads(item.message) for item in caplog.records
                  if item.name == "voice_terminal.llm_context_audit")
    assert record["text"] == "首句开头完整😀"
    assert record["previous_text"] == "开头完整😀"
    assert record["changed_start"] == 0
    assert record["changed_end"] == 2
    assert record["previous_changed_end"] == 0
    assert record["settings_revision"] == 4 and record["revision"] == 2
    assert record["field_labels"]["settings_revision"] == "设置版本"
    assert record["field_labels"]["text"] == "完整文字"


def test_parameter_audit_correlates_saved_values_and_actual_scoring(caplog):
    caplog.set_level(logging.INFO, logger="voice_terminal.llm_context_audit")
    p = SessionSpeakerPolicy(SpeakerConfig(), lambda _: .4)
    turn = p.begin_turn("a", start_sample=0)
    p.observe_turn("a", (1., 0.), start=0, end=19200, speech_ms=1200, first=True)
    assert turn.scores["effective_session_weight"] == 0
    p.session_centroid = (1., 0.)
    p.tune(replace(p.config, session_weight=.75))
    p.observe_turn("a", (1., 0.), start=0, end=25600, speech_ms=400, window_speech_ms=1600)
    records = [json.loads(item.message) for item in caplog.records
               if item.name == "voice_terminal.llm_context_audit"]
    applied = next(item for item in records if item["kind"].startswith("settings_applied"))
    assert applied["effective"]["session_weight"] == .75
    result = [item for item in records if item["kind"].startswith("inference_result")][-1]
    assert result["settings_revision"] == applied["settings_revision"]
    assert result["effective_session_weight"] == .75
    assert result["combined_score"] == pytest.approx(.85)
    assert result["speech_ms"] == 1600 and result["new_speech_ms"] == 400
    assert result["threshold_used"] == .6
    assert result["identity_result"] == "owner"
    assert "当次阈值二分" in result["field_labels"]["identity_result"]
    assert "non_owner_threshold" not in result and "non_owner_threshold" not in applied["effective"]


def test_audit_failure_does_not_abort_input_policy(monkeypatch):
    monkeypatch.setattr("voice_terminal.speaker.audit.content_logger.info", Mock(side_effect=OSError("磁盘不可写")))
    p = SessionSpeakerPolicy(SpeakerConfig(), lambda _: .8)
    turn = p.begin_turn("a", start_sample=0)
    p.set_turn_text("a", "原文完整")
    p.observe_turn("a", (1., 0.), start=0, end=19200, speech_ms=1200, first=True)
    assert turn.admitted and p.admitted_text("a") == "原文完整"


def test_parameter_snapshot_is_allowlisted_and_has_effective_boundaries():
    result = parameter_snapshot({"confirm_threshold": .7, "session_weight": .6, "api_key": "不能导出"})
    assert result == {"confirm_threshold": .7, "session_weight": .6}
    effects = parameter_effects()
    assert "session_weight" in effects["next_judgment（下次判断）"]
    assert "calibration_speech_ms" in effects["next_activation（下次激活）"]
    assert "enrollment_min_total_speech_ms" in effects["next_enrollment（下次登记）"]


def test_speaker_summary_is_separate_from_transcript_and_reaches_shadow(tmp_path):
    ledger = SessionLedger()
    summary = {"admitted": True, "foreign_voice_suspected": True,
               "interpretation": "疑似异声，具体文字归属未确定"}
    ledger.commit_input("a", "完整原话", details={"speaker_summary": summary})
    messages = ledger.context_messages("")
    user = [item for item in messages if item["role"] == "user"]
    assert len(user) == 1
    assert json.loads(user[0]["content"].removeprefix("用户语音最终文本：")) == {"asr_final": "完整原话"}
    assert any(item["role"] == "system" and "疑似异声" in item["content"] for item in messages)
    before = ledger.context_messages("")
    current = CurrentInput(text="完整原话", turn_id="a", speaker_summary=summary)
    tail = LLMContextProjector.runtime_tail(current=current, speak_state={"state": "empty"})
    assert json.loads(tail.removeprefix("当前轮运行状态："))["runtime"]["speaker_summary"] == summary
    ledger.project_shadow_speaker_summary("a", summary)
    revision = ledger.shadow_input_revision
    ledger.project_shadow_speaker_summary("a", summary)
    assert ledger.shadow_input_revision == revision
    assert ledger.shadow_context_snapshot().frames[-1].kind == "speaker_annotation"
    assert ledger.shadow_context_snapshot().frames[-1].origin == "runtime"
    assert ledger.context_messages("") == before
    ledger.reset()
    ledger.project_shadow_speaker_summary("a", summary)
    assert ledger.shadow_input_revision == 1


def test_closed_record_budget_preserves_active_input():
    p = SessionSpeakerPolicy(SpeakerConfig(pending_max_turns=1), lambda _: .8)
    for name in ("a", "b"):
        p.begin_turn(name, start_sample=0)
        p.close_turn(name)
    p.begin_turn("active", start_sample=0)
    p.set_turn_text("active", "当前完整原文")
    assert "a" not in p.turns and "b" in p.turns
    assert p.turns["active"].text == "当前完整原文"


def test_enrollment_uses_settings_snapshot_from_its_start():
    from types import SimpleNamespace
    from voice_terminal.server.websocket import SessionRuntime
    runtime = SessionRuntime.__new__(SessionRuntime)
    runtime._speaker_enrollment = {}
    runtime.config = SimpleNamespace(speaker=SpeakerConfig())
    first = runtime.start_speaker_enrollment()["enrollment_id"]
    runtime.config.speaker = replace(runtime.config.speaker, enrollment_min_total_speech_ms=18000)
    second = runtime.start_speaker_enrollment()["enrollment_id"]
    assert runtime._speaker_enrollment[first]["config"].enrollment_min_total_speech_ms == 15000
    assert runtime._speaker_enrollment[second]["config"].enrollment_min_total_speech_ms == 18000


def test_reconnect_snapshot_preserves_admission_metadata_and_filters_extra_fields():
    from voice_terminal.server.product import sanitize_runtime_snapshot
    summary = {"admitted": True, "initial_identity": "owner", "observation_state": "owner_continuity",
               "foreign_voice_suspected": True, "private_extra": "不属于声纹摘要"}
    snapshot = sanitize_runtime_snapshot({"current": {"admission_phase": "relaxed", "speaker_summary": summary}})
    current = snapshot["conversation"]["current"]
    assert current["admission_phase"] == "relaxed"
    assert current["speaker_summary"]["observation_state"] == "owner_continuity"
    assert current["speaker_summary"]["foreign_voice_suspected"]
    assert "private_extra" not in current["speaker_summary"]


def test_async_log_channels_keep_full_text_out_of_control_log(tmp_path):
    """真实队列写入与关闭排空，验证导出所用两个现有文件。"""
    script = """
import logging, sys
from pathlib import Path
from voice_terminal.logging_setup import configure_logging, _stop_context_audit
from voice_terminal.application.desktop import DesktopControl
from voice_terminal.speaker.audit import audit, transcript_audit
configure_logging(Path(sys.argv[1]) / 'voice-server.log')
audit('settings_applied（参数已应用）', settings_revision=8, effective={'confirm_threshold': .7})
transcript_audit('a', 'admitted（放行）', '完整句首专用测试文字', '', 1, settings_revision=8)
control = DesktopControl.__new__(DesktopControl)
messages = []
control.send = lambda kind, **values: messages.append((kind, values))
control._flush_logs(13)
assert messages == [('logs_flushed', {'request_id': 13})]
assert '完整句首专用测试文字' in (Path(sys.argv[1]) / 'voice-server-llm-context.jsonl').read_text(encoding='utf-8')
_stop_context_audit()
logging.shutdown()
"""
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script, str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
    control = (tmp_path / "voice-server.log").read_text(encoding="utf-8")
    assert "完整句首专用测试文字" not in control
    assert '"settings_revision":8' in control
    records = [json.loads(line) for line in (tmp_path / "voice-server-llm-context.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(item.get("text") == "完整句首专用测试文字" and item["settings_revision"] == 8 for item in records)
