"""AnifLive（本地语音）任务开始前的受管路径写入检查。"""
from pathlib import Path
from uuid import uuid4


def require_writable_workspace(local: Path, data: Path) -> None:
    """在下载前验证实际输出目录；不请求提升权限，不修改现有文件。"""
    directories = [local / 'cache/aniflive-tts', local / 'tmp', local / 'models/aniflive-tts',
                   local / 'runtimes/aniflive-tts', data / 'config', data / 'state', data / 'tts/aniflive']
    release = local / 'app/release.json'
    if release.exists():
        directories.append(release.parent)
    for directory in directories:
        probe = directory / ('.aniflive-write-' + uuid4().hex)
        try:
            for parent in (directory, *directory.parents):
                if parent.is_symlink() or (getattr(parent, 'is_junction', lambda: False)()):
                    raise ValueError(f'模型任务目录不能经过链接：{directory}')
            directory.mkdir(parents=True, exist_ok=True)
            with probe.open('xb') as stream:
                stream.write(b'workspace write check')
            renamed = probe.with_suffix('.renamed')
            probe.rename(renamed)
            renamed.unlink()
        except OSError as exc:
            raise ValueError(f'模型任务无法写入或替换目录中的文件：{directory}。请将程序安装到当前用户可写的目录后重试。') from exc
        finally:
            probe.unlink(missing_ok=True)
    if release.exists():
        try:
            # 以可写方式打开，但不写入；兼顾只读属性及文件访问权限。
            with release.open('r+b'):
                pass
        except OSError as exc:
            raise ValueError(f'模型登记无法更新程序清单：{release}。请检查当前用户的写入权限。') from exc
