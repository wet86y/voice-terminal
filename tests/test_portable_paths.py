from pathlib import Path
import json
import pytest
from voice_terminal.paths import AppPaths
from voice_terminal.config import load_config
from voice_terminal.distribution import manifest, verify, verify_install, migrate, unpack, digest, files, require_stopped
from voice_terminal.paths import atomic_json
from voice_terminal.kws.keywords import generate_keywords_for
from voice_terminal.application.desktop import DesktopControl
import asyncio
import io
import time
import os
import subprocess
from types import SimpleNamespace
import zipfile


def test_paths_independent_of_working_directory(tmp_path, monkeypatch):
    program = tmp_path / "中文 程序"
    (program / "config/persona").mkdir(parents=True)
    (program / "config/default.yaml").write_text("wake:\n  keywords: ['你好助手']\n", encoding="utf-8")
    (program / "config/persona/user.md").write_text("原始模板", encoding="utf-8")
    monkeypatch.setenv("VOICE_PROGRAM_ROOT", str(program))
    monkeypatch.setenv("VOICE_INSTALL_ROOT", str(tmp_path))
    monkeypatch.setenv("VOICE_DATA_DIR", "中文 数据")
    monkeypatch.chdir(tmp_path)
    paths = AppPaths.resolve()
    paths.prepare()
    assert paths.persona == tmp_path / "中文 数据/persona"
    user = paths.persona / "user.md"
    user.write_text("用户修改", encoding="utf-8")
    paths.prepare()
    assert user.read_text(encoding="utf-8") == "用户修改"
    config = load_config(paths.default_config)
    assert config.kws_keywords_file == paths.config / "kws/keywords.txt"
    assert config.persona.directory == paths.persona
    assert paths.environment()["HF_HOME"].startswith(str(paths.cache))


