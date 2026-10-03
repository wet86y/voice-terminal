import json
import sqlite3
import time
from unittest.mock import patch

from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.contracts import DeliveryReport
from voice_terminal.session.storage import SessionJournal, StoredSequence


def test_disk_history_matches_memory_with_eviction_refinement_delivery_and_reset(tmp_path):
    memory = SessionLedger(); disk = SessionLedger(storage_dir=tmp_path, cache_bytes=256)
    try:
        for i in range(30):
            for ledger in (memory, disk):
                ledger.commit_input(str(i), "用户原文" * 20)
                ledger.commit_semantic_event("notice", {"text": "事件内容" * 20})
        assert disk.journal.flush()
        assert disk.journal.cache_size <= 256
        for ledger in (memory, disk):
            ledger.update_input_refinement("0", "迟到精修", details={"history_content": "禁止改写"})
            ledger.project_shadow_current_turn("active", "当前未结算输入")
        assert disk.context_messages("") == memory.context_messages("")
        assert disk.shadow_context_snapshot() == memory.shadow_context_snapshot()
        assert disk.shadow_context_update(20, epoch=1) == memory.shadow_context_update(20, epoch=1)
        assert disk.has_input("0") and disk.input_refinement("0") == "迟到精修"
        assert disk.journal.flush()
        old_path = disk.journal.path
        disk.reset()
        assert disk.context_messages("") == []
        assert disk.shadow_input_revision == 0
        assert not disk.has_input("0")
        with sqlite3.connect(old_path) as connection:
            assert connection.execute("SELECT count(*) FROM records").fetchone()[0] > 60
    finally:
        assert disk.close_storage()


def test_journal_preserves_pending_data_on_disk_failure_and_retries(tmp_path):
    journal = SessionJournal(tmp_path, cache_bytes=0)
    sequence = StoredSequence(journal, "entries")
    try:
        # 真实 SQLite（本地数据库）只读错误：已写成功的记录仍可读取，失败正文不释放。
        with sqlite3.connect(journal.path) as connection:
            connection.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON records BEGIN SELECT RAISE(FAIL, 'disk failure'); END;")
        sequence.append({"kind": "event", "content": "完整待写正文"})
        assert not journal.flush()
        assert sequence[0]["content"] == "完整待写正文"
        assert journal.pending_bytes > 0
        assert not journal.close()
        with sqlite3.connect(journal.path) as connection:
            connection.execute("DROP TRIGGER fail_insert")
        sequence[0] = sequence[0]
        assert journal.flush()
        assert journal.pending_bytes == 0
        assert sequence[0]["content"] == "完整待写正文"
    finally:
        assert journal.close()


def test_metadata_written_again_after_eviction_keeps_frozen_prefix(tmp_path):
    ledger = SessionLedger(storage_dir=tmp_path, cache_bytes=0)
    try:
        ledger.commit_input("first", "第一版")
        assert ledger.journal.flush()
        before = ledger.context_messages("")
        ledger.update_input_refinement("first", "修正版")
        assert ledger.journal.flush()
        assert ledger.context_messages("") == before
        assert ledger.input_refinement("first") == "修正版"
    finally:
        ledger.close_storage()


def test_readable_document_and_archive_identity_keep_complete_records(tmp_path):
    from voice_terminal.handoff import PairedArchiveStore, RecordView, stable_id
    journal = SessionJournal(tmp_path / "journals", cache_bytes=128)
    sequence = StoredSequence(journal, "complete")
    items = [{"kind": "input", "content": "第一条"}, {"kind": "event", "content": "长文" * 10000}]
    for item in items:
        sequence.append(item)
    assert journal.flush()
    store = PairedArchiveStore(tmp_path / "archives", streaming=True)
    view = RecordView((sequence, lambda value: value))
    archive = store.save(voice_session_id="voice", dsh_session_id="", cwd="cwd",
                         voice_records=view, dsh_records=[], captures=[])
    expected = stable_id("voice", "", "cwd", json.dumps({"voice": items, "dsh": []}, ensure_ascii=False, sort_keys=True))
    assert archive.archive_id == expected
    source = json.loads((archive.directory / "source.json").read_text(encoding="utf-8"))
    assert source["voice"] == items
    assert (archive.directory / "voice-000002.txt").is_file()
    assert journal.close()
    assert "第一条" in journal.path.with_suffix(".md").read_text(encoding="utf-8")


def test_native_shadow_record_view_keeps_raw_archive_and_filtered_records(tmp_path):
    from voice_terminal.handoff import PairedArchiveStore, RecordView
    raw = [{"type": "assistant/message", "data": {"message": {"content": "后台完整正文"}}}]
    view = RecordView((raw, lambda value: value))
    archive = PairedArchiveStore(tmp_path, streaming=True).save(
        voice_session_id="voice", dsh_session_id="native", cwd="cwd",
        voice_records=[], dsh_records=view, captures=[])
    assert json.loads((archive.directory / "source.json").read_text(encoding="utf-8"))["dsh"] == raw
    assert archive.dsh_records[0]["data"] == raw[0]["data"]
