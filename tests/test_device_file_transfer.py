from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest

from voice_terminal.devices.file_transfer import (
    MAX_CHUNK_BYTES, DeviceFileTransferError, DeviceFileTransferStore,
)


def test_device_file_upload_is_generation_bound_and_exposes_verified_stream(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    ticket = store.prepare_device_upload(device_id="device-a", connection_generation=2,
                                        executor_generation=3, expected_bytes=256 * 600,
                                        transfer_id="a" * 32)
    content = bytes(range(256)) * 600
    digest = hashlib.sha256(content).hexdigest()
    assert "sftp_path" not in ticket
    channel = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id="device-a",
        connection_generation=2, executor_generation=3, operation="file.sftp.upload",
        maximum_bytes=len(content))
    channel_key = {"transfer_id":ticket["transfer_id"],"device_id":"device-a",
        "connection_generation":2,"executor_generation":3,"channel_token":channel["channel_token"]}
    for offset in range(0, len(content), MAX_CHUNK_BYTES):
        part = content[offset:offset + MAX_CHUNK_BYTES]
        store.append_device_channel_chunk(**channel_key, offset=offset,
            data_base64=base64.b64encode(part).decode("ascii"),
            chunk_sha256=hashlib.sha256(part).hexdigest())
    store.finish_device_channel_upload(**channel_key, expected_bytes=len(content), expected_sha256=digest)

    with pytest.raises(DeviceFileTransferError):
        store.verify_device_upload(transfer_id=ticket["transfer_id"], device_id="device-b",
            connection_generation=2, executor_generation=3, expected_bytes=len(content), expected_sha256=digest)

    ready = store.verify_device_upload(transfer_id=ticket["transfer_id"], device_id="device-a",
        connection_generation=2, executor_generation=3, expected_bytes=len(content), expected_sha256=digest)
    assert ready["state"] == "ready" and ready["bytes"] == len(content)
    request = {key: value for key, value in channel_key.items() if key != "channel_token"}
    with pytest.raises(DeviceFileTransferError):
        store.open_staged_read(**{**request, "connection_generation": 1})
    handle, size = store.open_staged_read(**request)
    try:
        with pytest.raises(DeviceFileTransferError):
            store.open_staged_read(**request)
        with pytest.raises(DeviceFileTransferError):
            store.close(**request)
        assert size == len(content) and handle.read() == content
    finally:
        store.finish_staged_read(handle, **request)
    assert handle.closed and not list(store.root.iterdir())


