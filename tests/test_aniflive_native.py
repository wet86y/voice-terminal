"""固定源码和路径兼容的安全边界验证。"""
import json
import ast
import os
import sys
from types import SimpleNamespace
from pathlib import Path
import zipfile

import pytest

from voice_terminal.tts import aniflive_native as native


def archive_fixture(tmp_path, monkeypatch):
    # 此夹具只验证归档事务；阶段拆分另用完整固定源码执行验证。
    from voice_terminal.tts import aniflive_export
    monkeypatch.setattr(aniflive_export, 'stage_export_source', lambda text: text)
    archive = tmp_path / "source.zip"
    files = {
        "minimal_inference/export_onnx.py": "ssl_model = ssl_model.to(device)\ndef export():\n    torch.set_num_threads(4)\n" + native.GPT_ENCODER_VALIDATION + '\n',
        "minimal_inference/GPT_SoVITS/text/korean.py": "dictionary_path = mecab_ko_dic.dictionary_path.as_posix()\n",
        "src/aniflive_tts/service.py": "if int(properties.total_memory) < 8 * 1024 * 1024 * 1024:\n    pass\n",
        "src/aniflive_tts/backend/trt_builder.py": "if not parser.parse_from_file(str(source)):\n    pass\n",
        "scripts/setup_shared_assets.py": 'sorted(item for item in path.rglob("*") if item.is_file())\n',
    }
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, text in files.items():
            bundle.writestr(f"AnifLive-TTS-{native.UPSTREAM_COMMIT}/{name}", text)
    monkeypatch.setattr(native, "ARCHIVE_SHA256", native.digest(archive))
    original_patch = native.patch_source
    def patch_and_pin(source):
        receipt = original_patch(source)
        monkeypatch.setattr(native, "PATCHED_SHA256", {r["path"]: r["after_sha256"] for r in receipt["files"]})
        return receipt
    monkeypatch.setattr(native, "patch_source", patch_and_pin)
    local = tmp_path / "local"
    return archive, local


@pytest.mark.parametrize('fail', [False, True])
def test_encoder_validation_threads_restore_original_budget(monkeypatch, fail):
    state = {'threads': 4, 'calls': []}
    def set_threads(value):
        state['threads'] = value
        state['calls'].append(value)
    monkeypatch.setattr(native, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        get_num_threads=lambda: state['threads'], set_num_threads=set_threads))
    try:
        with native.gpt_encoder_validation_threads():
            assert state['threads'] == 24
            if fail:
                raise RuntimeError('模拟官方数值校验失败')
    except RuntimeError:
        assert fail
    assert state == {'threads': 4, 'calls': [24, 4]}


def test_non_windows_encoder_validation_preserves_resource_policy(monkeypatch):
    monkeypatch.setattr(native, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace())
    with native.gpt_encoder_validation_threads():
        pass  # 非 Windows（操作系统）不访问或改变张量库线程。


@pytest.mark.parametrize("windows", [False, True])
def test_vram_boundary_is_bounded_and_preserves_real_amount(windows):
    minimum = 8 * 1024**3 - (16 * 1024**2 if windows else 0)
    assert native.nominal_vram_supported(minimum, windows=windows)
    assert not native.nominal_vram_supported(minimum - 1, windows=windows)
    assert not native.nominal_vram_supported(True, windows=windows)


