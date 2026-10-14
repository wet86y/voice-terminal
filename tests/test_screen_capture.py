"""屏幕输入回归：验证坐标、同帧裁剪、恢复归档和请求附件生命周期。"""
import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from voice_terminal.screen_capture import ScreenCaptureConfig, ScreenCaptureService, choose_center, encode_pair, capture_message
from voice_terminal.session.ledger import SessionLedger
from voice_terminal.session.context_frames import ContextFrame, ContextLane
from voice_terminal.session.context import consolidate_request_tail
from voice_terminal.providers.deepseek import DeepSeekClient
from voice_terminal.agent.context_audit import build_context_snapshot
from voice_terminal.agent.screen_input import ScreenInputMixin
from voice_terminal.handoff import PairedArchiveStore


def state(**values):
    return {"monitor_bounds": (-1600, 0, 0, 900), "client_bounds": (-1500, 100, -100, 800),
            "cursor": (-1400, 200), "cursor_visible": True, "executable": "GAME.EXE", "notification_state": 0, **values}


def packet():
    return encode_pair(Image.new("RGB", (1600, 900), (120, 40, 210)), state(), ScreenCaptureConfig())


@pytest.mark.parametrize("values,source,center", [
    ({}, "cursor", (-1400, 200)),
    ({"cursor": (200, 200)}, "window_center", (-800, 450)),
    ({"cursor_visible": False}, "window_center", (-800, 450)),
    ({"client_bounds": (-1600, 0, 0, 900), "cursor_visible": False}, "screen_center", (-800, 450)),
    ({"client_bounds": (-1600, 0, 0, 900)}, "cursor", (-1400, 200)),
    ({"cursor": None}, "window_center", (-800, 450)),
])
def test_system_selected_screen_center_rules(values, source, center):
    selected, facts = choose_center(state(**values), ScreenCaptureConfig())
    assert selected == center and facts["center_source"] == source


def test_case_insensitive_override_and_unconfirmed_exclusive():
    cfg = ScreenCaptureConfig(app_overrides=({"executable": "game.exe", "mode": "center"},))
    center, facts = choose_center(state(), cfg)
    assert center == (-800, 450) and facts["exclusive"] is None
    assert choose_center(state(client_bounds=(-1600, 0, 0, 900), notification_state=3), cfg)[1]["exclusive"] is True


@pytest.mark.parametrize("values", [{"enabled": 1}, {"crop_width": 255}, {"crop_height": 2049}, {"global_max_edge": 900},
    {"app_overrides": [{"executable": "a.exe", "mode": "invalid"}]},
    {"app_overrides": [{"executable": "a.exe", "mode": "auto"}, {"executable": "A.EXE", "mode": "center"}]}])
def test_settings_reject_invalid_combinations(values):
    with pytest.raises(ValueError):
        ScreenCaptureConfig(**values)


def test_same_frame_crop_padding_and_no_global_upscale():
    result = packet()
    full, local = [Image.open(io.BytesIO(base64.b64decode(a["base64"]))) for a in result["attachments"]]
    assert full.size == (1600, 900) and local.size == (960, 640)
    assert local.getpixel((0, 0)) == (0, 0, 0)
    assert local.getpixel((480, 320)) == (120, 40, 210)
    assert result["crop_rect"] == (-1880, -120, -920, 520)
    assert result["valid_region"] == (-1600, 0, -920, 520)


@pytest.mark.parametrize("stable_retry", [True, False])
def test_layout_change_reenumerates_outputs_and_never_returns_stale_frame(monkeypatch, stable_retry):
    import sys
    import numpy as np
    import voice_terminal.screen_capture as capture
    class DpiCall:
        def __call__(self, value): return 0
    monkeypatch.setattr(capture.ctypes, "WinDLL", lambda _: SimpleNamespace(SetThreadDpiAwarenessContext=DpiCall()), raising=False)
    baseline = {**state(), "hwnd": 10, "monitor_handle": 20, "device": "display",
                "monitor_layout": ((20, -1600, 0, 0, 900),)}
    changed = {**baseline, "monitor_layout": ((20, -1600, 0, 0, 900), (30, 0, 0, 1920, 1080))}
    final = changed if stable_retry else baseline
    states = iter((baseline, changed, changed, final))
    monkeypatch.setattr(capture, "windows_state", lambda: next(states))
    class Factory:
        def __init__(self):
            self.refreshes = getattr(self, "refreshes", 0) + 1
            self.outputs = [[SimpleNamespace(desc=SimpleNamespace(Monitor=20))]]
    factory = Factory(); factory.refreshes = 0
    releases = []
    def create(**kwargs):
        assert kwargs["processor_backend"] == "numpy" and kwargs["output_color"] == "BGRA"
        return SimpleNamespace(grab=lambda **_: np.zeros((900, 1600, 4), dtype=np.uint8), release=lambda: releases.append(True))
    monkeypatch.setitem(sys.modules, "dxcam", SimpleNamespace(__factory=factory, create=create))
    if stable_retry:
        assert len(capture.capture_windows(ScreenCaptureConfig())["attachments"]) == 2
    else:
        with pytest.raises(RuntimeError, match="发生变化"):
            capture.capture_windows(ScreenCaptureConfig())
    assert factory.refreshes == 2 and len(releases) == 2


