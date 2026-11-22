"""旧导出暂存清理更新多层收据，且失败时逐字节恢复元数据。"""
import hashlib
import json
from pathlib import Path
import pytest
from voice_terminal import storage_cleanup as cleanup


def setup(tmp_path):
    package = 'models/aniflive-tts/packages/demo/' + 'a'*24
    stage = package + '/.export-stages-fixture'
    (tmp_path/stage).mkdir(parents=True)
    (tmp_path/'app').mkdir()
    (tmp_path/package/'engine.bin').write_bytes(b'keep engine')
    (tmp_path/stage/'export.bin').write_bytes(b'old export')
    def row(relative):
        content=(tmp_path/package/relative).read_bytes()
        return {'path':relative,'size':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    receipt={'identity':{'original':'build'},'validation':{'engine_count':9,'enqueue_verified':True},'files':[row('engine.bin'),row('.export-stages-fixture/export.bin')]}
    (tmp_path/package/'application-receipt.json').write_bytes(cleanup.encoded(receipt))
    checksums={r['path']:r['sha256'] for r in receipt['files']}
    checksums['application-receipt.json']=hashlib.sha256((tmp_path/package/'application-receipt.json').read_bytes()).hexdigest()
    (tmp_path/package/'checksums.json').write_bytes(cleanup.encoded(checksums))
    records=[]
    for path in (tmp_path/package).rglob('*'):
        if path.is_file():
            content=path.read_bytes()
            records.append({'path':path.relative_to(tmp_path/'models').as_posix(),'size':len(content),'sha256':hashlib.sha256(content).hexdigest()})
    (tmp_path/'models/manifest.json').write_bytes(cleanup.encoded({'files':records}))
    (tmp_path/'app/release.json').write_bytes(cleanup.encoded({'components':[{'kind':'models','manifest_sha256':'old'}]}))
    return package, stage


def test_prune_retains_engine_and_rebinds_all_hashes(tmp_path):
    package, stage=setup(tmp_path)
    result=cleanup.execute(tmp_path,stage)
    assert result['removed_bytes']==len(b'old export')
    assert not (tmp_path/stage).exists()
    assert (tmp_path/package/'engine.bin').read_bytes()==b'keep engine'
    receipt=json.loads((tmp_path/package/'application-receipt.json').read_bytes())
    assert receipt['validation']['engine_count']==9
    checksums=json.loads((tmp_path/package/'checksums.json').read_bytes())
    for relative, sha in checksums.items():
        assert hashlib.sha256((tmp_path/package/relative).read_bytes()).hexdigest()==sha
    manifest=json.loads((tmp_path/'models/manifest.json').read_bytes())
    for row in manifest['files']:
        content=(tmp_path/'models'/row['path']).read_bytes()
        assert len(content)==row['size']
        assert hashlib.sha256(content).hexdigest()==row['sha256']
    release=json.loads((tmp_path/'app/release.json').read_bytes())
    assert release['components'][0]['manifest_sha256']==hashlib.sha256((tmp_path/'models/manifest.json').read_bytes()).hexdigest()


def test_failed_metadata_commit_restores_exact_original_bytes(tmp_path,monkeypatch):
    package, stage=setup(tmp_path)
    originals={path:path.read_bytes() for path in tmp_path.rglob('*.json')}
    write=cleanup.atomic_bytes
    calls=0
    def fail_once(path,content):
        nonlocal calls
        calls+=1
        if calls==2: raise OSError('simulated interruption')
        write(path,content)
    monkeypatch.setattr(cleanup,'atomic_bytes',fail_once)
    with pytest.raises(OSError): cleanup.execute(tmp_path,stage)
    assert (tmp_path/stage/'export.bin').read_bytes()==b'old export'
    for path,content in originals.items(): assert path.read_bytes()==content


def test_foreign_and_changed_content_are_retained(tmp_path):
    package,stage=setup(tmp_path)
    with pytest.raises(ValueError): cleanup.execute(tmp_path,'data/history')
    (tmp_path/stage/'export.bin').write_bytes(b'user file')
    with pytest.raises(ValueError,match='身份不符'): cleanup.execute(tmp_path,stage)
    assert (tmp_path/stage/'export.bin').read_bytes()==b'user file'


def test_partial_removal_reports_only_successful_bytes_and_resumes(tmp_path, monkeypatch):
    package, stage = setup(tmp_path)
    remove = cleanup.remove_tree
    def partial(path, boundary):
        (path/'export.bin').unlink()
        raise PermissionError('simulated occupied directory')
    monkeypatch.setattr(cleanup, 'remove_tree', partial)
    result = cleanup.execute(tmp_path, stage)
    assert result['removed_bytes'] == len(b'old export')
    assert '继续收尾' in result['error']
    assert (tmp_path/cleanup.JOURNAL).exists()
    assert (tmp_path/package/'engine.bin').read_bytes() == b'keep engine'
    monkeypatch.setattr(cleanup, 'remove_tree', remove)
    assert cleanup.execute(tmp_path, cleanup.JOURNAL)['removed_bytes'] == 0
    assert not (tmp_path/cleanup.JOURNAL).exists()


def test_retired_runtime_referenced_by_legacy_settings_is_preserved(tmp_path):
    target = tmp_path/'models/audiocpp/runtime'
    target.mkdir(parents=True)
    (target/'legacy.bin').write_bytes(b'legacy runtime')
    catalog = tmp_path/'app/config/resources/catalog.json'
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(cleanup.encoded({'resources':[]}))
    (tmp_path/'models/manifest.json').write_bytes(cleanup.encoded({'files':[{
        'path':'audiocpp/runtime/legacy.bin','size':len(b'legacy runtime'),
        'sha256':hashlib.sha256(b'legacy runtime').hexdigest()}]}))
    (tmp_path/'app/release.json').write_bytes(cleanup.encoded({'components':[]}))
    settings = tmp_path/'data/config/product-settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_bytes(cleanup.encoded({'tts':{'backend':'audiocpp'}}))
    with pytest.raises(ValueError,match='仍引用'):
        cleanup.execute(tmp_path,'models/audiocpp/runtime')
    assert (target/'legacy.bin').read_bytes() == b'legacy runtime'
    assert not (tmp_path/cleanup.JOURNAL).exists()
