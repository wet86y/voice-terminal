from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from voice_terminal.agent.episode import AgentEpisodeRuntime
from voice_terminal.agent.persona import PersonaDocumentLoader
from voice_terminal.agent.prompts import PERSONA_DOCUMENT_POLICY
from voice_terminal.config import AppConfig, PersonaConfig
from voice_terminal.paths import AppPaths
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.session.context_frames import ContextLane
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.runtime import ConversationRuntime


def _write_documents(directory: Path, *, suffix: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "instructions.md").write_bytes(f"# 指令{suffix}\n等待定稿。\n".encode("utf-8"))
    (directory / "personality.md").write_bytes(f"# 人格{suffix}\n保持平静。\n".encode("utf-8"))
    (directory / "user.md").write_bytes(f"# 用户{suffix}\n称呼：小林。\n".encode("utf-8"))


def test_persona_config_resolves_relative_directory_from_project_root(tmp_path, monkeypatch):
    # 本用例验证源码相对路径，以没有 portable.json（工作区布局）的独立路径上下文执行。
    monkeypatch.setattr(AppPaths, "resolve", staticmethod(lambda _root: AppPaths(tmp_path, tmp_path)))
    config = AppConfig.from_mapping(
        {"persona": {"directory": "profiles", "user_file": "current.md"}},
        base_dir=tmp_path,
    )

    assert config.persona.directory == tmp_path / "profiles"
    assert config.persona.personality_file == "personality.md"
    assert config.persona.user_file == "current.md"
    assert config.persona.instructions_file == "instructions.md"


def test_persona_loader_preserves_markdown_and_records_status(tmp_path):
    _write_documents(tmp_path)

    documents = PersonaDocumentLoader(PersonaConfig(directory=tmp_path)).load()

    assert documents.personality == "# 人格\n保持平静。\n"
    assert documents.user == "# 用户\n称呼：小林。\n"
    assert [status.name for status in documents.statuses] == ["personality", "user"]
    assert all(status.loaded for status in documents.statuses)
    assert all(status.chars > 0 for status in documents.statuses)


def test_persona_loader_skips_missing_empty_and_invalid_documents(tmp_path, caplog):
    (tmp_path / "instructions.md").write_text("\n  ", encoding="utf-8")
    (tmp_path / "personality.md").write_bytes(b"\xff\xfe")
    # user.md remains missing.

    with caplog.at_level(logging.WARNING, logger="voice_terminal.agent.persona"):
        documents = PersonaDocumentLoader(PersonaConfig(directory=tmp_path)).load()

    assert documents == documents.__class__(statuses=documents.statuses)
    assert {status.reason for status in documents.statuses} == {"invalid_utf8", "missing"}
    assert "agent.persona_document_skipped" in caplog.text
    assert "document=instructions" not in caplog.text
    assert "document=personality" in caplog.text
    assert "document=user" in caplog.text


def test_persona_documents_are_static_and_projected_into_responses_in_order(tmp_path):
    _write_documents(tmp_path)

    class Registry:
        def action_definitions(self):
            return []

        def begin_turn(self, _episode_id):
            return None

        def cancel_turn(self, _episode_id):
            return None

    async def emit(*_args):
        return None

    async def scenario():
        output = SimpleNamespace(
            status="IDLE",
            speak_snapshot={"queue_status": "empty", "text_only_queue": True},
        )
        conversation = ConversationRuntime()
        runtime = AgentEpisodeRuntime(
            None,
            Registry(),
            output,
            emit,
            conversation=conversation,
            ledger=SessionLedger(),
            persona_config=PersonaConfig(directory=tmp_path),
        )
        await runtime.ensure_episode(turn_id="turn_a", text="你好")
        first = runtime.messages({"USER_INPUT_FINALIZED"})
        first_static = [
            message["content"]
            for message in first
            if message["context_lane"] == ContextLane.STATIC_INSTRUCTIONS
        ]
        first_rendered = "\n".join(first_static)
        assert "# 指令\n等待定稿" not in first_rendered
        assert "# 人格" in first_rendered
        assert "# 用户" in first_rendered
        assert first_rendered.index("人格主体文档") < first_rendered.index("用户资料文档")
        assert PERSONA_DOCUMENT_POLICY in first_static

        payload = DeepSeekClient._responses_payload(
            first,
            model="deepseek",
            schema={},
            schema_name="decision",
            reasoning_effort="none",
            max_output_tokens=100,
        )
        assert "# 指令\n等待定稿" not in payload["instructions"]
        assert "# 人格" in payload["instructions"]
        assert "# 用户" in payload["instructions"]
        assert all(
            "# 人格" not in item["content"][0]["text"]
            and "# 用户" not in item["content"][0]["text"]
            for item in payload["input"]
        )

        digest_before = runtime._canonical_digest(first_static)
        (tmp_path / "personality.md").write_bytes("# 人格新版本\n".encode("utf-8"))
        second = runtime.messages({"USER_INPUT_FINALIZED"})
        second_static = [
            message["content"]
            for message in second
            if message["context_lane"] == ContextLane.STATIC_INSTRUCTIONS
        ]
        assert "# 人格新版本" not in "\n".join(second_static)
        assert runtime._canonical_digest(second_static) == digest_before

        if runtime._tokenizer_prewarm_task is not None:
            await runtime._tokenizer_prewarm_task

    asyncio.run(scenario())


def test_new_episode_reads_changed_persona_documents(tmp_path):
    _write_documents(tmp_path, suffix="一")

    class Registry:
        def action_definitions(self):
            return []

        def begin_turn(self, _episode_id):
            return None

    async def emit(*_args):
        return None

    async def make_runtime(conversation):
        output = SimpleNamespace(
            status="IDLE",
            speak_snapshot={"queue_status": "empty", "text_only_queue": True},
        )
        runtime = AgentEpisodeRuntime(
            None,
            Registry(),
            output,
            emit,
            conversation=conversation,
            ledger=SessionLedger(),
            persona_config=PersonaConfig(directory=tmp_path),
        )
        await runtime.ensure_episode(turn_id="turn", text="你好")
        if runtime._tokenizer_prewarm_task is not None:
            await runtime._tokenizer_prewarm_task
        return runtime

    async def scenario():
        conversation = ConversationRuntime()
        first = await make_runtime(conversation)
        assert "# 人格一" in first.persona_documents.personality

        (tmp_path / "personality.md").write_bytes("# 人格二\n".encode("utf-8"))
        second = await make_runtime(conversation)
        assert "# 人格二" in second.persona_documents.personality

    asyncio.run(scenario())
