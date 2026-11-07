"""AnifLive（本地合成）的推理／导出依赖隔离，禁止加载环境内路径脚本。"""
from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import sys

from .aniflive_native import contained
_DLL_HANDLES = []  # 保持本进程显式 DLL（动态库）搜索目录有效。

RUNTIME_VERSIONS = {  # RUNTIME_VERSIONS（推理关键依赖固定版本）。
    "torch": "2.10.0+cu128", "torchaudio": "2.10.0+cu128",
    "tensorrt-cu12": "11.2.1.2", "transformers": "5.5.0",
    "tokenizers": "0.22.2", "huggingface-hub": "1.28.0",
}
EXPORT_VERSIONS = {  # EXPORT_VERSIONS（仅导出子进程启用的版本覆盖）。
    "transformers": "4.51.3", "tokenizers": "0.21.4", "huggingface-hub": "0.34.4",
}


def package_paths(local: Path, role: str) -> list[Path]:
    """package_paths（依赖目录）：显式列举，不运行 site（环境路径处理）。"""
    if role not in {"inference", "export"}:
        raise ValueError("AnifLive 依赖角色必须为 inference（推理）或 export（导出）。")
    root = contained(local, local / "runtimes/aniflive-tts")
    directories = ([root / "export-packages"] if role == "export" else []) + [root / "packages"]
    for path in directories:
        contained(local, path)
        if not path.is_dir():
            raise RuntimeError(f"AnifLive 专用依赖目录尚未准备：{path.name}")
    return directories


def validate_versions(directories: list[Path], role: str) -> dict:
    """读取隔离目录自身的包元数据；主环境已安装的包不能满足检查。"""
    installed = {}
    for directory in directories:
        for distribution in metadata.distributions(path=[str(directory)]):
            name = str(distribution.metadata.get("Name", "")).lower().replace("_", "-")
            installed.setdefault(name, distribution.version)
    required = {**RUNTIME_VERSIONS, **(EXPORT_VERSIONS if role == "export" else {})}
    errors = [f"{name}：需要 {version}，实际 {installed.get(name, '未安装')}"
              for name, version in required.items() if installed.get(name) != version]
    if errors:
        raise RuntimeError("AnifLive 隔离依赖不匹配：" + "；".join(errors))
    return required


def configure(local: Path, program: Path, role: str) -> dict:
    """configure（配置子进程）：必须在加载任何计算依赖前调用。"""
    if any(name in sys.modules for name in ("torch", "transformers", "tokenizers", "huggingface_hub", "tensorrt")):
        raise RuntimeError("计算依赖已加载，不能在当前进程切换 AnifLive 依赖角色。")
    if (local / 'runtimes/aniflive-tts/dependency-transaction.json').exists():
        raise RuntimeError('隔离运行依赖存在未完成组件事务，请在组件列表中修复后再加载模型。')
    directories = package_paths(local, role)
    versions = validate_versions(directories, role)
    # 未经桌面宿主启动的源码工具必须明确标记为开发渠道，否则首次资源策略
    # 装配会把默认 product（正式）渠道传给二次导出进程并触发路径拒绝。
    if not os.environ.get('VOICE_LAUNCH_CHANNEL') and (program / 'apps/desktop/Cargo.toml').is_file():
        if local.resolve() != (program / '.local').resolve():
            raise ValueError('源码配置任务只能使用当前项目的 .local（本机资源）目录。')
        os.environ['VOICE_LAUNCH_CHANNEL'] = 'development'
    from ..resources import ResourcePolicy
    policy = local / 'data/config/runtime-resources.json'
    os.environ.update(ResourcePolicy.load(policy if policy.is_file() else program / 'config/runtime-resources.json').environment('voice'))
    root = contained(local, local / "runtimes/aniflive-tts/source")
    source = root / "minimal_inference"
    if not (root / "windows-source-receipt.json").is_file():
        raise RuntimeError("AnifLive 兼容源码尚未完成准备。")
    # 保留解释器标准库，清除现有主环境及用户包目录。DLL（动态库）从包自身加载。
    python = Path(sys.base_prefix)
    stdlib = [python / "python312.zip", python / "Lib", python / "DLLs", python]
    sys.path[:] = [str(p) for p in [*directories, program, root / "src", source,
                                  source / "GPT_SoVITS", *stdlib]]
    if os.name == 'nt':
        for directory in directories:
            native = contained(local, directory / 'lib/site-packages/MeCab')
            if native.is_dir():
                _DLL_HANDLES.append(os.add_dll_directory(str(native)))
    scratch = contained(local, local / 'tmp/aniflive-runtime')
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ.update({"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "TEMP": str(scratch), "TMP": str(scratch),
                       "HF_XET_CACHE": str(local / 'cache/huggingface/xet'), "HF_HUB_DISABLE_XET": "1",
                       "MPLCONFIGDIR": str(local / 'cache/matplotlib'),
                       "NUMBA_CACHE_DIR": str(local / 'cache/numba'),
                       "NLTK_DATA": str(local / 'models/aniflive-tts/shared/nltk_data'),
                       "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "ANIFLIVE_TTS_SOURCE_DIR": str(source),
                       "ANIFLIVE_TTS_SHARED_DIR": str(local / "models/aniflive-tts/shared"),
                       "HF_HOME": str(local / "cache/huggingface"),
                       "TORCH_HOME": str(local / "cache/torch")})
    os.environ.pop("PYTHONPATH", None)
    os.environ.pop("ANIFLIVE_TTS_ALLOW_8GIB_BOUNDARY", None)
    return {"role": role, "versions": versions, "package_paths": [str(p) for p in directories],
            "fields": {"role": "本进程依赖角色", "versions": "关键依赖实际登记版本",
                       "package_paths": "本进程显式依赖目录"}}
