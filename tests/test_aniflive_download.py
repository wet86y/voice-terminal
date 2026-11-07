"""固定输入下载的真实断点、损坏与中断边界。"""
import hashlib
import io
from pathlib import Path
import pytest
from voice_terminal.tts import aniflive_install as install


class Response(io.BytesIO):
    def __init__(self, body, status=200, content_range=''):
        super().__init__(body)
        self.status = status
        self.headers = {'Content-Range': content_range}


@pytest.fixture
def download(tmp_path, monkeypatch):
    payload = b'fixed-model-input'
    row = {'url': 'https://example.invalid/model', 'size': len(payload),
           'sha256': hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(install.time, 'sleep', lambda _: None)
    return row, tmp_path / 'model', payload


def test_short_response_resumes_from_received_bytes(download, monkeypatch):
    row, target, payload = download
    requests = []
    def open_response(request, **_):
        requests.append(request.get_header('Range'))
        return Response(payload[:4]) if len(requests) == 1 else Response(payload[4:], 206, f'bytes 4-{len(payload)-1}/{len(payload)}')
    monkeypatch.setattr(install, 'urlopen', open_response)
    install.artifact(row, target, local=target.parent, program=target.parent)
    assert requests == [None, 'bytes=4-'] and target.read_bytes() == payload
    assert not target.with_suffix('.part').exists()


def test_complete_partial_is_verified_without_network(download, monkeypatch):
    row, target, payload = download
    target.with_suffix('.part').write_bytes(payload)
    monkeypatch.setattr(install, 'urlopen', lambda *a, **kw: pytest.fail('完整有效断点不应重复联网'))
    install.artifact(row, target, local=target.parent, program=target.parent)
    assert target.read_bytes() == payload


def test_same_size_damage_retries_without_enabling_corrupt_file(download, monkeypatch):
    row, target, payload = download
    count = []
    def open_response(*_, **__):
        count.append(1)
        return Response(b'x' * len(payload))
    monkeypatch.setattr(install, 'urlopen', open_response)
    target.write_bytes(b'previous-candidate')
    with pytest.raises(ValueError, match='摘要不匹配'):
        install.artifact(row, target, local=target.parent, program=target.parent)
    assert len(count) == 3
    assert target.read_bytes() == b'previous-candidate'
    assert not target.with_suffix('.part').exists()


def test_cancel_keeps_partial_and_rejects_wrong_resume_range(download, monkeypatch):
    row, target, payload = download
    class Interrupted(Response):
        def read(self, size):
            if self.tell():
                raise KeyboardInterrupt('模拟用户取消')
            return super().read(4)
    monkeypatch.setattr(install, 'urlopen', lambda *a, **kw: Interrupted(payload))
    with pytest.raises(KeyboardInterrupt):
        install.artifact(row, target, local=target.parent, program=target.parent)
    assert not target.exists() and target.with_suffix('.part').read_bytes() == payload[:4]
    monkeypatch.setattr(install, 'urlopen', lambda *a, **kw: Response(payload[4:], 206, 'bytes 3-9/17'))
    with pytest.raises(RuntimeError, match='续传范围不匹配'):
        install.artifact(row, target, local=target.parent, program=target.parent)
    assert not target.exists() and target.with_suffix('.part').read_bytes() == payload[:4]
