"""准备隔离的内存优化候选资产，不修改原始模型文件。

ONNX（开放神经网络交换格式）模型和复制的分词器文件均固定版本并校验哈希。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_ROOT = ROOT / ".local/cache/memory-optimization-20261010/models"
SENSEVOICE_SOURCE = ROOT / ".local/models/analysis/sensevoice"
SENSEVOICE_REVISION = "4e95991ddb34c70e9b94f026d62ac82a9d941ae1"
SENSEVOICE_BASE_REVISION = "3847d57b6bdf2dd8875cb1508d2af43d80a16bf7"
SENSEVOICE_FILES = {
    "model_quant.onnx": (241216270, "21dc965f689a78d1604717bf561e40d5a236087c85a95584567835750549e822"),
    "config.yaml": (1855, "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19"),
    "am.mvn": (11203, "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5"),
    "tokens.json": (352064, "a2594fc1474e78973149cba8cd1f603ebed8c39c7decb470631f66e70ce58e97"),
    "configuration.json": (56, "c57f6a580d63f7465c6a22ba95847aee05a1ae1181f5abddffb943d9febda061"),
    "README.md": (4938, "f7442f8542b0d1ee38d85f1ff10db5fc1677d97bac5aa4172e3c17e1557d28bd"),
}
SENSEVOICE_BPE = (
    "chn_jpn_yue_eng_ko_spectok.bpe.model",
    377341,
    "aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8",
)
VAD_SOURCE = ROOT / ".local/models/funasr/fsmn-vad"
VAD_REVISION = "v2.0.4"
VAD_FILES = {
    "model.pt": (1721366, "b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5"),
    "config.yaml": (1215, "486861ca26ddb79081663b6179cb204c6bfae71c52f04aafc48a9e9d8dde1e93"),
    "am.mvn": (8040, "6820fef9687708c4fc3fab2530179c8fcea6262daa25514380056cd8f6eb1754"),
    "configuration.json": (365, "7bce8867e37d55c3dd8f672695ced18077a2be199ea529a5d432d5350fc0acba"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(path: Path, size: int, expected: str) -> None:
    actual_size = path.stat().st_size
    actual_hash = sha256(path)
    if actual_size != size or actual_hash != expected:
        raise ValueError(
            f"资产校验失败：{path.name}，大小 {actual_size}/{size}，SHA-256 {actual_hash}/{expected}"
        )


def copy_verified(source: Path, destination: Path, size: int, expected: str) -> None:
    verify(source, size, expected)
    shutil.copy2(source, destination)
    verify(destination, size, expected)


def write_provenance(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_vad_candidate() -> Path:
    destination = CANDIDATE_ROOT / "fsmn-vad"
    if destination.exists():
        raise FileExistsError(f"候选目录已存在，拒绝覆盖：{destination}")
    CANDIDATE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fsmn-vad-candidate-", dir=CANDIDATE_ROOT) as temp:
        staging = Path(temp) / "fsmn-vad"
        staging.mkdir()
        files = {}
        for name, (size, expected) in VAD_FILES.items():
            copy_verified(VAD_SOURCE / name, staging / name, size, expected)
            files[name] = {
                "size_bytes": size,
                "size_bytes_zh": "该候选文件的字节数",
                "sha256": expected,
                "sha256_zh": "该候选文件的 SHA-256（安全散列）校验值",
                "source_path": str(VAD_SOURCE / name),
                "source_path_zh": "本地原始模型文件路径，仅复制读取",
            }
        write_provenance(staging / "candidate-provenance.json", {
            "schema_version": 1,
            "schema_version_zh": "候选来源清单格式版本",
            "source_repository": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "source_repository_zh": "FSMN-VAD（前馈序列记忆语音活动检测）源模型仓库",
            "source_revision": VAD_REVISION,
            "source_revision_zh": "固定源模型版本",
            "files": files,
            "files_zh": "复制到隔离候选目录的源文件及逐文件校验信息",
            "note_zh": "model.pt 原文件未修改；ONNX 导出将在此候选目录内生成。",
        })
        final_staging = Path(temp) / "commit"
        os.replace(staging, final_staging)
        os.replace(final_staging, destination)
    return destination


def download_verified(name: str, size: int, expected: str, destination: Path) -> str:
    url = (
        "https://modelscope.cn/api/v1/models/iic/SenseVoiceSmall-onnx/repo?"
        f"Revision={SENSEVOICE_REVISION}&FilePath={name}"
    )
    temporary = destination.with_suffix(destination.suffix + ".download")
    digest = hashlib.sha256()
    actual_size = 0
    request = Request(url, headers={"User-Agent": "voice-terminal-memory-candidate/1.0"})
    try:
        with urlopen(request, timeout=60) as response, temporary.open("wb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
                digest.update(block)
                actual_size += len(block)
        actual_hash = digest.hexdigest()
        if actual_size != size or actual_hash != expected:
            raise ValueError(
                f"下载资产校验失败：{name}，大小 {actual_size}/{size}，SHA-256 {actual_hash}/{expected}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return url


def prepare_sensevoice_candidate() -> Path:
    destination = CANDIDATE_ROOT / "sensevoice-onnx"
    if destination.exists():
        raise FileExistsError(f"候选目录已存在，拒绝覆盖：{destination}")
    source_manifest_path = SENSEVOICE_SOURCE / "manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest.get("revision") != SENSEVOICE_BASE_REVISION:
        raise ValueError("本地 SenseVoice BPE（分词器）修订与预期固定版本不符，拒绝复制")
    bpe_name, bpe_size, bpe_hash = SENSEVOICE_BPE
    if source_manifest.get("sha256", {}).get(bpe_name) != bpe_hash:
        raise ValueError("本地 SenseVoice BPE（分词器）清单哈希不符，拒绝复制")

    CANDIDATE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sensevoice-onnx-candidate-", dir=CANDIDATE_ROOT) as temp:
        staging = Path(temp) / "sensevoice-onnx"
        staging.mkdir()
        files = {}
        for name, (size, expected) in SENSEVOICE_FILES.items():
            url = download_verified(name, size, expected, staging / name)
            files[name] = {
                "size_bytes": size,
                "size_bytes_zh": "该候选文件的字节数",
                "sha256": expected,
                "sha256_zh": "该候选文件的 SHA-256（安全散列）校验值",
                "source_url": url,
                "source_url_zh": "含固定仓库修订号的官方下载来源地址",
                "source_revision": SENSEVOICE_REVISION,
                "source_revision_zh": "获取此文件时使用的官方 ONNX 仓库快照提交",
            }
        copy_verified(SENSEVOICE_SOURCE / bpe_name, staging / bpe_name, bpe_size, bpe_hash)
        files[bpe_name] = {
            "size_bytes": bpe_size,
            "size_bytes_zh": "该候选文件的字节数",
            "sha256": bpe_hash,
            "sha256_zh": "该候选文件的 SHA-256（安全散列）校验值",
            "source_repository": "FunAudioLLM/SenseVoiceSmall",
            "source_repository_zh": "原始 SenseVoiceSmall（语音理解小型模型）仓库",
            "source_revision": SENSEVOICE_BASE_REVISION,
            "source_revision_zh": "项目已固定的源模型提交；仅说明复制的 BPE 文件来源",
            "source_path": str(SENSEVOICE_SOURCE / bpe_name),
            "source_path_zh": "复制读取的本地 BPE（分词器）文件路径",
        }
        write_provenance(staging / "candidate-provenance.json", {
            "schema_version": 1,
            "schema_version_zh": "候选来源清单格式版本",
            "onnx_repository": "iic/SenseVoiceSmall-onnx",
            "onnx_repository_zh": "官方 SenseVoiceSmall 量化 ONNX（开放神经网络交换格式）模型仓库",
            "onnx_revision": SENSEVOICE_REVISION,
            "onnx_revision_zh": "官方 ONNX 资产固定提交",
            "upstream_model_repository": "FunAudioLLM/SenseVoiceSmall",
            "upstream_model_repository_zh": "官方 SenseVoiceSmall 原始模型仓库",
            "upstream_model_revision": None,
            "upstream_model_revision_zh": "官方 ONNX 仓库未声明底层 .pt（PyTorch 权重）提交；不可据此声称与本地 3847d57… 权重完全匹配",
            "copied_tokenizer_revision": SENSEVOICE_BASE_REVISION,
            "copied_tokenizer_revision_zh": "仅为当前 funasr_onnx（FunASR ONNX 推理包）加载器所需 BPE（子词分词器）文件的来源版本，不表示 ONNX 权重来源版本",
            "files": files,
            "files_zh": "候选文件的来源、固定修订、字节数与校验值",
            "note_zh": "模型量化 ONNX 权重来自官方 iic 仓库固定提交；配置与 am.mvn 哈希恰与项目所固定的原始模型相同，但上游未记录 ONNX 权重所由之 .pt 提交，故保留为独立候选、不覆盖生产模型且不宣称精确权重匹配。",
        })
        final_staging = Path(temp) / "commit"
        os.replace(staging, final_staging)
        os.replace(final_staging, destination)
    return destination


def main() -> int:
    vad = prepare_vad_candidate()
    print(f"FSMN-VAD 候选已准备：{vad}")
    sensevoice = prepare_sensevoice_candidate()
    print(f"SenseVoice ONNX 候选已准备：{sensevoice}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
