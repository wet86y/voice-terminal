"""公开仓库导入：权重选择、版本传递及下载恢复边界。"""
import hashlib
import io
import urllib.parse

import pytest

from voice_terminal.tts import aniflive_import as importer
from voice_terminal.tts.aniflive_preflight import require_writable_workspace


def test_generic_project_directory_does_not_label_auxiliary_weights():
    paths = ['GPT-SoVITS-overlay/GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin',
             'weights/tangyuge_v2pp_dpo-e10.ckpt', 'weights/tangyuge_v2pp_e4_s564.pth']
    rows = importer._classify([{'path': path} for path in paths])['weights']
    assert next(row for row in rows if row['role'] == 'gpt')['path'] == paths[1]
    assert next(row for row in rows if row['role'] == 'sovits')['path'] == paths[2]
    assert next(row for row in rows if row['path'] == paths[0])['role'] == 'unknown'


def test_modelscope_query_revision_and_recursive_listing(monkeypatch):
    source, repository, revision = importer._repo_url(
        'https://modelscope.cn/models/owner/model/files?Revision=release%2Ftwo')
    assert (source, repository, revision) == ('modelscope', 'owner/model', 'release/two')
    requests = []
    def download(url):
        requests.append(url)
        if '/repo/files?' in url:
            return {'Data': {'Files': [{'Type': 'tree', 'Path': 'weights'},
                {'Type': 'blob', 'Path': 'weights/voice.pth', 'Size': 9, 'Revision': 'fixed-file'}]}}, {}
        return {'Data': {'Revision': 'master'}}, {}
    monkeypatch.setattr(importer, '_download_json', download)
    fixed, files = importer._modelscope_files(repository, revision)
    assert fixed == 'release/two'
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(requests[-1]).query) == {
        'Revision': ['release/two'], 'Recursive': ['true']}
    assert files == [{'path': 'weights/voice.pth', 'size': 9, 'revision': 'fixed-file'}]


class Response(io.BytesIO):
    def __init__(self, content, *, status=200, headers=None, interrupt=False):
        super().__init__(content)
        self.status = status
        self.headers = headers or {}
        self.interrupt = interrupt
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if self.interrupt and self.reads == 2:
            raise OSError('网络连接中断')
        return super().read(size)