def test_real_provider_payload_and_audit_never_stringify_image_blocks():
    p = packet()
    frames = [ContextFrame.make(ContextLane.STATIC_INSTRUCTIONS, "system", "固定指令"), capture_message(p)]
    frames = consolidate_request_tail(frames)
    wire = DeepSeekClient._responses_payload(frames, model="deepseek-flash", schema={}, schema_name="test", reasoning_effort="off", max_output_tokens=100)
    images = [m for m in wire["input"] if isinstance(m["content"], list) and len(m["content"]) == 3]
    assert len(images) == 1 and images[0]["role"] == "user"
    assert images[0]["content"][1]["type"] == "input_image"
    client = DeepSeekClient(base_url="https://api.deepseek.com", model="deepseek-flash", api_key="fixture", transport="responses")
    audit = build_context_snapshot(client, frames, {}, conversation_id="c", request_id="r", turn_id="t", events=[], mode="normal",
                                  reasoning_effort="off", context_compaction={}, context_token_estimate=2048)
    assert p["attachments"][0]["base64"] not in json.dumps(audit)
    assert "图片编码" in json.dumps(audit, ensure_ascii=False)
    assert DeepSeekClient._chat_content(capture_message(p)["content"])[1]["type"] == "image_url"


def test_restart_preserves_single_encoded_record_and_descriptive_snapshot(tmp_path):
    ledger = SessionLedger(); ledger.attach_storage(tmp_path, resume=True)
    p = ledger.store_screen_capture(packet(), "conversation", "turn")
    assert ledger.commit_screen_capture(p)
    assert not ledger.commit_screen_capture(p)
    aid = p["attachments"][0]["attachment_id"]
    ledger.update_screen_status(p, "已发送")
    assert len(ledger.conversation.entries) == 1
    from datetime import datetime
    assert datetime.fromisoformat(ledger.conversation.entries[0]["at"]).timestamp() == pytest.approx(p["captured_at"], abs=1e-6, rel=0)
    assert "base64" not in json.dumps(ledger.conversation.snapshot())
    assert ledger.journal.flush() and ledger.close_storage()
    restored = SessionLedger(); restored.attach_storage(tmp_path, resume=True)
    try:
        assert len(restored.image_records) == 1
        assert restored.conversation.snapshot()["entries"][0]["attachments"][0]["conversation_id"] == "conversation"
        assert restored.screen_attachment("conversation", aid)["base64"] == p["attachments"][0]["base64"]
        assert restored.screen_attachment("other", aid) is None
        images = restored.context_messages("")
        assert len(images) == 1 and images[0]["context_lane"] == "history"
        assert len([part for part in images[0]["content"] if part["type"] == "image"]) == 2
        assert restored.shadow_context_snapshot().frames[0].kind == "observation"
        assert (tmp_path / "screen-images" / aid).is_file()
    finally:
        restored.close_storage()


def test_archive_contains_encoding_once_but_readable_and_organizer_material_do_not(tmp_path):
    p = packet()
    store = PairedArchiveStore(tmp_path, streaming=False)
    archive = store.save(voice_session_id="c", dsh_session_id="", cwd=".", voice_records=[{"kind": "screen_capture", **p}], dsh_records=[], captures=[])
    encoded = p["attachments"][0]["base64"]
    assert (archive.directory / "source.json").read_text(encoding="utf-8").count(encoded) == 1
    assert encoded not in (archive.directory / "voice.md").read_text(encoding="utf-8")
    assert encoded not in json.dumps(store.organizer_records(archive))


def test_request_claim_preserves_original_images_when_new_capture_arrives():
    ledger = SessionLedger()
    class Runtime(ScreenInputMixin):
        pass
    r = Runtime(); r.ledger = ledger; r.input_scope_closed = False; r.pending = {"SCREEN_CAPTURE_READY"}
    r._screen_requests = {}; r._screen_context_packet = None
    first = ledger.store_screen_capture(packet(), "c", "t")
    r._screen_pending = first
    r._prepare_screen_context("t", {"SCREEN_CAPTURE_READY"}, False); r._claim_screen_context("t")
    second = ledger.store_screen_capture(packet(), "c", "t")
    r._screen_pending = second
    r._prepare_screen_context("t", {"LLM_RETRY"}, False)
    assert r._screen_context_packet is first and r._screen_pending is second
    r._screen_accepted()
    assert ledger.conversation.entries[0]["status"] == "已发送"
    r._settle_screen_context("t", "completed")
    assert not r._screen_requests and r._screen_pending is second
    r.invalidate_screen_capture()
    assert r._screen_pending is None and ledger.conversation.entries[-1]["status"] == "未发送"


