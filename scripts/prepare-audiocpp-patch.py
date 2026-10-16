"""准备 audio.cpp（原生语音框架）官方基础包与随本体分发的覆盖补丁，客户机无需编译。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NATIVE_REVISION = "b4459b3bfa8d6cf4c579cc264dd3f4dfb57dd8ec"
SERVER_SHA256 = "4082389c40c0208dbafe4468370c30e0be5057e729b7188c82a784a2d97c2fcb"
OFFICIAL = {
    "binary": ("audio-v0.9.1-bin-windows-x64-cuda13.3.zip", "dd04894fae8bb0d1365b4f62407fb51550558a6478f1ef719cad7aefc00720de"),
    "cudart": ("audio-v0.9.1-cudart-windows-x64-cuda13.3.zip", "fa0fb817540c578bad085c7befb9d25b5a7634548e47955bff7b732b04b8a84b"),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(runtime: Path, cache: Path, source_repo: Path) -> dict:
    """prepare（准备）：固定来源、补丁与最终文件白名单，拒绝来源不符的发布资源。"""
    binary = runtime / "runtimes/audiocpp/cuda/audiocpp_server.exe"
    if digest(binary) != SERVER_SHA256:
        raise ValueError("本地语音服务程序不匹配已验证修订，拒绝生成补丁。")
    revision = subprocess.run(["git", "-C", str(source_repo), "rev-parse", NATIVE_REVISION],
                              check=True, capture_output=True, text=True).stdout.strip()
    if revision != NATIVE_REVISION:
        raise ValueError("原生补丁源码修订不匹配。")
    source_patch = subprocess.run(["git", "-C", str(source_repo), "diff", "--binary", "v0.9.1", revision],
                                 check=True, capture_output=True).stdout
    directory = ROOT / "config/resources/assets/audiocpp"
    directory.mkdir(parents=True, exist_ok=True)
    patch_path = directory / "voxcpm-patch.zip"
    candidate = patch_path.with_suffix(".pending.zip")
    # model_specs（模型规格）来自固定源码提交，避免打包另一个实验中已变动的本机副本。
    patch_files = {"cuda/audiocpp_server.exe": binary.read_bytes(),
                   **{f"model_specs/{name}.json": subprocess.run(
                       ["git", "-C", str(source_repo), "show", f"{revision}:model_specs/{name}.json"],
                       check=True, capture_output=True).stdout for name in ("voxcpm1", "voxcpm2")}}
    records = []
    with zipfile.ZipFile(candidate, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative, content in patch_files.items():
            # 固定时间戳使同一输入的补丁压缩包可复现。
            info = zipfile.ZipInfo(relative, date_time=(2026, 10, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
            records.append({"path": relative, "source_path": relative, "archive_id": "patch",
                            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        info = zipfile.ZipInfo("source.patch", date_time=(2026, 10, 14, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, source_patch)
    candidate.replace(patch_path)
    artifacts = []
    names = {row["path"] for row in records}
    for archive_id, (filename, expected) in OFFICIAL.items():
        path = cache / filename
        if not path.is_file() or digest(path) != expected:
            path = cache / filename.replace(".zip", ".verified.zip")
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"官方归档完整摘要不匹配：{filename}")
        artifacts.append({"id": archive_id, "filename": filename, "size_bytes": path.stat().st_size,
                          "sha256": expected, "url": f"https://github.com/0xShug0/audio.cpp/releases/download/v0.9.1/{filename}"})
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                name = Path(entry.filename).name
                if entry.is_dir() or not (name.lower().endswith(".dll") or name == "LICENSE"):
                    continue
                relative = f"cuda/{name}"
                if relative in names:
                    raise ValueError(f"官方归档包含重名运行文件：{relative}")
                names.add(relative)
                with archive.open(entry) as stream:
                    expected = hashlib.file_digest(stream, "sha256").hexdigest()
                records.append({"path": relative, "source_path": entry.filename, "archive_id": archive_id,
                                "size_bytes": entry.file_size, "sha256": expected})
    spec = {"resource_id": "tts-audiocpp-runtime", "kind": "model", "title": "audio.cpp（本地语音共用运行程序）",
            "destination": "audiocpp/runtime", "version": f"v0.9.1-{NATIVE_REVISION[:12]}", "required": False,
            "source": {"platform": "github", "repo_id": "0xShug0/audio.cpp", "revision": "v0.9.1",
                       "page_url": "https://github.com/0xShug0/audio.cpp/releases/tag/v0.9.1",
                       "license_url": "https://github.com/0xShug0/audio.cpp/blob/v0.9.1/LICENSE",
                       "native_revision": NATIVE_REVISION,
                       "transfer": {"kind": "official_audiocpp_patch", "artifacts": artifacts,
                                    "patch": {"asset": "assets/audiocpp/voxcpm-patch.zip", "size_bytes": patch_path.stat().st_size,
                                              "sha256": digest(patch_path)}}},
            "files": sorted(records, key=lambda row: row["path"])}
    catalog_path = ROOT / "config/resources/catalog.json"
    catalog = json.loads(catalog_path.read_text("utf-8"))
    catalog["resources"] = [row for row in catalog["resources"] if row["resource_id"] != spec["resource_id"]]
    catalog["resources"].insert(-1, spec)
    catalog["fields"].update({"artifacts": "锁定的官方基础归档及配套计算运行库", "id": "归档编号",
                              "archive_id": "文件所属官方归档或随包补丁编号", "patch": "随本体提供的覆盖补丁及其完整摘要",
                              "native_revision": "覆盖补丁对应的原生源码修订；源码差异保存在补丁包 source.patch（源码补丁）"})
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"patch_bytes": patch_path.stat().st_size, "official_download_bytes": sum(row["size_bytes"] for row in artifacts),
            "installed_bytes": sum(row["size_bytes"] for row in records), "files": len(records),
            "fields": {"patch_bytes": "随本体携带的补丁字节数", "official_download_bytes": "官方归档最大下载字节数",
                       "installed_bytes": "安装后完整文件字节数", "files": "最终运行文件数量"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / ".local", help="runtime（经验证的本机资源目录）")
    parser.add_argument("--cache", type=Path, default=ROOT / ".local/cache/audiocpp-official", help="cache（官方归档缓存）")
    parser.add_argument("--source-repo", type=Path, required=True, help="source-repo（包含锁定原生修订的源码仓库）")
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime, args.cache, args.source_repo), ensure_ascii=False, indent=2))
