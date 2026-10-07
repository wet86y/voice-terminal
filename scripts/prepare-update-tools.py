"""Update tools（临时更新工具）：释放正式解释器占用，不复制计算依赖。"""
from pathlib import Path
import json
import shutil
import subprocess
import sys

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').is_file())
sys.path.insert(0, str(ROOT))
from voice_terminal.local_layout import read_portable
from voice_terminal.local_files import remove_tree
from voice_terminal.distribution import files, digest
from voice_terminal.local_update import update_lock
from voice_terminal.paths import atomic_json


def prepare():
    local = ROOT / '.local'
    selected = read_portable(local)
    source = (local / selected['python']).parent
    target = local / 'tmp/update-tools'
    with update_lock(local):
        if (local / 'tmp/update-transaction.json').exists():
            raise RuntimeError('请先用已保留的临时工具恢复更新事务')
        remove_tree(target, local)
        target.mkdir(parents=True)
        paths = [source / 'python.exe', source / 'python312.zip', *source.glob('*.dll'), *source.glob('*.pyd')]
        for path in paths:
            shutil.copy2(path, target / path.name)
        for package in ('psutil', 'yaml'):
            origin = source / 'Lib/site-packages' / package
            for path in files(origin):
                if path.suffix == '.pyc' or '__pycache__' in path.parts:
                    continue
                destination = target / 'Lib/site-packages' / path.relative_to(source / 'Lib/site-packages')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        # 临时工具只复制标准库及小型原生校验依赖；纯脚本依赖仍读取当前正式目录。
        (target / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n' + str(source / 'Lib/site-packages') + '\nimport site\n', encoding='utf-8')
        probe = 'import sys;sys.path.insert(0,' + repr(str(ROOT)) + ');from voice_terminal.dsh.profile import build_profile;from voice_terminal.local_update import install;import psutil,yaml;'
        probe += 'assert not any(n in sys.modules for n in ("torch","numpy","onnxruntime"));'
        probe += 'assert all(not (getattr(m,"__file__","") or "").lower().endswith(".pyd") or not ' + repr(str(source).lower()) + ' in m.__file__.lower() for m in list(sys.modules.values()));print("临时工具闭包校验通过")'
        subprocess.run([str(target / 'python.exe'), '-B', '-X', 'utf8', '-c', probe], check=True)
        size = sum(p.stat().st_size for p in files(target))
        atomic_json(target / 'tools.json', {'source_manifest': digest(source / 'manifest.json'), 'size': size,
                                          'fields': {'source_manifest': '工具来源运行清单摘要', 'size': '临时工具字节数'}})
    print(f'临时更新工具：{size / 1024**2:.2f} MiB（兆二进制字节），未复制大型计算依赖。')


if __name__ == '__main__':
    prepare()