def test_native_worker_is_serial_and_config_is_frozen():
    import threading
    release = threading.Event(); entered = threading.Event()
    observed = []
    def fake(config):
        entered.set(); release.wait(2); observed.append(config.crop_width); return {}
    service = ScreenCaptureService(capture=fake)
    async def scenario():
        task = asyncio.create_task(service.grab())
        await asyncio.to_thread(entered.wait, 1)
        service.configure(ScreenCaptureConfig(crop_width=1200))
        with pytest.raises(RuntimeError, match="尚未结束"):
            await service.grab()
        release.set(); await task
    try:
        asyncio.run(scenario())
        assert observed == [960]
    finally:
        release.set(); service.close()


def test_disable_during_inflight_request_removes_images_from_subsequent_retry():
    class Runtime(ScreenInputMixin):
        pass
    r = Runtime(); r.ledger = SessionLedger(); r.pending = set(); r.input_scope_closed = False
    r._screen_pending = r.ledger.store_screen_capture(packet(), "c", "t")
    r._screen_requests = {}; r._screen_context_packet = None
    r._prepare_screen_context("t", {"SCREEN_CAPTURE_READY"}, False); r._claim_screen_context("t")
    frozen = r._screen_context_packet
    r.active_request = object()
    r.ledger.screen_capture.configure(ScreenCaptureConfig(enabled=False)); r.invalidate_screen_capture()
    assert r._screen_context_packet is frozen and not r._screen_requests
    r._prepare_screen_context("t", {"LLM_RETRY"}, False)
    assert r._screen_context_packet is None
    assert len(r.ledger.context_messages("")) == 1  # 已冻结观察仍留在稳定历史。
    r.ledger.screen_capture.close()


def test_multimodal_history_interleaves_text_and_images_and_serializes_both_protocols():
    ledger = SessionLedger()
    ledger.commit_input("first", "查看图片")
    first = ledger.store_screen_capture(packet(), "c", "first")
    ledger.commit_screen_capture(first)
    ledger.commit_input("second", "继续依据刚才的图片解释")
    second = ledger.store_screen_capture(packet(), "c", "second")
    ledger.commit_screen_capture(second)
    messages = ledger.context_messages("")
    assert [m["context_kind"] if "context_kind" in m else "text" for m in messages] == ["text", "observation", "text", "observation"]
    wire = DeepSeekClient._responses_payload(messages, model="deepseek-flash", schema={}, schema_name="test", reasoning_effort="off", max_output_tokens=100)
    assert sum(p["type"] == "input_image" for m in wire["input"] for p in m["content"]) == 4
    assert DeepSeekClient._chat_content(messages[1]["content"])[1]["type"] == "image_url"
    from voice_terminal.screen_capture import image_summary
    assert first["attachments"][0]["base64"] not in json.dumps(image_summary(messages))
    ledger.screen_capture.close()


def test_provider_acceptance_survives_response_failure_without_mutating_image_history():
    ledger = SessionLedger()
    class Runtime(ScreenInputMixin): pass
    r = Runtime(); r.ledger = ledger; r.input_scope_closed = False; r.pending = set()
    r._screen_requests = {}; r._screen_context_packet = None
    r._screen_pending = ledger.store_screen_capture(packet(), "c", "t")
    r._prepare_screen_context("t", set(), False); r._claim_screen_context("t")
    before = ledger.context_messages("")
    r._screen_accepted(); r._settle_screen_context("t", "failed")
    assert ledger.conversation.entries[0]["status"] == "已发送"  # 响应失败不能倒改图片受理事实。
    assert ledger.context_messages("") == before
    assert len(ledger.shadow_context_snapshot().frames) == 1
    ledger.screen_capture.close()


def test_captured_images_dispatch_on_next_heartbeat_and_remain_in_stable_history():
    from tests.test_scheduler_staircase import _BlockingProvider, _speaking_runtime, _finish_blocking_request
    class Provider(_BlockingProvider):
        def __init__(self):
            super().__init__(); self.messages = []
        def stream_events(self, messages, **kwargs):
            self.messages.append(messages)
            return super().stream_events(messages, **kwargs)
    async def scenario():
        provider = Provider(); r = _speaking_runtime(provider)
        service = ScreenCaptureService(capture=lambda config: packet())
        r.ledger.screen_capture = service
        r.scheduler.start_heartbeat(0)
        r.ledger.commit_input("turn-a", "看看当前画面")
        await r._capture_screen_action(r.identity(), "turn-a")
        assert "SCREEN_CAPTURE_READY" in r.pending and provider.calls == 0
        await r._on_heartbeat(.4)
        for _ in range(30):
            if provider.calls: break
            await asyncio.sleep(0)
        assert provider.calls == 1
        first = [f for f in provider.messages[0] if f.get("context_kind") == "observation"]
        assert len(first) == 1 and first[0]["context_lane"] == "history"
        assert r.max_request_concurrency == 1
        await _finish_blocking_request(r, provider)
        assert r._screen_pending is None and not r._screen_requests
        later = [f for f in r.messages({"USER_SEGMENT_UPDATED"}) if f.get("context_kind") == "observation"]
        assert later == first
        assert len([e for e in r.ledger.entries if e.kind == "observation"]) == 1
        await r.end("test"); service.close()
    asyncio.run(scenario())