def test_core_attachment_chunks_are_sequential_idempotent_and_digest_checked(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    ticket = store.prepare_core_upload(device_id="device-a", connection_generation=4,
        executor_generation=5, expected_bytes=len(b"approved attachment data" * 1000),
        expected_sha256=hashlib.sha256(b"approved attachment data" * 1000).hexdigest(),
        target_path=str(tmp_path / "output.bin"), transfer_id="b" * 32)
    content = b"approved attachment data" * 1000
    first, rest = content[:8192], content[8192:]
    request = dict(transfer_id=ticket["transfer_id"], device_id="device-a",
        connection_generation=4, executor_generation=5)
    first_payload = base64.b64encode(first).decode("ascii")
    first_receipt = store.append_core_chunk(**request, offset=0, data_base64=first_payload)
    assert first_receipt["next_offset"] == len(first)
    assert store.append_core_chunk(**request, offset=0, data_base64=first_payload) == first_receipt
    with pytest.raises(DeviceFileTransferError):
        store.append_core_chunk(**request, offset=1, data_base64=first_payload)
    store.append_core_chunk(**request, offset=len(first), data_base64=base64.b64encode(rest).decode("ascii"))
    digest = hashlib.sha256(content).hexdigest()
    ready = store.finish_core_upload(**request, expected_bytes=len(content), expected_sha256=digest)
    assert ready["state"] == "ready" and ready["sha256"] == digest
    with pytest.raises(DeviceFileTransferError):
        store.open_staged_read(**request)
    assert store.device_file_download_ticket(**request)["sha256"] == digest
    assert store.close(**request) is True


def test_local_large_file_is_stream_staged_with_source_identity_and_chunk_hash(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    source = tmp_path / "source.bin"
    content = bytes(range(256)) * 700
    source.write_bytes(content)
    ticket = store.prepare_local_upload(device_id="local", connection_generation=1,
        executor_generation=1, expected_bytes=len(content), transfer_id="c" * 32)
    staged = store.stage_local_file(transfer_id=ticket["transfer_id"], device_id="local",
        connection_generation=1, executor_generation=1, source_path=source)
    assert staged["state"] == "ready" and staged["bytes"] == len(content)
    request = dict(transfer_id=ticket["transfer_id"], device_id="local",
                   connection_generation=1, executor_generation=1)
    handle, size = store.open_staged_read(**request)
    try:
        assert size == len(content) and handle.read() == content
    finally:
        store.finish_staged_read(handle, **request)
    assert source.read_bytes() == content


def test_cancelled_binary_response_releases_staged_handle_and_ticket(tmp_path):
    from voice_terminal.server.dsh_attachment_transport import FILE_READ_SIZE, StagedAttachmentResponse

    async def scenario():
        store = DeviceFileTransferStore(tmp_path / "data")
        source = tmp_path / "source.bin"
        data = b"\x89PNG\x00\xff" * FILE_READ_SIZE
        source.write_bytes(data)
        key = dict(transfer_id="d" * 32, device_id="local", connection_generation=1, executor_generation=1)
        store.prepare_local_upload(**key, expected_bytes=len(data))
        store.stage_local_file(**key, source_path=source)
        handle, size = store.open_staged_read(**key)
        response = StagedAttachmentResponse(handle, size, is_current=lambda: True,
                                            release=lambda: store.finish_staged_read(handle, **key))
        first_block = asyncio.Event()

        async def receive():
            await first_block.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message.get("body"):
                assert message["body"] == data[:FILE_READ_SIZE]
                first_block.set()
                await asyncio.Future()  # 模拟发送首块时客户端断开。

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 2)
        assert handle.closed and not list(store.root.iterdir())
        with pytest.raises(DeviceFileTransferError):
            store.open_staged_read(**key)

    asyncio.run(scenario())


def test_local_attachment_commit_is_atomic_non_overwriting_and_idempotent(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    content = b"local attachment" * 1000
    digest = hashlib.sha256(content).hexdigest()
    target = tmp_path / "approved-output.bin"
    ticket = store.prepare_core_upload(device_id="local", connection_generation=2,
        executor_generation=3, expected_bytes=len(content), expected_sha256=digest,
        target_path=str(target), transfer_id="d" * 32)
    key = dict(transfer_id=ticket["transfer_id"], device_id="local", connection_generation=2,
        executor_generation=3)
    store.append_core_chunk(**key, offset=0, data_base64=base64.b64encode(content).decode("ascii"))
    store.finish_core_upload(**key, expected_bytes=len(content), expected_sha256=digest)
    result = store.commit_local_file(**key, target_path=target)
    assert target.read_bytes() == content and result["sha256"] == digest
    duplicate = store.commit_local_file(**key, target_path=target)
    assert duplicate["duplicate"] is True

    other = store.prepare_core_upload(device_id="local", connection_generation=2,
        executor_generation=3, expected_bytes=1, expected_sha256=hashlib.sha256(b"x").hexdigest(),
        target_path=str(target), transfer_id="e" * 32)
    other_key = dict(transfer_id=other["transfer_id"], device_id="local", connection_generation=2,
        executor_generation=3)
    store.append_core_chunk(**other_key, offset=0, data_base64=base64.b64encode(b"x").decode("ascii"))
    store.finish_core_upload(**other_key, expected_bytes=1, expected_sha256=hashlib.sha256(b"x").hexdigest())
    with pytest.raises(DeviceFileTransferError):
        store.commit_local_file(**other_key, target_path=target)


def test_device_file_channel_upload_reuses_ticket_offset_and_digest_authority(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    content = bytes(range(256)) * 700
    digest = hashlib.sha256(content).hexdigest()
    ticket = store.prepare_device_upload(device_id="client-a", connection_generation=9,
        executor_generation=3, expected_bytes=len(content), transfer_id="f" * 32)
    begin = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id="client-a",
        connection_generation=9, executor_generation=3, operation="file.sftp.upload",
        maximum_bytes=len(content))
    assert begin["total_bytes"] == len(content)
    assert begin["next_offset"] == 0 and begin["ready"] is False
    assert "path" not in begin and "sftp_path" not in begin

    key = {"transfer_id": ticket["transfer_id"], "device_id": "client-a",
        "connection_generation": 9, "executor_generation": 3,
        "channel_token": begin["channel_token"]}
    first = content[:MAX_CHUNK_BYTES]
    first_payload = base64.b64encode(first).decode("ascii")
    first_sha = hashlib.sha256(first).hexdigest()
    ack = store.append_device_channel_chunk(**key, offset=0, data_base64=first_payload,
        chunk_sha256=first_sha)
    assert ack["next_offset"] == len(first)
    assert store.append_device_channel_chunk(**key, offset=0, data_base64=first_payload,
        chunk_sha256=first_sha) == ack
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**key, offset=1, data_base64=first_payload,
            chunk_sha256=first_sha)
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**key, offset=len(first), data_base64=first_payload,
            chunk_sha256="0" * 64)
    oversized = b"x" * (MAX_CHUNK_BYTES + 1)
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**key, offset=len(first),
            data_base64=base64.b64encode(oversized).decode("ascii"),
            chunk_sha256=hashlib.sha256(oversized).hexdigest())
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**{**key, "executor_generation": 4}, offset=0,
            data_base64=first_payload, chunk_sha256=first_sha)
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**{**key, "channel_token": "x" * 43}, offset=0,
            data_base64=first_payload, chunk_sha256=first_sha)

    for offset in range(len(first), len(content), MAX_CHUNK_BYTES):
        part = content[offset:offset + MAX_CHUNK_BYTES]
        store.append_device_channel_chunk(**key, offset=offset,
            data_base64=base64.b64encode(part).decode("ascii"),
            chunk_sha256=hashlib.sha256(part).hexdigest())
    with pytest.raises(DeviceFileTransferError):
        store.finish_device_channel_upload(**key, expected_bytes=len(content), expected_sha256="0" * 64)
    ready = store.finish_device_channel_upload(**key, expected_bytes=len(content), expected_sha256=digest)
    assert ready["state"] == "ready" and ready["bytes"] == len(content) and ready["sha256"] == digest
    assert "target_path" not in ready and "path" not in ready
    duplicate = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id="client-a",
        connection_generation=9, executor_generation=3, operation="file.sftp.upload",
        maximum_bytes=len(content))
    assert duplicate["ready"] is True and duplicate["sha256"] == digest


