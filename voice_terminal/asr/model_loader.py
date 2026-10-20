"""Selective imports（按需导入）及有边界的权重加载适配。"""
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

# 只有这个 FunASR 版本及其源文件摘要匹配时才应用内存中的 AST（抽象语法树）适配。
_VERSION = "1.4.14"
_SOURCE_HASHES = {
    "__init__.py": "2e14bea49617caf22b22db64c51da367ee15f686d23cade0c905b1bf5c43d64c",
    "auto/auto_model.py": "c0dc2a7b0f95196c74ec98a54de643f0a1b05d1a2ba8ede0e951eaa95e9ac79c",
    "train_utils/load_pretrained_model.py": "ff1cab2426e9b25057cb7ea6be8d9e5fdaf41d03e104702c1baa47394c49c982",
}

# 每项格式为注册名称、注册表和实现模块；组件集合按本地 config.yaml 精确识别。
_COMPONENTS = {
    "offline_model": ("BiCifParaformer", "model_classes", "funasr.models.bicif_paraformer.model"),
    "offline_encoder": ("SANMEncoder", "encoder_classes", "funasr.models.sanm.encoder"),
    "offline_predictor": ("CifPredictorV3", "predictor_classes", "funasr.models.bicif_paraformer.cif_predictor"),
    "emotion_model": ("Emotion2vec", "model_classes", "funasr.models.emotion2vec.model"),
    "paraformer_model": ("ParaformerStreaming", "model_classes", "funasr.models.paraformer_streaming.model"),
    "paraformer_encoder": ("SANMEncoderChunkOpt", "encoder_classes", "funasr.models.scama.encoder"),
    # 同名解码器有重复注册；使用完整扫描最终选中的实现，避免改变模型构造。
    "paraformer_decoder": ("ParaformerSANMDecoder", "decoder_classes", "funasr.models.e_paraformer.decoder"),
    "paraformer_predictor": ("CifPredictorV2", "predictor_classes", "funasr.models.paraformer.cif_predictor"),
    "wav_frontend": ("WavFrontend", "frontend_classes", "funasr.frontends.wav_frontend"),
    "online_frontend": ("WavFrontendOnline", "frontend_classes", "funasr.frontends.wav_frontend"),
    "char_tokenizer": ("CharTokenizer", "tokenizer_classes", "funasr.tokenizer.char_tokenizer"),
    "sentencepiece_tokenizer": ("SentencepiecesTokenizer", "tokenizer_classes", "funasr.tokenizer.sentencepiece_tokenizer"),
    "specaug": ("SpecAugLFR", "specaug_classes", "funasr.models.specaug.specaug"),
    "sensevoice_model": ("SenseVoiceSmall", "model_classes", "funasr.models.sense_voice.model"),
    "sensevoice_encoder": ("SenseVoiceEncoderSmall", "encoder_classes", "funasr.models.sense_voice.model"),
    "fsmn_vad_model": ("FsmnVADStreaming", "model_classes", "funasr.models.fsmn_vad_streaming.model"),
    "fsmn_encoder": ("FSMN", "encoder_classes", "funasr.models.fsmn_vad_streaming.encoder"),
    "campplus_model": ("CAMPPlus", "model_classes", "funasr.models.campplus.model"),
    "streaming_punc_model": ("CTTransformerStreaming", "model_classes", "funasr.models.ct_transformer_streaming.model"),
    "streaming_punc_encoder": ("SANMVadEncoder", "encoder_classes", "funasr.models.ct_transformer_streaming.encoder"),
}
_MODEL_CONFIGS = {
    "offline": (
        {"model": "BiCifParaformer", "encoder": "SANMEncoder", "predictor": "CifPredictorV3",
         "frontend": "WavFrontend", "tokenizer": "CharTokenizer"},
        ("offline_model", "offline_encoder", "offline_predictor", "paraformer_decoder", "wav_frontend", "char_tokenizer", "specaug"),
    ),
    "emotion": (
        {"model": "Emotion2vec", "tokenizer": "CharTokenizer"},
        ("emotion_model", "char_tokenizer"),
    ),
    "streaming_punc": (
        {"model": "CTTransformerStreaming", "encoder": "SANMVadEncoder", "tokenizer": "CharTokenizer"},
        ("streaming_punc_model", "streaming_punc_encoder", "char_tokenizer"),
    ),
    "paraformer": (
        {"model": "ParaformerStreaming", "encoder": "SANMEncoderChunkOpt",
         "decoder": "ParaformerSANMDecoder", "predictor": "CifPredictorV2",
         "frontend": "WavFrontendOnline", "tokenizer": "CharTokenizer", "specaug": "SpecAugLFR"},
        ("paraformer_model", "paraformer_encoder", "paraformer_decoder", "paraformer_predictor",
         "online_frontend", "char_tokenizer", "specaug"),
    ),
    "sensevoice": (
        {"model": "SenseVoiceSmall", "encoder": "SenseVoiceEncoderSmall",
         "tokenizer": "SentencepiecesTokenizer", "frontend": "WavFrontend", "specaug": "SpecAugLFR"},
        ("sensevoice_model", "sensevoice_encoder", "sentencepiece_tokenizer", "wav_frontend", "specaug"),
    ),
    "fsmn_vad": (
        {"model": "FsmnVADStreaming", "encoder": "FSMN", "frontend": "WavFrontendOnline"},
        ("fsmn_vad_model", "fsmn_encoder", "online_frontend"),
    ),
}
_SIDE_MODELS = ("vad_model", "punc_model", "spk_model", "trust_remote_code", "remote_code")
_MODULE_PATHS = {
    "funasr": "__init__.py",
    "funasr.auto.auto_model": "auto/auto_model.py",
    "funasr.train_utils.load_pretrained_model": "train_utils/load_pretrained_model.py",
}


