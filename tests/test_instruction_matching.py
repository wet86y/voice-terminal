"""自定义指令检索：真实音素候选、隔离、缓存及超时恢复。"""
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from voice_terminal.custom_instructions import validate_entries, voice_projection, shadow_projection
from voice_terminal.instruction_matching import InstructionMatcher, InstructionBank


def entry(identifier="quiet", keywords=None, **values):
    return {"id": identifier, "trigger": "用户要求进入静默", "target": "shadow",
            "instruction": "使用提前静默工具。", "enabled": True,
            "keywords": ["静默"] if keywords is None else keywords, **values}


def test_keywords_optional_validation_and_projection():
    legacy = entry()
    legacy.pop("keywords")
    assert validate_entries([legacy]) == [legacy]
    rule = validate_entries([entry(keywords=[" 静默 ", "退下", "静默"])])[0]
    assert rule["keywords"] == ["静默", "退下"]
    assert "keywords" not in voice_projection([rule])
    assert "keywords" not in shadow_projection([rule])[0]
    for invalid in ("静默", None, ["静"], ["静默状态呀"], ["abc"], ["退。下"], [1]):
        with pytest.raises(ValueError, match="关键词"):
            validate_entries([{**entry(), "keywords": invalid}])
    assert validate_entries([entry(keywords=[])])[0]["keywords"] == []


def test_optional_display_name_is_saved_but_never_projected():
    rule = validate_entries([{**entry(), "name": " 我的显示名称 "}])[0]
    assert rule["name"] == "我的显示名称"
    assert "我的显示名称" not in voice_projection([rule])
    assert "我的显示名称" not in str(shadow_projection([rule]))
    assert validate_entries([{**entry(), "name": " "}])[0]["name"] == ""
    for name in (None, 1, "长" * 81, "坏\x00名"):
        with pytest.raises(ValueError, match="名称"):
            validate_entries([{**entry(), "name": name}])


@pytest.mark.asyncio
async def test_native_phonetic_candidates_and_sentence_boundaries():
    service = InstructionMatcher()
    rules = [entry(), entry("shared"), entry("close", ["关闭窗口"]),
             entry("open", ["打开设置"]), entry("retreat", ["退下"]),
             entry("disabled", enabled=False)]
    try:
        await service.configure(rules)
        for text, expected in [
            ("进入进墨", {"quiet", "shared"}),
            ("请管闭窗扣", {"close"}),
            ("打凯设制", {"open"}),
            ("推下吧", {"retreat"}),
        ]:
            assert set(await service.match(text, (text,))) == expected
        for punctuation in "，。！？；,!?;\n":
            assert await service.match(punctuation, (f"退{punctuation}下",)) == ()
        assert await service.match("unrelated", ("你好今天天气怎么样",)) == ()
        assert await service.match("evidence", ("你好", "请进入进墨")) == ("quiet", "shared")
    finally:
        service.close()


@pytest.mark.asyncio
async def test_cache_hot_update_clear_and_disable(monkeypatch):
    service = InstructionMatcher()
    calls = []
    native = service.run
    def counted(bank, texts):
        calls.append(texts)
        return native(bank, texts)
    monkeypatch.setattr(service, "run", counted)
    try:
        await service.configure([entry()])
        assert service._preparing is None
        assert await service.match("turn", ("静默",)) == ("quiet",)
        assert await service.match("turn", ("静默",)) == ("quiet",)
        assert service.last_status == "cached" and len(calls) == 1
        assert await service.match("turn", ("你好",)) == ()
        assert len(calls) == 2
        await service.configure([entry("new")])
        assert await service.match("turn", ("静默",)) == ("new",), service.last_status
        await service.configure([entry("new", enabled=False)])
        assert service.bank is None and await service.match("turn", ("静默",)) == ()
        assert service._preparing is None
        await service.configure([entry(keywords=[])])
        assert service.bank is None
    finally:
        service.close()
    assert service._settings is None and service._executor is None
    empty = InstructionMatcher()
    await empty.configure([entry(keywords=[])])
    assert empty._executor is None
    empty.close()


