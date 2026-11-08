"""AnifLive（本地合成）模型版本登记；播放选择由产品设置唯一持有。"""
from __future__ import annotations
import json
from pathlib import Path
import re

from .aniflive_native import contained, digest, UPSTREAM_COMMIT, PATCH_REVISION, PATCHED_SHA256
from .aniflive_install import atomic_json, DEFAULT_MODEL
from .settings import valid_local_model

_MODEL_ID = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}\Z')
_REVISION = re.compile(r'[a-f0-9]{24}\Z')
BUILD_ADAPTER_FILES = (
    'voice_terminal/tts/aniflive_native.py', 'voice_terminal/tts/aniflive_environment.py',
    'voice_terminal/tts/aniflive_worker.py', 'voice_terminal/tts/aniflive_build_diagnostics.py',
    'scripts/run-aniflive.py', 'voice_terminal/tts/aniflive_aliases.py',
    'voice_terminal/tts/aniflive_shared.py', 'voice_terminal/tts/aniflive_service.py',
    'voice_terminal/tts/aniflive_export.py',
)  # BUILD_ADAPTER_FILES（构建与私有接口桥接的完整源码身份）。


def validate_build_identity(identity, program: Path):
    """按稳定推理协议判断兼容性；旧规则仍支持精确审阅的兼容组合。"""
    rules_path = program / 'config/runtime/aniflive-build-compatibility.json'
    rules = json.loads(rules_path.read_text(encoding='utf-8')) if rules_path.is_file() else {}
    contract = rules.get('runtime_contract')
    if contract is not None:
        from .aniflive_environment import RUNTIME_VERSIONS
        # 源码摘要保留为构建溯源；启动依赖稳定推理协议，避免普通工具修改强制重编。
        source = identity.get('source', {})
        if (rules.get('schema') != 1 or not isinstance(contract, dict) or contract.get('revision') != 1
                or contract.get('runtime_packages') != RUNTIME_VERSIONS
                or source.get('upstream_commit') != contract.get('upstream_commit')
                or source.get('patch_revision') not in contract.get('source_patch_revisions', [])
                or identity.get('runtime_contract', 1) != contract['revision']):
            raise ValueError('模型推理协议或上游架构不兼容，请在组件列表重新转换。')
        return
    source = identity.get('source', {})
    if (identity.get('dependency_lock') != digest(program / 'config/runtime/aniflive-runtime.lock.json')
            or identity.get('shared_lock') != digest(program / 'config/runtime/aniflive-shared-assets.lock.json')
            or source.get('upstream_commit') != UPSTREAM_COMMIT):
        raise ValueError('模型构建的补丁或依赖身份已变化，请在组件列表重新转换。')
    adapters = identity.get('adapter')
    required = set(BUILD_ADAPTER_FILES)
    if reviewed_export_compatibility(identity, program):
        return
    if (source.get('patch_revision') != PATCH_REVISION
            or {row['path']: row['after_sha256'] for row in source.get('files', [])} != PATCHED_SHA256):
        raise ValueError('模型构建的补丁身份已变化，且未经过运行兼容审阅，请在组件列表重新转换。')
    if not isinstance(adapters, dict) or set(adapters) != required:
        raise ValueError('模型缺少完整桥接实现身份，请重新转换。')
    if any(digest(contained(program, program / name)) != expected for name, expected in adapters.items()):
        raise ValueError('模型桥接实现身份已变化，请在组件列表重新转换。')


def reviewed_export_compatibility(identity, program: Path) -> bool:
    """收据仍保持真实旧身份，不将旧引擎改写成新补丁构建。"""
    path = program/'config/runtime/aniflive-build-compatibility.json'
    if not path.is_file():
        return False
    rules = json.loads(path.read_text(encoding='utf-8'))
    if rules.get('schema') != 1 or not isinstance(rules.get('profiles'), list):
        return False
    for profile in rules['profiles']:
        if (not isinstance(profile, dict) or profile.get('source') != identity.get('source')
                or profile.get('adapter') != identity.get('adapter')):
            continue
        runtime = profile.get('runtime_adapter')
        if not isinstance(runtime, dict) or set(runtime) != set(BUILD_ADAPTER_FILES):
            continue
        if all(digest(contained(program, program/name)) == expected for name, expected in runtime.items()):
            return True
    return False


def validate_model_id(value):
    if not valid_local_model(value):
        raise ValueError('本地模型编号必须为安全的英文小写标识。')
    return value


def validate_registered_package(local: Path, package: Path) -> dict:
    """受管推理启动的轻量校验；显式维修及外部导入继续执行完整内容检查。"""
    package = contained(local, package)
    relative = package.relative_to(Path(local) / 'models/aniflive-tts/packages')
    if len(relative.parts) != 2:
        raise ValueError('启动模型必须为受管的精确模型版本目录。')
    model, revision = relative.parts
    actual, _, _ = AnifLiveModelRegistry(local).resolve(model, revision)
    if actual != package:
        raise ValueError('启动模型目录与登记不一致。')
    checksums = json.loads(contained(package, package / 'checksums.json').read_text(encoding='utf-8'))
    receipt = json.loads((package / 'application-receipt.json').read_text(encoding='utf-8'))
    expected = {row['path']: row['sha256'] for row in receipt['files']}
    expected['application-receipt.json'] = digest(package / 'application-receipt.json')
    if checksums != expected:
        raise ValueError('模型摘要清单与实际验证收据不一致，请执行完整性校验。')
    return checksums


