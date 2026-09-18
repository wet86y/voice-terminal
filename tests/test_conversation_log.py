"""The conversation log is the rail's only ordering source.

The product rail used to merge three unrelated projections on the client, which
meant a system-owned line (the KWS wake acknowledgement) had no stable position
and drifted to the bottom of the list.  These tests pin the server-side
contract that replaced it.
"""

from voice_terminal.session.conversation_log import (
    ASSISTANT_SPEECH,
    SYSTEM_SPEECH,
    USER_TURN,
    ConversationLog,
    speech_kind,
)
from voice_terminal.session.ledger import SessionLedger


def test_entries_keep_the_order_they_were_created_in():
    log = ConversationLog()
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="第一句")
    # The wake acknowledgement of the *next* turn arrives before that turn.
    log.append(SYSTEM_SPEECH, conversation_id="conversation_a", turn_id="wake_1",
               speech_id="speech_wake", text="我在，请说。", status="completed")
    log.append(ASSISTANT_SPEECH, conversation_id="conversation_a", turn_id="input_1",
               speech_id="speech_1", text="回复一", status="completed")
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_2", text="第二句")

    assert [entry["seq"] for entry in log.entries] == [1, 2, 3, 4]
    assert [entry["text"] for entry in log.entries] == ["第一句", "我在，请说。", "回复一", "第二句"]
    # The later turn is appended after the acknowledgement instead of pushing
    # it down: nothing is ever reordered by a later arrival.
    assert log.entries[1]["kind"] == SYSTEM_SPEECH
    assert log.entries[2]["kind"] == ASSISTANT_SPEECH


def test_re_appending_a_turn_refreshes_it_without_moving_it():
    log = ConversationLog()
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="进行中", status="pending")
    log.append(ASSISTANT_SPEECH, conversation_id="conversation_a", turn_id="input_1",
               speech_id="speech_1", text="回复一", status="completed")
    refreshed = log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1",
                           text="最终转写", status="empty_input")

    assert refreshed["seq"] == 1
    assert refreshed["status"] == "empty_input"
    assert refreshed["text"] == "最终转写"
    assert [entry["text"] for entry in log.entries] == ["最终转写", "回复一"]
    assert log.latest_seq == 2


def test_a_settled_transaction_is_one_line_per_speech_id():
    log = ConversationLog()
    log.append(ASSISTANT_SPEECH, conversation_id="conversation_a", turn_id="input_1",
               speech_id="speech_1", text="被打断的开头", status="interrupted",
               delivery={"status": "partial", "termination": "interrupted"})
    # A replacement is a new transaction, so it gets its own line.
    log.append(ASSISTANT_SPEECH, conversation_id="conversation_a", turn_id="input_1",
               speech_id="speech_2", text="替换后的回答", status="completed")

    assert len(log.entries) == 2
    assert log.entries[0]["delivery"] == {"status": "partial", "termination": "interrupted"}
    assert log.entries[1]["status"] == "completed"


def test_snapshot_is_a_copy_with_the_head_seq():
    log = ConversationLog()
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="内容")
    snapshot = log.snapshot()
    snapshot["entries"][0]["text"] = "被改写的副本"

    assert log.entries[0]["text"] == "内容"
    assert log.snapshot() == {"entries": [dict(log.entries[0])], "latest_seq": 1}


def test_the_log_is_bounded_and_trimmed_entries_can_return_at_the_tail():
    log = ConversationLog(limit=2)
    for index in range(3):
        log.append(USER_TURN, conversation_id="conversation_a", turn_id=f"input_{index}",
                   text=f"第 {index} 句")

    assert [entry["turn_id"] for entry in log.entries] == ["input_1", "input_2"]
    assert log.latest_seq == 3
    # A trimmed identity is forgotten, so re-appending it lands at the tail
    # instead of resurrecting an old position.
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_0", text="迟到")
    assert [entry["turn_id"] for entry in log.entries] == ["input_2", "input_0"]


def test_every_append_is_published_and_reset_restarts_the_order():
    published = []
    log = ConversationLog()
    log.set_sink(published.append)
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="内容")
    log.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="内容（更终）")

    assert [entry["text"] for entry in published] == ["内容", "内容（更终）"]
    assert published[1]["seq"] == published[0]["seq"]

    log.reset()
    assert log.entries == [] and log.latest_seq == 0
    log.append(USER_TURN, conversation_id="conversation_b", turn_id="input_1", text="新会话")
    assert log.entries[0]["seq"] == 1


def test_system_checks_are_their_own_conversation_kind():
    assert speech_kind("system_check") == SYSTEM_SPEECH
    assert speech_kind(None) == ASSISTANT_SPEECH
    assert speech_kind("user") == ASSISTANT_SPEECH


def test_session_reset_clears_the_conversation_log():
    ledger = SessionLedger()
    ledger.conversation.append(USER_TURN, conversation_id="conversation_a", turn_id="input_1", text="内容")

    ledger.reset()

    assert ledger.conversation.entries == []
    assert ledger.conversation.latest_seq == 0