@pytest.mark.asyncio
async def test_timeout_busy_late_result_and_exception(monkeypatch):
    service = InstructionMatcher()
    release = Event()
    calls = []
    monkeypatch.setattr(service, "prepare", lambda _: InstructionBank(None, {}, ()))
    def slow(_bank, _texts):
        calls.append(1)
        release.wait(2)
        return ("quiet",)
    monkeypatch.setattr(service, "run", slow)
    try:
        await service.configure([entry()])
        assert await service.match("turn", ("静默",)) == ()
        assert service.last_status == "timeout"
        for _ in range(5):
            assert await service.match("turn", ("静默",)) == ()
            assert service.last_status == "busy"
        assert len(calls) == 1
        release.set()
        await service._pending
        assert not service._turns["turn"]  # 迟到完成没有写入结果缓存。
        monkeypatch.setattr(service, "run", lambda *_: (_ for _ in ()).throw(ValueError("failure")))
        assert await service.match("turn", ("静默",)) == ()
        assert service.last_status == "error"
        monkeypatch.setattr(service, "run", lambda *_: ("quiet",))
        assert await service.match("turn", ("静默",)) == ("quiet",)
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_update_during_match_rejects_old_generation(monkeypatch):
    service = InstructionMatcher()
    release = Event()
    monkeypatch.setattr(service, "prepare", lambda _: InstructionBank(None, {}, ()))
    def old_result(*_):
        release.wait(2)
        return ("old",)
    monkeypatch.setattr(service, "run", old_result)
    try:
        await service.configure([entry("old")])
        task = asyncio.create_task(service.match("turn", ("静默",)))
        await asyncio.sleep(0)
        await service.configure([entry(keywords=[])])
        release.set()
        assert await task == ()
        assert service.last_status == "stale"
        assert not service._turns
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_frozen_candidates_retries_and_no_system_replay():
    from voice_terminal.agent.episode import AgentEpisodeRuntime
    from collections import OrderedDict
    calls = []
    async def match(turn, texts):
        calls.append((turn, texts))
        return ("quiet",)
    runtime = SimpleNamespace(
        instruction_matcher=SimpleNamespace(match=match), closed=False,
        custom_instructions=[entry(keywords=["荆墨"])],
        inputs={"turn": {"text": "静默", "refined_text": "进墨"}},
        _instruction_candidates_by_turn=OrderedDict(),
    )
    method = AgentEpisodeRuntime._request_instruction_candidates
    first = await method(runtime, "turn", {"USER_INPUT_FINALIZED"})
    assert "用户要求进入静默" in first and "荆墨" not in first and "keywords" not in first
    runtime.inputs["turn"] = {"text": "不需要静默"}
    assert await method(runtime, "turn", {"LLM_RETRY"}) == first
    assert await method(runtime, "turn", {"EXCEPTION_RECOVERY"}, recovery=True) == first
    for kind in ("final_silence", "kws_wake", "secondary_mailbox"):
        assert await method(runtime, "turn", {"SYSTEM_CHECK"}, prompt_kind=kind) == ""
    assert await method(runtime, "turn", {"SHADOW_INFORMATION"}) == ""
    assert len(calls) == 1
    await method(runtime, "turn", {"LLM_RETRY", "USER_INPUT_FINALIZED"})
    assert calls[-1][1] == ("不需要静默",)
    await method(runtime, "turn", {"USER_INPUT_FINALIZED"})
    assert calls[-1][1] == ("不需要静默",)