def test_fixed_archive_prepares_once_and_detects_modified_patch(tmp_path, monkeypatch):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    receipt = native.prepare_source(archive, local)
    assert len(receipt["files"]) == 5
    assert native.prepare_source(archive, local) == receipt
    source = local / "runtimes/aniflive-tts/source"
    path = source / receipt["files"][0]["path"]
    assert "ssl_model.float()" in path.read_text(encoding="utf-8")
    path.write_text("damaged", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        native.prepare_source(archive, local)


def test_wrong_archive_does_not_change_existing_files(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"invalid")
    local = tmp_path / "local"
    with pytest.raises(ValueError, match="摘要"):
        native.prepare_source(archive, local)
    assert not local.exists()


@pytest.mark.parametrize('activation_fails', [False, True])
def test_known_source_upgrade_checks_prior_and_restores_if_activation_fails(tmp_path, monkeypatch, activation_fails):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(native, 'PATCH_REVISION', 3)
    prior = native.prepare_source(archive, local)
    hashes = {r['path']:r['after_sha256'] for r in prior['files']}
    monkeypatch.setattr(native, 'PREVIOUS_PATCHED_SHA256', {3:hashes})
    monkeypatch.setattr(native, 'PATCH_REVISION', 5)
    target = local/'runtimes/aniflive-tts/source'
    calls = []
    def activate():
        calls.append(True)
        assert json.loads((target/'windows-source-receipt.json').read_text())['patch_revision'] == 3
        if activation_fails:
            raise RuntimeError('模拟暂停失败')
    if activation_fails:
        with pytest.raises(RuntimeError, match='暂停失败'):
            native.prepare_source(archive, local, allow_upgrade=True, before_activate=activate)
        assert json.loads((target/'windows-source-receipt.json').read_text()) == prior
    else:
        result = native.prepare_source(archive, local, allow_upgrade=True, before_activate=activate)
        assert result['patch_revision'] == 5
        assert native.prepare_source(archive, local) == result
    assert calls == [True]
    assert not (target.parent/'dependency-transaction.json').exists()


def test_unknown_source_structure_never_activates(tmp_path, monkeypatch):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(f"AnifLive-TTS-{native.UPSTREAM_COMMIT}/another.txt", "untouched")
    monkeypatch.setattr(native, "ARCHIVE_SHA256", native.digest(archive))
    monkeypatch.setattr(native, "patch_source", lambda source: (_ for _ in ()).throw(ValueError("结构")))
    with pytest.raises(ValueError, match="结构"):
        native.prepare_source(archive, local)
    assert not (local / "runtimes/aniflive-tts/source").exists()
    assert list((local / "tmp/aniflive-source").iterdir()) == []


@pytest.mark.parametrize("member", ["../escape", "C:/escape", "source/../escape", "source/x\\escape"])
def test_archive_traversal_rejected(tmp_path, monkeypatch, member):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.writestr(member, "bad")
    monkeypatch.setattr(native, "ARCHIVE_SHA256", native.digest(archive))
    with pytest.raises(ValueError, match="非法"):
        native.prepare_source(archive, local)
    assert not (local / "runtimes/aniflive-tts/source").exists()


def test_containment_rejects_external_target(tmp_path):
    with pytest.raises(ValueError, match="越出"):
        native.contained(tmp_path / "root", tmp_path / "outside")
    with pytest.raises(ValueError, match="父目录"):
        native.contained(tmp_path / "root", tmp_path / "root/../outside")


def test_modified_receipt_cannot_authorize_modified_source(tmp_path, monkeypatch):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    native.prepare_source(archive, local)
    source = local / "runtimes/aniflive-tts/source"
    path = source / "minimal_inference/export_onnx.py"
    path.write_text("modified", encoding="utf-8")
    receipt_path = source / "windows-source-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["inventory"]["minimal_inference/export_onnx.py"] = native.digest(path)
    receipt["files"][0]["after_sha256"] = native.digest(path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="固定官方归档"):
        native.prepare_source(archive, local)


def test_unpatched_source_corruption_is_not_hidden_by_patch_receipt(tmp_path, monkeypatch):
    archive, local = archive_fixture(tmp_path, monkeypatch)
    native.prepare_source(archive, local)
    (local / "runtimes/aniflive-tts/source/extra.py").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ValueError, match="集合损坏"):
        native.prepare_source(archive, local)


@pytest.mark.skipif(os.name != "nt", reason="Windows（操作系统）盘符映射验证")
def test_real_windows_alias_is_same_file_and_is_removed_on_exception(tmp_path, monkeypatch):
    monkeypatch.setenv('VOICE_INSTALL_ROOT', str(tmp_path))
    folder = tmp_path / "中文计算图"
    folder.mkdir()
    source = folder / "test.onnx"
    source.write_bytes(b"test")
    monkeypatch.setattr(native, "verify_external_data", lambda path: None)
    alias = None
    with pytest.raises(RuntimeError, match="模拟解析失败"):
        with native.parser_path(source) as path:
            alias = path
            assert str(path).isascii()
            assert os.path.samefile(path, source)
            raise RuntimeError("模拟解析失败")
    assert alias is not None and not alias.exists()
    assert source.read_bytes() == b"test"
