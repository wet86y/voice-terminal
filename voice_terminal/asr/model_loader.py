"""Selective imports（按需导入）：仅适配已验证的流式识别依赖，保留原模型实现。"""
from __future__ import annotations

import ast
import hashlib
import importlib
from importlib import metadata
from importlib.abc import MetaPathFinder
from importlib.machinery import SourceFileLoader
from importlib.util import find_spec, spec_from_file_location
import logging
import os
from pathlib import Path
import sys
import threading

log = logging.getLogger(__name__)
_lock = threading.RLock()

# supported_version（已验证版本）及 source_hashes（适配源文件摘要）：版本或文件改变即走原路径。
_VERSION = "1.4.14"
_SOURCE_HASHES = {
    "__init__.py": "2e14bea49617caf22b22db64c51da367ee15f686d23cade0c905b1bf5c43d64c",
    "auto/auto_model.py": "c0dc2a7b0f95196c74ec98a54de643f0a1b05d1a2ba8ede0e951eaa95e9ac79c",
}
_COMPONENTS = {  # components（允许按需加载的模型配置及注册表名称）。
    "model": ("ParaformerStreaming", "model_classes", "funasr.models.paraformer_streaming.model"),
    "encoder": ("SANMEncoderChunkOpt", "encoder_classes", "funasr.models.scama.encoder"),
    # 同名解码器有重复注册；使用完整扫描最终选中的实现，避免改变模型构造。
    "decoder": ("ParaformerSANMDecoder", "decoder_classes", "funasr.models.e_paraformer.decoder"),
    "predictor": ("CifPredictorV2", "predictor_classes", "funasr.models.paraformer.cif_predictor"),
    "frontend": ("WavFrontendOnline", "frontend_classes", "funasr.frontends.wav_frontend"),
    "tokenizer": ("CharTokenizer", "tokenizer_classes", "funasr.tokenizer.char_tokenizer"),
    "specaug": ("SpecAugLFR", "specaug_classes", "funasr.models.specaug.specaug"),
}


def _supported_model(model_dir: Path) -> bool:
    """未知配置、外部代码和附加模型继续使用完整注册流程。"""
    import yaml
    try:
        config = yaml.safe_load((model_dir / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return False
    return (isinstance(config, dict)
            and all(config.get(key) == item[0] for key, item in _COMPONENTS.items())
            and config.get("normalize") is None
            and not any(config.get(key) for key in ("vad_model", "punc_model", "spk_model", "trust_remote_code", "remote_code")))


def _entry_code(source: str, filename: str):
    """保留官方包入口，只去掉唯一的全包扫描调用。"""
    tree = ast.parse(source, filename)
    scans = [node for node in tree.body if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
             and node.value.func.id == "import_submodules"]
    if len(scans) != 1:
        raise ValueError("语音框架包入口不匹配已验证结构")
    tree.body.remove(scans[0])
    return compile(tree, filename, "exec")


def _model_code(source: str, filename: str):
    """把官方聚类导入移动到实际选择声纹子模型的分支，推理代码保持原样。"""
    tree = ast.parse(source, filename)
    imports = [node for node in tree.body if isinstance(node, ast.Try)
               and any(isinstance(item, ast.ImportFrom)
                       and item.module == "funasr.models.campplus.cluster_backend" for item in node.body)]
    model = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AutoModel")
    init = next(node for node in model.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    branches = [node for node in init.body if isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "spk_model"]
    if len(imports) != 1 or len(branches) != 1:
        raise ValueError("语音模型入口不匹配已验证结构")
    deferred = imports[0]
    names = [alias.asname or alias.name for item in deferred.body if isinstance(item, ast.ImportFrom) for alias in item.names]
    if set(names) != {"sv_chunk", "postprocess", "distribute_spk", "ClusterBackend"}:
        raise ValueError("语音框架可选聚类导入不匹配")
    tree.body.remove(deferred)
    branches[0].body[0:0] = [ast.Global(names=names), deferred]
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")


class _PreparedLoader(SourceFileLoader):
    """PreparedLoader（预编译加载器）：适配仅存在于内存，不改依赖文件或字节码缓存。"""
    def __init__(self, name: str, path: Path, code):
        super().__init__(name, str(path))
        self.code = code  # code（经过固定结构校验的原模块代码）。

    def get_code(self, fullname):
        return self.code


class _PreparedFinder(MetaPathFinder):
    """PreparedFinder（临时模块查找器）：只接管两个明确指定的模块。"""
    def __init__(self, root: Path, codes: dict):
        self.root, self.codes = root, codes

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.codes:
            return None
        source = self.root / ("__init__.py" if fullname == "funasr" else "auto/auto_model.py")
        return spec_from_file_location(fullname, source, loader=_PreparedLoader(fullname, source, self.codes[fullname]),
                                       submodule_search_locations=[str(self.root)] if fullname == "funasr" else None)


def auto_model_class(model_dir: Path):
    """返回原 AutoModel（模型加载入口）；不符合适配条件时完整导入。"""
    with _lock:
        # VOICE_ASR_IMPORT_MODE（识别导入方式）：full（完整导入）用于对照与回退。
        if os.environ.get("VOICE_ASR_IMPORT_MODE") == "full" or "funasr" in sys.modules or not _supported_model(model_dir):
            return importlib.import_module("funasr").AutoModel
        try:
            spec = find_spec("funasr")
            if metadata.version("funasr") != _VERSION or spec is None or spec.origin is None:
                return importlib.import_module("funasr").AutoModel
            root = Path(spec.origin).parent
            sources = {name: (root / name).read_bytes() for name in _SOURCE_HASHES}
            if any(hashlib.sha256(sources[name]).hexdigest() != digest for name, digest in _SOURCE_HASHES.items()):
                log.info("识别导入：依赖文件与已验证摘要不同，使用完整加载")
                return importlib.import_module("funasr").AutoModel
            codes = {"funasr": _entry_code(sources["__init__.py"].decode("utf-8"), str(root / "__init__.py")),
                     "funasr.auto.auto_model": _model_code(sources["auto/auto_model.py"].decode("utf-8"), str(root / "auto/auto_model.py"))}
        except (metadata.PackageNotFoundError, OSError, SyntaxError, ValueError, StopIteration):
            log.info("识别导入：适配条件未满足，使用完整加载")
            return importlib.import_module("funasr").AutoModel
        finder = _PreparedFinder(root, codes)
        sys.meta_path.insert(0, finder)
        try:
            package = importlib.import_module("funasr")
            for _, _, module in _COMPONENTS.values():
                importlib.import_module(module)
            tables = importlib.import_module("funasr.register").tables
            for name, registry, module in _COMPONENTS.values():
                if getattr(tables, registry)[name].__module__ != module:
                    raise ImportError("语音模型组件注册来源不匹配：" + name)
            result = package.AutoModel
            log.info("识别导入：按需注册流式模型，已导入框架模块数=%s",
                     sum(name == "funasr" or name.startswith("funasr.") for name in sys.modules))
            return result
        except Exception:
            # 完整恢复注册表；本次尚未构造模型，所有识别状态和设备回退仍由原加载器负责。
            log.exception("识别按需导入失败，恢复完整加载")
            for name in list(sys.modules):
                if name == "funasr" or name.startswith("funasr."):
                    sys.modules.pop(name, None)
        finally:
            sys.meta_path.remove(finder)
        return importlib.import_module("funasr").AutoModel