@pytest.mark.asyncio
async def test_cancelled_configuration_finishes_and_does_not_queue_more(monkeypatch):
    service = InstructionMatcher()
    started, release = Event(), Event()
    calls = []
    def prepare(_):
        calls.append(1)
        started.set()
        release.wait(2)
        return InstructionBank(None, {}, ())
    monkeypatch.setattr(service, "prepare", prepare)
    try:
        task = asyncio.create_task(service.configure([entry()]))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replacement = asyncio.create_task(service.configure([entry()]))
        await asyncio.sleep(0)
        release.set()
        await replacement
        assert service.bank is not None and len(calls) == 1
        assert service.configured_for([entry()])
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_configuration_not_blocked_by_timed_out_matching(monkeypatch):
    service = InstructionMatcher()
    release = Event()
    monkeypatch.setattr(service, "prepare", lambda _: InstructionBank(None, {}, ()))
    def slow(*_):
        release.wait(2)
        return ("old",)
    monkeypatch.setattr(service, "run", slow)
    try:
        await service.configure([entry("old")])
        assert await service.match("turn", ("静默",)) == ()
        assert service.last_status == "timeout" and not service._pending.done()
        await asyncio.wait_for(service.configure([entry("new")]), 0.5)
        assert service.configured_for([entry("new")])
        assert not service._pending.done()
    finally:
        release.set()
        service.close()


@pytest.mark.asyncio
async def test_keyword_api_roundtrip_and_shadow_projection(tmp_path):
    import httpx
    from voice_terminal.config import AppConfig
    from voice_terminal.server.product import ProductSettingsStore
    from voice_terminal.server.websocket import SessionRuntime, create_app

    config = AppConfig()
    store = ProductSettingsStore(path=tmp_path / "product.json", config=config)
    runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "data")
    app = create_app(runtime, product_settings=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/api/v1/product/settings/custom-instructions", json={
                "custom_instructions": [{**entry(keywords=[" 荆墨 ", "退下", "荆墨"]), "name": " 静默显示名称 "}],
            })
            assert response.status_code == 200
            rules = response.json()["settings"]["custom_instructions"]
            assert rules[0]["keywords"] == ["荆墨", "退下"]
            assert rules[0]["name"] == "静默显示名称"
            assert runtime.instruction_matcher.configured_for(rules)
            assert ProductSettingsStore(path=store.path, config=config).settings_only()["custom_instructions"] == rules
            environment = runtime.shadow_context.environment_snapshot()
            assert "keywords" not in environment["custom_instructions"][0]
            assert "荆墨" not in str(environment)
            assert "静默显示名称" not in str(environment)
            rejected = await client.put("/api/v1/product/settings/custom-instructions", json={
                "custom_instructions": [entry(keywords=["静"])],
            })
            assert rejected.status_code == 422
            assert store.settings_only()["custom_instructions"] == rules
            cleared = await client.put("/api/v1/product/settings/custom-instructions", json={
                "custom_instructions": [entry(keywords=[])],
            })
            assert cleared.status_code == 200 and runtime.instruction_matcher.bank is None
    finally:
        runtime.hotwords.close()
        runtime.instruction_matcher.close()
        runtime.history.close_storage()


@pytest.mark.asyncio
async def test_real_request_tail_contains_only_projected_candidates(tmp_path):
    from voice_terminal.agent.episode import AgentEpisodeRuntime
    from voice_terminal.config import PersonaConfig
    from voice_terminal.session.ledger import SessionLedger
    from voice_terminal.session.runtime import ConversationRuntime

    class Registry:
        def action_definitions(self):
            return []
        def begin_turn(self, *_):
            pass
        def cancel_turn(self, *_):
            pass
    class Output:
        status = "IDLE"
        speak_snapshot = {"queue_status": "empty", "text_only_queue": True}
        def cancel_all(self, *_):
            pass
    async def emit(*_):
        pass
    service = InstructionMatcher()
    rules = [entry(keywords=["荆墨"])]
    await service.configure(rules)
    runtime = AgentEpisodeRuntime(None, Registry(), Output(), emit,
        conversation=ConversationRuntime(), ledger=SessionLedger(),
        persona_config=PersonaConfig(directory=tmp_path),
        custom_instructions_provider=lambda: rules, instruction_matcher=service)
    try:
        await runtime.ensure_episode(turn_id="turn", text="静默")
        kinds = {"USER_INPUT_FINALIZED"}
        candidates = await runtime._request_instruction_candidates("turn", kinds)
        assert candidates
        frames = runtime.messages(kinds, instruction_candidates=candidates)
        reminder = next(frame["content"] for frame in frames if "【本次自定义指令核对】" in str(frame["content"]))
        assert "用户要求进入静默" in reminder
        assert "荆墨" not in str(frames) and '"keywords"' not in str(frames)
        assert "使用提前静默工具" not in str(frames)  # 后台执行目标继续隔离。
        assert "口头确认或 wait" in reminder
        system = runtime.messages({"SYSTEM_CHECK"}, prompt_kind="final_silence", instruction_candidates=candidates)
        assert "【本次自定义指令核对】" not in str(system)
        assert "静默" in runtime.inputs["turn"]["text"]
    finally:
        service.close()
        await runtime.end("test_complete")
        if runtime._tokenizer_prewarm_task is not None:
            await runtime._tokenizer_prewarm_task


