"""依赖角色迁移保持现用推理文件，并可从部分搬迁中断恢复。"""
import hashlib
import json
from pathlib import Path
import pytest
from voice_terminal.tts import aniflive_layout as layout

BUILDERS = ('ptx','sm100','sm120','sm75','sm80','sm86','sm89','sm90')  # 历史现场的八个转换构建器资源。

def fixture(tmp_path):
    root = tmp_path / 'runtimes/aniflive-tts'
    rows = []
    files = [('torch/keep.py', b'keep'), ('onnx/build.py', b'build'),
             ('tensorrt_libs/nvinfer_11.dll', b'runtime')]
    files.extend((f'tensorrt_libs/nvinfer_builder_resource_{name}_11.dll',b'builder') for name in BUILDERS)
    for relative, content in files:
        path = root / 'packages' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        rows.append({'path': relative, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
    runtime = {'schema': 1, 'role':'runtime', 'packages': {'torch':'fixed', 'onnx':'fixed'}, 'files': rows}
    export = {'schema': 1, 'role':'export', 'packages':{}, 'files':[]}
    (root/'export-packages').mkdir()
    for folder, value in [('packages', runtime), ('export-packages', export)]:
        (root/folder/'aniflive-packages.json').write_text(json.dumps(value), encoding='utf-8')
    return root, runtime


def test_split_preserves_runtime_and_records_conversion_files(tmp_path):
    root, original = fixture(tmp_path)
    layout.split(tmp_path)
    assert (root/'packages/torch/keep.py').read_bytes() == b'keep'
    assert (root/'packages/tensorrt_libs/nvinfer_11.dll').read_bytes() == b'runtime'
    assert (root/'export-packages/onnx/build.py').read_bytes() == b'build'
    assert (root/'conversion-libs/nvinfer_builder_resource_sm90_11.dll').read_bytes() == b'builder'
    assert not (root/'packages/onnx/build.py').exists()
    assert not (root/'layout-transaction.json').exists()
    receipt = json.loads((root/'packages/aniflive-packages.json').read_text('utf-8'))
    assert receipt['packages'] == {'torch':'fixed'}
    assert len(receipt['conversion_libraries']) == 8
    assert len(list((root/'conversion-libs').glob('*.dll'))) == 8
    layout.split(tmp_path)


def test_partial_move_failure_rolls_back_receipts_and_files(tmp_path, monkeypatch):
    root, original = fixture(tmp_path)
    rename = layout.os.rename
    calls = 0
    def fail_once(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated sharing violation')
        return rename(source, target)
    monkeypatch.setattr(layout.os, 'rename', fail_once)
    with pytest.raises(OSError):
        layout.split(tmp_path)
    assert json.loads((root/'packages/aniflive-packages.json').read_text('utf-8')) == original
    assert (root/'packages/onnx/build.py').read_bytes() == b'build'
    assert (root/'packages/tensorrt_libs/nvinfer_builder_resource_sm90_11.dll').read_bytes() == b'builder'


def test_changed_file_prevents_migration_before_writes(tmp_path):
    root, original = fixture(tmp_path)
    (root/'packages/onnx/build.py').write_bytes(b'user change')
    with pytest.raises(ValueError, match='内容变化'):
        layout.split(tmp_path)
    assert not (root/'layout-transaction.json').exists()
    assert (root/'packages/onnx/build.py').read_bytes() == b'user change'


def test_conversion_library_reinstalls_from_fixed_wheel_after_uninstall(tmp_path):
    import zipfile
    import shutil
    root, _ = fixture(tmp_path)
    layout.split(tmp_path)
    shutil.rmtree(root/'conversion-libs')
    wheel = tmp_path/'fixed.whl'
    with zipfile.ZipFile(wheel,'w') as package:
        for name in BUILDERS:
            package.writestr(f'tensorrt_libs/nvinfer_builder_resource_{name}_11.dll',b'builder')
    layout.prepare_libraries(tmp_path, lambda:wheel)
    assert (root/'conversion-libs/nvinfer_builder_resource_sm90_11.dll').read_bytes() == b'builder'
    assert (root/'packages/tensorrt_libs/nvinfer_11.dll').read_bytes() == b'runtime'
    assert len(list((root/'conversion-libs').glob('*.dll'))) == 8


def test_layout_first_recovers_existing_dependency_transaction(tmp_path):
    root, original = fixture(tmp_path)
    stage = root/'.source-stage-fixture'
    stage.mkdir()
    (stage/'temporary').write_bytes(b'temp')
    journal = {'schema':1,'state':'prepared','items':[{'target':'source','stage':stage.name,'backup':'.source-old-fixture','original':False}]}
    (root/'dependency-transaction.json').write_text(json.dumps(journal),encoding='utf-8')
    layout.split(tmp_path)
    assert not (root/'dependency-transaction.json').exists()
    assert not stage.exists()
    assert (root/'conversion-libs/nvinfer_builder_resource_sm90_11.dll').exists()