def _config_components(model_dir: Path) -> tuple[str, ...] | None:
    """返回已知配置的组件并集；未知配置必须走完整注册扫描。"""
    import yaml

    try:
        config = yaml.safe_load((Path(model_dir) / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(config, dict) or any(config.get(key) for key in _SIDE_MODELS):
        return None
    for family, (expected, components) in _MODEL_CONFIGS.items():
        if not all(config.get(key) == value for key, value in expected.items()):
            continue
        if family == "paraformer" and config.get("normalize") is not None:
            continue
        return components
    return None


def _supported_model(model_dir: Path) -> bool:
    """兼容旧调用名：是否为本模块已验证的单模型配置。"""
    return _config_components(model_dir) is not None


def _requested_components(model_dirs) -> tuple[str, ...] | None:
    selected: list[str] = []
    for model_dir in model_dirs:
        components = _config_components(Path(model_dir))
        if components is None:
            return None
        for component in components:
            if component not in selected:
                selected.append(component)
    return tuple(selected)


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


def _pretrained_loader_code(source: str, filename: str):
    """候选加载路径：避免整份 deepcopy，并为兼容格式回退到原 torch.load。"""
    tree = ast.parse(source, filename)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "load_pretrained_model"]
    if len(functions) != 1:
        raise ValueError("预训练权重加载函数不匹配已验证结构")
    function = functions[0]
    checkpoint_loads = []
    deepcopies = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id == "ori_state" and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "torch" and call.func.attr == "load"
                    and call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "path"):
                checkpoint_loads.append(node)
        if node.targets[0].id == "src_state" and isinstance(node.value, ast.Call):
            call = node.value
            if (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "copy" and call.func.attr == "deepcopy"
                    and len(call.args) == 1 and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == "ori_state"):
                deepcopies.append(node)
    if len(checkpoint_loads) != 1 or len(deepcopies) != 1:
        raise ValueError("预训练权重复制或文件加载表达式不匹配")

    helper = ast.parse('''
def _voice_load_checkpoint(path, map_location):
    try:
        return torch.load(path, map_location=map_location, mmap=True)
    except (TypeError, RuntimeError) as error:
        # 只对“mmap 参数/文件格式不支持”回退；真实损坏、设备错误等仍原样抛出。
        message = str(error).lower()
        unsupported_keyword = "mmap" in message and "unexpected keyword argument" in message
        unsupported_format = "mmap can only be used" in message
        if not (unsupported_keyword or unsupported_format):
            raise
        logging.warning("checkpoint mmap（内存映射）不可用，改用常规 torch.load：%s", error)
        return torch.load(path, map_location=map_location)
''', filename).body
    checkpoint_loads[0].value = ast.Call(
        func=ast.Name(id="_voice_load_checkpoint", ctx=ast.Load()),
        args=[ast.Name(id="path", ctx=ast.Load()), ast.Name(id="map_location", ctx=ast.Load())],
        keywords=[],
    )
    deepcopies[0].value = ast.Name(id="ori_state", ctx=ast.Load())
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Import) and any(alias.name == "copy" for alias in node.names))]
    index = tree.body.index(function)
    tree.body[index:index] = helper
    tree.body.append(ast.Assign(
        targets=[ast.Attribute(value=ast.Name(id="load_pretrained_model", ctx=ast.Load()),
                               attr="__voice_model_load_mode__", ctx=ast.Store())],
        value=ast.Constant(value="mmap"),
    ))
    ast.fix_missing_locations(tree)
    return compile(tree, filename, "exec")


