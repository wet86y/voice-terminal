"""Hotword integration（热词集成）：真实音素算法、标点、代次和超时。"""
import asyncio
from copy import deepcopy
import threading
import time
from types import SimpleNamespace

import pytest

from voice_terminal.asr.hotwords import HotwordService, parse_hotword_text, validate_hotwords, to_hotword_text
from voice_terminal.agent.sentences import SentenceStore
from voice_terminal.config import AppConfig
from voice_terminal.server.product import ProductSettingsStore


def settings(word="Claude", alias="克劳得"):
    return {"enabled": True, "threshold": .85,
            "entries": [{"word": word, "aliases": [alias], "blacklist": []}]}


def test_import_export_and_validation():
    rows = parse_hotword_text("\ufeff# 注释\nClaude | 克劳德\nClaude | 克劳得 ~~~ 天气\n科大讯飞\n")
    assert rows[0] == {"word": "Claude", "aliases": ["克劳德", "克劳得"], "blacklist": ["天气"]}
    assert parse_hotword_text(to_hotword_text(rows)) == rows
    for bad in ("Claude |", "Claude ~~~", "Claude ~~~ a ~~~ b"):
        with pytest.raises(ValueError):
            parse_hotword_text(bad)
    with pytest.raises(ValueError):
        validate_hotwords({**settings(), "threshold": float("nan")})


@pytest.mark.asyncio
async def test_real_correction_preserves_punctuation_and_raw_sentence_ids():
    service = HotwordService()
    try:
        await service.configure(settings())
        store = SentenceStore(6, 20)
        raw = "请让克劳得检查一下。"
        source = store.feed(raw)["sentences"]
        snapshot = deepcopy(source)
        corrected = await service.sentences(source, ("turn", 1))
        assert corrected[0]["text"] == "请让Claude检查一下。"
        assert corrected[0]["sentence_id"] == source[0]["sentence_id"]
        assert corrected[0]["raw_text"] == raw
        assert source == snapshot and store.emitted == raw
        final = await service.finalized(raw + "谢谢。", ("turn", 1))
        assert final["text"] == "请让Claude检查一下。谢谢。"
        assert final["raw_text"] == raw + "谢谢。"
        await service.configure({**settings(), "entries": [{"word": "科大讯飞", "aliases": [], "blacklist": []}]})
        result = await service.correct("科大，迅飞。", ("new", 1))
        assert result["text"] == "科大，迅飞。"  # 禁止替换跨越已确定标点边界。
        result = await service.correct("科大. 迅飞.", ("english-punctuation", 1))
        assert result["text"] == "科大. 迅飞."
        await service.configure({**settings(), "entries": [{"word": "Llama.cpp", "aliases": ["拉马点CPP"], "blacklist": []}]})
        assert (await service.correct("请用拉马点CPP。", "dotted-name"))["text"] == "请用Llama.cpp。"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_settings_are_pinned_per_turn_and_disabled_next_turn():
    service = HotwordService()
    try:
        await service.configure(settings())
        service._turn(("old", 1))
        await service.configure({**settings(), "enabled": False})
        assert (await service.correct("克劳得。", ("old", 1)))["text"] == "Claude。"
        assert (await service.correct("克劳得检查。", ("old", 2)))["text"] == "Claude检查。"  # 同轮续段仍用旧词表。
        assert (await service.correct("克劳得。", ("new", 1)))["text"] == "克劳得。"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_startup_reads_settings_after_inflight_save():
    service = HotwordService()
    current = settings()
    try:
        async with service.update_lock:
            task = asyncio.create_task(service.configure(lambda: current))
            await asyncio.sleep(0)
            current = {**settings(), "enabled": False}
        await task
        assert service.bank.settings["enabled"] is False
    finally:
        service.close()