@pytest.mark.parametrize('honors_range', [True, False])
def test_download_retries_partial_connection_and_verifies_content(tmp_path, monkeypatch, honors_range):
    local = tmp_path / '中文 安装路径'
    (local / 'data').mkdir(parents=True)
    service = importer.AnifLiveImportService(local, local / 'data')
    monkeypatch.setattr(importer.time, 'sleep', lambda _: None)
    body = b'a' * (1024 * 1024) + b'end'
    requests = []
    def open_url(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            return Response(body[:1024 * 1024], headers={'Content-Length': str(len(body))}, interrupt=True)
        assert request.get_header('Range') == 'bytes=1048576-'
        return (Response(body[1024 * 1024:], status=206,
                    headers={'Content-Range': f'bytes 1048576-{len(body)-1}/{len(body)}'})
                if honors_range else Response(body))
    monkeypatch.setattr(importer.urllib.request, 'urlopen', open_url)
    destination = local / 'cache' / 'voice.pth'
    count, path = service._source_file({'source_type': 'huggingface', 'source': 'owner/model'},
        {'path': 'weights/中文 voice.pth', 'revision': 'fixed/version', 'size': len(body),
         'sha256': hashlib.sha256(body).hexdigest()}, destination, 'a' * 32, len(body), 0)
    assert count == len(body) and path.read_bytes() == body
    assert 'fixed%2Fversion/weights/%E4%B8%AD%E6%96%87%20voice.pth' in requests[0].full_url
    assert not destination.with_name('voice.pth.part').exists()


def test_wrong_content_never_published_and_all_retries_bounded(tmp_path, monkeypatch):
    service = importer.AnifLiveImportService(tmp_path, tmp_path / 'data')
    monkeypatch.setattr(importer.time, 'sleep', lambda _: None)
    requests = []
    def open_url(request, timeout):
        requests.append(request)
        return Response(b'bad')
    monkeypatch.setattr(importer.urllib.request, 'urlopen', open_url)
    destination = tmp_path / 'download.pth'
    with pytest.raises(ValueError, match='三次'):
        service._source_file({'source_type': 'modelscope', 'source': 'owner/model', 'revision': 'branch'},
            {'path': 'weights/model.pth', 'revision': 'fixed', 'size': 3, 'sha256': '0' * 64},
            destination, 'a' * 32, 3, 0)
    assert len(requests) == 3 and not destination.exists()
    assert all(urllib.parse.parse_qs(urllib.parse.urlsplit(r.full_url).query) == {
        'Revision': ['fixed'], 'FilePath': ['weights/model.pth']} for r in requests)


def test_preflight_checks_manifest_write_permission_before_download(tmp_path, monkeypatch):
    release = tmp_path / 'app/release.json'
    release.parent.mkdir()
    release.write_text('{}', encoding='utf-8')
    original = type(release).open
    def open_path(path, mode='r', *args, **kwargs):
        if path == release and mode == 'r+b':
            raise PermissionError('只读程序目录')
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(type(release), 'open', open_path)
    with pytest.raises(ValueError, match='程序清单'):
        require_writable_workspace(tmp_path, tmp_path / 'data')
    assert release.read_text(encoding='utf-8') == '{}'
    assert not list(tmp_path.rglob('.aniflive-write-*'))


def test_registration_receipt_permission_failure_is_reported_and_retryable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from voice_terminal.tts.aniflive_models import AnifLiveModelRegistry
    from voice_terminal.tts import aniflive_reference_registration
    task_id = 'a' * 32
    data = tmp_path / 'data'
    service = importer.AnifLiveImportService(tmp_path, data,
        voice_libraries=SimpleNamespace(for_model=lambda _: object()))
    task = {'task_id': task_id, 'model_id': 'model', 'status': 'converting'}
    importer._atomic_json(service._job_path(task_id), task)
    importer._atomic_json(data / 'state/resource-jobs.json', {'jobs': [{
        'job_id': 'aniflive-import-' + task_id, 'state': 'running', 'phase': '正在登记音色参考'}]})
    receipt = data / 'tts/aniflive/reference-imports' / task_id / 'reference-registration.json'
    monkeypatch.setattr(AnifLiveModelRegistry, 'resolve', lambda *args, **kwargs: (tmp_path, 'revision', {}))
    result = {'ready': 1, 'pending': 0, 'voice_id': 'voice', 'revision': 1}
    monkeypatch.setattr(aniflive_reference_registration, 'register_reference_bundle', lambda *args: result)
    write = importer._atomic_json
    def denied(path, value):
        if path == receipt:
            raise PermissionError('登记收据目录不可写')
        write(path, value)
    monkeypatch.setattr(importer, '_atomic_json', denied)
    failed = service._merge_desktop_status(task)
    assert failed['status'] == 'reference_failed'
    assert 'reference_registration' not in failed
    assert not receipt.exists()
    monkeypatch.setattr(importer, '_atomic_json', write)
    retried = service._merge_desktop_status(failed)
    assert retried['status'] == 'registering_references'
    assert retried['reference_registration'] == result
    assert receipt.is_file()


@pytest.mark.parametrize('status,receipt_valid,successful', [
    ('reference_failed', True, False), ('registering_references', False, False),
    ('registering_references', True, True),
])
def test_registration_worker_requires_success_state_and_matching_receipt(
    tmp_path, monkeypatch, capsys, status, receipt_valid, successful,
):
    import importlib.util
    import json
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[1] / 'scripts/register-aniflive-references.py'
    spec = importlib.util.spec_from_file_location('reference_worker_test', path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    task_id = 'a' * 32
    result = {'ready': 1, 'pending': 0, 'voice_id': 'voice', 'revision': 1}
    task = {'task_id': task_id, 'model_id': 'model', 'status': status, 'reference_registration': result}
    if receipt_valid:
        importer._atomic_json(tmp_path / 'tts/aniflive/reference-imports' / task_id / 'reference-registration.json',
            {'task_id': task_id, 'model_id': 'model', 'registration': result})
    monkeypatch.setattr(worker, 'ModelVoiceLibraries', lambda _: object())
    monkeypatch.setattr(worker, 'AnifLiveImportService', lambda *args, **kwargs: SimpleNamespace(task=lambda _: task))
    monkeypatch.setattr(sys, 'argv', [str(path), '--root', str(tmp_path), '--data', str(tmp_path), '--task', task_id])
    if successful:
        worker.main()
    else:
        with pytest.raises(SystemExit) as raised:
            worker.main()
        assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload['ok'] is successful
