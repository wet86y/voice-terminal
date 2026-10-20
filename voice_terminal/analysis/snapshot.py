"""固定模型快照校验：本地目录只能加载随包清单登记的文件。"""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=48)
def _digest(path: str, size: int, modified: int) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_snapshot(directory: Path, resource_id: str) -> dict:
    from ..local_layout import project_root
    from ..resource_distribution import ResourceCatalog
    spec = ResourceCatalog(project_root()).get(resource_id)
    for record in spec.files:
        if record.optional:
            continue
        path = directory / record.path
        if not path.is_file():
            raise FileNotFoundError(f"{spec.title} 缺少固定文件：{path.name}")
        stat = path.stat()
        if stat.st_size != record.size_bytes or _digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns) != record.sha256:
            raise ValueError(f"{spec.title} 文件摘要不符：{path.name}")
    return {"resource_id": resource_id, "source": spec.source["repo_id"], "revision": spec.version}