@pytest.mark.asyncio
async def test_forced_split_and_timeout_get_final_correction():
    service = HotwordService()
    try:
        await service.configure({**settings(), "entries": [{"word": "科大讯飞", "aliases": [], "blacklist": []}]})
        store = SentenceStore(2, 6)
        raw = "请看看科大迅飞的结果。"
        rows = store.feed(raw)["sentences"]
        projected = await service.sentences(rows, "forced")
        assert "科大讯飞" not in "".join(row["text"] for row in projected)
        final = await service.finalized(raw, "forced")
        assert final["text"] == "请看看科大讯飞的结果。"
        store.feed(raw, final=True)
        raw_rows = [row.snapshot() for row in store.sentences]
        final_rows = service.final_sentences(raw_rows, raw, final, "forced")
        assert "".join(row["text"] for row in final_rows) == final["text"]
        assert [row["sentence_id"] for row in final_rows] == [row["sentence_id"] for row in raw_rows]
        assert "".join(row["raw_text"] for row in final_rows) == raw
        await service.configure(settings())
        guarded = SentenceStore(2, 6, boundary_adjuster=lambda text, limit: service.protect_boundary(text, limit, "guarded", 2))
        rows = guarded.feed("我们先请克劳得检查模型。")["sentences"]
        assert any("克劳得" in row["text"] for row in rows)
        projected = await service.sentences(rows, "guarded")
        assert "Claude" in "".join(row["text"] for row in projected)
        turn = service._turn("retry")
        turn["sealed"] = [{"text": "克劳得。", "raw_text": "克劳得。", "hotword_matches": [], "hotword_status": "timeout"}]
        assert (await service.finalized("克劳得。", "retry"))["text"] == "Claude。"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_controller_projects_short_sentences_without_changing_source():
    from unittest.mock import AsyncMock
    from voice_terminal.controller.realtime_controller import RealtimeController
    from voice_terminal.controller.transcript_segmenter import TranscriptSegmenter
    from voice_terminal.controller.turn_workspace import TurnWorkspace
    service = HotwordService()
    try:
        await service.configure(settings())
        ws = TurnWorkspace("input", generation=1)
        controller = SimpleNamespace(workspace=ws, segmenter=TranscriptSegmenter(), speaker_mode="off",
                                     hotwords=service, _detail=AsyncMock())
        await RealtimeController._segment(controller, "请让克劳得检查一下。")
        assert ws.segments[0]["text"] == "请让Claude检查一下。"
        assert controller.segmenter.store.sentences[0].text == "请让克劳得检查一下。"
        payload = controller._detail.call_args.kwargs
        assert payload["changed_ids"] == [1]
        assert payload["sentences"][0]["raw_text"] == "请让克劳得检查一下。"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_timeout_does_not_queue_or_apply_late_result():
    service = HotwordService()
    started, release = threading.Event(), threading.Event()
    class SlowMatcher:
        def correct(self, text):
            started.set()
            release.wait(2)
            return SimpleNamespace(text="错误的迟到结果", matches=[])
    service.bank.matchers = {"stream": SlowMatcher()}
    try:
        task = asyncio.create_task(service.correct("原始文本。", "turn"))
        while not started.is_set():
            await asyncio.sleep(.001)
        first = await task
        second = await service.correct("更新文本。", "turn")
        assert first["text"] == "原始文本。" and first["status"] == "timeout"
        assert second["text"] == "更新文本。" and second["status"] == "busy"
        release.set()
        await asyncio.shield(service.pending["stream"])
        assert (await service.correct("原始文本。", "turn"))["text"] == "原始文本。"
    finally:
        release.set()
        service.close()


def test_persistence_and_failed_save_keep_previous_settings(tmp_path, monkeypatch):
    store = ProductSettingsStore(path=tmp_path / "product.json", config=AppConfig())
    store.save_hotwords(settings())
    assert ProductSettingsStore(path=store.path, config=AppConfig()).settings_only()["hotwords"] == settings()
    monkeypatch.setattr(store, "_save_settings", lambda: (_ for _ in ()).throw(OSError("write failed")))
    with pytest.raises(OSError):
        store.save_hotwords({**settings(), "enabled": False})
    assert store.settings_only()["hotwords"] == settings()