class _PreparedLoader(SourceFileLoader):
    """PreparedLoader（预编译加载器）：适配仅存在内存，不写依赖文件或字节码缓存。"""
    def __init__(self, name: str, path: Path, code):
        super().__init__(name, str(path))
        self.code = code

    def get_code(self, fullname):
        return self.code


class _PreparedFinder(MetaPathFinder):
    """临时查找器：只接管通过摘要验证的明确模块。"""
    def __init__(self, root: Path, codes: dict):
        self.root, self.codes = root, codes

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.codes:
            return None
        source = self.root / _MODULE_PATHS[fullname]
        locations = [str(self.root)] if fullname == "funasr" else None
        return spec_from_file_location(fullname, source,
                                       loader=_PreparedLoader(fullname, source, self.codes[fullname]),
                                       submodule_search_locations=locations)


def _verified_root() -> Path | None:
    try:
        if metadata.version("funasr") != _VERSION:
            return None
        spec = find_spec("funasr")
        return Path(spec.origin).parent if spec is not None and spec.origin else None
    except (metadata.PackageNotFoundError, OSError, ValueError):
        return None


def _read_verified_source(root: Path, relative: str) -> bytes | None:
    try:
        source = (root / relative).read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(source).hexdigest()
    if digest != _SOURCE_HASHES[relative]:
        log.info("FunASR source hash（源码摘要）不匹配：%s", relative)
        return None
    return source


def _ensure_full_registration(package):
    """对未知配置补跑完整递归扫描，不能因包已导入而误认为注册已完整。"""
    scan = getattr(package, "import_submodules", None)
    if callable(scan):
        scan(package, recursive=True)
        return
    # 官方不同版本若未暴露扫描函数，逐层遍历作为保守完整回退。
    import pkgutil
    for _, name, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        try:
            importlib.import_module(name)
        except Exception:
            log.debug("完整注册扫描跳过导入失败模块：%s", name, exc_info=True)


def _register_components(components: tuple[str, ...]) -> None:
    for component in components:
        _, _, module = _COMPONENTS[component]
        importlib.import_module(module)
    tables = importlib.import_module("funasr.register").tables
    for component in components:
        name, registry, module = _COMPONENTS[component]
        table = getattr(tables, registry)
        registered = table.get(name)
        if registered is None or registered.__module__ != module:
            raise ImportError("语音模型组件注册来源不匹配：" + name)


def _load_mode() -> str:
    # 固定版本的真实 CPU（中央处理器）/CUDA（显卡计算）对照已验收；原路径仍可显式回退。
    mode = os.environ.get("VOICE_MODEL_LOAD_MODE", "mmap").strip().lower()
    if mode not in {"original", "mmap"}:
        log.warning("VOICE_MODEL_LOAD_MODE（权重加载模式）未知值 %r，使用 original（原版）", mode)
        return "original"
    return mode


def _set_loaded_load_mode(mode: str, source: bytes | None) -> str:
    """在同一进程重复选择模型时同步 AutoModel 持有的加载函数引用。"""
    module_name = "funasr.train_utils.load_pretrained_model"
    module = sys.modules.get(module_name)
    if module is None:
        return "original"
    current = getattr(module, "load_pretrained_model", None)
    current_mode = getattr(current, "__voice_model_load_mode__", "original")
    if mode == current_mode:
        pass
    elif mode == "mmap" and source is not None:
        try:
            code = _pretrained_loader_code(source.decode("utf-8"), getattr(module, "__file__", module_name))
            exec(code, module.__dict__)
        except (SyntaxError, UnicodeDecodeError, ValueError, TypeError, StopIteration):
            log.exception("mmap（内存映射）加载适配失败，保留上游加载器")
    elif mode == "original" and current_mode == "mmap" and source is not None:
        try:
            exec(compile(source.decode("utf-8"), getattr(module, "__file__", module_name), "exec"),
                 module.__dict__)
        except (SyntaxError, UnicodeDecodeError):
            log.exception("恢复 original（原版）加载器失败")
    auto_model = sys.modules.get("funasr.auto.auto_model")
    if auto_model is not None and module is sys.modules.get(module_name):
        auto_model.load_pretrained_model = module.load_pretrained_model
    return getattr(module.load_pretrained_model, "__voice_model_load_mode__", "original")


