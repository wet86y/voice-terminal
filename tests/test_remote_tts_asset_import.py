from __future__ import annotations

import base64
import hashlib
import io

import numpy as np
import pytest
import soundfile as sf

from voice_terminal.devices.file_transfer import (
    MAX_CHUNK_BYTES,
    DeviceFileTransferError,
    DeviceFileTransferStore,
)
from voice_terminal.tts.voices import MAX_IMPORT_BYTES, VoiceLibrary, VoiceLibraryError


def _reference_wav() -> bytes:
    sample_rate = 16_000
    time = np.arange(sample_rate * 5, dtype=np.float32) / sample_rate
    signal = (0.15 * np.sin(2 * np.pi * 220 * time)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, signal, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _upload_tts_ticket(store: DeviceFileTransferStore, ticket: dict, device_id: str,
                       connection_generation: int, executor_generation: int, data: bytes,
                       sha256: str) -> None:
    channel = store.open_device_file_channel(
        transfer_id=ticket["transfer_id"], device_id=device_id,
        connection_generation=connection_generation, executor_generation=executor_generation,
        operation="file.sftp.upload", maximum_bytes=len(data),
    )
    channel_key = {
        "transfer_id": ticket["transfer_id"], "device_id": device_id,
        "connection_generation": connection_generation, "executor_generation": executor_generation,
        "channel_token": channel["channel_token"],
    }
    for offset in range(0, len(data), MAX_CHUNK_BYTES):
        part = data[offset:offset + MAX_CHUNK_BYTES]
        store.append_device_channel_chunk(
            **channel_key, offset=offset, data_base64=base64.b64encode(part).decode("ascii"),
            chunk_sha256=hashlib.sha256(part).hexdigest(),
        )
    store.finish_device_channel_upload(
        **channel_key, expected_bytes=len(data), expected_sha256=sha256,
    )


@pytest.mark.parametrize("purpose", ["asset", "source", "source_and_asset"])
def test_tts_import_commit_is_stable_and_source_and_asset_share_one_ticket(tmp_path, purpose):
    device_id = "tts-client"
    connection_generation = 4
    executor_generation = 9
    model_id = "minimax-female-shaonv-zh"
    parent_request_id = f"tts-import-{purpose}"
    transfer_id = hashlib.sha256(f"{device_id}\0{connection_generation}\0{executor_generation}\0{parent_request_id}".encode()).hexdigest()[:32]
    filename = "reference.wav"
    data = _reference_wav()
    digest = hashlib.sha256(data).hexdigest()
    store = DeviceFileTransferStore(tmp_path / "device-data")
    library = VoiceLibrary(tmp_path / "voices", model=model_id)

    ticket = store.prepare_tts_asset_import(
        transfer_id=transfer_id, device_id=device_id,
        connection_generation=connection_generation, executor_generation=executor_generation,
        parent_request_id=parent_request_id, model_id=model_id, purpose=purpose,
        filename=filename, expected_bytes=len(data),
    )
    assert ticket["expected_bytes"] == len(data)
    assert ticket["transfer_id"] == transfer_id
    assert "path" not in ticket and "staging_dir" not in ticket
    _upload_tts_ticket(store, ticket, device_id, connection_generation, executor_generation, data, digest)

    staged, binding = store.read_tts_asset_import(
        transfer_id=transfer_id, device_id=device_id,
        connection_generation=connection_generation, executor_generation=executor_generation,
        parent_request_id=parent_request_id, model_id=model_id, purpose=purpose,
        filename=filename, expected_bytes=len(data), expected_sha256=digest,
    )
    assert staged == data
    first = library.commit_remote_import(staged, **binding)
    asset_count = len(library._index["assets"])
    repeated = library.commit_remote_import(staged, **binding)
    assert repeated == first
    assert len(library._index["assets"]) == asset_count

    if purpose == "asset":
        assert set(first) == {"asset"}
    elif purpose == "source":
        assert set(first) == {"asset"}
    else:
        assert set(first) == {"asset", "source"}
        assert first["asset"]["asset_id"] != first["source"]["asset_id"]

    public_roles = ({"asset": "asset", "source": "source"} if purpose == "source_and_asset"
                    else {"asset": "source"} if purpose == "source" else {"asset": "asset"})
    for public_role, storage_role in public_roles.items():
        result = first[public_role]
        expected_id = library.remote_import_asset_id(transfer_id, purpose, storage_role)
        assert result["asset_id"] == expected_id
        assert "path" not in result

    changed = {**binding, "filename": "different.wav"}
    with pytest.raises(VoiceLibraryError):
        library.commit_remote_import(staged, **changed)


def test_tts_import_ticket_rejects_oversize_wrong_generation_wrong_transfer_and_hash(tmp_path):
    store = DeviceFileTransferStore(tmp_path / "device-data")
    device_id = "tts-client"
    transfer_id = "e" * 32
    parent_request_id = "tts-import-validation"
    model_id = "minimax-female-shaonv-zh"
    with pytest.raises(DeviceFileTransferError):
        store.prepare_tts_asset_import(
            transfer_id="d" * 32, device_id=device_id, connection_generation=2,
            executor_generation=3, parent_request_id=parent_request_id, model_id="voxcpm2",
            purpose="asset", filename="reference.wav", expected_bytes=1,
        )
    with pytest.raises(DeviceFileTransferError):
        store.prepare_tts_asset_import(
            transfer_id=transfer_id, device_id=device_id, connection_generation=2,
            executor_generation=3, parent_request_id=parent_request_id, model_id=model_id,
            purpose="asset", filename="reference.wav", expected_bytes=MAX_IMPORT_BYTES + 1,
        )

    data = _reference_wav()
    digest = hashlib.sha256(data).hexdigest()
    ticket = store.prepare_tts_asset_import(
        transfer_id=transfer_id, device_id=device_id, connection_generation=2,
        executor_generation=3, parent_request_id=parent_request_id, model_id=model_id,
        purpose="asset", filename="reference.wav", expected_bytes=len(data),
    )
    _upload_tts_ticket(store, ticket, device_id, 2, 3, data, digest)
    with pytest.raises(DeviceFileTransferError):
        store.read_tts_asset_import(
            transfer_id=transfer_id, device_id=device_id, connection_generation=1,
            executor_generation=3, parent_request_id=parent_request_id, model_id=model_id,
            purpose="asset", filename="reference.wav", expected_bytes=len(data), expected_sha256=digest,
        )
    with pytest.raises(DeviceFileTransferError):
        store.read_tts_asset_import(
            transfer_id="f" * 32, device_id=device_id, connection_generation=2,
            executor_generation=3, parent_request_id=parent_request_id, model_id=model_id,
            purpose="asset", filename="reference.wav", expected_bytes=len(data), expected_sha256=digest,
        )
    with pytest.raises(DeviceFileTransferError):
        store.read_tts_asset_import(
            transfer_id=transfer_id, device_id=device_id, connection_generation=2,
            executor_generation=3, parent_request_id=parent_request_id, model_id=model_id,
            purpose="asset", filename="reference.wav", expected_bytes=len(data), expected_sha256="0" * 64,
        )
