"""共享语言资源的离线解包边界与现用目录保护。"""
import hashlib
import json
from pathlib import Path
import zipfile
import pytest
from voice_terminal.tts import aniflive_shared as shared


@pytest.mark.parametrize('member', ['example/../../escape', '/example/file', 'example/..\\escape', 'different/file'])
def test_dictionary_archive_cannot_escape_managed_candidate(tmp_path, monkeypatch, member):
    archive = tmp_path / 'download.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(member, 'invalid')
    row = {'path': 'nltk_data/corpora/example.zip', 'size': archive.stat().st_size,
           'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    upstream = {'NLTK_PACKAGES': {'example': {'subdir': 'corpora', 'tree_sha256': 'b' * 64}}}
    monkeypatch.setattr(shared, 'specification', lambda *_: ({'files': [row]}, upstream))
    with pytest.raises(ValueError, match='非法路径'):
        shared.activate(tmp_path, tmp_path, [(row, archive)])
    assert not (tmp_path / 'models/aniflive-tts/shared/nltk_data/corpora/example').exists()
    assert not (tmp_path / 'escape').exists()
    assert not list((tmp_path / 'tmp/aniflive-shared').iterdir())


def test_invalid_extracted_tree_never_replaces_existing_dictionary(tmp_path, monkeypatch):
    archive = tmp_path / 'download.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('example/model.txt', 'candidate')
    row = {'path': 'nltk_data/corpora/example.zip', 'size': archive.stat().st_size,
           'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    target = tmp_path / 'models/aniflive-tts/shared/nltk_data/corpora/example'
    target.mkdir(parents=True)
    (target / 'old.txt').write_text('原目录', encoding='utf-8')
    upstream = {'NLTK_PACKAGES': {'example': {'subdir': 'corpora', 'tree_sha256': 'b' * 64}},
                '_tree_digest': lambda _: 'a' * 64}
    monkeypatch.setattr(shared, 'specification', lambda *_: ({'files': [row]}, upstream))
    with pytest.raises(ValueError, match='目录摘要不符'):
        shared.activate(tmp_path, tmp_path, [(row, archive)])
    assert (target / 'old.txt').read_text(encoding='utf-8') == '原目录'
    assert not (target / 'model.txt').exists()
