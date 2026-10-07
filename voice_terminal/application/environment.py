"""Business environment（业务环境）：仅由唯一便携配置选择解释器。"""
from pathlib import Path
from ..local_layout import read_portable, assert_launch_ready


def activated_python(root: Path) -> str:
    install = root / '.local' if (root / 'apps/desktop/Cargo.toml').is_file() else root.parent
    assert_launch_ready(install)
    selected = read_portable(install)
    executable = install / selected['python']
    if not executable.is_file():
        raise RuntimeError('便携配置的 Python（后端运行环境）不可用')
    return str(executable)


def resolve_python(root: Path, explicit: str = '') -> str:
    executable = activated_python(root)
    if explicit and Path(explicit).resolve() != Path(executable).resolve():
        raise ValueError('业务解释器必须与 portable.json（便携配置）一致')
    return executable