class AnifLiveModelRegistry:
    def __init__(self, local: Path, data: Path | None = None):
        self.local = Path(local).absolute()
        self.data = Path(data).absolute() if data else self.local / 'data'
        contained(self.local, self.data)
        self.path = contained(self.local, self.data / 'config/aniflive-models.json')

    def read(self):
        contained(self.local, self.path)
        if not self.path.exists():
            return {'schema': 1, 'models': {}}
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if value.get('schema') != 1 or not isinstance(value.get('models'), dict):
            raise ValueError('AnifLive 模型登记格式无效，原件已保留。')
        return value

    def resolve(self, model_id: str, revision: str | None = None, *, verify_hashes=False):
        validate_model_id(model_id)
        entry = self.read()['models'].get(model_id)
        if not isinstance(entry, dict):
            raise ValueError('所选 AnifLive 模型尚未安装并验证。')
        revision = revision or entry['current_revision']
        if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
            raise ValueError('模型版本编号无效。')
        version = entry['versions'].get(revision)
        if not isinstance(version, dict) or version.get('state') != 'validated':
            raise ValueError('模型版本尚未通过本机验证。')
        package = contained(self.local, self.local / 'models/aniflive-tts/packages' / model_id / revision)
        manifest = json.loads(contained(package, package / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('model_id') != model_id or manifest.get('format') != 'aniflive-tts-model-package':
            raise ValueError('模型包与登记身份不匹配。')
        receipt = json.loads(contained(package, package / 'application-receipt.json').read_text(encoding='utf-8'))
        validation = receipt.get('validation', {})
        if (receipt.get('identity') != version['identity'] or validation.get('enqueue_verified') is not True
                or validation.get('engine_count') != 9 or receipt.get('streaming_verified') is not True):
            raise ValueError('模型缺少完整的本机合成验证收据。')
        validate_build_identity(version['identity'], Path(__file__).resolve().parents[2])
        rules = json.loads((Path(__file__).resolve().parents[2] / 'config/runtime/aniflive-build-compatibility.json').read_text(encoding='utf-8'))
        contract = rules.get('runtime_contract')
        if contract and (manifest.get('model_family') != contract['model_family'] or manifest.get('backend') != contract['backend']):
            raise ValueError('模型计算架构或推理后端不兼容。')
        files = receipt.get('files')
        if not isinstance(files, list) or not files:
            raise ValueError('模型缺少实际生成文件清单。')
        seen = set()
        for row in files:
            relative = row.get('path')
            if not isinstance(relative, str) or relative in seen:
                raise ValueError('模型生成文件清单无效。')
            seen.add(relative)
            target = contained(package, package / relative)
            if not target.is_file() or target.stat().st_size != row.get('size'):
                raise ValueError('模型生成文件缺失或大小不符。')
            if not isinstance(row.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
                raise ValueError('模型生成文件摘要无效。')
            if verify_hashes and digest(target) != row['sha256']:
                raise ValueError('模型生成文件摘要不符。')
        if not contained(package, package / 'checksums.json').is_file():
            raise ValueError('模型包缺少最终摘要文件。')
        # 普通启动检查登记文件属性；主动完整性校验才遍历并检查额外文件。
        if verify_hashes:
            actual = {path.relative_to(package).as_posix() for path in package.rglob('*') if path.is_file()}
            if actual != seen | {'checksums.json', 'application-receipt.json'}:
                raise ValueError('模型包存在未登记文件或缺少最终摘要文件。')
        return package, revision, version

    def register(self, model_id, revision, name, identity, *, persist=True):
        validate_model_id(model_id)
        if not _REVISION.fullmatch(revision):
            raise ValueError('模型版本编号无效。')
        value = self.read()
        entry = value['models'].setdefault(model_id, {'name': name, 'versions': {}})
        previous = entry['versions'].get(revision)
        if previous is not None and previous.get('identity') != identity:
            raise ValueError('模型版本身份冲突，保留原登记。')
        entry['versions'][revision] = {'state': 'validated', 'identity': identity}
        entry['current_revision'] = revision
        entry['name'] = str(name)[:80]
        value['fields'] = {'schema': '模型登记格式', 'models': '已登记模型', 'name': '显示名称',
                           'versions': '不可变模型版本', 'current_revision': '新选择采用的已验证版本',
                           'state': '本机验证状态', 'identity': '原始输入及完整构建身份'}
        if persist:
            atomic_json(self.path, value)
        return value

    def models(self):
        defaults = {DEFAULT_MODEL: '中文少女声（通用基底）', 'tangyuge-v2pp-d78cf598': '唐语歌（中文微调）'}
        registered = self.read()['models']
        defaults.update({key: row.get('name', key) for key, row in registered.items()})
        rows = []
        for model_id, name in defaults.items():
            available, revision = False, None
            try:
                _, revision, _ = self.resolve(model_id)
                available = True
            except (ValueError, KeyError, OSError, TypeError):
                pass
            rows.append({'id': model_id, 'name': name, 'available': available, 'revision': revision,
                         'loaded': False, 'native_sample_rate': 32000})
        return rows