def _compile_codes(root: Path, *, selective: bool, load_mode: str):
    codes = {}
    sources = {}
    if selective:
        for relative in ("__init__.py", "auto/auto_model.py"):
            source = _read_verified_source(root, relative)
            if source is None:
                return None, None, None
            sources[relative] = source
        try:
            codes["funasr"] = _entry_code(sources["__init__.py"].decode("utf-8"), str(root / "__init__.py"))
            codes["funasr.auto.auto_model"] = _model_code(
                sources["auto/auto_model.py"].decode("utf-8"), str(root / "auto/auto_model.py"))
        except (SyntaxError, UnicodeDecodeError, ValueError, StopIteration):
            log.info("识别导入 AST（抽象语法树）结构不匹配，使用完整加载", exc_info=True)
            return None, None, None
    load_source = None
    if load_mode == "mmap":
        load_source = _read_verified_source(root, "train_utils/load_pretrained_model.py")
        if load_source is not None:
            try:
                codes["funasr.train_utils.load_pretrained_model"] = _pretrained_loader_code(
                    load_source.decode("utf-8"), str(root / "train_utils/load_pretrained_model.py"))
            except (SyntaxError, UnicodeDecodeError, ValueError, StopIteration):
                log.info("权重加载器 AST 结构不匹配，使用 original（原版）torch.load", exc_info=True)
                load_source = None
        else:
            log.info("权重加载器版本或摘要不匹配，使用 original（原版）torch.load")
    return codes, load_source, sources


def auto_model_class(model_dir: Path, *, additional_model_dirs=(), family: str | None = None):
    """返回原 AutoModel；可为多个已知配置增量注册，并独立选择权重加载模式。"""
    import_mode = os.environ.get("VOICE_ASR_IMPORT_MODE", "selective").strip().lower()
    if import_mode not in {"selective", "full"}:
        log.warning("VOICE_ASR_IMPORT_MODE（识别导入模式）未知值 %r，使用 full（完整导入）", import_mode)
        import_mode = "full"
    model_dirs = (Path(model_dir), *(Path(item) for item in additional_model_dirs))
    components = None if import_mode == "full" else (("campplus_model",) if family == "campplus" else _requested_components(model_dirs))
    load_mode = _load_mode()
    with _lock:
        root = _verified_root()
        if root is None:
            log.info("FunASR 版本未知；使用完整注册扫描及 original（原版）权重加载")
            package = importlib.import_module("funasr")
            _ensure_full_registration(package)
            return package.AutoModel

        selective = import_mode != "full" and components is not None
        codes, load_source, _ = _compile_codes(root, selective=selective, load_mode=load_mode)
        if codes is None:
            selective = False
            codes, load_source, _ = _compile_codes(root, selective=False, load_mode=load_mode)
        codes = codes or {}

        package_loaded = "funasr" in sys.modules
        # 当前包已由其他代码导入时，改成在已加载模块内存中补注册；不能简单跳过。
        finder = None
        if not package_loaded and codes:
            finder = _PreparedFinder(root, codes)
            sys.meta_path.insert(0, finder)
        try:
            package = importlib.import_module("funasr")
            if components is None or import_mode == "full" or not selective:
                # 新包的官方入口已做完整扫描；仅已存在的包需要显式补扫，
                # 防止此前按需导入遗漏未知配置组件，同时避免重复全包遍历。
                if package_loaded:
                    _ensure_full_registration(package)
            else:
                try:
                    _register_components(components)
                except Exception:
                    log.exception("按需组件注册失败，补跑完整递归扫描")
                    _ensure_full_registration(package)
            # 包导入后触发 AutoModel 懒导出；Finder 仍在时可以接管加载函数模块。
            auto_model = package.AutoModel
            actual_load_mode = "original"
            if load_mode == "mmap" and load_source is not None:
                if "funasr.train_utils.load_pretrained_model" not in sys.modules:
                    importlib.import_module("funasr.train_utils.load_pretrained_model")
                actual_load_mode = _set_loaded_load_mode("mmap", load_source)
            elif load_mode == "original" and "funasr.train_utils.load_pretrained_model" in sys.modules:
                original_source = _read_verified_source(root, "train_utils/load_pretrained_model.py")
                actual_load_mode = _set_loaded_load_mode("original", original_source)
            log.info("FunASR 加载完成：import_mode（导入模式）=%s, load_mode（权重加载模式）=%s, selective（按需注册）=%s",
                     import_mode, actual_load_mode, selective and components is not None)
            return auto_model
        except Exception:
            log.exception("FunASR 按需初始化失败，尝试完整注册扫描")
            if "funasr" not in sys.modules:
                package = importlib.import_module("funasr")
            else:
                package = sys.modules["funasr"]
            _ensure_full_registration(package)
            if load_mode == "mmap" and load_source is not None:
                _set_loaded_load_mode("mmap", load_source)
            return package.AutoModel
        finally:
            if finder is not None:
                try:
                    sys.meta_path.remove(finder)
                except ValueError:
                    pass
