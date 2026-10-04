"""AppPaths（应用路径）：程序、只读资源和用户数据的唯一解析入口。"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tempfile


@dataclass(frozen=True)
class AppPaths:
    program_root: Path  # program_root（当前程序包或源码目录）。
    install_root: Path  # install_root（应用安装／解压根目录）。
    data_root: Path | None = None  # data_root（独立用户数据目录；为空时兼容源码布局）。

    @classmethod
    def resolve(cls, program_root: Path | None = None, *, data_dir: Path | None = None) -> "AppPaths":
        program = Path(os.environ.get("VOICE_PROGRAM_ROOT") or program_root or Path(__file__).resolve().parents[1]).resolve()
        install = Path(os.environ.get("VOICE_INSTALL_ROOT") or program).resolve()
        value = data_dir or os.environ.get("VOICE_DATA_DIR")
        data = (install / value).resolve() if value else None
        if data:
            development_data = install == program and data.is_relative_to(program) and data.relative_to(program).parts[:1] in [(".runtime",), ("data",)]
            if (data == program or program.is_relative_to(data) or data.is_relative_to(program)) and not development_data:
                raise ValueError("用户数据目录不能与程序包重叠；源码模式仅允许独立的 data（数据）或 .runtime（本地运行）子目录。")
        return cls(program, install, data)

    @property
    def separated(self) -> bool:
        return self.data_root is not None

    @property
    def config(self) -> Path:
        return (self.data_root or self.program_root) / "config"

    @property
    def default_config(self) -> Path:
        return self.program_root / "config/default.yaml"

    @property
    def persona(self) -> Path:
        return self.data_root / "persona" if self.data_root else self.config / "persona"

    @property
    def speaker(self) -> Path:
        return self.data_root / "speaker" if self.data_root else self.config / "speaker"

    @property
    def history(self) -> Path:
        return self.data_root / "history" if self.data_root else self.program_root / ".runtime/continuity"

    @property
    def logs(self) -> Path:
        return (self.data_root or self.program_root) / "logs"

    @property
    def cache(self) -> Path:
        return self.data_root / "cache" if self.data_root else self.program_root / ".runtime/cache"

    @property
    def application_state(self) -> Path:
        return self.data_root / "state/application" if self.data_root else self.program_root / ".runtime/application"

    @property
    def env_file(self) -> Path:
        return self.data_root / ".env.local" if self.data_root else self.program_root / ".env.local"

    @property
    def dsh_home(self) -> Path:
        return self.data_root / "dsh" if self.data_root else self.program_root / ".runtime/dsh/home"

    @property
    def keywords(self) -> Path:
        return self.cache / "kws/keywords.txt"

    def environment(self) -> dict[str, str]:
        values = {"VOICE_PROGRAM_ROOT": str(self.program_root), "VOICE_INSTALL_ROOT": str(self.install_root)}
        if self.data_root:
            values.update(VOICE_DATA_DIR=str(self.data_root), VOICE_RESOURCE_POLICY=str(self.config / "runtime-resources.json"),
                          PYTHONPYCACHEPREFIX=str(self.cache / "python"), PYTHONNOUSERSITE="1")
            for name, folder in (("MODELSCOPE_CACHE", "modelscope"), ("HF_HOME", "huggingface"),
                                 ("TORCH_HOME", "torch"), ("PIP_CACHE_DIR", "pip")):
                values[name] = str(self.cache / folder)
        return values

    def prepare(self) -> None:
        """只补缺失的用户模板，绝不覆盖已有设置或个人资料。"""
        if not self.data_root:
            return
        for directory in (self.config, self.persona, self.speaker, self.history, self.logs, self.cache,
                          self.application_state, self.data_root / "imports", self.dsh_home, self.keywords.parent):
            if not directory.resolve().is_relative_to(self.data_root):
                raise ValueError(f"用户数据子目录指向数据根目录之外：{directory.name}")
            directory.mkdir(parents=True, exist_ok=True)
        # 真实写入测试同时覆盖权限与只读盘，不能仅凭 os.access（权限推断）判断。
        with tempfile.TemporaryFile(dir=self.data_root):
            pass
        templates = [(self.program_root / "config/runtime-resources.json", self.config / "runtime-resources.json")]
        persona_templates = self.program_root / "config/templates/persona"
        if not persona_templates.is_dir():
            persona_templates = self.program_root / "config/persona"
        templates += [(persona_templates / name, self.persona / name)
                      for name in ("personality.md", "user.md", "instructions.md")]
        for source, target in templates:
            if source.is_file() and not target.exists():
                shutil.copyfile(source, target)


def atomic_json(path: Path, value: dict) -> None:
    """atomic_json（原子结构化记录）：同卷临时文件替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def python_command(executable: str, module: str, *arguments: str, root: Path | None = None) -> list[str]:
    """显式脚本入口兼容可嵌入解释器，不依赖被隔离的 PYTHONPATH（模块搜索环境变量）。"""
    program = AppPaths.resolve(root).program_root
    entry = program / "scripts/backend-entry.py"
    if entry.is_file():
        paths = AppPaths.resolve(root)
        prefix = ["-X", f"pycache_prefix={paths.cache / 'python'}"] if paths.separated else []
        return [executable, "-X", "utf8", *prefix, str(entry), module, *map(str, arguments)]
    return [executable, "-m", module, *map(str, arguments)]
