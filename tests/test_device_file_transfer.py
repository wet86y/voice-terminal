from __future__ import annotations

import base64
import hashlib

import pytest

from voice_terminal.devices.file_transfer import DeviceFileTransferError, DeviceFileTransferStore


def test_device_sftp_upload_is_generation_bound_and_exposes_verified_chunks(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "data")
    ticket = store.prepare_device_upload(device_id="device-a", connection_generation=2,
                                        executor_generation=3, expected_bytes=256 * 600,
                                        transfer_id="a" * 32)
    content = bytes(range(256)) * 600
    path = ticket["sftp_path"]
    with open(path, "xb") as handle:
        handle.write(content)
    digest = hashlib.sha256(content).hexdigest()

    with pytest.raises(DeviceFileTransferError):
        store.verify_device_upload(transfer_id=ticket["transfer_id"], device_id="device-b",
            connection_generation=2, executor_generation=3, expected_bytes=len(content), expected_sha256=digest)

    ready = store.verify_device_upload(transfer_id=ticket["transfer_id"], device_id="device-a",
        connection_generation=2, executor_generation=3, expected_bytes=len(content), expected_sha256=digest)
    assert ready["state"] == "ready" and ready["bytes"] == len(content)
    chunk = store.read_chunk(transfer_id=ticket["transfer_id"], device_id="device-a",
        connection_generation=2, executor_generation=3, offset=0, limit=4096)
    raw = base64.b64decode(chunk["data_base64"], validate=True)
    assert raw == content[:4096]
    assert chunk["chunk_sha256"] == hashlib.sha256(raw).hexdigest()
    assert chunk["sha256"] == digest
    with pytest.raises(DeviceFileTransferError):
        store.read_chunk(transfer_id=ticket["transfer_id"], device_id="device-a",
            connection_generation=1, executor_generation=3, offset=0)


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
    first_out = store.read_chunk(**request, offset=0, limit=8192)
    assert base64.b64decode(first_out["data_base64"], validate=True) == content[:8192]
    assert store.close(**request) is True
    with pytest.raises(DeviceFileTransferError):
        store.read_chunk(**request, offset=0)


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
    chunk = store.read_chunk(transfer_id=ticket["transfer_id"], device_id="local",
        connection_generation=1, executor_generation=1, offset=0, limit=8192)
    raw = base64.b64decode(chunk["data_base64"], validate=True)
    assert raw == content[:8192]
    assert chunk["chunk_sha256"] == hashlib.sha256(raw).hexdigest()


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