def test_component_integrity_and_escape(tmp_path):
    (tmp_path / "中文 文件.txt").write_text("内容", encoding="utf-8")
    record = manifest(tmp_path, kind="app", build="test")
    verify(tmp_path, record)
    (tmp_path / "中文 文件.txt").write_text("损坏", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        verify(tmp_path, record)
    record["files"][0]["path"] = "../outside"
    with pytest.raises(ValueError, match="不安全"):
        verify(tmp_path, record)


def test_explicit_source_debug_keeps_an_independent_formal_workspace(tmp_path, monkeypatch):
    """Source debug（源码调试）只替换业务输入，工作区不被强制改回源码目录。"""
    from voice_terminal.local_layout import default_portable
    source = tmp_path / "源码 项目"
    install = tmp_path / "已安装 中文工作区"
    (source / "apps/desktop").mkdir(parents=True)
    (source / "apps/desktop/Cargo.toml").write_text("", "utf-8")
    install.mkdir()
    atomic_json(install / "portable.json", default_portable())
    monkeypatch.setenv("VOICE_PROGRAM_ROOT", str(source))
    monkeypatch.setenv("VOICE_INSTALL_ROOT", str(install))
    monkeypatch.setenv("VOICE_DATA_DIR", str(install / "data"))
    monkeypatch.setenv("VOICE_LAUNCH_CHANNEL", "product")
    monkeypatch.setenv("VOICE_SOURCE_DEBUG", "1")
    paths = AppPaths.resolve(source)
    assert paths.program_root == source
    assert paths.install_root == install
    assert paths.data_root == install / "data"
    monkeypatch.delenv("VOICE_SOURCE_DEBUG")
    with pytest.raises(ValueError, match="正式子进程"):
        AppPaths.resolve(source)


def test_migration_preserves_source_and_refuses_conflicts(tmp_path):
    old, data = tmp_path / "原 项目", tmp_path / "新 数据"
    (old / "config/persona").mkdir(parents=True)
    (old / "config/persona/user.md").write_text("用户", encoding="utf-8")
    (old / ".env.local").write_text("PRIVATE=example", encoding="utf-8")
    record = migrate(old, data, check_running=False)
    assert record["status"] == "copied_verified"
    assert (data / "persona/user.md").read_bytes() == (old / "config/persona/user.md").read_bytes()
    assert (old / ".env.local").is_file()
    with pytest.raises(FileExistsError):
        migrate(old, data, check_running=False)


def test_archive_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../escape.txt", "no")
    with pytest.raises(ValueError):
        unpack(archive, tmp_path / "stage")
    assert not (tmp_path / "escape.txt").exists()


def test_release_context_rejects_program_data_overlap(tmp_path, monkeypatch):
    program = tmp_path / 'apps/build'
    program.mkdir(parents=True)
    monkeypatch.setenv('VOICE_PROGRAM_ROOT', str(program))
    monkeypatch.setenv('VOICE_INSTALL_ROOT', str(tmp_path))
    for data in (program, program / 'config', tmp_path):
        with pytest.raises(ValueError, match='重叠'):
            AppPaths.resolve(data_dir=data)
    assert AppPaths.resolve(data_dir=tmp_path / 'data').separated


def test_kws_alias_lexicon_and_generated_files_stay_in_data(tmp_path, monkeypatch):
    program, data, model = tmp_path / 'apps/build', tmp_path / '数据 空格', tmp_path / 'models/kws'
    model.mkdir(parents=True)
    (model / 'tokens.txt').write_text('h 1\ne 2\n', encoding='utf-8')
    (model / 'en.phone').write_text('HEY HH EY\nCLAW K L AO\n', encoding='utf-8')
    original = {file.name: file.read_bytes() for file in model.iterdir()}
    monkeypatch.setenv('VOICE_PROGRAM_ROOT', str(program))
    monkeypatch.setenv('VOICE_INSTALL_ROOT', str(tmp_path))
    monkeypatch.setenv('VOICE_DATA_DIR', str(data))
    output = data / 'config/kws/keywords.txt'
    observed = []
    def token_command(command, **_kwargs):
        lexicon = Path(command[command.index('--lexicon') + 1])
        assert lexicon.is_relative_to(output.parent)
        assert 'HEI HH EY' in lexicon.read_text(encoding='utf-8')
        observed.append(lexicon)
        Path(command[-1]).write_text('h e k @HEI_CLAW\n', encoding='utf-8')
    monkeypatch.setattr('voice_terminal.kws.keywords.subprocess.run', token_command)
    generate_keywords_for(model_dir=model, raw_path=output.with_name('keywords_raw.txt'), output_path=output,
                          entries=[('hei claw', 'HEI_CLAW')])
    assert output.is_file() and observed and not observed[0].exists()
    assert {file.name: file.read_bytes() for file in model.iterdir()} == original


@pytest.mark.parametrize('name', ['CON.txt', 'directory./file', 'path/../other', 'C:/absolute', 'path\\file'])
def test_archive_rejects_windows_aliases(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as source:
        entry = zipfile.ZipInfo('placeholder')
        entry.filename = name  # 保留异常反斜杠；Windows（操作系统）的写入器默认会自动规范化。
        source.writestr(entry, 'content')
    with pytest.raises(ValueError):
        unpack(archive, tmp_path / 'stage')


def test_portable_fixed_schema_rejects_other_paths_and_selections(tmp_path):
    from voice_terminal.local_layout import read_portable, default_portable
    selected = default_portable()
    atomic_json(tmp_path / 'portable.json', selected)
    assert read_portable(tmp_path) == selected
    # 用户数据里的旧启用副本不能影响正常路径选择。
    atomic_json(tmp_path / 'data/state/active.json', {'app': 'invalid'})
    assert read_portable(tmp_path) == selected
    atomic_json(tmp_path / 'portable.json', {**selected, 'python': '../outside/python.exe'})
    with pytest.raises(ValueError, match='固定组件路径'):
        read_portable(tmp_path)


@pytest.mark.parametrize('state', ['prepared', 'installing', 'committed'])
def test_pending_update_blocks_source_and_headless_before_data_preparation(tmp_path, monkeypatch, state):
    from voice_terminal.application.environment import activated_python
    from voice_terminal.local_layout import default_portable
    program = tmp_path / 'source'
    install = program / '.local'
    (program / 'apps/desktop').mkdir(parents=True)
    (program / 'apps/desktop/Cargo.toml').write_text('fixture', encoding='utf-8')
    atomic_json(install / 'portable.json', default_portable())
    atomic_json(install / 'tmp/update-transaction.json', {'schema': 1, 'state': state, 'items': []})
    monkeypatch.setenv('VOICE_PROGRAM_ROOT', str(program))
    monkeypatch.setenv('VOICE_INSTALL_ROOT', str(install))
    monkeypatch.setenv('VOICE_DATA_DIR', str(install / 'data'))
    for start in (lambda: AppPaths.resolve(program), lambda: activated_python(program)):
        with pytest.raises(RuntimeError, match='未完成更新'):
            start()
    assert not (install / 'data').exists()
    (install / 'tmp/update-transaction.json').unlink()
    assert AppPaths.resolve(program).data_root == install / 'data'


def test_private_pipe_honors_exit_budget_and_rejects_malformed_messages(monkeypatch):
    class InlineLoop:
        def call_soon_threadsafe(self, callback): callback()
    for message, budget in [('[]\n', 14), ('{"protocol":1,"kind":"shutdown","budget_ms":300}\n', 0.3)]:
        application = SimpleNamespace(loop=InlineLoop(), exit_event=asyncio.Event())
        control = DesktopControl(application)
        assert not control.owned.is_set()
        monkeypatch.setattr('sys.stdin', io.StringIO(message))
        before = time.monotonic()
        control._read()
        assert application.exit_event.is_set() and control.owned.is_set()
        assert before + budget <= application.shutdown_deadline <= time.monotonic() + budget


def test_desktop_ready_frame_carries_one_time_token_only_over_private_pipe(monkeypatch):
    from voice_terminal.application import desktop
    output = io.StringIO()
    class DeferredThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    application = SimpleNamespace(loop=None, exit_event=asyncio.Event(), state=SimpleNamespace(value={"instance_id": "instance-1"}))
    control = DesktopControl(application)
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr(desktop.threading, "Thread", DeferredThread)
    control.start("http://127.0.0.1:5173", "short-lived-token")
    message = json.loads(output.getvalue())
    assert message["kind"] == "ready"
    assert message["origin"] == "http://127.0.0.1:5173"
    assert message["launch_token"] == "short-lived-token"
    assert message["instance_id"] == "instance-1"


def test_publisher_collection_can_exclude_data_before_following_links(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'program.txt').write_text('程序', encoding='utf-8')
    (tmp_path / 'data/user.txt').write_text('用户', encoding='utf-8')
    assert [file.name for file in files(tmp_path, exclude_roots=('data',))] == ['program.txt']


def test_migration_stopped_check_uses_working_directory_for_module_launch(tmp_path, monkeypatch):
    source, other = tmp_path / '项目', tmp_path / '项目-其他'
    source.mkdir(); other.mkdir()
    process = SimpleNamespace(pid=os.getpid()+1, info={'cmdline':['python', '-m', 'voice_terminal.application.launch']},
                              cwd=lambda: str(source), exe=lambda: str(tmp_path / 'tools/python.exe'))
    monkeypatch.setattr('psutil.process_iter', lambda _fields: [process])
    with pytest.raises(RuntimeError, match='退出应用'):
        require_stopped(source, tmp_path / '数据')
    process.cwd = lambda: str(other)
    require_stopped(source, tmp_path / '数据')  # 不拦截其他项目使用同名模块的进程。


@pytest.mark.skipif(os.name != 'nt', reason='Windows（操作系统）目录联接验证')
def test_migration_rejects_a_linked_source_root(tmp_path):
    source, external, data = tmp_path / 'source', tmp_path / 'external', tmp_path / 'data'
    source.mkdir(); external.mkdir()
    (external / 'private.txt').write_text('外部数据', encoding='utf-8')
    link = source / 'logs'
    subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                    'New-Item -ItemType Junction -Path $env:VOICE_TEST_LINK -Value $env:VOICE_TEST_TARGET -ErrorAction Stop | Out-Null'],
                   env={**os.environ, 'VOICE_TEST_LINK': str(link), 'VOICE_TEST_TARGET': str(external)},
                   check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='链接'):
            migrate(source, data, check_running=False)
        with pytest.raises(ValueError, match='链接'):
            list(files(link))
        assert not data.exists()
        assert (external / 'private.txt').is_file()
    finally:
        link.rmdir()  # 仅移除测试创建的联接，保留实际目标。
