"""图片自然上下文回归：文件复用、后台加载及正常整理。"""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.test_screen_capture import packet
from voice_terminal.handoff import ContinuityOrganizer, PairedArchiveStore
from voice_terminal.providers.deepseek import DeepSeekClient, DeepSeekImageReferenceError, DeepSeekHttpError
from voice_terminal.session.ledger import SessionLedger


def client():
    return DeepSeekClient(base_url="https://api.deepseek.com", model="deepseek-flash", api_key="fixture", transport="responses")


def material():
    ledger = SessionLedger()
    stored = ledger.store_screen_capture(packet(), "c", "t")
    ledger.commit_screen_capture(stored)
    ledger.screen_capture.close()
    return ledger, stored


def test_file_upload_deduplicates_parallel_history_and_survives_restart():
    async def scenario():
        ledger, stored = material()
        requests = []
        async def reply(request):
            requests.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"id": f"file-api-{len(requests)}", "expires_at": time.time() + 600})
        provider = client(); provider._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        messages = ledger.context_messages("")
        first, duplicate = await asyncio.gather(provider.prepare_messages(messages), provider.prepare_messages(messages))
        assert first == duplicate and len(requests) == 2
        assert "data" not in first[0]["content"][1] and "data" in messages[0]["content"][1]
        assert len([p for p in provider._responses_payload(first, model=provider.model, schema={}, schema_name="test", reasoning_effort="off", max_output_tokens=64)["input"][0]["content"] if "file_id" in p]) == 2
        checkpoint = provider.image_transport_snapshot()
        assert stored["attachments"][0]["base64"] not in json.dumps(checkpoint)
        restored = client(); restored.restore_image_transport(checkpoint)
        restored._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        assert await restored.prepare_messages(messages) == first and len(requests) == 2
        wrong_scope = client(); wrong_scope.api_key = "another"
        wrong_scope.restore_image_transport(checkpoint)
        assert wrong_scope.image_transport_snapshot()["sources"] == {}
        await restored.close(); await provider.close()
    asyncio.run(scenario())


def test_file_failure_chooses_stable_inline_without_repeated_upload(caplog):
    async def scenario():
        ledger, stored = material()
        requests = []
        def reply(request):
            requests.append(request)
            return httpx.Response(503, text="不可用")
        provider = client(); provider._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        first = await provider.prepare_messages(ledger.context_messages(""))
        assert await provider.prepare_messages(ledger.context_messages("")) == first
        assert len(requests) == 2 and "data" in first[0]["content"][1]
        assert stored["attachments"][0]["base64"] not in caplog.text
        await provider.close()
    asyncio.run(scenario())


def test_expired_file_is_reuploaded_and_new_reference_is_stable():
    async def scenario():
        ledger, _ = material()
        provider = client()
        requests = []
        def reply(request):
            requests.append(request)
            return httpx.Response(200, json={"id": f"file-api-{len(requests)}", "expires_at": None})
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        before = await provider.prepare_messages(ledger.context_messages(""))
        for source in provider._image_sources.values(): source["expires_at"] = 0
        after = await provider.prepare_messages(ledger.context_messages(""))
        assert before != after and len(requests) == 4
        assert await provider.prepare_messages(ledger.context_messages("")) == after
        assert len(requests) == 4
        await provider.close()
    asyncio.run(scenario())


def test_server_deleted_file_rebuilds_only_invalid_reference_and_preserves_history():
    async def scenario():
        ledger, _ = material()
        requests = []
        def reply(request):
            if request.url.path == '/files':
                requests.append(request)
                return httpx.Response(200, json={"id": f"file-api-{len(requests)}", "expires_at": time.time() + 600})
            return httpx.Response(400, json={"error": {"message": "file-api-1 does not exist"}})
        provider = client(); provider._client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        history = ledger.context_messages("")
        first = await provider.prepare_messages(history)
        with pytest.raises(DeepSeekImageReferenceError) as error:
            async for item in provider.stream_events(first, schema={}): pass
        assert error.value.retryable
        restored = await provider.prepare_messages(history)
        assert restored[0]["content"][1]["file_id"] != first[0]["content"][1]["file_id"]
        assert restored[0]["content"][2] == first[0]["content"][2]
        assert len(requests) == 3 and ledger.context_messages("") == history
        await provider.close()
    asyncio.run(scenario())


def test_provider_error_preview_redacts_echoed_image_encoding():
    _, stored = material()
    encoded = stored["attachments"][0]["base64"]
    error = DeepSeekHttpError(400, json.dumps({"error": {"request": {"type": "image", "data": encoded}}}))
    assert encoded not in str(error) and "图片编码" in str(error)


@pytest.mark.parametrize("segmented", [False, True])
def test_continuity_understands_native_images_once_and_keeps_encoding_out_of_text(tmp_path, segmented):
    async def scenario():
        ledger, stored = material()
        observation = next(e for e in ledger.entries if e.kind == "observation")
        store = PairedArchiveStore(tmp_path, streaming=False)
        archive = store.save(voice_session_id="c", dsh_session_id="", cwd=".", voice_records=[
            {"kind": "observation", "content": observation.content},
            {"kind": "screen_capture", **stored},
            *([{ "kind": "input", "content": "重复的文字材料" * 1500 }] if segmented else []),
        ], dsh_records=[], captures=[])
        requests = []
        async def complete(**kwargs):
            requests.append(kwargs)
            value = {"document": "已观察到紫色画面，保留观察时点。", "extracted_items": [], "consumed_entry_ids": []}
            return SimpleNamespace(output=[{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}])
        provider = SimpleNamespace(complete=complete, supports_images=True)
        result = await ContinuityOrganizer(provider, max_context_tokens=16384 if segmented else 524288, max_output_tokens=128).organize(archive, "", [])
        image_parts = [p for request in requests for item in request["input_items"] if isinstance(item["content"], list)
                       for p in item["content"] if p["type"] == "input_image"]
        assert len(image_parts) == 2
        assert all(p["image_url"].startswith("data:image/") for p in image_parts)
        texts = [p["text"] for request in requests for item in request["input_items"] if isinstance(item["content"], list)
                 for p in item["content"] if p["type"] == "input_text"]
        assert stored["attachments"][0]["base64"] not in json.dumps(texts)
        assert stored["attachments"][0]["base64"] not in result.document
        assert len(requests) > 1 if segmented else len(requests) == 1
    asyncio.run(scenario())


def test_text_only_continuity_model_cannot_silently_drop_pictures(tmp_path):
    async def scenario():
        _, stored = material()
        archive = PairedArchiveStore(tmp_path, streaming=False).save(voice_session_id="c", dsh_session_id="", cwd=".",
            voice_records=[{"kind": "screen_capture", **stored}], dsh_records=[], captures=[])
        with pytest.raises(RuntimeError, match="不支持图片"):
            await ContinuityOrganizer(SimpleNamespace(supports_images=False)).organize(archive, "", [])
        assert (archive.directory / "source.json").is_file()
    asyncio.run(scenario())
