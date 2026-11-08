"""转换进度依据已完成导出阶段及实际引擎，不将日志中的开始当作完成。"""
from voice_terminal.tts.aniflive_install import conversion_progress
import json


def test_export_and_precision_steps_are_reported():
    log = "[AnifLive 分阶段导出] 启动独立阶段 {'stage': 'bert', 'memory': (1, 2)}\n"
    assert conversion_progress(log, set()) == ('构建模型：已导出 0 / 5 项计算图，正在导出文本表征', 0, 15)
    log += "[AnifLive 分阶段导出] 阶段完成 {'stage': 'bert'}\n" * 2
    assert conversion_progress(log, set())[1] == 1
    assert conversion_progress(log + 'onnx_to_fp16.py', set())[1:] == (5, 15)


def test_engine_build_does_not_count_current_engine_until_written():
    log = '[converter] Building TensorRT 11 engine: ssl\n[converter] Building TensorRT 11 engine: bert\n'
    phase, done, total = conversion_progress(log, {'ssl', 'unrelated'})
    assert '1 / 9' in phase and '正在构建 bert' in phase
    assert (done, total) == (7, 15)
    assert conversion_progress(log, {'ssl', 'bert'})[1:] == (7, 15)  # 当前文件仍可能在写入。
    assert conversion_progress(log, {'ssl', 'bert'}, complete=True)[1:] == (8, 15)


def test_ordinary_conversion_reuses_matching_dependency_receipts_without_hashing(tmp_path, monkeypatch):
    from voice_terminal.tts import aniflive_install as install
    task = install.AnifLiveInstallTask.__new__(install.AnifLiveInstallTask)
    task.local = tmp_path
    task.runtime = tmp_path / 'runtimes/aniflive-tts'
    task.lock_sha = 'new formatting only（仅锁文件排版变化）'
    task.lock = {'wheels': [], 'runtime': {'torch': '1', 'transformers': '2'},
                 'export': {'torch': '1', 'transformers': '3'}}
    for role, folder, packages in [('runtime', 'packages', task.lock['runtime']),
                                   ('export', 'export-packages', {'transformers': '3'})]:
        path = task.runtime / folder
        path.mkdir(parents=True)
        (path / 'aniflive-packages.json').write_text(json.dumps({'schema': 1, 'role': role,
            'lock_sha256': 'old formatting（旧锁文件排版）', 'packages': packages,
            'files': [{'path': 'large.bin', 'size': 9 * 1024**3, 'sha256': 'a' * 64}]}))
    monkeypatch.setattr(install, 'digest', lambda path: (_ for _ in ()).throw(AssertionError('普通任务不应读取依赖完整摘要')))
    task.prepare_dependencies()
