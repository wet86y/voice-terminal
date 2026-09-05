from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .kws.keywords import generate_keywords, write_raw_keywords


KWS_ARCHIVE_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2"
)
PARAFORMER_MODEL_ID = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online"
VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_atomic(url: str, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    if temporary.exists():
        temporary.unlink()
    request = urllib.request.Request(url, headers={"User-Agent": "voice-terminal/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
    temporary.replace(destination)
    return _sha256(destination)


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, mode="r:bz2") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"模型压缩包包含越界路径：{member.name}")
        tar.extractall(destination)


def _install_kws(model_dir: Path) -> dict[str, Any]:
    target = model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M"
    archive = model_dir / "cache" / "sherpa" / "kws-model.tar.bz2"
    required = ["tokens.txt"]
    if target.is_dir() and all((target / name).is_file() for name in required) and list(target.glob("encoder*.onnx")):
        archive_sha = _sha256(archive) if archive.is_file() else None
    else:
        if target.exists():
            raise RuntimeError(f"KWS 模型目录不完整，请人工移走后重试：{target}")
        archive_sha = _download_atomic(KWS_ARCHIVE_URL, archive)
        with tempfile.TemporaryDirectory(prefix="voice-terminal-kws-", dir=model_dir / "cache") as temporary_name:
            temporary = Path(temporary_name)
            _safe_extract(archive, temporary)
            roots = [path for path in temporary.rglob("tokens.txt") if path.is_file()]
            if not roots:
                raise RuntimeError("KWS 压缩包中未找到 tokens.txt")
            extracted_root = roots[0].parent
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted_root.rename(target)

    raw_path = target / "keywords_raw.txt"
    output_path = target / "keywords.txt"
    write_raw_keywords(raw_path, "hei claw", "HEI_CLAW")
    generate_keywords(
        model_dir=target,
        raw_path=raw_path,
        output_path=output_path,
        keyword="hei claw",
        label="HEI_CLAW",
    )
    return {
        "name": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
        "kind": "kws",
        "source": KWS_ARCHIVE_URL,
        "path": str(target),
        "archive_sha256": archive_sha,
        "files": _file_manifest(target),
    }


def _install_modelscope(model_dir: Path, model_id: str, target: Path, name: str, kind: str) -> dict[str, Any]:
    if target.is_dir() and _is_funasr_model_complete(target):
        return {
            "name": name,
            "kind": kind,
            "source": f"ModelScope: {model_id}",
            "path": str(target),
            "files": _file_manifest(target),
        }
    if target.exists():
        raise RuntimeError(f"模型目录不完整，请人工移走后重试：{target}")
    try:
        from modelscope import snapshot_download  # type: ignore
    except ImportError as exc:
        raise RuntimeError("缺少 modelscope，请先运行 scripts\\setup.ps1。") from exc

    cache_dir = model_dir / "cache" / "modelscope"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MODELSCOPE_CACHE"] = str(cache_dir)
    downloaded = snapshot_download(model_id, cache_dir=str(cache_dir))
    downloaded_path = Path(downloaded)
    if not downloaded_path.is_dir():
        raise RuntimeError(f"ModelScope 下载路径无效：{downloaded_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(target.name + ".part")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(downloaded_path, staging)
    staging.rename(target)
    if not _is_funasr_model_complete(target):
        raise RuntimeError(f"下载完成但模型文件不完整：{target}")
    return {
        "name": name,
        "kind": kind,
        "source": f"ModelScope: {model_id}",
        "path": str(target),
        "files": _file_manifest(target),
    }


def _is_funasr_model_complete(path: Path) -> bool:
    if not (path / "config.yaml").is_file():
        return False
    model_files = list(path.rglob("*.pt")) + list(path.rglob("*.bin")) + list(path.rglob("*.onnx"))
    return bool(model_files)


def _file_manifest(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        result.append(
            {
                "relative_path": str(file.relative_to(path)),
                "size": file.stat().st_size,
                "sha256": _sha256(file),
            }
        )
    return result


def validate_models(model_dir: Path) -> list[str]:
    errors: list[str] = []
    kws = model_dir / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M"
    if (
        not kws.is_dir()
        or not (kws / "tokens.txt").is_file()
        or not list(kws.glob("encoder*.onnx"))
        or not list(kws.glob("decoder*.onnx"))
        or not list(kws.glob("joiner*.onnx"))
    ):
        errors.append(f"KWS 模型不完整：{kws}")
    if not _is_funasr_model_complete(model_dir / "funasr" / "paraformer-zh-streaming"):
        errors.append(f"Paraformer 模型不完整：{model_dir / 'funasr' / 'paraformer-zh-streaming'}")
    if not _is_funasr_model_complete(model_dir / "funasr" / "fsmn-vad"):
        errors.append(f"FSMN-VAD 模型不完整：{model_dir / 'funasr' / 'fsmn-vad'}")
    keywords = kws / "keywords.txt"
    if not keywords.is_file() or "HEI_CLAW" not in keywords.read_text(encoding="utf-8", errors="ignore"):
        errors.append(f"KWS 关键词文件缺少 HEI_CLAW：{keywords}")
    return errors


def write_manifest(model_dir: Path, entries: list[dict[str, Any]]) -> Path:
    manifest_dir = model_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_dir": str(model_dir),
        "wake_keyword": "hei claw",
        "entries": entries,
    }
    path = manifest_dir / "model-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and validate Voice Terminal models")
    parser.add_argument("--model-dir", type=Path, default=Path(r"D:\模型\voice-terminal"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    model_dir = args.model_dir.resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    for name in ("kws", "funasr", "cache", "manifests"):
        (model_dir / name).mkdir(parents=True, exist_ok=True)

    if args.offline:
        errors = validate_models(model_dir)
        if errors:
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print(f"PASS: local models are ready under {model_dir}")
        return 0

    entries = [
        _install_kws(model_dir),
        _install_modelscope(
            model_dir,
            PARAFORMER_MODEL_ID,
            model_dir / "funasr" / "paraformer-zh-streaming",
            "paraformer-zh-streaming",
            "asr",
        ),
        _install_modelscope(
            model_dir,
            VAD_MODEL_ID,
            model_dir / "funasr" / "fsmn-vad",
            "fsmn-vad",
            "vad",
        ),
    ]
    errors = validate_models(model_dir)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: downloaded and validated {len(entries)} models")
    print(f"MANIFEST: {write_manifest(model_dir, entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