def test_device_file_channel_download_reads_only_approved_ticket_and_is_pathless(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    content = b"approved attachment" * 5000
    digest = hashlib.sha256(content).hexdigest()
    ticket = store.prepare_core_upload(device_id="client-b", connection_generation=4,
        executor_generation=8, expected_bytes=len(content), expected_sha256=digest,
        target_path=r"D:\approved\device-target.bin", transfer_id="1" * 32)
    key = {"transfer_id": ticket["transfer_id"], "device_id": "client-b",
        "connection_generation": 4, "executor_generation": 8}
    for offset in range(0, len(content), MAX_CHUNK_BYTES):
        part = content[offset:offset + MAX_CHUNK_BYTES]
        store.append_core_chunk(**key, offset=offset,
            data_base64=base64.b64encode(part).decode("ascii"))
    store.finish_core_upload(**key, expected_bytes=len(content), expected_sha256=digest)
    begin = store.open_device_file_channel(**key, operation="file.sftp.download",
        maximum_bytes=len(content), expected_sha256=digest)
    assert begin["total_bytes"] == len(content) and begin["sha256"] == digest
    assert "path" not in begin and "sftp_path" not in begin and "target_path" not in begin
    channel = {**key, "channel_token": begin["channel_token"]}
    with pytest.raises(DeviceFileTransferError):
        store.read_device_channel_chunk(**channel, offset=1, limit=MAX_CHUNK_BYTES)
    downloaded = bytearray()
    for offset in range(0, len(content), MAX_CHUNK_BYTES):
        chunk = store.read_device_channel_chunk(**channel, offset=offset, limit=MAX_CHUNK_BYTES)
        raw = base64.b64decode(chunk["data_base64"], validate=True)
        assert raw == content[offset:offset + MAX_CHUNK_BYTES]
        assert chunk["offset"] == offset
        assert chunk["chunk_sha256"] == hashlib.sha256(raw).hexdigest()
        assert chunk["sha256"] == digest
        downloaded.extend(raw)
    assert bytes(downloaded) == content
    with pytest.raises(DeviceFileTransferError):
        store.read_device_channel_chunk(**{**channel, "channel_token": "z" * 43}, offset=0)
    with pytest.raises(DeviceFileTransferError):
        store.open_device_file_channel(**key, operation="file.sftp.upload",
            maximum_bytes=len(content))
    finished = store.finish_device_channel_download(**channel,
        expected_bytes=len(content), expected_sha256=digest)
    assert finished["state"] == "ready" and finished["sha256"] == digest
    assert "target_path" not in finished and "path" not in finished


def test_device_file_channel_cancel_discards_partial_upload_and_invalidates_token(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    ticket = store.prepare_device_upload(device_id="client-c", connection_generation=2,
        executor_generation=5, expected_bytes=6, transfer_id="2" * 32)
    begin = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id="client-c",
        connection_generation=2, executor_generation=5, operation="file.sftp.upload", maximum_bytes=6)
    key = {"transfer_id": ticket["transfer_id"], "device_id": "client-c",
        "connection_generation": 2, "executor_generation": 5,
        "channel_token": begin["channel_token"]}
    data = base64.b64encode(b"abc").decode("ascii")
    store.append_device_channel_chunk(**key, offset=0, data_base64=data,
        chunk_sha256=hashlib.sha256(b"abc").hexdigest())
    assert store.cancel_device_file_channel(**key) is True
    with pytest.raises(DeviceFileTransferError):
        store.append_device_channel_chunk(**key, offset=3, data_base64=data,
            chunk_sha256=hashlib.sha256(b"abc").hexdigest())
    restarted = store.open_device_file_channel(transfer_id=ticket["transfer_id"], device_id="client-c",
        connection_generation=2, executor_generation=5, operation="file.sftp.upload", maximum_bytes=6)
    assert restarted["next_offset"] == 0
    assert restarted["channel_token"] != begin["channel_token"]
