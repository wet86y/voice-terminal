"""共享语言输入通过正式任务下载；暂停服务后离线启用并核对固定目录摘要。"""
from __future__ import annotations
import json
from pathlib import Path, PurePosixPath
import os
import runpy
import shutil
import tempfile
import zipfile
from .aniflive_native import contained, digest, PATCHED_SHA256, UPSTREAM_COMMIT


def specification(program: Path, local: Path):
    source = contained(local, local / 'runtimes/aniflive-tts/source/scripts/setup_shared_assets.py')
    if digest(source) != PATCHED_SHA256['scripts/setup_shared_assets.py']:
        raise ValueError('共享资源准备源码与已审阅补丁不符。')
    upstream = runpy.run_path(str(source))
    lock = json.loads((program / 'config/runtime/aniflive-shared-assets.lock.json').read_text(encoding='utf-8'))
    expected = dict(upstream['GPT_SOVITS_SHA256'])
    expected['fast_langdetect/lid.176.bin'] = upstream['FASTTEXT_SHA256']
    expected.update({f'nltk_data/{row["subdir"]}/{name}.zip': row['zip_sha256']
                     for name, row in upstream['NLTK_PACKAGES'].items()})
    if (lock.get('upstream_commit') != UPSTREAM_COMMIT or len(lock['files']) != len(expected)
            or {row['path']: row['sha256'] for row in lock['files']} != expected
            or lock['repository'] != upstream['GPT_SOVITS_REPO'] or lock['revision'] != upstream['GPT_SOVITS_REVISION']):
        raise ValueError('共享输入锁与固定官方来源不一致。')
    return lock, upstream


def download_inputs(program: Path, local: Path):
    from .aniflive_install import artifact, emit
    lock, _ = specification(program, local)
    inputs = []
    emit('核对并下载固定共享输入')
    for row in lock['files']:
        target = contained(local, local / 'models/aniflive-tts/shared' / row['path'])
        if not target.is_file() or target.stat().st_size != row['size'] or digest(target) != row['sha256']:
            url = row.get('url') or f'https://huggingface.co/{lock["repository"]}/resolve/{lock["revision"]}/{row["path"]}'
            target = artifact({**row, 'url': url}, contained(local, local / 'cache/aniflive-tts/shared-inputs' / row['path']), local=local, program=program)
        inputs.append((row, target))
    return inputs


def activate(program: Path, local: Path, inputs):
    from .aniflive_install import atomic_json, emit
    lock, upstream = specification(program, local)
    root = contained(local, local / 'models/aniflive-tts/shared')
    for row, source in inputs:
        contained(local, source)
        target = contained(local, root / row['path'])
        if source.stat().st_size != row['size'] or digest(source) != row['sha256']:
            raise ValueError('共享输入在启用前发生变化。')
        if source != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
    trees = {}
    for name, metadata in upstream['NLTK_PACKAGES'].items():
        target = contained(local, root / 'nltk_data' / metadata['subdir'] / name)
        expected = metadata['tree_sha256']
        if not target.is_dir() or upstream['_tree_digest'](target) != expected:
            emit('离线准备语言词典：' + name)
            temporary = contained(local, local / 'tmp/aniflive-shared')
            temporary.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='nltk-', dir=temporary))
            try:
                archive = target.with_suffix('.zip')
                with zipfile.ZipFile(archive) as bundle:
                    rows = bundle.infolist()
                    if len(rows) > 4096 or sum(row.file_size for row in rows) > 128 * 1024**2:
                        raise ValueError('语言词典归档超出允许范围。')
                    for row in rows:
                        parts = PurePosixPath(row.filename).parts
                        if (not parts or parts[0] != name or any(part in {'.', '..'} or ':' in part or '\\' in part for part in parts)
                                or (row.external_attr >> 16) & 0o170000 == 0o120000):
                            raise ValueError('语言词典归档包含非法路径。')
                        output = contained(stage, stage.joinpath(*parts))
                        if row.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with bundle.open(row) as stream, output.open('xb') as file:
                                shutil.copyfileobj(stream, file)
                candidate = stage / name
                if upstream['_tree_digest'](candidate) != expected:
                    raise ValueError('语言词典解包后的目录摘要不符。')
                # 已匹配的现用树不会进入此分支；无效树直到完整候选验证后才替换。
                if target.exists():
                    shutil.rmtree(contained(local, target))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(candidate, target)
            finally:
                shutil.rmtree(contained(temporary, stage))
        trees[name] = expected
    files = {row['path']: {'bytes': row['size'], 'sha256': row['sha256']} for row in lock['files'] if not row['path'].endswith('.zip')}
    atomic_json(root / 'shared-assets.json', {'schema': 1,
        'gpt_sovits': {'repo': lock['repository'], 'revision': lock['revision']},
        'asset_lock_sha256': upstream['_digest'](upstream['ASSET_LOCK_PATH']), 'files': files,
        'nltk_trees': trees, 'licenses': 'See THIRD_PARTY_NOTICES.md and licenses/（第三方许可说明）',
        'fields': {'files': '固定共享模型文件身份', 'nltk_trees': '离线词典目录摘要', 'licenses': '第三方许可说明'}})
    return {'shared': str(root), 'files': len(inputs), 'nltk_trees': trees}
