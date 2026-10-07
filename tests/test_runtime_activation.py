"""Runtime selection（运行环境选择）：只读便携配置，不选择系统解释器。"""
from pathlib import Path
import json
import pytest
from voice_terminal.application.environment import resolve_python
from voice_terminal.local_layout import default_portable
from voice_terminal.paths import atomic_json


def fixture(root):
    (root / 'apps/desktop').mkdir(parents=True)
    (root / 'apps/desktop/Cargo.toml').touch()
    install = root / '.local'
    python = install / 'runtimes/python/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    atomic_json(install / 'portable.json', default_portable())
    return install, python


def test_source_uses_same_formal_interpreter(tmp_path):
    install, python = fixture(tmp_path)
    assert resolve_python(tmp_path) == str(python)
    assert resolve_python(tmp_path, str(python)) == str(python)


def test_missing_selected_runtime_fails_without_fallback(tmp_path):
    _, python = fixture(tmp_path)
    python.unlink()
    with pytest.raises(RuntimeError): resolve_python(tmp_path)


def test_other_explicit_interpreter_is_rejected(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError): resolve_python(tmp_path, 'other-python.exe')


def test_old_schema_is_not_accepted_by_normal_entry(tmp_path):
    install, _ = fixture(tmp_path)
    atomic_json(install / 'portable.json', {**default_portable(), 'schema': 1})
    with pytest.raises(ValueError, match='第二版'): resolve_python(tmp_path)