@pytest.mark.asyncio
async def test_reset_keeps_busy_native_lane_until_worker_really_exits():
    service = HotwordService()
    release = threading.Event()
    calls = []
    class SlowMatcher:
        def correct(self, text):
            calls.append(text)
            release.wait(2)
            return SimpleNamespace(text=text, matches=[])
    service.bank.matchers = {"stream": SlowMatcher()}
    executor = service.executors["stream"]
    try:
        first = await service.correct("重置前输入。", "old")
        assert first["status"] == "timeout"
        service.clear()
        second = await service.correct("重置后输入。", "new")
        assert second["status"] == "busy" and calls == ["重置前输入"]
        assert service.executors["stream"] is executor
        release.set()
        await asyncio.shield(service.pending["stream"])
        assert (await service.correct("可运行输入。", "next"))["status"] == "unchanged"
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_late_refinement_keeps_both_raw_sources_and_corrects_agent_input():
    from unittest.mock import AsyncMock
    from voice_terminal.controller.episode_controller import EpisodeController
    from voice_terminal.controller.turn_workspace import TurnWorkspace
    from voice_terminal.session.ledger import SessionLedger
    service = HotwordService()
    try:
        await service.configure(settings())
        ws = TurnWorkspace("turn", generation=1)
        ws.paraformer_final = ws.raw_final_text = "请让克劳得检查。"
        ws.final_text = "请让Claude检查。"
        ws.refined_text = ""
        ws.episode_id = "episode"
        ledger = SessionLedger()
        ledger.commit_input(ws.turn_id, ws.final_text, details={"raw_text": ws.raw_final_text})
        controller = object.__new__(EpisodeController)
        controller.hotwords = service
        controller.wake_enabled = False
        controller.workspace = ws
        controller._refinement_events = set()
        controller._refinement_agent_events = set()
        controller.history = ledger
        controller.episode_history = {}
        controller._detail = AsyncMock()
        controller.episode = SimpleNamespace(episode_id="episode", closed=False,
                                            inputs={"turn": {"final": True}}, observe=AsyncMock())
        raw_refined = "请让克劳得检查代码。"
        await controller._refinement_completed(ws, {"text": raw_refined, "segments": []})
        entry = next(entry for entry in ledger.entries if entry.kind == "input")
        assert entry.metadata["raw_text"] == "请让克劳得检查。"
        assert entry.metadata["raw_refined_text"] == raw_refined
        assert controller.episode.observe.call_args.kwargs["text"] == "请让Claude检查代码。"
        assert ws.paraformer_final == "请让克劳得检查。"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_speaker_recovery_corrects_only_admitted_text_and_preserves_source():
    from unittest.mock import AsyncMock
    from voice_terminal.controller.episode_controller import EpisodeController
    service = HotwordService()
    try:
        await service.configure(settings())
        raw = "请让克劳得检查。"
        controller = object.__new__(EpisodeController)
        controller.hotwords = service
        controller.speaker_mode = "filter"
        controller.workspace = SimpleNamespace(turn_id="turn")
        controller.speaker_policy = SimpleNamespace(activation_id="activation", admitted_text=lambda _: raw,
            admitted_turns=lambda: [{"turn_id": "turn", "generation": 1, "text": raw}])
        controller._speaker_published = {}
        controller._detail = AsyncMock()
        controller.episode = SimpleNamespace(observe=AsyncMock())
        await controller._flush_speaker_admissions()
        item = controller.episode.observe.call_args.kwargs["entries"][0]
        assert item["text"] == "请让Claude检查。" and item["raw_text"] == raw
        await controller._flush_speaker_admissions()
        assert controller.episode.observe.await_count == 1
    finally:
        service.close()