@pytest.mark.asyncio
async def test_early_timer_expiry_keeps_original_budget_without_resubmitting(monkeypatch):
    import voice_terminal.instruction_matching as matching
    service = InstructionMatcher()
    calls = []
    monkeypatch.setattr(service, "prepare", lambda _: InstructionBank(None, {}, ()))
    monkeypatch.setattr(service, "run", lambda *_: calls.append(1) or ("quiet",))
    wait_for = asyncio.wait_for
    early = True
    async def early_once(future, timeout):
        nonlocal early
        if early:
            early = False
            raise asyncio.TimeoutError
        return await wait_for(future, timeout)
    try:
        await service.configure([entry()])
        monkeypatch.setattr(matching.asyncio, "wait_for", early_once)
        assert await service.match("turn", ("静默",)) == ("quiet",)
        assert len(calls) == 1
    finally:
        service.close()


@pytest.mark.asyncio
async def test_new_transcript_during_matching_cannot_install_old_reminder():
    from collections import OrderedDict
    from voice_terminal.agent.episode import AgentEpisodeRuntime
    started, release = asyncio.Event(), asyncio.Event()
    async def match(*_):
        started.set()
        await release.wait()
        return ("quiet",)
    runtime = SimpleNamespace(
        instruction_matcher=SimpleNamespace(match=match), closed=False,
        custom_instructions=[entry()], inputs={"turn": {"text": "静默"}},
        _instruction_candidates_by_turn=OrderedDict(),
    )
    task = asyncio.create_task(AgentEpisodeRuntime._request_instruction_candidates(
        runtime, "turn", {"USER_INPUT_FINALIZED"}))
    await started.wait()
    runtime.inputs["turn"] = {"text": "不用静默了"}
    release.set()
    assert await task == ""
    assert not runtime._instruction_candidates_by_turn


@pytest.mark.asyncio
async def test_cancelled_refresh_keeps_saved_semantics_and_finishes_index(monkeypatch):
    from voice_terminal.server.settings_runtime import ProductSettingsRuntimeMixin
    service = InstructionMatcher()
    started, release = Event(), Event()
    rules = [entry()]
    projected = []
    def prepare(_):
        started.set()
        release.wait(2)
        return InstructionBank(None, {}, ())
    monkeypatch.setattr(service, "prepare", prepare)
    async def publish(*_):
        pass
    runtime = SimpleNamespace(
        product_settings=SimpleNamespace(settings_only=lambda: {"custom_instructions": rules}),
        _session_lock=asyncio.Lock(), _instruction_snapshot=[], _shadow_user_profile="",
        shadow_context=SimpleNamespace(set_product_context=lambda _, entries: projected.append(entries)),
        controller=SimpleNamespace(episode=SimpleNamespace(refresh_custom_instructions=projected.append)),
        _persist_voice_identity=lambda: None, instruction_matcher=service,
        bus=SimpleNamespace(publish=publish), snapshot=lambda: {},
    )
    try:
        task = asyncio.create_task(ProductSettingsRuntimeMixin.refresh_custom_instruction_settings(runtime))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime._instruction_snapshot == rules and projected == [rules, rules]
        preparing = service._preparing
        release.set()
        await preparing
        assert service.configured_for(rules) and service._preparing is None
    finally:
        release.set()
        service.close()
